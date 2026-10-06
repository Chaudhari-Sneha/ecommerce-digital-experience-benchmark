"""PostgreSQL connection helper and small shared DB utilities."""
import psycopg2
import psycopg2.extras

from .config import DB_CONFIG


def get_connection():
    return psycopg2.connect(**DB_CONFIG)


def upsert_companies(conn, companies: list[dict]) -> dict[str, int]:
    """Sync the companies table to config/companies.yaml. Companies removed
    from the config are marked inactive (history kept), not deleted.
    Returns a mapping of domain -> company_id for active companies."""
    with conn.cursor() as cur:
        for c in companies:
            cur.execute(
                """
                INSERT INTO companies (company_name, domain, homepage_url, category)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (domain) DO UPDATE SET
                    company_name = EXCLUDED.company_name,
                    homepage_url = EXCLUDED.homepage_url,
                    category = EXCLUDED.category,
                    is_active = TRUE
                """,
                (c["name"], c["domain"], c["homepage_url"], c.get("category")),
            )
        cur.execute(
            "UPDATE companies SET is_active = FALSE WHERE NOT (domain = ANY(%s))",
            ([c["domain"] for c in companies],),
        )
        cur.execute("SELECT company_id, domain FROM companies WHERE is_active")
        mapping = {domain: company_id for company_id, domain in cur.fetchall()}
    conn.commit()
    return mapping


def abort_stale_runs(conn, older_than_hours: int = 2) -> int:
    """Runs killed mid-flight (machine slept, process closed) never reach
    finish_collection_run; mark them so the pipeline-quality page doesn't
    show them as perpetually 'running'."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE collection_runs
            SET status = 'aborted', notes = 'run did not finish (process stopped)'
            WHERE status = 'running' AND run_started_at < now() - make_interval(hours => %s)
            """,
            (older_than_hours,),
        )
        count = cur.rowcount
    conn.commit()
    return count


def start_collection_run(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO collection_runs (run_started_at, status) "
            "VALUES (clock_timestamp(), 'running') RETURNING run_id"
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def finish_collection_run(conn, run_id: int, attempted: int, succeeded: int, failed: int, notes: str = None):
    status = "success" if failed == 0 else ("failed" if succeeded == 0 else "partial")
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE collection_runs
            SET run_finished_at = clock_timestamp(), status = %s,
                companies_attempted = %s, companies_succeeded = %s,
                companies_failed = %s, notes = %s
            WHERE run_id = %s
            """,
            (status, attempted, succeeded, failed, notes, run_id),
        )
    conn.commit()


def upsert_company_run_result(conn, run_id: int, company_id: int, **statuses):
    """statuses: pagespeed_status, uptime_status, linkcheck_status, error_message
    (any subset). Missing keys default to NULL on insert / stay unchanged on
    conflict via COALESCE(EXCLUDED.x, existing.x)."""
    fields = ("pagespeed_status", "uptime_status", "linkcheck_status", "error_message")
    values = {f: statuses.get(f) for f in fields}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO company_run_results
                (run_id, company_id, pagespeed_status, uptime_status, linkcheck_status, error_message)
            VALUES (%(run_id)s, %(company_id)s, %(pagespeed_status)s, %(uptime_status)s,
                    %(linkcheck_status)s, %(error_message)s)
            ON CONFLICT (run_id, company_id) DO UPDATE SET
                pagespeed_status = COALESCE(EXCLUDED.pagespeed_status, company_run_results.pagespeed_status),
                uptime_status = COALESCE(EXCLUDED.uptime_status, company_run_results.uptime_status),
                linkcheck_status = COALESCE(EXCLUDED.linkcheck_status, company_run_results.linkcheck_status),
                error_message = COALESCE(EXCLUDED.error_message, company_run_results.error_message)
            """,
            {"run_id": run_id, "company_id": company_id, **values},
        )
    conn.commit()
