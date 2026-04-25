# ================================================================
#  package_chromium.ps1 -- pack chrome_win64\ into a release-ready
#  zip + SHA256 sidecar for upload as a GitHub release asset.
#
#  Why this script exists
#  ----------------------
#  chrome_win64\ is ~600 MB uncompressed and lives outside git
#  (.gitignore'd; chrome.dll alone is 383 MB, way past GitHub's
#  100 MB per-file in-tree limit). Distribution path is:
#
#      developer  ->  package_chromium.ps1  ->  dist\chrome_win64-vX.Y.Z.W.zip
#                                          ->  dist\chrome_win64-vX.Y.Z.W.zip.sha256
#                                          ->  manual upload to a GitHub Release as an asset
#                                          ->  CI / fellow devs run download_chromium.ps1
#
#  The .sha256 sidecar lets download_chromium.ps1 verify integrity
#  before extraction -- important because release assets do get
#  occasionally corrupted in transit, and a half-extracted Chromium
#  fails at startup with cryptic side-by-side errors.
#
#  Why System.IO.Compression and not Compress-Archive
#  --------------------------------------------------
#  PowerShell 5's Compress-Archive uses ZipFile under the hood but
#  silently truncates entries > 2 GB and chokes on chrome.dll-sized
#  files on some hosts. System.IO.Compression.ZipFile via the
#  ZIP64-aware overloads handles big single files cleanly.
#
#  Usage:
#    .\scripts\package_chromium.ps1
#    .\scripts\package_chromium.ps1 -Version "0.2.0.3"
#    .\scripts\package_chromium.ps1 -SourceDir "F:\chromium\out\GhostShell"
# ================================================================

[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$SourceDir = "",
    [string]$OutDir = ""
)

$ErrorActionPreference = "Stop"

# Resolve paths relative to repo root (parent of scripts/)
$here     = Split-Path -Parent $MyInvocation.MyCommand.Definition
$repoRoot = Split-Path -Parent $here

if (-not $SourceDir) { $SourceDir = Join-Path $repoRoot "chrome_win64" }
if (-not $OutDir)    { $OutDir    = Join-Path $repoRoot "dist" }

# Auto-resolve version from installer's build number + .iss constants
# if the caller did not pass one. Falls back to "dev" when neither
# source is present (e.g. running outside a checkout).
if (-not $Version) {
    $issPath  = Join-Path $repoRoot "installer\ghost_shell_installer.iss"
    $bnPath   = Join-Path $repoRoot "installer\.build_number"
    $major = "0"; $minor = "0"; $patch = "0"; $build = "0"
    if (Test-Path $issPath) {
        $iss = Get-Content -Raw -Path $issPath
        if ($iss -match '#define\s+AppVersionMajor\s+"(\d+)"') { $major = $Matches[1] }
        if ($iss -match '#define\s+AppVersionMinor\s+"(\d+)"') { $minor = $Matches[1] }
        if ($iss -match '#define\s+AppVersionPatch\s+"(\d+)"') { $patch = $Matches[1] }
    }
    if (Test-Path $bnPath) {
        $raw = (Get-Content -Raw -Path $bnPath) -replace '[^0-9]', ''
        if ($raw) { $build = $raw.Trim() }
    }
    $Version = "$major.$minor.$patch.$build"
}

if (-not (Test-Path $SourceDir)) {
    Write-Error "[package] source not found: $SourceDir"
    exit 1
}

# Sanity-check it really IS a Chromium build dir, not a half-empty
# leftover from a failed sync. Without chrome.exe + chrome.dll +
# resources.pak the zip would ship a non-functional package.
$required = @("chrome.exe", "chrome.dll", "resources.pak")
foreach ($f in $required) {
    if (-not (Test-Path (Join-Path $SourceDir $f))) {
        Write-Error "[package] $f missing in $SourceDir - refusing to package incomplete dir"
        exit 1
    }
}

if (-not (Test-Path $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
}

$zipName    = "chrome_win64-v$Version.zip"
$zipPath    = Join-Path $OutDir $zipName
$shaPath    = "$zipPath.sha256"

# Wipe any previous zip with the same name -- ZipFile.CreateFromDirectory
# refuses to write over an existing file.
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
if (Test-Path $shaPath) { Remove-Item $shaPath -Force }

Write-Host "[package] source:  $SourceDir"
Write-Host "[package] target:  $zipPath"
Write-Host "[package] zipping... (takes ~30-60s for ~600 MB at Optimal level)"

Add-Type -AssemblyName System.IO.Compression.FileSystem

# Optimal compression. Chromium binaries do not compress hard
# (already-optimized bytes), so ~50% reduction at most. Optimal vs
# Fastest is a marginal time trade for ~5-10% smaller asset.
$compression = [System.IO.Compression.CompressionLevel]::Optimal
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $SourceDir,
    $zipPath,
    $compression,
    $false
)

$zipBytes = (Get-Item $zipPath).Length
Write-Host ("[package] zipped: {0:N0} bytes ({1:N1} MB)" -f $zipBytes, ($zipBytes / 1MB))

# SHA256 sidecar -- formatted "<HEX_HASH>  <filename>" to mimic the
# layout produced by `sha256sum` so users can verify with the same
# tool on Linux/macOS:
#     sha256sum -c chrome_win64-v0.2.0.3.zip.sha256
Write-Host "[package] hashing..."
$sha = (Get-FileHash -Path $zipPath -Algorithm SHA256).Hash.ToLower()
"$sha  $zipName" | Set-Content -Path $shaPath -Encoding ASCII -NoNewline

Write-Host "[package] sha256:  $sha"
Write-Host "[package] sidecar: $shaPath"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Open https://github.com/thuesdays/ghost_shell_browser/releases/edit/v$Version"
Write-Host "     (or create a new release with tag v$Version)"
Write-Host "  2. Drag-and-drop both files into the 'Attach binaries' area:"
Write-Host "       $zipPath"
Write-Host "       $shaPath"
Write-Host "  3. Publish."
Write-Host ""
Write-Host "After release is live, fellow devs can fetch chrome_win64\ via:"
Write-Host "  .\scripts\download_chromium.ps1"
exit 0
