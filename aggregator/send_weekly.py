#!/usr/bin/env python3
"""Compose and send the weekly digest email via the Buttondown API.

Takes the matches first seen in the last 7 days from jobs.db. If there are
none, exits without sending. Requires BUTTONDOWN_API_KEY unless --dry-run.

Usage:
    python3 send_weekly.py --dry-run   # print the email, send nothing
    python3 send_weekly.py             # create + send via Buttondown
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "jobs.db"
SITE_URL = "https://csabahegedus.github.io/job-aggregator/"
API_URL = "https://api.buttondown.com/v1/emails"


def collect_week():
    con = sqlite3.connect(DB_PATH)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    return con.execute(
        "SELECT source, title, url, llm_json FROM jobs "
        "WHERE is_mobile=1 AND is_contract=1 AND first_seen >= ? "
        "ORDER BY posted_at DESC",
        (cutoff,),
    ).fetchall()


def compose(rows):
    today = datetime.now(timezone.utc).strftime("%B %-d, %Y")
    lines = [
        f"Here {'is the' if len(rows) == 1 else 'are the'} **{len(rows)}** new contract/freelance "
        f"gig{'s' if len(rows) != 1 else ''} for iOS & mobile developers found this week:",
        "",
    ]
    for source, title, url, llm_json in rows:
        meta = json.loads(llm_json) if llm_json else {}
        badge = "🍎 native iOS" if meta.get("is_native_ios") else "📱 mobile"
        rate = meta.get("rate_or_budget")
        lines.append(f"### [{title}]({url})")
        lines.append(f"*{badge} · {source}*" + (f" · **{rate}**" if rate else ""))
        if meta.get("summary"):
            lines.append("")
            lines.append(meta["summary"])
        lines.append("")
    lines += [
        "---",
        "",
        f"Browse all open listings anytime: [iOS Contract Digest]({SITE_URL})",
        "",
        "*Aggregated daily from Hacker News, RemoteOK, WeWorkRemotely, Remotive and Reddit. "
        "No full-time-only listings, no self-ads.*",
    ]
    subject = f"iOS Contract Digest — {len(rows)} new gig{'s' if len(rows) != 1 else ''} ({today})"
    return subject, "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print the email instead of sending")
    args = parser.parse_args()

    rows = collect_week()
    if not rows:
        print("No new matches in the last 7 days — skipping this week's email.")
        return

    subject, body = compose(rows)
    if args.dry_run:
        print(f"SUBJECT: {subject}\n\n{body}")
        return

    api_key = os.environ.get("BUTTONDOWN_API_KEY")
    if not api_key:
        sys.exit("BUTTONDOWN_API_KEY is not set")
    r = requests.post(
        API_URL,
        headers={"Authorization": f"Token {api_key}"},
        json={"subject": subject, "body": body, "status": "about_to_send"},
        timeout=30,
    )
    if r.status_code not in (200, 201):
        sys.exit(f"Buttondown API error {r.status_code}: {r.text[:500]}")
    print(f"Sent: {subject} (email id: {r.json().get('id')})")


if __name__ == "__main__":
    main()
