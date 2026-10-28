"""Shallow, cached checkouts of the git commit each docs release was built from."""

import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import anyio

LOG = logging.getLogger(__name__)

GIT_TIMEOUT = 300.0
GREP_TIMEOUT = 30.0
MAX_LINE_CHARS = 300
MAX_FILE_BYTES = 2_000_000
SYNC_MARKER = "stackhpc-readthedocs-mcp-synced"


class RepoError(Exception):
    """A git operation or path lookup failed; the message is written for the model."""


@dataclass(frozen=True)
class Checkout:
    path: Path
    repo_url: str
    ref: str
    commit: str

    def web_url(self, rel_path: str, directory: bool = False) -> str | None:
        """Link to a path at this commit, for repositories hosted on GitHub."""
        match = re.match(r"https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$", self.repo_url)
        if not match:
            return None
        kind = "tree" if directory else "blob"
        return f"https://github.com/{match.group(1)}/{kind}/{self.commit}/{rel_path}".rstrip("/")


async def _git(*args: str | Path, timeout: float = GIT_TIMEOUT, ok_codes=(0,)) -> str:
    cmd = ["git", *map(str, args)]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        with anyio.fail_after(timeout):
            result = await anyio.run_process(cmd, check=False, env=env)
    except TimeoutError as exc:
        raise RepoError(f"git timed out after {timeout:.0f}s") from exc
    except FileNotFoundError as exc:
        raise RepoError("git is not installed on the MCP server host") from exc
    if result.returncode not in ok_codes:
        stderr = result.stderr.decode(errors="replace").strip()
        raise RepoError(f"git failed ({result.returncode}): {stderr[-500:]}")
    return result.stdout.decode(errors="replace")


class Repos:
    def __init__(self, cache_dir: Path, ttl: float):
        self._root = cache_dir / "repos"
        self._ttl = ttl
        self._locks: dict[Path, anyio.Lock] = {}

    async def checkout(self, project: str, release: str, repo_url: str, ref: str) -> Checkout:
        """Return an up-to-date checkout of ref, cloning or fetching only when stale."""
        if not repo_url:
            raise RepoError(f"Read the Docs has no repository URL for {project}.")
        if ref.startswith("-"):
            raise RepoError(f"Refusing suspicious git ref {ref!r}.")
        dest = self._root / project / release
        lock = self._locks.setdefault(dest, anyio.Lock())
        async with lock:
            if (dest / ".git").is_dir():
                marker = dest / ".git" / SYNC_MARKER
                if not marker.exists() or time.time() - marker.stat().st_mtime > self._ttl:
                    try:
                        await self._sync(dest, repo_url, ref)
                    except RepoError as exc:
                        LOG.warning("using stale checkout of %s %s: %s", project, release, exc)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = Path(tempfile.mkdtemp(prefix=".clone-", dir=dest.parent))
                try:
                    await _git("init", "-q", tmp)
                    await self._sync(tmp, repo_url, ref)
                    try:
                        tmp.rename(dest)
                    except OSError:
                        # Another server process sharing the cache got there first.
                        if not (dest / ".git").is_dir():
                            raise
                        shutil.rmtree(tmp, ignore_errors=True)
                except BaseException:
                    shutil.rmtree(tmp, ignore_errors=True)
                    raise
            commit = (await _git("-C", dest, "rev-parse", "HEAD")).strip()
        return Checkout(path=dest, repo_url=repo_url, ref=ref, commit=commit)

    async def _sync(self, path: Path, repo_url: str, ref: str) -> None:
        remotes = (await _git("-C", path, "remote")).split()
        await _git("-C", path, "remote", "set-url" if "origin" in remotes else "add", "origin", repo_url)
        await _git("-C", path, "fetch", "-q", "--depth", "1", "--no-tags", "origin", ref)
        await _git("-C", path, "-c", "advice.detachedHead=false", "checkout", "-q", "--force", "--detach", "FETCH_HEAD")
        (path / ".git" / SYNC_MARKER).touch()

    async def grep(
        self,
        checkout: Checkout,
        pattern: str,
        path: str | None = None,
        ignore_case: bool = False,
        fixed_string: bool = False,
    ) -> list[str]:
        """Matching lines as "path:line:text", via git grep (tracked text files only)."""
        args = ["-C", checkout.path, "grep", "-n", "-I", "--no-color", "--full-name"]
        args.append("-F" if fixed_string else "-E")
        if ignore_case:
            args.append("-i")
        args += ["-e", pattern]
        if path:
            args += ["--", path.lstrip("/")]
        output = await _git(*args, timeout=GREP_TIMEOUT, ok_codes=(0, 1))
        return [line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + " …" for line in output.splitlines()]

    def resolve(self, checkout: Checkout, rel_path: str) -> Path:
        """Map a repository path to the checkout, refusing anything that escapes it."""
        root = checkout.path.resolve()
        target = (root / rel_path.strip().lstrip("/")).resolve()
        if not target.is_relative_to(root):
            raise RepoError(f"{rel_path!r} is outside the repository.")
        parts = target.relative_to(root).parts
        if parts and parts[0] == ".git":
            raise RepoError("The .git directory is not available.")
        if not target.exists():
            raise RepoError(
                f"{rel_path!r} does not exist at {checkout.ref}. Use grep_repo, or read_repo on a parent directory."
            )
        return target

    def list_dir(self, checkout: Checkout, rel_path: str) -> list[str]:
        target = self.resolve(checkout, rel_path)
        if not target.is_dir():
            raise RepoError(f"{rel_path!r} is a file, not a directory.")
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        return [p.name + ("/" if p.is_dir() else "") for p in entries if p.name != ".git"]

    def read_text(self, checkout: Checkout, rel_path: str) -> str:
        target = self.resolve(checkout, rel_path)
        if target.is_dir():
            raise RepoError(f"{rel_path!r} is a directory.")
        if target.stat().st_size > MAX_FILE_BYTES:
            raise RepoError(f"{rel_path!r} is larger than {MAX_FILE_BYTES} bytes; use grep_repo.")
        data = target.read_bytes()
        if b"\0" in data[:8192]:
            raise RepoError(f"{rel_path!r} is a binary file.")
        return data.decode("utf-8", errors="replace")
