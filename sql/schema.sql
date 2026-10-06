-- Core schema: reference data, collection runs, raw measurements and derived scores.
-- Applied by scripts/setup_db.py. Safe to re-run (everything is IF NOT EXISTS).

-- Companies being benchmarked. Removing a company from config/companies.yaml
-- marks it inactive; its history is kept.
CREATE TABLE IF NOT EXISTS companies (
    company_id    SMALLSERIAL PRIMARY KEY,
    company_name  TEXT        NOT NULL,
    domain        TEXT        NOT NULL UNIQUE,
    homepage_url  TEXT        NOT NULL,
    category      TEXT,
    is_active     BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per pipeline run.
CREATE TABLE IF NOT EXISTS collection_runs (
    run_id               BIGSERIAL PRIMARY KEY,
    run_started_at       TIMESTAMPTZ NOT NULL,
    run_finished_at      TIMESTAMPTZ,
    trigger_source       TEXT        NOT NULL DEFAULT 'task_scheduler',
    status               TEXT        NOT NULL DEFAULT 'running',   -- running / success / partial / failed / aborted
    companies_attempted  SMALLINT,
    companies_succeeded  SMALLINT,
    companies_failed     SMALLINT,
    notes                TEXT
);

-- Outcome of each data source, per company, per run.
CREATE TABLE IF NOT EXISTS company_run_results (
    run_id            BIGINT   NOT NULL REFERENCES collection_runs (run_id),
    company_id        SMALLINT NOT NULL REFERENCES companies (company_id),
    pagespeed_status  TEXT,
    uptime_status     TEXT,
    linkcheck_status  TEXT,
    error_message     TEXT,
    PRIMARY KEY (run_id, company_id)
);

-- Google PageSpeed Insights results (Lighthouse lab test + Chrome UX Report real-user data in raw_json).
CREATE TABLE IF NOT EXISTS pagespeed_raw (
    reading_id            BIGSERIAL PRIMARY KEY,
    run_id                BIGINT      NOT NULL REFERENCES collection_runs (run_id),
    company_id            SMALLINT    NOT NULL REFERENCES companies (company_id),
    strategy              TEXT        NOT NULL CHECK (strategy IN ('mobile', 'desktop')),
    fetched_at            TIMESTAMPTZ NOT NULL,
    performance_score     NUMERIC(5,2),
    accessibility_score   NUMERIC(5,2),
    best_practices_score  NUMERIC(5,2),
    seo_score             NUMERIC(5,2),
    lcp_ms                NUMERIC,
    cls                   NUMERIC,
    inp_ms                NUMERIC,    -- lab responsiveness proxy (max potential FID); lab tests cannot measure true INP
    ttfb_ms               NUMERIC,
    fetch_status          TEXT        NOT NULL,   -- success / page_error / blocked / error
    error_message         TEXT,
    raw_json              JSONB
);
CREATE INDEX IF NOT EXISTS ix_pagespeed_company_time ON pagespeed_raw (company_id, fetched_at);

-- Direct homepage availability and response-time checks.
CREATE TABLE IF NOT EXISTS uptime_checks (
    check_id          BIGSERIAL PRIMARY KEY,
    run_id            BIGINT      NOT NULL REFERENCES collection_runs (run_id),
    company_id        SMALLINT    NOT NULL REFERENCES companies (company_id),
    checked_at        TIMESTAMPTZ NOT NULL,
    status_code       INT,
    response_time_ms  INT,
    outcome           TEXT        NOT NULL CHECK (outcome IN ('up', 'down', 'blocked', 'timeout')),
    final_url         TEXT,
    error_message     TEXT
);
CREATE INDEX IF NOT EXISTS ix_uptime_company_time ON uptime_checks (company_id, checked_at);

-- Homepage broken-link scans (aggregates plus a small sample of broken URLs).
CREATE TABLE IF NOT EXISTS link_check_runs (
    link_run_id         BIGSERIAL PRIMARY KEY,
    run_id              BIGINT      NOT NULL REFERENCES collection_runs (run_id),
    company_id          SMALLINT    NOT NULL REFERENCES companies (company_id),
    checked_at          TIMESTAMPTZ NOT NULL,
    links_found         INT,
    links_checked       INT,
    links_broken        INT,
    links_unchecked     INT,
    broken_pct          NUMERIC(5,2),
    collection_status   TEXT,       -- ok / partial / blocked / no_links / error
    broken_urls_sample  JSONB
);
CREATE INDEX IF NOT EXISTS ix_linkcheck_company_time ON link_check_runs (company_id, checked_at);

-- Daily Digital Friction Score per company. Each component is stored as its weighted
-- contribution, so the five contributions add up exactly to friction_score.
CREATE TABLE IF NOT EXISTS friction_scores (
    company_id                SMALLINT    NOT NULL REFERENCES companies (company_id),
    score_date                DATE        NOT NULL,   -- India business day (Asia/Kolkata)
    perf_score_mobile         NUMERIC(5,2),
    perf_score_desktop        NUMERIC(5,2),
    quality_score             NUMERIC(5,2),
    cwv_badness               NUMERIC(5,2),
    uptime_pct                NUMERIC(5,2),
    avg_response_ms           NUMERIC,
    broken_link_pct           NUMERIC(5,2),
    perf_contribution         NUMERIC(6,3),
    cwv_contribution          NUMERIC(6,3),
    quality_contribution      NUMERIC(6,3),
    reliability_contribution  NUMERIC(6,3),
    errors_contribution       NUMERIC(6,3),
    friction_score            NUMERIC(6,3) NOT NULL,
    runs_aggregated           SMALLINT,
    data_completeness_pct     NUMERIC(5,2),
    reliability_source        TEXT,       -- 'direct' check, or 'pagespeed' when the site blocks direct checks
    computed_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, score_date)
);
CREATE INDEX IF NOT EXISTS ix_friction_scores_date ON friction_scores (score_date);

-- Anomaly detection results (and 'insufficient_history' markers while a company warms up).
CREATE TABLE IF NOT EXISTS anomalies (
    anomaly_id      BIGSERIAL PRIMARY KEY,
    company_id      SMALLINT    NOT NULL REFERENCES companies (company_id),
    metric_name     TEXT        NOT NULL,
    detected_date   DATE        NOT NULL,
    metric_value    NUMERIC,
    rolling_median  NUMERIC,
    rolling_mad     NUMERIC,
    z_score         NUMERIC,
    baseline_days   SMALLINT,   -- reliable scored days in the 21-day baseline window
    anomaly_type    TEXT,       -- spike / drop
    severity        TEXT,       -- medium / high
    status          TEXT        NOT NULL DEFAULT 'new',
    detected_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company_id, metric_name, detected_date)
);
CREATE INDEX IF NOT EXISTS ix_anomalies_company_date ON anomalies (company_id, detected_date);
