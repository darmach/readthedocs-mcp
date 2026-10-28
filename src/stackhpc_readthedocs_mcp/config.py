"""Runtime settings, read from the environment."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

ENV_PREFIX = "STACKHPC_RTD_MCP_"

# Read the Docs projects served when STACKHPC_RTD_MCP_PROJECTS is unset. The
# first one is the default for tools called without a project.
DEFAULT_PROJECTS = ("stackhpc-kayobe-config", "azimuth-config")

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "stackhpc-readthedocs-mcp"


def parse_projects(value: str) -> tuple[str, ...]:
    """Parse a comma-separated list of Read the Docs project slugs."""
    projects = tuple(p.strip() for p in value.split(",") if p.strip())
    if not projects:
        raise ValueError("at least one Read the Docs project slug is required")
    for slug in projects:
        if not _SLUG.match(slug):
            raise ValueError(f"invalid Read the Docs project slug: {slug!r}")
    return projects


@dataclass(frozen=True)
class Settings:
    projects: tuple[str, ...] = DEFAULT_PROJECTS
    # Optional Read the Docs API token. Anonymous access works but is
    # throttled per IP, which matters for a shared, hosted server.
    rtd_token: str | None = None
    cache_dir: Path = field(default_factory=_default_cache_dir)
    # Seconds before project metadata, pages and repository checkouts are
    # refreshed.
    cache_ttl: float = 3600.0
    rtd_api_url: str = "https://app.readthedocs.org/api/v3"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> "Settings":
        def get(name: str) -> str | None:
            return environ.get(ENV_PREFIX + name) or None

        kwargs: dict = {}
        if projects := get("PROJECTS"):
            kwargs["projects"] = parse_projects(projects)
        if token := get("RTD_TOKEN"):
            kwargs["rtd_token"] = token
        if cache_dir := get("CACHE_DIR"):
            kwargs["cache_dir"] = Path(cache_dir).expanduser()
        if ttl := get("CACHE_TTL"):
            kwargs["cache_ttl"] = float(ttl)
        if api_url := get("RTD_API_URL"):
            kwargs["rtd_api_url"] = api_url
        return cls(**kwargs)
