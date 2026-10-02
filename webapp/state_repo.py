"""
GitHub-API-backed access to the private mime-state repo (outreach_queue.csv,
outreach_log.csv). Vercel functions are stateless/serverless with no
persistent disk between invocations, so every read/write goes straight over
HTTPS to GitHub's Contents API instead of a local git clone. mime-state stays
the single source of truth shared with the GitHub Actions daily pipeline.
"""
import os
import csv
import io
import base64
import requests

REPO = "Asheryram/mime-state"
API_ROOT = f"https://api.github.com/repos/{REPO}/contents"
BRANCH = "main"

QUEUE_FIELDS = [
    "company_name", "website", "email", "company_type",
    "recipient_name", "company_address", "status",
    "date_added", "date_sent", "date_followup", "message_id",
    "track", "lead_reviewed", "replied", "bounced",
]


def _headers():
    return {
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
    }


def _get_file(path):
    """Returns (text_content, sha) or (None, None) if the file doesn't exist yet."""
    r = requests.get(f"{API_ROOT}/{path}", headers=_headers(), params={"ref": BRANCH}, timeout=15)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    data = r.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return content, data["sha"]


def _put_file(path, text_content, sha, message):
    payload = {
        "message": message,
        "content": base64.b64encode(text_content.encode("utf-8")).decode("ascii"),
        "branch": BRANCH,
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(f"{API_ROOT}/{path}", headers=_headers(), json=payload, timeout=15)
    r.raise_for_status()
    return r.json()["content"]["sha"]


def load_queue():
    text, _ = _get_file("outreach_queue.csv")
    rows = []
    if text:
        for row in csv.DictReader(io.StringIO(text)):
            for field in QUEUE_FIELDS:
                row.setdefault(field, "")
            rows.append(row)
    return rows


def save_queue(rows, commit_message):
    _, sha = _get_file("outreach_queue.csv")
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=QUEUE_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    _put_file("outreach_queue.csv", buf.getvalue(), sha, commit_message)


def update_company(slug, changes, commit_message):
    """Find the row whose slugified company_name matches `slug` and merge `changes` into it."""
    rows = load_queue()
    found = None
    for row in rows:
        if slugify(row["company_name"]) == slug:
            row.update(changes)
            found = row
            break
    if found is None:
        raise KeyError(f"No company matching '{slug}'")
    save_queue(rows, commit_message)
    return found


def slugify(name):
    import re
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:120] or "company"
