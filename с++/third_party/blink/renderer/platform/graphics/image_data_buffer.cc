/*
 * Copyright (c) 2008, Google Inc. All rights reserved.
 * Copyright (C) 2009 Dirk Schulze <krit@webkit.org>
 * Copyright (C) 2010 Torch Mobile (Beijing) Co. Ltd. All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions are
 * met:
 *
 *     * Redistributions of source code must retain the above copyright
 * notice, this list of conditions and the following disclaimer.
 *     * Redistributions in binary form must reproduce the above
 * copyright notice, this list of conditions and the following disclaimer
 * in the documentation and/or other materials provided with the
 * distribution.
 *     * Neither the name of Google Inc. nor the names of its
 * contributors may be used to endorse or promote products derived from
 * this software without specific prior written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
 * "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
 * LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
 * A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
 * OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
 * SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
 * LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
 * DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
 * THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 * (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
 * OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 */

#include "third_party/blink/renderer/platform/graphics/image_data_buffer.h"

// Ghost Shell Anty: per-profile canvas pixel noise. Reads from
// GhostShellConfig (the central per-profile fingerprint singleton —
// populated once on launch from --ghost-shell-payload base64 JSON).
// No new CLI flags; canvas_noise + random_seed are just two more
// fields on the existing config record.
#include "third_party/blink/renderer/platform/ghost_shell_config.h"

#include "base/compiler_specific.h"
#include "base/memory/ptr_util.h"
#include "third_party/blink/renderer/platform/image-encoders/image_encoder_utils.h"
#include "third_party/blink/renderer/platform/wtf/text/base64.h"
#include "third_party/blink/renderer/platform/wtf/text/strcat.h"
#include "third_party/skia/include/core/SkImage.h"
#include "third_party/skia/include/core/SkSurface.h"
#include "ui/gfx/skia_span_util.h"

namespace blink {

namespace {

// SplitMix64 — keyed hash producing uniform output for every
// (profile_seed, x, y) triplet. Used to pick which pixels get
// perturbed and which direction each channel flips.
inline uint64_t GhostShellMix(uint64_t z) {
  z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
  z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
  return z ^ (z >> 31);
}

// Apply +/- 1-LSB noise to ~0.5% of N32 pixels. No-op when
// GhostShellConfig isn't active OR pixmap isn't 8-bit-per-channel
// RGBA. Seed comes from GetRandomSeed() (per-profile stable);
// density modulated by canvas_noise (0..0.003 typical).
//
// Visually imperceptible (1 LSB on 8-bit channel < quantization
// threshold) but makes the pixel hash differ across profiles.
void GhostShellApplyCanvasNoise(SkPixmap* dst) {
  auto& cfg = blink::GhostShellConfig::GetInstance();
  if (!cfg.IsActive() || !dst || dst->info().bytesPerPixel() != 4) {
    return;
  }
  const double noise_strength = cfg.GetCanvasNoise();
  if (noise_strength <= 0.0) {
    return;
  }
  // canvas_noise is a 0..0.003 density value: at 0.003 we perturb
  // ~3 pixels per thousand. The divisor below converts that to a
  // "1-in-N" gate for the hash test.
  const uint64_t gate = noise_strength > 0
      ? std::max<uint64_t>(2ULL, static_cast<uint64_t>(1.0 / noise_strength))
      : 0;
  if (gate == 0) return;
  const uint64_t seed = static_cast<uint64_t>(cfg.GetRandomSeed());
  const int w = dst->width();
  const int h = dst->height();
  if (w <= 0 || h <= 0) return;
  for (int y = 0; y < h; ++y) {
    uint8_t* row = static_cast<uint8_t*>(dst->writable_addr(0, y));
    if (!row) continue;
    for (int x = 0; x < w; ++x) {
      const uint64_t k = GhostShellMix(
          seed ^ static_cast<uint64_t>(x) ^
          (static_cast<uint64_t>(y) << 32));
      // Density-modulated gate: 1-in-N pixels touched, where N
      // is derived from canvas_noise (e.g. 0.005 → 1-in-200).
      if ((k % gate) != 0) continue;
      uint8_t* px = row + x * 4;
      // +/- 1 LSB on R/G/B; alpha untouched. Direction comes from
      // bits 8..10 of k so each channel flips independently.
      px[0] = static_cast<uint8_t>(px[0] + ((k & 0x100) ? 1 : -1));
      px[1] = static_cast<uint8_t>(px[1] + ((k & 0x200) ? 1 : -1));
      px[2] = static_cast<uint8_t>(px[2] + ((k & 0x400) ? 1 : -1));
    }
  }
}

}  // namespace


ImageDataBuffer::ImageDataBuffer(scoped_refptr<StaticBitmapImage> image) {
  if (!image)
    return;
  PaintImage paint_image = image->PaintImageForCurrentFrame();
  if (!paint_image || paint_image.IsPaintWorklet())
    return;

  SkImageInfo paint_image_info = paint_image.GetSkImageInfo();
  if (paint_image_info.isEmpty())
    return;

#if defined(MEMORY_SANITIZER)
  // Test if software SKImage has an initialized pixmap.
  SkPixmap pixmap;
  if (!paint_image.IsTextureBacked() &&
      paint_image.GetSwSkImage()->peekPixels(&pixmap)) {
    MSAN_CHECK_MEM_IS_INITIALIZED(pixmap.addr(), pixmap.computeByteSize());
  }
#endif

  if (paint_image.IsTextureBacked() || paint_image.IsLazyGenerated() ||
      paint_image_info.alphaType() != kUnpremul_SkAlphaType) {
    // Unpremul is handled upfront, using readPixels, which will correctly clamp
    // premul color values that would otherwise cause overflows in the skia
    // encoder unpremul logic.
    SkColorType colorType = paint_image.GetColorType();
    if (colorType == kRGBA_8888_SkColorType ||
        colorType == kBGRA_8888_SkColorType)
      colorType = kN32_SkColorType;  // Work around for bug with JPEG encoder
    const SkImageInfo info =
        SkImageInfo::Make(paint_image_info.width(), paint_image_info.height(),
                          paint_image_info.colorType(), kUnpremul_SkAlphaType,
                          paint_image_info.refColorSpace());
    const size_t rowBytes = info.minRowBytes();
    size_t size = info.computeByteSize(rowBytes);
    if (SkImageInfo::ByteSizeOverflowed(size))
      return;

    sk_sp<SkData> data = SkData::MakeUninitialized(size);
    pixmap_ = {info, data->writable_data(), info.minRowBytes()};
    if (!paint_image.readPixels(info, pixmap_.writable_addr(), rowBytes, 0,
                                0)) {
      pixmap_.reset();
      return;
    }
    MSAN_CHECK_MEM_IS_INITIALIZED(pixmap_.addr(), pixmap_.computeByteSize());
    retained_image_ = SkImages::RasterFromData(info, std::move(data), rowBytes);
  } else {
    retained_image_ = paint_image.GetSwSkImage();
    if (!retained_image_->peekPixels(&pixmap_))
      return;
    MSAN_CHECK_MEM_IS_INITIALIZED(pixmap_.addr(), pixmap_.computeByteSize());
  }
  is_valid_ = true;
}

ImageDataBuffer::ImageDataBuffer(const SkPixmap& pixmap)
    : pixmap_(pixmap),
      is_valid_(pixmap_.addr() &&
                !gfx::Size(pixmap.width(), pixmap.height()).IsEmpty()) {}

std::unique_ptr<ImageDataBuffer> ImageDataBuffer::Create(
    scoped_refptr<StaticBitmapImage> image) {
  std::unique_ptr<ImageDataBuffer> buffer =
      base::WrapUnique(new ImageDataBuffer(image));
  if (!buffer->IsValid())
    return nullptr;
  return buffer;
}

std::unique_ptr<ImageDataBuffer> ImageDataBuffer::Create(
    const SkPixmap& pixmap) {
  std::unique_ptr<ImageDataBuffer> buffer =
      base::WrapUnique(new ImageDataBuffer(pixmap));
  if (!buffer->IsValid())
    return nullptr;
  return buffer;
}

base::span<const uint8_t> ImageDataBuffer::PixelData() const {
  DCHECK(is_valid_);
  return gfx::SkPixmapToSpan(pixmap_);
}

bool ImageDataBuffer::EncodeImage(const ImageEncodingMimeType mime_type,
                                  const double& quality,
                                  Vector<unsigned char>* encoded_image) const {
  // Ghost Shell Anty: apply per-profile noise to pixmap pixels
  // before encoding. const_cast is sound because the pixmap is
  // backed by writable SkData we created on construction (or a
  // peeked image we're about to encode away from). No-op when
  // GhostShellConfig isn't active.
  SkPixmap mut = pixmap_;
  GhostShellApplyCanvasNoise(&mut);
  return ImageEncoder::Encode(encoded_image, mut, mime_type, quality);
}

String ImageDataBuffer::ToDataURL(const ImageEncodingMimeType mime_type,
                                  const double& quality) const {
  DCHECK(is_valid_);
  Vector<unsigned char> result;
  // Ghost Shell Anty: same noise injection as EncodeImage above.
  SkPixmap mut = pixmap_;
  GhostShellApplyCanvasNoise(&mut);
  if (!ImageEncoder::Encode(&result, mut, mime_type, quality)) {
    return "data:,";
  }
  return StrCat({"data:", ImageEncoderUtils::MimeTypeName(mime_type),
                 ";base64,", Base64Encode(result)});
}

}  // namespace blink
