"""Single entrypoint Task Scheduler calls on a schedule (see
scripts/register_task_scheduler.ps1). Runs all three collectors for every
active company, scoring each company (today's Digital Friction Score +
anomaly detection) as soon as its data is in, and logs everything to
collection_runs / company_run_results.

Manual run:  python -m collectors.orchestrator
"""
import json
import logging
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import anomaly_detection, db, linkcheck_collector, pagespeed_collector, scoring, uptime_collector
from .config import COMPANY_STAGGER_SECONDS, DATA_RAW_DIR, ROOT_DIR, load_companies, redact


class _RedactingFormatter(logging.Formatter):
    """Tracebacks quote request URLs, so keep the API key out of the log."""
    def format(self, record):
        return redact(super().format(record))


# The scheduled task runs windowless (pythonw.exe, no stderr), so the file is the real log.
LOG_DIR = ROOT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
_handlers = [logging.FileHandler(LOG_DIR / "pipeline.log", encoding="utf-8")]
if sys.stderr is not None:
    _handlers.append(logging.StreamHandler())
for _h in _handlers:
    _h.setFormatter(_RedactingFormatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=_handlers)
log = logging.getLogger("orchestrator")

IST = ZoneInfo("Asia/Kolkata")


def _dump_raw_snapshot(conn, run_id: int, company_id: int, domain: str):
    """Audit-trail JSON of exactly what got written to the raw tables for
    this company on this run (pagespeed raw_json omitted here -- it's kept
    in full in the DB already, this file is for quick human inspection)."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT strategy, performance_score, accessibility_score, best_practices_score,
                      seo_score, lcp_ms, cls, inp_ms, ttfb_ms, fetch_status
               FROM pagespeed_raw WHERE run_id = %s AND company_id = %s""",
            (run_id, company_id),
        )
        pagespeed = [
            dict(zip(
                ["strategy", "performance_score", "accessibility_score", "best_practices_score",
                 "seo_score", "lcp_ms", "cls", "inp_ms", "ttfb_ms", "fetch_status"], row
            ))
            for row in cur.fetchall()
        ]
        cur.execute(
            """SELECT status_code, response_time_ms, outcome, final_url, error_message
               FROM uptime_checks WHERE run_id = %s AND company_id = %s""",
            (run_id, company_id),
        )
        row = cur.fetchone()
        uptime = dict(zip(["status_code", "response_time_ms", "outcome", "final_url", "error_message"], row)) if row else None

        cur.execute(
            """SELECT links_found, links_checked, links_broken, links_unchecked, broken_pct,
                      collection_status, broken_urls_sample
               FROM link_check_runs WHERE run_id = %s AND company_id = %s""",
            (run_id, company_id),
        )
        row = cur.fetchone()
        linkcheck = dict(zip(
            ["links_found", "links_checked", "links_broken", "links_unchecked",
             "broken_pct", "collection_status", "broken_urls_sample"], row
        )) if row else None

    conn.commit()  # end the read transaction; don't leave it idle across the next company's collection

    day_dir = DATA_RAW_DIR / datetime.now(IST).strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    out_path = day_dir / f"{domain}_run{run_id}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"pagespeed": pagespeed, "uptime": uptime, "linkcheck": linkcheck}, f, indent=2, default=str)


def run():
    conn = db.get_connection()
    companies = load_companies()
    domain_to_id = db.upsert_companies(conn, companies)
    aborted = db.abort_stale_runs(conn)
    if aborted:
        log.warning("Marked %d stale unfinished run(s) as aborted", aborted)
    run_id = db.start_collection_run(conn)
    log.info("Started collection run %s for %d companies", run_id, len(companies))

    succeeded = 0
    failed = 0

    for company in companies:
        domain = company["domain"]
        company_id = domain_to_id[domain]
        homepage_url = company["homepage_url"]
        log.info("Collecting %s (%s)", company["name"], domain)

        statuses = {}
        try:
            statuses["pagespeed_status"], perr = pagespeed_collector.collect_for_company(
                conn, run_id, company_id, homepage_url
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Pagespeed collection failed for %s", domain)
            statuses["pagespeed_status"], perr = "error", str(exc)

        try:
            statuses["uptime_status"], uerr = uptime_collector.collect_for_company(
                conn, run_id, company_id, homepage_url
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Uptime collection failed for %s", domain)
            statuses["uptime_status"], uerr = "error", str(exc)

        try:
            statuses["linkcheck_status"], lerr = linkcheck_collector.collect_for_company(
                conn, run_id, company_id, homepage_url, domain
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Link check failed for %s", domain)
            statuses["linkcheck_status"], lerr = "error", str(exc)

        error_message = redact(" | ".join(filter(None, [perr, uerr, lerr]))) or None
        db.upsert_company_run_result(conn, run_id, company_id, error_message=error_message, **statuses)

        try:
            _dump_raw_snapshot(conn, run_id, company_id, domain)
        except Exception:  # noqa: BLE001
            log.exception("Raw snapshot dump failed for %s (non-fatal)", domain)

        # 'blocked' is a known, recorded outcome (site's bot protection), not a pipeline failure.
        if any(v == "error" for v in statuses.values()):
            failed += 1
        else:
            succeeded += 1

        # Score each company as soon as it's collected, so a run cut short
        # (PC shut down, window closed) still leaves usable scores behind.
        try:
            scoring.compute_and_store(conn, company_id)
            anomaly_detection.detect_for_company(conn, company_id)
        except Exception:  # noqa: BLE001
            conn.rollback()
            log.exception("Scoring failed for %s", domain)

        time.sleep(COMPANY_STAGGER_SECONDS)

    log.info("Collection complete: %d succeeded, %d had at least one failed source", succeeded, failed)
    db.finish_collection_run(conn, run_id, len(companies), succeeded, failed)
    conn.close()
    log.info("Run %s finished", run_id)


if __name__ == "__main__":
    run()
