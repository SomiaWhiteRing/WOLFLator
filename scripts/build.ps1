$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

& (Join-Path $PSScriptRoot "fetch_vendor.ps1")
$uv = Join-Path $root "vendor\uv.exe"
$python = Join-Path $root ".venv\Scripts\python.exe"
if (!(Test-Path -LiteralPath $python)) {
    & $uv venv (Join-Path $root ".venv")
}
& $uv pip install --python $python -r requirements-dev.txt
$openpyxlLicense = Get-ChildItem -File (Join-Path $root ".venv\Lib\site-packages\openpyxl-*.dist-info\LICENCE.rst") | Select-Object -First 1
if (!$openpyxlLicense) {
    throw "openpyxl license file was not found."
}
Copy-Item -Force -LiteralPath $openpyxlLicense.FullName -Destination (Join-Path $root "vendor\licenses\openpyxl-MIT.rst")
& $python -m unittest discover -s tests -v
& $python -m PyInstaller --noconfirm WOLFLator.spec

$dist = Join-Path $root "dist\WOLFLator"
Copy-Item -Force -LiteralPath (Join-Path $root "LICENSE") -Destination $dist
Copy-Item -Force -LiteralPath (Join-Path $root "THIRD_PARTY_NOTICES.md") -Destination $dist

$requiredRuntimeFiles = @(
    "WOLFLator.exe",
    "_internal\vendor\UberWolfCli.exe",
    "_internal\vendor\uv.exe",
    "_internal\vendor\manifest.json",
    "_internal\vendor\fonts\FusionPixel\fusion-pixel-12px-proportional-zh_hans.ttf",
    "_internal\vendor\fonts\FusionPixel\OFL.txt"
)
foreach ($relative in $requiredRuntimeFiles) {
    if (!(Test-Path -LiteralPath (Join-Path $dist $relative))) {
        throw "Packaged runtime file is missing: $relative"
    }
}

$hashLines = Get-ChildItem -LiteralPath $dist -Recurse -File |
    Where-Object { $_.Extension -in @(".exe", ".dll", ".pyd", ".ttf") } |
    Sort-Object FullName |
    ForEach-Object {
        $relative = [IO.Path]::GetRelativePath($dist, $_.FullName).Replace("\", "/")
        $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant()
        "$hash *$relative"
    }
if (!$hashLines) {
    throw "No packaged binaries were found for SHA256SUMS.txt."
}
[IO.File]::WriteAllLines(
    (Join-Path $dist "SHA256SUMS.txt"),
    [string[]]$hashLines,
    [Text.UTF8Encoding]::new($false)
)

Write-Host "Build ready: $dist"
