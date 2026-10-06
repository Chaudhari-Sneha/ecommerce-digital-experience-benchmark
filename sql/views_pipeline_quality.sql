-- Views behind the Data Quality & Methodology page.

CREATE OR REPLACE VIEW v_recent_runs AS
SELECT
    run_id, run_started_at, run_finished_at, trigger_source, status,
    companies_attempted, companies_succeeded, companies_failed,
    EXTRACT(EPOCH FROM (run_finished_at - run_started_at)) AS duration_seconds
FROM collection_runs
ORDER BY run_started_at DESC;

-- Per-company, per-source outcome of the most recent run -- answers
-- "which sources failed" and "when did we last succeed" directly.
CREATE OR REPLACE VIEW v_last_run_status AS
SELECT
    c.company_id,
    c.company_name,
    crr.run_id,
    cr.run_started_at,
    crr.pagespeed_status,
    crr.uptime_status,
    crr.linkcheck_status,
    crr.error_message
FROM companies c
LEFT JOIN LATERAL (
    SELECT crr.*
    FROM company_run_results crr
    JOIN collection_runs cr ON cr.run_id = crr.run_id
    WHERE crr.company_id = c.company_id
    ORDER BY cr.run_started_at DESC
    LIMIT 1
) crr ON true
LEFT JOIN collection_runs cr ON cr.run_id = crr.run_id
WHERE c.is_active;
