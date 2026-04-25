// Copyright 2014 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "third_party/blink/renderer/platform/ghost_shell_config.h"
#include "third_party/blink/renderer/core/frame/navigator_concurrent_hardware.h"

#include "base/system/sys_info.h"


namespace blink {

unsigned NavigatorConcurrentHardware::hardwareConcurrency() const {
  if (GhostShellConfig::GetInstance().IsActive()) {
    int v = GhostShellConfig::GetInstance().GetHardwareConcurrency();
    if (v > 0) return static_cast<unsigned>(v);
  }
  return static_cast<unsigned>(base::SysInfo::NumberOfProcessors());
}

}  // namespace blink
