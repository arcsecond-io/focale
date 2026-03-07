from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
from arcsecond_service_platesolver.solver import AstrometryServiceSolver

from .exceptions import FocaleError


@dataclass(frozen=True)
class PlateSolveResult:
    status: str
    center_ra_deg: float | None = None
    center_dec_deg: float | None = None
    scale_arcsec_per_pixel: float | None = None
    wcs_header: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PlateSolverClient:
    def __init__(
        self,
        *,
        service_url: str | None = None,
        cache_dir: str | None = None,
        scales: list[int] | None = None,
    ) -> None:
        self.service_url = (service_url or "").rstrip("/") or None
        self.cache_dir = cache_dir
        self.scales = scales or [6]
        self._local_solver: AstrometryServiceSolver | None = None

        if not self.service_url:
            self._init_local_solver()

    @property
    def mode(self) -> str:
        return "remote" if self.service_url else "local"

    @property
    def is_ready(self) -> bool:
        if self.mode == "remote":
            return True
        return self._local_solver is not None

    def close(self) -> None:
        if self._local_solver is not None:
            self._local_solver.close()
            self._local_solver = None

    def health(self) -> dict[str, Any]:
        if self.service_url:
            try:
                response = httpx.get(f"{self.service_url}/health", timeout=10)
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise FocaleError(f"Remote plate solver health failed: {exc}") from exc

            if not isinstance(payload, dict):
                raise FocaleError("Remote plate solver health returned a non-object payload.")
            return payload

        if not self.is_ready:
            raise FocaleError("Local plate solver failed to initialize.")
        return {"ok": True, "mode": "local"}

    def solve(
        self,
        *,
        peaks_xy: list[list[float]],
        ra_deg: float | None = None,
        dec_deg: float | None = None,
        radius_deg: float | None = None,
        lower_arcsec_per_pixel: float | None = None,
        upper_arcsec_per_pixel: float | None = None,
    ) -> PlateSolveResult:
        payload = {
            "peaks_xy": peaks_xy,
            "scales": self.scales,
            "ra_deg": ra_deg,
            "dec_deg": dec_deg,
            "radius_deg": radius_deg,
            "lower_arcsec_per_pixel": lower_arcsec_per_pixel,
            "upper_arcsec_per_pixel": upper_arcsec_per_pixel,
        }

        if self.service_url:
            return self._solve_remote(payload)
        return self._solve_local(payload)

    def _init_local_solver(self) -> None:
        cache = self.cache_dir or str(
            Path.home() / ".cache" / "focale" / "astrometry"
        )
        Path(cache).mkdir(parents=True, exist_ok=True)

        try:
            self._local_solver = AstrometryServiceSolver(
                cache_dir=cache,
                scales=set(self.scales),
            )
        except Exception as exc:  # pragma: no cover - third-party runtime failures
            raise FocaleError(f"Local plate solver failed to initialize: {exc}") from exc

    def _solve_remote(self, payload: dict[str, Any]) -> PlateSolveResult:
        try:
            response = httpx.post(
                f"{self.service_url}/platesolve",
                json=payload,
                timeout=120,
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise FocaleError(f"Remote plate solver request failed: {exc}") from exc

        if not isinstance(data, dict):
            raise FocaleError("Remote plate solver returned a non-object payload.")
        return _to_result(data)

    def _solve_local(self, payload: dict[str, Any]) -> PlateSolveResult:
        if not self._local_solver:
            raise FocaleError("Local plate solver is unavailable.")

        result = self._local_solver.solve(
            payload["peaks_xy"],
            ra_deg=payload["ra_deg"],
            dec_deg=payload["dec_deg"],
            radius_deg=payload["radius_deg"],
            lower_arcsec_per_pixel=payload["lower_arcsec_per_pixel"],
            upper_arcsec_per_pixel=payload["upper_arcsec_per_pixel"],
        )
        if not result.has_match:
            return PlateSolveResult(status="no_match")
        return PlateSolveResult(
            status="match",
            center_ra_deg=result.center_ra_deg,
            center_dec_deg=result.center_dec_deg,
            scale_arcsec_per_pixel=result.scale_arcsec_per_pixel,
            wcs_header=result.wcs_header,
        )


def _to_result(data: dict[str, Any]) -> PlateSolveResult:
    status = str(data.get("status") or "no_match")
    if status != "match":
        return PlateSolveResult(status="no_match")
    return PlateSolveResult(
        status="match",
        center_ra_deg=data.get("center_ra_deg"),
        center_dec_deg=data.get("center_dec_deg"),
        scale_arcsec_per_pixel=data.get("scale_arcsec_per_pixel"),
        wcs_header=data.get("wcs_header"),
    )
