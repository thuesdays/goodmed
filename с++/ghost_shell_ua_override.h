// Copyright 2026 Ghost Shell Browser.
//
// ghost_shell_ua_override.h — browser-process readable copy of the
// ghost-shell-payload UA metadata. This exists because GhostShellConfig
// in //third_party/blink/renderer/platform is renderer-only, but the
// function that builds UserAgentMetadata lives in the browser process.
//
// Reads --ghost-shell-payload once, caches the UA metadata subset,
// exposes getters that user_agent_utils.cc consults.

#ifndef COMPONENTS_EMBEDDER_SUPPORT_GHOST_SHELL_UA_OVERRIDE_H_
#define COMPONENTS_EMBEDDER_SUPPORT_GHOST_SHELL_UA_OVERRIDE_H_

#include <string>
#include <vector>

#include "base/no_destructor.h"
#include "third_party/blink/public/common/user_agent/user_agent_metadata.h"

namespace embedder_support {

class GhostShellUAOverride {
 public:
  // Singleton access. Reads command line on first call, then caches.
  static GhostShellUAOverride& GetInstance();

  // Returns true when --ghost-shell-payload was present AND contained
  // a valid ua_metadata dict. When false, every getter returns empty/
  // default values — caller should fall through to native logic.
  bool IsActive() const { return is_active_; }

  // Brands: {(brand, version)} pairs. `major_version` is what JS sees
  // in navigator.userAgentData.brands, full_version_list is what
  // getHighEntropyValues({"fullVersionList"}) returns.
  const std::vector<blink::UserAgentBrandVersion>& GetBrandList() const {
    return brand_version_list_;
  }
  const std::vector<blink::UserAgentBrandVersion>& GetBrandFullVersionList() const {
    return brand_full_version_list_;
  }

  // Scalar fields — empty string means "use default / not spoofed".
  std::string GetFullVersion()      const { return full_version_; }
  std::string GetPlatform()         const { return platform_; }
  std::string GetPlatformVersion()  const { return platform_version_; }
  std::string GetArchitecture()     const { return architecture_; }
  std::string GetBitness()          const { return bitness_; }
  std::string GetModel()            const { return model_; }
  bool GetMobile()                  const { return mobile_; }
  bool GetWoW64()                   const { return wow64_; }

  GhostShellUAOverride(const GhostShellUAOverride&) = delete;
  GhostShellUAOverride& operator=(const GhostShellUAOverride&) = delete;

 private:
  friend class base::NoDestructor<GhostShellUAOverride>;
  GhostShellUAOverride();
  ~GhostShellUAOverride();

  void Initialize();

  bool is_active_ = false;

  std::vector<blink::UserAgentBrandVersion> brand_version_list_;
  std::vector<blink::UserAgentBrandVersion> brand_full_version_list_;

  std::string full_version_;
  std::string platform_;
  std::string platform_version_;
  std::string architecture_;
  std::string bitness_;
  std::string model_;
  bool mobile_ = false;
  bool wow64_ = false;
};

}  // namespace embedder_support

#endif  // COMPONENTS_EMBEDDER_SUPPORT_GHOST_SHELL_UA_OVERRIDE_H_
