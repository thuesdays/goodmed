# Script Engine — Deep Audit & Test-Case Catalog

**Дата:** 2026-04-29  
**Аудит покрывает:** action handlers (49 шт), parse_ads, click_ad с fallback'ами, conditions, variable interpolation, container scoping, watchdog interaction, captcha-rotation chain, race conditions.

**Цель:** до запуска любого скрипта понимать какие edge cases протестированы, какие race conditions защищены, и где живут оставшиеся footguns.

**Метод:** 4 параллельных Explore агента собирали факты + я синтезировал в этот документ.

---

## ЧАСТЬ I — ИНВЕНТАРЬ ACTION HANDLERS

49 handler'ов в трёх категориях:

| Категория | Префикс | Кол-во | Где живут | Диспатч |
|---|---|---|---|---|
| Legacy per-ad | `_act_*` | 25 | runner.py | ACTION_HANDLERS dict + `_legacy_ctx_from` bridge |
| Loop-level | `_loop_*` | 8 | runner.py | LOOP_ACTION_HANDLERS dict |
| Unified flow | `_flow_*` | 16 | runner.py | if/elif chain в `_exec_single` |

### Глобальные параметры

Применяются на dispatcher-уровне (НЕ в самих handler'ах):

| Параметр | Default | Где enforces | Применяется к |
|---|---|---|---|
| `enabled` | true | `_exec_steps:3502`, `run_main_script:2378`, `_run_action_pipeline_for_ad:2481` | Все 49 handler'ов |
| `probability` | 1.0 | Те же 3 dispatcher'а | Все 49 |
| `abort_on_error` | false | Сами handlers в except-блоках | Все 49 |
| `skip_on_my_domain` | false | Legacy pipeline + `_exec_single` bridge | Только legacy `_act_*` внутри foreach_ad |
| `skip_on_target` | false | Те же | Те же |
| `only_on_target` | false | Те же | Те же |
| `only_on_my_domain` | false | Те же | Те же |

**Footgun:** если handler вызван **напрямую**, минуя dispatcher (например, через legacy entrypoint), `enabled`/`probability` НЕ проверяются. В обычном использовании скриптами это не происходит, но любой кастомный код снаружи runner.py — да.

---

## ЧАСТЬ II — PARSE_ADS DEEP-DIVE

`main.py:parse_ads(driver, query)` инжектирует ~370-строчный JS, использует TreeWalker + querySelectorAll.

### Селекторы по форматам

| Формат | Селекторы | Идентификация |
|---|---|---|
| **text** | DOM walk-up 8 уровней от "Sponsored"/"Реклама" markers | `div[data-text-ad]` или sponsored-badge |
| **shopping_carousel** | `.pla-unit, .mnr-c.pla-unit, g-inner-card.mnr-c, div[data-docid][data-pla], div.KZmu8e, div.cu-container div[data-docid]` | `data-dtld` атрибут (merchant domain) |
| **pla_grid** | `.commercial-unit-desktop-top, .commercial-unit-desktop-rhs, .cu-container.cu-container-unit` | parent имеет commercial-unit class |

### Anchor stamping (`anchor_id`)

- Формат: `gs-{scanId}-{counter}` где `scanId = Math.random().toString(36).slice(2,10)` (8 символов)
- Когда: внутри `extractFromBlock()`, **сразу после** выбора primary anchor
- Куда: `setAttribute('data-gs-ad-id', id)` на `<a href>`
- Dedup: новый scanId на каждый scan → нет коллизий с прошлыми stamping'ами

### URL extraction priority

1. **`google_click_url`** — первая `a[href]` с `/aclk?` или `googleadservices.com`
2. **`clean_url`** — приоритет атрибутов:
   - `data-pcu` (canonical destination)
   - `data-pla-url, data-href, data-url, data-target-url`
   - Fallback: `data-rh, data-pcuw, data-pcuwe` (bare host → `https://`)
   - Visible text scan: regex `^[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:\/[^\s]*)?$/i`
   - Last resort: первая `a[href^="http"]` НЕ google.com/search
3. **`display_url`** (зелёный текст) — `<cite>` или `.VuuXrf, .x2VHCd, .LbUacb, .E5ocAb, .aULzUe`, или `data-dtld`

### Filter pipeline (Python-side, после JS)

| Шаг | Логика | Файл/строка |
|---|---|---|
| google-internal | `domain` содержит google.com / google.ua / googleusercontent.com / googlesyndication.com | main.py:1318 |
| **own-domain (substring!)** | `any(my.lower() in domain.lower() for my in MY_DOMAINS)` — НЕ exact, НЕ subdomain — **substring** | main.py:1330 |
| dedup в рамках query | `if domain in seen_domains: skip` | main.py:1339 |

**Found bug**: own-domain filter использует `"in"` substring → `MY_DOMAINS=["goodmedika.com.ua"]` блокирует `subdomain.goodmedika.com.ua` ✓ (intended), НО также `goodmedika.com.ua.evil.com` ✗ (false positive). И `?ref=goodmedika.com.ua` в URL competitor'а тоже заблокирует.

---

## ЧАСТЬ III — CLICK_AD: 3-TIER LADDER + 3 FALLBACKS

### Anchor relocation cascade

```
ANCHOR_ID lookup       (CSS selector: a[data-gs-ad-id="..."])
        ↓ fails
URL fragment match     (CSS selector: a[href*="<url[:80]>"]) + own-domain check
        ↓ fails
DOMAIN-match JS scan   (querySelectorAll all anchors, filter by domain + /aclk + not own + not maps)
        ↓ fails
LOG warning, NO CLICK
```

### Click ladder (после успешного relocation)

```
ActionChains Ctrl+Click       (key_down(CTRL) + click + key_up)
        ↓ ElementNotInteractableException (0×0, hidden, off-screen)
Native element.click()        (Selenium API)
        ↓ Same exception
JS .click()                   (driver.execute_script("arguments[0].setAttribute('target','_blank'); arguments[0].click()"))
        ↓ JS exception
GIVE UP, log + return
```

### Post-click safety nets

| Safety | Action | Условие |
|---|---|---|
| Own-domain bail-out | Close tab без dwell | `_href_is_own(landed_url)` (substring match) |
| Maps/Google-internal bail-out | Close tab без dwell | URL contains `google.com/maps`, `maps.google.com`, `google.com/url?`, `google.com/aclk?...&redirect` |
| Mid-dwell health probe | Try-switch на original SERP, return | `current_url` raises NoSuchWindowException (tab vanished) |
| Window-handles guard | Bail или fallback на handles[0] | `window_handles` raises |

---

## ЧАСТЬ IV — CONDITIONS TRUTH TABLES

| Kind | True когда | False когда | None/Error |
|---|---|---|---|
| `always` | unconditional | — | — |
| `never` | — | unconditional | — |
| `ad_is_competitor` | domain ≠ my AND not is_target | is_target=true OR domain в my OR ad=None | ctx.ad=None → False |
| `ad_is_external` | domain ≠ my (loose: competitor OR target) | domain в my OR ad=None | ctx.ad=None → False |
| `ad_is_target` | `(ctx.ad or {}).get("is_target")==true` | flag missing/false | ctx.ad=None → False |
| `ad_is_mine` | domain в my (substring + subdomain suffix) | else | ctx.ad=None → False |
| `ads_found` | `len(ctx.ads) > 0` | `len()==0` | ads=None → False |
| `no_ads` | `len(ctx.ads)==0` | `len()>0` | ads=None → True |
| `ads_count_gte` | `len >= cond.value` | `len < value` | non-int value → False |
| `captcha_present` | `ctx.flags["captcha_present"]==True` | flag missing/false | Initialized False |
| `url_contains` | `needle in current_url` | else | driver=None → False; exception → False |
| `element_exists` | `len(find_elements) > 0` | else | driver=None → False; default timeout 1s; exception → False |
| `var_equals` | `str(resolve(var)) == str(interp(value))` | else | None → "None" string |
| `var_contains` | `rhs in lhs` (str coerce, "" default) | else | None → "" |
| `var_matches` | `regex.search(pattern, lhs)` | no match | re.error → False |
| `var_empty` | `not bool(resolve(var))` | truthy | None/""/0/[]/{} → True |
| `and` | `all(sub-conditions)` | any False | empty list → True |
| `or` | `any(sub-conditions)` | all False | empty list → False |

**Noteable:** `and`/`or` НЕ short-circuit — все sub-conditions evaluated.

---

## ЧАСТЬ V — VARIABLE INTERPOLATION

### Resolution order (RunContext.resolve_path)

1. `head=="ad"` → `self.ad or {}` + dotted traverse
2. `head=="ads"` → если rest[0]=="count" возвращает `len()`, иначе list + integer indexing
3. `head=="item"` → `self.item` напрямую
4. `head=="var"` → `self.vars[rest[0]]` + nested dict traverse
5. `head=="query"` → `self.query`
6. `head=="profile"` → `self.profile_name`
7. `head=="flag"` → `self.flags.get(rest[0])`
8. **Unknown head** → fallback `self.vars.get(head)` + traverse

### None-handling

- None → `""` (empty string) после `_VAR_PATTERN.sub`
- True → `"True"`, 1 → `"1"`
- Missing path silently empties

### Особые пути

- `{ad.is_target}` → `"True"` или `"False"` (bool→str coercion)
- `{ads.count}` → integer length, не list
- `{ads.0}` → ads[0] (integer indexing)
- `{var.foo.bar.baz}` → arbitrary depth nested dict traverse, breaks on non-dict → None → ""
- `{vault.<id>.<field>}` → `self.vars["vault"]` populated at root init from env `GHOST_SHELL_VAULT_RESOLVED`

### `_NESTED_STEPS_KEYS` protection

`{"steps", "then_steps", "else_steps"}` — НЕ interpolated при walk через container'ы. Это защита от преждевременной resolve'ы placeholder'ов в child scope (без неё `{item}` в inner step'е резолвился бы к "" на parent scope где item=None).

---

## ЧАСТЬ VI — WATCHDOG MECHANICS

| Параметр | Значение |
|---|---|
| Probe interval | 30s |
| Probe operation | `driver.title` через deadline thread |
| Probe timeout | 20s per attempt |
| Fail threshold | 3 consecutive |
| Kill action | `kill_process_tree` на chromedriver PID |
| Pause/resume | `_watchdog_pause` Event |

### Известные callers `watchdog_pause`

- `force_rotate_ip` (60s wait)
- `check_and_rotate_if_burned` (60s wait, добавлено в audit #105)

### Operations >10s БЕЗ pause (potential mid-action kill)

| Операция | Длительность | Файл |
|---|---|---|
| `click_ad` dwell | 6-18s | runner.py:497 |
| `commercial_inflate` per-query | 8-15s × 2-3 = 30-60s total | runner.py:1419 |
| `search_query` navigation | 15s timeout | main.py:1547 |
| `import_storage` | 12s | session/manager.py:196 |
| `fill_form` 50 fields | 4-12s | runner.py:1656 |
| `wait_for_url` polling | до 15s default | runner.py:1855 |
| `extract_text` JS | unbounded | runner.py:1663 |
| `driver.back()` после click_organic | до 300s default | runner.py:1590 |
| `visit_external_fp_tester` (creepjs/pixelscan) | 10-30s | runner.py |

---

# ЧАСТЬ VII — ТЕСТ-КЕЙСЫ И RACE CONDITIONS (230+)

## A. Action Dispatch (test cases #1-30)

1. Step с `enabled=false` — должен skip, не dispatch
2. Step с `enabled=true, probability=0.5`, RNG returns 0.7 — skip
3. Step с `probability=0.0` — всегда skip (`random()>0` ≈ всегда true)
4. Step с `probability=1.0` — всегда run (short-circuit `prob<1.0`)
5. Step с `probability=2.0` (out of range) — всегда run (short-circuit)
6. Step с `probability=-0.5` (negative) — всегда run (short-circuit)
7. Step без `enabled` field — default true, runs
8. Step без `probability` field — default 1.0, runs
9. Step с `type` неизвестного типа — log warning "unknown action type", continue
10. `_exec_steps` встречает step с `should_break=True` в ctx — exit loop
11. `_exec_steps` встречает step с `should_continue=True` в ctx — exit (handled by outer loop)
12. Container step с пустым `steps:[]` — no-op
13. `if` step без `condition` — defaults to "always", runs then_steps
14. `if` step с `condition.kind="never"` — runs else_steps
15. `if` step с условием возвращающим non-bool (string "ok") — bool() coerces, "ok" → True
16. `if` step с условием возвращающим None — bool(None) → False, runs else_steps
17. Параллельные steps — runner НЕ thread-spawn'ит (single-threaded), no race
18. `_exec_steps` обработка mid-iteration step list mutation — НЕ происходит, list snapshot
19. Probability gate fires `random()` ОДИН РАЗ на step — не на каждом sub-condition
20. Watchdog heartbeat вызван перед каждым step (`dog.heartbeat()` line 3508)
21. Watchdog heartbeat не вызван если `loop_ctx.get("watchdog")` is None — defensive, ok
22. `step["enabled"]` is integer 0 — falsy, skip ✓
23. `step["enabled"]` is string "false" — truthy! runs (footgun, but unlikely from JSON)
24. Step с `_comment` field — preserved через interpolate, не trigger'ит warning
25. Container `foreach_ad` без ads — early return `[foreach_ad] skipped — no ads in context`
26. Container `foreach` с `items=""` (empty string) — splits to [], early return
27. Container `foreach` с `items="a\nb\n"` — trailing empty line filtered by `if l.strip()`
28. Container `foreach` с `shuffle=true` — `random.shuffle(raw_items)` mutates list, ok per-iteration determinism not guaranteed
29. `loop` (legacy alias) → calls `_flow_foreach` через `_flow_loop_legacy`
30. `break` step set `ctx.should_break=True` — caught by parent loop, ALSO cleared after loop exits (audit #103 fix)

## B. Parse_ads (test cases #31-60)

31. SERP с 0 ads, organic=21 — log "0 candidates"
32. SERP с captcha (recaptcha iframe) — log warning, return []
33. SERP с "did you mean" suggestion — detected, included в diagnostics
34. SERP с 0 organic results — detected, "did not match any documents" parsed
35. Page navigated mid-parse — JS captures snapshot at exec instant, returns stale data (no sync)
36. JS exception в injected code — caught at line 1155, return []
37. Anchor stamping: same scan re-parsed, no collisions (new scanId)
38. Cross-scan: 2 scans → 2 different scanIds → 2 sets of stamps в DOM
39. Shopping carousel с `data-dtld="evil.com"` — short-circuit picks merchant domain
40. Shopping carousel БЕЗ `data-dtld` — fallback на generic URL extractor
41. Text ad с "Sponsored" в .org класс content — false positive walk-up?
42. Text ad localization "Реклама" / "Sponsored" — both detected
43. Own-domain filter: `goodmedika.com.ua` blocks `goodmedika.com.ua.evil.com` ✗ (false positive)
44. Own-domain filter: `?ref=goodmedika.com.ua` в URL competitor'а — substring match блокирует, false positive
45. Own-domain filter: `goodmedika.com.ua` blocks `subdomain.goodmedika.com.ua` ✓ (intended)
46. Google-internal filter: `googleadservices.com` НЕ заблокирован (it's where /aclk lives) ✓
47. Google-internal filter: `gstatic.com` — not blocked (CDN, ok)
48. Google-internal filter: `google-analytics.com` — not blocked (could be a problem if domain matches in cleared list)
49. Dedup: same domain в text + shopping_carousel → one entry (text wins, first seen)
50. Dedup: same domain across queries → kept (no cross-query dedup)
51. anchor_id stamped, page navigates SPA-style (history.pushState), DOM kept — anchor still findable ✓
52. anchor_id stamped, page hard-reloads — anchor wiped, fallback chain triggers
53. anchor_id stamped, page Content-Script injects iframe over SERP — anchor still in main frame DOM
54. clean_url extraction: `data-pcu="https://example.com/?utm=..."` — full URL preserved
55. clean_url extraction: `data-rh="example.com"` (bare host) — synthesized to `https://example.com`
56. clean_url extraction: nothing matches — visible-text regex tries hostname extraction
57. clean_url extraction: all fail — returns empty, ad still recorded with `display_url` only
58. Local Pack ads: separate "View on Maps" anchor in same container — parser may stamp wrong one
59. Empty ad title (rare) — preserved, blank string in result
60. Ad title with non-ASCII (Cyrillic, emoji) — UTF-8 round-trip safe through JS string

## C. Click ladder (test cases #61-95)

61. anchor_id lookup succeeds — happy path, single click
62. anchor_id lookup fails (`NoSuchElementException`), URL fragment fallback succeeds — log debug "fell back to URL match"
63. URL fragment match returns own-domain anchor — abort, log "URL-match fallback hit own domain"
64. URL fragment exists but multiple anchors match — picks first, may be wrong ad
65. URL fragment fails (URL has time-sensitive params) → domain-match JS scan succeeds
66. Domain-match scan finds 2+ anchors — picks first match
67. Domain-match scan finds anchor going through `/aclk` — passes ad-anchor filter
68. Domain-match scan finds organic result with same domain — filtered out (no /aclk)
69. Domain-match scan finds anchor pointing to `google.com/maps` — filtered out
70. All 3 fallbacks fail — log warning, NO click
71. ActionChains Ctrl+Click on 0×0 element → ElementNotInteractableException → fallback native
72. Native click on 0×0 element → ElementNotInteractableException → fallback JS
73. JS click() — bypasses Selenium geometry check, succeeds
74. JS click() with `target=_blank` set — opens in new tab ✓
75. JS click() but Chrome popup blocker closes new tab instantly — `tabs` empty, no dwell
76. Click succeeds, lands on own domain (302 redirect) — close tab без dwell ✓
77. Click succeeds, lands on `google.com/maps` — close tab без dwell ✓
78. Click succeeds, lands on `google.com/aclk?...redirect` — close tab без dwell ✓
79. Click succeeds, valid landing — proceed to dwell
80. Mid-dwell, antifraud script на landing site вызывает `window.close()` — health probe catches, return to SERP
81. Mid-dwell, original SERP tab also closed (user manually) — fallback на `handles[0]`
82. Click succeeds но `tabs=[]` (Ctrl key not registered, opened same-tab) — no switch_to.window, dwell on SERP itself
83. `close_after=false` — tab stays open after dwell, returns to SERP via switch_to(original)
84. `scroll_after_click=false` — no `_human_scroll` after click
85. `deep_dive=true, depth=2` — clicks 2 internal links на landing site
86. `deep_dive=true, depth_min=1, depth_max=3` — random.randint, picks 1-3
87. `deep_dive` finds no internal links — break, proceed to close
88. `deep_dive` link points off-host (Facebook, Twitter) — filtered out by host check
89. `deep_dive` link is "Cart" / "Корзина" — filtered out by text regex
90. `deep_dive` link target removed `target=_blank` — keeps navigation in-tab
91. `deep_dive` clicked link goes to 404 — no recovery, dwell on 404 page
92. `dwell_min=0, dwell_max=0` — `_random_sleep(0, 0)` — no sleep
93. `dwell_min=18, dwell_max=6` — `if hi<lo: hi=lo` defensive normalization
94. `dwell_min=300, dwell_max=600` — 5-10 minute dwell, watchdog WILL kill (no pause)
95. anchor.scrollIntoView() fails (element gone after stamping) — `find_element` raises before scroll, caught in fallback chain

## D. Conditions (test cases #96-130)

96. `if always` → always runs then_steps
97. `if never` → always runs else_steps
98. `if ads_found` ctx.ads=[] — runs else_steps
99. `if ads_found` ctx.ads=None — runs else_steps (None treated as [])
100. `if no_ads` ctx.ads=[] — runs then_steps
101. `if no_ads negate=true` ctx.ads=[] — runs else_steps (negate inverts)
102. `if ads_count_gte value=5` ctx.ads has 3 — False, else
103. `if ads_count_gte value="abc"` (non-int) — int() raises ValueError → False (caught)
104. `if ad_is_competitor` ctx.ad=None — False, else
105. `if ad_is_competitor` ad has is_target=true — False (excludes targets, strict)
106. `if ad_is_external` same ad with is_target=true — True (loose: includes targets)
107. `if ad_is_target` ad without is_target field — False
108. `if ad_is_mine` substring match: my=["a.com"], ad.domain="a.com" — True
109. `if ad_is_mine` substring match: my=["a.com"], ad.domain="x.a.com" — True (subdomain endswith)
110. `if ad_is_mine` substring match: my=["a.com"], ad.domain="a.comains" — True (substring) ✗ false positive
111. `if captcha_present` после search_query на чистом SERP — False
112. `if captcha_present` после search_query на recaptcha-заблоченной странице — True (audit #103 fix)
113. `if url_contains value=""` — bool("")=False, returns False
114. `if url_contains value="{ad.domain}"` ctx.ad=None — interpolates to "", returns False
115. `if element_exists selector=".my-button" timeout=2` — overrides 30s implicit_wait, restores after
116. `if element_exists selector=""` — empty, returns False
117. `if element_exists` driver=None — False
118. `if var_equals var="missing" value="x"` — None=="x" → "None"!="x" → False
119. `if var_contains var=None value="test"` — "" contains "test" → False
120. `if var_empty var="missing"` — None → not bool(None) → True
121. `if var_matches regex="[0-9]+" var="abc"` — no match → False
122. `if var_matches regex="invalid["` — re.error → False (caught)
123. `if and conditions=[A, B, C]` все True — True (all evaluated)
124. `if and conditions=[A, B(False), C]` — False но C ВСЁ ЕЩЁ evaluates (no short-circuit)
125. `if or conditions=[A(False), B(True), C]` — True но C ВСЁ ЕЩЁ evaluates
126. `if and conditions=[]` — `all([])`=True
127. `if or conditions=[]` — `any([])`=False
128. `if negate=true kind="always"` — False
129. `if negate=true kind="and" conditions=[True,True]` — False (negate inverts True→False)
130. `if` без `condition` field — defaults to `kind="always"`, runs then_steps

## E. Variable Interpolation (test cases #131-160)

131. `"{item}"` ctx.item="apple" — "apple"
132. `"{item}"` ctx.item=None — ""
133. `"{ad.domain}"` ctx.ad={"domain":"x.com"} — "x.com"
134. `"{ad.domain}"` ctx.ad=None — "" (None root → empty traverse)
135. `"{ad.is_target}"` ad.is_target=True — "True"
136. `"{ad.is_target}"` ad.is_target=False — "False"
137. `"{ads.count}"` ctx.ads=[1,2,3] — "3"
138. `"{ads.count}"` ctx.ads=[] — "0"
139. `"{ads.count}"` ctx.ads=None — "0" (None or [] → len([])=0)
140. `"{ads.0}"` ctx.ads=[a,b,c] — str(a)
141. `"{ads.5}"` ctx.ads has 3 items — IndexError caught → None → ""
142. `"{var.foo}"` vars["foo"]="bar" — "bar"
143. `"{var.foo.bar}"` vars["foo"]={"bar":"baz"} — "baz"
144. `"{var.foo.bar.baz.qux}"` arbitrary depth — traverses, breaks on non-dict → None → ""
145. `"{var.missing}"` — None → ""
146. `"{var.foo.0}"` vars["foo"]=[10,20,30] — "10" (integer indexing)
147. `"{vault.42.username}"` vault unset (env empty) — "" (silent fallback)
148. `"{vault.42.username}"` vault populated, no item id=42 — "" (None)
149. `"{vault.42.username}"` populated, id=42 has username field — actual value
150. `"{query}"` ctx.query="goodmedika" — "goodmedika"
151. `"{profile}"` ctx.profile_name="profile_01" — "profile_01"
152. `"{flag.captcha_present}"` flag=True — "True"
153. `"{flag.unknown_flag}"` — None → ""
154. `"{unknown_root}"` — fallback на vars.get("unknown_root") → None → ""
155. `"{custom_var}"` (set via item_var="custom") — vars["custom"]=item_value
156. Nested step с `"steps":[...]"` — НЕ interpolated (NESTED_STEPS_KEYS protected)
157. Nested step с `"then_steps":[...]"` — НЕ interpolated
158. Nested step с `"else_steps":[...]"` — НЕ interpolated
159. Step has BOTH `"steps"` AND other params — only "steps" preserved, остальные resolved
160. Multiple `{...}` в одной строке: `"a {x} b {y}"` — both resolved

## F. Container scoping + break/continue (test cases #161-180)

161. `foreach_ad` with `break` inside — exits loop, ctx.should_break cleared post-loop
162. `foreach_ad` with `continue` inside — current iteration ends, next starts, child.should_continue cleared
163. Nested: `foreach_ad { if (cond) { break } }` — break exits foreach_ad, NOT outer scope
164. Nested: `foreach { foreach_ad { break } }` — inner foreach_ad breaks, outer foreach continues
165. `break` outside any loop — sets ctx.should_break, but no loop to consume; harmless
166. `foreach` shares `vars` with parent (intended) — save_var inside loop visible after loop
167. `foreach` does NOT share `ad`, `ads`, `item` (child override)
168. `foreach` `item_var="query"` — child.vars["query"]=item, accessible as `{var.query}` AND `{query}`(? no, `{query}` is ctx.query)
169. `foreach` `item_var="item"` (default) — only child.item set, не vars
170. `foreach_ad` `shuffle=true` — random order across iterations
171. `foreach_ad` `limit=3` — only first 3 ads
172. `foreach_ad` `limit=3 shuffle=true` — shuffles first, then limits (random 3)
173. `foreach_ad` `scan_between_ads=true` (default) — pause+scroll between iterations
174. `foreach_ad` `scan_between_ads=false` — no pause
175. `foreach_ad` `scan_dwell_min=10 scan_dwell_max=5` — auto-normalize
176. `foreach_ad` empty ads — log "skipped — no ads in context"
177. `foreach_ad` 1 ad — no scan_between (i>1 condition)
178. `if cond { foreach { break } }` next sibling step inside `then_steps` after foreach — runs ✓ (audit #103 fix clears break flag)
179. `loop` legacy alias — wraps `foreach`, same behavior
180. `child.should_break = True` after iteration — propagated to parent ctx, parent loop terminates

## G. Captcha → Rotation → Warmup chain (test cases #181-200)

181. search_query lands on recaptcha — `is_captcha_page` returns True, ctx.flags["captcha_present"]=True
182. Captcha detected, captcha_counter incremented (per profile, persistent)
183. Captcha counter <3, normal rotation flow: pause watchdog, force_rotate, wait_for_rotation 60s, resume
184. Captcha counter ==3 (Recovery #4 trigger): regenerate fingerprint, write to DB, NEXT run uses new FP
185. Recovery #1: pre-record IP at run start (ip_record_start) — old_ip never None when rotation starts
186. Recovery #1 fallback: live IP probe fails, last_known_ip used (audit #105 fix)
187. Recovery #2: watchdog paused for 60s wait_for_rotation в force_rotate_ip
188. Recovery #2: watchdog paused для check_and_rotate_if_burned (audit #105 fix)
189. Recovery #3: post-rotation warmup visits 6-domain pool sequentially
190. Recovery #3 fallback: all 6 domains fail (geo-blocked) → google.com/robots.txt visit (audit #105 fix)
191. wait_for_rotation receives same IP from provider 3 consecutive times → early bail (audit #105 fix)
192. wait_for_rotation timeout 60s — returns None, warning logged, run continues with old IP
193. Provider returns 401 — error logged with "your token expired" hint (audit #105 fix)
194. Provider returns 429 — rate-limit warning, retry next cadence (audit #105 fix)
195. Provider returns 5xx — transient warning, retry next rotation
196. Provider returns 200 but new_ip == old_ip — early bail or 60s timeout (depending on streak)
197. Geo-validation post-rotation: new IP в правильной стране — proceed
198. Geo-validation: new IP в неверной стране — log warning (но не trigger another rotation)
199. WebRTC leak check post-rotation — currently NOT performed (audit #105 finding #9, deferred)
200. captcha persists через 3 rotations — Recovery #4 fingerprint regen triggered

## H. Cross-component race conditions (test cases #201-230)

201. Two profiles share rotating proxy URL, both trigger rotation simultaneously — provider throttles, one succeeds, loser времени out 60s. **Loser's behavior**: returns None, run continues on cold/old IP. Recovery is per-profile so no inter-profile sync.

202. Rotation triggered mid-search_query — `search_fn(q)` callback continues executing on stale driver during 60s pause; затем search returns ads but they were collected through OLD IP. ctx.ads has stale data.

203. Profile A rotates, profile B's run mid-launch through same proxy — B sees A's new IP. Defensive: per-profile, не race per se but data consistency: ip_record_start writes new IP to A's history, B будет использовать его на следующей рекорда ip_history (но не текущего run).

204. `rotate_every_n_runs` counter: run 9 crashes — counter incremented in run_start (BEFORE run completes), so next run is 11, not 10. `11%10==1` → still triggers rotation.

205. Click_ad on burned IP — captcha appears in landing page (rare), ctx.flags["captcha_present"] NOT updated by click_ad (only by search_query/catch_ads)

206. foreach_ad iteration 5/12 — Chrome OOM kills tab — `current_url` raises NoSuchWindowException → click_ad returns gracefully → foreach_ad scan-pause тоже raises (но caught) → next iteration starts on new SERP handle (which doesn't exist) → foreach_ad bails

207. concurrent regenerate_fingerprint API call + active run for same profile — DB UPDATE demote + INSERT new (now wrapped in BEGIN IMMEDIATE per audit #106) — atomic, no row corruption. Active run uses OLD payload (saved at run start), новый picked up only on NEXT run.

208. Watchdog about to kill (probe failed 3×), главный thread сейчас в `commercial_inflate` 8s dwell — kill happens mid-dwell, run aborts. **Mitigation**: pause watchdog для commercial_inflate not currently done. Footgun.

209. Watchdog probe at 30s mark coincides with `driver.title` blocking due to CDP backlog — probe fails (20s timeout), 2 more probes fail, kill. **Cause**: driver heavily loaded by parallel JS execution.

210. `import_storage` 12s timeout coincides with watchdog probe at 30s — `driver.get(localStorage_seed_url)` blocks, watchdog probes, may align with kill threshold. Actually import_storage сам wraps в `try driver.set_page_load_timeout(12)` — but watchdog probe runs in separate thread, can still fire.

211. fill_form 50 fields × 60ms = 3s typing + 30 inter-field waits × 300ms = 9s = 12s total. Within watchdog probe window (30s) but if fields trigger JS validation (5s each blocking), could spike to 60s. No watchdog pause.

212. extract_text JS execution hangs (infinite loop in custom JS by user) — `driver.execute_script` blocks indefinitely, watchdog probes fail, kill triggered.

213. wait_for_url polling default 15s — CDP `current_url` could hang on each poll. If hang aligns with watchdog window, kill.

214. driver.back() after click_organic in commercial_inflate — no explicit timeout, default 300s. Run could stall 5 minutes here без watchdog firing (driver.title still responsive). But total wall-clock far exceeds expected dwell.

215. visit_external_fp_tester (creepjs/pixelscan) — site itself takes 10-30s to scan, unpaused. Watchdog probes during this, driver.title responsive (page loads OK), but script's expected dwell ratchets up.

216. screenshot save (driver.save_screenshot) — slow on large viewport or slow disk. Blocks unpaused. Unlikely to align with watchdog kill, but adds run time.

217. Two dashboards opened in two browsers, both trigger manual `Run` for same profile — `_SPAWN_LOCK` (audit #102 fix) serializes, second gets 409.

218. Scheduler tick coincides with dashboard manual run — same `_SPAWN_LOCK` mitigation, no race.

219. Profile delete clicked while run active for that profile — DB delete cascades remove profile row, but active run continues until natural exit. Run's last_run cleanup tries to UPDATE profile row that doesn't exist — `cur.rowcount==0`, no-op. Heartbeat updates to runs table still work.

220. Bulk-create still in progress (mid-fingerprint-generation), user clicks Run on that profile — `profile_is_ready=False` rejects with "setup pipeline incomplete" (audit #93 + #115 self-heal handle the false-positive case).

221. Save_var with `name="ad"` — rejected by audit #103 fix (RESERVED_KEYS includes "ad")

222. Save_var with `name="my custom var"` (spaces) — rejected by `_VAR_NAME_PATTERN` (audit #103)

223. Save_var with value containing 1MB of text — capped at 100KB by `_cap_var_value` (audit #103)

224. Extract_text on element with 5MB innerText — capped at 100KB

225. http_request to `http://localhost:6379/` — rejected by URL-validation (audit #103)

226. http_request to `http://10.0.0.1:8080/` — rejected (RFC1918)

227. http_request to `https://example.com` returning 500MB JSON — capped at 1MB stream (audit #103)

228. http_request method="DELETE" — passes through (no method whitelist), should add if needed

229. CDP command `Network.setUserAgentOverride` for mobile profile — uses System B `ua_metadata` first, fallback `ua_client_hints` (audit #119)

230. `if ad_is_competitor + only_on_target=true` combination — UI lint warning shows yellow banner before save (audit #100)

---

## ЧАСТЬ VIII — RESIDUAL FOOTGUNS (на следующие итерации)

Ситуации, где код работает но имеет защитные пробелы. Не блокеры, но ровно эти места под нагрузкой создадут отчёты "почему-то не сработало":

1. **own_domain substring filter** (parse_ads + click_ad's `_href_is_own`) — substring match, не subdomain-aware. `"a.com" in "a.com.evil"` → True. Лучше: split on dots + endswith.

2. **Watchdog kill во время `commercial_inflate` / `click_ad` dwell / `fill_form`** — длительные операции (8-30s) НЕ паузят watchdog. На медленной прокси watchdog может убить chrome. Решение: `with watchdog_pause("op-name")` на all operations >10s.

3. **wait_for_url default timeout 15s + watchdog 30s threshold** — рядом, но если CDP заfreezит, оба сходятся. Decoupling: make wait_for_url honor watchdog pause.

4. **visit_external_fp_tester with no per-action timeout** — creepjs/pixelscan могут зависнуть. Должен быть `set_page_load_timeout(45)` per action.

5. **Anchor relocation domain-match scan picks first match** — если 2 ads from same domain rendered on SERP (rare, дедуп ловит), могла бы кликнуть wrong instance.

6. **Captcha detected on click_ad landing site** — не обновляет ctx.flags["captcha_present"]. Если script полагается на этот flag для recovery, он не сработает на post-click captcha.

7. **Parallel http_request в скрипте** — нет пула, каждый блокирует main thread. На медленных webhook'ах это сериализует cumulative.

8. **deep_dive может wander off-host** — JS scan фильтрует по hostname, но если landing page имеет iframe с другого host'а и user clicks через JS event, навигация может уйти в iframe context.

9. **Cron schedule + manual run + scheduled regen — нет global mutex**. Все защищены через `_SPAWN_LOCK` на dashboard'е, но если scheduler и dashboard в разных процессах используют **разные** Python-процесс locks, race возможна. Mitigation: они share `RUNNER_POOL` через DB, что serializes.

10. **fingerprint regen не applies к текущему run'у** — design decision (документировано), но UX-confusing: «нажал Regenerate во время пробега → проба еще через старый FP падает → следующая run проходит». В UI должно быть явное "next run will use new fingerprint".

---

## ИТОГИ

**230 тест-кейсов** покрывают все основные пути исполнения скриптов плюс race conditions. Из них:

- **A (action dispatch):** 30 cases — основные диспатч-условия и параметры
- **B (parse_ads):** 30 cases — парсер ads + filter pipeline
- **C (click ladder):** 35 cases — 3-tier click + 3 fallback levels
- **D (conditions):** 35 cases — все kinds + edge cases
- **E (variable interpolation):** 30 cases — все root-paths + special cases
- **F (container scoping):** 20 cases — break/continue, foreach/foreach_ad/loop
- **G (captcha-rotation chain):** 20 cases — recovery #1-#4 end-to-end
- **H (cross-component races):** 30 cases — concurrent component interactions

Все защиты, фолбеки и рекавери-ветки из предыдущих audit'ов (#102-#106) остаются в силе и покрыты тест-кейсами.

10 residual footguns задокументированы для следующей итерации — они не блокируют production, но создадут confusion-by-design на edge case'ах.
