// Copyright 2014 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#ifndef THIRD_PARTY_BLINK_RENDERER_CORE_GEOMETRY_DOM_RECT_READ_ONLY_H_
#define THIRD_PARTY_BLINK_RENDERER_CORE_GEOMETRY_DOM_RECT_READ_ONLY_H_

#include "third_party/blink/renderer/core/core_export.h"
#include "third_party/blink/renderer/core/geometry/geometry_util.h"
#include "third_party/blink/renderer/platform/bindings/script_wrappable.h"
#include "third_party/blink/renderer/platform/ghost_shell_config.h"
#include "ui/gfx/geometry/point_f.h"
#include "ui/gfx/geometry/rect.h"
#include "ui/gfx/geometry/rect_f.h"

namespace blink {

// Ghost Shell Anty (Tier 2 #1): per-rect deterministic sub-pixel
// offset to DOMRect getters. The base offset comes from
// GhostShellConfig::GetRectOffset(); a SplitMix64 hash of the
// rect's stable members (x_, y_, width_, height_) produces a per-
// rect mix factor in roughly [-1, +1] so two profiles see different
// rects but the same profile sees the same rect every call. Skip
// when offset is 0.0 (config default — patch off for non-active
// profiles).
inline double GhostShellRectAdjust(double base, int field_id,
                                   double x_, double y_,
                                   double width_, double height_) {
  auto& cfg = GhostShellConfig::GetInstance();
  if (!cfg.IsActive()) return base;
  const double off = cfg.GetRectOffset();
  if (off == 0.0) return base;
  uint64_t h = static_cast<uint64_t>(cfg.GetRandomSeed());
  h ^= 0x9E3779B97F4A7C15ULL;
  // Mix all four members + the field id (0=x,1=y,2=w,3=h,4=t,5=r,6=b,7=l)
  auto mix64 = [&](double v) {
    union { double d; uint64_t u; } u;
    u.d = v;
    h ^= u.u;
    h *= 0xBF58476D1CE4E5B9ULL;
    h = (h >> 27) | (h << 37);
  };
  mix64(x_); mix64(y_); mix64(width_); mix64(height_);
  h ^= static_cast<uint64_t>(field_id);
  h *= 0x94D049BB133111EBULL;
  const double mix = static_cast<double>(
      static_cast<int32_t>(h & 0xFFFFFFFFu)) / 2147483648.0;  // [-1, +1)
  return base + off * mix;
}

class DOMRectInit;
class ScriptObject;
class ScriptState;

class CORE_EXPORT DOMRectReadOnly : public ScriptWrappable {
  DEFINE_WRAPPERTYPEINFO();

 public:
  static DOMRectReadOnly* Create(double x,
                                 double y,
                                 double width,
                                 double height);
  static DOMRectReadOnly* FromRect(const gfx::Rect&);
  static DOMRectReadOnly* FromRectF(const gfx::RectF&);
  static DOMRectReadOnly* fromRect(const DOMRectInit*);

  DOMRectReadOnly(double x, double y, double width, double height);

  double x() const {
    return GhostShellRectAdjust(x_, 0, x_, y_, width_, height_);
  }
  double y() const {
    return GhostShellRectAdjust(y_, 1, x_, y_, width_, height_);
  }
  double width() const {
    return GhostShellRectAdjust(width_, 2, x_, y_, width_, height_);
  }
  double height() const {
    return GhostShellRectAdjust(height_, 3, x_, y_, width_, height_);
  }

  double top() const {
    return GhostShellRectAdjust(
        geometry_util::NanSafeMin(y_, y_ + height_), 4,
        x_, y_, width_, height_);
  }
  double right() const {
    return GhostShellRectAdjust(
        geometry_util::NanSafeMax(x_, x_ + width_), 5,
        x_, y_, width_, height_);
  }
  double bottom() const {
    return GhostShellRectAdjust(
        geometry_util::NanSafeMax(y_, y_ + height_), 6,
        x_, y_, width_, height_);
  }
  double left() const {
    return GhostShellRectAdjust(
        geometry_util::NanSafeMin(x_, x_ + width_), 7,
        x_, y_, width_, height_);
  }

  // This is just a utility function, which is not web exposed.
  gfx::PointF Center() const;

  ScriptObject toJSONForBinding(ScriptState*) const;

  bool IsPointInside(double x, double y) const {
    return x >= left() && x < right() && y >= top() && y < bottom();
  }

 protected:
  double x_;
  double y_;
  double width_;
  double height_;
};

}  // namespace blink

#endif  // THIRD_PARTY_BLINK_RENDERER_CORE_GEOMETRY_DOM_RECT_READ_ONLY_H_
