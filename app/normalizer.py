"""
URL normalization & duplicate detection.

Rules applied (documented for the assignment writeup):
- lower-case scheme + host
- drop default ports (80 for http, 443 for https)
- strip fragments (#...) -- fragments recorded separately as "fragment_stripped"
- collapse duplicate slashes in the path
- remove trailing slash (except bare root "/")
- sort query-string parameters so ?b=2&a=1 == ?a=1&b=2
- drop known tracking params (utm_*, gclid, fbclid) since they don't change content
"""
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

TRACKING_PREFIXES = ("utm_",)
TRACKING_EXACT = {"gclid", "fbclid", "mc_cid", "mc_eid"}


def normalize_url(raw_url: str):
    """Returns (normalized_url, had_fragment: bool)"""
    parts = urlsplit(raw_url.strip())

    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()

    # strip default ports
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    path = re.sub(r"/{2,}", "/", parts.path) or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
        if path == "":
            path = "/"

    query_pairs = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not (k.lower() in TRACKING_EXACT or any(k.lower().startswith(p) for p in TRACKING_PREFIXES))
    ]
    query_pairs.sort()
    query = urlencode(query_pairs)

    had_fragment = bool(parts.fragment)
    normalized = urlunsplit((scheme, netloc, path, query, ""))  # fragment always dropped
    return normalized, had_fragment


def same_registrable_domain(url: str, domain_netloc: str, allow_subdomains: bool = True) -> bool:
    netloc = urlsplit(url).netloc.lower()
    domain_netloc = domain_netloc.lower()
    if netloc == domain_netloc:
        return True
    if allow_subdomains and netloc.endswith("." + domain_netloc):
        return True
    return False
