-- Insight views built from data already collected in pagespeed_raw.raw_json.
-- Recreated on every apply (column lists may change).
DROP VIEW IF EXISTS v_load_time_split;
DROP VIEW IF EXISTS v_device_gap;
DROP VIEW IF EXISTS v_real_user_checks;
DROP VIEW IF EXISTS v_real_user_experience;
DROP VIEW IF EXISTS v_top_findings;

-- Real-user experience from the Chrome UX Report (CrUX), which Google returns inside every
-- PageSpeed response: 75th-percentile values from real Chrome visitors over the previous 28 days
-- (mobile). Unlike the lab test, this is what actual shoppers experienced.
-- Google's thresholds (good / poor): LCP 2.5 s / 4 s, INP 200 / 500 ms, CLS 0.1 / 0.25.
CREATE VIEW v_real_user_experience AS
WITH latest AS (
    SELECT DISTINCT ON (company_id) company_id, fetched_at, raw_json->'loadingExperience' AS le
    FROM pagespeed_raw
    WHERE fetch_status = 'success' AND strategy = 'mobile' AND raw_json->'loadingExperience' ? 'metrics'
    ORDER BY company_id, fetched_at DESC
),
m AS (
    SELECT company_id, fetched_at,
        (le->'metrics'->'LARGEST_CONTENTFUL_PAINT_MS'->>'percentile')::float8 / 1000 AS lcp_p75_s,
        (le->'metrics'->'INTERACTION_TO_NEXT_PAINT'->>'percentile')::float8 AS inp_p75_ms,
        (le->'metrics'->'CUMULATIVE_LAYOUT_SHIFT_SCORE'->>'percentile')::float8 / 100 AS cls_p75,
        (le->'metrics'->'EXPERIMENTAL_TIME_TO_FIRST_BYTE'->>'percentile')::float8 / 1000 AS ttfb_p75_s,
        100 * (le->'metrics'->'LARGEST_CONTENTFUL_PAINT_MS'->'distributions'->2->>'proportion')::float8 AS lcp_poor_pct,
        100 * (le->'metrics'->'INTERACTION_TO_NEXT_PAINT'->'distributions'->2->>'proportion')::float8 AS inp_poor_pct,
        100 * (le->'metrics'->'CUMULATIVE_LAYOUT_SHIFT_SCORE'->'distributions'->2->>'proportion')::float8 AS cls_poor_pct,
        CASE WHEN le->>'origin_fallback' = 'true' THEN 'Whole site' ELSE 'Homepage' END AS data_scope
    FROM latest
)
SELECT c.company_id, c.company_name, m.fetched_at AS collected_at,
    m.lcp_p75_s, m.inp_p75_ms, m.cls_p75, m.ttfb_p75_s,
    m.lcp_poor_pct, m.inp_poor_pct, m.cls_poor_pct,
    CASE WHEN m.lcp_p75_s <= 2.5 THEN 'Good' WHEN m.lcp_p75_s <= 4 THEN 'Needs improvement' ELSE 'Poor' END AS lcp_status,
    CASE WHEN m.inp_p75_ms <= 200 THEN 'Good' WHEN m.inp_p75_ms <= 500 THEN 'Needs improvement' ELSE 'Poor' END AS inp_status,
    CASE WHEN m.cls_p75 <= 0.1 THEN 'Good' WHEN m.cls_p75 <= 0.25 THEN 'Needs improvement' ELSE 'Poor' END AS cls_status,
    (m.lcp_p75_s <= 2.5 AND m.inp_p75_ms <= 200 AND m.cls_p75 <= 0.1) AS passes_core_web_vitals,
    COALESCE(NULLIF(concat_ws(', ',
        CASE WHEN m.lcp_p75_s > 2.5 THEN 'Load time' END,
        CASE WHEN m.inp_p75_ms > 200 THEN 'Responsiveness' END,
        CASE WHEN m.cls_p75 > 0.1 THEN 'Stability' END), ''), 'None') AS failing_checks,
    m.data_scope
FROM m
JOIN companies c ON c.company_id = m.company_id
WHERE c.is_active;

-- Same data, one row per company x check, for a company-by-check matrix.
CREATE VIEW v_real_user_checks AS
SELECT company_id, company_name, 1 AS check_order, 'Load time (LCP)' AS check_name,
       round(lcp_p75_s::numeric, 1)::text || ' s' AS p75_display, lcp_status AS status, lcp_poor_pct AS poor_visits_pct
FROM v_real_user_experience
UNION ALL
SELECT company_id, company_name, 2, 'Responsiveness (INP)', round(inp_p75_ms::numeric)::text || ' ms', inp_status, inp_poor_pct
FROM v_real_user_experience
UNION ALL
SELECT company_id, company_name, 3, 'Stability (CLS)', round(cls_p75::numeric, 2)::text, cls_status, cls_poor_pct
FROM v_real_user_experience;

-- Who should fix slow loading: server side or page side? Real-user load time (p75 LCP) split into
-- the wait for the server's first byte (p75 TTFB: server, CDN and network) and the rest (page build:
-- front-end code, images, scripts). Approximate: 75th percentiles are not strictly additive.
CREATE VIEW v_load_time_split AS
SELECT company_id, company_name,
       ttfb_p75_s AS server_wait_s,
       GREATEST(lcp_p75_s - ttfb_p75_s, 0) AS page_build_s,
       lcp_p75_s AS load_time_s,
       ttfb_p75_s / NULLIF(lcp_p75_s, 0) AS server_share
FROM v_real_user_experience;

-- Is the mobile experience lagging behind desktop? Lighthouse performance score (0-100, higher =
-- better), median of each company's last 3 successful runs per device to damp run-to-run noise.
CREATE VIEW v_device_gap AS
WITH recent AS (
    SELECT company_id, strategy, performance_score,
           ROW_NUMBER() OVER (PARTITION BY company_id, strategy ORDER BY fetched_at DESC) AS rn
    FROM pagespeed_raw WHERE fetch_status = 'success' AND performance_score IS NOT NULL
),
med AS (
    SELECT company_id,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY performance_score) FILTER (WHERE strategy = 'mobile') AS mobile_score,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY performance_score) FILTER (WHERE strategy = 'desktop') AS desktop_score
    FROM recent WHERE rn <= 3 GROUP BY company_id
)
SELECT c.company_id, c.company_name, m.mobile_score, m.desktop_score,
       m.desktop_score - m.mobile_score AS mobile_gap
FROM med m JOIN companies c ON c.company_id = m.company_id
WHERE c.is_active AND m.mobile_score IS NOT NULL AND m.desktop_score IS NOT NULL;

-- What exactly is slowing each site down: Lighthouse's failing checks with its own estimated
-- savings (lab estimates, not guarantees). Only findings present in >= 2 of the company's last
-- 3 mobile runs are kept, so one-off test noise is dropped. Top 3 per company, ordered by
-- Lighthouse's largest single estimated saving (CLS weighted x10000 to compare with ms).
CREATE VIEW v_top_findings AS
WITH runs AS (
    SELECT company_id, raw_json,
           ROW_NUMBER() OVER (PARTITION BY company_id ORDER BY fetched_at DESC) AS rn
    FROM pagespeed_raw
    WHERE fetch_status = 'success' AND strategy = 'mobile'
),
audits AS (
    SELECT r.company_id, r.rn, a.key AS audit_id,
           a.value->>'title' AS finding, a.value->>'displayValue' AS detail,
           (a.value->'metricSavings'->>'LCP')::float8 AS lcp_ms,
           (a.value->'metricSavings'->>'FCP')::float8 AS fcp_ms,
           (a.value->'metricSavings'->>'TBT')::float8 AS tbt_ms,
           (a.value->'metricSavings'->>'CLS')::float8 AS cls
    FROM runs r, jsonb_each(r.raw_json->'lighthouseResult'->'audits') a
    WHERE r.rn <= 3
      AND jsonb_typeof(a.value->'score') = 'number' AND (a.value->>'score')::float8 < 0.9
      AND jsonb_typeof(a.value->'metricSavings') = 'object'
),
flagged AS (
    SELECT * FROM audits
    WHERE COALESCE(lcp_ms, 0) >= 100 OR COALESCE(fcp_ms, 0) >= 100
       OR COALESCE(tbt_ms, 0) >= 100 OR COALESCE(cls, 0) >= 0.05
),
persistent AS (
    SELECT company_id, audit_id, COUNT(*) AS seen FROM flagged GROUP BY company_id, audit_id HAVING COUNT(*) >= 2
),
runs_available AS (
    SELECT company_id, LEAST(3, COUNT(*)) AS n FROM runs GROUP BY company_id
),
latest_detail AS (
    SELECT DISTINCT ON (f.company_id, f.audit_id) f.*
    FROM flagged f JOIN persistent p ON p.company_id = f.company_id AND p.audit_id = f.audit_id
    ORDER BY f.company_id, f.audit_id, f.rn
),
ranked AS (
    SELECT d.*, p.seen,
           ROW_NUMBER() OVER (PARTITION BY d.company_id ORDER BY
               GREATEST(COALESCE(d.lcp_ms, 0), COALESCE(d.fcp_ms, 0), COALESCE(d.tbt_ms, 0), COALESCE(d.cls, 0) * 10000) DESC) AS finding_rank
    FROM latest_detail d JOIN persistent p ON p.company_id = d.company_id AND p.audit_id = d.audit_id
)
SELECT c.company_id, c.company_name, r.finding_rank, r.finding,
       COALESCE(r.detail, '') AS detail,
       concat_ws(', ',
           CASE WHEN r.lcp_ms >= 100 THEN 'load −' || round((r.lcp_ms / 1000)::numeric, 1) || ' s' END,
           CASE WHEN r.fcp_ms >= 100 THEN 'first paint −' || round((r.fcp_ms / 1000)::numeric, 1) || ' s' END,
           CASE WHEN r.tbt_ms >= 100 THEN 'blocking −' || round(r.tbt_ms::numeric) || ' ms' END,
           CASE WHEN r.cls >= 0.05 THEN 'layout shift −' || round(r.cls::numeric, 2) END) AS lighthouse_est_saving,
       r.seen::text || ' of last ' || ra.n::text || ' runs' AS seen_in
FROM ranked r
JOIN runs_available ra ON ra.company_id = r.company_id
JOIN companies c ON c.company_id = r.company_id
WHERE r.finding_rank <= 3 AND c.is_active;
