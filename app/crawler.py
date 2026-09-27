"""
URL discovery engine.

Combines several discovery methods, per assignment section 5:
  - robots.txt (Sitemap: directives)
  - sitemap.xml / sitemap indexes (recursive)
  - internal HTML <a href> links (breadth-first crawl, same-domain only unless allow_external)
  - <link rel="canonical">
  - <link rel="alternate" type="application/rss+xml|atom+xml"> feeds
  - pagination (<a rel="next">, ?page=, rel="next" link tags)

Stays within the configured domain unless allow_external is set (section 5's requirement).
Does NOT execute JavaScript (see README "Known limitations" -> JS-rendered sites, section 15/16).
"""
import time
from collections import deque
from urllib.parse import urljoin, urlsplit
import requests
from bs4 import BeautifulSoup

from .normalizer import same_registrable_domain

USER_AGENT = "WebArchiveSubmitterBot/1.0 (+educational-assignment)"
REQUEST_TIMEOUT = 10
DEFAULT_MAX_PAGES = 200  # safety cap for the HTML crawl per scan


class DiscoveredURL:
    __slots__ = ("url", "source")

    def __init__(self, url, source):
        self.url = url
        self.source = source


def _get(url, session):
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
        return resp
    except requests.RequestException:
        return None


def discover_from_robots(base_url, session):
    """Returns list of sitemap URLs referenced in robots.txt."""
    robots_url = urljoin(base_url, "/robots.txt")
    resp = _get(robots_url, session)
    sitemaps = []
    if resp and resp.status_code == 200:
        for line in resp.text.splitlines():
            line = line.strip()
            if line.lower().startswith("sitemap:"):
                sitemaps.append(line.split(":", 1)[1].strip())
    return sitemaps


def discover_from_sitemap(sitemap_url, session, seen_sitemaps=None, depth=0, max_depth=4):
    """Recursively parses sitemap indexes and urlsets. Returns list[DiscoveredURL]."""
    if seen_sitemaps is None:
        seen_sitemaps = set()
    if sitemap_url in seen_sitemaps or depth > max_depth:
        return []
    seen_sitemaps.add(sitemap_url)

    resp = _get(sitemap_url, session)
    if not resp or resp.status_code != 200:
        return []

    results = []
    try:
        soup = BeautifulSoup(resp.content, "xml")
    except Exception:
        soup = BeautifulSoup(resp.content, "html.parser")

    sitemap_tags = soup.find_all("sitemap")
    if sitemap_tags:
        for tag in sitemap_tags:
            loc = tag.find("loc")
            if loc and loc.text:
                results.extend(
                    discover_from_sitemap(loc.text.strip(), session, seen_sitemaps, depth + 1, max_depth)
                )
    else:
        for url_tag in soup.find_all("url"):
            loc = url_tag.find("loc")
            if loc and loc.text:
                results.append(DiscoveredURL(loc.text.strip(), "sitemap_index" if depth > 0 else "sitemap"))
    return results


def discover_via_html_crawl(base_url, domain_netloc, session, allow_external=False, max_pages=DEFAULT_MAX_PAGES):
    """
    Breadth-first crawl of internal links starting from base_url.
    Also picks up canonical links, feed links, and pagination links along the way.
    """
    results = []
    visited = set()
    queue = deque([base_url])

    while queue and len(visited) < max_pages:
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)

        resp = _get(url, session)
        if not resp or "text/html" not in resp.headers.get("Content-Type", ""):
            continue
        if resp.status_code >= 400:
            continue

        soup = BeautifulSoup(resp.text, "html.parser")

        # standard internal links
        for a in soup.find_all("a", href=True):
            link = urljoin(url, a["href"])
            if link.startswith("mailto:") or link.startswith("javascript:") or link.startswith("tel:"):
                continue
            in_domain = same_registrable_domain(link, domain_netloc)
            if in_domain or allow_external:
                results.append(DiscoveredURL(link, "html_link"))
                if in_domain and link not in visited:
                    queue.append(link)

        # canonical
        canonical = soup.find("link", rel=lambda v: v and "canonical" in v)
        if canonical and canonical.get("href"):
            results.append(DiscoveredURL(urljoin(url, canonical["href"]), "canonical"))

        # feeds
        for feed in soup.find_all("link", type=lambda v: v and ("rss" in v or "atom" in v)):
            if feed.get("href"):
                results.append(DiscoveredURL(urljoin(url, feed["href"]), "feed"))

        # pagination: <link rel="next"> and <a rel="next">
        next_link = soup.find("link", rel=lambda v: v and "next" in v)
        if next_link and next_link.get("href"):
            nxt = urljoin(url, next_link["href"])
            results.append(DiscoveredURL(nxt, "pagination"))
            if same_registrable_domain(nxt, domain_netloc) and nxt not in visited:
                queue.append(nxt)
        for a in soup.find_all("a", rel=lambda v: v and "next" in v, href=True):
            nxt = urljoin(url, a["href"])
            results.append(DiscoveredURL(nxt, "pagination"))

    return results


def discover_all(base_url, allow_external=False, max_pages=DEFAULT_MAX_PAGES):
    """
    Runs every discovery method and returns a combined list[DiscoveredURL].
    Caller is responsible for normalizing/deduping (see normalizer.py).
    """
    session = requests.Session()
    domain_netloc = urlsplit(base_url).netloc

    discovered = []

    # 1. robots.txt -> sitemap references
    sitemap_urls = discover_from_robots(base_url, session)
    if not sitemap_urls:
        # fall back to the conventional default location
        sitemap_urls = [urljoin(base_url, "/sitemap.xml")]

    for sm_url in sitemap_urls:
        discovered.extend(discover_from_sitemap(sm_url, session))

    # 2. HTML crawl (also yields canonical/feed/pagination links)
    discovered.extend(discover_via_html_crawl(base_url, domain_netloc, session, allow_external, max_pages))

    # always include the seed URL itself
    discovered.append(DiscoveredURL(base_url, "html_link"))

    return discovered


def check_url_status(url, session=None):
    """
    Lightweight HEAD (fallback GET) check for HTTP status / redirect target.
    Returns dict: {status, is_redirect, redirect_target, is_accessible}
    """
    sess = session or requests.Session()
    try:
        resp = sess.head(url, timeout=REQUEST_TIMEOUT, allow_redirects=False, headers={"User-Agent": USER_AGENT})
        if resp.status_code == 405:  # some servers reject HEAD
            resp = sess.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=False, headers={"User-Agent": USER_AGENT})
        is_redirect = resp.status_code in (301, 302, 303, 307, 308)
        return {
            "status": resp.status_code,
            "is_redirect": is_redirect,
            "redirect_target": resp.headers.get("Location") if is_redirect else None,
            "is_accessible": resp.status_code < 400,
        }
    except requests.RequestException as e:
        return {"status": None, "is_redirect": False, "redirect_target": None, "is_accessible": False, "error": str(e)}
