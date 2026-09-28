"""Shared database module for subscriber management via PostgreSQL."""

import os
import uuid
import logging
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)


def get_connection():
    """Connect using DATABASE_URL env var."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL environment variable is not set")
    # Parse URL explicitly to handle '.' in Supabase pooler usernames
    from urllib.parse import urlparse
    parsed = urlparse(database_url)
    return psycopg2.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        dbname=parsed.path.lstrip("/"),
        user=parsed.username,
        password=parsed.password,
    )


def init_db() -> None:
    """Create subscribers table if not exists."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subscribers (
                    id SERIAL PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    unsubscribe_token TEXT UNIQUE NOT NULL,
                    subscribed_at TIMESTAMP DEFAULT NOW(),
                    active BOOLEAN DEFAULT TRUE
                )
            """)
            cur.execute("""
                ALTER TABLE subscribers
                ADD COLUMN IF NOT EXISTS preference TEXT DEFAULT 'both'
            """)
            cur.execute("""
                ALTER TABLE subscribers
                ADD COLUMN IF NOT EXISTS confirmed BOOLEAN DEFAULT FALSE
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS stats_snapshots (
                    id SERIAL PRIMARY KEY,
                    recorded_at TIMESTAMP DEFAULT NOW(),
                    jobs_on_board INTEGER NOT NULL,
                    new_jobs_found INTEGER NOT NULL,
                    active_subscribers INTEGER NOT NULL,
                    total_jobs_ever INTEGER NOT NULL
                )
            """)
        conn.commit()
    logger.info("Database initialized")


def add_subscriber(email: str, preference: str = "both") -> dict:
    """Insert subscriber with generated UUID token. Return {success, message}."""
    if preference not in ("internship", "fulltime", "both"):
        preference = "both"
    token = str(uuid.uuid4())
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT active, confirmed FROM subscribers WHERE email = %s", (email,)
                )
                row = cur.fetchone()
                if row:
                    active, confirmed = row
                    if active and confirmed:
                        return {"success": True, "message": "Already subscribed!"}
                    if active and not confirmed:
                        # Pending confirmation — regenerate token and resend
                        cur.execute(
                            "UPDATE subscribers SET unsubscribe_token = %s, preference = %s WHERE email = %s",
                            (token, preference, email),
                        )
                        conn.commit()
                        return {"success": True, "message": "Check your email to confirm your subscription.", "token": token, "needs_confirm": True}
                    # Inactive (unsubscribed) — reactivate as unconfirmed
                    cur.execute(
                        "UPDATE subscribers SET active = TRUE, confirmed = FALSE, unsubscribe_token = %s, preference = %s WHERE email = %s",
                        (token, preference, email),
                    )
                else:
                    cur.execute(
                        "INSERT INTO subscribers (email, unsubscribe_token, preference, confirmed) VALUES (%s, %s, %s, FALSE)",
                        (email, token, preference),
                    )
            conn.commit()
        return {"success": True, "message": "Check your email to confirm your subscription.", "token": token, "needs_confirm": True}
    except Exception as e:
        logger.error("Failed to add subscriber %s: %s", email, e)
        return {"success": False, "message": "Failed to subscribe. Please try again."}


def confirm_subscriber(token: str) -> bool:
    """Set confirmed=TRUE where unsubscribe_token matches. Return success."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE subscribers SET confirmed = TRUE WHERE unsubscribe_token = %s AND active = TRUE AND confirmed = FALSE",
                    (token,),
                )
                updated = cur.rowcount > 0
            conn.commit()
        return updated
    except Exception as e:
        logger.error("Failed to confirm subscriber with token %s: %s", token, e)
        return False


def remove_subscriber(token: str) -> bool:
    """Set active=FALSE where unsubscribe_token matches. Return success."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE subscribers SET active = FALSE WHERE unsubscribe_token = %s AND active = TRUE",
                    (token,),
                )
                updated = cur.rowcount > 0
            conn.commit()
        return updated
    except Exception as e:
        logger.error("Failed to remove subscriber with token %s: %s", token, e)
        return False


def get_active_subscribers() -> list[dict]:
    """Return list of {email, unsubscribe_token, preference} where active=TRUE and confirmed=TRUE."""
    try:
        with get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT email, unsubscribe_token, COALESCE(preference, 'both') AS preference FROM subscribers WHERE active = TRUE AND confirmed = TRUE"
                )
                return [dict(row) for row in cur.fetchall()]
    except Exception as e:
        logger.error("Failed to get active subscribers: %s", e)
        return []


def is_subscribed(email: str) -> bool:
    """Check if email is already actively subscribed."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM subscribers WHERE email = %s AND active = TRUE",
                    (email,),
                )
                return cur.fetchone() is not None
    except Exception as e:
        logger.error("Failed to check subscription for %s: %s", email, e)
        return False


def record_stats_snapshot(jobs_on_board: int, new_jobs_found: int, active_subscribers: int) -> bool:
    """Record a stats snapshot. Computes total_jobs_ever from previous snapshot. Non-fatal on error."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT total_jobs_ever FROM stats_snapshots ORDER BY id DESC LIMIT 1"
                )
                row = cur.fetchone()
                if row is not None:
                    total_jobs_ever = row[0] + new_jobs_found
                else:
                    # First snapshot ever — seed with current board count
                    total_jobs_ever = jobs_on_board

                cur.execute(
                    """INSERT INTO stats_snapshots
                       (jobs_on_board, new_jobs_found, active_subscribers, total_jobs_ever)
                       VALUES (%s, %s, %s, %s)""",
                    (jobs_on_board, new_jobs_found, active_subscribers, total_jobs_ever),
                )
            conn.commit()
        logger.info(
            "Stats snapshot recorded: board=%d new=%d subs=%d total_ever=%d",
            jobs_on_board, new_jobs_found, active_subscribers, total_jobs_ever,
        )
        return True
    except Exception as e:
        logger.error("Failed to record stats snapshot: %s", e)
        return False


def get_stats_history(limit: int = 500) -> list[dict]:
    """Return recent stats snapshots as list of dicts, newest first."""
    try:
        with get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, recorded_at, jobs_on_board, new_jobs_found, "
                    "active_subscribers, total_jobs_ever "
                    "FROM stats_snapshots ORDER BY id DESC LIMIT %s",
                    (limit,),
                )
                rows = cur.fetchall()
                # Convert to plain dicts and make recorded_at JSON-serializable
                result = []
                for row in rows:
                    d = dict(row)
                    if d.get("recorded_at"):
                        d["recorded_at"] = d["recorded_at"].isoformat()
                    result.append(d)
                return result
    except Exception as e:
        logger.error("Failed to get stats history: %s", e)
        return []
