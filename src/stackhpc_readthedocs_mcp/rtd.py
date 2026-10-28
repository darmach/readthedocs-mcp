"""Read the Docs API v3: project metadata, releases and search."""

import json
import re
from dataclasses import dataclass

from stackhpc_readthedocs_mcp.fetch import Fetcher, FetchError

SEARCH_PAGE_SIZE = 10
SEARCH_TTL = 300.0


@dataclass(frozen=True)
class Version:
    slug: str  # e.g. "stackhpc-2025.1"
    name: str  # e.g. "stackhpc/2025.1"
    ref: str  # git branch, tag or commit the docs were built from
    docs_url: str  # ends with "/"


@dataclass(frozen=True)
class Project:
    slug: str
    name: str
    repo_url: str
    default_version: str
    docs_url: str
    versions: tuple[Version, ...]

    def resolve(self, release: str | None) -> Version:
        """Find a version by slug, git ref or a suffix such as "2025.1"."""
        if not release:
            for version in self.versions:
                if version.slug == self.default_version:
                    return version
            if self.versions:
                return self.versions[0]
            raise FetchError(f"{self.slug} has no public, built releases on Read the Docs.")
        wanted = release.strip().lower()
        exact = [v for v in self.versions if wanted in (v.slug.lower(), v.name.lower(), v.ref.lower())]
        if len(exact) == 1:
            return exact[0]
        suffix = re.compile(rf"(^|[-/_.]){re.escape(wanted)}$")
        partial = [v for v in self.versions if suffix.search(v.slug.lower()) or suffix.search(v.name.lower())]
        if len(partial) == 1:
            return partial[0]
        available = ", ".join(v.slug for v in self.versions)
        if len(partial) > 1:
            matches = ", ".join(v.slug for v in partial)
            raise FetchError(f"Release {release!r} is ambiguous in {self.slug}: {matches}.")
        raise FetchError(f"Unknown release {release!r} for {self.slug}. Available: {available}.")


def _natural_key(slug: str) -> list:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", slug)]


def _version(data: dict) -> Version | None:
    if not (data.get("active") and data.get("built")) or data.get("hidden"):
        return None
    if data.get("type") == "external" or data.get("privacy_level", "public") != "public":
        return None
    return Version(
        slug=data["slug"],
        name=data.get("verbose_name") or data["slug"],
        ref=data.get("identifier") or data["slug"],
        docs_url=data["urls"]["documentation"].rstrip("/") + "/",
    )


class ReadTheDocs:
    def __init__(self, fetcher: Fetcher, api_url: str, token: str | None = None):
        self._fetcher = fetcher
        self._api = api_url.rstrip("/")
        self._headers = {"Authorization": f"Token {token}"} if token else None

    async def _json(self, url: str, params: dict | None = None, ttl: float | None = None) -> dict:
        response = await self._fetcher.get(url, params=params, headers=self._headers, ttl=ttl)
        try:
            return json.loads(response.content)
        except ValueError as exc:
            raise FetchError(f"Read the Docs returned invalid JSON from {url}") from exc

    async def project(self, slug: str) -> Project:
        data = await self._json(f"{self._api}/projects/{slug}/")
        versions: list[Version] = []
        url: str | None = f"{self._api}/projects/{slug}/versions/"
        params: dict | None = {"active": "true", "limit": 100}
        while url:
            page = await self._json(url, params)
            versions += [v for v in map(_version, page.get("results", [])) if v]
            url, params = page.get("next"), None
        versions.sort(key=lambda v: _natural_key(v.slug), reverse=True)
        return Project(
            slug=data["slug"],
            name=data.get("name") or data["slug"],
            repo_url=(data.get("repository") or {}).get("url", ""),
            default_version=data.get("default_version") or "latest",
            docs_url=data["urls"]["documentation"],
            versions=tuple(versions),
        )

    async def search(self, project: str, version: str, query: str, page: int = 1) -> dict:
        params = {
            "q": f"project:{project}/{version} {query}",
            "page": page,
            "page_size": SEARCH_PAGE_SIZE,
        }
        return await self._json(f"{self._api}/search/", params, ttl=SEARCH_TTL)
