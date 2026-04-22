// Copyright 2026 Ghost Shell Browser. All rights reserved.

#include "third_party/blink/renderer/platform/ghost_shell_config.h"

#include "base/base64.h"
#include "base/command_line.h"
#include "base/json/json_reader.h"
#include "base/json/json_writer.h"
#include "base/logging.h"
#include "base/values.h"
#include "base/containers/span.h"

namespace blink {

namespace {

String ToBlinkString(const std::string* s) {
  if (!s || s->empty()) return String();
  return String::FromUtf8(base::span<const uint8_t>(
      reinterpret_cast<const uint8_t*>(s->data()), s->length()));
}

String ToBlinkString(const std::string& s) {
  if (s.empty()) return String();
  return String::FromUtf8(base::span<const uint8_t>(
      reinterpret_cast<const uint8_t*>(s.data()), s.length()));
}

// NOTE on types:
// Different Chromium versions expose the dict/list types under slightly
// different names — sometimes `base::Value::Dict` / `base::Value::List`,
// sometimes plain `base::DictValue` / `base::ListValue`.
// To stay portable we use template helpers and let the compiler deduce
// the concrete type from the call-site, which already works with `auto`.

template <typename ListT>
String SerializeListAsJson(const ListT& list) {
  std::string tmp;
  base::Value wrapper(list.Clone());   // named lvalue — safe to pass
  if (!base::JSONWriter::Write(wrapper, &tmp)) return String("[]");
  return ToBlinkString(tmp);
}

// Battery time parser — value can be int, double, or string "Infinity".
// Returns -1.0 as sentinel for "unknown / infinity".
template <typename DictT>
double ParseBatteryTime(const DictT& dict, const std::string& key) {
  const base::Value* v = dict.Find(key);
  if (!v) return -1.0;
  if (v->is_int())    return static_cast<double>(v->GetInt());
  if (v->is_double()) return v->GetDouble();
  if (v->is_string() && v->GetString() == "Infinity") return -1.0;
  return -1.0;
}

}  // namespace

GhostShellConfig& GhostShellConfig::GetInstance() {
  static base::NoDestructor<GhostShellConfig> instance;
  return *instance;
}

GhostShellConfig::GhostShellConfig() {
  Initialize();
}

void GhostShellConfig::Initialize() {
  base::CommandLine* command_line = base::CommandLine::ForCurrentProcess();

  if (!command_line->HasSwitch("ghost-shell-payload")) {
    is_active_ = false;
    return;
  }

  std::string b64_payload = command_line->GetSwitchValueASCII("ghost-shell-payload");
  std::string decoded_json;

  if (!base::Base64Decode(b64_payload, &decoded_json)) {
    LOG(ERROR) << "[GhostShell] Failed to decode base64 payload!";
    is_active_ = false;
    return;
  }

  auto root_opt = base::JSONReader::Read(decoded_json, 0);
  if (!root_opt || !root_opt->is_dict()) {
    LOG(ERROR) << "[GhostShell] Failed to parse JSON payload!";
    is_active_ = false;
    return;
  }

  const auto& dict = root_opt->GetDict();
  is_active_ = true;

  // ─── Hardware ──────────────────────────────────────────
  if (const auto* hw = dict.FindDict("hardware")) {
    hardware_concurrency_ = hw->FindInt("hardware_concurrency").value_or(hardware_concurrency_);
    device_memory_        = hw->FindDouble("device_memory").value_or(device_memory_);
    platform_             = ToBlinkString(hw->FindString("platform"));
    user_agent_           = ToBlinkString(hw->FindString("user_agent"));
    max_touch_points_     = hw->FindInt("max_touch_points").value_or(max_touch_points_);
    pdf_viewer_enabled_   = hw->FindBool("pdf_viewer_enabled").value_or(pdf_viewer_enabled_);
  }

  // ─── Languages ─────────────────────────────────────────
  if (const auto* langs = dict.FindDict("languages")) {
    language_        = ToBlinkString(langs->FindString("language"));
    accept_language_ = ToBlinkString(langs->FindString("accept_language"));
    if (const auto* list = langs->FindList("languages")) {
      languages_.clear();
      for (const auto& v : *list) {
        if (v.is_string()) languages_.push_back(ToBlinkString(v.GetString()));
      }
    }
  }

  // ─── Screen ────────────────────────────────────────────
  if (const auto* screen = dict.FindDict("screen")) {
    screen_width_       = screen->FindInt("width").value_or(screen_width_);
    screen_height_      = screen->FindInt("height").value_or(screen_height_);
    avail_width_        = screen->FindInt("avail_width").value_or(avail_width_);
    avail_height_       = screen->FindInt("avail_height").value_or(avail_height_);
    outer_width_        = screen->FindInt("outer_width").value_or(outer_width_);
    outer_height_       = screen->FindInt("outer_height").value_or(outer_height_);
    screen_x_           = screen->FindInt("screen_x").value_or(screen_x_);
    screen_y_           = screen->FindInt("screen_y").value_or(screen_y_);
    color_depth_        = screen->FindInt("color_depth").value_or(color_depth_);
    pixel_depth_        = screen->FindInt("pixel_depth").value_or(pixel_depth_);
    pixel_ratio_        = screen->FindDouble("pixel_ratio").value_or(pixel_ratio_);
    orientation_type_   = ToBlinkString(screen->FindString("orientation"));
    orientation_angle_  = screen->FindInt("orientation_angle").value_or(orientation_angle_);
  }

  // ─── Graphics ──────────────────────────────────────────
  if (const auto* graphics = dict.FindDict("graphics")) {
    gl_vendor_   = ToBlinkString(graphics->FindString("gl_vendor"));
    gl_renderer_ = ToBlinkString(graphics->FindString("gl_renderer"));
    gpu_vendor_  = ToBlinkString(graphics->FindString("webgpu_vendor"));
    gpu_arch_    = ToBlinkString(graphics->FindString("webgpu_arch"));
    gpu_device_  = ToBlinkString(graphics->FindString("webgpu_device"));
    if (const auto* exts = graphics->FindList("webgl_extensions")) {
      webgl_extensions_.clear();
      for (const auto& v : *exts) {
        if (v.is_string()) webgl_extensions_.push_back(ToBlinkString(v.GetString()));
      }
    }
  }

  // ─── Audio ─────────────────────────────────────────────
  if (const auto* audio = dict.FindDict("audio")) {
    audio_sample_rate_       = audio->FindInt("sample_rate").value_or(audio_sample_rate_);
    audio_base_latency_      = audio->FindDouble("base_latency").value_or(audio_base_latency_);
    audio_output_latency_    = audio->FindDouble("output_latency").value_or(audio_output_latency_);
    audio_max_channel_count_ = audio->FindInt("max_channel_count").value_or(audio_max_channel_count_);
  }

  // ─── Timezone ──────────────────────────────────────────
  if (const auto* tz = dict.FindDict("timezone")) {
    timezone_id_         = ToBlinkString(tz->FindString("id"));
    timezone_offset_min_ = tz->FindInt("offset_min").value_or(timezone_offset_min_);
  }

  // ─── Battery ───────────────────────────────────────────
  // Payload has battery = null on desktop, object on laptop.
  if (const auto* battery = dict.FindDict("battery")) {
    has_battery_              = true;
    battery_charging_         = battery->FindBool("charging").value_or(true);
    battery_level_            = battery->FindDouble("level").value_or(0.85);
    battery_charging_time_    = ParseBatteryTime(*battery, "charging_time");
    battery_discharging_time_ = ParseBatteryTime(*battery, "discharging_time");
  } else {
    has_battery_ = false;
  }

  // ─── Connection ────────────────────────────────────────
  if (const auto* conn = dict.FindDict("connection")) {
    connection_effective_type_ = ToBlinkString(conn->FindString("effective_type"));
    connection_downlink_       = conn->FindDouble("downlink").value_or(connection_downlink_);
    connection_rtt_            = conn->FindInt("rtt").value_or(connection_rtt_);
    connection_save_data_      = conn->FindBool("save_data").value_or(connection_save_data_);
    connection_type_           = ToBlinkString(conn->FindString("type"));
  }

  // ─── Fonts ─────────────────────────────────────────────
  if (const auto* fonts = dict.FindList("fonts")) {
    font_list_.clear();
    for (const auto& v : *fonts) {
      if (v.is_string()) font_list_.push_back(ToBlinkString(v.GetString()));
    }
  }

  // ─── Noise seeds ───────────────────────────────────────
  if (const auto* noise = dict.FindDict("noise")) {
    random_seed_       = noise->FindInt("seed").value_or(random_seed_);
    canvas_shift_      = noise->FindInt("canvas_shift").value_or(canvas_shift_);
    audio_offset_      = noise->FindDouble("audio_offset").value_or(audio_offset_);
    rect_offset_       = noise->FindDouble("rect_offset").value_or(rect_offset_);
    font_width_offset_ = noise->FindDouble("font_width_offset").value_or(font_width_offset_);
  }

  // ─── WebRTC Media ──────────────────────────────────────
  if (const auto* media = dict.FindDict("media")) {
    if (const auto* list = media->FindList("audio_inputs"))
      audio_inputs_json_ = SerializeListAsJson(*list);
    if (const auto* list = media->FindList("video_inputs"))
      video_inputs_json_ = SerializeListAsJson(*list);
    if (const auto* list = media->FindList("audio_outputs"))
      audio_outputs_json_ = SerializeListAsJson(*list);
  }

  // ─── UserAgentMetadata ─────────────────────────────────
  if (const auto* uam = dict.FindDict("ua_metadata")) {
    ua_full_version_     = ToBlinkString(uam->FindString("full_version"));
    ua_major_version_    = ToBlinkString(uam->FindString("major_version"));
    ua_platform_         = ToBlinkString(uam->FindString("platform"));
    ua_platform_version_ = ToBlinkString(uam->FindString("platform_version"));
    ua_architecture_     = ToBlinkString(uam->FindString("architecture"));
    ua_bitness_          = ToBlinkString(uam->FindString("bitness"));
    ua_model_            = ToBlinkString(uam->FindString("model"));
    ua_wow64_            = uam->FindBool("wow64").value_or(false);
    ua_mobile_           = uam->FindBool("mobile").value_or(false);

    if (const auto* list = uam->FindList("brands"))
      ua_brands_json_ = SerializeListAsJson(*list);
    if (const auto* list = uam->FindList("full_version_list"))
      ua_full_version_list_json_ = SerializeListAsJson(*list);
  }

  // ─── Plugins ───────────────────────────────────────────
  if (const auto* plugins = dict.FindList("plugins"))
    plugins_json_ = SerializeListAsJson(*plugins);

  // Use VLOG(1) instead of LOG(INFO) so these only appear when explicitly
  // debugging via --v=1 / --enable-logging. Prevents console spam in
  // normal runs where Chromium spawns renderers/GPU/utility subprocesses.
  VLOG(1) << "[GhostShell] Stealth profile loaded. Template=" << platform_.Utf8();
  VLOG(1) << "[GhostShell] UA=" << user_agent_.Utf8().substr(0, 80);
}

}  // namespace blink
