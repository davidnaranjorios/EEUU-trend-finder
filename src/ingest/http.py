"""Cliente HTTP con rate limit, reintentos con backoff y copia raw de cada respuesta."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import random
import re
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class ApiError(RuntimeError):
    """La API respondió con un error no recuperable o se agotaron los reintentos."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RateLimiter:
    """Espaciado mínimo de 60/rpm segundos entre solicitudes (sin ráfagas)."""

    def __init__(
        self,
        requests_per_minute: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval = 60.0 / requests_per_minute
        self._clock = clock
        self._sleep = sleep
        self._next_at: float | None = None

    def wait(self) -> None:
        now = self._clock()
        if self._next_at is not None and now < self._next_at:
            self._sleep(self._next_at - now)
            now = self._clock()
        self._next_at = now + self.interval


class ApiClient:
    """GET de JSON con rate limit, reintentos y copia raw sin credenciales.

    Las credenciales (`auth_params`) nunca se escriben en raw ni en logs: los errores de
    transporte se registran por tipo, no por mensaje, porque el mensaje incluye la URL.
    """

    def __init__(
        self,
        base_url: str,
        auth_params: dict[str, str],
        *,
        requests_per_minute: float,
        max_retries: int,
        timeout_s: float,
        backoff_base_s: float,
        raw_dir: Path | None = None,
        headers: dict[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        today: Callable[[], date] = date.today,
        rng: random.Random | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        # httpx registra la URL completa (con la API key) a nivel INFO: se silencia.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        self._http = httpx.Client(
            base_url=base_url, timeout=timeout_s, headers=headers, transport=transport
        )
        self._auth = auth_params
        self._limiter = RateLimiter(requests_per_minute, clock, sleep)
        self._sleep = sleep
        self._today = today
        self._rng = rng or random.Random()
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.raw_dir = raw_dir
        self.log = logger or logging.getLogger(__name__)
        self.n_requests = 0
        self.n_retries = 0

    def close(self) -> None:
        self._http.close()

    def get_json(
        self, endpoint: str, params: dict[str, Any], *, use_cache: bool = False
    ) -> dict[str, Any]:
        """Devuelve el JSON de `endpoint`. Con `use_cache`, reutiliza la copia raw de hoy."""
        raw_path = self._raw_path(endpoint, params)
        if use_cache and raw_path is not None and raw_path.exists():
            with gzip.open(raw_path, "rt", encoding="utf-8") as fh:
                return json.load(fh)["response"]

        for attempt in range(self.max_retries + 1):
            self._limiter.wait()
            self.n_requests += 1
            retry_after: float | None = None
            try:
                resp = self._http.get(endpoint, params={**params, **self._auth})
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                reason = type(exc).__name__
            else:
                if resp.status_code == 200:
                    data = resp.json()
                    self._save_raw(raw_path, endpoint, params, data)
                    return data
                if resp.status_code not in RETRYABLE_STATUS:
                    raise ApiError(
                        f"{endpoint}: HTTP {resp.status_code}: {_error_message(resp)}",
                        resp.status_code,
                    )
                reason = f"HTTP {resp.status_code}"
                retry_after = _retry_after(resp)

            if attempt == self.max_retries:
                raise ApiError(
                    f"{endpoint}: reintentos agotados ({self.max_retries}); último: {reason}"
                )
            delay = self.backoff_base_s * 2**attempt + self._rng.uniform(0, self.backoff_base_s)
            if retry_after is not None:
                delay = max(delay, retry_after)
            self.n_retries += 1
            self.log.warning(
                "reintento",
                extra={"fields": {"endpoint": endpoint, "reason": reason, "delay_s": delay}},
            )
            self._sleep(delay)
        raise AssertionError("inalcanzable")

    def _raw_path(self, endpoint: str, params: dict[str, Any]) -> Path | None:
        if self.raw_dir is None:
            return None
        canonical = json.dumps({"endpoint": endpoint, "params": params}, sort_keys=True)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        safe_endpoint = re.sub(r"[^A-Za-z0-9_-]", "_", endpoint)
        return self.raw_dir / safe_endpoint / self._today().isoformat() / f"{digest}.json.gz"

    def _save_raw(
        self, path: Path | None, endpoint: str, params: dict[str, Any], data: dict[str, Any]
    ) -> None:
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        payload = {
            "endpoint": endpoint,
            "params": params,
            "fetched_at": datetime.now(UTC).isoformat(),
            "response": data,
        }
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(payload, fh)
        tmp.replace(path)


def _error_message(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("error_message", ""))[:300]
    except ValueError:
        return resp.text[:300]


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None
