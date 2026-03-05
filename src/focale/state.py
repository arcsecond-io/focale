from __future__ import annotations

import json
import os
import stat
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .exceptions import FocaleStateError


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _restrict_permissions(path: Path) -> None:
    if os.name != "nt" and path.exists():
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


@dataclass
class AuthSession:
    username: str
    access_token: str
    auth_type: str = "token"
    access_exp: int | None = None
    refresh_token: str | None = None
    refresh_exp: int | None = None
    created_at: str = field(default_factory=_utcnow)


@dataclass
class InstallationRecord:
    agent_uuid: str
    public_key_b64: str
    scope_type: str
    scope_value: str
    created_at: str = field(default_factory=_utcnow)


@dataclass
class FocaleState:
    workspace_id: str
    hub_url: str | None = None
    auth: AuthSession | None = None
    installations: dict[str, InstallationRecord] = field(default_factory=dict)

    @classmethod
    def config_dir(cls) -> Path:
        home = Path.home()
        if os.name == "nt":
            root = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
            base = Path(root) if root else home / "AppData" / "Roaming"
            return base / "Arcsecond" / "focale"
        if sys.platform == "darwin":
            return home / "Library" / "Application Support" / "focale"
        xdg_root = os.environ.get("XDG_CONFIG_HOME")
        if xdg_root:
            return Path(xdg_root).expanduser() / "focale"
        return home / ".config" / "focale"

    @classmethod
    def state_file(cls) -> Path:
        return cls.config_dir() / "state.json"

    @classmethod
    def private_key_file(cls) -> Path:
        return cls.config_dir() / "agent-key.pem"

    @staticmethod
    def scope_key(scope_type: str, scope_value: str) -> str:
        return f"{scope_type}:{scope_value}"

    @classmethod
    def load(cls) -> "FocaleState":
        path = cls.state_file()
        if not path.exists():
            return cls(workspace_id=uuid.uuid4().hex)

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FocaleStateError(f"Unable to read {path}: {exc}") from exc

        installs = {}
        try:
            for key, record in (data.get("installations") or {}).items():
                installs[key] = InstallationRecord(**record)
        except TypeError as exc:
            raise FocaleStateError(f"Invalid installation record in {path}: {exc}") from exc

        workspace_id = data.get("workspace_id") or uuid.uuid4().hex
        auth_payload = data.get("auth")
        auth = None
        if auth_payload:
            try:
                auth = AuthSession(**auth_payload)
            except TypeError as exc:
                raise FocaleStateError(f"Invalid auth record in {path}: {exc}") from exc
        return cls(
            workspace_id=workspace_id,
            hub_url=data.get("hub_url"),
            auth=auth,
            installations=installs,
        )

    def save(self) -> None:
        directory = self.config_dir()
        directory.mkdir(parents=True, exist_ok=True)

        payload = {
            "workspace_id": self.workspace_id,
            "hub_url": self.hub_url,
            "auth": asdict(self.auth) if self.auth else None,
            "installations": {
                key: asdict(record) for key, record in self.installations.items()
            },
        }
        path = self.state_file()
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        _restrict_permissions(path)

    def get_installation(
        self, *, scope_type: str, scope_value: str
    ) -> InstallationRecord | None:
        return self.installations.get(self.scope_key(scope_type, scope_value))

    def set_installation(self, record: InstallationRecord) -> None:
        key = self.scope_key(record.scope_type, record.scope_value)
        self.installations[key] = record

    def clear_installation(self, *, scope_type: str, scope_value: str) -> None:
        key = self.scope_key(scope_type, scope_value)
        self.installations.pop(key, None)
