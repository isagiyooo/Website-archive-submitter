import threading
from urllib.parse import urlsplit
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, jsonify, flash

from .database import get_session
from .models import Domain, URLRecord, Submission, QueueItem, utcnow
from .normalizer import normalize_url
from .crawler import discover_all
from .worker import worker, enqueue_url
from .archiver import SERVICES

bp = Blueprint("main", __name__)

ALL_SERVICES = list(SERVICES.keys())


# ------------------------------------------------------------------ helpers

def _domain_stats(session, domain: Domain):
    total_urls = session.query(URLRecord).filter_by(domain_id=domain.id).count()
    queued = session.query(QueueItem).join(URLRecord).filter(
        URLRecord.domain_id == domain.id, QueueItem.status == "queued"
    ).count()
    processing = session.query(QueueItem).join(URLRecord).filter(
        URLRecord.domain_id == domain.id, QueueItem.status == "processing"
    ).count()
    done = session.query(QueueItem).join(URLRecord).filter(
        URLRecord.domain_id == domain.id, QueueItem.status == "done"
    ).count()
    failed = session.query(QueueItem).join(URLRecord).filter(
        URLRecord.domain_id == domain.id, QueueItem.status == "failed"
    ).count()
    successful_submissions = session.query(Submission).join(URLRecord).filter(
        URLRecord.domain_id == domain.id, Submission.status == "success"
    ).count()
    failed_submissions = session.query(Submission).join(URLRecord).filter(
        URLRecord.domain_id == domain.id, Submission.status == "failed"
    ).count()
    last_submission = session.query(Submission).join(URLRecord).filter(
        URLRecord.domain_id == domain.id
    ).order_by(Submission.last_attempted_at.desc()).first()

    return {
        "total_urls": total_urls,
        "queued": queued,
        "processing": processing,
        "done": done,
        "failed": failed,
        "successful_submissions": successful_submissions,
        "failed_submissions": failed_submissions,
        "pending": queued + processing,
        "last_submission_at": last_submission.last_attempted_at if last_submission else None,
    }


def _run_scan(domain_id: int):
    """Runs in a background thread: discover URLs, normalize, dedupe, insert new records."""
    session = get_session()
    try:
        domain = session.get(Domain, domain_id)
        if not domain:
            return
        domain.scan_status = "scanning"
        domain.last_scan_started_at = utcnow()
        session.commit()

        discovered = discover_all(domain.base_url, allow_external=domain.allow_external)

        new_count = 0
        seen_this_scan = set()
        for item in discovered:
            normalized, had_fragment = normalize_url(item.url)
            if normalized in seen_this_scan:
                continue
            seen_this_scan.add(normalized)

            existing = (
                session.query(URLRecord)
                .filter_by(domain_id=domain.id, normalized_url=normalized)
                .first()
            )
            if existing:
                existing.last_checked_at = utcnow()
                continue

            record = URLRecord(
                domain_id=domain.id,
                original_url=item.url,
                normalized_url=normalized,
                discovery_source=item.source,
                fragment_stripped=had_fragment,
                discovery_timestamp=utcnow(),
                last_checked_at=utcnow(),
            )
            session.add(record)
            new_count += 1

        domain.scan_status = "idle"
        domain.last_scan_completed_at = utcnow()
        session.commit()
    except Exception as e:
        session.rollback()
        domain = session.get(Domain, domain_id)
        if domain:
            domain.scan_status = "error"
            session.commit()
    finally:
        session.close()


# ------------------------------------------------------------------ routes

@bp.route("/")
def intro():
    return render_template("intro.html")


@bp.route("/dashboard")
def dashboard():
    session = get_session()
    try:
        domains = session.query(Domain).order_by(Domain.created_at.desc()).all()
        rows = [(d, _domain_stats(session, d)) for d in domains]
        return render_template("dashboard.html", rows=rows, worker_status=worker.status())
    finally:
        session.close()


@bp.route("/domains", methods=["POST"])
def add_domain():
    raw = request.form.get("url", "").strip()
    allow_external = bool(request.form.get("allow_external"))
    if not raw:
        flash("Please enter a domain or URL.")
        return redirect(url_for("main.dashboard"))

    if not raw.startswith("http://") and not raw.startswith("https://"):
        raw = "https://" + raw

    parts = urlsplit(raw)
    name = parts.netloc

    session = get_session()
    try:
        existing = session.query(Domain).filter_by(name=name).first()
        if existing:
            flash(f"{name} is already tracked.")
            return redirect(url_for("main.domain_detail", domain_id=existing.id))

        domain = Domain(name=name, base_url=f"{parts.scheme}://{parts.netloc}/", allow_external=allow_external)
        session.add(domain)
        session.commit()
        domain_id = domain.id
    finally:
        session.close()

    threading.Thread(target=_run_scan, args=(domain_id,), daemon=True).start()
    return redirect(url_for("main.domain_detail", domain_id=domain_id))


@bp.route("/domains/<int:domain_id>")
def domain_detail(domain_id):
    session = get_session()
    try:
        domain = session.get(Domain, domain_id)
        if not domain:
            return "Domain not found", 404
        urls = (
            session.query(URLRecord)
            .filter_by(domain_id=domain.id)
            .order_by(URLRecord.discovery_timestamp.desc())
            .limit(500)
            .all()
        )
        stats = _domain_stats(session, domain)
        return render_template("domain.html", domain=domain, urls=urls, stats=stats, services=ALL_SERVICES)
    finally:
        session.close()


@bp.route("/domains/<int:domain_id>/scan", methods=["POST"])
def trigger_scan(domain_id):
    """Re-scan a domain. Existing URLs are left alone; only newly discovered ones are inserted
    (section 11: incremental discovery of new URLs without rebuilding the repository)."""
    threading.Thread(target=_run_scan, args=(domain_id,), daemon=True).start()
    flash("Scan started.")
    return redirect(url_for("main.domain_detail", domain_id=domain_id))


@bp.route("/domains/<int:domain_id>/queue", methods=["POST"])
def build_queue(domain_id):
    """Queues every URL that has never been successfully archived by the selected service(s)."""
    selected_services = request.form.getlist("services") or ALL_SERVICES
    session = get_session()
    try:
        urls = session.query(URLRecord).filter_by(domain_id=domain_id).all()
        added = 0
        for url_record in urls:
            new_items = enqueue_url(session, url_record, selected_services)
            added += len(new_items)
        session.commit()
        worker.start()
        flash(f"Queued {added} submission(s) across {len(selected_services)} service(s).")
    finally:
        session.close()
    return redirect(url_for("main.domain_detail", domain_id=domain_id))


@bp.route("/domains/<int:domain_id>/reachive/<int:url_id>", methods=["POST"])
def reachive_one(domain_id, url_id):
    """Force re-archiving of a single URL, even if already archived (section 10)."""
    selected_services = request.form.getlist("services") or ALL_SERVICES
    session = get_session()
    try:
        url_record = session.get(URLRecord, url_id)
        if url_record and url_record.domain_id == domain_id:
            enqueue_url(session, url_record, selected_services, priority=10, force_reachive=True)
            session.commit()
            worker.start()
            flash("Re-archive queued.")
    finally:
        session.close()
    return redirect(url_for("main.domain_detail", domain_id=domain_id))


@bp.route("/search")
def search():
    q = request.args.get("q", "").strip()
    service_filter = request.args.get("service", "")
    status_filter = request.args.get("status", "")

    session = get_session()
    try:
        query = session.query(URLRecord)
        if q:
            like = f"%{q}%"
            query = query.join(Domain).filter(
                (URLRecord.normalized_url.like(like)) | (Domain.name.like(like))
            )
        results = query.order_by(URLRecord.discovery_timestamp.desc()).limit(200).all()

        if service_filter or status_filter:
            filtered = []
            for r in results:
                subs = r.submissions
                if service_filter:
                    subs = [s for s in subs if s.service == service_filter]
                if status_filter:
                    subs = [s for s in subs if s.status == status_filter]
                if subs:
                    filtered.append(r)
            results = filtered

        return render_template(
            "search.html", results=results, q=q, service_filter=service_filter,
            status_filter=status_filter, services=ALL_SERVICES,
        )
    finally:
        session.close()


@bp.route("/api/stats")
def api_stats():
    """JSON endpoint the dashboard polls for near-real-time progress (section 17)."""
    session = get_session()
    try:
        domains = session.query(Domain).all()
        data = []
        for d in domains:
            s = _domain_stats(session, d)
            s["domain"] = d.name
            s["id"] = d.id
            s["scan_status"] = d.scan_status
            data.append(s)
        return jsonify({"worker_status": worker.status(), "domains": data})
    finally:
        session.close()
