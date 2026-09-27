# Website Archive Submitter & Automated Backup Repository

A Flask + SQLite tool that takes a domain, discovers its public URLs, submits
them to the Internet Archive (Wayback Machine) and archive.today, and keeps a
permanent, searchable record of what was discovered and archived.

## 1. Setup

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

Then open **http://localhost:5000**. The SQLite database is created
automatically at `data/webarchiver.db` on first run.

> This was written and syntax-checked in a sandbox with no outbound network
> access, so the live HTTP calls (crawling a real site, calling
> web.archive.org / archive.ph) haven't been exercised end-to-end here — run
> it locally against a real domain to see the full flow, and check
> `python -m py_compile app/*.py` if you hit import errors after editing.

## 2. Architecture

```
run.py                 Flask entry point (starts the app + background worker)
app/
  __init__.py          App factory: initializes DB, starts the worker thread
  models.py            SQLAlchemy schema: Domain, URLRecord, Submission, QueueItem
  database.py          Engine/session setup (SQLite)
  normalizer.py         URL normalization + duplicate detection rules
  crawler.py            Discovery: robots.txt, sitemaps, HTML crawl, canonical/feed/pagination
  archiver.py           Wayback Machine + archive.today submission integrations
  worker.py             Persistent queue processor: resume, retry/backoff
  routes.py             Web routes: dashboard, add/scan domain, queue, search
  templates/            Dashboard, domain detail, search pages
  static/style.css
data/webarchiver.db     SQLite repository (created at runtime)
```

### Why this shape
- **SQLite + SQLAlchemy** instead of in-memory structures, so the crawl and
  archive history survive restarts and scale past what fits in RAM (section 14).
- **A persisted `QueueItem` table**, not an in-memory queue, is what makes
  "resume after interruption" real: on every startup, `worker.recover_interrupted()`
  finds anything stuck in `processing` (from a crash) and puts it back to
  `queued` before the worker starts polling again.
- **One `URLRecord` per normalized URL, many `Submission` rows per `URLRecord`**
  is what gives you full archive history per URL (re-archiving over time)
  without losing earlier submissions.
- **A background thread inside the Flask process** is deliberately the
  simplest thing that satisfies the assignment's queue/resume/retry
  requirements for a single-machine demo. See "Scaling beyond this" below
  for how it maps onto a real distributed-worker design.

### Data flow
1. `routes.add_domain` creates a `Domain` row and kicks off `_run_scan` in a
   background thread.
2. `crawler.discover_all` combines robots.txt → sitemap(s) (recursive, index
   or urlset) → a breadth-first HTML crawl that also harvests
   `<link rel=canonical>`, RSS/Atom `<link>` feeds, and `rel=next` pagination.
3. Each discovered URL is normalized (`normalizer.normalize_url`) and
   deduped against the domain's existing `URLRecord`s — only genuinely new
   URLs are inserted, so re-scanning a domain is incremental (section 11).
4. `routes.build_queue` enqueues `QueueItem`s for URLs that haven't been
   successfully archived yet by the selected service(s) (section 10).
5. `worker.ArchiveWorker` polls the queue, submits one item at a time per
   batch, records a `Submission` row per attempt, and retries failures with
   exponential backoff up to `max_attempts` — after which the item is left
   in `failed` for manual review (section 18).
6. The dashboard and domain-detail pages read live stats out of the same
   tables; `/api/stats` exposes them as JSON for polling-based "real-time"
   progress.

## 3. Archive service integration notes

- **Wayback Machine**: uses the documented "Save Page Now" endpoint
  (`GET https://web.archive.org/save/<url>`), reading the resulting snapshot
  location from the `Content-Location` header. No API key needed for casual
  use; a `429` is treated as a rate limit and retried later, never bypassed.
- **archive.today**: has no stable public API. The integration posts to its
  submission form and looks for a resulting `archive.ph/...` URL. When the
  service responds with a CAPTCHA or a 403/429, this is recorded as a
  **failed** submission with a clear error message rather than attempted to
  be worked around — per the assignment's explicit constraint not to bypass
  CAPTCHAs, auth, or rate limits.

## 4. Mapping to the assignment sections

| Section | Where it's implemented |
|---|---|
| 3. Single domain | `add_domain`, `domain_detail`, full scan→queue→submit flow |
| 4. Multi-domain | `Domain` table is 1:N with everything; dashboard lists all domains independently, `build_queue` operates per-domain |
| 5. URL discovery | `crawler.py` (robots.txt, sitemap/sitemap-index, HTML links, canonical, pagination, feeds) |
| 6. URL processing | `normalizer.py` (normalize, dedupe, fragment handling) + `crawler.check_url_status` (HTTP status/redirects) |
| 7-8. Archive services + queue | `archiver.py`, `worker.py`, `QueueItem` |
| 9. Repository | `models.py` — every field the spec lists has a column |
| 10. Incremental backup | `URLRecord.ever_archived()` / `latest_submission_for()` gate re-queueing; `force_reachive` flag for explicit re-archiving |
| 11. Website changes | Re-scan only inserts URLs not already in the domain's `URLRecord` set |
| 12-13. Dashboard + search | `templates/dashboard.html`, `templates/search.html`, `routes.search` |
| 14. Large websites | SQLite-backed (not in-memory), batched queue processing, `max_pages` crawl cap, dedupe before insert |
| 18. Failure recovery | `worker.recover_interrupted()`, per-item try/except, retry/backoff, `failed` status stays visible |

## 5. Known limitations / not implemented (honest gaps)

These are the assignment's **bonus** items, intentionally left out or
stubbed so the core system stays solid and reviewable:

- **JavaScript-rendered pages** (section 15) and the **Instagram bonus**
  (section 16): the crawler only parses server-returned HTML; it doesn't run
  a browser engine. Adding Playwright/Selenium as an alternate discovery
  backend (swap `discover_via_html_crawl`'s `requests.get` for a rendered
  page source) is the natural extension point.
- **Distributed workers / 50+ domains concurrently**: the current worker is
  a single in-process thread. For real scale, swap `worker.py`'s thread loop
  for a proper task queue (Celery/RQ + Redis), and move `QueueItem` claiming
  to use `SELECT ... FOR UPDATE`-style locking so multiple worker processes
  can pull from the same table safely.
- **Change detection / snapshot comparison**: the schema has everything
  needed (timestamps, multiple submissions per URL) to diff two scans, but
  no diff UI is built yet.
- **CSV/JSON export**: not wired up, but trivial against the existing models
  (`URLRecord`/`Submission` → `csv.DictWriter` or `json.dumps`).

## 6. Demonstration checklist (section 19)

1. Add a domain on the dashboard → watch `scan_status` move
   `never_scanned → scanning → idle`.
2. Open the domain page to see the discovered URL inventory and count.
3. Click "Submit un-archived URLs" to build the queue; the dashboard's
   queued/done/failed counters update as the worker processes it.
4. Stop the process (Ctrl+C) mid-queue, restart `python run.py`, and confirm
   any `processing` items resume instead of vanishing (this is
   `recover_interrupted()`).
5. Click "Re-scan" a second time on the same domain and confirm only newly
   discovered URLs are added (existing count doesn't reset).
6. Add a second domain to show multi-domain support running independently.
