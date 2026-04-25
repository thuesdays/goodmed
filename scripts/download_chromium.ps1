# ================================================================
#  download_chromium.ps1 -- pull the patched Chromium binary from a
#  GitHub release asset and unpack it into chrome_win64\.
#
#  Why this script exists
#  ----------------------
#  The patched Chromium runtime (chrome_win64\) is ~600 MB and lives
#  outside git (.gitignore'd, way past GitHub's 100 MB per-file in-
#  tree limit). Source-only checkouts and CI need a way to populate
#  chrome_win64\ before the dashboard or installer can do anything
#  useful. We host the pre-built archive as a GitHub release asset
#  and pull it down here.
#
#  Resolution priority for which release to download:
#    1. -Tag <tag>  argument                                      (CLI)
#    2. CHROME_WIN64_TAG environment variable                    (CI)
#    3. The "latest" published release                          (default)
#
#  Asset naming convention (set by package_chromium.ps1):
#      chrome_win64-vX.Y.Z.W.zip
#      chrome_win64-vX.Y.Z.W.zip.sha256
#
#  Idempotency: chrome_win64\ already populated -> exit early.
#  Use -Force to re-download anyway.
#
#  Usage:
#    .\scripts\download_chromium.ps1
#    .\scripts\download_chromium.ps1 -Tag v0.2.0.3
#    .\scripts\download_chromium.ps1 -Force
# ================================================================

[CmdletBinding()]
param(
    [string]$Tag = "",
    [switch]$Force,
    [string]$DestDir = "",
    [string]$Repo = "thuesdays/ghost_shell_browser"
)

$ErrorActionPreference = "Stop"

$here     = Split-Path -Parent $MyInvocation.MyCommand.Definition
$repoRoot = Split-Path -Parent $here

if (-not $DestDir) { $DestDir = Join-Path $repoRoot "chrome_win64" }
if (-not $Tag -and $env:CHROME_WIN64_TAG) { $Tag = $env:CHROME_WIN64_TAG }

# TLS 1.2 -- older PowerShell 5 defaults to SSL 3.0/TLS 1.0 which
# the GitHub API has rejected since 2018.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Resolve-Release {
    param([string]$Repo, [string]$Tag)
    $base = "https://api.github.com/repos/$Repo"
    $hdr  = @{
        "User-Agent" = "ghost_shell_browser-download_chromium"
        "Accept"     = "application/vnd.github+json"
    }
    if ($env:GITHUB_TOKEN) { $hdr["Authorization"] = "Bearer $env:GITHUB_TOKEN" }
    if ($Tag) { $uri = "$base/releases/tags/$Tag" } else { $uri = "$base/releases/latest" }
    Write-Host "[download] querying $uri"
    try {
        $rel = Invoke-RestMethod -Uri $uri -Headers $hdr -TimeoutSec 30
    } catch {
        Write-Error "[download] release lookup failed: $($_.Exception.Message)"
        Write-Host  "[download]   - check the tag exists at https://github.com/$Repo/releases"
        Write-Host  "[download]   - or pass a different -Tag / -Repo"
        exit 1
    }
    return $rel
}

function Find-Asset {
    param($Release, [string]$Pattern)
    foreach ($a in $Release.assets) {
        if ($a.name -like $Pattern) { return $a }
    }
    return $null
}

function Test-CurrentInstall {
    param([string]$DestDir)
    foreach ($f in @("chrome.exe", "chrome.dll", "resources.pak")) {
        if (-not (Test-Path (Join-Path $DestDir $f))) { return $false }
    }
    return $true
}

# ---- Look up the release --------------------------------------
$release = Resolve-Release -Repo $Repo -Tag $Tag
$tagName = $release.tag_name
Write-Host "[download] release: $tagName  ($($release.name))"

$zipAsset = Find-Asset -Release $release -Pattern "chrome_win64-*.zip"
if (-not $zipAsset) {
    Write-Error "[download] no chrome_win64-*.zip asset found on release '$tagName'"
    Write-Host  "[download] assets present:"
    foreach ($a in $release.assets) { Write-Host "[download]   - $($a.name)" }
    exit 1
}
$shaAsset = Find-Asset -Release $release -Pattern "chrome_win64-*.zip.sha256"

if (-not $Force -and (Test-CurrentInstall -DestDir $DestDir)) {
    Write-Host "[download] $DestDir already has chrome.exe+chrome.dll+resources.pak"
    Write-Host "[download] skipping - pass -Force to re-download anyway"
    exit 0
}

$tmpDir = Join-Path $env:TEMP "ghost_shell_chromium_dl"
if (-not (Test-Path $tmpDir)) { New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null }

$zipPath = Join-Path $tmpDir $zipAsset.name
$shaPath = if ($shaAsset) { Join-Path $tmpDir $shaAsset.name } else { $null }

Write-Host ("[download] zip url: {0}" -f $zipAsset.browser_download_url)
Write-Host ("[download] size:    {0:N1} MB" -f ($zipAsset.size / 1MB))
Write-Host ("[download] target:  {0}" -f $zipPath)
Write-Host "[download] downloading..."

try {
    Invoke-WebRequest -Uri $zipAsset.browser_download_url `
                      -OutFile $zipPath `
                      -UseBasicParsing `
                      -TimeoutSec 1800
} catch {
    Write-Error "[download] zip download failed: $($_.Exception.Message)"
    exit 1
}

if ($shaAsset) {
    try {
        Invoke-WebRequest -Uri $shaAsset.browser_download_url `
                          -OutFile $shaPath `
                          -UseBasicParsing `
                          -TimeoutSec 60
    } catch {
        Write-Host "[download] sha sidecar fetch failed - skipping verification"
        $shaPath = $null
    }
}

if ($shaPath -and (Test-Path $shaPath)) {
    $expected = ((Get-Content -Raw -Path $shaPath) -split '\s+')[0].ToLower()
    $actual   = (Get-FileHash -Path $zipPath -Algorithm SHA256).Hash.ToLower()
    if ($expected -ne $actual) {
        Write-Error "[download] HASH MISMATCH - refusing to extract"
        Write-Host  "[download]   expected: $expected"
        Write-Host  "[download]   actual:   $actual"
        Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
        exit 1
    }
    Write-Host "[download] sha256 OK ($actual)"
} else {
    Write-Host "[download] no sha256 sidecar - skipping hash check (less safe)"
}

if (Test-Path $DestDir) {
    Write-Host "[download] wiping existing $DestDir before extract"
    Remove-Item $DestDir -Recurse -Force
}
New-Item -ItemType Directory -Path $DestDir -Force | Out-Null

Write-Host "[download] extracting to $DestDir ..."
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::ExtractToDirectory($zipPath, $DestDir)

foreach ($f in @("chrome.exe", "chrome.dll", "resources.pak")) {
    if (-not (Test-Path (Join-Path $DestDir $f))) {
        Write-Error "[download] $f missing after extract - archive may be corrupt"
        exit 1
    }
}

Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
if ($shaPath) { Remove-Item $shaPath -Force -ErrorAction SilentlyContinue }

$nFiles = (Get-ChildItem $DestDir -Recurse -File).Count
Write-Host ""
Write-Host "[download] done."
Write-Host "[download]   tag:    $tagName"
Write-Host "[download]   dest:   $DestDir"
Write-Host "[download]   files:  $nFiles"
exit 0
