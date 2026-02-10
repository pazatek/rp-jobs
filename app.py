"""Flask web app for the UIUC Research Park Job Board."""

import logging
import os
import re
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from flask import Flask, jsonify, render_template, request

from database import add_subscriber, get_active_subscribers, init_db, remove_subscriber

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

JOBS_JSON_URL = (
    "https://raw.githubusercontent.com/pazatek/rp-jobs/main/jobs.json"
)
CACHE_TTL = 300  # 5 minutes

_jobs_cache: dict = {"data": None, "fetched_at": 0.0}


def fetch_jobs() -> list[dict]:
    """Fetch jobs.json from GitHub raw URL with in-memory TTL cache."""
    now = time.time()
    if _jobs_cache["data"] is not None and now - _jobs_cache["fetched_at"] < CACHE_TTL:
        return _jobs_cache["data"]

    try:
        resp = requests.get(JOBS_JSON_URL, timeout=10)
        resp.raise_for_status()
        jobs = resp.json()
        _jobs_cache["data"] = jobs
        _jobs_cache["fetched_at"] = now
        return jobs
    except Exception as e:
        logger.error("Failed to fetch jobs.json: %s", e)
        # Return stale cache if available
        if _jobs_cache["data"] is not None:
            return _jobs_cache["data"]
        return []


def format_posted_date(posted_date_str: str, published_parsed=None) -> str:
    """Format date for display. Lightweight version for the web app."""
    if not posted_date_str or posted_date_str == "N/A":
        return "N/A"
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        if published_parsed:
            dt = datetime(*published_parsed[:6], tzinfo=ZoneInfo("UTC"))
            cst = ZoneInfo("America/Chicago")
            dt_cst = dt.astimezone(cst)
            return dt_cst.strftime("%b %d, %Y %I:%M %p CST")

        import email.utils

        timestamp = email.utils.parsedate_tz(posted_date_str)
        if timestamp:
            dt = datetime(*timestamp[:6], tzinfo=ZoneInfo("UTC"))
            cst = ZoneInfo("America/Chicago")
            dt_cst = dt.astimezone(cst)
            return dt_cst.strftime("%b %d, %Y %I:%M %p CST")
    except Exception:
        pass

    return posted_date_str[:16] if len(posted_date_str) > 16 else posted_date_str


@app.route("/")
def index():
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    jobs = [dict(j) for j in fetch_jobs()]
    jobs.sort(key=lambda x: x.get("published_parsed") or [0] * 9, reverse=True)

    cutoff = datetime.now(ZoneInfo("UTC")) - timedelta(days=3)

    for job in jobs:
        job["formatted_date"] = format_posted_date(
            job.get("posted_date"), job.get("published_parsed")
        )
        pp = job.get("published_parsed")
        if pp:
            try:
                job_dt = datetime(*pp[:6], tzinfo=ZoneInfo("UTC"))
                job["is_new"] = job_dt > cutoff
            except Exception:
                job["is_new"] = False
        else:
            job["is_new"] = False

    return render_template("index.html", jobs=jobs)


@app.route("/api/subscribe", methods=["POST"])
def subscribe():
    data = request.get_json()
    if not data or not data.get("email"):
        return jsonify({"success": False, "message": "Email is required"}), 400

    email = data["email"].strip().lower()

    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return jsonify({"success": False, "message": "Invalid email address"}), 400

    result = add_subscriber(email)
    if result["success"] and result["message"] != "Already subscribed!":
        send_welcome_email(email, result.get("token", ""))
    # Don't expose token to client
    response = {"success": result["success"], "message": result["message"]}
    status_code = 200 if result["success"] else 500
    return jsonify(response), status_code


def send_welcome_email(recipient: str, token: str) -> None:
    """Send a welcome email to a new subscriber."""
    sender = os.environ.get("EMAIL_SENDER")
    password = os.environ.get("EMAIL_PASSWORD")
    app_url = os.environ.get("APP_URL", "").rstrip("/")

    if not sender or not password:
        return

    unsubscribe_url = f"{app_url}/unsubscribe?token={token}" if app_url and token else ""

    html = f"""
    <html>
      <body style="font-family: Arial, sans-serif; line-height: 1.6;">
        <h2 style="color: #13294b;">Welcome to Research Park Job Alerts!</h2>
        <p>You're now subscribed to receive notifications when new jobs are posted at the UIUC Research Park.</p>
        <p>You'll get an email whenever new positions are detected (we check every 15 minutes during business hours).</p>
        <p><a href="{app_url}" style="display: inline-block; background-color: #13294b; color: #fff; padding: 10px 20px; border-radius: 6px; text-decoration: none; font-weight: bold;">View the Job Board</a></p>
        <p style="color: #999; font-size: 11px; margin-top: 30px;">
          <a href="{unsubscribe_url}" style="color: #999;">Unsubscribe from these notifications</a>
        </p>
      </body>
    </html>
    """

    try:
        msg = MIMEMultipart()
        msg["From"] = sender
        msg["To"] = recipient
        msg["Subject"] = "Welcome to Research Park Job Alerts"
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            server.send_message(msg)
        logger.info("Welcome email sent to %s", recipient)
    except Exception as e:
        logger.error("Failed to send welcome email to %s: %s", recipient, e)


@app.route("/unsubscribe")
def unsubscribe():
    token = request.args.get("token", "")
    if not token:
        return render_template("unsubscribed.html", success=False), 400

    success = remove_subscriber(token)
    return render_template("unsubscribed.html", success=success)


def require_admin():
    """Check for admin key in Authorization header."""
    admin_key = os.environ.get("ADMIN_KEY")
    if not admin_key:
        return jsonify({"success": False, "message": "ADMIN_KEY not configured"}), 500
    provided = request.headers.get("Authorization", "").replace("Bearer ", "")
    if provided != admin_key:
        return jsonify({"success": False, "message": "Unauthorized"}), 401
    return None


@app.route("/api/test-notification", methods=["POST"])
def test_notification():
    """Send a fake job notification to all subscribers for testing."""
    auth_error = require_admin()
    if auth_error:
        return auth_error
    sender = os.environ.get("EMAIL_SENDER")
    password = os.environ.get("EMAIL_PASSWORD")
    app_url = os.environ.get("APP_URL", "").rstrip("/")

    if not sender or not password:
        return jsonify({"success": False, "message": "Email credentials not set"}), 500

    from database import get_active_subscribers
    subscribers = get_active_subscribers()
    if not subscribers:
        return jsonify({"success": False, "message": "No subscribers found"}), 404

    fake_jobs = [
        {"company": "Acme Corp", "position": "Software Engineering Intern - Summer 2026"},
        {"company": "TechStart Inc", "position": "Senior Data Scientist"},
    ]

    count = len(fake_jobs)
    subject = f"\U0001f393 {count} New Research Park Job{'s' if count > 1 else ''} Found!"
    html = f"""
    <html>
      <body style="font-family: Arial, sans-serif; line-height: 1.6;">
        <h2 style="color: #13294b;">New Job Postings at Research Park</h2>
        <p>The following new positions were just detected:</p>
        <ul style="list-style-type: none; padding: 0;">
    """
    for job in fake_jobs:
        html += f"""
          <li style="margin-bottom: 15px; border-left: 4px solid #E84A27; padding-left: 10px;">
            <strong>{job['company']}</strong><br>
            {job['position']}
          </li>
        """
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            for sub in subscribers:
                unsubscribe_url = f"{app_url}/unsubscribe?token={sub['unsubscribe_token']}" if app_url else ""
                body = html + f"""
        </ul>
        <p><a href="{app_url}" style="display: inline-block; background-color: #13294b; color: #fff; padding: 10px 20px; border-radius: 6px; text-decoration: none; font-weight: bold;">View the Job Board</a></p>
        <p style="color: #666; font-size: 12px; margin-top: 30px;">
          This is a <strong>test notification</strong> from your Research Park Job Monitor.
        </p>
        <p style="color: #999; font-size: 11px;">
          <a href="{unsubscribe_url}" style="color: #999;">Unsubscribe from these notifications</a>
        </p>
      </body>
    </html>
    """
                msg = MIMEMultipart()
                msg["From"] = sender
                msg["To"] = sub["email"]
                msg["Subject"] = subject
                msg.attach(MIMEText(body, "html"))
                server.send_message(msg)
        return jsonify({"success": True, "message": f"Test notification sent to {len(subscribers)} subscriber(s)"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/remove-subscriber", methods=["POST"])
def admin_remove_subscriber():
    auth_error = require_admin()
    if auth_error:
        return auth_error
    data = request.get_json()
    if not data or not data.get("email"):
        return jsonify({"success": False, "message": "Email is required"}), 400
    from database import get_connection
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM subscribers WHERE email = %s", (data["email"],))
                deleted = cur.rowcount > 0
            conn.commit()
        return jsonify({"success": deleted, "message": "Removed" if deleted else "Not found"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/stats")
def stats():
    auth_error = require_admin()
    if auth_error:
        return auth_error
    subscribers = get_active_subscribers()
    app_url = os.environ.get("APP_URL", "NOT SET")
    return jsonify({"subscribers": len(subscribers), "app_url": app_url})


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# Initialize DB on startup
with app.app_context():
    try:
        init_db()
    except Exception as e:
        logger.error("Failed to initialize database: %s", e)


if __name__ == "__main__":
    app.run(debug=True, port=5001)
