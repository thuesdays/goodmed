$target = "f:\projects\ghost_shell_browser\с++"
$src = "f:\projects\chromium\src"

function Copy-Source($relPath) {
    $fullSrc = Join-Path $src $relPath
    $fullDest = Join-Path $target $relPath
    $destDir = Split-Path $fullDest
    if (!(Test-Path $destDir)) { New-Item -ItemType Directory -Force $destDir }
    Copy-Item $fullSrc $fullDest -Force
    Write-Host "Copied: $relPath"
}

$files = @(
    "third_party/blink/renderer/platform/ghost_shell_config.h",
    "third_party/blink/renderer/platform/ghost_shell_config.cc",
    "third_party/blink/renderer/modules/webgpu/gpu_adapter.cc",
    "third_party/blink/renderer/modules/webgpu/gpu_adapter_info.cc",
    "third_party/blink/renderer/modules/media_capabilities/media_capabilities.cc",
    "third_party/blink/renderer/modules/canvas/canvas2d/base_rendering_context_2d.cc",
    "third_party/blink/renderer/modules/webgl/webgl_rendering_context_base.cc",
    "third_party/blink/renderer/modules/webaudio/realtime_analyser.cc",
    "third_party/blink/renderer/modules/webaudio/base_audio_context.h",
    "third_party/blink/renderer/modules/webaudio/audio_buffer.cc",
    "third_party/blink/renderer/modules/speech/speech_synthesis.cc",
    "third_party/blink/renderer/modules/screen_orientation/screen_orientation.cc",
    "third_party/blink/renderer/modules/permissions/permissions.cc",
    "third_party/blink/renderer/modules/netinfo/network_information.cc",
    "third_party/blink/renderer/modules/mediastream/media_devices.cc",
    "third_party/blink/renderer/modules/battery/battery_manager.cc",
    "third_party/blink/renderer/core/frame/screen.cc",
    "third_party/blink/renderer/core/frame/navigator_device_memory.cc",
    "third_party/blink/renderer/core/frame/navigator_id.cc",
    "third_party/blink/renderer/core/frame/navigator_language.cc",
    "third_party/blink/renderer/core/frame/navigator_concurrent_hardware.cc",
    "third_party/blink/renderer/core/execution_context/navigator_base.cc",
    "third_party/blink/renderer/core/timing/performance.cc",
    "third_party/blink/renderer/core/frame/local_dom_window.cc",
    "third_party/blink/renderer/core/dom/element.cc",
    "third_party/blink/renderer/core/dom/events/event.cc",
    "third_party/blink/renderer/platform/fonts/font_cache.cc",
    "third_party/blink/renderer/bindings/core/v8/v8_initializer.cc",
    "third_party/webrtc/pc/peer_connection.cc"
)

foreach ($file in $files) {
    Copy-Source $file
}
