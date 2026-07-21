$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$vendor = Join-Path $root "vendor"
$manifest = Get-Content -Raw -LiteralPath (Join-Path $vendor "manifest.json") | ConvertFrom-Json
$temp = Join-Path $vendor ".download"
New-Item -ItemType Directory -Force -Path $temp | Out-Null

function Get-VerifiedFile {
    param([string]$Url, [string]$Destination, [string]$Sha256)
    $part = "$Destination.part"
    Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $part
    & curl.exe -L --fail --retry 3 --retry-all-errors --retry-delay 2 --output $part $Url
    if ($LASTEXITCODE -ne 0) {
        throw "Download failed with curl exit code ${LASTEXITCODE}: $Url"
    }
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $part).Hash.ToLowerInvariant()
    if ($actual -ne $Sha256.ToLowerInvariant()) {
        Remove-Item -Force -LiteralPath $part
        throw "SHA-256 mismatch for $Url. Expected $Sha256, got $actual."
    }
    Move-Item -Force -LiteralPath $part -Destination $Destination
}

function Get-PlainFile {
    param([string]$Url, [string]$Destination)
    & curl.exe -L --fail --retry 3 --retry-all-errors --retry-delay 2 --output $Destination $Url
    if ($LASTEXITCODE -ne 0) {
        throw "Download failed with curl exit code ${LASTEXITCODE}: $Url"
    }
}

$uberwolf = Join-Path $vendor "UberWolfCli.exe"
if (!(Test-Path -LiteralPath $uberwolf) -or
    (Get-FileHash -Algorithm SHA256 -LiteralPath $uberwolf).Hash.ToLowerInvariant() -ne $manifest.uberwolf.sha256) {
    Get-VerifiedFile $manifest.uberwolf.url $uberwolf $manifest.uberwolf.sha256
}

$uvTarget = Join-Path $vendor "uv.exe"
$uvReady = (Test-Path -LiteralPath $uvTarget) -and
    ((Get-FileHash -Algorithm SHA256 -LiteralPath $uvTarget).Hash.ToLowerInvariant() -eq $manifest.uv.exe_sha256)
if (!$uvReady) {
    $localUv = Get-Command uv -ErrorAction SilentlyContinue
    if ($localUv -and ((Get-FileHash -Algorithm SHA256 -LiteralPath $localUv.Source).Hash.ToLowerInvariant() -eq $manifest.uv.exe_sha256)) {
        Copy-Item -Force -LiteralPath $localUv.Source -Destination $uvTarget
        $uvReady = $true
    }
}
if (!$uvReady) {
    $uvArchive = Join-Path $temp "uv.zip"
    if (!(Test-Path -LiteralPath $uvArchive) -or
        (Get-FileHash -Algorithm SHA256 -LiteralPath $uvArchive).Hash.ToLowerInvariant() -ne $manifest.uv.sha256) {
        Get-VerifiedFile $manifest.uv.url $uvArchive $manifest.uv.sha256
    }
    $uvExtract = Join-Path $temp "uv"
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue -LiteralPath $uvExtract
    Expand-Archive -LiteralPath $uvArchive -DestinationPath $uvExtract
    Copy-Item -Force -LiteralPath (Join-Path $uvExtract "uv.exe") -Destination $uvTarget
}
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $uvTarget).Hash.ToLowerInvariant() -ne $manifest.uv.exe_sha256) {
    throw "uv.exe SHA-256 mismatch after extraction."
}

$licenses = Join-Path $vendor "licenses"
New-Item -ItemType Directory -Force -Path $licenses | Out-Null
Get-PlainFile "https://raw.githubusercontent.com/Sinflower/UberWolf/v0.6.3/LICENSE" (Join-Path $licenses "UberWolf-MIT.txt")
Get-PlainFile "https://raw.githubusercontent.com/astral-sh/uv/0.10.9/LICENSE-MIT" (Join-Path $licenses "uv-MIT.txt")
Get-PlainFile "https://raw.githubusercontent.com/astral-sh/uv/0.10.9/LICENSE-APACHE" (Join-Path $licenses "uv-APACHE.txt")
Get-PlainFile "https://raw.githubusercontent.com/spdx/license-list-data/v3.27.0/text/LGPL-3.0-only.txt" (Join-Path $licenses "PySide6-LGPL-3.0.txt")
Get-PlainFile "https://raw.githubusercontent.com/spdx/license-list-data/v3.27.0/text/GPL-3.0-only.txt" (Join-Path $licenses "PySide6-GPL-3.0.txt")

Write-Host "Vendor tools are ready in $vendor"
