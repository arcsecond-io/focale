# Focale

Focale is a small desktop/CLI bootstrap for Focale users that:

- logs in with the user's Arcsecond account
- creates and stores a local Hub agent identity
- enrolls that identity with Arcsecond when needed
- mints a short-lived Hub JWT
- connects to the Arcsecond Hub using the signed Ed25519 challenge flow

The package depends on the published [`arcsecond`](https://pypi.org/project/arcsecond/) CLI/library and reuses its account configuration instead of creating a second login system.

By default, Focale uses Arcsecond password login to obtain a short-lived bearer JWT plus a refresh token. That avoids storing a long-lived Access Key in the normal path. Access Key login is still available as a fallback.

## User install

### Python / terminal

```bash
pip install focale
```

Then:

```bash
focale login
focale context list
focale context use personal
focale connect --hub-url wss://hub.arcsecond.io/ws/agent
```

### Desktop GUI

`python -m focale` launches the PySide6 desktop app when no CLI arguments are given.
The existing `focale` console script remains available for terminal-driven workflows.

### Windows installer

This repository includes a bootstrap for building a Windows installer from CI. The installer packages
the PySide6 desktop app as `focale.exe`.

See:

- [`.github/workflows/windows-installer.yml`](.github/workflows/windows-installer.yml)
- [`packaging/windows/focale.iss`](packaging/windows/focale.iss)
- [`scripts/build_windows.ps1`](scripts/build_windows.ps1)

## Commands

```bash
focale login
focale login --auth-mode access-key
focale status
focale context show
focale context list
focale context use personal
focale context use my-observatory
focale doctor --hub-url wss://hub.arcsecond.io/ws/agent
focale doctor --hub-url wss://hub.arcsecond.io/ws/agent --json
focale connect --hub-url wss://hub.arcsecond.io/ws/agent
focale --api-server https://api.arcsecond.dev connect --hub-url wss://hub.arcsecond.dev/ws/agent --once
focale connect --organisation my-observatory --hub-url wss://hub.arcsecond.io/ws/agent
focale platesolver status
focale platesolver solve --peaks-file ./peaks.json
```

`focale connect` will automatically:

1. refresh the Arcsecond access JWT when needed
2. create a local Ed25519 keypair if needed
3. enroll a personal or organisation-scoped agent installation if needed
4. mint a Hub JWT
5. discover local ASCOM Remote (Alpaca) servers and register new ones in the selected context
6. complete the Hub challenge-response handshake

You can set the default context once and keep connect/doctor simple:

```bash
focale context use personal
# or
focale context use my-observatory
```

## Plate solving

Plate solving is included with `pip install focale` — `arcsecond-astrometry`
is a mandatory dependency and ships native binaries for Windows, macOS, and Linux with no Docker
or external tooling required.

```bash
focale platesolver status
focale platesolver solve --peaks-file ./peaks.json --scales 6
```

You can also target a remote service:

```bash
focale platesolver status --service-url http://127.0.0.1:8900
focale platesolver solve --service-url http://127.0.0.1:8900 --peaks-file ./peaks.json
```

## Development

If you are developing Focale next to the local Arcsecond CLI checkout:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ../arcsecond-cli
pip install -e .[dev]
pytest -q
```

## Publishing

This repo includes:

- a PEP 621 `pyproject.toml`
- a CI workflow for tests
- a PyPI publish workflow on tags such as `v0.2.0`
- a Windows installer workflow that builds a PyInstaller bundle and wraps it with Inno Setup
