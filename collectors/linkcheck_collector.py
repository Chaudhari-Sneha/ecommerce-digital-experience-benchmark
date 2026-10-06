"""Shallow, polite broken-link checker: homepage only, same-domain links,
sequential (no concurrency burst), aborts early on repeated blocking rather
than counting a blocked crawl as "broken links".
"""
import json
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from . import http
from .config import BROWSER_HEADERS as HEADERS

MAX_LINKS = 40
LINK_STAGGER_SECONDS = 0.7
CONSECUTIVE_BLOCKED_ABORT_THRESHOLD = 3
# Worst case per link is HEAD + GET fallback timeouts, so cap total time per company.
COMPANY_TIME_BUDGET_SECONDS = 90
BLOCKED_STATUS_CODES = (202, 403, 429)


def _same_registrable_domain(base_domain: str, link_url: str) -> bool:
    host = urlparse(link_url).netloc.lower()
    host = host.split(":")[0]
    base_domain = base_domain.lower()
    return host == base_domain or host.endswith("." + base_domain)


def _extract_links(homepage_url: str, html: str, base_domain: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(homepage_url, href)
        if not absolute.startswith(("http://", "https://")):
            continue
        if not _same_registrable_domain(base_domain, absolute):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        links.append(absolute)
        if len(links) >= MAX_LINKS:
            break
    return links


def _check_link(url: str) -> int | None:
    """Returns a status code, or None on connection failure/timeout."""
    try:
        resp = requests.head(url, headers=HEADERS, timeout=(5, 10), allow_redirects=True)
        if resp.status_code == 405:
            raise requests.RequestException("HEAD not allowed")
        return resp.status_code
    except requests.RequestException:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=(5, 10), stream=True)
            resp.close()
            return resp.status_code
        except requests.RequestException:
            return None


def collect_for_company(conn, run_id: int, company_id: int, homepage_url: str, domain: str) -> tuple[str, str | None]:
    links_found = links_checked = links_broken = links_unchecked = 0
    broken_samples = []
    collection_status = "ok"
    error_message = None

    try:
        resp = http.get(homepage_url, headers=HEADERS, read_timeout=15, deadline_seconds=25)
    except requests.RequestException as exc:
        error_message = str(exc)
        _store(conn, run_id, company_id, 0, 0, 0, 0, [], "error", error_message)
        return "error", error_message

    if resp.status_code in BLOCKED_STATUS_CODES:
        _store(conn, run_id, company_id, 0, 0, 0, 0, [], "blocked", None)
        return "blocked", None
    if not resp.ok:
        error_message = f"homepage HTTP {resp.status_code}"
        _store(conn, run_id, company_id, 0, 0, 0, 0, [], "error", error_message)
        return "error", error_message

    links = _extract_links(homepage_url, resp.text, domain)
    links_found = len(links)
    if not links:
        # Homepage loaded but its links are rendered client-side by JavaScript
        # (e.g. Amazon India, Tata CLiQ), so a plain HTML fetch can't see them.
        _store(conn, run_id, company_id, 0, 0, 0, 0, [], "no_links", None)
        return "unsupported", "homepage links are rendered by JavaScript"

    deadline = time.monotonic() + COMPANY_TIME_BUDGET_SECONDS
    consecutive_blocked = 0
    for i, link in enumerate(links):
        if consecutive_blocked >= CONSECUTIVE_BLOCKED_ABORT_THRESHOLD:
            links_unchecked += len(links) - i
            collection_status = "blocked"
            break
        if time.monotonic() > deadline:
            links_unchecked += len(links) - i
            collection_status = "partial"
            break

        status = _check_link(link)
        time.sleep(LINK_STAGGER_SECONDS)

        if status is None:
            links_unchecked += 1
            continue
        if status == 429:
            links_unchecked += len(links) - i
            collection_status = "partial"
            break
        if status == 403:
            consecutive_blocked += 1
            links_unchecked += 1
            continue

        consecutive_blocked = 0
        links_checked += 1
        if status >= 400:
            links_broken += 1
            if len(broken_samples) < 10:
                broken_samples.append({"url": link, "status_code": status})

    broken_pct = round(100 * links_broken / links_checked, 2) if links_checked else None
    _store(conn, run_id, company_id, links_found, links_checked, links_broken, links_unchecked, broken_samples, collection_status, error_message, broken_pct)

    if links_checked == 0:
        return ("blocked" if collection_status == "blocked" else "error"), error_message or "no links could be checked"
    return "success", None


def _store(conn, run_id, company_id, links_found, links_checked, links_broken, links_unchecked, broken_samples, collection_status, error_message, broken_pct=None):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO link_check_runs (
                run_id, company_id, checked_at, links_found, links_checked,
                links_broken, links_unchecked, broken_pct, collection_status, broken_urls_sample
            ) VALUES (%s, %s, clock_timestamp(), %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id, company_id, links_found, links_checked, links_broken,
                links_unchecked, broken_pct, collection_status, json.dumps(broken_samples),
            ),
        )
    conn.commit()
