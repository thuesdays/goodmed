// Copyright 2026 Ghost Shell Browser.

#include "components/embedder_support/ghost_shell_ua_override.h"

#include "base/base64.h"
#include "base/command_line.h"
#include "base/json/json_reader.h"
#include "base/logging.h"
#include "base/no_destructor.h"
#include "base/values.h"

namespace embedder_support {

namespace {

std::string StringOrEmpty(const std::string* s) {
  return s ? *s : std::string();
}

// Parses a list of {"brand": "X", "version": "Y"} dicts into a vector
// of UserAgentBrandVersion.
std::vector<blink::UserAgentBrandVersion> ParseBrandList(
    const base::ListValue& list) {
  std::vector<blink::UserAgentBrandVersion> out;
  out.reserve(list.size());
  for (const auto& v : list) {
    if (!v.is_dict()) continue;
    const auto& d = v.GetDict();
    const std::string* brand = d.FindString("brand");
    const std::string* ver   = d.FindString("version");
    if (brand && ver) {
      blink::UserAgentBrandVersion bv;
      bv.brand   = *brand;
      bv.version = *ver;
      out.push_back(std::move(bv));
    }
  }
  return out;
}

}  // namespace

// Singleton instance — NoDestructor to avoid exit-time ordering issues.
GhostShellUAOverride& GhostShellUAOverride::GetInstance() {
  static base::NoDestructor<GhostShellUAOverride> instance;
  return *instance;
}

GhostShellUAOverride::GhostShellUAOverride() {
  Initialize();
}

GhostShellUAOverride::~GhostShellUAOverride() = default;

void GhostShellUAOverride::Initialize() {
  base::CommandLine* cmd = base::CommandLine::ForCurrentProcess();
  if (!cmd->HasSwitch("ghost-shell-payload")) {
    is_active_ = false;
    return;
  }

  std::string b64 = cmd->GetSwitchValueASCII("ghost-shell-payload");
  std::string json_str;
  if (!base::Base64Decode(b64, &json_str)) {
    LOG(ERROR) << "[GhostShellUA] base64 decode failed";
    is_active_ = false;
    return;
  }

  auto parsed = base::JSONReader::Read(json_str, 0);
  if (!parsed || !parsed->is_dict()) {
    LOG(ERROR) << "[GhostShellUA] JSON parse failed";
    is_active_ = false;
    return;
  }

  const auto& root = parsed->GetDict();
  const auto* uam = root.FindDict("ua_metadata");
  if (!uam) {
    // No ua_metadata block — caller shouldn't spoof.
    is_active_ = false;
    return;
  }

  full_version_      = StringOrEmpty(uam->FindString("full_version"));
  platform_          = StringOrEmpty(uam->FindString("platform"));
  platform_version_  = StringOrEmpty(uam->FindString("platform_version"));
  architecture_      = StringOrEmpty(uam->FindString("architecture"));
  bitness_           = StringOrEmpty(uam->FindString("bitness"));
  model_             = StringOrEmpty(uam->FindString("model"));
  mobile_            = uam->FindBool("mobile").value_or(false);
  wow64_             = uam->FindBool("wow64").value_or(false);

  if (const auto* brands = uam->FindList("brands")) {
    brand_version_list_ = ParseBrandList(*brands);
  }
  if (const auto* full_brands = uam->FindList("full_version_list")) {
    brand_full_version_list_ = ParseBrandList(*full_brands);
  }

  is_active_ = true;
  VLOG(1) << "[GhostShellUA] loaded, platform=" << platform_
          << " ver=" << full_version_;
}

}  // namespace embedder_support
