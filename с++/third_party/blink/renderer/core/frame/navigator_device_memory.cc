// Copyright 2014 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "third_party/blink/renderer/platform/ghost_shell_config.h"
#include "third_party/blink/renderer/core/frame/navigator_device_memory.h"

#include "third_party/blink/public/common/device_memory/approximated_device_memory.h"
#include "third_party/blink/public/mojom/use_counter/metrics/web_feature.mojom-shared.h"
#include "third_party/blink/renderer/core/dom/document.h"
#include "third_party/blink/renderer/core/frame/local_dom_window.h"


namespace blink {
// Touched to force ninja recompilation for GhostShellConfig

float NavigatorDeviceMemory::deviceMemory() const {
  if (GhostShellConfig::GetInstance().IsActive()) {
    double v = GhostShellConfig::GetInstance().GetDeviceMemory();
    if (v > 0) return static_cast<float>(v);
  }
  return ApproximatedDeviceMemory::GetApproximatedDeviceMemory();
}

}  // namespace blink
