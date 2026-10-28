"""HTTP fetching with a small TTL cache and Read the Docs rate-limit handling."""

import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass

import httpx2

LOG = logging.getLogger(__name__)


class FetchError(Exception):
    """A request failed; the message is written for the model to act on."""


class NotFound(FetchError):
    pass


class RateLimited(FetchError):
    def __init__(self, retry_after: float | None):
        self.retry_after = retry_after
        wait = f" Try again in about {int(retry_after)}s." if retry_after else ""
        super().__init__(
            "Read the Docs is rate-limiting requests from this server."
            + wait
            + " A hosted server should set STACKHPC_RTD_MCP_RTD_TOKEN."
        )


@dataclass(frozen=True)
class Response:
    url: str
    status: int
    content: bytes

    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


def _retry_after(response: httpx2.Response) -> float | None:
    header = response.headers.get("Retry-After", "")
    if header.isdigit():
        return float(header)
    # Read the Docs puts it in the body: "Expected available in 50 seconds."
    match = re.search(r"available in (\d+) second", response.text)
    return float(match.group(1)) if match else None


class Fetcher:
    """GET with caching. Serves a stale copy rather than failing when throttled."""

    def __init__(self, http: httpx2.AsyncClient, ttl: float, max_entries: int = 256):
        self._http = http
        self._ttl = ttl
        self._max_entries = max_entries
        self._cache: OrderedDict[tuple, tuple[float, Response]] = OrderedDict()

    async def get(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        ttl: float | None = None,
    ) -> Response:
        key = (url, tuple(sorted((params or {}).items())))
        cached = self._cache.get(key)
        if cached and cached[0] > time.monotonic():
            self._cache.move_to_end(key)
            return cached[1]
        try:
            response = await self._fetch(url, params, headers)
        except RateLimited:
            if cached:
                LOG.warning("rate-limited, serving stale copy of %s", url)
                return cached[1]
            raise
        self._cache[key] = (time.monotonic() + (self._ttl if ttl is None else ttl), response)
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)
        return response

    async def _fetch(self, url: str, params: dict | None, headers: dict | None) -> Response:
        try:
            response = await self._http.get(url, params=params, headers=headers)
        except httpx2.HTTPError as exc:
            raise FetchError(f"Could not fetch {url}: {exc}") from exc
        if response.status_code == 429:
            raise RateLimited(_retry_after(response))
        if response.status_code == 404:
            raise NotFound(f"Not found: {url}")
        if response.status_code >= 400:
            raise FetchError(f"HTTP {response.status_code} from {url}")
        return Response(str(response.url), response.status_code, response.content)
