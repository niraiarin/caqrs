"""HTTP fetch + on-disk cache for Polymarket archive parquet hours.

Each file is large (100-400 MB); the client caches downloads to a
local directory so repeat queries don't re-fetch. Writes are atomic
(``.tmp`` + rename) so a crash mid-download does not leave a
half-written file claiming to be a complete hour.

The archive itself updates hourly. The ``base_url`` defaults to the
documented public r2 endpoint; pass an explicit ``base_url`` (e.g.
for a local mirror) to override.
"""

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Self

import httpx

from caqrs.data.polymarket.clob_client import PolymarketError

_DEFAULT_BASE_URL = "https://r2v2.pmxt.dev"
_DEFAULT_TIMEOUT_S = 120.0
_HTTP_OK = 200
_HOURS_PER_DAY = 24


class PolymarketArchiveClient:
    """Async client that fetches hourly parquet snapshots and caches them.

    Parameters
    ----------
    cache_dir:
        Local directory under which downloaded parquet files are
        kept. Created on first use if missing.
    base_url:
        Archive base URL. Defaults to the documented public endpoint.
    http_client:
        Optional pre-built ``httpx.AsyncClient``; the archive client
        will not close it on context exit. When omitted, the archive
        client owns its own client.
    timeout_s:
        Per-request timeout. Default is 120 s because individual
        files can be hundreds of MB.
    """

    def __init__(
        self,
        *,
        cache_dir: Path,
        base_url: str = _DEFAULT_BASE_URL,
        http_client: httpx.AsyncClient | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._cache_dir = cache_dir
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._owns_http_client = http_client is None
        self._http_client = http_client

    async def __aenter__(self) -> Self:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=self._timeout_s)
            self._owns_http_client = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    # === Public methods ===

    def cached_path(self, hour: datetime) -> Path:
        """Local path the parquet for ``hour`` would be cached under.

        ``hour`` must be tz-aware UTC; minutes / seconds are truncated.
        """
        if hour.tzinfo is None:
            msg = "hour must be timezone-aware (use datetime(..., tzinfo=UTC))"
            raise ValueError(msg)
        truncated = hour.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        return self._cache_dir / _filename_for(truncated)

    async def fetch_hour(self, hour: datetime) -> Path:
        """Return the local parquet path for ``hour``, downloading if missing.

        Atomic: on download failure the partial file is removed and a
        :class:`PolymarketError` is raised; the cache directory does
        not contain an empty / truncated parquet on the next call.
        """
        target = self.cached_path(hour)
        if target.exists():
            return target

        truncated = hour.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        url = f"{self._base_url}/{_filename_for(truncated)}"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")

        try:
            await self._download_to(url=url, dest=tmp)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(target)
        return target

    async def fetch_range(
        self,
        *,
        start: datetime,
        end: datetime,
    ) -> tuple[Path, ...]:
        """Download every hour in ``[start, end)`` and return the cached paths.

        Both bounds must be tz-aware. ``end`` is exclusive so that
        ``[8:00, 9:00)`` returns just the 08:00 file.
        """
        if start.tzinfo is None or end.tzinfo is None:
            msg = "start and end must be timezone-aware"
            raise ValueError(msg)
        if end <= start:
            msg = f"end must be strictly after start; got start={start} end={end}"
            raise ValueError(msg)

        start_h = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        end_h = end.astimezone(UTC).replace(minute=0, second=0, microsecond=0)

        results: list[Path] = []
        cursor = start_h
        while cursor < end_h:
            results.append(await self.fetch_hour(cursor))
            cursor += timedelta(hours=1)
        return tuple(results)

    # === Internals ===

    async def _download_to(self, *, url: str, dest: Path) -> None:
        client = self._http_client
        if client is None:
            async with httpx.AsyncClient(timeout=self._timeout_s) as one_shot:
                await self._stream_to_file(client=one_shot, url=url, dest=dest)
            return
        await self._stream_to_file(client=client, url=url, dest=dest)

    async def _stream_to_file(
        self,
        *,
        client: httpx.AsyncClient,
        url: str,
        dest: Path,
    ) -> None:
        try:
            response = await client.get(url)
        except httpx.RequestError as exc:
            msg = f"Polymarket archive request failed: {type(exc).__name__}: {exc}"
            raise PolymarketError(message=msg) from exc

        if response.status_code != _HTTP_OK:
            msg = (
                f"Polymarket archive returned {response.status_code} for {url}: "
                f"{response.text[:200]}"
            )
            raise PolymarketError(message=msg, status_code=response.status_code)
        dest.write_bytes(response.content)


def _filename_for(hour: datetime) -> str:
    return f"polymarket_orderbook_{hour.strftime('%Y-%m-%dT%H')}.parquet"


def _all_hours_in_range(*, start: datetime, end: datetime) -> Iterable[datetime]:
    """Yield each hour boundary in ``[start, end)``. Exposed for tests."""
    cursor = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    end_h = end.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    while cursor < end_h:
        yield cursor
        cursor += timedelta(hours=1)
    _ = _HOURS_PER_DAY  # reserved for future safety guard on huge ranges
