"""
Repository schema.

Domain            -> one tracked website/project
URLRecord         -> one discovered URL belonging to a Domain (deduped by normalized_url)
Submission        -> one archive-service submission attempt for a URLRecord
QueueItem         -> one pending/processing/done unit of work for the background worker

Design notes:
- A URLRecord can have MANY Submissions over time (re-archiving), satisfying the
  "history of multiple archive submissions for the same URL" requirement.
- QueueItem is the persisted, resumable work queue. On process restart, anything
  left in 'processing' is requeued (see worker.py) so a crash never loses work.
"""
from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, DateTime, ForeignKey, Boolean, Text, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc)


class Domain(Base):
    __tablename__ = "domains"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), unique=True, nullable=False)          # e.g. example.com
    base_url = Column(String(500), nullable=False)                    # e.g. https://example.com
    allow_external = Column(Boolean, default=False)                   # follow off-domain links?
    created_at = Column(DateTime, default=utcnow)
    last_scan_started_at = Column(DateTime, nullable=True)
    last_scan_completed_at = Column(DateTime, nullable=True)
    scan_status = Column(String(30), default="never_scanned")
    # never_scanned | scanning | idle | error

    urls = relationship("URLRecord", back_populates="domain", cascade="all, delete-orphan")


class URLRecord(Base):
    __tablename__ = "url_records"
    __table_args__ = (UniqueConstraint("domain_id", "normalized_url", name="uq_domain_normalized_url"),)

    id = Column(Integer, primary_key=True)
    domain_id = Column(Integer, ForeignKey("domains.id"), nullable=False)

    original_url = Column(Text, nullable=False)        # exactly as first discovered
    normalized_url = Column(Text, nullable=False)       # deduped/canonical form
    discovery_source = Column(String(50), nullable=False)
    # html_link | sitemap | sitemap_index | robots_txt | canonical | pagination | feed

    discovery_timestamp = Column(DateTime, default=utcnow)
    last_checked_at = Column(DateTime, nullable=True)

    http_status = Column(Integer, nullable=True)
    is_redirect = Column(Boolean, default=False)
    redirect_target = Column(Text, nullable=True)
    is_accessible = Column(Boolean, nullable=True)      # None = not checked yet
    fragment_stripped = Column(Boolean, default=False)

    domain = relationship("Domain", back_populates="urls")
    submissions = relationship("Submission", back_populates="url_record", cascade="all, delete-orphan")
    queue_items = relationship("QueueItem", back_populates="url_record", cascade="all, delete-orphan")

    def latest_submission_for(self, service):
        subs = [s for s in self.submissions if s.service == service]
        return max(subs, key=lambda s: s.last_attempted_at or utcnow(), default=None)

    def ever_archived(self):
        return any(s.status == "success" for s in self.submissions)


class Submission(Base):
    __tablename__ = "submissions"

    id = Column(Integer, primary_key=True)
    url_record_id = Column(Integer, ForeignKey("url_records.id"), nullable=False)

    service = Column(String(50), nullable=False)         # wayback_machine | archive_today
    status = Column(String(20), default="pending")        # pending|success|failed
    attempt_number = Column(Integer, default=1)

    submission_started_at = Column(DateTime, default=utcnow)
    submission_completed_at = Column(DateTime, nullable=True)
    last_attempted_at = Column(DateTime, default=utcnow)

    archive_url = Column(Text, nullable=True)
    archive_identifier = Column(String(255), nullable=True)
    http_status = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)

    url_record = relationship("URLRecord", back_populates="submissions")


class QueueItem(Base):
    __tablename__ = "queue_items"

    id = Column(Integer, primary_key=True)
    url_record_id = Column(Integer, ForeignKey("url_records.id"), nullable=False)
    service = Column(String(50), nullable=False)

    status = Column(String(20), default="queued")
    # queued | processing | done | failed
    priority = Column(Integer, default=0)                 # higher = sooner
    attempts = Column(Integer, default=0)
    max_attempts = Column(Integer, default=3)
    next_attempt_at = Column(DateTime, default=utcnow)     # for retry backoff
    force_reachive = Column(Boolean, default=False)

    added_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow)
    last_error = Column(Text, nullable=True)

    url_record = relationship("URLRecord", back_populates="queue_items")
