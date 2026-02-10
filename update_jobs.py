#!/usr/bin/env python3
"""
Script that checks the Research Park job board and posts updates.
Runs on GitHub Actions to notify people when new jobs get posted.
"""

import json
import logging
import os
import re
import smtplib
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser

from database import get_active_subscribers, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

RSS_FEED_URL = "https://researchpark.illinois.edu/?feed=job_feed"
JOBS_FILE = "jobs.json"
README_FILE = "JOBS.md"

JOBS_PER_PAGE = 10
MAX_PAGES = 20
FETCH_RETRIES = 2
FETCH_RETRY_DELAY = 5

README_TEMPLATE = """# UIUC Research Park Jobs List

Auto-updated job listings from the [University of Illinois Research Park](https://researchpark.illinois.edu).

**Updated:** {last_updated} | **Total:** {total_positions}

---

| Company | Position | Posted | Link |
| ------- | -------- | ------ | ---- |
{job_table}

{posting_stats}

---

## About This Project

This repository automatically monitors the Research Park job feed and updates every hour.

- **Source:** [Research Park Job Board](https://researchpark.illinois.edu/work-here/careers/)

### How It Works

1. Python script fetches the RSS feed hourly
2. Compares against cached job listings
3. GitHub automatically commits and pushes any changes to this README

### Get Notifications

**Watch this repository** to get notified when new jobs are added:

- Click "Watch" at the top of this repo
- Select "Custom" → Check "Commits"
- You'll get a notification every time new jobs are posted!

---

_Built with Python • Automated with GitHub Actions_
"""


def load_existing_jobs() -> list[dict]:
    """Load jobs we already know about from the cache file."""
    if not Path(JOBS_FILE).exists():
        return []
    with open(JOBS_FILE, "r") as f:
        return json.load(f)


def save_jobs(jobs: list[dict]) -> None:
    """Save the current job list to a file so we remember them next time."""
    with open(JOBS_FILE, "w") as f:
        json.dump(jobs, f, indent=2)


def fetch_rss_page(page: int = 1) -> feedparser.FeedParserDict | None:
    """Grab one page of jobs from the RSS feed with retry logic."""
    url = f"{RSS_FEED_URL}&paged={page}" if page > 1 else RSS_FEED_URL
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})

    for attempt in range(1, FETCH_RETRIES + 2):
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                return feedparser.parse(response.read())
        except Exception as e:
            logger.warning(
                "Attempt %d/%d fetching RSS page %d failed: %s",
                attempt,
                FETCH_RETRIES + 1,
                page,
                e,
            )
            if attempt <= FETCH_RETRIES:
                time.sleep(FETCH_RETRY_DELAY)
    return None


def parse_job_board() -> list[dict]:
    """Go through all the pages of the job board and collect all the jobs."""
    jobs = []
    page = 1

    while page <= MAX_PAGES:
        feed = fetch_rss_page(page)
        if not feed or not feed.entries:
            break

        page_jobs = []
        for entry in feed.entries:
            company = entry.get("job_listing_company", "N/A")
            job_id = entry.get("guid", entry.link)

            job = {
                "id": job_id,
                "company": company,
                "position": entry.title,
                "link": entry.link,
                "posted_date": entry.get("published", ""),
                "published_parsed": entry.get("published_parsed"),
            }
            page_jobs.append(job)

        if not page_jobs:
            break

        jobs.extend(page_jobs)
        logger.info("Page %d: found %d jobs (total: %d)", page, len(page_jobs), len(jobs))

        if len(page_jobs) < JOBS_PER_PAGE:
            break

        page += 1

    return jobs


def find_new_jobs(current_jobs: list[dict], existing_jobs: list[dict]) -> list[dict]:
    """Figure out which jobs are new since last time we checked."""
    seen_ids = {job["id"] for job in existing_jobs}
    return [job for job in current_jobs if job["id"] not in seen_ids]


def format_posted_date(posted_date_str: str, published_parsed: list | None = None) -> str:
    """Turn the raw date from RSS into something readable like 'Nov 5, 2025 3:45 PM CST'."""
    if not posted_date_str or posted_date_str == "N/A":
        return "N/A"

    if published_parsed:
        try:
            dt = datetime(*published_parsed[:6], tzinfo=ZoneInfo("UTC"))
            cst = ZoneInfo("America/Chicago")
            dt_cst = dt.astimezone(cst)
            return dt_cst.strftime("%b %d, %Y %I:%M %p CST")
        except Exception:
            pass

    try:
        import email.utils

        timestamp = email.utils.parsedate_tz(posted_date_str)
        if timestamp:
            dt = datetime(*timestamp[:6], tzinfo=ZoneInfo("UTC"))
            cst = ZoneInfo("America/Chicago")
            dt_cst = dt.astimezone(cst)
            return dt_cst.strftime("%b %d, %Y %I:%M %p CST")
    except Exception:
        pass

    try:
        dt_str = posted_date_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo:
            cst = ZoneInfo("America/Chicago")
            dt = dt.astimezone(cst)
        else:
            dt = dt.replace(tzinfo=ZoneInfo("America/Chicago"))
        return dt.strftime("%b %d, %Y %I:%M %p CST")
    except Exception:
        return posted_date_str[:16] if len(posted_date_str) > 16 else posted_date_str


def generate_posting_insights(jobs: list[dict]) -> tuple[list[int], dict[int, int]] | None:
    """Generate insights about posting times to help users know when to check."""
    posting_hours = []

    for job in jobs:
        published_parsed = job.get("published_parsed")
        if not published_parsed:
            continue
        try:
            dt = datetime(*published_parsed[:6], tzinfo=ZoneInfo("UTC"))
            cst = ZoneInfo("America/Chicago")
            dt_cst = dt.astimezone(cst)
            posting_hours.append(dt_cst.hour)
        except Exception:
            continue

    if len(posting_hours) < 3:
        return None

    hour_counts: dict[int, int] = {}
    for hour in posting_hours:
        hour_counts[hour] = hour_counts.get(hour, 0) + 1

    return posting_hours, hour_counts


def generate_posting_chart(jobs: list[dict]) -> str | None:
    """Generate a Unicode bar chart showing posting time distribution."""
    result = generate_posting_insights(jobs)
    if not result:
        return None

    posting_hours, hour_counts = result

    labels = []
    data = []
    for hour in range(24):
        count = hour_counts.get(hour, 0)
        if count > 0:
            if hour == 0:
                label = "12 AM"
            elif hour < 12:
                label = f"{hour} AM"
            elif hour == 12:
                label = "12 PM"
            else:
                label = f"{hour - 12} PM"
            labels.append(label)
            data.append(count)

    max_value = max(data) if data else 1
    max_bar_length = 40

    chart_lines = [
        "## Posting Time Distribution",
        "",
        f"### Job Posting Times (Based on {len(posting_hours)} postings)",
        "",
        "```",
        "Jobs",
        "",
    ]

    for label, count in zip(labels, data):
        bar_length = int((count / max_value) * max_bar_length) if max_value > 0 else 0
        bar = "\u2588" * bar_length
        chart_lines.append(f"{label:>6} \u2502{bar} {count}")

    chart_lines.extend(
        [
            "       \u2514" + "\u2500" * max_bar_length,
            "        0" + " " * (max_bar_length - 1) + str(max_value),
            "```",
        ]
    )

    return "\n".join(chart_lines)


def update_readme(jobs: list[dict]) -> None:
    """Update the README file with the latest job listings."""
    sorted_jobs = sorted(
        jobs,
        key=lambda x: x.get("published_parsed") or (0, 0, 0, 0, 0, 0),
        reverse=True,
    )

    table_rows = []
    for job in sorted_jobs:
        position = job["position"].replace("|", "-")
        company = job["company"].replace("|", "-")
        posted = format_posted_date(job.get("posted_date", ""), job.get("published_parsed"))
        link = job["link"]
        table_rows.append(f"| {company} | {position} | {posted} | [Apply]({link}) |")

    table_text = "\n".join(table_rows)

    cst = ZoneInfo("America/Chicago")
    cst_time = datetime.now(cst)
    last_updated = cst_time.strftime("%B %d, %Y at %I:%M %p CST")

    chart = generate_posting_chart(jobs)
    stats_text = f"\n{chart}\n" if chart else ""

    readme_content = README_TEMPLATE.format(
        last_updated=last_updated,
        total_positions=len(jobs),
        job_table=table_text,
        posting_stats=stats_text,
    )

    with open(README_FILE, "w") as f:
        f.write(readme_content)


def send_email(new_jobs: list[dict]) -> None:
    """Send email notification for new jobs using SMTP."""
    sender_email = os.environ.get("EMAIL_SENDER")
    sender_password = os.environ.get("EMAIL_PASSWORD")
    recipients_env = os.environ.get("EMAIL_RECIPIENTS", "")
    app_url = os.environ.get("APP_URL", "").rstrip("/")

    if not sender_email or not sender_password:
        logger.warning("Email credentials not found. Skipping notification.")
        return

    # Admin recipients from env var
    admin_recipients = [r.strip() for r in recipients_env.split(",") if r.strip()]

    # DB subscribers
    db_subscribers = get_active_subscribers()

    if not admin_recipients and not db_subscribers:
        logger.warning("No recipients found. Skipping notification.")
        return

    count = len(new_jobs)
    subject = f"\U0001f393 {count} New Research Park Job{'s' if count > 1 else ''} Found!"

    def build_html(unsubscribe_link: str | None = None) -> str:
        html = f"""
    <html>
      <body style="font-family: Arial, sans-serif; line-height: 1.6;">
        <h2 style="color: #13294b;">New Job Posting{'s' if count > 1 else ''} at Research Park</h2>
        <p>The following new position{'s were' if count > 1 else ' was'} just detected:</p>
        <ul style="list-style-type: none; padding: 0;">
    """
        for job in new_jobs:
            html += f"""
          <li style="margin-bottom: 15px; border-left: 4px solid #E84A27; padding-left: 10px;">
            <strong>{job['company']}</strong><br>
            {job['position']}
          </li>
        """
        html += f"""
        </ul>
        <p><a href="{app_url}" style="display: inline-block; background-color: #13294b; color: #fff; padding: 10px 20px; border-radius: 6px; text-decoration: none; font-weight: bold;">View the Job Board</a></p>
        <p style="color: #666; font-size: 12px; margin-top: 30px;">
          This is an automated notification from your Research Park Job Monitor.
        </p>
    """
        if unsubscribe_link:
            html += f"""
        <p style="color: #999; font-size: 11px;">
          <a href="{unsubscribe_link}" style="color: #999;">Unsubscribe from these notifications</a>
        </p>
    """
        html += """
      </body>
    </html>
    """
        return html

    try:
        logger.info("Connecting to SMTP server...")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, sender_password)

            # Send to admin recipients (no unsubscribe link)
            for recipient in admin_recipients:
                msg = MIMEMultipart()
                msg["From"] = sender_email
                msg["To"] = recipient
                msg["Subject"] = subject
                msg.attach(MIMEText(build_html(), "html"))
                server.send_message(msg)

            # Send to DB subscribers (with unsubscribe link)
            for sub in db_subscribers:
                # Skip if already in admin list
                if sub["email"] in admin_recipients:
                    continue
                unsubscribe_url = (
                    f"{app_url}/unsubscribe?token={sub['unsubscribe_token']}"
                    if app_url
                    else None
                )
                msg = MIMEMultipart()
                msg["From"] = sender_email
                msg["To"] = sub["email"]
                msg["Subject"] = subject
                msg.attach(MIMEText(build_html(unsubscribe_url), "html"))
                server.send_message(msg)

        total = len(admin_recipients) + len(db_subscribers)
        logger.info("Email notification sent to %d recipient(s)", total)
    except Exception as e:
        logger.error("Failed to send email: %s", e)


def main() -> None:
    logger.info("=" * 60)
    logger.info("UIUC Research Park Job Monitor")
    logger.info("=" * 60)

    init_db()

    logger.info("Fetching all job listings from job board...")
    current_jobs = parse_job_board()
    existing_jobs = load_existing_jobs()

    # Preserve metadata from existing jobs
    existing_ids = {job["id"] for job in existing_jobs}
    existing_jobs_dict = {job["id"]: job for job in existing_jobs}

    for job in current_jobs:
        if job["id"] not in existing_ids:
            job["discovered_date"] = datetime.now().isoformat()
        else:
            existing_job = existing_jobs_dict.get(job["id"])
            if existing_job:
                if "discovered_date" in existing_job:
                    job["discovered_date"] = existing_job["discovered_date"]

    new_jobs = find_new_jobs(current_jobs, existing_jobs)

    if new_jobs:
        logger.info("%d new job(s) detected:", len(new_jobs))
        for job in new_jobs:
            logger.info("  - %s - %s", job["company"], job["position"])

        send_email(new_jobs)

        with open("new_jobs.json", "w") as f:
            json.dump(new_jobs, f, indent=2)
    else:
        logger.info("No new jobs (scanned %d listings)", len(current_jobs))
        Path("new_jobs.json").touch()

    update_readme(current_jobs)
    save_jobs(current_jobs)
    logger.info("=" * 60)
    logger.info("Update complete!")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
