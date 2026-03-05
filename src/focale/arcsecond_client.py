from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
from arcsecond.api import ArcsecondConfig

from .exceptions import ArcsecondGatewayError
from .state import AuthSession, FocaleState


@dataclass(frozen=True)
class MintedHubToken:
    jwt: str
    exp: int


class ArcsecondGateway:
    def __init__(
        self,
        *,
        state: FocaleState,
        api_name: str = "cloud",
        api_server: str | None = None,
    ) -> None:
        self.state = state
        self.config = ArcsecondConfig(api_name=api_name)
        self.api_server = (api_server or self.config.api_server or "").rstrip("/")
        if not self.api_server:
            raise ArcsecondGatewayError(
                f"Unable to resolve an Arcsecond API server for profile `{api_name}`.", 400
            )

    @property
    def username(self) -> str:
        if self.state.auth and self.state.auth.username:
            return self.state.auth.username
        return self.config.username

    @property
    def is_logged_in(self) -> bool:
        return self.state.auth is not None or self.config.is_logged_in

    @property
    def auth_type(self) -> str | None:
        if self.state.auth:
            return self.state.auth.auth_type
        if self.config.access_key:
            return "key"
        return None

    @property
    def has_access_key(self) -> bool:
        return bool(self.config.access_key) or (
            bool(self.state.auth) and self.state.auth.auth_type == "key"
        )

    @property
    def has_refresh_token(self) -> bool:
        return bool(self.state.auth and self.state.auth.refresh_token)

    def login_with_access_key(self, *, username: str, access_key: str) -> None:
        self._request(
            "post",
            "auth/key/verify",
            json={"username": username, "key": access_key},
            authenticated=False,
        )
        self.state.auth = AuthSession(
            username=username,
            access_token=access_key,
            auth_type="key",
        )
        self.state.save()

    def login_with_password(self, *, username: str, password: str) -> None:
        payload = self._request(
            "post",
            "auth/token",
            json={"username": username, "password": password},
            authenticated=False,
        )
        access_token = payload.get("access")
        refresh_token = payload.get("refresh")
        response_username = payload.get("username") or username
        if not access_token or not refresh_token:
            raise ArcsecondGatewayError("Arcsecond did not return access and refresh tokens.", 500)

        self.state.auth = AuthSession(
            username=response_username,
            access_token=access_token,
            access_exp=payload.get("access_exp"),
            refresh_token=refresh_token,
            refresh_exp=payload.get("refresh_exp"),
            auth_type="token",
        )
        self.state.save()

    def refresh_access_token(self) -> None:
        session = self.require_auth_session()
        if session.auth_type != "token" or not session.refresh_token:
            raise ArcsecondGatewayError("No refreshable JWT session is available.", 401)

        now = int(time.time())
        if session.refresh_exp and now >= int(session.refresh_exp):
            raise ArcsecondGatewayError("The Arcsecond refresh token has expired. Run `focale login` again.", 401)

        payload = self._request(
            "post",
            "auth/token/refresh",
            json={"refresh": session.refresh_token},
            authenticated=False,
        )
        access_token = payload.get("access")
        refresh_token = payload.get("refresh")
        if not access_token or not refresh_token:
            raise ArcsecondGatewayError("Arcsecond returned an incomplete refresh response.", 500)

        self.state.auth = AuthSession(
            username=payload.get("username") or session.username,
            access_token=access_token,
            access_exp=payload.get("access_exp"),
            refresh_token=refresh_token,
            refresh_exp=payload.get("refresh_exp"),
            auth_type="token",
            created_at=session.created_at,
        )
        self.state.save()

    def enroll_agent(self, *, public_key_b64: str, organisation: str | None = None) -> str:
        payload = {"public_key_b64": public_key_b64}
        if organisation:
            payload["organisation"] = organisation
        else:
            payload["profile"] = self.require_username()

        response = self._request("post", self._scope_path("agent/enroll", organisation), json=payload)
        agent_uuid = response.get("uuid")
        if not agent_uuid:
            raise ArcsecondGatewayError("Arcsecond did not return an agent uuid.", 500)
        return agent_uuid

    def mint_agent_token(
        self, *, agent_uuid: str, organisation: str | None = None
    ) -> MintedHubToken:
        payload = {"agent_uuid": agent_uuid}
        if organisation:
            payload["organisation"] = organisation
        else:
            payload["profile"] = self.require_username()

        response = self._request("post", self._scope_path("agent/mint", organisation), json=payload)
        jwt_token = response.get("jwt")
        exp = response.get("exp")
        if not jwt_token or exp is None:
            raise ArcsecondGatewayError("Arcsecond returned an incomplete Hub token response.", 500)
        return MintedHubToken(jwt=jwt_token, exp=int(exp))

    def require_login(self) -> None:
        if not self.is_logged_in:
            raise ArcsecondGatewayError("No Arcsecond login was found. Run `focale login` first.", 401)

    def require_username(self) -> str:
        self.require_login()
        username = self.username
        if not username:
            raise ArcsecondGatewayError(
                "The stored Arcsecond session is missing its username. Login again with `focale login`.",
                400,
            )
        return username

    def require_auth_session(self) -> AuthSession:
        if self.state.auth:
            return self.state.auth
        if self.config.access_key:
            return AuthSession(
                username=self.config.username,
                access_token=self.config.access_key,
                auth_type="key",
            )
        raise ArcsecondGatewayError("No Arcsecond credentials are available. Run `focale login` first.", 401)

    def ensure_authenticated(self) -> None:
        session = self.require_auth_session()
        if session.auth_type != "token":
            return

        now = int(time.time())
        access_exp = int(session.access_exp or 0)
        if access_exp and now < access_exp - 30:
            return
        self.refresh_access_token()

    def _scope_path(self, path: str, organisation: str | None) -> str:
        if organisation:
            return f"{organisation}/{path}"
        return path

    def _auth_headers(self) -> dict[str, str]:
        session = self.require_auth_session()
        if session.auth_type == "token":
            return {"Authorization": f"Bearer {session.access_token}"}
        return {"X-Arcsecond-API-Authorization": f"Key {session.access_token}"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        authenticated: bool = True,
        retry_on_401: bool = True,
    ) -> dict:
        headers = {}
        if authenticated:
            self.ensure_authenticated()
            headers.update(self._auth_headers())

        url = f"{self.api_server}/{path.strip('/')}/"
        try:
            response = httpx.request(method.upper(), url, json=json, headers=headers, timeout=30)
        except httpx.RequestError as exc:
            raise ArcsecondGatewayError(str(exc), 400) from exc

        if response.status_code == 401 and authenticated and retry_on_401:
            session = self.require_auth_session()
            if session.auth_type == "token" and session.refresh_token:
                self.refresh_access_token()
                return self._request(
                    method,
                    path,
                    json=json,
                    authenticated=authenticated,
                    retry_on_401=False,
                )

        if not (200 <= response.status_code < 300):
            raise ArcsecondGatewayError(response.text or f"HTTP {response.status_code}", response.status_code)

        try:
            data = response.json() if response.text else {}
        except ValueError as exc:
            raise ArcsecondGatewayError(
                f"Arcsecond returned invalid JSON for {path}: {exc}",
                response.status_code,
            ) from exc

        if not isinstance(data, dict):
            raise ArcsecondGatewayError(
                f"Unexpected Arcsecond response type: {type(data)!r}.",
                response.status_code,
            )
        return data
