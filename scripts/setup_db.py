"""Creates the target database (if missing) and applies schema.sql + all
views. Run once after filling in .env:

    python scripts/setup_db.py
"""
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collectors.config import DB_CONFIG  # noqa: E402

ROOT_DIR = Path(__file__).resolve().parent.parent
SQL_FILES = [
    ROOT_DIR / "sql" / "schema.sql",
    ROOT_DIR / "sql" / "views_kpi.sql",
    ROOT_DIR / "sql" / "views_pipeline_quality.sql",
    ROOT_DIR / "sql" / "views_management.sql",
    ROOT_DIR / "sql" / "views_insights.sql",
]


def ensure_database_exists():
    maintenance_config = {**DB_CONFIG, "dbname": "postgres"}
    conn = psycopg2.connect(**maintenance_config)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_CONFIG["dbname"],))
            if cur.fetchone() is None:
                cur.execute(f'CREATE DATABASE "{DB_CONFIG["dbname"]}"')
                print(f"Created database {DB_CONFIG['dbname']}")
            else:
                print(f"Database {DB_CONFIG['dbname']} already exists")
    finally:
        conn.close()


def apply_sql_files():
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            for path in SQL_FILES:
                print(f"Applying {path.name}")
                cur.execute(path.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    ensure_database_exists()
    apply_sql_files()
    print("Database setup complete.")
