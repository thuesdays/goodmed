// Copyright 2026 Ghost Shell Browser. All rights reserved.
// Native Stealth Configuration Core for Chromium

#ifndef THIRD_PARTY_BLINK_RENDERER_PLATFORM_GHOST_SHELL_CONFIG_H_
#define THIRD_PARTY_BLINK_RENDERER_PLATFORM_GHOST_SHELL_CONFIG_H_

#include "base/no_destructor.h"
#include "third_party/blink/renderer/platform/platform_export.h"
#include "third_party/blink/renderer/platform/wtf/hash_map.h"
#include "third_party/blink/renderer/platform/wtf/text/string_hash.h"
#include "third_party/blink/renderer/platform/wtf/text/wtf_string.h"
#include "third_party/blink/renderer/platform/wtf/vector.h"

namespace blink {

class PLATFORM_EXPORT GhostShellConfig {
 public:
  static GhostShellConfig& GetInstance();

  GhostShellConfig(const GhostShellConfig&) = delete;
  GhostShellConfig& operator=(const GhostShellConfig&) = delete;

  bool IsActive() const { return is_active_; }

  // ─── Hardware ───────────────────────────────────────────
  int GetHardwareConcurrency() const { return hardware_concurrency_; }
  // W3C spec: navigator.deviceMemory must be one of
  // {0.25, 0.5, 1, 2, 4, 8} — anything above is clamped to 8 for
  // privacy. The underlying device_memory_ can be set to 16 etc. for
  // consistency with other fingerprint fields, but this getter —
  // the one feeding navigator.deviceMemory — applies the spec clamp.
  double GetDeviceMemory() const {
    if (device_memory_ >= 8.0) return 8.0;
    if (device_memory_ >= 4.0) return 4.0;
    if (device_memory_ >= 2.0) return 2.0;
    if (device_memory_ >= 1.0) return 1.0;
    if (device_memory_ >= 0.5) return 0.5;
    return 0.25;
  }
  // Raw value (for internal use, e.g. performance.memory.jsHeapSizeLimit)
  double GetDeviceMemoryRaw() const { return device_memory_; }
  String GetPlatform() const { return platform_; }
  String GetUserAgent() const { return user_agent_; }
  int GetMaxTouchPoints() const { return max_touch_points_; }
  bool GetPdfViewerEnabled() const { return pdf_viewer_enabled_; }

  // ─── Languages ──────────────────────────────────────────
  String GetLanguage() const { return language_; }
  const Vector<String>& GetLanguages() const { return languages_; }
  String GetAcceptLanguage() const { return accept_language_; }

  // ─── Screen ─────────────────────────────────────────────
  int GetScreenWidth() const { return screen_width_; }
  int GetScreenHeight() const { return screen_height_; }
  int GetAvailWidth() const {
    // No horizontal jitter — real desktops don't have vertical
    // taskbars on either side, so availWidth usually equals width.
    // (If someone runs a side-docked taskbar, bump this later.)
    return avail_width_;
  }

  int GetAvailHeight() const {
    // Real desktops subtract taskbar height (Windows 40-48, macOS 24).
    // screen_avail_jitter_ is a 0-12 pixel per-profile stable value,
    // combined with the template's base taskbar to produce realistic
    // variance. Two profiles on the "same" 1920×1080 screen report
    // availHeight values 10-20px apart, matching real-user config.
    int result = avail_height_ - screen_avail_jitter_;
    // Guard against negative values if someone generates a pathological
    // fingerprint. availHeight < 100 would be suspicious by itself.
    return result > 100 ? result : avail_height_;
  }
  int GetOuterWidth() const { return outer_width_; }
  int GetOuterHeight() const { return outer_height_; }
  int GetScreenX() const { return screen_x_; }
  int GetScreenY() const { return screen_y_; }
  int GetColorDepth() const { return color_depth_; }
  int GetPixelDepth() const { return pixel_depth_; }
  double GetPixelRatio() const { return pixel_ratio_; }
  String GetOrientationType() const { return orientation_type_; }
  int GetOrientationAngle() const { return orientation_angle_; }

  // ─── Graphics ───────────────────────────────────────────
  String GetGLVendor() const { return gl_vendor_; }
  String GetGLRenderer() const { return gl_renderer_; }
  String GetGPUVendor() const { return gpu_vendor_; }
  String GetGPUArch() const { return gpu_arch_; }
  String GetGPUDevice() const { return gpu_device_; }
  const Vector<String>& GetWebGLExtensions() const { return webgl_extensions_; }

  // ─── Audio ──────────────────────────────────────────────
  int GetAudioSampleRate() const { return audio_sample_rate_; }
  double GetAudioBaseLatency() const { return audio_base_latency_; }
  double GetAudioOutputLatency() const { return audio_output_latency_; }
  int GetAudioMaxChannelCount() const { return audio_max_channel_count_; }

  // ─── Timezone ───────────────────────────────────────────
  String GetTimezoneId() const { return timezone_id_; }
  int GetTimezoneOffsetMin() const { return timezone_offset_min_; }

  // ─── Battery ────────────────────────────────────────────
  bool HasBattery() const { return has_battery_; }
  bool GetBatteryCharging() const { return battery_charging_; }
  double GetBatteryLevel() const { return battery_level_; }
  // -1 означает Infinity для discharging/charging time
  double GetBatteryChargingTime() const { return battery_charging_time_; }
  double GetBatteryDischargingTime() const { return battery_discharging_time_; }

  // ─── Network Connection ─────────────────────────────────
  String GetConnectionEffectiveType() const { return connection_effective_type_; }
  double GetConnectionDownlink() const { return connection_downlink_; }
  int GetConnectionRtt() const { return connection_rtt_; }
  bool GetConnectionSaveData() const { return connection_save_data_; }
  String GetConnectionType() const { return connection_type_; }

  // ─── Fonts ──────────────────────────────────────────────
  const Vector<String>& GetFontList() const { return font_list_; }

  // ─── Noise Seeds ────────────────────────────────────────
  //
  // Every profile gets a stable noise profile — same jitter on every
  // run of the same profile name (so Creep.js stability score passes),
  // but DIFFERENT jitter between profiles (so a pool doesn't collide
  // on one identical hash). All getters return values the per-subsystem
  // patches fold into their outputs:
  //
  //   canvas_*   → blink::CanvasRenderingContext2D::getImageData()
  //   webgl_*    → blink::WebGLRenderingContext::getShaderPrecisionFormat()
  //                and a subset of getParameter() queries (mask-controlled)
  //   audio_*    → blink::AudioContext sample-rate + getFloatFrequencyData()
  //   rect_*     → blink::DOMRect::{x,y,width,height}
  //   font_*     → blink::Element::offsetWidth/Height (text-measuring paths)
  //   screen_*   → GetAvailWidth/Height — subtract for fake taskbar
  //   tz_*       → Date::getTimezoneOffset (±1 minute drift)
  //
  // The values are seeded by device_templates.py::_build_noise() from
  // SHA256(profile_name), so profile_01 always gets the same numbers.
  int GetRandomSeed() const { return random_seed_; }
  int GetCanvasShift() const { return canvas_shift_; }
  double GetCanvasNoise() const { return canvas_noise_; }
  double GetWebGLNoise() const { return webgl_noise_; }
  int GetWebGLParamsMask() const { return webgl_params_mask_; }
  double GetAudioOffset() const { return audio_offset_; }
  int GetAudioRateJitter() const { return audio_rate_jitter_; }
  double GetRectOffset() const { return rect_offset_; }
  double GetFontWidthOffset() const { return font_width_offset_; }
  int GetScreenAvailJitter() const { return screen_avail_jitter_; }
  int GetTimezoneOffsetJitter() const { return timezone_offset_jitter_; }

  // ─── WebRTC Media Devices (JSON) ────────────────────────
  String GetAudioInputsJSON() const { return audio_inputs_json_; }
  String GetVideoInputsJSON() const { return video_inputs_json_; }
  String GetAudioOutputsJSON() const { return audio_outputs_json_; }

  // ─── GPU (WebGL UNMASKED_* + WebGPU adapter info) ───────
  //
  // These are the values returned by:
  //   gl.getParameter(UNMASKED_VENDOR_WEBGL)    → vendor
  //   gl.getParameter(UNMASKED_RENDERER_WEBGL)  → renderer
  //   navigator.gpu.requestAdapter().requestAdapterInfo() → .vendor / .device
  //
  // WebGL and WebGPU MUST agree — a real GPU answers identically to both
  // APIs. Using different values here is a dead giveaway (creepjs flags it).
  String GetUnmaskedVendor() const { return unmasked_vendor_; }
  String GetUnmaskedRenderer() const { return unmasked_renderer_; }

  // ─── Media Codecs ───────────────────────────────────────
  //
  // navigator.mediaCapabilities.decodingInfo() returns
  // { supported, smooth, powerEfficient } for a codec. Our config
  // stores a dict keyed by short codec name ("av1", "vp9", "h264", "h265")
  // — the patched media_capabilities.cc looks up the matching entry.
  //
  // Returns false for unknown keys so JS callers see a graceful "not
  // supported" rather than the default-true tell of unpatched Chromium.
  bool GetCodecSupported(const String& key) const;
  bool GetCodecSmooth(const String& key) const;
  bool GetCodecPowerEfficient(const String& key) const;

  // ─── UserAgentMetadata (для Sec-CH-UA-*) ────────────────
  String GetUAFullVersion() const { return ua_full_version_; }
  String GetUAMajorVersion() const { return ua_major_version_; }
  String GetUAPlatform() const { return ua_platform_; }
  String GetUAPlatformVersion() const { return ua_platform_version_; }
  String GetUAArchitecture() const { return ua_architecture_; }
  String GetUABitness() const { return ua_bitness_; }
  String GetUAModel() const { return ua_model_; }
  bool GetUAWow64() const { return ua_wow64_; }
  bool GetUAMobile() const { return ua_mobile_; }
  // JSON-массивы brands и full_version_list для сериализации в GetUserAgentMetadata()
  String GetUABrandsJSON() const { return ua_brands_json_; }
  String GetUAFullVersionListJSON() const { return ua_full_version_list_json_; }

  // ─── Plugins (JSON-массив для navigator.plugins) ────────
  String GetPluginsJSON() const { return plugins_json_; }

  // ─── Permissions API (for navigator.permissions.query) ───
  // Returns "granted" / "denied" / "prompt" / empty-string for
  // unspecified permission names. Chromium code calls this with the
  // feature name ("geolocation", "notifications", "camera", etc.).
  String GetPermissionState(const String& name) const;

 private:
  friend class base::NoDestructor<GhostShellConfig>;
  GhostShellConfig();
  ~GhostShellConfig() = default;

  void Initialize();

  bool is_active_ = false;

  // Hardware
  int hardware_concurrency_ = 8;
  double device_memory_ = 8.0;
  String platform_ = "Win32";
  String user_agent_;
  int max_touch_points_ = 0;
  bool pdf_viewer_enabled_ = true;

  // Languages
  String language_ = "uk-UA";
  Vector<String> languages_;
  String accept_language_ = "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7";

  // Screen
  int screen_width_ = 1920;
  int screen_height_ = 1080;
  int avail_width_ = 1920;
  int avail_height_ = 1040;
  int outer_width_ = 1920;
  int outer_height_ = 1040;
  int screen_x_ = 0;
  int screen_y_ = 0;
  int color_depth_ = 24;
  int pixel_depth_ = 24;
  double pixel_ratio_ = 1.0;
  String orientation_type_ = "landscape-primary";
  int orientation_angle_ = 0;

  // Graphics
  String gl_vendor_;
  String gl_renderer_;
  String gpu_vendor_ = "intel";
  String gpu_arch_ = "xe";
  String gpu_device_;
  Vector<String> webgl_extensions_;

  // Audio
  int audio_sample_rate_ = 48000;
  double audio_base_latency_ = 0.01;
  double audio_output_latency_ = 0.0;
  int audio_max_channel_count_ = 2;

  // Timezone
  String timezone_id_ = "Europe/Kyiv";
  int timezone_offset_min_ = -180;

  // Battery
  bool has_battery_ = false;
  bool battery_charging_ = true;
  double battery_level_ = 1.0;
  double battery_charging_time_ = -1.0;      // -1 = Infinity
  double battery_discharging_time_ = -1.0;   // -1 = Infinity

  // Network Connection
  String connection_effective_type_ = "4g";
  double connection_downlink_ = 10.0;
  int connection_rtt_ = 50;
  bool connection_save_data_ = false;
  String connection_type_ = "wifi";

  // Fonts
  Vector<String> font_list_;

  // Noise seeds — see header block above GetRandomSeed() for semantics.
  // All fields default to 0 / 0.0 = no noise applied (fingerprint
  // identical to vanilla Chromium). Profiles set these via config JSON.
  int random_seed_ = 0;
  int canvas_shift_ = 0;
  double canvas_noise_ = 0.0;
  double webgl_noise_ = 0.0;
  int webgl_params_mask_ = 0;
  double audio_offset_ = 0.0;
  int audio_rate_jitter_ = 0;
  double rect_offset_ = 0.0;
  double font_width_offset_ = 0.0;
  int screen_avail_jitter_ = 0;
  int timezone_offset_jitter_ = 0;

  // WebRTC media (raw JSON — парсится на месте использования)
  String audio_inputs_json_ = "[]";
  String video_inputs_json_ = "[]";
  String audio_outputs_json_ = "[]";

  // GPU — WebGL UNMASKED_* + WebGPU adapter info. Defaults match an
  // Intel integrated GPU on Windows (most common real answer) so an
  // unconfigured profile doesn't jump out as weird.
  String unmasked_vendor_   = "Google Inc. (Intel)";
  String unmasked_renderer_ =
      "ANGLE (Intel, Mesa Intel(R) UHD Graphics 620 (0x00005917), "
      "OpenGL 4.6)";

  // Media codecs — raw JSON dict of {codec_key: {supported, smooth,
  // power_efficient}}. Parsed on each lookup (cheap; called rarely).
  String codecs_json_ = "{}";

  // UserAgentMetadata
  String ua_full_version_;
  String ua_major_version_;
  String ua_platform_ = "Windows";
  String ua_platform_version_ = "15.0.0";
  String ua_architecture_ = "x86";
  String ua_bitness_ = "64";
  String ua_model_;
  bool ua_wow64_ = false;
  bool ua_mobile_ = false;
  String ua_brands_json_ = "[]";
  String ua_full_version_list_json_ = "[]";

  // Plugins (для navigator.plugins)
  String plugins_json_ = "[]";

  // Permissions (name → "granted"/"denied"/"prompt").
  // Example default: {"geolocation": "prompt", "notifications": "prompt",
  //                   "clipboard-read": "prompt", "camera": "prompt"}
  HashMap<String, String> permissions_;
};

}  // namespace blink

// C-style bridge for WebRTC and other components that can't include Blink headers.
extern "C" {
PLATFORM_EXPORT bool GhostShell_IsActive();
PLATFORM_EXPORT int GhostShell_GetRandomSeed();
}

#endif  // THIRD_PARTY_BLINK_RENDERER_PLATFORM_GHOST_SHELL_CONFIG_H_
