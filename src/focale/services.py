from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .agent_auth import AgentKeypair
from .alpaca import discover_alpaca_servers, normalize_alpaca_address
from .arcsecond_client import ArcsecondGateway
from .exceptions import ArcsecondGatewayError, FocaleError
from .hub import HubClient
from .platesolver import PlateSolverClient
from .state import AlpacaServerRecord, FocaleState, InstallationRecord

Logger = Callable[[str], None]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scope(organisation: str | None, username: str) -> tuple[str, str]:
    if organisation:
        return "organisation", organisation
    return "profile", username


def context_label(organisation: str | None) -> str:
    if organisation:
        return f"organisation `{organisation}`"
    return "personal"


def resolve_context_organisation(
    state: FocaleState,
    organisation: str | None,
) -> str | None:
    if organisation is not None:
        return organisation.strip() or None
    if state.default_organisation:
        return state.default_organisation
    return None


def resolve_hub_url(state: FocaleState, hub_url: str | None) -> str:
    resolved = hub_url or state.hub_url
    if not resolved:
        raise FocaleError(
            "No Hub URL is configured. Provide one before connecting or running diagnostics."
        )
    return resolved


def parse_scales(scales: str) -> list[int]:
    values: list[int] = []
    for part in scales.split(","):
        raw = part.strip()
        if not raw:
            continue
        try:
            values.append(int(raw))
        except ValueError as exc:
            raise FocaleError(
                f"Invalid scale value `{raw}`. Use comma-separated integers, e.g. 6 or 5,6."
            ) from exc
    if not values:
        raise FocaleError("At least one scale must be provided.")
    return values


def load_peaks_file(path: Path) -> list[list[float]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FocaleError(f"Unable to read peaks file `{path}`: {exc}") from exc

    peaks = payload.get("peaks_xy") if isinstance(payload, dict) else payload
    if not isinstance(peaks, list):
        raise FocaleError("Peaks payload must be a list or an object with `peaks_xy`.")

    parsed: list[list[float]] = []
    for row in peaks:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise FocaleError("Each peak must be a two-value [x, y] array.")
        try:
            parsed.append([float(row[0]), float(row[1])])
        except (TypeError, ValueError) as exc:
            raise FocaleError("Each peak coordinate must be numeric.") from exc
    return parsed


def _gateway(
    *,
    state: FocaleState,
    api_name: str,
    api_server: str | None,
) -> ArcsecondGateway:
    return ArcsecondGateway(
        state=state,
        api_name=api_name,
        api_server=api_server,
    )


def _ensure_installation(
    gateway: ArcsecondGateway,
    state: FocaleState,
    keypair: AgentKeypair,
    *,
    organisation: str | None,
    re_enroll: bool,
    echo: Logger,
) -> InstallationRecord:
    username = gateway.require_username()
    scope_type, scope_value = _scope(organisation, username)
    existing = state.get_installation(scope_type=scope_type, scope_value=scope_value)

    if re_enroll and existing:
        state.clear_installation(scope_type=scope_type, scope_value=scope_value)
        existing = None

    if existing and existing.public_key_b64 == keypair.public_key_b64:
        return existing

    echo("Enrolling a local Hub agent with Arcsecond.")
    agent_uuid = gateway.enroll_agent(
        public_key_b64=keypair.public_key_b64,
        organisation=organisation,
    )
    record = InstallationRecord(
        agent_uuid=agent_uuid,
        public_key_b64=keypair.public_key_b64,
        scope_type=scope_type,
        scope_value=scope_value,
    )
    state.set_installation(record)
    state.save()
    return record


def _discover_and_register_alpaca(
    gateway: ArcsecondGateway,
    state: FocaleState,
    *,
    organisation: str | None,
    echo: Logger,
) -> None:
    discovered = discover_alpaca_servers()
    if not discovered:
        echo("No local ASCOM Remote servers discovered.")
        return

    username = gateway.require_username()
    scope_type, scope_value = _scope(organisation, username)
    existing_remote = gateway.list_alpaca_servers(organisation=organisation)
    existing_by_address: dict[str, dict[str, Any]] = {}
    for server in existing_remote:
        address = server.get("address")
        if not address:
            continue
        existing_by_address[normalize_alpaca_address(str(address))] = server

    created = 0
    already = 0
    changed = False
    now = _utcnow()

    for server in discovered:
        normalized_address = normalize_alpaca_address(server.address)
        if not normalized_address:
            continue

        cached = state.get_alpaca_server(
            scope_type=scope_type,
            scope_value=scope_value,
            address=normalized_address,
        )
        remote_match = existing_by_address.get(normalized_address)

        if remote_match:
            remote_uuid = remote_match.get("uuid")
            state.set_alpaca_server(
                AlpacaServerRecord(
                    scope_type=scope_type,
                    scope_value=scope_value,
                    address=normalized_address,
                    name=str(remote_match.get("name") or server.name),
                    manufacturer=(
                        str(remote_match.get("manufacturer"))
                        if remote_match.get("manufacturer")
                        else server.manufacturer
                    ),
                    remote_uuid=str(remote_uuid) if remote_uuid else None,
                    last_seen_at=now,
                    registered_at=(
                        cached.registered_at
                        if cached and cached.registered_at
                        else now
                    ),
                )
            )
            already += 1
            changed = True
            continue

        created_server = gateway.create_alpaca_server(
            name=server.name,
            address=normalized_address,
            manufacturer=server.manufacturer,
            organisation=organisation,
        )
        existing_by_address[normalized_address] = created_server
        created_uuid = created_server.get("uuid")
        state.set_alpaca_server(
            AlpacaServerRecord(
                scope_type=scope_type,
                scope_value=scope_value,
                address=normalized_address,
                name=str(created_server.get("name") or server.name),
                manufacturer=(
                    str(created_server.get("manufacturer"))
                    if created_server.get("manufacturer")
                    else server.manufacturer
                ),
                remote_uuid=str(created_uuid) if created_uuid else None,
                last_seen_at=now,
                registered_at=now,
            )
        )
        created += 1
        changed = True
        echo(f"Registered ASCOM Remote server: {server.name} ({normalized_address})")

    if changed:
        state.save()
    echo(f"ASCOM discovery: {already} already registered, {created} new registrations.")


def login(
    *,
    api_name: str,
    api_server: str | None,
    username: str,
    secret: str,
    auth_mode: str,
) -> dict[str, Any]:
    state = FocaleState.load()
    gateway = _gateway(state=state, api_name=api_name, api_server=api_server)

    if auth_mode == "password":
        gateway.login_with_password(username=username, password=secret)
    elif auth_mode == "access-key":
        gateway.login_with_access_key(username=username, access_key=secret)
    else:
        raise FocaleError(f"Unsupported auth mode `{auth_mode}`.")

    return {
        "ok": True,
        "username": gateway.username,
        "auth_mode": auth_mode,
        "api_server": gateway.api_server,
    }


def status(
    *,
    api_name: str,
    api_server: str | None,
) -> dict[str, Any]:
    state = FocaleState.load()
    gateway = _gateway(state=state, api_name=api_name, api_server=api_server)
    return {
        "focale_version": __version__,
        "api_name": api_name,
        "api_server": gateway.api_server,
        "config_path": str(gateway.config.file_path()),
        "logged_in": gateway.is_logged_in,
        "username": gateway.username or None,
        "auth_type": gateway.auth_type,
        "has_refresh_token": gateway.has_refresh_token,
        "has_access_key": gateway.has_access_key,
        "state_path": str(state.state_file()),
        "workspace_id": state.workspace_id,
        "hub_url": state.hub_url,
        "default_context": context_label(state.default_organisation),
        "known_alpaca_servers": len(state.alpaca_servers),
        "installations": {
            key: {
                "agent_uuid": record.agent_uuid,
                "created_at": record.created_at,
            }
            for key, record in sorted(state.installations.items())
        },
    }


def list_contexts(
    *,
    api_name: str,
    api_server: str | None,
) -> dict[str, Any]:
    state = FocaleState.load()
    gateway = _gateway(state=state, api_name=api_name, api_server=api_server)
    gateway.ensure_authenticated()
    return {
        "current_default": context_label(state.default_organisation),
        "personal": gateway.require_username(),
        "organisations": [
            {
                "subdomain": item.subdomain,
                "name": item.name,
                "role": item.role,
            }
            for item in gateway.list_organisation_contexts()
        ],
    }


def set_default_context(
    *,
    api_name: str,
    api_server: str | None,
    target: str,
    force: bool = False,
) -> dict[str, Any]:
    normalized = target.strip()
    if not normalized:
        raise FocaleError("Context target cannot be empty.")

    state = FocaleState.load()
    gateway = _gateway(state=state, api_name=api_name, api_server=api_server)

    if normalized.lower() in {"personal", "profile", "me"}:
        state.default_organisation = None
        state.save()
        return {"default_context": "personal"}

    if not force:
        gateway.ensure_authenticated()
        memberships = {item.subdomain for item in gateway.list_organisation_contexts()}
        if normalized not in memberships:
            raise FocaleError(
                f"`{normalized}` is not listed in your memberships. Refresh contexts or force the change."
            )

    state.default_organisation = normalized
    state.save()
    return {"default_context": context_label(normalized)}


def connect_once(
    *,
    api_name: str,
    api_server: str | None,
    hub_url: str | None,
    organisation: str | None,
    workspace_id: str | None,
    re_enroll: bool,
    discover_alpaca: bool,
    echo: Logger,
) -> dict[str, Any]:
    state = FocaleState.load()
    gateway = _gateway(state=state, api_name=api_name, api_server=api_server)
    keypair = AgentKeypair.load_or_create(state.private_key_file())
    gateway.ensure_authenticated()
    resolved_organisation = resolve_context_organisation(state, organisation)
    resolved_hub_url = resolve_hub_url(state, hub_url)

    if hub_url and hub_url != state.hub_url:
        state.hub_url = hub_url
        state.save()

    record = _ensure_installation(
        gateway,
        state,
        keypair,
        organisation=resolved_organisation,
        re_enroll=re_enroll,
        echo=echo,
    )

    try:
        minted = gateway.mint_agent_token(
            agent_uuid=record.agent_uuid,
            organisation=resolved_organisation,
        )
    except ArcsecondGatewayError as exc:
        username = gateway.require_username()
        scope_type, scope_value = _scope(resolved_organisation, username)
        if exc.status != 403 or re_enroll:
            raise

        echo("Stored agent enrollment was rejected. Re-enrolling once.")
        state.clear_installation(scope_type=scope_type, scope_value=scope_value)
        record = _ensure_installation(
            gateway,
            state,
            keypair,
            organisation=resolved_organisation,
            re_enroll=False,
            echo=echo,
        )
        minted = gateway.mint_agent_token(
            agent_uuid=record.agent_uuid,
            organisation=resolved_organisation,
        )

    if discover_alpaca:
        try:
            _discover_and_register_alpaca(
                gateway,
                state,
                organisation=resolved_organisation,
                echo=echo,
            )
        except Exception as exc:  # pragma: no cover - best effort path
            echo(f"ASCOM discovery skipped: {exc}")

    echo(
        f"Connecting to Hub as agent {record.agent_uuid} "
        f"({context_label(resolved_organisation)}) on workspace "
        f"{workspace_id or state.workspace_id}."
    )

    welcome = asyncio.run(
        HubClient(
            hub_url=resolved_hub_url,
            workspace_id=workspace_id or state.workspace_id,
            agent_uuid=record.agent_uuid,
            jwt=minted.jwt,
            keypair=keypair,
        ).connect(once=True, echo=echo)
    )

    return {
        "ok": True,
        "context": context_label(resolved_organisation),
        "hub_url": resolved_hub_url,
        "workspace_id": workspace_id or state.workspace_id,
        "agent_uuid": record.agent_uuid,
        "session_id": welcome.session_id,
        "keepalive_s": welcome.keepalive_s,
    }


def doctor(
    *,
    api_name: str,
    api_server: str | None,
    hub_url: str | None,
    organisation: str | None,
    workspace_id: str | None,
    force_refresh: bool,
    re_enroll: bool,
    echo: Logger,
) -> dict[str, Any]:
    state: FocaleState | None = None
    gateway: ArcsecondGateway | None = None
    keypair: AgentKeypair | None = None
    record: InstallationRecord | None = None
    resolved_hub_url: str | None = None
    resolved_organisation: str | None = None
    minted = None
    had_failure = False
    results: list[dict[str, object]] = []

    def report(label: str, ok: bool, detail: str, **extra: object) -> None:
        results.append({"label": label, "ok": ok, "detail": detail, **extra})
        status = "OK" if ok else "FAIL"
        echo(f"[{status}] {label}: {detail}")

    try:
        state = FocaleState.load()
        gateway = _gateway(state=state, api_name=api_name, api_server=api_server)
        resolved_organisation = resolve_context_organisation(state, organisation)
        report("state", True, f"workspace_id={state.workspace_id}", workspace_id=state.workspace_id)
        report("context", True, context_label(resolved_organisation))
    except FocaleError as exc:
        report("state", False, str(exc))
        return {
            "ok": False,
            "api_server": api_server,
            "hub_url": hub_url,
            "steps": results,
        }

    try:
        gateway.require_login()
        report(
            "login",
            True,
            f"username={gateway.username} auth_type={gateway.auth_type}",
            username=gateway.username,
            auth_type=gateway.auth_type,
        )
    except FocaleError as exc:
        report("login", False, str(exc))
        return {
            "ok": False,
            "api_server": gateway.api_server if gateway else api_server,
            "hub_url": hub_url,
            "steps": results,
        }

    try:
        if force_refresh and gateway.auth_type == "token":
            gateway.refresh_access_token()
            report("refresh", True, "forced refresh succeeded")
        else:
            gateway.ensure_authenticated()
            detail = (
                "JWT is valid or refreshed automatically"
                if gateway.auth_type == "token"
                else "using access-key authentication"
            )
            report("refresh", True, detail)
    except FocaleError as exc:
        report("refresh", False, str(exc))
        had_failure = True

    try:
        keypair = AgentKeypair.load_or_create(state.private_key_file())
        report(
            "keypair",
            True,
            f"public_key_b64_prefix={keypair.public_key_b64[:12]}",
            public_key_b64_prefix=keypair.public_key_b64[:12],
        )
    except FocaleError as exc:
        report("keypair", False, str(exc))
        had_failure = True

    try:
        resolved_hub_url = resolve_hub_url(state, hub_url)
        if hub_url and hub_url != state.hub_url:
            state.hub_url = hub_url
            state.save()
        report("hub-url", True, resolved_hub_url, hub_url=resolved_hub_url)
    except FocaleError as exc:
        report("hub-url", False, str(exc))
        had_failure = True

    if not had_failure and gateway and state and keypair:
        try:
            record = _ensure_installation(
                gateway,
                state,
                keypair,
                organisation=resolved_organisation,
                re_enroll=re_enroll,
                echo=lambda _message: None,
            )
            report("enroll", True, f"agent_uuid={record.agent_uuid}", agent_uuid=record.agent_uuid)
        except FocaleError as exc:
            report("enroll", False, str(exc))
            had_failure = True

    if not had_failure and gateway and record:
        try:
            minted = gateway.mint_agent_token(
                agent_uuid=record.agent_uuid,
                organisation=resolved_organisation,
            )
            report("mint", True, f"exp={minted.exp}", exp=minted.exp)
        except FocaleError as exc:
            report("mint", False, str(exc))
            had_failure = True

    if not had_failure and keypair and record and resolved_hub_url and gateway and state:
        try:
            welcome = asyncio.run(
                HubClient(
                    hub_url=resolved_hub_url,
                    workspace_id=workspace_id or state.workspace_id,
                    agent_uuid=record.agent_uuid,
                    jwt=minted.jwt,
                    keypair=keypair,
                ).connect(once=True, echo=echo)
            )
            report(
                "hub",
                True,
                f"session_id={welcome.session_id} keepalive_s={welcome.keepalive_s}",
                session_id=welcome.session_id,
                keepalive_s=welcome.keepalive_s,
            )
        except FocaleError as exc:
            report("hub", False, str(exc))
            had_failure = True

    return {
        "ok": not had_failure,
        "api_server": gateway.api_server if gateway else api_server,
        "hub_url": resolved_hub_url or hub_url,
        "context": context_label(resolved_organisation),
        "steps": results,
    }


def platesolver_status(
    *,
    service_url: str | None,
    cache_dir: str | None,
    scales: str,
) -> dict[str, Any]:
    solver = PlateSolverClient(
        service_url=service_url,
        cache_dir=cache_dir,
        scales=parse_scales(scales),
    )
    try:
        health = solver.health()
    finally:
        solver.close()
    return {"mode": solver.mode, "health": health}


def platesolver_solve(
    *,
    peaks_file: Path,
    service_url: str | None,
    cache_dir: str | None,
    scales: str,
    ra_deg: float | None,
    dec_deg: float | None,
    radius_deg: float | None,
    lower_arcsec_per_pixel: float | None,
    upper_arcsec_per_pixel: float | None,
) -> dict[str, Any]:
    peaks_xy = load_peaks_file(peaks_file)
    solver = PlateSolverClient(
        service_url=service_url,
        cache_dir=cache_dir,
        scales=parse_scales(scales),
    )
    try:
        result = solver.solve(
            peaks_xy=peaks_xy,
            ra_deg=ra_deg,
            dec_deg=dec_deg,
            radius_deg=radius_deg,
            lower_arcsec_per_pixel=lower_arcsec_per_pixel,
            upper_arcsec_per_pixel=upper_arcsec_per_pixel,
        )
    finally:
        solver.close()
    return result.to_dict()
