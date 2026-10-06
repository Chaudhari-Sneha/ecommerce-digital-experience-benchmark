-- Management layer: prioritization views built only from measured signals.
--
-- Management Priority Score (MPS, 0-100) ranks where to look first. It is a
-- prioritization aid, NOT an estimate of revenue, conversion or customer impact
-- (none of those are measured by this pipeline).
--
--   MPS = confidence_factor x (0.35 x Severity + 0.25 x Gap + 0.25 x Deterioration + 0.15 x Persistence)
--
--   Severity      = latest reliable friction score (0-100)
--   Gap           = points above that day's industry average x 5, capped at 100 (20+ pts above = 100; at/below avg = 0)
--   Deterioration = points above the company's own 7-day baseline x 10, capped at 100
--                   (10+ pts worse = 100; improving = 0). The baseline is the mean of its reliable
--                   readings in the 7 days before the latest one -- single readings are noisy.
--   Persistence   = % of the company's reliable readings in the last 14 days that were above that
--                   day's industry average
--   confidence_factor = 0.5 + 0.5 x (data completeness / 100) x min(1, readings in last 21 days / 7)
--                   (1.0 = fully measured with a week of history; never below 0.5, so weak data
--                   lowers priority but cannot hide a severe problem)
--
-- Only reliable scores (>= 60% data completeness, same rule as is_score_reliable) are used.

-- Recreated from scratch on every apply (column lists change as the layer evolves);
-- CASCADE only reaches the views defined in this file, which all depend on v_reliable_scores.
DROP VIEW IF EXISTS v_reliable_scores CASCADE;

CREATE OR REPLACE FUNCTION fmt_signed(x NUMERIC) RETURNS TEXT
LANGUAGE sql IMMUTABLE AS
$$ SELECT CASE WHEN x IS NULL THEN 'n/a' WHEN x >= 0 THEN '+' || round(x, 1)::text ELSE round(x, 1)::text END $$;

CREATE OR REPLACE VIEW v_reliable_scores AS
SELECT fs.company_id, c.company_name, c.category, fs.score_date, fs.friction_score,
       fs.data_completeness_pct, fs.reliability_source,
       fs.perf_contribution, fs.cwv_contribution, fs.quality_contribution,
       fs.reliability_contribution, fs.errors_contribution
FROM friction_scores fs
JOIN companies c ON c.company_id = fs.company_id
WHERE c.is_active AND fs.data_completeness_pct >= 60;

CREATE OR REPLACE VIEW v_reliable_components AS
SELECT company_id, company_name, score_date, component, points FROM (
    SELECT company_id, company_name, score_date, 'Performance' AS component, perf_contribution AS points FROM v_reliable_scores
    UNION ALL SELECT company_id, company_name, score_date, 'Core Web Vitals', cwv_contribution FROM v_reliable_scores
    UNION ALL SELECT company_id, company_name, score_date, 'Site Quality', quality_contribution FROM v_reliable_scores
    UNION ALL SELECT company_id, company_name, score_date, 'Reliability', reliability_contribution FROM v_reliable_scores
    UNION ALL SELECT company_id, company_name, score_date, 'Broken Links', errors_contribution FROM v_reliable_scores
) u
WHERE points IS NOT NULL;

-- Is a component's movement sustained or a one-reading blip?
-- Baseline = mean of the component over the 7 days BEFORE the previous reading, so that
-- both the latest and the previous reading can be judged against the same yardstick.
--   Sustained worsening : latest AND previous reading both >= 1 pt above baseline
--   New one-reading jump: latest >= 1 pt above baseline, previous reading was not
--   Improving           : latest >= 1 pt below baseline
--   Stable              : otherwise
CREATE OR REPLACE VIEW v_component_status AS
WITH ranked AS (
    SELECT rc.*, ROW_NUMBER() OVER (PARTITION BY company_id, component ORDER BY score_date DESC) AS rn
    FROM v_reliable_components rc
),
latest AS (SELECT * FROM ranked WHERE rn = 1),
prev AS (SELECT * FROM ranked WHERE rn = 2),
base AS (
    SELECT r.company_id, r.component, AVG(r.points)::float8 AS baseline_points, COUNT(*) AS baseline_readings
    FROM ranked r
    JOIN prev p ON p.company_id = r.company_id AND p.component = r.component
    WHERE r.score_date < p.score_date AND r.score_date >= p.score_date - 7
    GROUP BY r.company_id, r.component
)
SELECT
    l.company_id, l.company_name, l.component,
    l.score_date AS latest_date,
    l.points AS latest_points,
    p.points AS previous_points,
    l.points - p.points AS change_vs_previous,
    b.baseline_points,
    b.baseline_readings,
    l.points - b.baseline_points AS change_vs_baseline,
    CASE
        WHEN p.points IS NULL OR b.baseline_points IS NULL THEN 'Insufficient history'
        WHEN l.points - b.baseline_points >= 1 AND p.points - b.baseline_points >= 1 THEN 'Sustained worsening'
        WHEN l.points - b.baseline_points >= 1 THEN 'New one-reading jump'
        WHEN l.points - b.baseline_points <= -1 THEN 'Improving'
        ELSE 'Stable'
    END AS pattern,
    CASE
        WHEN p.points IS NULL OR b.baseline_points IS NULL THEN 5
        WHEN l.points - b.baseline_points >= 1 AND p.points - b.baseline_points >= 1 THEN 1
        WHEN l.points - b.baseline_points >= 1 THEN 2
        WHEN l.points - b.baseline_points <= -1 THEN 4
        ELSE 3
    END AS pattern_order
FROM latest l
LEFT JOIN prev p ON p.company_id = l.company_id AND p.component = l.component
LEFT JOIN base b ON b.company_id = l.company_id AND b.component = l.component;

CREATE OR REPLACE VIEW v_management_priority AS
WITH params AS (
    SELECT 0.35 AS w_severity, 0.25 AS w_gap, 0.25 AS w_deterioration, 0.15 AS w_persistence,
           5.0 AS gap_scale, 10.0 AS deterioration_scale,
           1.0 AS worsening_threshold_pts,   -- quadrant: "worsening" = >= 1 pt above own 7-day baseline
           45 AS high_priority_min, 25 AS medium_priority_min
),
latest AS (
    SELECT DISTINCT ON (company_id) * FROM v_reliable_scores ORDER BY company_id, score_date DESC
),
industry_day AS (
    SELECT score_date, AVG(friction_score) AS industry_avg FROM v_reliable_scores GROUP BY score_date
),
industry_component AS (
    SELECT score_date, component, AVG(points) AS industry_points FROM v_reliable_components GROUP BY score_date, component
),
prev AS (
    SELECT DISTINCT ON (s.company_id) s.company_id, s.friction_score AS previous_score
    FROM v_reliable_scores s JOIN latest l ON l.company_id = s.company_id
    WHERE s.score_date < l.score_date
    ORDER BY s.company_id, s.score_date DESC
),
baseline AS (
    SELECT s.company_id, AVG(s.friction_score) AS baseline_score, COUNT(*) AS baseline_readings
    FROM v_reliable_scores s JOIN latest l ON l.company_id = s.company_id
    WHERE s.score_date < l.score_date AND s.score_date >= l.score_date - 7
    GROUP BY s.company_id
),
persistence AS (
    SELECT s.company_id, COUNT(*) AS readings_14d,
           COUNT(*) FILTER (WHERE s.friction_score > i.industry_avg) AS above_avg_14d
    FROM v_reliable_scores s
    JOIN latest l ON l.company_id = s.company_id
    JOIN industry_day i ON i.score_date = s.score_date
    WHERE s.score_date > l.score_date - 14
    GROUP BY s.company_id
),
history AS (
    SELECT s.company_id, COUNT(*) AS readings_21d
    FROM v_reliable_scores s JOIN latest l ON l.company_id = s.company_id
    WHERE s.score_date > l.score_date - 21
    GROUP BY s.company_id
),
driver_candidates AS (
    -- Driver = biggest improvement opportunity, chosen in this order (gaps/changes count from 1 pt):
    --   1 'above industry'    : component furthest above the industry average
    --   2 'worsening'         : else the component that rose most vs its own baseline
    --   3 'largest component' : else the company's largest component
    SELECT rc.company_id, rc.component, rc.points, ic.industry_points,
           rc.points - ic.industry_points AS gap, cs.change_vs_baseline,
           CASE WHEN rc.points - ic.industry_points >= 1 THEN 1
                WHEN cs.change_vs_baseline >= 1 THEN 2 ELSE 3 END AS basis_rank
    FROM v_reliable_components rc
    JOIN latest l ON l.company_id = rc.company_id AND l.score_date = rc.score_date
    JOIN industry_component ic ON ic.score_date = rc.score_date AND ic.component = rc.component
    LEFT JOIN v_component_status cs ON cs.company_id = rc.company_id AND cs.component = rc.component
),
driver AS (
    SELECT DISTINCT ON (company_id)
        company_id, component AS driver_component, points AS driver_points,
        industry_points AS driver_industry_points, gap AS driver_gap,
        CASE basis_rank WHEN 1 THEN 'above industry' WHEN 2 THEN 'worsening' ELSE 'largest component' END AS driver_basis
    FROM driver_candidates
    ORDER BY company_id, basis_rank,
             CASE basis_rank WHEN 1 THEN gap WHEN 2 THEN change_vs_baseline ELSE points END DESC
),
signals AS (
    SELECT
        l.company_id, l.company_name, l.category, l.score_date AS latest_date,
        l.friction_score, i.industry_avg,
        l.friction_score - i.industry_avg AS gap_vs_industry,
        l.friction_score - p.previous_score AS change_vs_previous,
        l.friction_score - b.baseline_score AS change_vs_baseline,
        b.baseline_readings, pe.readings_14d, pe.above_avg_14d, h.readings_21d,
        l.data_completeness_pct, l.reliability_source,
        d.driver_component, d.driver_points, d.driver_industry_points, d.driver_gap, d.driver_basis,
        cs.pattern AS driver_pattern,
        LEAST(100, l.friction_score) AS severity_pts,
        LEAST(100, GREATEST(0, l.friction_score - i.industry_avg) * pr.gap_scale) AS gap_pts,
        LEAST(100, GREATEST(0, COALESCE(l.friction_score - b.baseline_score, 0)) * pr.deterioration_scale) AS deterioration_pts,
        100.0 * pe.above_avg_14d / pe.readings_14d AS persistence_pts,
        0.5 + 0.5 * (l.data_completeness_pct / 100.0) * LEAST(1.0, h.readings_21d / 7.0) AS confidence_factor,
        pr.*
    FROM latest l
    CROSS JOIN params pr
    JOIN industry_day i ON i.score_date = l.score_date
    LEFT JOIN prev p ON p.company_id = l.company_id
    LEFT JOIN baseline b ON b.company_id = l.company_id
    JOIN persistence pe ON pe.company_id = l.company_id
    JOIN history h ON h.company_id = l.company_id
    LEFT JOIN driver d ON d.company_id = l.company_id
    LEFT JOIN v_component_status cs ON cs.company_id = l.company_id AND cs.component = d.driver_component
),
scored AS (
    SELECT s.*,
        confidence_factor * w_severity * severity_pts AS severity_contribution,
        confidence_factor * w_gap * gap_pts AS gap_contribution,
        confidence_factor * w_deterioration * deterioration_pts AS deterioration_contribution,
        confidence_factor * w_persistence * persistence_pts AS persistence_contribution,
        confidence_factor * (w_severity * severity_pts + w_gap * gap_pts
                             + w_deterioration * deterioration_pts + w_persistence * persistence_pts) AS priority_score,
        gap_vs_industry > 0 AS above_industry_avg,
        COALESCE(change_vs_baseline, 0) >= worsening_threshold_pts AS worsening
    FROM signals s
)
-- Computed numerics are cast to double precision: Postgres NUMERIC division keeps up to
-- ~65 significant digits, which Power BI's PostgreSQL driver (max 28) refuses to load.
SELECT
    company_id, company_name, category, latest_date,
    friction_score::float8, industry_avg::float8, gap_vs_industry::float8,
    change_vs_previous::float8, change_vs_baseline::float8,
    baseline_readings, readings_14d, above_avg_14d, readings_21d,
    data_completeness_pct::float8, reliability_source,
    driver_component, driver_points::float8, driver_industry_points::float8, driver_gap::float8,
    driver_basis, driver_pattern,
    severity_pts::float8, gap_pts::float8, deterioration_pts::float8, persistence_pts::float8,
    confidence_factor::float8,
    severity_contribution::float8, gap_contribution::float8, deterioration_contribution::float8,
    persistence_contribution::float8,
    priority_score::float8,
    RANK() OVER (ORDER BY priority_score DESC) AS priority_rank,
    CASE WHEN priority_score >= high_priority_min THEN 'High'
         WHEN priority_score >= medium_priority_min THEN 'Medium' ELSE 'Low' END AS priority_tier,
    CASE WHEN above_industry_avg AND worsening THEN 'Critical'
         WHEN above_industry_avg THEN 'Lagging'
         WHEN worsening THEN 'At Risk'
         ELSE 'Healthy' END AS priority_quadrant,
    CASE WHEN data_completeness_pct >= 100 AND readings_21d >= 7 THEN 'High'
         WHEN data_completeness_pct >= 80 AND readings_21d >= 3 THEN 'Medium' ELSE 'Low' END AS data_confidence,
    CASE WHEN data_completeness_pct >= 100 AND readings_21d >= 7 THEN 'High'
         WHEN data_completeness_pct >= 80 AND readings_21d >= 3 THEN 'Medium' ELSE 'Low' END
        || ': ' || round(data_completeness_pct)::text || '% of components measured, '
        || readings_21d::text || ' readings in 21 days'
        || CASE WHEN reliability_source = 'pagespeed' THEN ', reliability via PageSpeed (site blocks direct checks)' ELSE '' END
        AS data_confidence_note,
    driver_component
        || CASE driver_basis WHEN 'above industry' THEN ' worse than industry'
                             WHEN 'worsening' THEN ' worsening vs own baseline'
                             ELSE ' (largest component; none above industry)' END
        || CASE WHEN driver_pattern IN ('Sustained worsening', 'New one-reading jump', 'Improving')
                THEN ' - ' || lower(driver_pattern) ELSE '' END AS issue,
    'Score ' || round(friction_score, 1)::text || ' (' || fmt_signed(gap_vs_industry) || ' vs industry avg '
        || round(industry_avg, 1)::text || '); ' || driver_component || ' ' || round(driver_points, 1)::text
        || ' pts vs industry ' || round(driver_industry_points, 1)::text || ' (' || fmt_signed(driver_gap) || '); '
        || fmt_signed(change_vs_baseline) || ' vs own 7-day baseline; above industry avg on '
        || above_avg_14d::text || ' of ' || readings_14d::text || ' readings (14 days)' AS evidence,
    CASE driver_component
        WHEN 'Performance' THEN 'Review the Lighthouse performance audit: page weight, render-blocking scripts, third-party tags and server response.'
        WHEN 'Core Web Vitals' THEN 'Investigate mobile Core Web Vitals (LCP, CLS, INP): hero-image loading, layout shifts and main-thread work.'
        WHEN 'Site Quality' THEN 'Review failing Lighthouse accessibility, best-practice and SEO audits.'
        WHEN 'Reliability' THEN 'Assess availability and response-time incidents; review uptime monitoring and CDN/WAF behaviour.'
        WHEN 'Broken Links' THEN 'Review the broken homepage links listed in the latest link check.'
    END
        || CASE WHEN data_completeness_pct < 100 OR readings_21d < 7
                THEN ' Confirm with more readings before acting (limited data).' ELSE '' END AS recommended_investigation,
    company_name || ' (' || round(priority_score)::text || ')' AS company_label
FROM scored;

-- Improvement opportunity: each component's points vs the industry average on the
-- company's latest reliable date (positive part only = points above industry).
CREATE OR REPLACE VIEW v_component_gap AS
WITH latest AS (
    SELECT company_id, MAX(score_date) AS latest_date FROM v_reliable_scores GROUP BY company_id
),
industry_component AS (
    SELECT score_date, component, AVG(points)::float8 AS industry_points FROM v_reliable_components GROUP BY score_date, component
)
SELECT rc.company_id, rc.company_name, rc.component, rc.score_date AS latest_date,
       rc.points, ic.industry_points,
       rc.points - ic.industry_points AS gap_vs_industry,
       GREATEST(rc.points - ic.industry_points, 0) AS points_above_industry
FROM v_reliable_components rc
JOIN latest l ON l.company_id = rc.company_id AND l.latest_date = rc.score_date
JOIN industry_component ic ON ic.score_date = rc.score_date AND ic.component = rc.component;

-- Per-company anomaly-detection readiness; mirrors collectors/anomaly_detection.py
-- (>= 7 reliable scored days in the 21 days before the latest date).
CREATE OR REPLACE VIEW v_anomaly_readiness AS
WITH ref AS (SELECT MAX(score_date) AS latest FROM v_reliable_scores)
SELECT c.company_id, c.company_name,
       COUNT(s.score_date) FILTER (WHERE s.score_date < ref.latest AND s.score_date >= ref.latest - 21) AS baseline_days,
       COUNT(s.score_date) FILTER (WHERE s.score_date < ref.latest AND s.score_date >= ref.latest - 21) >= 7 AS monitored
FROM companies c
CROSS JOIN ref
LEFT JOIN v_reliable_scores s ON s.company_id = c.company_id
WHERE c.is_active
GROUP BY c.company_id, c.company_name;
