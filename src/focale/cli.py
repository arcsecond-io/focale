from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import click

from . import __version__
from .agent_auth import AgentKeypair
from .arcsecond_client import ArcsecondGateway
from .exceptions import ArcsecondGatewayError, FocaleError
from .hub import HubClient
from .state import FocaleState, InstallationRecord


@dataclass
class RuntimeOptions:
    api_name: str
    api_server: str | None


pass_options = click.make_pass_decorator(RuntimeOptions, ensure=True)


def _scope(organisation: str | None, username: str) -> tuple[str, str]:
    if organisation:
        return "organisation", organisation
    return "profile", username


def _resolve_hub_url(state: FocaleState, hub_url: str | None) -> str:
    resolved = hub_url or state.hub_url
    if not resolved:
        raise click.ClickException(
            "No Hub URL is configured. Pass `--hub-url` the first time you connect."
        )
    return resolved


def _result_line(label: str, ok: bool, detail: str) -> None:
    status = "OK" if ok else "FAIL"
    click.echo(f"[{status}] {label}: {detail}")


def _ensure_installation(
    gateway: ArcsecondGateway,
    state: FocaleState,
    keypair: AgentKeypair,
    *,
    organisation: str | None,
    re_enroll: bool,
    echo,
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


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--api-name",
    default="cloud",
    show_default=True,
    help="Arcsecond API profile name from the shared arcsecond config.",
)
@click.option(
    "--api-server",
    help="Override the Arcsecond API base URL, for example https://api.arcsecond.dev.",
)
@click.version_option(version=__version__)
@click.pass_context
def main(ctx: click.Context, api_name: str, api_server: str | None) -> None:
    ctx.obj = RuntimeOptions(api_name=api_name, api_server=api_server)


@main.command(help="Login to Arcsecond for Focale, preferably with password/JWT.")
@click.option("--username", prompt=True, help="Arcsecond username (without @).")
@click.option(
    "--auth-mode",
    type=click.Choice(["password", "access-key"], case_sensitive=False),
    default="password",
    show_default=True,
    help="Prefer password/JWT auth. Access Key remains available as a fallback.",
)
@pass_options
def login(options: RuntimeOptions, username: str, auth_mode: str) -> None:
    try:
        state = FocaleState.load()
        gateway = ArcsecondGateway(
            state=state,
            api_name=options.api_name,
            api_server=options.api_server,
        )
        if auth_mode == "password":
            password = click.prompt("Arcsecond password", hide_input=True)
            gateway.login_with_password(username=username, password=password)
        else:
            access_key = click.prompt("Arcsecond Access Key", hide_input=True)
            gateway.login_with_access_key(username=username, access_key=access_key)
    except ArcsecondGatewayError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(
        f"Arcsecond {auth_mode} login saved for {gateway.username}. "
        f"API: {gateway.api_server}"
    )


@main.command(help="Show the current Focale and Arcsecond session status.")
@pass_options
def status(options: RuntimeOptions) -> None:
    try:
        state = FocaleState.load()
        gateway = ArcsecondGateway(
            state=state,
            api_name=options.api_name,
            api_server=options.api_server,
        )
    except FocaleError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Focale version: {__version__}")
    click.echo(f"Arcsecond api profile: {options.api_name}")
    click.echo(f"Arcsecond api server: {gateway.api_server}")
    click.echo(f"Arcsecond config: {gateway.config.file_path()}")
    click.echo(f"Logged in: {'yes' if gateway.is_logged_in else 'no'}")
    click.echo(f"Username: {gateway.username or '(none)'}")
    click.echo(f"Auth type: {gateway.auth_type or '(none)'}")
    click.echo(f"Refresh token available: {'yes' if gateway.has_refresh_token else 'no'}")
    click.echo(f"Access key fallback available: {'yes' if gateway.has_access_key else 'no'}")
    click.echo(f"Focale state: {state.state_file()}")
    click.echo(f"Workspace id: {state.workspace_id}")
    click.echo(f"Default hub url: {state.hub_url or '(none)'}")

    if state.installations:
        click.echo("Agent installations:")
        for key, record in sorted(state.installations.items()):
            click.echo(
                f"  - {key}: agent_uuid={record.agent_uuid} created_at={record.created_at}"
            )
    else:
        click.echo("Agent installations: none")


@main.command(help="Connect to the Arcsecond Hub using the secured challenge flow.")
@click.option("--hub-url", help="Hub websocket URL, for example wss://hub.arcsecond.io/ws/agent.")
@click.option("--organisation", help="Organisation subdomain for an organisation-scoped agent.")
@click.option(
    "--workspace-id",
    help="Optional override for the local workspace identifier. Defaults to the saved local id.",
)
@click.option(
    "--once",
    is_flag=True,
    help="Validate the login, enrollment, JWT minting, and Hub handshake, then exit.",
)
@click.option(
    "--re-enroll",
    is_flag=True,
    help="Force a fresh Arcsecond agent enrollment before connecting.",
)
@pass_options
def connect(
    options: RuntimeOptions,
    hub_url: str | None,
    organisation: str | None,
    workspace_id: str | None,
    once: bool,
    re_enroll: bool,
) -> None:
    try:
        state = FocaleState.load()
        gateway = ArcsecondGateway(
            state=state,
            api_name=options.api_name,
            api_server=options.api_server,
        )
        keypair = AgentKeypair.load_or_create(state.private_key_file())
        gateway.ensure_authenticated()
        resolved_hub_url = _resolve_hub_url(state, hub_url)
        if hub_url and hub_url != state.hub_url:
            state.hub_url = hub_url
            state.save()

        record = _ensure_installation(
            gateway,
            state,
            keypair,
            organisation=organisation,
            re_enroll=re_enroll,
            echo=click.echo,
        )

        try:
            minted = gateway.mint_agent_token(
                agent_uuid=record.agent_uuid,
                organisation=organisation,
            )
        except ArcsecondGatewayError as exc:
            username = gateway.require_username()
            scope_type, scope_value = _scope(organisation, username)
            if exc.status != 403 or re_enroll:
                raise

            click.echo("Stored agent enrollment was rejected. Re-enrolling once.")
            state.clear_installation(scope_type=scope_type, scope_value=scope_value)
            record = _ensure_installation(
                gateway,
                state,
                keypair,
                organisation=organisation,
                re_enroll=False,
                echo=click.echo,
            )
            minted = gateway.mint_agent_token(
                agent_uuid=record.agent_uuid,
                organisation=organisation,
            )

        click.echo(
            f"Connecting to Hub as agent {record.agent_uuid} on workspace "
            f"{workspace_id or state.workspace_id}."
        )
        welcome = asyncio.run(
            HubClient(
                hub_url=resolved_hub_url,
                workspace_id=workspace_id or state.workspace_id,
                agent_uuid=record.agent_uuid,
                jwt=minted.jwt,
                keypair=keypair,
            ).connect(once=once, echo=click.echo)
        )
    except FocaleError as exc:
        raise click.ClickException(str(exc)) from exc

    if once:
        click.echo(
            f"Hub handshake succeeded. session_id={welcome.session_id} keepalive_s={welcome.keepalive_s}"
        )


@main.command(help="Run a step-by-step diagnostic for Arcsecond auth and Hub connectivity.")
@click.option("--hub-url", help="Hub websocket URL, for example wss://hub.arcsecond.io/ws/agent.")
@click.option("--organisation", help="Organisation subdomain for an organisation-scoped agent.")
@click.option(
    "--workspace-id",
    help="Optional override for the local workspace identifier. Defaults to the saved local id.",
)
@click.option(
    "--force-refresh",
    is_flag=True,
    help="Force a JWT refresh before the rest of the checks.",
)
@click.option(
    "--re-enroll",
    is_flag=True,
    help="Force a new agent enrollment before minting the Hub JWT.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Emit machine-readable JSON instead of human-readable lines.",
)
@pass_options
def doctor(
    options: RuntimeOptions,
    hub_url: str | None,
    organisation: str | None,
    workspace_id: str | None,
    force_refresh: bool,
    re_enroll: bool,
    json_output: bool,
) -> None:
    state: FocaleState | None = None
    gateway: ArcsecondGateway | None = None
    keypair: AgentKeypair | None = None
    record: InstallationRecord | None = None
    resolved_hub_url: str | None = None
    minted = None
    had_failure = False
    results: list[dict[str, object]] = []

    def report(label: str, ok: bool, detail: str, **extra: object) -> None:
        item = {"label": label, "ok": ok, "detail": detail, **extra}
        results.append(item)
        if not json_output:
            _result_line(label, ok, detail)

    try:
        state = FocaleState.load()
        gateway = ArcsecondGateway(
            state=state,
            api_name=options.api_name,
            api_server=options.api_server,
        )
        report("state", True, f"workspace_id={state.workspace_id}", workspace_id=state.workspace_id)
    except FocaleError as exc:
        report("state", False, str(exc))
        if json_output:
            click.echo(
                json.dumps(
                    {
                        "ok": False,
                        "api_server": options.api_server,
                        "hub_url": hub_url,
                        "steps": results,
                    },
                    indent=2,
                )
            )
            raise SystemExit(1) from exc
        raise click.ClickException("Doctor failed at state initialization.") from exc

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
        if json_output:
            click.echo(
                json.dumps(
                    {
                        "ok": False,
                        "api_server": gateway.api_server if gateway else options.api_server,
                        "hub_url": hub_url,
                        "steps": results,
                    },
                    indent=2,
                )
            )
            raise SystemExit(1) from exc
        raise click.ClickException("Doctor failed because no usable Arcsecond login is available.") from exc

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
        resolved_hub_url = _resolve_hub_url(state, hub_url)
        if hub_url and hub_url != state.hub_url:
            state.hub_url = hub_url
            state.save()
        report("hub-url", True, resolved_hub_url, hub_url=resolved_hub_url)
    except click.ClickException as exc:
        report("hub-url", False, str(exc))
        had_failure = True

    if not had_failure and gateway and state and keypair:
        try:
            record = _ensure_installation(
                gateway,
                state,
                keypair,
                organisation=organisation,
                re_enroll=re_enroll,
                echo=lambda message: None,
            )
            report("enroll", True, f"agent_uuid={record.agent_uuid}", agent_uuid=record.agent_uuid)
        except FocaleError as exc:
            report("enroll", False, str(exc))
            had_failure = True

    if not had_failure and gateway and record:
        try:
            minted = gateway.mint_agent_token(
                agent_uuid=record.agent_uuid,
                organisation=organisation,
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
                ).connect(once=True, echo=click.echo)
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

    if json_output:
        click.echo(
            json.dumps(
                {
                    "ok": not had_failure,
                    "api_server": gateway.api_server if gateway else options.api_server,
                    "hub_url": resolved_hub_url or hub_url,
                    "steps": results,
                },
                indent=2,
            )
        )
        raise SystemExit(1 if had_failure else 0)

    if had_failure:
        raise click.ClickException("Doctor found at least one failing step.")
