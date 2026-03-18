from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from .exceptions import FocaleError


DISCOVERY_PORT = 32227
DISCOVERY_PAYLOAD = b"alpacadiscovery1"


@dataclass(frozen=True)
class DiscoveredAlpacaServer:
    name: str
    address: str
    manufacturer: str | None = None


@dataclass(frozen=True)
class ConfiguredAlpacaDevice:
    type: str
    number: int
    name: str
    unique_id: str


@dataclass(frozen=True)
class AlpacaSiteCoordinates:
    longitude: float
    latitude: float
    height: float | None = None


def normalize_alpaca_address(address: str) -> str:
    raw = address.strip().rstrip("/")
    if not raw:
        return raw

    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    scheme = parsed.scheme.lower() if parsed.scheme else "http"
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        return raw.lower()

    if not host:
        return raw.lower()
    if port is None:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def discover_alpaca_servers(
    *,
    timeout_s: float = 1.0,
    retries: int = 2,
    description_timeout_s: float = 2.0,
) -> list[DiscoveredAlpacaServer]:
    candidates: dict[str, tuple[str, int]] = {}
    per_round_timeout = max(0.2, timeout_s / max(1, retries))

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(per_round_timeout)
            sock.bind(("", 0))

            for _ in range(max(1, retries)):
                try:
                    sock.sendto(DISCOVERY_PAYLOAD, ("255.255.255.255", DISCOVERY_PORT))
                except OSError:
                    continue

                while True:
                    try:
                        data, sender = sock.recvfrom(4096)
                    except socket.timeout:
                        break
                    except OSError:
                        break

                    payload = _parse_discovery_payload(data)
                    if not payload:
                        continue
                    port = payload.get("alpaca_port")
                    if not isinstance(port, int) or port <= 0:
                        continue
                    candidates[f"{sender[0]}:{port}"] = (sender[0], port)
    except OSError:
        return []

    discovered: dict[str, DiscoveredAlpacaServer] = {}
    for host, port in candidates.values():
        info = _fetch_management_description(
            host=host,
            port=port,
            timeout_s=description_timeout_s,
        )
        address = normalize_alpaca_address(f"http://{host}:{port}")
        name = str(info.get("server_name") or f"ASCOM Remote {host}:{port}")
        manufacturer = info.get("manufacturer")
        discovered[address] = DiscoveredAlpacaServer(
            name=name,
            address=address,
            manufacturer=manufacturer,
        )

    return sorted(discovered.values(), key=lambda server: server.address)


def _parse_discovery_payload(data: bytes) -> dict[str, int] | None:
    try:
        decoded = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None

    raw_port = (
        decoded.get("AlpacaPort")
        or decoded.get("alpacaPort")
        or decoded.get("alpaca_port")
    )
    try:
        port = int(raw_port)
    except (TypeError, ValueError):
        return None
    return {"alpaca_port": port}


def _fetch_management_description(*, host: str, port: int, timeout_s: float) -> dict[str, Any]:
    url = f"http://{host}:{port}/management/v1/description"
    try:
        response = httpx.get(url, timeout=timeout_s)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return {}

    if not isinstance(payload, dict):
        return {}
    return {
        "server_name": payload.get("ServerName") or payload.get("server_name"),
        "manufacturer": payload.get("Manufacturer") or payload.get("manufacturer"),
    }


def get_configured_devices(
    address: str,
    *,
    timeout_s: float = 3.0,
) -> list[ConfiguredAlpacaDevice]:
    normalized_address = normalize_alpaca_address(address)
    url = f"{normalized_address}/management/v1/configureddevices"
    try:
        response = httpx.get(url, timeout=timeout_s)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise FocaleError(f"Unable to inspect Alpaca devices at {normalized_address}: {exc}") from exc

    if isinstance(payload, dict):
        error_number = payload.get("ErrorNumber")
        if error_number not in (None, 0):
            error_message = payload.get("ErrorMessage") or f"error {error_number}"
            raise FocaleError(
                f"Alpaca configured devices query failed at {normalized_address}: {error_message}"
            )
        value = payload.get("Value")
        if isinstance(value, list):
            payload = value

    if not isinstance(payload, list):
        raise FocaleError(
            f"Unexpected configured devices response from {normalized_address}."
        )

    devices: list[ConfiguredAlpacaDevice] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            number = int(item.get("DeviceNumber"))
        except (TypeError, ValueError):
            continue

        unique_id = str(item.get("UniqueID") or "").strip()
        device_type = str(item.get("DeviceType") or "").strip()
        name = str(item.get("DeviceName") or "").strip()
        if not unique_id or not device_type or not name:
            continue

        devices.append(
            ConfiguredAlpacaDevice(
                type=device_type,
                number=number,
                name=name,
                unique_id=unique_id,
            )
        )

    return devices


def get_telescope_site_coordinates(
    address: str,
    *,
    device_number: int,
    timeout_s: float = 3.0,
) -> AlpacaSiteCoordinates | None:
    try:
        latitude = _get_device_value(
            address=address,
            device_type="telescope",
            device_number=device_number,
            attribute="sitelatitude",
            timeout_s=timeout_s,
        )
        longitude = _get_device_value(
            address=address,
            device_type="telescope",
            device_number=device_number,
            attribute="sitelongitude",
            timeout_s=timeout_s,
        )
    except FocaleError:
        return None

    height: float | None = None
    try:
        elevation = _get_device_value(
            address=address,
            device_type="telescope",
            device_number=device_number,
            attribute="siteelevation",
            timeout_s=timeout_s,
        )
    except FocaleError:
        elevation = None

    try:
        latitude_value = float(latitude)
        longitude_value = float(longitude)
    except (TypeError, ValueError):
        return None

    if elevation is not None:
        try:
            height = float(elevation)
        except (TypeError, ValueError):
            height = None

    return AlpacaSiteCoordinates(
        longitude=longitude_value,
        latitude=latitude_value,
        height=height,
    )


def _get_device_value(
    *,
    address: str,
    device_type: str,
    device_number: int,
    attribute: str,
    timeout_s: float,
) -> Any:
    normalized_address = normalize_alpaca_address(address)
    url = f"{normalized_address}/api/v1/{device_type}/{device_number}/{attribute}"
    try:
        response = httpx.get(url, timeout=timeout_s)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise FocaleError(
            f"Unable to query Alpaca {device_type}#{device_number} {attribute} at {normalized_address}: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise FocaleError(
            f"Unexpected Alpaca response for {device_type}#{device_number} {attribute}."
        )

    error_number = payload.get("ErrorNumber")
    if error_number not in (None, 0):
        error_message = payload.get("ErrorMessage") or f"error {error_number}"
        raise FocaleError(
            f"Alpaca {device_type}#{device_number} {attribute} failed: {error_message}"
        )

    return payload.get("Value")
