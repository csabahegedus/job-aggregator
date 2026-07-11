#!/usr/bin/env python3
"""iOS contract/freelance job aggregator — fetch, filter, classify, store.

Sources: HN "Who is hiring?" + "Freelancer? Seeking freelancer?" (Algolia API),
RemoteOK (JSON API), WeWorkRemotely (RSS), Reddit (JSON endpoints).

Classification: keyword heuristic by default; if ANTHROPIC_API_KEY is set and
the `anthropic` package is installed, borderline items are classified with
Claude Haiku as well.

Usage:
    python3 pipeline.py            # fetch all sources, update jobs.db, write digest.md
    python3 pipeline.py --digest   # only regenerate digest.md from the DB
"""

import argparse
import html
import json
import os
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "jobs.db"
DIGEST_PATH = BASE_DIR / "digest.md"
SITE_PATH = BASE_DIR.parent / "docs" / "index.html"
UA = {"User-Agent": "ios-contract-aggregator/0.1 (personal job search tool)"}

MOBILE_KEYWORDS = re.compile(
    r"\b(ios|swift|swiftui|uikit|objective-?c|xcode|react native|flutter|mobile (dev|engineer|developer))\b",
    re.IGNORECASE,
)
CONTRACT_KEYWORDS = re.compile(
    r"\b(contract|contractor|freelance|freelancer|part[- ]time|hourly|consultant|consulting|gig|short[- ]term|project[- ]based)\b",
    re.IGNORECASE,
)
FULLTIME_ONLY = re.compile(r"\b(full[- ]time only|no contractors|w2 only|permanent role)\b", re.IGNORECASE)


# ---------------------------------------------------------------- fetchers

def fetch_hn():
    """HN 'Who is hiring?' and 'Freelancer? Seeking freelancer?' latest threads."""
    jobs = []
    # Official monthly threads are posted by the 'whoishiring' bot — filter by
    # author, then pick the newest story whose title matches.
    try:
        r = requests.get(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={"tags": "story,author_whoishiring", "hitsPerPage": 10},
            headers=UA, timeout=30,
        )
        r.raise_for_status()
        stories = r.json().get("hits", [])
    except requests.RequestException as e:
        print(f"[warn] HN thread lookup failed: {e}", file=sys.stderr)
        return jobs
    for title_marker in ("who is hiring", "freelancer"):
        try:
            story = next((s for s in stories if title_marker in (s.get("title") or "").lower()), None)
            if story is None:
                continue
            story_id = story["objectID"]
            # all top-level comments of the thread
            page = 0
            while page < 5:
                cr = requests.get(
                    "https://hn.algolia.com/api/v1/search_by_date",
                    params={"tags": f"comment,story_{story_id}", "hitsPerPage": 1000, "page": page},
                    headers=UA, timeout=30,
                )
                cr.raise_for_status()
                data = cr.json()
                for c in data.get("hits", []):
                    if c.get("parent_id") != int(story_id):
                        continue  # only top-level comments are job posts
                    text = html.unescape(re.sub(r"<[^>]+>", " ", c.get("comment_text") or ""))
                    jobs.append({
                        "id": f"hn-{c['objectID']}",
                        "source": f"HN ({story.get('title', '')})",
                        "title": text.strip().split("\n")[0][:120],
                        "url": f"https://news.ycombinator.com/item?id={c['objectID']}",
                        "posted_at": c.get("created_at", ""),
                        "text": text[:4000],
                    })
                if page + 1 >= data.get("nbPages", 1):
                    break
                page += 1
        except requests.RequestException as e:
            print(f"[warn] HN fetch failed ({title_marker}): {e}", file=sys.stderr)
    return jobs


def fetch_remoteok():
    try:
        r = requests.get("https://remoteok.com/api", headers=UA, timeout=30)
        r.raise_for_status()
        items = r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[warn] RemoteOK fetch failed: {e}", file=sys.stderr)
        return []
    jobs = []
    for it in items:
        if not isinstance(it, dict) or "id" not in it:
            continue  # first element is a legal notice
        text = html.unescape(re.sub(r"<[^>]+>", " ", it.get("description") or ""))
        jobs.append({
            "id": f"remoteok-{it['id']}",
            "source": "RemoteOK",
            "title": f"{it.get('position', '')} @ {it.get('company', '')}".strip(" @"),
            "url": it.get("url", ""),
            "posted_at": it.get("date", ""),
            "text": " ".join(it.get("tags", [])) + " " + text[:4000],
        })
    return jobs


def fetch_wwr():
    jobs = []
    for feed in ("https://weworkremotely.com/categories/remote-programming-jobs.rss",
                 "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss"):
        try:
            r = requests.get(feed, headers=UA, timeout=30)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except (requests.RequestException, ET.ParseError) as e:
            print(f"[warn] WWR fetch failed: {e}", file=sys.stderr)
            continue
        for item in root.iter("item"):
            link = (item.findtext("link") or "").strip()
            title = html.unescape(item.findtext("title") or "")
            desc = html.unescape(re.sub(r"<[^>]+>", " ", item.findtext("description") or ""))
            jobs.append({
                "id": f"wwr-{link.rstrip('/').rsplit('/', 1)[-1]}",
                "source": "WeWorkRemotely",
                "title": title[:120],
                "url": link,
                "posted_at": item.findtext("pubDate") or "",
                "text": desc[:4000],
            })
    return jobs


def fetch_remotive():
    try:
        r = requests.get("https://remotive.com/api/remote-jobs",
                         params={"category": "software-dev", "limit": 200},
                         headers=UA, timeout=30)
        r.raise_for_status()
        items = r.json().get("jobs", [])
    except (requests.RequestException, ValueError) as e:
        print(f"[warn] Remotive fetch failed: {e}", file=sys.stderr)
        return []
    jobs = []
    for it in items:
        text = html.unescape(re.sub(r"<[^>]+>", " ", it.get("description") or ""))
        job_type = it.get("job_type") or ""
        jobs.append({
            "id": f"remotive-{it['id']}",
            "source": "Remotive",
            "title": f"{it.get('title', '')} @ {it.get('company_name', '')}".strip(" @"),
            "url": it.get("url", ""),
            "posted_at": it.get("publication_date", ""),
            # prepend the structured job_type so the contract keywords can see it
            "text": f"job_type: {job_type}. " + " ".join(it.get("tags", [])) + " " + text[:4000],
        })
    return jobs


def fetch_reddit():
    # The JSON endpoints 403 unauthenticated clients; the RSS feeds still work.
    jobs = []
    for sub in ("iOSProgramming", "forhire", "jobbit"):
        root = None
        # reddit aggressively rate-limits unauthenticated clients: retry with backoff
        for attempt in range(1, 4):
            try:
                r = requests.get(f"https://www.reddit.com/r/{sub}/new/.rss?limit=50",
                                 headers=UA, timeout=30)
                r.raise_for_status()
                root = ET.fromstring(r.content)
                break
            except (requests.RequestException, ET.ParseError) as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status == 429 and attempt < 3:
                    time.sleep(30 * attempt)
                    continue
                print(f"[warn] Reddit r/{sub} fetch failed: {e}", file=sys.stderr)
                break
        if root is None:
            continue
        time.sleep(10)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("a:entry", ns):
            link = entry.find("a:link", ns)
            url = link.get("href") if link is not None else ""
            content = entry.findtext("a:content", default="", namespaces=ns)
            jobs.append({
                "id": f"reddit-{(entry.findtext('a:id', default=url, namespaces=ns)).rsplit('/', 1)[-1]}",
                "source": f"r/{sub}",
                "title": html.unescape(entry.findtext("a:title", default="", namespaces=ns))[:120],
                "url": url,
                "posted_at": entry.findtext("a:updated", default="", namespaces=ns),
                "text": html.unescape(re.sub(r"<[^>]+>", " ", content))[:4000],
            })
    return jobs


# ---------------------------------------------------------------- filtering

def heuristic_score(job):
    """Return (is_mobile, is_contract) based on keywords."""
    blob = f"{job['title']} {job['text']}"
    is_mobile = bool(MOBILE_KEYWORDS.search(blob))
    is_contract = bool(CONTRACT_KEYWORDS.search(blob)) and not FULLTIME_ONLY.search(blob)
    # HN freelancer thread: only "SEEKING FREELANCER" posts are gigs;
    # "SEEKING WORK" posts are other freelancers advertising themselves.
    if "freelancer" in job["source"].lower():
        is_contract = job["text"].lstrip().upper().startswith("SEEKING FREELANCER")
    if job["source"] == "r/forhire" and job["title"].lower().startswith("[hiring]"):
        is_contract = True
    # "[FOR HIRE]" posts are freelancers advertising themselves, not gigs
    if job["title"].lower().startswith("[for hire]"):
        is_contract = False
    return is_mobile, is_contract


def make_llm_classifier():
    """Return a classify(job) -> dict function using Claude Haiku, or None if unavailable."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        return None
    client = anthropic.Anthropic()
    schema = {
        "type": "object",
        "properties": {
            "is_mobile": {"type": "boolean",
                          "description": "True if the role is primarily mobile app development (native iOS, React Native, or Flutter)"},
            "is_native_ios": {"type": "boolean"},
            "is_contract_or_freelance": {"type": "boolean",
                                         "description": "True only for contract/freelance/part-time engagements, not permanent full-time roles"},
            "remote": {"type": "boolean"},
            "rate_or_budget": {"type": ["string", "null"]},
            "summary": {"type": "string"},
        },
        "required": ["is_mobile", "is_native_ios", "is_contract_or_freelance", "remote", "rate_or_budget", "summary"],
        "additionalProperties": False,
    }

    def classify(job):
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=512,
            system="You classify job postings for a senior native iOS developer "
                   "(Swift, SwiftUI, UIKit, Combine) looking for contract/freelance work. "
                   "Posts where a freelancer advertises their own services (e.g. titled "
                   "'[FOR HIRE]' or 'SEEKING WORK') are NOT opportunities: set "
                   "is_contract_or_freelance to false for those.",
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": f"Classify this posting:\n\n{job['title']}\n\n{job['text'][:3000]}"}],
        )
        text = next(b.text for b in resp.content if b.type == "text")
        return json.loads(text)

    return classify


# ---------------------------------------------------------------- storage

def open_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        source TEXT, title TEXT, url TEXT, posted_at TEXT,
        is_mobile INTEGER, is_contract INTEGER,
        llm_json TEXT, first_seen TEXT
    )""")
    return con


def write_digest(con):
    rows = con.execute(
        "SELECT source, title, url, posted_at, llm_json FROM jobs "
        "WHERE is_mobile=1 AND is_contract=1 ORDER BY posted_at DESC LIMIT 50"
    ).fetchall()
    lines = [f"# iOS contract digest — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
             "", f"{len(rows)} matching posting(s) (mobile + contract/freelance):", ""]
    for source, title, url, posted_at, llm_json in rows:
        extra = ""
        if llm_json:
            meta = json.loads(llm_json)
            rate = meta.get("rate_or_budget")
            extra = f" — {meta.get('summary', '')}" + (f" ({rate})" if rate else "")
        lines.append(f"- **[{title}]({url})** — {source}, {posted_at[:10]}{extra}")
    DIGEST_PATH.write_text("\n".join(lines) + "\n")
    write_site(con)
    return len(rows)


SITE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>iOS Contract Digest</title>
<meta name="description" content="Curated contract and freelance gigs for iOS and mobile developers, aggregated daily from HN, RemoteOK, WeWorkRemotely, Remotive and Reddit.">
<style>
:root {{ --bg:#faf9f7; --card:#fff; --text:#1f2328; --muted:#6a6f76; --accent:#0a66c2; --badge-ios:#0a66c2; --badge-mobile:#6a6f76; --border:#e4e2de; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#14161a; --card:#1d2127; --text:#e8e6e3; --muted:#9aa0a6; --accent:#6cb2ff; --border:#2c313a; }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
main {{ max-width:760px; margin:0 auto; padding:2.5rem 1.25rem 4rem; }}
h1 {{ font-size:1.9rem; margin:0 0 .25rem; }}
.sub {{ color:var(--muted); margin:0 0 2rem; }}
.job {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:1rem 1.25rem; margin-bottom:1rem; }}
.job h2 {{ font-size:1.05rem; margin:0 0 .35rem; }}
.job h2 a {{ color:var(--accent); text-decoration:none; }}
.job h2 a:hover {{ text-decoration:underline; }}
.meta {{ font-size:.85rem; color:var(--muted); margin-bottom:.5rem; }}
.badge {{ display:inline-block; font-size:.72rem; font-weight:600; padding:.1rem .5rem; border-radius:999px; color:#fff; margin-right:.5rem; vertical-align:1px; }}
.badge.ios {{ background:var(--badge-ios); }}
.badge.mobile {{ background:var(--badge-mobile); }}
.rate {{ font-weight:600; }}
p.summary {{ margin:.4rem 0 0; font-size:.95rem; }}
footer {{ margin-top:3rem; font-size:.85rem; color:var(--muted); }}
footer a {{ color:var(--accent); }}
</style>
</head>
<body>
<main>
<h1>iOS Contract Digest</h1>
<p class="sub">Contract &amp; freelance gigs for iOS and mobile developers — aggregated daily from Hacker News, RemoteOK, WeWorkRemotely, Remotive and Reddit. No full-time-only listings, no self-ads.</p>
{jobs}
<footer>Last updated: {updated} UTC · {count} open listing(s) ·
Built with a small open pipeline — <a href="https://github.com/csabahegedus/job-aggregator">source on GitHub</a>.</footer>
</main>
</body>
</html>
"""


def write_site(con):
    rows = con.execute(
        "SELECT source, title, url, posted_at, llm_json FROM jobs "
        "WHERE is_mobile=1 AND is_contract=1 ORDER BY posted_at DESC LIMIT 100"
    ).fetchall()
    cards = []
    for source, title, url, posted_at, llm_json in rows:
        meta = json.loads(llm_json) if llm_json else {}
        badge = ('<span class="badge ios">native iOS</span>' if meta.get("is_native_ios")
                 else '<span class="badge mobile">mobile</span>')
        rate = meta.get("rate_or_budget")
        rate_html = f' · <span class="rate">{html.escape(str(rate))}</span>' if rate else ""
        summary = meta.get("summary", "")
        summary_html = f'<p class="summary">{html.escape(summary)}</p>' if summary else ""
        cards.append(
            f'<article class="job"><h2>{badge}<a href="{html.escape(url)}">{html.escape(title)}</a></h2>'
            f'<div class="meta">{html.escape(source)} · {html.escape(posted_at[:10])}{rate_html}</div>'
            f"{summary_html}</article>"
        )
    SITE_PATH.parent.mkdir(exist_ok=True)
    SITE_PATH.write_text(SITE_TEMPLATE.format(
        jobs="\n".join(cards) or "<p>No open listings right now — check back tomorrow.</p>",
        updated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        count=len(rows),
    ))


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--digest", action="store_true", help="only regenerate digest.md from the DB")
    args = parser.parse_args()

    con = open_db()
    if args.digest:
        n = write_digest(con)
        print(f"digest.md regenerated ({n} matches)")
        return

    all_jobs = []
    for name, fn in (("HN", fetch_hn), ("RemoteOK", fetch_remoteok),
                     ("WWR", fetch_wwr), ("Remotive", fetch_remotive),
                     ("Reddit", fetch_reddit)):
        jobs = fn()
        print(f"{name}: {len(jobs)} postings fetched")
        all_jobs.extend(jobs)
        time.sleep(1)

    classify = make_llm_classifier()
    print(f"LLM classification: {'enabled (claude-haiku-4-5)' if classify else 'disabled (no ANTHROPIC_API_KEY / anthropic pkg) — heuristic only'}")

    new_matches = 0
    for job in all_jobs:
        if con.execute("SELECT 1 FROM jobs WHERE id=?", (job["id"],)).fetchone():
            continue
        is_mobile, is_contract = heuristic_score(job)
        llm_json = None
        # LLM refines only prefiltered mobile items (keeps cost near zero)
        if classify and is_mobile:
            try:
                meta = classify(job)
                # LLM verdict overrides the (deliberately loose) heuristic
                is_mobile = meta["is_mobile"]
                is_contract = meta["is_contract_or_freelance"]
                llm_json = json.dumps(meta, ensure_ascii=False)
            except Exception as e:
                print(f"[warn] LLM classify failed for {job['id']}: {e}", file=sys.stderr)
        con.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
            (job["id"], job["source"], job["title"], job["url"], job["posted_at"],
             int(is_mobile), int(is_contract), llm_json, datetime.now(timezone.utc).isoformat()),
        )
        if is_mobile and is_contract:
            new_matches += 1
            print(f"  MATCH: [{job['source']}] {job['title'][:80]}\n         {job['url']}")
    con.commit()

    total = write_digest(con)
    print(f"\n{new_matches} new match(es) this run; digest.md lists {total} total. DB: {DB_PATH.name}")


if __name__ == "__main__":
    main()
