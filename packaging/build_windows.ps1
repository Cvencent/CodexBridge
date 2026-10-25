param(
    [string]$Version = "2.2.10",
    [switch]$SkipInstaller,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Dist = Join-Path $Root "dist"
$Release = Join-Path $Root "release"
$Work = Join-Path $Root "build\windows"

if (-not $SkipTests) {
    python -m unittest discover -s (Join-Path $Root "tests") -v
    if ($LASTEXITCODE -ne 0) { throw "CodexBridge tests failed" }
}

if (Test-Path $Dist) { Remove-Item -LiteralPath $Dist -Recurse -Force }
if (Test-Path $Work) { Remove-Item -LiteralPath $Work -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Dist, $Release, $Work | Out-Null

python -m PyInstaller --noconfirm --clean --distpath $Dist --workpath $Work (Join-Path $PSScriptRoot "CodexBridge.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$Exe = Join-Path $Dist "CodexBridge.exe"
if (-not (Test-Path -LiteralPath $Exe -PathType Leaf)) {
    throw "PyInstaller did not produce dist\CodexBridge.exe"
}
$Executables = @(Get-ChildItem -LiteralPath $Dist -Filter "*.exe" -File -Recurse)
if ($Executables.Count -ne 1 -or $Executables[0].FullName -ne $Exe) {
    throw "Expected exactly one executable in dist: $Exe"
}

# Offline Vosk speech needs these native libraries next to the module. They are
# dlopen'd at runtime, so a missing one is invisible until a recording silently
# falls back to System.Speech. Fail the build instead of shipping that.
$RequiredVoskFiles = @(
    "vosk/libvosk.dll",
    "vosk/libstdc++-6.dll",
    "vosk/libgcc_s_seh-1.dll",
    "vosk/libwinpthread-1.dll"
)
$VoskCheck = @"
import sys
from PyInstaller.archive.readers import CArchiveReader

required = {
    "vosk/libvosk.dll",
    "vosk/libstdc++-6.dll",
    "vosk/libgcc_s_seh-1.dll",
    "vosk/libwinpthread-1.dll",
}
reader = CArchiveReader(sys.argv[1])
names = {str(name).replace("\\", "/") for name in reader.toc}
missing = sorted(required - names)
if missing:
    print("MISSING:" + ",".join(missing))
    sys.exit(1)
"@
$Missing = python -c $VoskCheck $Exe
if ($LASTEXITCODE -ne 0) {
    throw "Built executable is missing bundled offline speech file: $Missing"
}

$Portable = Join-Path $Release "CodexBridge-$Version-win64-portable.zip"
if (Test-Path -LiteralPath $Portable) { Remove-Item -LiteralPath $Portable -Force }
Compress-Archive -LiteralPath $Exe -DestinationPath $Portable -CompressionLevel Optimal

if (-not $SkipInstaller) {
    $Iscc = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if (-not $Iscc) {
        $Candidates = @(
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
            "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
        )
        foreach ($Candidate in $Candidates) {
            if (Test-Path $Candidate) { $Iscc = $Candidate; break }
        }
    }
    if (-not $Iscc) {
        throw "Inno Setup 6 (ISCC.exe) was not found. Install it with: winget install -e --id JRSoftware.InnoSetup"
    }
    & $Iscc "/DAppVersion=$Version" "/DSourceDir=$Dist" "/DOutputDir=$Release" (Join-Path $PSScriptRoot "CodexBridge.iss")
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
}

Get-ChildItem -LiteralPath $Release | Select-Object Name,Length
