from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

import websockets
from websockets.client import WebSocketClientProtocol

from .agent_auth import AgentKeypair
from .exceptions import HubProtocolError


def frame(message_type: str, payload: dict | None = None) -> dict:
    return {
        "v": 1,
        "type": message_type,
        "id": uuid.uuid4().hex,
        "ts": int(time.time()),
        "payload": payload or {},
    }


@dataclass(frozen=True)
class HubWelcome:
    agent_uuid: str
    session_id: str
    keepalive_s: int


class HubClient:
    def __init__(
        self,
        *,
        hub_url: str,
        workspace_id: str,
        agent_uuid: str,
        jwt: str,
        keypair: AgentKeypair,
    ) -> None:
        self.hub_url = hub_url
        self.workspace_id = workspace_id
        self.agent_uuid = agent_uuid
        self.jwt = jwt
        self.keypair = keypair

    async def connect(self, *, once: bool = False, echo=print) -> HubWelcome:
        try:
            async with websockets.connect(
                self.hub_url,
                ping_interval=None,
                max_size=256 * 1024,
                open_timeout=20,
            ) as websocket:
                await self._send(
                    websocket,
                    "hello",
                    {
                        "agent_uuid": self.agent_uuid,
                        "workspace_id": self.workspace_id,
                        "jwt": self.jwt,
                    },
                )

                challenge = await self._recv(websocket)
                if challenge.get("type") != "challenge":
                    self._raise_for_error(challenge, "Expected a Hub challenge frame.")

                payload = challenge.get("payload") or {}
                nonce_b64 = payload.get("nonce")
                challenge_agent_uuid = payload.get("agent_uuid")
                if not nonce_b64 or not challenge_agent_uuid:
                    raise HubProtocolError("Hub challenge was missing nonce or agent_uuid.")

                await self._send(
                    websocket,
                    "challenge_response",
                    {
                        "agent_uuid": challenge_agent_uuid,
                        "nonce": nonce_b64,
                        "sig": self.keypair.sign_nonce(
                            agent_uuid=challenge_agent_uuid,
                            nonce_b64=nonce_b64,
                        ),
                    },
                )

                welcome_frame = await self._recv(websocket)
                if welcome_frame.get("type") != "welcome":
                    self._raise_for_error(welcome_frame, "Expected a Hub welcome frame.")

                welcome_payload = welcome_frame.get("payload") or {}
                welcome = HubWelcome(
                    agent_uuid=welcome_payload.get("agent_uuid", self.agent_uuid),
                    session_id=welcome_payload.get("session_id", ""),
                    keepalive_s=int(welcome_payload.get("keepalive_s", 0)),
                )

                if once:
                    return welcome

                echo(
                    "Hub session established. "
                    f"session_id={welcome.session_id} keepalive_s={welcome.keepalive_s}"
                )

                while True:
                    message = await self._recv(websocket)
                    message_type = message.get("type")
                    if message_type == "ping":
                        await self._send(websocket, "pong", {})
                        continue
                    if message_type == "error":
                        self._raise_for_error(message, "Hub returned an error frame.")

                    echo(json.dumps(message, indent=2, sort_keys=True))

                return welcome
        except (OSError, websockets.InvalidHandshake, websockets.InvalidURI) as exc:
            raise HubProtocolError(f"Unable to connect to Hub at {self.hub_url}: {exc}") from exc

    async def _send(
        self,
        websocket: WebSocketClientProtocol,
        message_type: str,
        payload: dict,
    ) -> None:
        await websocket.send(
            json.dumps(frame(message_type, payload), separators=(",", ":"))
        )

    async def _recv(self, websocket: WebSocketClientProtocol) -> dict:
        try:
            raw = await websocket.recv()
        except websockets.ConnectionClosed as exc:
            raise HubProtocolError(
                f"Hub connection closed unexpectedly: code={exc.code} reason={exc.reason}"
            ) from exc

        if not isinstance(raw, str):
            raise HubProtocolError("Hub returned a non-text websocket frame.")

        try:
            message = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HubProtocolError(f"Hub returned invalid JSON: {exc}") from exc

        if not isinstance(message, dict):
            raise HubProtocolError("Hub returned a non-object JSON frame.")

        return message

    def _raise_for_error(self, message: dict, fallback: str) -> None:
        payload = message.get("payload") or {}
        detail = payload.get("message") or fallback
        code = payload.get("code")
        if code:
            raise HubProtocolError(f"{detail} [{code}]")
        raise HubProtocolError(detail)
