"""Turn Sphinx and MkDocs pages into Markdown, and list the pages of a docs release."""

import json
import re
import zlib
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag
from markdownify import MarkdownConverter

# Where the page body lives, most specific first: Sphinx (Immaterial, RTD
# theme, Furo), MkDocs Material, then generic fallbacks.
MAIN_SELECTORS = (
    "article[role=main]",
    "div[itemprop=articleBody]",
    "article",
    "[role=main]",
    "main",
    "div.body",
    "body",
)
NOISE_SELECTORS = (
    "script",
    "style",
    "nav",
    "form",
    "button",
    "footer",
    "[role=navigation]",
    "[role=search]",
    ".headerlink",
    ".md-sidebar",
    ".md-source-file",
    ".md-content__button",
    ".sphinxsidebar",
    ".rst-footer-buttons",
)
HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"]  # a list: bs4 name filters take lists


@dataclass(frozen=True)
class Document:
    title: str
    markdown: str


@dataclass(frozen=True)
class PageEntry:
    path: str  # relative to the release's docs URL
    title: str


def resolve_page_url(docs_url: str, path: str) -> str:
    """Resolve a page given as a full URL, a site path or a path relative to the release."""
    path = path.strip()
    base = urlsplit(docs_url)
    if path.startswith(("http://", "https://")):
        if urlsplit(path).netloc != base.netloc:
            raise ValueError(f"{path} is not on {base.netloc}.")
        return path
    if path.startswith("/"):
        return f"{base.scheme}://{base.netloc}{path}"
    return urljoin(docs_url, path)


def url_candidates(url: str) -> list[str]:
    """The URL itself, plus the .html and directory forms for extension-less paths."""
    page, fragment = urldefrag(url)
    candidates = [page]
    last = page.rsplit("/", 1)[-1]
    if last and "." not in last:
        candidates += [page + ".html", page + "/"]
    return [c + (f"#{fragment}" if fragment else "") for c in candidates]


def _is_heading(tag: Tag) -> bool:
    return isinstance(tag, Tag) and tag.name in HEADINGS


def _is_section(tag: Tag) -> bool:
    return tag.name == "section" or "section" in (tag.get("class") or [])


def _section(main: Tag, fragment: str) -> Tag:
    target = main.find(id=fragment)
    if target is None:
        ids = [h["id"] for h in main.find_all(HEADINGS, id=True)]
        ids += [s["id"] for s in main.find_all(_is_section) if s.get("id")]
        hint = f" Sections on this page: {', '.join(dict.fromkeys(ids))}." if ids else ""
        raise ValueError(f"No section #{fragment} on this page.{hint}")
    if _is_section(target):
        return target
    heading = target if _is_heading(target) else target.find_parent(HEADINGS)
    if heading is None and target.name == "span":
        # Sphinx puts label anchors in an empty span just before the heading.
        heading = target.find_next(HEADINGS)
    if heading is None:
        return target
    parent = heading.parent
    if parent is not None and _is_section(parent) and parent.find(HEADINGS) is heading:
        return parent
    # MkDocs has flat markup: take siblings up to the next heading of the
    # same or higher level.
    level = int(heading.name[1])
    parts = [str(heading)]
    for sibling in heading.next_siblings:
        if _is_heading(sibling) and int(sibling.name[1]) <= level:
            break
        parts.append(str(sibling))
    return BeautifulSoup("".join(parts), "html.parser")


def _code_language(pre: Tag) -> str | None:
    for tag in (pre, *list(pre.parents)[:3]):
        for cls in tag.get("class") or []:
            if cls.startswith("highlight-") and cls != "highlight-default":
                return cls.removeprefix("highlight-")
            if cls.startswith("language-"):
                return cls.removeprefix("language-")
    return None


def html_to_document(html: str, url: str) -> Document:
    """Extract a page's main content as Markdown, or one section of it if url has a #fragment."""
    page_url, fragment = urldefrag(url)
    soup = BeautifulSoup(html, "html.parser")
    main = next((m for s in MAIN_SELECTORS if (m := soup.select_one(s))), soup)
    for selector in NOISE_SELECTORS:
        for tag in main.select(selector):
            tag.decompose()
    for link in main.find_all("a", href=True):
        link["href"] = urljoin(page_url, link["href"])
    heading = main.find("h1")
    title = heading.get_text(" ", strip=True) if heading else ""
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    content = _section(main, fragment) if fragment else main
    if fragment:
        section_heading = content.find(HEADINGS)
        if section_heading:
            title = section_heading.get_text(" ", strip=True)
    markdown = MarkdownConverter(
        heading_style="ATX",
        bullets="-",
        code_language_callback=_code_language,
        # Escaping turns group_vars into group\_vars, which models copy into paths.
        escape_underscores=False,
        escape_asterisks=False,
    ).convert_soup(content)
    markdown = re.sub(r"[ \t]+\n", "\n", markdown)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
    return Document(title=title, markdown=markdown)


def paginate(text: str, offset: int, max_chars: int) -> tuple[str, int | None]:
    """Return text[offset:offset+max_chars], cut at a line break, and the next offset if any."""
    if offset >= len(text):
        return "", None
    end = offset + max_chars
    if end >= len(text):
        return text[offset:], None
    newline = text.rfind("\n", offset, end)
    if newline > offset + max_chars // 2:
        end = newline + 1
    return text[offset:end], end


def parse_sphinx_inventory(data: bytes) -> list[PageEntry]:
    """Pages (std:doc entries) from a Sphinx objects.inv."""
    if not data.startswith(b"# Sphinx inventory version 2"):
        raise ValueError("not a Sphinx v2 inventory")
    _, _, _, _, body = data.split(b"\n", 4)
    pages = []
    for line in zlib.decompress(body).decode("utf-8").splitlines():
        match = re.match(r"(.+?)\s+(\S+?):(\S+)\s+(-?\d+)\s+(\S*)\s+(.*)", line)
        if not match:
            continue
        name, domain, role, _, uri, display = match.groups()
        if (domain, role) != ("std", "doc"):
            continue
        if uri.endswith("$"):
            uri = uri[:-1] + name
        pages.append(PageEntry(path=uri, title=name if display == "-" else display))
    return sorted(pages, key=lambda p: p.path)


def parse_mkdocs_index(data: bytes) -> list[PageEntry]:
    """Pages from an MkDocs search/search_index.json, in navigation order."""
    docs = json.loads(data)["docs"]
    return [
        PageEntry(path=d["location"] or "./", title=d.get("title") or d["location"])
        for d in docs
        if "#" not in d["location"]
    ]
