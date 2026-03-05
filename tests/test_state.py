from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from focale.state import AuthSession, FocaleState, InstallationRecord


def test_state_roundtrip():
    with TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        with patch.object(
            FocaleState,
            "config_dir",
            classmethod(lambda cls: tmp_path),
        ):
            state = FocaleState(
                workspace_id="workspace-1",
                hub_url="wss://hub.example/ws/agent",
                auth=AuthSession(
                    username="alice",
                    access_token="access-token",
                    access_exp=123,
                    refresh_token="refresh-token",
                    refresh_exp=456,
                ),
            )
            state.set_installation(
                InstallationRecord(
                    agent_uuid="agent-1",
                    public_key_b64="public-key",
                    scope_type="profile",
                    scope_value="alice",
                )
            )
            state.save()

            loaded = FocaleState.load()

            assert loaded.workspace_id == "workspace-1"
            assert loaded.hub_url == "wss://hub.example/ws/agent"
            assert loaded.auth is not None
            assert loaded.auth.username == "alice"
            assert loaded.auth.refresh_token == "refresh-token"
            record = loaded.get_installation(scope_type="profile", scope_value="alice")
            assert record is not None
            assert record.agent_uuid == "agent-1"


def test_missing_state_generates_workspace_id():
    with TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        with patch.object(
            FocaleState,
            "config_dir",
            classmethod(lambda cls: tmp_path),
        ):
            state = FocaleState.load()

            assert state.workspace_id
            assert state.hub_url is None
            assert state.installations == {}
