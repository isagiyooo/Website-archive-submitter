"""
Archive-service integrations.

Internet Archive / Wayback Machine
-----------------------------------
Uses the documented "Save Page Now" endpoint: a simple GET/POST to
https://web.archive.org/save/<url>. On success, the archived snapshot's
location is returned in the `Content-Location` response header, and/or can
be derived from the returned `Link` header. No API key is required for
occasional use; the service enforces its own rate limits, which we respect
(no bypass) via backoff in worker.py.

Archive.today / archive.ph
---------------------------
Archive.today has no published, stable public API. Its web submission form
(POST to https://archive.ph/submit/ with a `url` field) is the only
documented mechanism, and the service actively rate-limits and sometimes
CAPTCHA-gates automated submitters. Per the assignment's constraint ("without
bypassing CAPTCHAs, authentication controls, rate limits or other security
mechanisms"), this integration:
  - makes a best-effort plain HTTP submission,
  - detects and surfaces a CAPTCHA/blocked response as a *failed* submission
    with a clear error message, rather than attempting to solve/bypass it,
  - never retries aggressively against this service (see worker.py backoff).
"""
import re
import requests

USER_AGENT = "WebArchiveSubmitterBot/1.0 (+educational-assignment)"
TIMEOUT = 30

WAYBACK_SAVE_URL = "https://web.archive.org/save/{url}"
ARCHIVE_TODAY_SUBMIT_URL = "https://archive.ph/submit/"


class SubmissionResult:
    def __init__(self, success, archive_url=None, archive_identifier=None, http_status=None, error=None):
        self.success = success
        self.archive_url = archive_url
        self.archive_identifier = archive_identifier
        self.http_status = http_status
        self.error = error


def submit_to_wayback_machine(url: str) -> SubmissionResult:
    target = WAYBACK_SAVE_URL.format(url=url)
    try:
        resp = requests.get(target, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, allow_redirects=True)
    except requests.RequestException as e:
        return SubmissionResult(False, error=f"request failed: {e}")

    if resp.status_code == 429:
        return SubmissionResult(False, http_status=429, error="rate limited by web.archive.org")

    archive_url = resp.headers.get("Content-Location")
    if archive_url and archive_url.startswith("/web/"):
        archive_url = "https://web.archive.org" + archive_url

    if not archive_url:
        # fall back: the final resolved URL after redirects is often the snapshot itself
        if "web.archive.org/web/" in resp.url:
            archive_url = resp.url

    if resp.status_code in (200, 302) and archive_url:
        identifier_match = re.search(r"/web/(\d+)/", archive_url)
        identifier = identifier_match.group(1) if identifier_match else None
        return SubmissionResult(True, archive_url=archive_url, archive_identifier=identifier, http_status=resp.status_code)

    return SubmissionResult(
        False,
        http_status=resp.status_code,
        error=f"unexpected response (status {resp.status_code}), no snapshot location returned",
    )


def submit_to_archive_today(url: str) -> SubmissionResult:
    try:
        resp = requests.post(
            ARCHIVE_TODAY_SUBMIT_URL,
            data={"url": url},
            timeout=TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=True,
        )
    except requests.RequestException as e:
        return SubmissionResult(False, error=f"request failed: {e}")

    body_lower = resp.text.lower()
    if "captcha" in body_lower or resp.status_code in (403, 429):
        return SubmissionResult(
            False,
            http_status=resp.status_code,
            error="archive.today requires human verification (CAPTCHA/rate-limit) for this request; "
                  "not bypassed per assignment constraints",
        )

    # archive.today typically redirects to the finished snapshot, e.g. https://archive.ph/xxxxx
    if "archive.ph/" in resp.url or "archive.today/" in resp.url:
        return SubmissionResult(True, archive_url=resp.url, http_status=resp.status_code)

    # Sometimes it returns a page containing a "Refresh" header or a wip link
    match = re.search(r"https?://archive\.(ph|today|is|md)/[A-Za-z0-9]+", resp.text)
    if match:
        return SubmissionResult(True, archive_url=match.group(0), http_status=resp.status_code)

    return SubmissionResult(
        False,
        http_status=resp.status_code,
        error="submission did not return a recognizable snapshot URL (service may have changed its form)",
    )


SERVICES = {
    "wayback_machine": submit_to_wayback_machine,
    "archive_today": submit_to_archive_today,
}
