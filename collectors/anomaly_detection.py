"""Robust rolling z-score anomaly detection over friction_scores history.

Baseline = the company's own reliable scores from the previous
BASELINE_WINDOW_DAYS calendar days (today excluded), summarized by median and
MAD (robust to the few points a young project has). Detection needs at least
MIN_BASELINE_DAYS scored days in that window; until then it writes an explicit
'insufficient_history' row so the dashboard can say so instead of guessing.

Two guards keep near-constant metrics (Reliability and Broken Links are 0 on
most days) from producing huge z-scores out of tiny moves:
  * MAD is floored at MAD_FLOOR_POINTS, and
  * a change must also be at least MIN_CHANGE_POINTS from the median.
"""
from datetime import date, datetime, timedelta

import pandas as pd
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

METRICS = [
    "friction_score", "perf_contribution", "cwv_contribution",
    "quality_contribution", "reliability_contribution", "errors_contribution",
    "uptime_pct",
]

BASELINE_WINDOW_DAYS = 21
MIN_BASELINE_DAYS = 7
RELIABLE_COMPLETENESS_PCT = 60  # same cut-off as is_score_reliable in the SQL views
MEDIUM_Z = 3.5
HIGH_Z = 5.0
# Metrics are on a 0-100 point scale; a 1-point spread floor and a 2-point
# minimum move keep noise and near-flat metrics from being flagged.
MAD_FLOOR_POINTS = 1.0
MIN_CHANGE_POINTS = 2.0


def _load_history(conn, company_id: int) -> pd.DataFrame:
    columns = ["score_date", *METRICS]
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(columns)} FROM friction_scores "
            "WHERE company_id = %s AND data_completeness_pct >= %s ORDER BY score_date",
            (company_id, RELIABLE_COMPLETENESS_PCT),
        )
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=columns)
    df[METRICS] = df[METRICS].astype(float)
    return df


def _evaluate_metric(series: pd.Series, as_of_date) -> dict | None:
    """series: metric values indexed by score_date (reliable days only), ascending."""
    if as_of_date not in series.index:
        return None
    today_value = series.loc[as_of_date]
    if pd.isna(today_value):
        return None

    window_start = as_of_date - timedelta(days=BASELINE_WINDOW_DAYS)
    baseline = series[(series.index >= window_start) & (series.index < as_of_date)].dropna()
    if len(baseline) < MIN_BASELINE_DAYS:
        return {"status": "insufficient_history", "value": float(today_value), "baseline_days": len(baseline)}

    median = baseline.median()
    mad = (baseline - median).abs().median()
    z = 0.6745 * (today_value - median) / max(mad, MAD_FLOOR_POINTS)

    if abs(z) < MEDIUM_Z or abs(today_value - median) < MIN_CHANGE_POINTS:
        return {"status": "normal"}

    return {
        "status": "new", "value": float(today_value), "median": float(median), "mad": float(mad),
        "z_score": float(z), "baseline_days": len(baseline),
        "severity": "high" if abs(z) >= HIGH_Z else "medium",
        "anomaly_type": "spike" if today_value > median else "drop",
    }


def detect_for_company(conn, company_id: int, as_of_date: date = None):
    as_of_date = as_of_date or datetime.now(IST).date()
    df = _load_history(conn, company_id)
    if df.empty:
        return []

    df = df.set_index("score_date")
    findings = []

    with conn.cursor() as cur:
        for metric in METRICS:
            result = _evaluate_metric(df[metric], as_of_date)
            if result is None:
                continue
            if result["status"] == "normal":
                # Re-runs on the same day must clear an earlier flag or 'insufficient' row.
                cur.execute(
                    "DELETE FROM anomalies WHERE company_id = %s AND metric_name = %s AND detected_date = %s",
                    (company_id, metric, as_of_date),
                )
            elif result["status"] == "insufficient_history":
                cur.execute(
                    """
                    INSERT INTO anomalies (company_id, metric_name, detected_date, metric_value, baseline_days, status)
                    VALUES (%s, %s, %s, %s, %s, 'insufficient_history')
                    ON CONFLICT (company_id, metric_name, detected_date) DO UPDATE SET
                        metric_value = EXCLUDED.metric_value, baseline_days = EXCLUDED.baseline_days,
                        rolling_median = NULL, rolling_mad = NULL, z_score = NULL,
                        anomaly_type = NULL, severity = NULL, status = 'insufficient_history'
                    """,
                    (company_id, metric, as_of_date, result["value"], result["baseline_days"]),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO anomalies (
                        company_id, metric_name, detected_date, metric_value, rolling_median,
                        rolling_mad, z_score, baseline_days, anomaly_type, severity, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'new')
                    ON CONFLICT (company_id, metric_name, detected_date) DO UPDATE SET
                        metric_value = EXCLUDED.metric_value, rolling_median = EXCLUDED.rolling_median,
                        rolling_mad = EXCLUDED.rolling_mad, z_score = EXCLUDED.z_score,
                        baseline_days = EXCLUDED.baseline_days, anomaly_type = EXCLUDED.anomaly_type,
                        severity = EXCLUDED.severity, status = 'new'
                    """,
                    (
                        company_id, metric, as_of_date, result["value"], result["median"], result["mad"],
                        result["z_score"], result["baseline_days"], result["anomaly_type"], result["severity"],
                    ),
                )
                findings.append({"metric": metric, **result})
    conn.commit()
    return findings


def run_for_all_companies(conn, company_ids: list[int], as_of_date: date = None):
    all_findings = {}
    for company_id in company_ids:
        all_findings[company_id] = detect_for_company(conn, company_id, as_of_date)
    return all_findings
