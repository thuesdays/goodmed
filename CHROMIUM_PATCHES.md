# Ghost Shell — Chromium C++ Patches Reference

Complete source-of-truth for every patch applied to the Chromium tree.
Use this document to re-apply patches after pulling a new Chromium release.

---

## Architecture overview

Three layers of interception:

| Layer | Process | Files |
|-------|---------|-------|
| Browser-process config | Browser (main) | `chrome/browser/chrome_content_browser_client.cc`, `chrome/app/chrome_main_delegate.cc` |
| Renderer-process config | Renderer (Blink) | `third_party/blink/renderer/platform/ghost_shell_config.{h,cc}` |
| Call-site patches | Renderer (Blink) | 15+ files in `third_party/blink/renderer/...` |

Data flow:

```
Python DeviceTemplateBuilder
    → generates JSON payload (14 sections)
    → base64-encodes it
    → launches chrome.exe --ghost-shell-payload=<base64>
         │
         ▼
   Browser process
    - GhostBrowserConfig reads payload → serves HTTP UA + Client Hints
    - GhostTzConfig reads payload → sets system TZ + ICU default
    - AppendExtraCommandLineSwitches: propagates --ghost-shell-payload to child processes  ← CRITICAL FIX
         │
         ▼
   Renderer process (Blink, per tab)
    - GhostShellConfig (singleton) reads payload → cached in Blink memory
    - Every call-site patch reads from GhostShellConfig::GetInstance()
```

---

## Build configuration

**`out/GhostShell/args.gn`** (or whatever dir you use):

```gn
# Release-ish build, keeps debug info tolerable
is_debug = false
is_official_build = false
symbol_level = 1

# Skip telemetry / official Google pieces
proprietary_codecs = true
ffmpeg_branding = "Chrome"

# Speed
enable_nacl = false
blink_symbol_level = 0
treat_warnings_as_errors = false

# Target
target_cpu = "x64"
```

Build:
```powershell
gn gen out\GhostShell
autoninja -C out\GhostShell chrome
```

---

# PART 1 — Core config files (new files we add)

## 1.1. `third_party/blink/renderer/platform/ghost_shell_config.h`

Singleton exposed to all Blink call-sites. `PLATFORM_EXPORT` makes it
linkable across Blink targets.

Full source: see `ghost_shell_config.h` in this repo. Key shape:

```cpp
namespace blink {

class PLATFORM_EXPORT GhostShellConfig {
 public:
  static GhostShellConfig& GetInstance();

  bool IsActive() const;

  // Hardware
  int    GetHardwareConcurrency() const;
  double GetDeviceMemory() const;
  String GetPlatform() const;
  String GetUserAgent() const;
  int    GetMaxTouchPoints() const;

  // Languages
  String GetLanguage() const;
  const Vector<String>& GetLanguages() const;

  // Screen
  int    GetScreenWidth() const;
  int    GetScreenHeight() const;
  int    GetAvailWidth() const;
  int    GetAvailHeight() const;
  int    GetColorDepth() const;
  int    GetPixelDepth() const;
  double GetPixelRatio() const;
  String GetOrientationType() const;

  // Graphics
  String GetGLVendor() const;
  String GetGLRenderer() const;
  String GetGPUVendor() const;
  const Vector<String>& GetWebGLExtensions() const;

  // Audio / Noise / Battery / Connection / Fonts / Plugins ...
  // (see full header for all 50+ getters)

 private:
  GhostShellConfig();
  void Initialize();
  // ...
};

}  // namespace blink
```

## 1.2. `third_party/blink/renderer/platform/ghost_shell_config.cc`

Implementation — parses base64 payload from CLI, builds the config once
on first `GetInstance()` call. Safe against missing keys (every field has
a default).

Key parsing flow:

```cpp
void GhostShellConfig::Initialize() {
  base::CommandLine* command_line = base::CommandLine::ForCurrentProcess();
  if (!command_line->HasSwitch("ghost-shell-payload")) {
    is_active_ = false;
    return;
  }
  std::string b64 = command_line->GetSwitchValueASCII("ghost-shell-payload");
  std::string decoded_json;
  if (!base::Base64Decode(b64, &decoded_json)) { is_active_ = false; return; }

  auto root = base::JSONReader::Read(decoded_json, 0);
  if (!root || !root->is_dict()) { is_active_ = false; return; }
  const auto& dict = root->GetDict();
  is_active_ = true;

  if (const auto* hw = dict.FindDict("hardware"))     { /* ... */ }
  if (const auto* langs = dict.FindDict("languages")) { /* ... */ }
  if (const auto* screen = dict.FindDict("screen"))   { /* ... */ }
  // ... 14 sections total
}
```

Full source in `ghost_shell_config.cc`.

## 1.3. Adding the new files to the Blink GN build

Edit **`third_party/blink/renderer/platform/BUILD.gn`** — find `source_set("platform")` or similar target that has `blink_platform_sources`. Add to `sources`:

```gn
sources = [
  # ... existing sources ...
  "ghost_shell_config.cc",
  "ghost_shell_config.h",
]
```

---

# PART 2 — Browser-process patches

## 2.1. `chrome/browser/chrome_content_browser_client.cc`

Three changes — two UA overrides + **one CRITICAL fix** that propagates our
CLI switch to child processes.

### 2.1.1. Headers

Add at top:

```cpp
#include "base/command_line.h"
#include "base/base64.h"
#include "base/values.h"
#include "base/json/json_reader.h"
#include "base/no_destructor.h"
```

### 2.1.2. Local `GhostBrowserConfig` struct

Add near the top of the anonymous namespace:

```cpp
// === GHOST SHELL BROWSER CONFIG ===
struct GhostBrowserConfig {
  bool is_active = false;
  std::string user_agent;
  blink::UserAgentMetadata meta;

  static const GhostBrowserConfig& Get() {
    static base::NoDestructor<GhostBrowserConfig> instance([]() {
      GhostBrowserConfig config;
      base::CommandLine* cmd = base::CommandLine::ForCurrentProcess();
      if (cmd->HasSwitch("ghost-shell-payload")) {
        std::string b64_payload = cmd->GetSwitchValueASCII("ghost-shell-payload");
        std::string json;
        if (base::Base64Decode(b64_payload, &json)) {
          auto parsed = base::JSONReader::Read(json, 0);
          if (parsed && parsed->is_dict()) {
            config.is_active = true;
            const auto& d = parsed->GetDict();

            if (const auto* hw = d.FindDict("hardware")) {
              if (const std::string* ua = hw->FindString("user_agent"))
                config.user_agent = *ua;
            }

            if (const auto* m_dict = d.FindDict("ua_metadata")) {
              if (const std::string* p = m_dict->FindString("platform"))
                config.meta.platform = *p;
              if (const std::string* pv = m_dict->FindString("platform_version"))
                config.meta.platform_version = *pv;
              if (const std::string* a = m_dict->FindString("architecture"))
                config.meta.architecture = *a;
              if (const std::string* m = m_dict->FindString("model"))
                config.meta.model = *m;
              if (const std::string* b = m_dict->FindString("bitness"))
                config.meta.bitness = *b;
              if (const std::string* fv = m_dict->FindString("full_version"))
                config.meta.full_version = *fv;

              config.meta.wow64  = m_dict->FindBool("wow64").value_or(false);
              config.meta.mobile = m_dict->FindBool("mobile").value_or(false);
              config.meta.form_factors = {"Desktop"};

              if (const auto* brands_list = m_dict->FindList("brands")) {
                for (const auto& item : *brands_list) {
                  if (item.is_dict()) {
                    const std::string* bn = item.GetDict().FindString("brand");
                    const std::string* bv = item.GetDict().FindString("version");
                    if (bn && bv)
                      config.meta.brand_version_list.emplace_back(*bn, *bv);
                  }
                }
              }
              if (const auto* full_list = m_dict->FindList("full_version_list")) {
                for (const auto& item : *full_list) {
                  if (item.is_dict()) {
                    const std::string* bn = item.GetDict().FindString("brand");
                    const std::string* bv = item.GetDict().FindString("version");
                    if (bn && bv)
                      config.meta.brand_full_version_list.emplace_back(*bn, *bv);
                  }
                }
              }
            }
          }
        }
      }
      return config;
    }());
    return *instance;
  }
};
// === END GHOST SHELL BROWSER CONFIG ===
```

### 2.1.3. Override `GetUserAgent()` and `GetUserAgentMetadata()`

```cpp
std::string ChromeContentBrowserClient::GetUserAgent() {
  if (GhostBrowserConfig::Get().is_active &&
      !GhostBrowserConfig::Get().user_agent.empty()) {
    return GhostBrowserConfig::Get().user_agent;
  }
  return embedder_support::GetUserAgent();
}

blink::UserAgentMetadata ChromeContentBrowserClient::GetUserAgentMetadata() {
  DCHECK_CURRENTLY_ON(BrowserThread::UI);
  if (GhostBrowserConfig::Get().is_active) {
    return GhostBrowserConfig::Get().meta;
  }
  return embedder_support::GetUserAgentMetadata();
}
```

### 2.1.4. CRITICAL — propagate `ghost-shell-payload` to child processes

Without this, renderer (and GPU/utility) processes won't see the payload
and every Blink-level check will fall back to real hardware values.

Find `ChromeContentBrowserClient::AppendExtraCommandLineSwitches(...)` and
modify `kCommonSwitchNames`:

```cpp
void ChromeContentBrowserClient::AppendExtraCommandLineSwitches(
    base::CommandLine* command_line,
    int child_process_id) {
  // ... existing code ...

  static const char* const kCommonSwitchNames[] = {
      embedder_support::kUserAgent,
      switches::kUserDataDir,
      "ghost-shell-payload",   // Added: propagate to renderer / GPU / utility
  };
  command_line->CopySwitchesFrom(browser_command_line, kCommonSwitchNames);

  // ... rest of original method ...
}
```

> **This one-liner was the difference between `8/13` and `13/13` self-check.**

## 2.2. `chrome/app/chrome_main_delegate.cc`

Two patches — timezone override + standalone launch safe-defaults.

### 2.2.1. Headers

```cpp
#include "base/base64.h"
#include "base/json/json_reader.h"
#include "base/values.h"
#include "base/no_destructor.h"
#include "unicode/timezone.h"
#include "unicode/unistr.h"
```

### 2.2.2. `GhostTzConfig` struct

```cpp
// === GHOST SHELL TZ CONFIG ===
struct GhostTzConfig {
  std::string timezone_id;

  static const GhostTzConfig& Get() {
    static base::NoDestructor<GhostTzConfig> instance([]() {
      GhostTzConfig config;
      base::CommandLine* cmd = base::CommandLine::ForCurrentProcess();
      if (cmd->HasSwitch("ghost-shell-payload")) {
        std::string json;
        if (base::Base64Decode(cmd->GetSwitchValueASCII("ghost-shell-payload"),
                               &json)) {
          auto parsed = base::JSONReader::Read(json, 0);
          if (parsed && parsed->is_dict()) {
            if (const auto* tz_dict = parsed->GetDict().FindDict("timezone")) {
              if (const std::string* id = tz_dict->FindString("id")) {
                config.timezone_id = *id;
              }
            }
          }
        }
      }
      return config;
    }());
    return *instance;
  }
};
// === END GHOST SHELL TZ CONFIG ===
```

### 2.2.3. Apply timezone in `PreBrowserMain()`

```cpp
std::optional<int> ChromeMainDelegate::PreBrowserMain() {
  std::optional<int> exit_code = content::ContentMainDelegate::PreBrowserMain();
  if (exit_code.has_value()) return exit_code;

  // === GHOST SHELL: override timezone for whole process ===
  const std::string& tz_id = GhostTzConfig::Get().timezone_id;
  if (!tz_id.empty()) {
  #if BUILDFLAG(IS_WIN)
    _putenv_s("TZ", tz_id.c_str());
    _tzset();
  #else
    setenv("TZ", tz_id.c_str(), 1);
    tzset();
  #endif
    // Hardcore override for V8 Intl API
    icu::TimeZone* tz = icu::TimeZone::createTimeZone(
        icu::UnicodeString::fromUTF8(tz_id));
    icu::TimeZone::adoptDefault(tz);
  }
  // === END GHOST SHELL ===

  // ... rest of original method ...
}
```

### 2.2.4. Standalone launch safe defaults (NEW)

Fixes the crashpad crash when user double-clicks `chrome.exe`.

In `ChromeMainDelegate::BasicStartupComplete()`, add at the very top:

```cpp
std::optional<int> ChromeMainDelegate::BasicStartupComplete() {
  // === GHOST SHELL: safe defaults for standalone launch ===
  // Crashpad handler can't initialize when chrome.exe is run without
  // automation args. These flags make double-click launches work while
  // being harmless for Selenium.
  {
    base::CommandLine* cmd = base::CommandLine::ForCurrentProcess();
    if (!cmd->HasSwitch("disable-crash-reporter"))
      cmd->AppendSwitch("disable-crash-reporter");
    if (!cmd->HasSwitch("disable-breakpad"))
      cmd->AppendSwitch("disable-breakpad");
    if (!cmd->HasSwitch("no-default-browser-check"))
      cmd->AppendSwitch("no-default-browser-check");
    if (!cmd->HasSwitch("no-first-run"))
      cmd->AppendSwitch("no-first-run");
  }
  // === END GHOST SHELL ===

  // ... rest of original method ...
```

---

# PART 3 — Blink call-site patches

Pattern is identical for every patch: check `IsActive()`, return ghost value
if present, otherwise fall through to original code.

## 3.1. Navigator

### `third_party/blink/renderer/core/execution_context/navigator_base.cc` — userAgent

```cpp
String NavigatorBase::userAgent() const {
  if (GhostShellConfig::GetInstance().IsActive()) {
    String ua = GhostShellConfig::GetInstance().GetUserAgent();
    if (!ua.empty()) return ua;
  }
  ExecutionContext* execution_context = GetExecutionContext();
  return execution_context ? execution_context->UserAgent() : String();
}
```

### `third_party/blink/renderer/core/frame/navigator_concurrent_hardware.cc`

```cpp
unsigned NavigatorConcurrentHardware::hardwareConcurrency() const {
  if (GhostShellConfig::GetInstance().IsActive()) {
    int v = GhostShellConfig::GetInstance().GetHardwareConcurrency();
    if (v > 0) return static_cast<unsigned>(v);
  }
  return static_cast<unsigned>(base::SysInfo::NumberOfProcessors());
}
```

### `third_party/blink/renderer/core/frame/navigator_device_memory.cc`

```cpp
float NavigatorDeviceMemory::deviceMemory() const {
  if (GhostShellConfig::GetInstance().IsActive()) {
    double v = GhostShellConfig::GetInstance().GetDeviceMemory();
    if (v > 0) return static_cast<float>(v);
  }
  return ApproximatedDeviceMemory::GetApproximatedDeviceMemory();
}
```

### `third_party/blink/renderer/core/frame/navigator_language.cc`

```cpp
const Vector<String>& NavigatorLanguage::languages() {
  if (GhostShellConfig::GetInstance().IsActive()) {
    if (!languages_dirty_ && !languages_.empty()) return languages_;
    languages_ = GhostShellConfig::GetInstance().GetLanguages();
    languages_dirty_ = false;
    return languages_;
  }
  // ... original fallback ...
}

AtomicString NavigatorLanguage::language() {
  if (GhostShellConfig::GetInstance().IsActive()) {
    const Vector<String>& langs = languages();
    if (!langs.empty()) return AtomicString(langs.front());
  }
  return AtomicString(languages().front());
}
```

### `third_party/blink/renderer/core/frame/navigator.cc` — misc

* `webdriver()` — keep returning `false` (should be default)
* `maxTouchPoints()` — similar pattern with `GetMaxTouchPoints()`
* `doNotTrack()` — return `null` (default)

## 3.2. Screen / Window

### `third_party/blink/renderer/core/frame/screen.cc`

```cpp
int Screen::width() const {
  if (GhostShellConfig::GetInstance().IsActive()) {
    int w = GhostShellConfig::GetInstance().GetScreenWidth();
    if (w > 0) return w;
  }
  return GetRect(true).width();
}

int Screen::height() const {
  if (GhostShellConfig::GetInstance().IsActive()) {
    int h = GhostShellConfig::GetInstance().GetScreenHeight();
    if (h > 0) return h;
  }
  return GetRect(true).height();
}

int Screen::availWidth() const {
  if (GhostShellConfig::GetInstance().IsActive()) {
    int w = GhostShellConfig::GetInstance().GetAvailWidth();
    if (w > 0) return w;
  }
  return GetRect(false).width();
}

int Screen::availHeight() const {
  if (GhostShellConfig::GetInstance().IsActive()) {
    int h = GhostShellConfig::GetInstance().GetAvailHeight();
    if (h > 0) return h;
  }
  return GetRect(false).height();
}

unsigned Screen::colorDepth() const {
  if (GhostShellConfig::GetInstance().IsActive())
    return static_cast<unsigned>(GhostShellConfig::GetInstance().GetColorDepth());
  return 24;
}

unsigned Screen::pixelDepth() const { return colorDepth(); }
```

### `third_party/blink/renderer/core/frame/local_dom_window.cc`

```cpp
double LocalDOMWindow::devicePixelRatio() const {
  if (GhostShellConfig::GetInstance().IsActive()) {
    double dpr = GhostShellConfig::GetInstance().GetPixelRatio();
    if (dpr > 0.0) return dpr;
  }
  if (GetFrame()) return GetFrame()->DevicePixelRatio();
  return 0.0;
}
```

## 3.3. Canvas / WebGL noise

### `third_party/blink/renderer/modules/webgl/webgl_rendering_context_base.cc`

Inside `GetParameter()` switch, case `UNMASKED_VENDOR_WEBGL` (0x9245) and
`UNMASKED_RENDERER_WEBGL` (0x9246):

```cpp
case WebGLDebugRendererInfo::kUnmaskedRendererWebgl:
  if (ExtensionEnabled(kWebGLDebugRendererInfoName)) {
    if (GhostShellConfig::GetInstance().IsActive()) {
      String v = GhostShellConfig::GetInstance().GetGLRenderer();
      if (!v.empty()) return WebGLAny(script_state, v);
    }
    return WebGLAny(script_state, String(ContextGL()->GetString(GL_RENDERER)));
  }
```

Same pattern for `kUnmaskedVendorWebgl` using `GetGLVendor()`.

### `third_party/blink/renderer/modules/canvas/canvas2d/base_rendering_context_2d.cc`

In `getImageDataInternal(...)` after the snapshot is produced:

```cpp
// === GHOST SHELL CANVAS NOISE ===
if (image_data && GhostShellConfig::GetInstance().IsActive()) {
  int shift = GhostShellConfig::GetInstance().GetCanvasShift();
  if (shift > 0) {
    SkPixmap pixmap = image_data->GetSkPixmap();
    if (pixmap.addr() && pixmap.computeByteSize() > 0) {
      unsigned char* pixels = static_cast<unsigned char*>(pixmap.writable_addr());
      size_t len = pixmap.computeByteSize();
      for (size_t i = 0; i + 3 < len; i += 4) {
        pixels[i] ^= static_cast<unsigned char>(shift & 0x03);
      }
    }
  }
}
// === END ===
```

### `third_party/blink/renderer/core/dom/element.cc`

In `getBoundingClientRect()` before returning:

```cpp
if (GhostShellConfig::GetInstance().IsActive()) {
  double off = GhostShellConfig::GetInstance().GetRectOffset();
  if (off != 0.0) {
    result->setX(result->x() + off);
    result->setY(result->y() + off);
  }
}
```

## 3.4. Audio

### `third_party/blink/renderer/modules/webaudio/audio_buffer.cc`

Add field to the class (`audio_buffer.h` if needed):
```cpp
bool noise_applied_ = false;
```

In `getChannelData()`:

```cpp
if (GhostShellConfig::GetInstance().IsActive() && !noise_applied_) {
  double offset = GhostShellConfig::GetInstance().GetAudioOffset();
  if (offset != 0.0) {
    float* data = channel_array->Data();
    size_t noise_count = std::min(channel_array->length(),
                                   static_cast<size_t>(100));
    for (size_t i = 0; i < noise_count; ++i) {
      data[i] += static_cast<float>(offset);
    }
    noise_applied_ = true;
  }
}
```

### `third_party/blink/renderer/modules/webaudio/audio_context.cc`

```cpp
float AudioContext::sampleRate() const {
  if (GhostShellConfig::GetInstance().IsActive())
    return static_cast<float>(GhostShellConfig::GetInstance().GetAudioSampleRate());
  return BaseAudioContext::sampleRate();
}

double AudioContext::baseLatency() const {
  if (GhostShellConfig::GetInstance().IsActive())
    return GhostShellConfig::GetInstance().GetAudioBaseLatency();
  return base_latency_;
}
```

## 3.5. Fonts / Battery / Network

### `third_party/blink/renderer/platform/fonts/font_cache.cc`

In `GetFontData()`, before loading the font:

```cpp
if (GhostShellConfig::GetInstance().IsActive()) {
  const Vector<String>& allowed = GhostShellConfig::GetInstance().GetFontList();
  if (!allowed.empty()) {
    bool found = false;
    for (const String& name : allowed) {
      if (EqualIgnoringAsciiCase(name, family)) { found = true; break; }
    }
    if (!found && !IsGenericFamily(family)) return nullptr;
  }
}
```

### `third_party/blink/renderer/modules/battery/battery_manager.cc`

```cpp
bool BatteryManager::charging() {
  auto& g = GhostShellConfig::GetInstance();
  if (g.IsActive() && g.HasBattery()) return g.GetBatteryCharging();
  return battery_status_.Charging();
}

double BatteryManager::level() {
  auto& g = GhostShellConfig::GetInstance();
  if (g.IsActive() && g.HasBattery()) return g.GetBatteryLevel();
  return battery_status_.Level();
}

double BatteryManager::chargingTime() {
  auto& g = GhostShellConfig::GetInstance();
  if (g.IsActive() && g.HasBattery()) {
    double t = g.GetBatteryChargingTime();
    return t < 0 ? std::numeric_limits<double>::infinity() : t;
  }
  return battery_status_.charging_time().InSecondsF();
}

double BatteryManager::dischargingTime() {
  auto& g = GhostShellConfig::GetInstance();
  if (g.IsActive() && g.HasBattery()) {
    double t = g.GetBatteryDischargingTime();
    return t < 0 ? std::numeric_limits<double>::infinity() : t;
  }
  return battery_status_.discharging_time().InSecondsF();
}
```

### `third_party/blink/renderer/modules/netinfo/network_information.cc`

```cpp
V8EffectiveConnectionType NetworkInformation::effectiveType() {
  if (GhostShellConfig::GetInstance().IsActive()) {
    String t = GhostShellConfig::GetInstance().GetConnectionEffectiveType();
    if (t == "4g") return V8EffectiveConnectionType(V8EffectiveConnectionType::Enum::k4G);
    if (t == "3g") return V8EffectiveConnectionType(V8EffectiveConnectionType::Enum::k3G);
    if (t == "2g") return V8EffectiveConnectionType(V8EffectiveConnectionType::Enum::k2G);
    if (t == "slow-2g") return V8EffectiveConnectionType(V8EffectiveConnectionType::Enum::kSlow2G);
  }
  // ... original path ...
}

double NetworkInformation::downlink() const {
  if (GhostShellConfig::GetInstance().IsActive())
    return GhostShellConfig::GetInstance().GetConnectionDownlink();
  return downlink_mbps_;
}

int NetworkInformation::rtt() const {
  if (GhostShellConfig::GetInstance().IsActive())
    return GhostShellConfig::GetInstance().GetConnectionRtt();
  return rtt_msec_;
}
```

---

# PART 4 — Reapply checklist after Chromium upgrade

When you `gclient sync` to a newer Chromium release, re-verify:

| File | What to check |
|------|---------------|
| `chrome_content_browser_client.cc` | 3 edits: headers, `GhostBrowserConfig` struct, `kCommonSwitchNames` has `"ghost-shell-payload"`, `GetUserAgent()` + `GetUserAgentMetadata()` overrides |
| `chrome_main_delegate.cc` | 4 edits: headers, `GhostTzConfig`, `PreBrowserMain()` TZ override, `BasicStartupComplete()` crashpad defaults |
| `blink/renderer/platform/BUILD.gn` | Added `ghost_shell_config.h/.cc` to sources |
| `navigator_base.cc` | `userAgent()` override |
| `navigator_concurrent_hardware.cc` | `hardwareConcurrency()` |
| `navigator_device_memory.cc` | `deviceMemory()` |
| `navigator_language.cc` | `languages()`, `language()` |
| `screen.cc` | `width()`, `height()`, `availWidth()`, `availHeight()`, `colorDepth()` |
| `local_dom_window.cc` | `devicePixelRatio()` |
| `webgl_rendering_context_base.cc` | `GetParameter()` unmasked vendor/renderer |
| `base_rendering_context_2d.cc` | `getImageDataInternal()` noise |
| `element.cc` | `getBoundingClientRect()` offset |
| `audio_buffer.cc` | `getChannelData()` noise + `noise_applied_` field |
| `audio_context.cc` | `sampleRate()`, `baseLatency()` |
| `font_cache.cc` | `GetFontData()` filter |
| `battery_manager.cc` | 4 methods |
| `network_information.cc` | `effectiveType()`, `downlink()`, `rtt()` |

All patches follow the same pattern — `IsActive()` check → ghost value → fall through to original. Merge conflicts are almost always trivial.

---

# PART 5 — Diagnostics

## How to know a patch is working

1. Start the monitor
2. Open **Profile detail** → **Self-check** section
3. Should be **13/13 ✓**

If a test fails, inspect `profiles/<name>/selfcheck.json`:

```json
{
  "tests": {
    "ua_matches_payload": false,   ← patch broken
    ...
  },
  "actual_values": {
    "userAgent": "Mozilla/5.0 ... Chrome/132.0.0.0 ...",   ← real value
  },
  "expected_values": {
    "hardware": { "user_agent": "Mozilla/5.0 ... Chrome/132.0.6834.210 ..." }  ← payload
  }
}
```

Compare `actual` vs `expected` to pinpoint which patch didn't land.

## Common issues

| Symptom | Root cause | Fix |
|---------|------------|-----|
| All 5 hardware checks fail | `ghost-shell-payload` not in `kCommonSwitchNames` | PART 2.1.4 above |
| `ua_matches_payload` fails only | UA Reduction freezing version to `.0.0.0` | Add `--disable-features=ReduceUserAgentMinorVersion` |
| `timezone_matches` fails | PART 2.2.3 not applied | Reapply timezone override |
| `screen_width_matches` but `dpr_matches` fails | `local_dom_window.cc` untouched | PART 3.2 |
| Chrome crashes on standalone double-click | No crashpad defaults | PART 2.2.4 |

---

# PART 6 — Build commands

```powershell
# Initial build (40–90 min depending on hardware)
cd f:\projects\chromium\src
gn gen out\GhostShell
autoninja -C out\GhostShell chrome

# Incremental rebuild after editing C++ (typically 30s–5min)
autoninja -C out\GhostShell chrome

# Full clean rebuild (if GN targets changed)
gn clean out\GhostShell
gn gen out\GhostShell
autoninja -C out\GhostShell chrome
```

Output: `out\GhostShell\chrome.exe` — point Ghost Shell's `browser.binary_path` config there.
