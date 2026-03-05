# Focale

Focale is a thin desktop/CLI bootstrap for Arcsecond users that:

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
focale connect --hub-url wss://hub.arcsecond.io/ws/agent
```

### Windows installer

This repository includes a bootstrap for building a Windows installer from CI. The installer packages the `focale` executable and can optionally add it to `PATH`.

See:

- [`.github/workflows/windows-installer.yml`](.github/workflows/windows-installer.yml)
- [`packaging/windows/focale.iss`](packaging/windows/focale.iss)
- [`scripts/build_windows.ps1`](scripts/build_windows.ps1)

## Commands

```bash
focale login
focale login --auth-mode access-key
focale status
focale doctor --hub-url wss://hub.arcsecond.io/ws/agent
focale doctor --hub-url wss://hub.arcsecond.io/ws/agent --json
focale connect --hub-url wss://hub.arcsecond.io/ws/agent
focale --api-server https://api.arcsecond.dev connect --hub-url wss://hub.arcsecond.dev/ws/agent --once
focale connect --organisation my-observatory --hub-url wss://hub.arcsecond.io/ws/agent
```

`focale connect` will automatically:

1. refresh the Arcsecond access JWT when needed
2. create a local Ed25519 keypair if needed
3. enroll a personal or organisation-scoped agent installation if needed
4. mint a Hub JWT
5. complete the Hub challenge-response handshake

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
- a PyPI publish workflow on tags such as `v0.1.0`
- a Windows installer workflow that builds a PyInstaller bundle and wraps it with Inno Setup
