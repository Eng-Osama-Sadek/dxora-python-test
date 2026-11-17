"""Fetch a web page safely and turn its HTML into compact, LLM-ready text."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from app.utils.validators import InvalidURLError, assert_public_host, validate_url

MAX_REDIRECTS = 5
USER_AGENT = "Mozilla/5.0 (compatible; LinkAnalysisBot/1.0; +https://github.com)"
REMOVE_TAGS = ["script", "style", "noscript", "svg", "iframe", "canvas", "template", "form", "button", "select"]
CHROME_TAGS = ["nav", "footer", "aside"]
TEXT_TAGS = ["h1", "h2", "h3", "h4", "p", "li", "blockquote", "figcaption", "dt", "dd", "summary", "td"]
NESTED_BLOCKS = ["p", "li", "h1", "h2", "h3", "h4", "blockquote", "td", "dd"]
ORG_TYPES = {"organization", "localbusiness", "corporation", "website", "professionalservice", "store"}


class WebsiteFetchError(Exception):
    """The website could not be fetched or contains nothing we can analyze."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class ExtractedSite:
    url: str
    final_url: str
    title: str = ""
    meta_description: str = ""
    site_name: str = ""
    language: str = ""
    headings: list[str] = field(default_factory=list)
    nav_links: list[str] = field(default_factory=list)
    text: str = ""
    truncated: bool = False

    def to_prompt_block(self) -> str:
        parts = [f"URL: {self.final_url}"]
        if self.language:
            parts.append(f"Declared language: {self.language}")
        if self.site_name:
            parts.append(f"Site name (metadata): {self.site_name}")
        parts.append(f"Page title: {self.title or '(none)'}")
        parts.append(f"Meta description: {self.meta_description or '(none)'}")
        if self.nav_links:
            parts.append("Navigation labels: " + " | ".join(self.nav_links))
        parts.append("Page text (headings marked with #):\n" + (self.text or "(empty)"))
        return "\n".join(parts)


# ------------------------------------------------------------------ fetching
async def fetch_html(
    url: str,
    *,
    timeout: float = 15.0,
    max_bytes: int = 2_000_000,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, str]:
    """Download a page. Redirects are followed manually so every hop is SSRF-checked.

    Returns (html, final_url). Raises WebsiteFetchError / UnsafeURLError / InvalidURLError.
    """
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                "Accept-Language": "en-US,en;q=0.9,ar;q=0.5",
            },
        )
    try:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            async with client.stream("GET", current) as response:
                if response.has_redirect_location:
                    target = urljoin(current, response.headers["location"])
                    current = await validate_url(target)  # re-check every hop
                    continue
                return await _read_response(response, current, max_bytes), current
        raise WebsiteFetchError("The website redirected too many times.", 502)
    except httpx.TimeoutException as exc:
        raise WebsiteFetchError("The website took too long to respond.", 504) from exc
    except httpx.HTTPError as exc:
        raise WebsiteFetchError("Could not connect to the website.", 502) from exc
    finally:
        if owns_client:
            await client.aclose()


async def _read_response(response: httpx.Response, url: str, max_bytes: int) -> str:
    code = response.status_code
    if code in (401, 403, 406, 429):
        raise WebsiteFetchError(
            f"The website blocked automated access (HTTP {code}). Try another URL.", 422
        )
    if code == 404 or code == 410:
        raise WebsiteFetchError(f"Page not found (HTTP {code}).", 422)
    if code >= 500:
        raise WebsiteFetchError(f"The website returned a server error (HTTP {code}).", 502)
    if code >= 400:
        raise WebsiteFetchError(f"The website returned HTTP {code}.", 422)

    content_type = response.headers.get("content-type", "").lower()
    if content_type and not any(token in content_type for token in ("html", "xml", "text/plain")):
        raise WebsiteFetchError(
            f"The URL does not return an HTML page (content-type: {content_type.split(';')[0]}).", 422
        )

    buffer = bytearray()
    async for chunk in response.aiter_bytes():
        buffer.extend(chunk)
        if len(buffer) >= max_bytes:
            break
    encoding = response.encoding or "utf-8"
    try:
        return bytes(buffer[:max_bytes]).decode(encoding, errors="replace")
    except LookupError:
        return bytes(buffer[:max_bytes]).decode("utf-8", errors="replace")


# ------------------------------------------------------------------ parsing
def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _meta(soup: BeautifulSoup, *, name: str | None = None, prop: str | None = None) -> str:
    attrs = {"name": name} if name else {"property": prop}
    tag = soup.find("meta", attrs=attrs)
    return _clean(tag.get("content", "")) if tag else ""


def _json_ld_organization(soup: BeautifulSoup) -> tuple[str, str]:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (ValueError, TypeError):
            continue
        nodes: list = []
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                nodes.append(node)
                graph = node.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
        for node in nodes:
            types = node.get("@type", [])
            types = [types] if isinstance(types, str) else types
            if any(str(t).lower() in ORG_TYPES for t in types) and node.get("name"):
                return _clean(str(node["name"])), _clean(str(node.get("description", "")))
    return "", ""


def extract_site(html: str, *, url: str, final_url: str | None = None, max_chars: int = 12_000) -> ExtractedSite:
    """Parse HTML into an ExtractedSite. Raises WebsiteFetchError when nothing useful is found."""
    soup = BeautifulSoup(html, "html.parser")

    title = _clean(soup.title.get_text()) if soup.title else ""
    meta_description = _meta(soup, name="description") or _meta(soup, prop="og:description")
    site_name = _meta(soup, prop="og:site_name") or _meta(soup, name="application-name")
    ld_name, ld_description = _json_ld_organization(soup)
    site_name = site_name or ld_name
    meta_description = meta_description or ld_description
    language = _clean(soup.html.get("lang", "")) if soup.html else ""

    nav_links: list[str] = []
    for nav in soup.find_all("nav"):
        for anchor in nav.find_all("a"):
            label = _clean(anchor.get_text())
            if 2 <= len(label) <= 40 and label not in nav_links:
                nav_links.append(label)
    nav_links = nav_links[:40]

    for tag in soup(REMOVE_TAGS):
        tag.decompose()
    for tag in soup(CHROME_TAGS):
        tag.decompose()

    lines: list[str] = []
    headings: list[str] = []
    seen: set[str] = set()
    total = 0
    truncated = False
    for element in soup.find_all(TEXT_TAGS):
        name = element.name
        if name in {"li", "blockquote", "dd", "td", "summary"} and element.find(NESTED_BLOCKS):
            continue
        text = _clean(element.get_text(" "))
        is_heading = name in {"h1", "h2", "h3", "h4"}
        if len(text) < (2 if is_heading else 12 if name == "li" else 25):
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        if is_heading:
            prefix = "# " if name in {"h1", "h2"} else "## "
            if name in {"h1", "h2"} and len(headings) < 25:
                headings.append(text)
        else:
            prefix = "- " if name == "li" else ""
        line = prefix + text
        if total + len(line) > max_chars:
            truncated = True
            break
        lines.append(line)
        total += len(line) + 1

    if total < 200:  # div-only layouts: fall back to the raw visible text
        for raw_line in soup.get_text("\n").splitlines():
            text = _clean(raw_line)
            if len(text) >= 25 and text.lower() not in seen:
                seen.add(text.lower())
                if total + len(text) > max_chars:
                    truncated = True
                    break
                lines.append(text)
                total += len(text) + 1

    text = "\n".join(lines)
    if len(text) < 80 and not meta_description:
        raise WebsiteFetchError(
            "No readable text content was found. The page may be empty, blocked, or rendered "
            "with JavaScript only.",
            422,
        )
    return ExtractedSite(
        url=url,
        final_url=final_url or url,
        title=title,
        meta_description=meta_description,
        site_name=site_name,
        language=language,
        headings=headings,
        nav_links=nav_links,
        text=text,
        truncated=truncated,
    )


async def extract_from_url(url: str, *, timeout: float, max_bytes: int, max_chars: int) -> ExtractedSite:
    html, final_url = await fetch_html(url, timeout=timeout, max_bytes=max_bytes)
    return extract_site(html, url=url, final_url=final_url, max_chars=max_chars)


__all__ = [
    "ExtractedSite",
    "InvalidURLError",
    "WebsiteFetchError",
    "assert_public_host",
    "extract_from_url",
    "extract_site",
    "fetch_html",
]
