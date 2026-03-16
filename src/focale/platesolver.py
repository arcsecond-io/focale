from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import astrometry
import httpx

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
        self._local_solver: astrometry.Solver | None = None

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
        cache = (
            Path(self.cache_dir)
            if self.cache_dir
            else Path.home() / ".cache" / "focale" / "astrometry"
        )
        cache.mkdir(parents=True, exist_ok=True)

        try:
            index_files = _index_files(cache, set(self.scales))
            self._local_solver = astrometry.Solver(index_files)
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
            size_hint=_size_hint(payload),
            position_hint=_position_hint(payload),
            solution_parameters=astrometry.SolutionParameters(),
        )
        if not result.has_match():
            return PlateSolveResult(status="no_match")
        match = result.best_match()
        return PlateSolveResult(
            status="match",
            center_ra_deg=match.center_ra_deg,
            center_dec_deg=match.center_dec_deg,
            scale_arcsec_per_pixel=match.scale_arcsec_per_pixel,
            wcs_header={key: value for key, (value, _comment) in match.wcs_fields.items()},
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


def _index_files(cache_dir: Path, scales: set[int]) -> list[Path]:
    invalid_scales = sorted(scale for scale in scales if scale < 0 or scale > 19)
    if invalid_scales:
        joined = ", ".join(str(scale) for scale in invalid_scales)
        raise FocaleError(
            f"Invalid astrometry scales: {joined}. Expected integers between 0 and 19."
        )

    index_files: list[Path] = []
    light_scales = {scale for scale in scales if scale <= 6}
    wide_scales = {scale for scale in scales if scale >= 7}

    if light_scales:
        index_files.extend(
            astrometry.series_5200.index_files(
                cache_directory=cache_dir,
                scales=light_scales,
            )
        )
    if wide_scales:
        index_files.extend(
            astrometry.series_4100.index_files(
                cache_directory=cache_dir,
                scales=wide_scales,
            )
        )
    return index_files


def _size_hint(payload: dict[str, Any]) -> astrometry.SizeHint | None:
    lower = payload["lower_arcsec_per_pixel"]
    upper = payload["upper_arcsec_per_pixel"]
    if lower is None and upper is None:
        return None
    return astrometry.SizeHint(
        lower_arcsec_per_pixel=(
            astrometry.DEFAULT_LOWER_ARCSEC_PER_PIXEL if lower is None else lower
        ),
        upper_arcsec_per_pixel=(
            astrometry.DEFAULT_UPPER_ARCSEC_PER_PIXEL if upper is None else upper
        ),
    )


def _position_hint(payload: dict[str, Any]) -> astrometry.PositionHint | None:
    ra_deg = payload["ra_deg"]
    dec_deg = payload["dec_deg"]
    radius_deg = payload["radius_deg"]
    if ra_deg is None and dec_deg is None and radius_deg is None:
        return None
    if ra_deg is None or dec_deg is None:
        raise FocaleError("Local plate solver position hint requires both RA and Dec.")
    return astrometry.PositionHint(
        ra_deg=ra_deg,
        dec_deg=dec_deg,
        radius_deg=180.0 if radius_deg is None else radius_deg,
    )
