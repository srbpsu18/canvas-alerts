import os
import sys
import json
import re
import time
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from html import unescape
from pathlib import Path

import requests
from dateutil import parser as dtparse

# --- Config ---

CANVAS_API_TOKEN = os.environ["CANVAS_API_TOKEN"]
CANVAS_BASE_URL = os.environ.get("CANVAS_BASE_URL", "https://psu.instructure.com/api/v1")
EMAIL_SENDER = os.environ["EMAIL_SENDER"]
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]
EMAIL_RECIPIENTS = [e.strip() for e in os.environ["EMAIL_RECIPIENTS"].split(",")]
DIGEST_MODE = os.environ.get("DIGEST_MODE", "")
STATE_FILE = Path(__file__).parent / "state.json"

ET = ZoneInfo("US/Eastern")
HEADERS = {"Authorization": f"Bearer {CANVAS_API_TOKEN}"}
HIGH_STAKES_THRESHOLD = 10

COLORS = {
    "missed": "#dc3545",
    "today": "#dc3545",
    "tomorrow": "#e67e22",
    "soon": "#3498db",
    "new": "#27ae60",
    "announcement": "#6c757d",
    "warning": "#f0ad4e",
}


# --- API helpers ---

def fetch_paginated(url, params=None):
    """GET with Link-header pagination and single retry."""
    results = []
    params = params or {}
    while url:
        for attempt in range(2):
            try:
                resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
                resp.raise_for_status()
                break
            except requests.RequestException:
                if attempt == 0:
                    time.sleep(2)
                else:
                    raise
        results.extend(resp.json())
        # follow pagination
        url = None
        params = {}  # params are baked into the Link URL
        link = resp.headers.get("Link", "")
        for part in link.split(","):
            if 'rel="next"' in part:
                url = part.split("<")[1].split(">")[0]
    return results


def fetch_active_courses():
    url = f"{CANVAS_BASE_URL}/courses"
    courses = fetch_paginated(url, {"enrollment_state": "active", "per_page": 100})
    now = datetime.now(ET)
    active = []
    for c in courses:
        # filter to courses currently in session
        start = dtparse.parse(c["start_at"]).astimezone(ET) if c.get("start_at") else None
        end = dtparse.parse(c["end_at"]).astimezone(ET) if c.get("end_at") else None
        if start and start > now:
            continue
        if end and end < now:
            continue
        active.append(c)
    return active


def fetch_assignments(course_id, bucket):
    url = f"{CANVAS_BASE_URL}/courses/{course_id}/assignments"
    return fetch_paginated(url, {
        "per_page": 100,
        "include[]": "submission",
        "bucket": bucket,
    })


def fetch_announcements(course_id, since_ts):
    url = f"{CANVAS_BASE_URL}/courses/{course_id}/discussion_topics"
    items = fetch_paginated(url, {
        "only_announcements": "true",
        "per_page": 100,
        "order_by": "recent_activity",
    })
    # filter to announcements posted since last run
    cutoff = dtparse.parse(since_ts) if isinstance(since_ts, str) else since_ts
    return [a for a in items if dtparse.parse(a["posted_at"]).astimezone(ET) > cutoff.astimezone(ET)]


def fetch_peer_reviews(course_id, assignment_id):
    url = f"{CANVAS_BASE_URL}/courses/{course_id}/assignments/{assignment_id}/peer_reviews"
    return fetch_paginated(url)


def fetch_todo_items():
    return fetch_paginated(f"{CANVAS_BASE_URL}/users/self/todo", {"per_page": 100})


def fetch_calendar_events():
    return fetch_paginated(f"{CANVAS_BASE_URL}/users/self/upcoming_events", {"per_page": 100})


# --- Helpers ---

def parse_dt(val):
    if not val:
        return None
    return dtparse.parse(val).astimezone(ET)


def effective_deadline(a):
    due = parse_dt(a.get("due_at"))
    lock = parse_dt(a.get("lock_at"))
    if due and lock:
        return min(due, lock)
    return due or lock


def is_submitted(a):
    sub = a.get("submission")
    if not sub:
        return False
    return sub.get("workflow_state") in ("submitted", "graded")


def strip_html(text):
    if not text:
        return ""
    clean = re.sub(r"<[^>]+>", "", unescape(text))
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def truncate(text, n=150):
    if len(text) <= n:
        return text
    return text[:n].rstrip() + "..."


def fmt_time(dt_obj):
    if not dt_obj:
        return ""
    return dt_obj.strftime("%-I:%M %p ET").lstrip("0")


def fmt_date(dt_obj):
    return dt_obj.strftime("%a %-I:%M %p ET")


def course_name(course):
    return course.get("course_code") or course.get("name", "Unknown Course")


# --- Categorization ---

def categorize(assignments, state, now):
    """Bucket assignments into sections for the morning digest."""
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)
    tomorrow_end = today_start + timedelta(days=2)
    soon_end = today_start + timedelta(days=4)  # 2-3 days out (today+1 through today+3)

    last_run = parse_dt(state.get("last_morning_run")) or (now - timedelta(days=1))
    seen_ids = set(state.get("seen_assignments", {}).keys())

    buckets = {
        "missed": [],
        "today_past": [],
        "today": [],
        "tomorrow": [],
        "soon": [],
        "new": [],
    }

    seen_in_new = set()  # track IDs already placed in "new" to avoid duplication

    for a in assignments:
        dl = effective_deadline(a)
        if not dl:
            continue

        aid = str(a["id"])
        submitted = is_submitted(a)

        # missed: unsubmitted, deadline between last_run and now
        if not submitted and last_run < dl <= now:
            buckets["missed"].append(a)
            continue

        # skip past-due submitted items
        if dl <= now:
            continue

        # new detection (future items not previously seen)
        if aid not in seen_ids:
            buckets["new"].append(a)
            seen_in_new.add(aid)

        # place in time-based bucket too (new items also appear in their time bucket)
        if today_start <= dl < today_end:
            if dl < now:
                buckets["today_past"].append(a)
            else:
                buckets["today"].append(a)
        elif today_end <= dl < tomorrow_end:
            buckets["tomorrow"].append(a)
        elif tomorrow_end <= dl < soon_end:
            buckets["soon"].append(a)

    # sort each bucket by deadline
    for key in buckets:
        buckets[key].sort(key=lambda a: effective_deadline(a) or now)

    return buckets


# --- Email HTML builders ---

def badge_html(text, color):
    return (
        f'<span style="display:inline-block;background:{color};color:#fff;'
        f'padding:2px 8px;border-radius:3px;font-size:12px;font-weight:bold;'
        f'margin-right:4px;">{text}</span>'
    )


def assignment_card(a, show_new=False, show_done=True):
    dl = effective_deadline(a)
    due_raw = parse_dt(a.get("due_at"))
    lock_raw = parse_dt(a.get("lock_at"))
    pts = a.get("points_possible")
    desc = truncate(strip_html(a.get("description", "")))
    sub_types = a.get("submission_types", [])
    sub_label = ", ".join(s.replace("_", " ").title() for s in sub_types if s != "none")
    course_label = a.get("_course_name", "")
    url = a.get("html_url", "")

    badges = ""
    if pts and pts >= HIGH_STAKES_THRESHOLD:
        badges += badge_html("HIGH STAKES", "#8b0000")
    if show_new:
        badges += badge_html("NEW", COLORS["new"])
    if show_done and is_submitted(a):
        badges += badge_html("&#10003; DONE", "#555")

    lock_line = ""
    if lock_raw and due_raw and lock_raw != due_raw:
        lock_line = f'<div style="color:#888;font-size:13px;">Locks: {fmt_date(lock_raw)}</div>'

    desc_line = f'<div style="color:#666;font-size:13px;margin-top:4px;">"{desc}"</div>' if desc else ""
    pts_str = f" &middot; {int(pts)} pts" if pts else ""
    sub_str = f" &middot; {sub_label}" if sub_label else ""

    return f"""
    <div style="background:#fff;border:1px solid #e0e0e0;border-radius:6px;padding:14px;margin-bottom:10px;">
        <div>{badges}</div>
        <div style="font-size:16px;font-weight:bold;margin-top:4px;">{a.get('name', 'Untitled')}</div>
        <div style="color:#555;font-size:13px;">{course_label} &middot; Due {fmt_date(dl)}{pts_str}{sub_str}</div>
        {lock_line}
        {desc_line}
        <div style="margin-top:8px;"><a href="{url}" style="color:#0066cc;font-size:13px;text-decoration:none;">View on Canvas &rarr;</a></div>
    </div>"""


def section_html(title, items, color, show_new=False, empty_hide=True):
    if not items and empty_hide:
        return ""
    cards = "\n".join(assignment_card(a, show_new=show_new) for a in items)
    return f"""
    <div style="border-left:4px solid {color};padding-left:16px;margin-bottom:24px;">
        <h2 style="color:{color};font-size:18px;margin:0 0 12px 0;">{title}</h2>
        {cards if cards else '<p style="color:#888;">None</p>'}
    </div>"""


def announcement_section(announcements_by_course):
    if not announcements_by_course:
        return ""
    html = f"""
    <div style="border-left:4px solid {COLORS['announcement']};padding-left:16px;margin-bottom:24px;">
        <h2 style="color:{COLORS['announcement']};font-size:18px;margin:0 0 12px 0;">ANNOUNCEMENTS</h2>"""
    for cname, anns in announcements_by_course.items():
        for ann in anns:
            title = ann.get("title", "Untitled")
            msg = truncate(strip_html(ann.get("message", "")), 200)
            url = ann.get("html_url", "")
            posted = parse_dt(ann.get("posted_at"))
            posted_str = posted.strftime("%b %-d, %-I:%M %p ET") if posted else ""
            html += f"""
        <div style="background:#fff;border:1px solid #e0e0e0;border-radius:6px;padding:14px;margin-bottom:10px;">
            <div style="font-size:15px;font-weight:bold;">{title}</div>
            <div style="color:#555;font-size:13px;">{cname} &middot; {posted_str}</div>
            <div style="color:#666;font-size:13px;margin-top:4px;">{msg}</div>
            <div style="margin-top:6px;"><a href="{url}" style="color:#0066cc;font-size:13px;text-decoration:none;">Read more &rarr;</a></div>
        </div>"""
    html += "</div>"
    return html


def warning_banner(failed_courses):
    if not failed_courses:
        return ""
    names = ", ".join(failed_courses)
    return f"""
    <div style="background:#fff3cd;border:1px solid {COLORS['warning']};border-radius:6px;padding:12px;margin-bottom:20px;">
        <strong style="color:#856404;">&#9888; Warning:</strong>
        <span style="color:#856404;">Failed to fetch data for: {names}. These courses are excluded from this digest.</span>
    </div>"""


def build_morning_html(buckets, announcements_by_course, failed_courses, total_courses, total_assignments):
    now = datetime.now(ET)
    date_str = now.strftime("%b %-d, %Y")

    all_empty = all(len(v) == 0 for v in buckets.values()) and not announcements_by_course
    if all_empty and not failed_courses:
        body = '<div style="background:#d4edda;border-radius:6px;padding:20px;text-align:center;color:#155724;font-size:16px;">No upcoming deadlines — you\'re all clear.</div>'
    else:
        body = warning_banner(failed_courses)
        body += section_html("&#9888;&#65039; MISSED", buckets["missed"], COLORS["missed"])
        body += section_html("&#9888;&#65039; DUE TODAY — PAST", buckets["today_past"], COLORS["today"])
        body += section_html("DUE TODAY", buckets["today"], COLORS["today"])
        body += section_html("DUE TOMORROW", buckets["tomorrow"], COLORS["tomorrow"])
        body += section_html("DUE IN 2-3 DAYS", buckets["soon"], COLORS["soon"])
        body += section_html("NEW ASSIGNMENTS", buckets["new"], COLORS["new"], show_new=True)
        body += announcement_section(announcements_by_course)

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f5f5f5;margin:0;padding:20px;">
<div style="max-width:640px;margin:0 auto;">
    <div style="background:#1a1a2e;color:#fff;padding:20px;border-radius:8px 8px 0 0;">
        <h1 style="margin:0;font-size:22px;">Canvas Daily Digest</h1>
        <div style="color:#ccc;font-size:14px;margin-top:4px;">{date_str} &middot; {total_courses} courses &middot; {total_assignments} assignments tracked</div>
    </div>
    <div style="background:#f9f9f9;padding:20px;border-radius:0 0 8px 8px;">
        {body}
    </div>
    <div style="text-align:center;color:#999;font-size:12px;margin-top:16px;">
        Sent by Canvas Alerts
    </div>
</div>
</body></html>"""


def build_evening_html(tomorrow_items):
    now = datetime.now(ET)
    date_str = now.strftime("%b %-d, %Y")

    if not tomorrow_items:
        return None  # signal: don't send

    cards = "\n".join(assignment_card(a, show_done=False) for a in tomorrow_items)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f5f5f5;margin:0;padding:20px;">
<div style="max-width:640px;margin:0 auto;">
    <div style="background:#e67e22;color:#fff;padding:20px;border-radius:8px 8px 0 0;">
        <h1 style="margin:0;font-size:22px;">Canvas Evening Alert</h1>
        <div style="color:#fff;opacity:0.85;font-size:14px;margin-top:4px;">{date_str} &middot; Due tomorrow</div>
    </div>
    <div style="background:#f9f9f9;padding:20px;border-radius:0 0 8px 8px;">
        <div style="border-left:4px solid {COLORS['tomorrow']};padding-left:16px;margin-bottom:24px;">
            <h2 style="color:{COLORS['tomorrow']};font-size:18px;margin:0 0 12px 0;">DUE TOMORROW (unsubmitted)</h2>
            {cards}
        </div>
    </div>
    <div style="text-align:center;color:#999;font-size:12px;margin-top:16px;">
        Sent by Canvas Alerts
    </div>
</div>
</body></html>"""


def build_error_html(error_msg):
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f5f5f5;margin:0;padding:20px;">
<div style="max-width:640px;margin:0 auto;">
    <div style="background:#dc3545;color:#fff;padding:20px;border-radius:8px;">
        <h1 style="margin:0;font-size:20px;">Canvas Alerts Failed</h1>
        <p style="margin:8px 0 0 0;opacity:0.9;">{error_msg}</p>
        <p style="margin:8px 0 0 0;opacity:0.7;font-size:13px;">Check your API token and GitHub secrets.</p>
    </div>
</div>
</body></html>"""


# --- Email sender ---

def send_email(subject, html, recipients):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, recipients, msg.as_string())
    print(f"Email sent to {', '.join(recipients)}")


# --- Main execution ---

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_morning_run": None, "last_evening_run": None, "seen_assignments": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str) + "\n")


def determine_mode():
    if DIGEST_MODE in ("morning", "evening"):
        return DIGEST_MODE
    now = datetime.now(ET)
    hour = now.hour
    if 6 <= hour < 12:
        return "morning"
    if 18 <= hour < 23:
        return "evening"
    print(f"Current ET hour is {hour} — outside digest windows, exiting.")
    sys.exit(0)


def main():
    now = datetime.now(ET)
    mode = determine_mode()
    print(f"Mode: {mode} | Time: {now.strftime('%Y-%m-%d %I:%M %p ET')}")

    state = load_state()

    # fetch courses
    try:
        courses = fetch_active_courses()
    except Exception as e:
        print(f"Fatal: cannot fetch courses — {e}")
        send_email(
            f"Canvas Alerts Error — {now.strftime('%b %-d, %Y')}",
            build_error_html(f"Could not fetch courses: {e}"),
            EMAIL_RECIPIENTS,
        )
        return

    print(f"Active courses: {len(courses)}")
    course_map = {c["id"]: course_name(c) for c in courses}

    # fetch per-course data
    all_assignments = {}  # id -> assignment dict
    announcements_by_course = {}
    failed_courses = []

    for c in courses:
        cid = c["id"]
        cname = course_name(c)
        try:
            for bucket in ("upcoming", "past"):
                for a in fetch_assignments(cid, bucket):
                    a["_course_name"] = cname
                    all_assignments[a["id"]] = a

            # peer reviews
            for aid, a in list(all_assignments.items()):
                if a.get("peer_reviews") and a.get("_course_name") == cname:
                    try:
                        reviews = fetch_peer_reviews(cid, aid)
                        if reviews:
                            a["_peer_reviews"] = reviews
                    except Exception:
                        pass  # non-critical

            # announcements
            last_run = state.get(f"last_{mode}_run") or (now - timedelta(days=1)).isoformat()
            anns = fetch_announcements(cid, last_run)
            if anns:
                announcements_by_course[cname] = anns

        except Exception as e:
            print(f"Failed to fetch data for {cname}: {e}")
            failed_courses.append(cname)

    # merge todo items
    try:
        for item in fetch_todo_items():
            a = item.get("assignment")
            if a and a["id"] not in all_assignments:
                cname = course_map.get(item.get("course_id"), "Unknown")
                a["_course_name"] = cname
                all_assignments[a["id"]] = a
    except Exception as e:
        print(f"Todo fetch failed (non-critical): {e}")

    # merge calendar events
    try:
        for ev in fetch_calendar_events():
            if ev.get("assignment"):
                a = ev["assignment"]
                if a["id"] not in all_assignments:
                    a["_course_name"] = course_map.get(a.get("course_id"), "Unknown")
                    all_assignments[a["id"]] = a
    except Exception as e:
        print(f"Calendar fetch failed (non-critical): {e}")

    assignments = list(all_assignments.values())
    print(f"Total assignments: {len(assignments)}")

    if mode == "morning":
        buckets = categorize(assignments, state, now)
        html = build_morning_html(buckets, announcements_by_course, failed_courses, len(courses), len(assignments))
        subject = f"Canvas Daily Digest — {now.strftime('%b %-d, %Y')}"
        send_email(subject, html, EMAIL_RECIPIENTS)

        # update state
        state["last_morning_run"] = now.isoformat()
        for a in assignments:
            aid = str(a["id"])
            dl = effective_deadline(a)
            if aid not in state["seen_assignments"]:
                state["seen_assignments"][aid] = {
                    "name": a.get("name", ""),
                    "course": a.get("_course_name", ""),
                    "due_at": dl.isoformat() if dl else None,
                    "first_seen": now.isoformat(),
                }

    elif mode == "evening":
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_start = today_start + timedelta(days=1)
        tomorrow_end = today_start + timedelta(days=2)

        tomorrow_unsubmitted = [
            a for a in assignments
            if not is_submitted(a)
            and effective_deadline(a)
            and tomorrow_start <= effective_deadline(a) < tomorrow_end
        ]
        tomorrow_unsubmitted.sort(key=lambda a: effective_deadline(a))

        html = build_evening_html(tomorrow_unsubmitted)
        if html is None:
            print("No unsubmitted items due tomorrow — skipping evening email.")
        else:
            subject = f"Canvas Evening Alert — {now.strftime('%b %-d, %Y')}"
            send_email(subject, html, EMAIL_RECIPIENTS)

        state["last_evening_run"] = now.isoformat()

    save_state(state)
    print("State saved.")


if __name__ == "__main__":
    main()
