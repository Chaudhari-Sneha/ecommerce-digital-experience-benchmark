"""Google PageSpeed Insights collector.

Fetched server-side by Google, so our own IP never touches the target site.
Free tier is ~400 req/100s and 25,000/day; at 10 companies x 2 strategies x
a few runs/day we're nowhere near the limit.
"""
import json
import time

import requests

from . import http
from .config import PAGESPEED_API_KEY, redact

PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
CATEGORIES = ["performance", "accessibility", "best-practices", "seo"]
# A Lighthouse run on a heavy page can legitimately take 60-100s.
READ_TIMEOUT_SECONDS = 120
TOTAL_DEADLINE_SECONDS = 150
MAX_RETRIES = 1
BLOCKED_STATUS_CODES = (403, 429)


def _score_pct(categories: dict, key: str):
    cat = categories.get(key)
    if not cat or cat.get("score") is None:
        return None
    return round(cat["score"] * 100, 2)


def _audit_value(audits: dict, *keys):
    for key in keys:
        audit = audits.get(key)
        if audit and audit.get("numericValue") is not None:
            return audit["numericValue"]
    return None


def _document_status_code(audits: dict):
    items = (audits.get("network-requests") or {}).get("details", {}).get("items") or []
    for item in items:
        if item.get("resourceType") == "Document":
            return item.get("statusCode")
    return items[0].get("statusCode") if items else None


def classify(payload: dict) -> tuple[str, str | None]:
    """Returns (fetch_status, message) for a PSI response:
    - 'blocked': the site refused Google's crawler (403/429). Lighthouse still
      "scores" the tiny error page -- often 100/100 -- so these readings must
      never feed the friction score.
    - 'page_error': the page genuinely failed to load (runtimeError, 5xx, 404).
    - 'success': a real page was measured.
    """
    lighthouse = payload.get("lighthouseResult", {})
    runtime_error = lighthouse.get("runtimeError")
    if runtime_error:
        return "page_error", f'{runtime_error.get("code")}: {runtime_error.get("message", "")}'[:500]
    status = _document_status_code(lighthouse.get("audits", {}))
    if status in BLOCKED_STATUS_CODES:
        return "blocked", f"site returned HTTP {status} to Lighthouse"
    if status is not None and status >= 400:
        return "page_error", f"site returned HTTP {status} to Lighthouse"
    return "success", None


def fetch_one(url: str, strategy: str) -> dict:
    """Call PSI for one URL/strategy. Returns parsed fields plus raw_json, or
    raises requests.RequestException on API failure."""
    params = {"url": url, "strategy": strategy, "category": CATEGORIES}
    if PAGESPEED_API_KEY:
        params["key"] = PAGESPEED_API_KEY
    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = http.get(
                PSI_ENDPOINT, params=params, read_timeout=READ_TIMEOUT_SECONDS,
                deadline_seconds=TOTAL_DEADLINE_SECONDS,
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = requests.RequestException(f"PSI HTTP {resp.status_code}")
                time.sleep(5 * (attempt + 1))
                continue
            resp.raise_for_status()
            payload = resp.json()
            break
        except requests.Timeout:
            # Lighthouse itself couldn't finish this page; retrying just doubles the wait.
            raise
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(5 * (attempt + 1))
    else:
        raise last_exc

    lighthouse = payload.get("lighthouseResult", {})
    categories = lighthouse.get("categories", {})
    audits = lighthouse.get("audits", {})
    fetch_status, message = classify(payload)

    return {
        "fetch_status": fetch_status,
        "message": message,
        "performance_score": _score_pct(categories, "performance"),
        "accessibility_score": _score_pct(categories, "accessibility"),
        "best_practices_score": _score_pct(categories, "best-practices"),
        "seo_score": _score_pct(categories, "seo"),
        "lcp_ms": _audit_value(audits, "largest-contentful-paint"),
        "cls": _audit_value(audits, "cumulative-layout-shift"),
        "inp_ms": _audit_value(
            audits, "interaction-to-next-paint", "experimental-interaction-to-next-paint", "max-potential-fid"
        ),
        "ttfb_ms": _audit_value(audits, "server-response-time"),
        "raw_json": payload,
    }


def collect_for_company(conn, run_id: int, company_id: int, homepage_url: str) -> tuple[str, str | None]:
    """Fetch mobile + desktop PSI results for one company and store them.
    Returns (status, error_message) for company_run_results."""
    results = {}
    for strategy in ("mobile", "desktop"):
        try:
            results[strategy] = fetch_one(homepage_url, strategy)
        except Exception as exc:  # noqa: BLE001 - record any failure and continue
            results[strategy] = {"fetch_status": "error", "message": redact(str(exc))[:500]}

    with conn.cursor() as cur:
        for strategy, data in results.items():
            if data["fetch_status"] == "error":
                cur.execute(
                    """
                    INSERT INTO pagespeed_raw (run_id, company_id, strategy, fetched_at, fetch_status, error_message)
                    VALUES (%s, %s, %s, clock_timestamp(), 'error', %s)
                    """,
                    (run_id, company_id, strategy, data["message"]),
                )
                continue
            cur.execute(
                """
                INSERT INTO pagespeed_raw (
                    run_id, company_id, strategy, fetched_at,
                    performance_score, accessibility_score, best_practices_score, seo_score,
                    lcp_ms, cls, inp_ms, ttfb_ms, fetch_status, error_message, raw_json
                ) VALUES (%s, %s, %s, clock_timestamp(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_id, company_id, strategy,
                    data["performance_score"], data["accessibility_score"],
                    data["best_practices_score"], data["seo_score"],
                    data["lcp_ms"], data["cls"], data["inp_ms"], data["ttfb_ms"],
                    data["fetch_status"], data["message"], json.dumps(data["raw_json"]),
                ),
            )
    conn.commit()

    statuses = {d["fetch_status"] for d in results.values()}
    messages = " | ".join(f'{s}: {d["message"]}' for s, d in results.items() if d["message"]) or None
    if statuses == {"error"}:
        return "error", messages
    if statuses == {"blocked"}:
        return "blocked", messages
    if "error" in statuses:
        return "partial", messages
    # 'page_error' is real availability data, not a collector failure.
    return "success", messages
