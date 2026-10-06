"""Homepage uptime / response-time checker.

403/429 responses are classified as 'blocked', not 'down' -- several targets
(Amazon India, Flipkart, Myntra) run WAFs that reject automated requests
regardless of politeness. Folding that into 'down' would unfairly penalize
their reliability score for a detection artifact, not a real outage.
"""
import time

import requests

from . import http
from .config import BROWSER_HEADERS as HEADERS

# Amazon-style bot challenges come back as 202 with a tiny interstitial page.
BLOCKED_STATUS_CODES = (202, 403, 429)
MAX_RETRIES = 1  # only for connection-level failures, not HTTP error codes


def check_one(url: str) -> dict:
    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            start = time.monotonic()
            resp = http.get(url, headers=HEADERS, deadline_seconds=20)
            elapsed_ms = int((time.monotonic() - start) * 1000)

            if resp.status_code in BLOCKED_STATUS_CODES:
                outcome = "blocked"
            elif resp.ok:
                outcome = "up"
            else:
                outcome = "down"

            return {
                "status_code": resp.status_code,
                "response_time_ms": elapsed_ms,
                "outcome": outcome,
                "final_url": resp.url,
                "error_message": None,
            }
        except requests.Timeout as exc:
            last_exc = exc
            return {
                "status_code": None, "response_time_ms": None,
                "outcome": "timeout", "final_url": None, "error_message": str(exc),
            }
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(2)
            continue

    return {
        "status_code": None, "response_time_ms": None,
        "outcome": "down", "final_url": None, "error_message": str(last_exc),
    }


def collect_for_company(conn, run_id: int, company_id: int, homepage_url: str) -> tuple[str, str | None]:
    result = check_one(homepage_url)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO uptime_checks
                (run_id, company_id, checked_at, status_code, response_time_ms, outcome, final_url, error_message)
            VALUES (%s, %s, clock_timestamp(), %s, %s, %s, %s, %s)
            """,
            (
                run_id, company_id, result["status_code"], result["response_time_ms"],
                result["outcome"], result["final_url"], result["error_message"],
            ),
        )
    conn.commit()

    if result["outcome"] == "up":
        return "success", None
    if result["outcome"] == "blocked":
        return "blocked", None
    return "error", result["error_message"] or f"outcome={result['outcome']}"
