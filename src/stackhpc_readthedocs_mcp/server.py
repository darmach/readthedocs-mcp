"""The MCP server: tools over StackHPC docs releases and the repositories behind them."""

import argparse
import html
import logging
import math
import re
import sys
from dataclasses import replace
from typing import Annotated

import httpx2
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from stackhpc_readthedocs_mcp import __version__
from stackhpc_readthedocs_mcp.config import Settings, parse_projects
from stackhpc_readthedocs_mcp.fetch import Fetcher, FetchError, NotFound
from stackhpc_readthedocs_mcp.pages import (
    html_to_document,
    paginate,
    parse_mkdocs_index,
    parse_sphinx_inventory,
    resolve_page_url,
    url_candidates,
)
from stackhpc_readthedocs_mcp.repo import Checkout, RepoError, Repos
from stackhpc_readthedocs_mcp.rtd import SEARCH_PAGE_SIZE, Project, ReadTheDocs, Version

INSTRUCTIONS = """\
Search and read StackHPC documentation published on Read the Docs, and the git \
repository each docs release is built from.

Every project has several releases (for stackhpc-kayobe-config: stackhpc-2025.1, \
stackhpc-2026.1, ...), and docs and configuration differ between them. Pass the \
release that matches the user's deployment. If you don't know it, ask, or say which \
release your answer is based on.

Typical flow: search_docs to find pages, then read_doc to read one (paths from search \
results work as they are, including #section anchors). Use list_doc_pages to browse. \
For the configuration itself (defaults, variables, environments), use grep_repo and \
read_repo on the same release. Cite the URLs these tools return.
"""

DEFAULT_MAX_CHARS = 40_000
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)

ProjectArg = Annotated[
    str | None,
    Field(
        description="Read the Docs project slug, e.g. 'stackhpc-kayobe-config'. "
        "Defaults to the first configured project; see list_projects."
    ),
]
ReleaseArg = Annotated[
    str | None,
    Field(
        description="Docs release, e.g. 'stackhpc-2025.1' or just '2025.1'. "
        "Defaults to the project's default release; see list_projects."
    ),
]

LOG = logging.getLogger(__name__)


def _strip_tags(text: str) -> str:
    """Search titles and highlights are HTML fragments; make them plain text."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _snippet(block: dict, limit: int = 300) -> str:
    highlights = (block.get("highlights") or {}).get("content") or []
    text = _strip_tags(highlights[0]) if highlights else block.get("content", "")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " …"


def _format_search(data: dict, query: str, project: Project, version: Version, page: int) -> str:
    count = data.get("count", 0)
    if not count:
        return f'No results for "{query}" in {project.slug} {version.slug}. Try other terms, or another release.'
    pages = max(1, math.ceil(count / SEARCH_PAGE_SIZE))
    lines = [f'{count} results for "{query}" in {project.slug} {version.slug} (page {page} of {pages}):']
    for n, result in enumerate(data.get("results", []), start=(page - 1) * SEARCH_PAGE_SIZE + 1):
        path = result.get("path", "")
        lines.append(f"\n{n}. {_strip_tags(result.get('title', ''))} — {path}")
        for block in (result.get("blocks") or [])[:3]:
            name = block.get("title") or block.get("name") or ""
            anchor = f"{path}#{block['id']}" if block.get("id") else path
            lines.append(f"   - {anchor} ({name}): {_snippet(block)}")
    if data.get("next"):
        lines.append(f"\nMore results: call search_docs again with page={page + 1}.")
    return "\n".join(lines)


def _format_project(project: Project) -> str:
    lines = [
        f"## {project.slug}",
        f"Docs: {project.docs_url}",
        f"Repository: {project.repo_url or 'unknown'}",
        f"Default release: {project.default_version}",
        "Releases (release → git ref):",
    ]
    lines += [f"- {v.slug} → {v.ref}" for v in project.versions]
    return "\n".join(lines)


def _checkout_header(project: Project, version: Version, checkout: Checkout) -> str:
    return f"{project.slug} {version.slug} @ {checkout.ref} (commit {checkout.commit[:12]})"


def build_server(settings: Settings, http: httpx2.AsyncClient | None = None) -> MCPServer:
    if http is None:
        http = httpx2.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": f"stackhpc-readthedocs-mcp/{__version__}"},
        )
    fetcher = Fetcher(http, ttl=settings.cache_ttl)
    rtd = ReadTheDocs(fetcher, settings.rtd_api_url, settings.rtd_token)
    repos = Repos(settings.cache_dir, settings.cache_ttl)

    server = MCPServer(
        "stackhpc-readthedocs-mcp",
        title="StackHPC docs",
        instructions=INSTRUCTIONS,
        version=__version__,
    )

    async def get_project(slug: str | None) -> Project:
        slug = slug or settings.projects[0]
        if slug not in settings.projects:
            raise ToolError(f"Project {slug!r} is not served here. Available: {', '.join(settings.projects)}.")
        try:
            return await rtd.project(slug)
        except FetchError as exc:
            raise ToolError(str(exc)) from exc

    async def get_release(slug: str | None, release: str | None) -> tuple[Project, Version]:
        project = await get_project(slug)
        try:
            return project, project.resolve(release)
        except FetchError as exc:
            raise ToolError(str(exc)) from exc

    async def get_checkout(project: Project, version: Version) -> Checkout:
        try:
            return await repos.checkout(project.slug, version.slug, project.repo_url, version.ref)
        except RepoError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def list_projects() -> str:
        """List the documentation projects served here.

        Shows each project's releases and the git ref each release is built from.
        """
        sections = []
        for slug in settings.projects:
            try:
                sections.append(_format_project(await rtd.project(slug)))
            except FetchError as exc:
                sections.append(f"## {slug}\nUnavailable: {exc}")
        return "\n\n".join(sections)

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def search_docs(
        query: Annotated[str, Field(description="Search terms. Supports Read the Docs search syntax.")],
        project: ProjectArg = None,
        release: ReleaseArg = None,
        page: Annotated[int, Field(ge=1, description="Results page, 10 results per page.")] = 1,
    ) -> str:
        """Full-text search of one docs release.

        Returns pages and matching sections with snippets. Pass a result's path to read_doc.
        """
        proj, version = await get_release(project, release)
        try:
            data = await rtd.search(proj.slug, version.slug, query, page)
        except FetchError as exc:
            raise ToolError(str(exc)) from exc
        return _format_search(data, query, proj, version, page)

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def read_doc(
        path: Annotated[
            str,
            Field(
                description="Page to read: a path from search_docs or list_doc_pages "
                "(e.g. 'configuration/cephadm.html' or '/en/stackhpc-2025.1/configuration/cephadm.html'), "
                "or a full docs URL. Add '#section-id' to read one section."
            ),
        ],
        project: ProjectArg = None,
        release: ReleaseArg = None,
        offset: Annotated[int, Field(ge=0, description="Character offset, to continue a long page.")] = 0,
        max_chars: Annotated[
            int, Field(ge=1000, le=100_000, description="Maximum characters to return.")
        ] = DEFAULT_MAX_CHARS,
    ) -> str:
        """Read a documentation page, or one section of it, as Markdown."""
        proj, version = await get_release(project, release)
        try:
            url = resolve_page_url(version.docs_url, path)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        response = None
        for candidate in url_candidates(url):
            try:
                response = await fetcher.get(candidate.split("#", 1)[0])
                url = candidate
                break
            except NotFound:
                continue
            except FetchError as exc:
                raise ToolError(str(exc)) from exc
        if response is None:
            raise ToolError(f"No page at {url}. Use search_docs or list_doc_pages to find the right path.")
        fragment = url.split("#", 1)[1] if "#" in url else ""
        try:
            doc = html_to_document(response.text(), response.url + (f"#{fragment}" if fragment else ""))
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        chunk, next_offset = paginate(doc.markdown, offset, max_chars)
        header = [f"Source: {url} (release {version.slug})"]
        if offset and doc.title:
            header.insert(0, f"Page: {doc.title} (continued)")
        if offset or next_offset:
            end = next_offset or len(doc.markdown)
            header.append(f"[Characters {offset}–{end} of {len(doc.markdown)}.]")
        footer = f"\n\n[Continues: call read_doc again with offset={next_offset}.]" if next_offset else ""
        return "\n".join(h for h in header if h) + "\n\n" + chunk + footer

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def list_doc_pages(
        project: ProjectArg = None,
        release: ReleaseArg = None,
        contains: Annotated[
            str | None, Field(description="Only pages whose path or title contains this text (case-insensitive).")
        ] = None,
    ) -> str:
        """List the pages of a docs release with their titles, from the site's Sphinx or MkDocs index."""
        proj, version = await get_release(project, release)
        pages = None
        for index, parse in (("objects.inv", parse_sphinx_inventory), ("search/search_index.json", parse_mkdocs_index)):
            try:
                response = await fetcher.get(version.docs_url + index)
                pages = parse(response.content)
                break
            except (NotFound, ValueError, KeyError):
                continue
            except FetchError as exc:
                raise ToolError(str(exc)) from exc
        if pages is None:
            raise ToolError(f"{proj.slug} {version.slug} publishes no page index; use search_docs instead.")
        if contains:
            needle = contains.lower()
            pages = [p for p in pages if needle in p.path.lower() or needle in p.title.lower()]
        lines = [f"{len(pages)} pages in {proj.slug} {version.slug} ({version.docs_url}):"]
        lines += [f"- {p.path} — {p.title}" for p in pages]
        return "\n".join(lines)

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def grep_repo(
        pattern: Annotated[
            str, Field(description="Extended regular expression (or a literal string with fixed_string).")
        ],
        project: ProjectArg = None,
        release: ReleaseArg = None,
        path: Annotated[
            str | None, Field(description="Limit to a directory or glob, e.g. 'etc/kayobe/' or '*.yml'.")
        ] = None,
        ignore_case: bool = False,
        fixed_string: bool = False,
        max_results: Annotated[int, Field(ge=1, le=1000)] = 100,
    ) -> str:
        """Search the git repository behind a docs release with git grep.

        Use it for configuration such as kayobe-config's etc/kayobe. Returns path:line:text.
        """
        proj, version = await get_release(project, release)
        checkout = await get_checkout(proj, version)
        try:
            lines = await repos.grep(checkout, pattern, path, ignore_case, fixed_string)
        except RepoError as exc:
            raise ToolError(str(exc)) from exc
        header = _checkout_header(proj, version, checkout)
        if not lines:
            return f"{header}: no matches for {pattern!r}" + (f" in {path}" if path else "") + "."
        shown = lines[:max_results]
        result = [
            f"{header}: {len(lines)} matching lines"
            + (f", showing {len(shown)}" if len(shown) < len(lines) else "")
            + ":"
        ]
        result += shown
        if len(shown) < len(lines):
            result.append("[Narrow the pattern or path, or raise max_results, to see more.]")
        return "\n".join(result)

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def read_repo(
        path: Annotated[
            str,
            Field(
                description="File or directory in the repository, e.g. 'etc/kayobe/cephadm.yml'. "
                "Empty for the top level."
            ),
        ] = "",
        project: ProjectArg = None,
        release: ReleaseArg = None,
        offset: Annotated[int, Field(ge=1, description="First line to return (1-based).")] = 1,
        limit: Annotated[int, Field(ge=1, le=5000, description="Maximum lines to return.")] = 400,
    ) -> str:
        """Read a file, or list a directory, from the git repository behind a docs release."""
        proj, version = await get_release(project, release)
        checkout = await get_checkout(proj, version)
        rel = path.strip().strip("/")
        header = f"{_checkout_header(proj, version, checkout)} — /{rel}"
        try:
            if repos.resolve(checkout, rel).is_dir():
                link = checkout.web_url(rel, directory=True)
                return "\n".join([header, *([link] if link else []), *repos.list_dir(checkout, rel)])
            text = repos.read_text(checkout, rel)
        except RepoError as exc:
            raise ToolError(str(exc)) from exc
        link = checkout.web_url(rel)
        lines = text.splitlines()
        if not lines:
            return "\n".join([header, *([link] if link else []), "(empty file)"])
        if offset > len(lines):
            raise ToolError(f"offset {offset} is past the end of {rel} ({len(lines)} lines).")
        window = lines[offset - 1 : offset - 1 + limit]
        end = offset - 1 + len(window)
        out = [header, *([link] if link else []), f"Lines {offset}–{end} of {len(lines)}:"]
        out += [f"{n:>6}  {line}" for n, line in enumerate(window, start=offset)]
        if end < len(lines):
            out.append(f"[Continues: call read_repo again with offset={end + 1}.]")
        return "\n".join(out)

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="stackhpc-readthedocs-mcp",
        description="MCP server for StackHPC documentation on Read the Docs and the repositories behind it.",
    )
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="streamable-http bind address")
    parser.add_argument("--port", type=int, default=8000, help="streamable-http port")
    parser.add_argument(
        "--projects",
        help="comma-separated Read the Docs project slugs (default: $STACKHPC_RTD_MCP_PROJECTS or the built-in list)",
    )
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    # stdout carries the protocol in stdio mode, so logs go to stderr.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)  # one INFO line per request otherwise
    settings = Settings.from_env()
    if args.projects:
        settings = replace(settings, projects=parse_projects(args.projects))
    server = build_server(settings)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        # Stateless: tools keep no per-session state, so any replica can answer.
        server.run("streamable-http", host=args.host, port=args.port, stateless_http=True)
