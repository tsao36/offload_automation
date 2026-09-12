"""
db_health_notify.py — DB health sentinel notifier.

Writes (or updates) a single sentinel row in ips_jira_bugs so that the
status of the last Bug_Dashboard_checker run is visible directly from the
PowerBI dashboard without needing email.

Sentinel identifier:  jira_id = 'HEALTH-CHECK-001'
Reporter shown:       Jonathan Tsao
Title when OK:        [DB HEALTH] DB is accurate (checked YYYY-MM-DD HH:MM)
Title when FAIL:      [DB HEALTH] DB not updated - ERROR ... (YYYY-MM-DD HH:MM)

Usage (called from bat after main script):
    python db_health_notify.py --status ok
    python db_health_notify.py --status error --detail "exit code 1"

Credentials are read from .env in the same directory (DB_NAME, DB_USER,
DB_PASS, DB_HOST, DB_PORT).  The script never raises so a connectivity
failure here will not mask the real exit code of the bat.
"""

import argparse
import datetime
import os
import sys

# ---------------------------------------------------------------------------
# Sentinel constants — change only if needed, keep stable across runs.
# The sentinel row is identified by the fixed prefix in jira_title / ips_title.
# jira_id is NOT used so no fake ticket ID pollutes the ID space.
# ---------------------------------------------------------------------------
SENTINEL_TITLE_PREFIX = "[DB HEALTH]"
SENTINEL_REPORTER = "Jonathan Tsao"
DB_TABLE = "ips_jira_bugs"


# ---------------------------------------------------------------------------
# Minimal .env loader (avoids adding python-dotenv dependency on server)
# ---------------------------------------------------------------------------
def _load_dotenv(path: str) -> None:
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def _find_dotenv(start_dir: str, max_up: int = 4) -> str | None:
    cur = os.path.abspath(start_dir)
    for _ in range(max_up + 1):
        candidate = os.path.join(cur, ".env")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Update DB health sentinel row in ips_jira_bugs.")
    ap.add_argument(
        "--status",
        choices=["ok", "error"],
        required=True,
        help="'ok' → DB is accurate; 'error' → DB not updated",
    )
    ap.add_argument(
        "--detail",
        default="",
        help="Optional extra detail appended to the error title (e.g. exit code).",
    )
    args = ap.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = _find_dotenv(script_dir)
    if env_path:
        _load_dotenv(env_path)

    db_name = os.getenv("DB_NAME")
    db_user = os.getenv("DB_USER")
    db_pass = os.getenv("DB_PASS")
    db_host = os.getenv("DB_HOST")
    db_port = int(os.getenv("DB_PORT", "5432"))

    if not all([db_name, db_user, db_pass, db_host]):
        print("[HEALTH NOTIFY] ERROR: DB credentials not found in .env — skipping sentinel update.", file=sys.stderr)
        return 1

    now = datetime.datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M")

    if args.status == "ok":
        title = f"[DB HEALTH] DB is accurate (checked {ts})"
    else:
        detail_str = f" | {args.detail}" if args.detail else ""
        title = f"[DB HEALTH] DB not updated - ERROR{detail_str} ({ts})"

    try:
        import psycopg2  # type: ignore
    except ImportError:
        print("[HEALTH NOTIFY] ERROR: psycopg2 not installed — cannot update sentinel.", file=sys.stderr)
        return 1

    conn = None
    try:
        try:
            conn = psycopg2.connect(
                dbname=db_name,
                user=db_user,
                password=db_pass,
                host=db_host,
                port=db_port,
            )
        except Exception:
            conn = psycopg2.connect(
                database=db_name,
                user=db_user,
                password=db_pass,
                host=db_host,
                port=db_port,
            )

        cur = conn.cursor()

        # Determine which title/date columns exist.
        cur.execute(
            """
            SELECT attname FROM pg_attribute a
            JOIN pg_class c ON a.attrelid = c.oid
            WHERE c.relname = %s
              AND pg_table_is_visible(c.oid)
              AND a.attnum > 0
              AND NOT a.attisdropped
            """,
            (DB_TABLE,),
        )
        existing_cols = {row[0] for row in cur.fetchall()}

        # Prefer jira_title as the lookup column (= Jira Summary field), fall back to ips_title.
        if "jira_title" in existing_cols:
            title_col = "jira_title"
        else:
            title_col = "ips_title"

        # Check whether the sentinel row already exists (match by fixed prefix).
        cur.execute(
            f"SELECT {title_col} FROM {DB_TABLE} WHERE {title_col} LIKE %s LIMIT 1",  # noqa: S608
            (f"{SENTINEL_TITLE_PREFIX}%",),
        )
        sentinel_exists = cur.fetchone() is not None

        today = now.date()

        if sentinel_exists:
            # Build UPDATE: refresh title columns, reporter and date.
            set_clauses = [
                f"{title_col} = %s",
                "reporter = %s",
            ]
            params: list = [title, SENTINEL_REPORTER]

            # Also sync the other title column if it exists.
            other_title_col = "ips_title" if title_col == "jira_title" else "jira_title"
            if other_title_col in existing_cols:
                set_clauses.append(f"{other_title_col} = %s")
                params.append(title)
            if "ips_created_date" in existing_cols:
                set_clauses.append("ips_created_date = %s")
                params.append(today)
            if "jira_created_date" in existing_cols:
                set_clauses.append("jira_created_date = %s")
                params.append(today)

            params.append(f"{SENTINEL_TITLE_PREFIX}%")
            cur.execute(
                f"UPDATE {DB_TABLE} SET {', '.join(set_clauses)} WHERE {title_col} LIKE %s",  # noqa: S608
                params,
            )
            action = "updated"
        else:
            # INSERT a minimal sentinel row.  Only include columns that exist.
            # jira_id is intentionally left out — no fake ticket ID.
            cols = [title_col, "reporter"]
            vals: list = [title, SENTINEL_REPORTER]

            optional: list[tuple[str, object]] = [
                ("ips_title" if title_col == "jira_title" else "jira_title", title),
                ("customer", "NA"),
                ("technology", "WiFi"),
                ("ips_case_number", -1),
                ("ips_created_date", today),
                ("jira_created_date", today),
                ("ips_status", "NA"),
                ("jira_status", "NA"),
            ]
            for col, val in optional:
                if col in existing_cols:
                    cols.append(col)
                    vals.append(val)

            placeholders = ", ".join(["%s"] * len(cols))
            col_list = ", ".join(cols)
            cur.execute(
                f"INSERT INTO {DB_TABLE} ({col_list}) VALUES ({placeholders})",  # noqa: S608
                vals,
            )
            action = "inserted"

        conn.commit()
        cur.close()
        print(f"[HEALTH NOTIFY] Sentinel {action}: {title}")
        return 0

    except Exception as exc:  # pylint: disable=broad-except
        print(f"[HEALTH NOTIFY] ERROR: Failed to update sentinel — {exc}", file=sys.stderr)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return 1
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
