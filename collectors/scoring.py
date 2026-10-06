"""Daily Digital Friction Score computation.

Aggregates today's raw readings (there may be several collection runs per
day) into one row per company per day in friction_scores, upserted so
re-running mid-day updates today's row rather than duplicating it.

Score is a linear weighted sum of five 0-100 "friction" components (higher =
worse), so it is exactly decomposable into per-component contributions.
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

WEIGHTS = {
    "perf": 0.25,
    "cwv": 0.20,
    "quality": 0.15,
    "reliability": 0.25,
    "errors": 0.15,
}

# Google's documented good/poor thresholds for Core Web Vitals.
CWV_THRESHOLDS = {
    "lcp_ms": (2500, 4000),
    "cls": (0.1, 0.25),
    "inp_ms": (200, 500),
}

MOBILE_WEIGHT = 0.6
DESKTOP_WEIGHT = 0.4

# Response-time penalty starts above these. Direct checks time the full HTML
# download; the PageSpeed fallback only has server response time (TTFB), so
# it uses Lighthouse's own 600ms "server response time" pass threshold.
DIRECT_RESPONSE_PENALTY_START_MS = 1000
PSI_TTFB_PENALTY_START_MS = 600
RESPONSE_PENALTY_CAP = 30.0


def _badness(value, good, poor):
    if value is None:
        return None
    if value <= good:
        return 0.0
    if value >= poor:
        return 100.0
    return 100.0 * (value - good) / (poor - good)


def _blend(mobile, desktop):
    if mobile is None and desktop is None:
        return None
    if mobile is None:
        return desktop
    if desktop is None:
        return mobile
    return MOBILE_WEIGHT * mobile + DESKTOP_WEIGHT * desktop


def _day_bounds_utc(score_date: date):
    start_local = datetime(score_date.year, score_date.month, score_date.day, tzinfo=IST)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(ZoneInfo("UTC")), end_local.astimezone(ZoneInfo("UTC"))


def _fetch_pagespeed_agg(cur, company_id, start_utc, end_utc):
    cur.execute(
        """
        SELECT strategy,
               AVG(performance_score), AVG(accessibility_score),
               AVG(best_practices_score), AVG(seo_score),
               AVG(lcp_ms), AVG(cls), AVG(inp_ms)
        FROM pagespeed_raw
        WHERE company_id = %s AND fetch_status = 'success'
          AND fetched_at >= %s AND fetched_at < %s
        GROUP BY strategy
        """,
        (company_id, start_utc, end_utc),
    )
    rows = {r[0]: r[1:] for r in cur.fetchall()}
    mobile = rows.get("mobile")
    desktop = rows.get("desktop")

    def col(row, idx):
        return float(row[idx]) if row and row[idx] is not None else None

    perf_mobile = col(mobile, 0)
    perf_desktop = col(desktop, 0)

    quality_mobile = None
    quality_desktop = None
    for row, target in ((mobile, "mobile"), (desktop, "desktop")):
        if row and all(row[i] is not None for i in (1, 2, 3)):
            avg_q = (float(row[1]) + float(row[2]) + float(row[3])) / 3
            if target == "mobile":
                quality_mobile = avg_q
            else:
                quality_desktop = avg_q

    lcp = _blend(col(mobile, 4), col(desktop, 4))
    cls = _blend(col(mobile, 5), col(desktop, 5))
    inp = _blend(col(mobile, 6), col(desktop, 6))

    return perf_mobile, perf_desktop, quality_mobile, quality_desktop, lcp, cls, inp


def _fetch_uptime_agg(cur, company_id, start_utc, end_utc):
    cur.execute(
        """
        SELECT outcome, response_time_ms
        FROM uptime_checks
        WHERE company_id = %s AND checked_at >= %s AND checked_at < %s
        """,
        (company_id, start_utc, end_utc),
    )
    rows = cur.fetchall()
    countable = [r for r in rows if r[0] in ("up", "down", "timeout")]
    if not countable:
        return None, None
    up_count = sum(1 for r in countable if r[0] == "up")
    uptime_pct = 100.0 * up_count / len(countable)
    response_times = [r[1] for r in rows if r[0] == "up" and r[1] is not None]
    avg_response_ms = sum(response_times) / len(response_times) if response_times else None
    return uptime_pct, avg_response_ms


def _fetch_psi_reliability(cur, company_id, start_utc, end_utc):
    """Fallback reliability for sites that block direct checks: did the page
    load when Google's Lighthouse fetched it, and how fast did the server respond."""
    cur.execute(
        """
        SELECT COUNT(*) FILTER (WHERE fetch_status = 'success'),
               COUNT(*) FILTER (WHERE fetch_status = 'page_error'),
               AVG(ttfb_ms) FILTER (WHERE fetch_status = 'success')
        FROM pagespeed_raw
        WHERE company_id = %s AND fetched_at >= %s AND fetched_at < %s
        """,
        (company_id, start_utc, end_utc),
    )
    loaded, failed, avg_ttfb = cur.fetchone()
    if loaded + failed == 0:
        return None, None
    return 100.0 * loaded / (loaded + failed), float(avg_ttfb) if avg_ttfb is not None else None


def _fetch_linkcheck_agg(cur, company_id, start_utc, end_utc):
    cur.execute(
        """
        SELECT broken_pct
        FROM link_check_runs
        WHERE company_id = %s AND checked_at >= %s AND checked_at < %s
          AND broken_pct IS NOT NULL
        """,
        (company_id, start_utc, end_utc),
    )
    values = [r[0] for r in cur.fetchall()]
    if not values:
        return None
    return float(sum(values)) / len(values)


def _count_runs(cur, company_id, start_utc, end_utc):
    cur.execute(
        """
        SELECT COUNT(DISTINCT run_id) FROM uptime_checks
        WHERE company_id = %s AND checked_at >= %s AND checked_at < %s
        """,
        (company_id, start_utc, end_utc),
    )
    return cur.fetchone()[0]


def compute_and_store(conn, company_id: int, score_date: date = None):
    score_date = score_date or datetime.now(IST).date()
    start_utc, end_utc = _day_bounds_utc(score_date)

    with conn.cursor() as cur:
        perf_mobile, perf_desktop, quality_mobile, quality_desktop, lcp, cls, inp = _fetch_pagespeed_agg(
            cur, company_id, start_utc, end_utc
        )
        uptime_pct, avg_response_ms = _fetch_uptime_agg(cur, company_id, start_utc, end_utc)
        reliability_source = "direct" if uptime_pct is not None else None
        penalty_start_ms = DIRECT_RESPONSE_PENALTY_START_MS
        if uptime_pct is None:
            uptime_pct, avg_response_ms = _fetch_psi_reliability(cur, company_id, start_utc, end_utc)
            if uptime_pct is not None:
                reliability_source = "pagespeed"
                penalty_start_ms = PSI_TTFB_PENALTY_START_MS
        broken_link_pct = _fetch_linkcheck_agg(cur, company_id, start_utc, end_utc)
        runs_aggregated = _count_runs(cur, company_id, start_utc, end_utc)

    perf_score = _blend(perf_mobile, perf_desktop)
    quality_score = _blend(quality_mobile, quality_desktop)

    lcp_bad = _badness(lcp, *CWV_THRESHOLDS["lcp_ms"])
    cls_bad = _badness(cls, *CWV_THRESHOLDS["cls"])
    inp_bad = _badness(inp, *CWV_THRESHOLDS["inp_ms"])
    cwv_components = [b for b in (lcp_bad, cls_bad, inp_bad) if b is not None]
    cwv_badness = sum(cwv_components) / len(cwv_components) if cwv_components else None

    reliability_friction = None
    if uptime_pct is not None:
        response_penalty = 0.0
        if avg_response_ms is not None:
            response_penalty = min(RESPONSE_PENALTY_CAP, max(0.0, (avg_response_ms - penalty_start_ms) / 50))
        reliability_friction = min(100.0, max(0.0, (100.0 - uptime_pct) + response_penalty))

    errors_friction = None
    if broken_link_pct is not None:
        errors_friction = min(100.0, broken_link_pct * 2)

    component_friction = {
        "perf": 100 - perf_score if perf_score is not None else None,
        "cwv": cwv_badness,
        "quality": 100 - quality_score if quality_score is not None else None,
        "reliability": reliability_friction,
        "errors": errors_friction,
    }
    present = {k: v for k, v in component_friction.items() if v is not None}
    if not present:
        return None, 0.0

    # Missing components are dropped and the remaining weights rescaled to sum
    # to 1, so a company whose site blocks a check isn't rewarded with 0 friction
    # for that component. Contributions are stored post-rescale, so they still
    # sum exactly to friction_score.
    weight_scale = 1.0 / sum(WEIGHTS[k] for k in present)
    contrib = {k: WEIGHTS[k] * weight_scale * v for k, v in present.items()}
    perf_contribution = contrib.get("perf")
    cwv_contribution = contrib.get("cwv")
    quality_contribution = contrib.get("quality")
    reliability_contribution = contrib.get("reliability")
    errors_contribution = contrib.get("errors")

    friction_score = sum(contrib.values())
    data_completeness_pct = round(100.0 * len(present) / len(WEIGHTS), 2)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO friction_scores (
                company_id, score_date, perf_score_mobile, perf_score_desktop,
                quality_score, cwv_badness, uptime_pct, avg_response_ms, broken_link_pct,
                perf_contribution, cwv_contribution, quality_contribution,
                reliability_contribution, errors_contribution, friction_score,
                runs_aggregated, data_completeness_pct, reliability_source, computed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (company_id, score_date) DO UPDATE SET
                perf_score_mobile = EXCLUDED.perf_score_mobile,
                perf_score_desktop = EXCLUDED.perf_score_desktop,
                quality_score = EXCLUDED.quality_score,
                cwv_badness = EXCLUDED.cwv_badness,
                uptime_pct = EXCLUDED.uptime_pct,
                avg_response_ms = EXCLUDED.avg_response_ms,
                broken_link_pct = EXCLUDED.broken_link_pct,
                perf_contribution = EXCLUDED.perf_contribution,
                cwv_contribution = EXCLUDED.cwv_contribution,
                quality_contribution = EXCLUDED.quality_contribution,
                reliability_contribution = EXCLUDED.reliability_contribution,
                errors_contribution = EXCLUDED.errors_contribution,
                friction_score = EXCLUDED.friction_score,
                runs_aggregated = EXCLUDED.runs_aggregated,
                data_completeness_pct = EXCLUDED.data_completeness_pct,
                reliability_source = EXCLUDED.reliability_source,
                computed_at = now()
            """,
            (
                company_id, score_date, perf_mobile, perf_desktop,
                quality_score, cwv_badness, uptime_pct, avg_response_ms, broken_link_pct,
                perf_contribution, cwv_contribution, quality_contribution,
                reliability_contribution, errors_contribution, friction_score,
                runs_aggregated, data_completeness_pct, reliability_source,
            ),
        )
    conn.commit()
    return friction_score, data_completeness_pct


def run_for_all_companies(conn, company_ids: list[int], score_date: date = None):
    results = {}
    for company_id in company_ids:
        results[company_id] = compute_and_store(conn, company_id, score_date)
    return results
