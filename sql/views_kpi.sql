-- KPI / benchmarking views consumed directly by Power BI.

-- Latest score per company, with prior-day and prior-week deltas, for the
-- Executive Overview page.
CREATE OR REPLACE VIEW v_latest_friction_score AS
WITH latest AS (
    SELECT DISTINCT ON (company_id)
        company_id, score_date, friction_score, data_completeness_pct, reliability_source
    FROM friction_scores
    ORDER BY company_id, score_date DESC
)
SELECT
    l.company_id,
    c.company_name,
    c.category,
    l.score_date AS latest_date,
    l.friction_score,
    l.friction_score - prior_day.friction_score AS delta_vs_prior_day,
    l.friction_score - prior_week.friction_score AS delta_vs_prior_week,
    l.data_completeness_pct,
    l.reliability_source,
    -- Scores built from fewer than 3 of 5 components aren't comparable to full ones.
    l.data_completeness_pct >= 60 AS is_score_reliable
FROM latest l
JOIN companies c ON c.company_id = l.company_id AND c.is_active
LEFT JOIN friction_scores prior_day
    ON prior_day.company_id = l.company_id
   AND prior_day.score_date = l.score_date - INTERVAL '1 day'
LEFT JOIN friction_scores prior_week
    ON prior_week.company_id = l.company_id
   AND prior_week.score_date = l.score_date - INTERVAL '7 day';

-- Company-by-company benchmark against the industry (all active companies) average,
-- for the Competitor Benchmarking page.
CREATE OR REPLACE VIEW v_competitor_benchmark AS
SELECT
    fs.company_id,
    c.company_name,
    c.category,
    fs.score_date,
    fs.friction_score,
    AVG(fs.friction_score) OVER (PARTITION BY fs.score_date) AS industry_avg_friction_score,
    fs.friction_score - AVG(fs.friction_score) OVER (PARTITION BY fs.score_date) AS vs_industry_avg,
    RANK() OVER (PARTITION BY fs.score_date ORDER BY fs.friction_score ASC) AS rank_best_to_worst,
    fs.perf_contribution, fs.cwv_contribution, fs.quality_contribution,
    fs.reliability_contribution, fs.errors_contribution,
    fs.uptime_pct, fs.broken_link_pct, fs.avg_response_ms,
    fs.reliability_source, fs.data_completeness_pct,
    fs.data_completeness_pct >= 60 AS is_score_reliable
FROM friction_scores fs
JOIN companies c ON c.company_id = fs.company_id
WHERE c.is_active;

-- Daily trend series for the Trends & Anomalies page.
CREATE OR REPLACE VIEW v_friction_trend AS
SELECT
    fs.company_id,
    c.company_name,
    c.category,
    fs.score_date,
    fs.friction_score,
    fs.perf_contribution, fs.cwv_contribution, fs.quality_contribution,
    fs.reliability_contribution, fs.errors_contribution,
    fs.data_completeness_pct,
    fs.reliability_source,
    fs.data_completeness_pct >= 60 AS is_score_reliable
FROM friction_scores fs
JOIN companies c ON c.company_id = fs.company_id AND c.is_active;
