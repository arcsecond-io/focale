param(
    [string]$Version = "0.2.0"
)

$ErrorActionPreference = "Stop"

python -m pip install --upgrade pip
python -m pip install .[dev]

pyinstaller `
  --noconfirm `
  --clean `
  --onedir `
  --console `
  --name focale `
  --paths src `
  --collect-submodules arcsecond `
  --collect-submodules focale `
  src/focale/__main__.py

iscc /DMyAppVersion=$Version packaging/windows/focale.iss
