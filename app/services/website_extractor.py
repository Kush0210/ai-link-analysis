"""Async, SSRF-safe website fetcher and structured HTML content extractor."""

from __future__ import annotations

import json
import logging
import re
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from app.config import Settings
from app.schemas import ExtractedWebsiteContent
from app.utils.validators import URLValidationError, validate_public_url

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; AILinkAnalysisBot/1.0; +https://github.com/ai-link-analysis)"
)

# Tags whose contents add noise rather than business meaning
NOISE_TAGS = (
    "script",
    "style",
    "noscript",
    "svg",
    "iframe",
    "canvas",
    "template",
    "audio",
    "video",
    "source",
    "object",
    "embed",
)

# High-signal internal subpage path keywords for optional context enrichment
SUBPAGE_KEYWORDS = (
    "about",
    "service",
    "solution",
    "product",
    "offering",
    "capabilit",
    "what-we-do",
    "pricing",
    "industr",
)

SOCIAL_DOMAINS = {
    "linkedin.com": "LinkedIn",
    "instagram.com": "Instagram",
    "facebook.com": "Facebook",
    "twitter.com": "X (Twitter)",
    "x.com": "X (Twitter)",
    "youtube.com": "YouTube",
}


class WebsiteExtractionError(Exception):
    """Raised when a website cannot be fetched or does not contain analyzable HTML."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class WebsiteExtractor:
    """Fetches a public URL safely (with SSRF checks on redirects) and extracts clean content."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def extract(self, url: str) -> ExtractedWebsiteContent:
        """Validate, fetch, and parse a website into clean structured content."""
        validated_url = validate_public_url(url)

        timeout = httpx.Timeout(
            connect=min(8.0, self._settings.request_timeout_seconds),
            read=self._settings.request_timeout_seconds,
            write=5.0,
            pool=5.0,
        )
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        async with httpx.AsyncClient(
            timeout=timeout,
            headers=headers,
            follow_redirects=False,
            verify=True,
        ) as client:
            final_url, html = await self._fetch_html_safe(client, validated_url)
            extracted = self.parse_html(
                requested_url=validated_url,
                final_url=final_url,
                html=html,
            )

            # Optional targeted subpage crawl to enrich thin homepages or capture /services & /about
            if self._settings.enable_subpage_crawl and self._settings.max_subpages > 0:
                candidate_subpages = self._discover_high_signal_subpages(html, final_url)
                extra_sections: list[str] = []
                crawled: list[str] = []

                for sub_url in candidate_subpages[: self._settings.max_subpages]:
                    try:
                        sub_valid = validate_public_url(sub_url)
                        _, sub_html = await self._fetch_html_safe(client, sub_valid)
                        sub_parsed = self.parse_html(
                            requested_url=sub_valid,
                            final_url=sub_valid,
                            html=sub_html,
                        )
                        if sub_parsed.main_text:
                            label = urlparse(sub_valid).path.strip("/") or "subpage"
                            snippet = sub_parsed.main_text[:2500]
                            extra_sections.append(f"\n--- Subpage: /{label} ---\n{snippet}")
                            crawled.append(sub_valid)
                            # Merge any newly discovered H1/H2 headings
                            for level in ("h1", "h2"):
                                existing = extracted.headings.setdefault(level, [])
                                for h in sub_parsed.headings.get(level, []):
                                    if h not in existing and len(existing) < 20:
                                        existing.append(h)
                    except Exception as exc:  # noqa: BLE001 — subpages are best-effort
                        logger.debug("Skipping subpage %s: %s", sub_url, exc)

                if extra_sections:
                    combined = (extracted.main_text + "\n" + "\n".join(extra_sections)).strip()
                    extracted.main_text = combined[: self._settings.max_extracted_chars]
                    extracted.subpages_crawled = crawled
                    extracted.content_length_chars = len(extracted.main_text)

        if not extracted.title and not extracted.meta_description and len(extracted.main_text) < 40:
            raise WebsiteExtractionError(
                "The website returned minimal or empty readable text (it may require JavaScript rendering or block automated access).",
                status_code=422,
            )

        return extracted

    async def _fetch_html_safe(
        self,
        client: httpx.AsyncClient,
        start_url: str,
    ) -> tuple[str, str]:
        """Fetch HTML while manually validating every redirect hop against SSRF rules."""
        current_url = start_url
        max_hops = self._settings.max_redirects

        for hop in range(max_hops + 1):
            try:
                async with client.stream("GET", current_url) as response:
                    # Handle HTTP redirects safely
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise WebsiteExtractionError(
                                f"Website returned redirect status {response.status_code} without a Location header.",
                                status_code=502,
                            )
                        if hop >= max_hops:
                            raise WebsiteExtractionError(
                                f"Website exceeded the maximum redirect limit ({max_hops}).",
                                status_code=502,
                            )
                        next_url = urljoin(current_url, location)
                        current_url = validate_public_url(next_url)
                        continue

                    if response.status_code == 404:
                        raise WebsiteExtractionError(
                            f"Website returned HTTP 404 (Page Not Found) for '{current_url}'.",
                            status_code=422,
                        )
                    if response.status_code in {401, 403}:
                        raise WebsiteExtractionError(
                            f"Website denied access (HTTP {response.status_code}) for '{current_url}'. It may block automated requests.",
                            status_code=422,
                        )
                    if response.status_code >= 400:
                        raise WebsiteExtractionError(
                            f"Website returned HTTP {response.status_code} for '{current_url}'.",
                            status_code=502,
                        )

                    content_type = (response.headers.get("content-type") or "").lower()
                    if content_type and not any(
                        token in content_type
                        for token in ("text/html", "application/xhtml", "text/plain", "application/xml")
                    ):
                        raise WebsiteExtractionError(
                            f"Unsupported content type '{content_type}'. Only HTML web pages can be analyzed.",
                            status_code=422,
                        )

                    chunks: list[bytes] = []
                    total_bytes = 0
                    async for chunk in response.aiter_bytes():
                        total_bytes += len(chunk)
                        if total_bytes > self._settings.max_html_bytes:
                            chunks.append(chunk[: max(0, self._settings.max_html_bytes - (total_bytes - len(chunk)))])
                            break
                        chunks.append(chunk)

                    raw_bytes = b"".join(chunks)
                    encoding = response.encoding or "utf-8"
                    html_text = raw_bytes.decode(encoding, errors="replace")
                    return current_url, html_text

            except URLValidationError:
                raise
            except WebsiteExtractionError:
                raise
            except httpx.TimeoutException as exc:
                raise WebsiteExtractionError(
                    f"Timed out while fetching '{current_url}' after {self._settings.request_timeout_seconds}s.",
                    status_code=504,
                ) from exc
            except httpx.ConnectError as exc:
                raise WebsiteExtractionError(
                    f"Could not connect to '{current_url}'. Verify the domain is reachable.",
                    status_code=502,
                ) from exc
            except httpx.HTTPError as exc:
                raise WebsiteExtractionError(
                    f"HTTP error while fetching '{current_url}': {exc}",
                    status_code=502,
                ) from exc

        raise WebsiteExtractionError("Failed to fetch website content.", status_code=502)

    def parse_html(
        self,
        requested_url: str,
        final_url: str,
        html: str,
    ) -> ExtractedWebsiteContent:
        """Parse raw HTML into cleaned, deduplicated, token-efficient business signals."""
        soup = BeautifulSoup(html, "html.parser")
        parsed_url = urlparse(final_url)
        domain = (parsed_url.hostname or "").lower()

        html_tag = soup.find("html")
        language = html_tag.get("lang") if isinstance(html_tag, Tag) else None
        if isinstance(language, list):
            language = language[0] if language else None

        # Extract JSON-LD structured data before removing <script> tags
        structured_data = self._extract_json_ld(soup)

        # Extract core metadata
        title = self._clean_whitespace(soup.title.get_text(" ", strip=True)) if soup.title else None
        meta_description = self._get_meta(soup, name="description")
        meta_keywords_raw = self._get_meta(soup, name="keywords")
        meta_keywords = (
            [kw.strip() for kw in meta_keywords_raw.split(",") if kw.strip()][:20]
            if meta_keywords_raw
            else []
        )
        og_title = self._get_meta(soup, prop="og:title")
        og_description = self._get_meta(soup, prop="og:description")
        og_site_name = self._get_meta(soup, prop="og:site_name")

        # Extract social links & navigation labels before stripping boilerplate
        social_links = self._extract_social_links(soup)
        navigation_links = self._extract_navigation_labels(soup)

        # Strip noisy tags
        for tag in soup.find_all(NOISE_TAGS):
            tag.decompose()

        # Extract heading hierarchy
        headings: dict[str, list[str]] = {}
        for level, limit in (("h1", 8), ("h2", 15), ("h3", 15)):
            items: list[str] = []
            for h in soup.find_all(level):
                text = self._clean_whitespace(h.get_text(" ", strip=True))
                if text and len(text) <= 200 and text not in items:
                    items.append(text)
                if len(items) >= limit:
                    break
            if items:
                headings[level] = items

        # Extract main cleaned text (prefer <main> or <article>, fall back to <body>)
        main_text = self._extract_clean_body_text(soup)
        main_text = main_text[: self._settings.max_extracted_chars]

        return ExtractedWebsiteContent(
            requested_url=requested_url,
            final_url=final_url,
            domain=domain,
            language=str(language).strip() if language else None,
            title=title or None,
            meta_description=meta_description,
            meta_keywords=meta_keywords,
            og_title=og_title,
            og_description=og_description,
            og_site_name=og_site_name,
            headings=headings,
            navigation_links=navigation_links,
            social_links=social_links,
            structured_data=structured_data,
            main_text=main_text,
            content_length_chars=len(main_text),
        )

    def _discover_high_signal_subpages(self, html: str, base_url: str) -> list[str]:
        """Find up to a few same-origin /about or /services links for richer understanding."""
        soup = BeautifulSoup(html, "html.parser")
        base_parsed = urlparse(base_url)
        base_host = (base_parsed.hostname or "").lower()
        base_path = base_parsed.path.rstrip("/")

        scored: list[tuple[int, str]] = []
        seen: set[str] = set()

        for anchor in soup.find_all("a", href=True):
            href = str(anchor["href"]).strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue

            full_url = urljoin(base_url, href)
            parsed = urlparse(full_url)
            if parsed.scheme not in ("http", "https"):
                continue
            if (parsed.hostname or "").lower() != base_host:
                continue

            clean_path = parsed.path.rstrip("/")
            if not clean_path or clean_path == base_path:
                continue

            # Avoid binary/media downloads or deep blog archives
            lower_path = clean_path.lower()
            if any(
                lower_path.endswith(ext)
                for ext in (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".zip", ".xml", ".json")
            ):
                continue
            if lower_path.count("/") > 3:
                continue

            normalized = f"{parsed.scheme}://{parsed.netloc}{clean_path}"
            if normalized in seen:
                continue

            anchor_text = self._clean_whitespace(anchor.get_text(" ", strip=True)).lower()
            score = 0
            for kw in SUBPAGE_KEYWORDS:
                if kw in lower_path:
                    score += 3
                if kw in anchor_text:
                    score += 2
            if score > 0:
                seen.add(normalized)
                scored.append((score, normalized))

        scored.sort(key=lambda item: (-item[0], len(item[1])))
        return [url for _, url in scored]

    @staticmethod
    def _get_meta(
        soup: BeautifulSoup,
        name: Optional[str] = None,
        prop: Optional[str] = None,
    ) -> Optional[str]:
        tag = None
        if name:
            tag = soup.find("meta", attrs={"name": re.compile(f"^{re.escape(name)}$", re.I)})
        elif prop:
            tag = soup.find("meta", attrs={"property": re.compile(f"^{re.escape(prop)}$", re.I)})
        if isinstance(tag, Tag) and tag.get("content"):
            cleaned = WebsiteExtractor._clean_whitespace(str(tag["content"]))
            return cleaned or None
        return None

    @staticmethod
    def _extract_json_ld(soup: BeautifulSoup) -> list[dict]:
        items: list[dict] = []
        for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
            raw = script.string or script.get_text()
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue

            nodes = parsed if isinstance(parsed, list) else [parsed]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                compact = {
                    k: node[k]
                    for k in ("@type", "name", "description", "url", "sameAs", "areaServed", "address", "serviceType")
                    if k in node
                }
                if compact:
                    items.append(compact)
                if len(items) >= 5:
                    return items
        return items

    @staticmethod
    def _extract_social_links(soup: BeautifulSoup) -> dict[str, str]:
        socials: dict[str, str] = {}
        for anchor in soup.find_all("a", href=True):
            href = str(anchor["href"]).strip()
            parsed = urlparse(href)
            host = (parsed.hostname or "").lower().removeprefix("www.")
            for domain, platform_name in SOCIAL_DOMAINS.items():
                if host == domain or host.endswith(f".{domain}"):
                    socials.setdefault(platform_name, href)
                    break
        return socials

    @staticmethod
    def _extract_navigation_labels(soup: BeautifulSoup) -> list[str]:
        labels: list[str] = []
        nav_containers = soup.find_all(["nav", "header"])
        for container in nav_containers:
            for anchor in container.find_all("a"):
                text = WebsiteExtractor._clean_whitespace(anchor.get_text(" ", strip=True))
                if 2 <= len(text) <= 45 and text not in labels:
                    labels.append(text)
                if len(labels) >= 25:
                    return labels
        return labels

    @staticmethod
    def _extract_clean_body_text(soup: BeautifulSoup) -> str:
        """Extract deduplicated readable paragraphs and list items."""
        body = soup.find("body") or soup
        # Keep footer text separately at the end (often holds location/company cues),
        # while prioritizing primary content elements.
        lines: list[str] = []
        seen_lower: set[str] = set()

        for element in body.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "th", "blockquote", "address"]):
            text = WebsiteExtractor._clean_whitespace(element.get_text(" ", strip=True))
            if len(text) < 3:
                continue
            key = text.lower()
            if key in seen_lower:
                continue
            seen_lower.add(key)
            lines.append(text)

        if not lines:
            # Fallback if page relies purely on <div> / <span> elements
            raw = body.get_text("\n", strip=True)
            for part in raw.splitlines():
                cleaned = WebsiteExtractor._clean_whitespace(part)
                if len(cleaned) >= 3 and cleaned.lower() not in seen_lower:
                    seen_lower.add(cleaned.lower())
                    lines.append(cleaned)

        return "\n".join(lines)

    @staticmethod
    def _clean_whitespace(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()
