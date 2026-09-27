"""
Background submission-queue worker.

- Runs in a daemon thread inside the Flask process (simple, single-machine
  design appropriate for this assignment's scope; see README for how this
  would be swapped for Celery/RQ + Redis in a production/large-scale setup).
- On startup, any QueueItem left in 'processing' (from a crash / restart) is
  requeued -- this satisfies "resume after interruption" / "server restart
  must not lose the queue".
- Processes items one at a time per call, sequentially, respecting each
  service's own rate limits (no forced concurrency against external services).
- Failures use exponential backoff and are retried up to max_attempts; after
  that they're left in 'failed' state, visible for manual review (section 18).
- A failed URL never stops the whole run: exceptions are caught per-item.
"""
import threading
import time
from datetime import datetime, timedelta, timezone

from .database import get_session
from .models import QueueItem, Submission, URLRecord, utcnow
from .archiver import SERVICES

POLL_INTERVAL_SECONDS = 3
BACKOFF_BASE_SECONDS = 30


class ArchiveWorker:
    def __init__(self):
        self._thread = None
        self._stop_event = threading.Event()
        self._status_lock = threading.Lock()
        self._current_status = "idle"

    # ---- lifecycle -----------------------------------------------------

    def start(self):
        self.recover_interrupted()
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def status(self):
        with self._status_lock:
            return self._current_status

    def _set_status(self, s):
        with self._status_lock:
            self._current_status = s

    # ---- crash recovery --------------------------------------------------

    def recover_interrupted(self):
        """Any item stuck 'processing' from a previous crash goes back to 'queued'."""
        session = get_session()
        try:
            stuck = session.query(QueueItem).filter(QueueItem.status == "processing").all()
            for item in stuck:
                item.status = "queued"
                item.last_error = (item.last_error or "") + " | recovered after restart"
                item.updated_at = utcnow()
            if stuck:
                session.commit()
        finally:
            session.close()

    # ---- main loop -------------------------------------------------------

    def _run_loop(self):
        while not self._stop_event.is_set():
            processed = self._process_one_batch(batch_size=5)
            self._set_status("processing" if processed else "idle")
            time.sleep(POLL_INTERVAL_SECONDS)

    def _process_one_batch(self, batch_size=5):
        session = get_session()
        processed_any = False
        try:
            now = utcnow()
            items = (
                session.query(QueueItem)
                .filter(QueueItem.status == "queued", QueueItem.next_attempt_at <= now)
                .order_by(QueueItem.priority.desc(), QueueItem.added_at.asc())
                .limit(batch_size)
                .all()
            )
            for item in items:
                processed_any = True
                self._process_item(session, item)
        finally:
            session.close()
        return processed_any

    def _process_item(self, session, item: QueueItem):
        item.status = "processing"
        item.updated_at = utcnow()
        session.commit()

        url_record = session.get(URLRecord, item.url_record_id)
        if url_record is None:
            item.status = "failed"
            item.last_error = "url_record missing"
            session.commit()
            return

        # Skip re-archiving already-successful URLs unless explicitly forced
        # (incremental-backup requirement, section 10).
        if not item.force_reachive and url_record.latest_submission_for(item.service) and \
           url_record.latest_submission_for(item.service).status == "success":
            item.status = "done"
            item.updated_at = utcnow()
            session.commit()
            return

        submit_fn = SERVICES.get(item.service)
        submission = Submission(
            url_record_id=url_record.id,
            service=item.service,
            attempt_number=item.attempts + 1,
            submission_started_at=utcnow(),
            last_attempted_at=utcnow(),
        )

        try:
            result = submit_fn(url_record.normalized_url) if submit_fn else None
        except Exception as e:  # a single failure must never kill the worker
            result = None
            submission.error_message = f"unhandled exception: {e}"

        submission.submission_completed_at = utcnow()
        item.attempts += 1
        item.updated_at = utcnow()

        if result and result.success:
            submission.status = "success"
            submission.archive_url = result.archive_url
            submission.archive_identifier = result.archive_identifier
            submission.http_status = result.http_status
            item.status = "done"
            item.last_error = None
        else:
            submission.status = "failed"
            submission.error_message = submission.error_message or (result.error if result else "unknown error")
            submission.http_status = result.http_status if result else None
            item.last_error = submission.error_message

            if item.attempts < item.max_attempts:
                # exponential backoff, kept out of a tight retry loop against the external service
                delay = BACKOFF_BASE_SECONDS * (2 ** (item.attempts - 1))
                item.status = "queued"
                item.next_attempt_at = utcnow() + timedelta(seconds=delay)
            else:
                item.status = "failed"

        session.add(submission)
        session.commit()


worker = ArchiveWorker()


def enqueue_url(session, url_record: URLRecord, services, priority=0, force_reachive=False):
    """Adds queue items for a URL for each requested service, skipping duplicates already queued."""
    added = []
    for service in services:
        existing = (
            session.query(QueueItem)
            .filter(
                QueueItem.url_record_id == url_record.id,
                QueueItem.service == service,
                QueueItem.status.in_(["queued", "processing"]),
            )
            .first()
        )
        if existing:
            if force_reachive:
                existing.force_reachive = True
            continue
        item = QueueItem(
            url_record_id=url_record.id,
            service=service,
            priority=priority,
            force_reachive=force_reachive,
        )
        session.add(item)
        added.append(item)
    return added
