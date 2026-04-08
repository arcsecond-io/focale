param(
    [string]$Version = "",
    [ValidateSet("production", "staging", "dev")]
    [string]$Environment = "production"
)

$ErrorActionPreference = "Stop"

if (-not $Version) {
    $VersionMatch = Select-String -Path "pyproject.toml" -Pattern '^version = "([^"]+)"$' | Select-Object -First 1
    if (-not $VersionMatch) {
        throw "Could not determine version from pyproject.toml"
    }
    $Version = $VersionMatch.Matches[0].Groups[1].Value
}

# Resolve environment-specific metadata
switch ($Environment) {
    "production" {
        $AppName    = "Focale"
        $ExeName    = "focale"
        $InstallDir = "{autopf}\Arcsecond\Focale"
        $AppId      = "11A3125E-D7EA-487D-9998-67E95343F4A5"
    }
    "staging" {
        $AppName    = "Focale Staging"
        $ExeName    = "focale-staging"
        $InstallDir = "{autopf}\Arcsecond\Focale Staging"
        $AppId      = "5B3A2E10-7C4D-4F9A-B1E8-2D6F0A3C5E71"
    }
    "dev" {
        $AppName    = "Focale Dev"
        $ExeName    = "focale-dev"
        $InstallDir = "{autopf}\Arcsecond\Focale Dev"
        $AppId      = "9E7C4B20-3A5F-4D8C-C2F9-3E70B4D6F820"
    }
}

# Bake the environment into the source
Set-Content -Path "src/focale/_environment.py" -Value @"
# This file is generated during the build process. Do not edit manually.
# The value is baked at build time to produce environment-specific applications.
ENVIRONMENT = "$Environment"
"@

python -m pip install --upgrade pip
python -m pip install .[dev]

pyinstaller `
  --noconfirm `
  --clean `
  --onedir `
  --windowed `
  --name $ExeName `
  --paths src `
  --collect-submodules arcsecond `
  --collect-submodules focale `
  src/focale/gui_main.py

iscc `
  /DMyAppVersion=$Version `
  "/DMyAppName=$AppName" `
  "/DMyAppId=$AppId" `
  "/DMyExeName=$ExeName.exe" `
  "/DMyDefaultDirName=$InstallDir" `
  packaging/windows/focale.iss
