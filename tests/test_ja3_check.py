"""Tests for ja3_check — TLS fingerprint validation.

Pure-helper unit tests. No live network: verdict_for + parse_probe_response
operate on dicts/strings only. The driver-based probe is mocked via a
fake driver object."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ── parse_probe_response ──────────────────────────────────────

def test_parse_valid_json():
    from ghost_shell.fingerprint.ja3_check import parse_probe_response
    raw = '{"ja3":"cd08e31494f9531f560d64c695473da9","user_agent":"X","ip":"1.2.3.4"}'
    out = parse_probe_response(raw)
    assert out["ja3"] == "cd08e31494f9531f560d64c695473da9"
    assert out["user_agent"] == "X"
    assert out["ip"] == "1.2.3.4"


def test_parse_lowercases_ja3():
    from ghost_shell.fingerprint.ja3_check import parse_probe_response
    raw = '{"ja3":"CD08E31494F9531F560D64C695473DA9"}'
    out = parse_probe_response(raw)
    assert out["ja3"] == "cd08e31494f9531f560d64c695473da9"


def test_parse_empty_string():
    from ghost_shell.fingerprint.ja3_check import parse_probe_response
    assert parse_probe_response("") == {}
    assert parse_probe_response(None) == {}


def test_parse_invalid_json():
    from ghost_shell.fingerprint.ja3_check import parse_probe_response
    assert parse_probe_response("not json") == {}
    assert parse_probe_response("{not_balanced") == {}


def test_parse_json_array_not_object():
    from ghost_shell.fingerprint.ja3_check import parse_probe_response
    assert parse_probe_response('["a", "b"]') == {}


def test_parse_missing_ja3_field():
    from ghost_shell.fingerprint.ja3_check import parse_probe_response
    assert parse_probe_response('{"user_agent": "X"}') == {}


def test_parse_short_ja3_rejected():
    """JA3 is MD5 hex = exactly 32 chars."""
    from ghost_shell.fingerprint.ja3_check import parse_probe_response
    assert parse_probe_response('{"ja3": "tooshort"}') == {}


def test_parse_non_hex_ja3_rejected():
    from ghost_shell.fingerprint.ja3_check import parse_probe_response
    assert parse_probe_response(
        '{"ja3": "GGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGG"}'
    ) == {}


def test_parse_optional_fields_default_to_empty():
    from ghost_shell.fingerprint.ja3_check import parse_probe_response
    raw = '{"ja3": "cd08e31494f9531f560d64c695473da9"}'
    out = parse_probe_response(raw)
    assert out["user_agent"] == ""
    assert out["ip"] == ""
    assert out["tls_version"] == ""


# ── verdict_for ───────────────────────────────────────────────

def test_verdict_no_probe_data_warn():
    from ghost_shell.fingerprint.ja3_check import verdict_for
    v = verdict_for({}, expected_chrome_major=149)
    assert v["ok"] is False
    assert v["level"] == "warn"
    assert "no usable data" in v["reason"]


def test_verdict_no_baseline_for_major_warn():
    from ghost_shell.fingerprint.ja3_check import verdict_for
    probe = {"ja3": "cd08e31494f9531f560d64c695473da9"}
    v = verdict_for(probe, expected_chrome_major=200)  # no baseline
    assert v["ok"] is False
    assert v["level"] == "warn"
    assert "baseline" in v["reason"].lower()
    assert v["actual_ja3"] == "cd08e31494f9531f560d64c695473da9"


def test_verdict_match_returns_ok(monkeypatch):
    """Inject a sentinel baseline because EXPECTED_JA3_BY_MAJOR is
    intentionally empty until users capture real stock-Chrome
    fingerprints (RC-71 audit fix)."""
    from ghost_shell.fingerprint import ja3_check
    sentinel = "a" * 32
    monkeypatch.setitem(ja3_check.EXPECTED_JA3_BY_MAJOR, 149, [sentinel])
    v = ja3_check.verdict_for({"ja3": sentinel}, expected_chrome_major=149)
    assert v["ok"] is True
    assert v["level"] == "ok"


def test_verdict_match_case_insensitive(monkeypatch):
    """Sprint 10.2: Sprint 3.2 cleared EXPECTED_JA3_BY_MAJOR[149] to
    avoid the placeholder-hash regression. Tests that need a known
    sentinel must inject one via monkeypatch."""
    from ghost_shell.fingerprint import ja3_check
    monkeypatch.setattr(
        ja3_check, "EXPECTED_JA3_BY_MAJOR",
        {149: ["cd08e31494f9531f560d64c695473da9"]},
    )
    v = ja3_check.verdict_for(
        {"ja3": "CD08E31494F9531F560D64C695473DA9"},
        expected_chrome_major=149,
    )
    assert v["ok"] is True


def test_verdict_mismatch_critical(monkeypatch):
    """Sprint 10.2: same monkeypatch pattern — provide a sentinel
    EXPECTED so the verdict logic has something to mismatch against."""
    from ghost_shell.fingerprint import ja3_check
    monkeypatch.setattr(
        ja3_check, "EXPECTED_JA3_BY_MAJOR",
        {149: ["cd08e31494f9531f560d64c695473da9"]},
    )
    fake_drift = "00112233445566778899aabbccddeeff"
    v = ja3_check.verdict_for({"ja3": fake_drift}, expected_chrome_major=149)
    assert v["ok"] is False
    assert v["level"] == "critical"
    assert "mismatch" in v["reason"].lower()


def test_verdict_includes_full_expected_list(monkeypatch):
    """When multiple known-good hashes exist for a major, the verdict
    surfaces all of them in expected_ja3."""
    from ghost_shell.fingerprint import ja3_check
    fixtures = ["a" * 32, "b" * 32, "c" * 32]
    monkeypatch.setitem(ja3_check.EXPECTED_JA3_BY_MAJOR, 149, fixtures)
    v = ja3_check.verdict_for({"ja3": "deadbeef" * 4}, expected_chrome_major=149)
    assert v["expected_ja3"] == fixtures


def test_verdict_filters_placeholder_empty_strings():
    """If the EXPECTED list has empty placeholder entries, they
    shouldn't be advertised as valid baselines."""
    from ghost_shell.fingerprint import ja3_check
    # Patch in placeholder list temporarily
    original = ja3_check.EXPECTED_JA3_BY_MAJOR.get(900, [])
    ja3_check.EXPECTED_JA3_BY_MAJOR[900] = ["", None, "shorthex"]
    try:
        v = ja3_check.verdict_for({"ja3": "cd08e31494f9531f560d64c695473da9"},
                                   expected_chrome_major=900)
        # All entries filtered → effectively no baseline → warn
        assert v["level"] == "warn"
    finally:
        if original:
            ja3_check.EXPECTED_JA3_BY_MAJOR[900] = original
        else:
            del ja3_check.EXPECTED_JA3_BY_MAJOR[900]


# ── probe_ja3 with mocked driver ──────────────────────────────

def test_probe_ja3_returns_parsed_result():
    from ghost_shell.fingerprint.ja3_check import probe_ja3
    driver = MagicMock()
    driver.execute_async_script.return_value = (
        '{"ja3": "cd08e31494f9531f560d64c695473da9", "user_agent": "X"}'
    )
    out = probe_ja3(driver)
    assert out["ja3"] == "cd08e31494f9531f560d64c695473da9"


def test_probe_ja3_returns_empty_on_error_marker():
    from ghost_shell.fingerprint.ja3_check import probe_ja3
    driver = MagicMock()
    driver.execute_async_script.return_value = "ERROR:NetworkError"
    assert probe_ja3(driver) == {}


def test_probe_ja3_returns_empty_on_non_string():
    from ghost_shell.fingerprint.ja3_check import probe_ja3
    driver = MagicMock()
    driver.execute_async_script.return_value = None
    assert probe_ja3(driver) == {}


def test_probe_ja3_swallows_driver_exceptions():
    from ghost_shell.fingerprint.ja3_check import probe_ja3
    driver = MagicMock()
    driver.execute_async_script.side_effect = RuntimeError("script timeout")
    assert probe_ja3(driver) == {}


def test_probe_ja3_sets_script_timeout(monkeypatch):
    from ghost_shell.fingerprint.ja3_check import probe_ja3
    driver = MagicMock()
    driver.execute_async_script.return_value = '{"ja3": "' + "a" * 32 + '"}'
    probe_ja3(driver, timeout=7.5)
    driver.set_script_timeout.assert_called_once_with(7.5)


# ── check_ja3_matches_chrome (validator integration) ─────────

def test_validator_check_skips_when_no_probe_data():
    from ghost_shell.fingerprint.validator import check_ja3_matches_chrome
    fp = {}  # no fp.ja3
    template = {"min_chrome_version": 149}
    assert check_ja3_matches_chrome(fp, template) is None


def test_validator_check_skips_when_no_baseline():
    """Warn-level verdict (no baseline for major) → skip rather than
    penalise score."""
    from ghost_shell.fingerprint.validator import check_ja3_matches_chrome
    fp = {"ja3": {"ja3": "cd08e31494f9531f560d64c695473da9"}}
    template = {"min_chrome_version": 200}  # no baseline
    assert check_ja3_matches_chrome(fp, template) is None


def test_validator_check_passes_on_match(monkeypatch):
    from ghost_shell.fingerprint import ja3_check
    from ghost_shell.fingerprint.validator import check_ja3_matches_chrome
    sentinel = "a" * 32
    monkeypatch.setitem(ja3_check.EXPECTED_JA3_BY_MAJOR, 149, [sentinel])
    fp = {"ja3": {"ja3": sentinel}}
    template = {"min_chrome_version": 149}
    status, detail = check_ja3_matches_chrome(fp, template)
    assert status == "pass"
    assert "matches" in detail.lower() or "baseline" in detail.lower()


def test_validator_check_fails_on_mismatch(monkeypatch):
    """Need a sentinel baseline so that a different actual hash
    classifies as 'critical mismatch' (without baseline = warn → skip)."""
    from ghost_shell.fingerprint import ja3_check
    from ghost_shell.fingerprint.validator import check_ja3_matches_chrome
    monkeypatch.setitem(ja3_check.EXPECTED_JA3_BY_MAJOR, 149, ["a" * 32])
    fp = {"ja3": {"ja3": "f" * 32}}
    template = {"min_chrome_version": 149}
    result = check_ja3_matches_chrome(fp, template)
    assert result is not None
    status, detail = result
    assert status == "fail"
    assert "mismatch" in detail.lower()


def test_validator_check_swallows_verdict_exceptions(monkeypatch):
    """If verdict_for crashes for any reason (corrupt fp.ja3 shape),
    return warn — never propagate."""
    from ghost_shell.fingerprint import validator
    fp = {"ja3": {"ja3": "cd08e31494f9531f560d64c695473da9"}}
    template = {"min_chrome_version": 149}

    def boom(*args, **kwargs):
        raise RuntimeError("on purpose")
    monkeypatch.setattr(
        "ghost_shell.fingerprint.ja3_check.verdict_for", boom
    )
    out = validator.check_ja3_matches_chrome(fp, template)
    assert out is not None
    status, detail = out
    assert status == "warn"


def test_validator_check_in_CHECKS_registry():
    """The TLS/JA3 entry must be in the registry with domain=network."""
    from ghost_shell.fingerprint.validator import CHECKS
    ja3_entries = [
        c for c in CHECKS
        if isinstance(c, tuple) and "JA3" in c[0]
    ]
    assert len(ja3_entries) == 1
    name, severity, domain, w_fail, w_warn, fn = ja3_entries[0]
    assert domain == "network"
    assert severity in ("critical", "important")
    assert callable(fn)


def test_validate_includes_network_domain_with_ja3(monkeypatch):
    """End-to-end: a validate() call with fp.ja3 + matching baseline
    produces a 'network' domain entry in by_domain breakdown."""
    from ghost_shell.fingerprint import ja3_check
    from ghost_shell.fingerprint.validator import validate

    sentinel = "a" * 32
    monkeypatch.setitem(ja3_check.EXPECTED_JA3_BY_MAJOR, 149, [sentinel])

    template = {
        "id": "x", "label": "X",
        "ua_platform_token": "Windows NT 10.0; Win64; x64",
        "platform": "Win32",
        "expected_gpu_vendor_marker": "NVIDIA",
        "min_chrome_version": 149, "max_chrome_version": 200,
        "preferred_languages": ["en-US"],
        "expected_timezones": ["America/New_York"],
        "fonts_required": [], "fonts_forbidden": [],
    }
    fp = {
        "navigator": {"userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/149.0.0.0",
                      "platform": "Win32", "vendor": "Google Inc.", "webdriver": False},
        "ja3": {"ja3": sentinel},
    }
    report = validate(fp, template)
    assert "network" in report["by_domain"]
    network = report["by_domain"]["network"]
    assert network["pass"] >= 1
