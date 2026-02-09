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
        conn.commit()
    logger.info("Database initialized")


def add_subscriber(email: str) -> dict:
    """Insert subscriber with generated UUID token. Return {success, message}."""
    token = str(uuid.uuid4())
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Check if already subscribed and active
                cur.execute(
                    "SELECT active FROM subscribers WHERE email = %s", (email,)
                )
                row = cur.fetchone()
                if row:
                    if row[0]:
                        return {"success": True, "message": "Already subscribed!"}
                    # Reactivate
                    cur.execute(
                        "UPDATE subscribers SET active = TRUE, unsubscribe_token = %s WHERE email = %s",
                        (token, email),
                    )
                else:
                    cur.execute(
                        "INSERT INTO subscribers (email, unsubscribe_token) VALUES (%s, %s)",
                        (email, token),
                    )
            conn.commit()
        return {"success": True, "message": "Successfully subscribed!"}
    except Exception as e:
        logger.error("Failed to add subscriber %s: %s", email, e)
        return {"success": False, "message": "Failed to subscribe. Please try again."}


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
    """Return list of {email, unsubscribe_token} where active=TRUE."""
    try:
        with get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT email, unsubscribe_token FROM subscribers WHERE active = TRUE"
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
