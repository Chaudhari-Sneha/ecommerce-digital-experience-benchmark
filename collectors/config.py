"""Shared configuration: env vars and the company list."""
import os
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": os.environ.get("DB_PORT", "5432"),
    "dbname": os.environ.get("DB_NAME", "digital_friction"),
    "user": os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", ""),
}

PAGESPEED_API_KEY = os.environ.get("PAGESPEED_API_KEY", "")


def redact(text):
    """Strip the API key from text. Failed requests raise errors that quote the
    full request URL, key included, and those messages end up in the database,
    the dashboard and the log."""
    if not text:
        return text
    text = re.sub(r"([?&]key=)[^&\s'\"]+", r"\1REDACTED", str(text))
    if PAGESPEED_API_KEY:
        text = text.replace(PAGESPEED_API_KEY, "REDACTED")
    return text


DATA_RAW_DIR = ROOT_DIR / "data" / "raw"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
}

# Seconds to wait between companies within a single collector, to avoid
# looking like a burst/scraping pattern to any shared WAF reputation system.
COMPANY_STAGGER_SECONDS = 12


def load_companies() -> list[dict]:
    with open(ROOT_DIR / "config" / "companies.yaml", "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data["companies"]
