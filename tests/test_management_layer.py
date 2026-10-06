"""Checks the SQL analytics layer against independent Python re-implementations,
computed from the raw friction_scores rows, plus unit tests of the anomaly rules.

    python -m unittest discover -s tests -v

Needs the local Postgres database (reads only).
"""
import statistics
import sys
import unittest
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collectors import anomaly_detection as ad  # noqa: E402
from collectors import db  # noqa: E402

COMPONENT_COLUMNS = {
    "Performance": "perf_contribution", "Core Web Vitals": "cwv_contribution",
    "Site Quality": "quality_contribution", "Reliability": "reliability_contribution",
    "Broken Links": "errors_contribution",
}
TOL = 0.01


def f(x):
    return None if x is None else float(x)


def query(sql, params=()):
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def reliable_rows():
    rows = query(
        """SELECT fs.*, c.company_name FROM friction_scores fs JOIN companies c USING (company_id)
           WHERE c.is_active AND fs.data_completeness_pct >= 60 ORDER BY company_id, score_date"""
    )
    by_company = defaultdict(list)
    for r in rows:
        by_company[r["company_id"]].append(r)
    return rows, by_company


def component_points(row):
    return {name: f(row[col]) for name, col in COMPONENT_COLUMNS.items() if row[col] is not None}


def expected_component_status(series):
    """series: [(date, points)] ascending for one company/component."""
    if len(series) < 2:
        return "Insufficient history", None
    (_, lp), (pd_, pp) = series[-1], series[-2]
    base = [p for d, p in series if pd_ - timedelta(days=7) <= d < pd_]
    if not base:
        return "Insufficient history", None
    b = sum(base) / len(base)
    if lp - b >= 1 and pp - b >= 1:
        return "Sustained worsening", lp - b
    if lp - b >= 1:
        return "New one-reading jump", lp - b
    if lp - b <= -1:
        return "Improving", lp - b
    return "Stable", lp - b


class PowerBICompatibility(unittest.TestCase):
    def test_no_view_returns_numerics_power_bi_cannot_load(self):
        # Power BI's PostgreSQL driver rejects NUMERIC values with more than 28 significant digits.
        cols = query("""SELECT table_name, column_name FROM information_schema.columns
                        WHERE table_schema = 'public' AND data_type = 'numeric' AND table_name LIKE 'v!_%%' ESCAPE '!'""")
        for c in cols:
            row = query(f"SELECT max(length(regexp_replace(abs({c['column_name']})::text, '[^0-9]', '', 'g'))) AS n "
                        f"FROM {c['table_name']}")[0]
            self.assertLessEqual(row["n"] or 0, 28, f"{c['table_name']}.{c['column_name']}")


class ScoreDecomposition(unittest.TestCase):
    def test_contributions_sum_to_score(self):
        for r in query("SELECT * FROM friction_scores"):
            parts = [f(r[c]) for c in COMPONENT_COLUMNS.values() if r[c] is not None]
            self.assertAlmostEqual(sum(parts), f(r["friction_score"]), delta=0.01,
                                   msg=f"company {r['company_id']} {r['score_date']}")


class ManagementPriority(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows, cls.by_company = reliable_rows()
        cls.view = {r["company_id"]: r for r in query("SELECT * FROM v_management_priority")}
        cls.status = {(r["company_id"], r["component"]): r for r in query("SELECT * FROM v_component_status")}
        industry = defaultdict(list)
        industry_comp = defaultdict(list)
        for r in cls.rows:
            industry[r["score_date"]].append(f(r["friction_score"]))
            for name, pts in component_points(r).items():
                industry_comp[(r["score_date"], name)].append(pts)
        cls.industry = {d: sum(v) / len(v) for d, v in industry.items()}
        cls.industry_comp = {k: sum(v) / len(v) for k, v in industry_comp.items()}

    def expected(self, company_id):
        hist = self.by_company[company_id]
        latest = hist[-1]
        L = latest["score_date"]
        score = f(latest["friction_score"])
        gap = score - self.industry[L]
        base = [f(r["friction_score"]) for r in hist if L - timedelta(days=7) <= r["score_date"] < L]
        det = score - sum(base) / len(base) if base else None
        last14 = [r for r in hist if r["score_date"] > L - timedelta(days=14)]
        above = sum(1 for r in last14 if f(r["friction_score"]) > self.industry[r["score_date"]])
        n21 = sum(1 for r in hist if r["score_date"] > L - timedelta(days=21))
        sev = min(100, score)
        gap_pts = min(100, max(0, gap) * 5)
        det_pts = min(100, max(0, det or 0) * 10)
        per_pts = 100 * above / len(last14)
        cf = 0.5 + 0.5 * (f(latest["data_completeness_pct"]) / 100) * min(1, n21 / 7)
        mps = cf * (0.35 * sev + 0.25 * gap_pts + 0.25 * det_pts + 0.15 * per_pts)
        return {"gap": gap, "det": det, "above": above, "n14": len(last14), "n21": n21, "cf": cf, "mps": mps,
                "latest": latest}

    def test_priority_score_matches_formula(self):
        self.assertEqual(set(self.view), set(self.by_company), "one row per company with reliable scores")
        for cid, v in self.view.items():
            e = self.expected(cid)
            name = v["company_name"]
            self.assertAlmostEqual(f(v["gap_vs_industry"]), e["gap"], delta=TOL, msg=name)
            if e["det"] is None:
                self.assertIsNone(v["change_vs_baseline"], name)
            else:
                self.assertAlmostEqual(f(v["change_vs_baseline"]), e["det"], delta=TOL, msg=name)
            self.assertEqual((v["above_avg_14d"], v["readings_14d"], v["readings_21d"]), (e["above"], e["n14"], e["n21"]), name)
            self.assertAlmostEqual(f(v["confidence_factor"]), e["cf"], delta=1e-6, msg=name)
            self.assertAlmostEqual(f(v["priority_score"]), e["mps"], delta=TOL, msg=name)

    def test_breakdown_sums_to_priority_score(self):
        for v in self.view.values():
            parts = sum(f(v[k]) for k in ("severity_contribution", "gap_contribution",
                                          "deterioration_contribution", "persistence_contribution"))
            self.assertAlmostEqual(parts, f(v["priority_score"]), delta=1e-6, msg=v["company_name"])

    def test_tiers_quadrants_and_rank(self):
        ranked = sorted(self.view.values(), key=lambda v: -f(v["priority_score"]))
        self.assertEqual(ranked[0]["priority_rank"], 1)
        for v in self.view.values():
            mps, gap, det = f(v["priority_score"]), f(v["gap_vs_industry"]), f(v["change_vs_baseline"]) or 0
            tier = "High" if mps >= 45 else "Medium" if mps >= 25 else "Low"
            self.assertEqual(v["priority_tier"], tier, v["company_name"])
            quadrant = ("Critical" if gap > 0 and det >= 1 else "Lagging" if gap > 0
                        else "At Risk" if det >= 1 else "Healthy")
            self.assertTrue(v["priority_quadrant"].startswith(quadrant), v["company_name"])

    def test_driver_rule(self):
        for cid, v in self.view.items():
            latest = self.by_company[cid][-1]
            L = latest["score_date"]
            cands = []
            for name, pts in component_points(latest).items():
                gap = pts - self.industry_comp[(L, name)]
                chg = self.status.get((cid, name), {}).get("change_vs_baseline")
                chg = f(chg)
                rank = 1 if gap >= 1 else 2 if (chg is not None and chg >= 1) else 3
                key = gap if rank == 1 else chg if rank == 2 else pts
                cands.append((rank, -key, name))
            expected = sorted(cands)[0][2]
            self.assertEqual(v["driver_component"], expected, v["company_name"])

    def test_component_gap_view(self):
        rows = query("SELECT * FROM v_component_gap")
        self.assertTrue(rows)
        for r in rows:
            latest = self.by_company[r["company_id"]][-1]
            expected = component_points(latest)[r["component"]] - self.industry_comp[(latest["score_date"], r["component"])]
            self.assertAlmostEqual(f(r["gap_vs_industry"]), expected, delta=TOL, msg=(r["company_name"], r["component"]))
            self.assertAlmostEqual(f(r["points_above_industry"]), max(0, expected), delta=TOL)

    def test_component_status_patterns(self):
        series = defaultdict(list)
        for r in self.rows:
            for name, pts in component_points(r).items():
                series[(r["company_id"], name)].append((r["score_date"], pts))
        for key, s in series.items():
            pattern, _ = expected_component_status(s)
            self.assertEqual(self.status[key]["pattern"], pattern, key)

    def test_recommendations_avoid_causal_or_financial_claims(self):
        banned = ("revenue", "conversion", "lost customers", "abandon", "roi", "causes", "caused by")
        for v in self.view.values():
            text = " ".join(str(v[k]) for k in ("issue", "evidence", "recommended_investigation")).lower()
            for word in banned:
                self.assertNotIn(word, text, v["company_name"])
            self.assertRegex(v["recommended_investigation"], r"^(Review|Investigate|Assess)")


class InsightViews(unittest.TestCase):
    def test_real_user_values_match_latest_pagespeed_response(self):
        latest = {r["company_id"]: r["raw_json"]["loadingExperience"]["metrics"] for r in query(
            """SELECT DISTINCT ON (company_id) company_id, raw_json FROM pagespeed_raw
               WHERE fetch_status = 'success' AND strategy = 'mobile' AND raw_json->'loadingExperience' ? 'metrics'
               ORDER BY company_id, fetched_at DESC""")}
        rows = query("SELECT * FROM v_real_user_experience")
        self.assertTrue(rows)
        for r in rows:
            m = latest[r["company_id"]]
            lcp = m["LARGEST_CONTENTFUL_PAINT_MS"]["percentile"] / 1000
            inp = m["INTERACTION_TO_NEXT_PAINT"]["percentile"]
            cls = m["CUMULATIVE_LAYOUT_SHIFT_SCORE"]["percentile"] / 100
            self.assertAlmostEqual(r["lcp_p75_s"], lcp, delta=1e-9, msg=r["company_name"])
            self.assertAlmostEqual(r["inp_p75_ms"], inp, delta=1e-9)
            self.assertAlmostEqual(r["cls_p75"], cls, delta=1e-9)
            self.assertEqual(r["passes_core_web_vitals"], lcp <= 2.5 and inp <= 200 and cls <= 0.1, r["company_name"])
            self.assertEqual(r["lcp_status"], "Good" if lcp <= 2.5 else "Needs improvement" if lcp <= 4 else "Poor")

    def test_load_time_split_adds_up(self):
        real = {r["company_id"]: r for r in query("SELECT * FROM v_real_user_experience")}
        rows = query("SELECT * FROM v_load_time_split")
        self.assertEqual(len(rows), len(real))
        for r in rows:
            src = real[r["company_id"]]
            self.assertAlmostEqual(r["server_wait_s"], src["ttfb_p75_s"], delta=1e-9)
            self.assertAlmostEqual(r["server_wait_s"] + r["page_build_s"], src["lcp_p75_s"], delta=1e-9, msg=r["company_name"])

    def test_device_gap_is_median_of_last_three_runs(self):
        rows = query("SELECT * FROM v_device_gap")
        self.assertTrue(rows)
        for r in rows:
            for device, col in (("mobile", "mobile_score"), ("desktop", "desktop_score")):
                scores = [f(x["performance_score"]) for x in query(
                    """SELECT performance_score FROM pagespeed_raw WHERE company_id = %s AND strategy = %s
                       AND fetch_status = 'success' AND performance_score IS NOT NULL ORDER BY fetched_at DESC LIMIT 3""",
                    (r["company_id"], device))]
                self.assertAlmostEqual(r[col], statistics.median(scores), delta=1e-9, msg=(r["company_name"], device))
            self.assertAlmostEqual(r["mobile_gap"], r["desktop_score"] - r["mobile_score"], delta=1e-9)

    def test_top_findings_are_persistent_and_capped(self):
        rows = query("SELECT * FROM v_top_findings")
        self.assertTrue(rows)
        per_company = defaultdict(list)
        for r in rows:
            per_company[r["company_id"]].append(r["finding_rank"])
            seen = int(r["seen_in"].split()[0])
            self.assertGreaterEqual(seen, 2, r)
            self.assertTrue(r["lighthouse_est_saving"], r)
        for ranks in per_company.values():
            self.assertEqual(sorted(ranks), list(range(1, len(ranks) + 1)))
            self.assertLessEqual(len(ranks), 3)


class AnomalyReadiness(unittest.TestCase):
    def test_readiness_matches_detection_window(self):
        _, by_company = reliable_rows()
        latest = max(r["score_date"] for rows in by_company.values() for r in rows)
        for r in query("SELECT * FROM v_anomaly_readiness"):
            days = sum(1 for x in by_company.get(r["company_id"], [])
                       if latest - timedelta(days=ad.BASELINE_WINDOW_DAYS) <= x["score_date"] < latest)
            self.assertEqual(r["baseline_days"], days, r["company_name"])
            self.assertEqual(r["monitored"], days >= ad.MIN_BASELINE_DAYS, r["company_name"])


class AnomalyRules(unittest.TestCase):
    days = [date(2026, 10, 1) + timedelta(i) for i in range(10)]

    def run_rule(self, values):
        return ad._evaluate_metric(pd.Series(values, index=self.days), self.days[-1])

    def test_small_blip_on_flat_metric_is_not_flagged(self):
        self.assertEqual(self.run_rule([0] * 9 + [0.5])["status"], "normal")

    def test_real_jump_is_flagged(self):
        self.assertEqual(self.run_rule([0] * 9 + [6])["status"], "new")

    def test_normal_noise_is_not_flagged(self):
        self.assertEqual(self.run_rule([30, 31, 29, 30, 32, 28, 31, 30, 29, 31])["status"], "normal")

    def test_spike_is_high_severity(self):
        r = self.run_rule([30, 31, 29, 30, 32, 28, 31, 30, 29, 45])
        self.assertEqual((r["status"], r["severity"], r["anomaly_type"]), ("new", "high", "spike"))

    def test_too_little_history_is_reported_not_guessed(self):
        r = ad._evaluate_metric(pd.Series([30, 31, 45], index=self.days[-3:]), self.days[-1])
        self.assertEqual(r["status"], "insufficient_history")

    def test_days_outside_21_day_window_are_ignored(self):
        idx = [date(2026, 9, 1)] + [date(2026, 9, 25) + timedelta(i) for i in range(5)] + [date(2026, 10, 5)]
        r = ad._evaluate_metric(pd.Series([30] * 6 + [44], index=idx), date(2026, 10, 5))
        self.assertEqual(r["baseline_days"], 5)


if __name__ == "__main__":
    unittest.main()
