param(
    [string]$Version = "1.3.1",
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Dist = Join-Path $Root "dist"
$Release = Join-Path $Root "release"
$Work = Join-Path $Root "build\windows"

if (Test-Path $Dist) { Remove-Item -LiteralPath $Dist -Recurse -Force }
if (Test-Path $Work) { Remove-Item -LiteralPath $Work -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Dist, $Release, $Work | Out-Null

python -m PyInstaller --noconfirm --clean --distpath $Dist --workpath $Work (Join-Path $PSScriptRoot "CodexBridge.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$Bundle = Join-Path $Dist "CodexBridge"
if (-not (Test-Path (Join-Path $Bundle "CodexBridge.exe"))) {
    throw "PyInstaller did not produce CodexBridge.exe"
}

$Portable = Join-Path $Release "CodexBridge-$Version-win64-portable.zip"
if (Test-Path $Portable) { Remove-Item -LiteralPath $Portable -Force }
Compress-Archive -Path (Join-Path $Bundle "*") -DestinationPath $Portable -CompressionLevel Optimal

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
    & $Iscc "/DAppVersion=$Version" "/DSourceDir=$Bundle" "/DOutputDir=$Release" (Join-Path $PSScriptRoot "CodexBridge.iss")
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
}

Get-ChildItem -LiteralPath $Release | Select-Object Name,Length