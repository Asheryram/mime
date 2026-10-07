"""
GitHub-API-backed access to the private companion repo (outreach_queue.csv,
outreach_log.csv, search_queries.txt, the CV) and to the pipeline repo's
Actions runs. Vercel functions are stateless with no persistent disk, so every
read and write goes over HTTPS to GitHub instead of a local git clone. The
companion repo stays the single source of truth shared with the scheduled
GitHub Actions workflows.
"""
import os
import re
import csv
import io
import base64
from urllib.parse import urlparse

import requests

REPO = os.environ.get("STATE_REPO", "Asheryram/mime-state")
PIPELINE_REPO = os.environ.get("PIPELINE_REPO", "Asheryram/mime")
API = "https://api.github.com"
BRANCH = "main"

QUEUE_PATH = "outreach_queue.csv"
LOG_PATH = "outreach_log.csv"
QUERIES_PATH = "search_queries.txt"
CV_PATH = os.environ.get("CV_FILENAME", "YramAsherTettehAbotsi_resume.pdf")

QUEUE_FIELDS = [
    "company_name", "website", "email", "company_type",
    "recipient_name", "company_address", "status",
    "date_added", "date_sent", "date_followup", "message_id",
    "track", "lead_reviewed", "replied", "bounced", "review_notified",
]


class ConflictError(Exception):
    """The file changed on GitHub between our read and our write."""


class TokenPermissionError(Exception):
    """GITHUB_TOKEN can't do what was asked (usually missing Actions access)."""


def _headers():
    return {
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _contents_url(path):
    return f"{API}/repos/{REPO}/contents/{path}"


def _get_file_bytes(path):
    """Returns (raw_bytes, sha), or (None, None) if the file doesn't exist yet."""
    r = requests.get(_contents_url(path), headers=_headers(), params={"ref": BRANCH}, timeout=15)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    data = r.json()
    return base64.b64decode(data["content"]), data["sha"]


def _put_file_bytes(path, raw_bytes, sha, message):
    payload = {
        "message": message,
        "content": base64.b64encode(raw_bytes).decode("ascii"),
        "branch": BRANCH,
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(_contents_url(path), headers=_headers(), json=payload, timeout=20)
    if r.status_code in (409, 422) and "sha" in r.text.lower():
        raise ConflictError(path)
    r.raise_for_status()
    return r.json()["content"]["sha"]


# --- Queue ---------------------------------------------------------------

def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:120] or "company"


def host_of(url):
    host = urlparse(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _parse_queue(text):
    rows = []
    for row in csv.DictReader(io.StringIO(text or "")):
        for field in QUEUE_FIELDS:
            if row.get(field) is None:
                row[field] = ""
        rows.append(row)
    return rows


def _serialize_queue(rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=QUEUE_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def load_queue():
    raw, _ = _get_file_bytes(QUEUE_PATH)
    return _parse_queue(raw.decode("utf-8") if raw else "")


def mutate_queue(fn, message, attempts=3):
    """
    Read the queue, let `fn` change the rows in place, and write it back pinned
    to the sha we read. If the pipeline wrote in between, GitHub rejects the
    write; re-read and re-apply rather than overwrite its changes.
    """
    for attempt in range(attempts):
        raw, sha = _get_file_bytes(QUEUE_PATH)
        rows = _parse_queue(raw.decode("utf-8") if raw else "")
        result = fn(rows)
        try:
            _put_file_bytes(QUEUE_PATH, _serialize_queue(rows).encode("utf-8"), sha, message)
            return result
        except ConflictError:
            if attempt == attempts - 1:
                raise


def _find(rows, slug):
    for row in rows:
        if slugify(row["company_name"]) == slug:
            return row
    raise KeyError(slug)


def update_company(slug, changes, message):
    """Merge `changes` into one row. Raises ValueError if a rename collides."""
    def fn(rows):
        row = _find(rows, slug)
        new_name = changes.get("company_name")
        if new_name is not None and slugify(new_name) != slug:
            if any(slugify(r["company_name"]) == slugify(new_name) for r in rows):
                raise ValueError(f"Another company is already named '{new_name}'.")
        row.update(changes)
        return dict(row)
    return mutate_queue(fn, message)


def add_company(row, message):
    """Append a row. Raises ValueError on a duplicate name or website."""
    def fn(rows):
        slug = slugify(row["company_name"])
        host = host_of(row.get("website"))
        for r in rows:
            if slugify(r["company_name"]) == slug:
                raise ValueError(f"'{r['company_name']}' is already in the queue.")
            if host and host_of(r["website"]) == host:
                raise ValueError(f"{host} is already in the queue as '{r['company_name']}'.")
        full = {f: "" for f in QUEUE_FIELDS}
        full.update(row)
        rows.append(full)
        return full
    return mutate_queue(fn, message)


def delete_company(slug, message):
    def fn(rows):
        row = _find(rows, slug)
        rows.remove(row)
        return row
    return mutate_queue(fn, message)


# --- Send log, search queries, CV ------------------------------------------

def load_log():
    raw, _ = _get_file_bytes(LOG_PATH)
    if not raw:
        return []
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))


def get_queries_text():
    """The search_queries.txt the scrape workflow reads, or None if not created yet."""
    raw, _ = _get_file_bytes(QUERIES_PATH)
    return raw.decode("utf-8") if raw is not None else None


def save_queries_text(text, message):
    _, sha = _get_file_bytes(QUERIES_PATH)
    _put_file_bytes(QUERIES_PATH, text.encode("utf-8"), sha, message)


def get_cv():
    """Returns (pdf_bytes, sha) for the current CV, or (None, None) if none exists yet."""
    return _get_file_bytes(CV_PATH)


def save_cv(pdf_bytes, message):
    _, sha = _get_file_bytes(CV_PATH)
    _put_file_bytes(CV_PATH, pdf_bytes, sha, message)


# --- History and pipeline runs ----------------------------------------------

def recent_commits(limit=20):
    """Every change to the companion repo, newest first: pipeline runs and dashboard edits alike."""
    r = requests.get(
        f"{API}/repos/{REPO}/commits", headers=_headers(),
        params={"sha": BRANCH, "per_page": limit}, timeout=15,
    )
    r.raise_for_status()
    out = []
    for c in r.json():
        out.append({
            "message": c["commit"]["message"].split("\n")[0],
            "date": c["commit"]["committer"]["date"],
            "author": c["commit"]["author"]["name"],
        })
    return out


def latest_run(workflow_file):
    """The most recent run of one workflow, or {'error': ...} if the token can't see Actions."""
    r = requests.get(
        f"{API}/repos/{PIPELINE_REPO}/actions/workflows/{workflow_file}/runs",
        headers=_headers(), params={"per_page": 1}, timeout=15,
    )
    if r.status_code in (403, 404):
        return {"error": "no_access"}
    r.raise_for_status()
    runs = r.json().get("workflow_runs", [])
    if not runs:
        return None
    run = runs[0]
    return {
        "status": run["status"],
        "conclusion": run["conclusion"],
        "created_at": run["created_at"],
        "html_url": run["html_url"],
        "event": run["event"],
    }


def dispatch_workflow(workflow_file):
    r = requests.post(
        f"{API}/repos/{PIPELINE_REPO}/actions/workflows/{workflow_file}/dispatches",
        headers=_headers(), json={"ref": BRANCH}, timeout=15,
    )
    if r.status_code in (403, 404):
        raise TokenPermissionError(PIPELINE_REPO)
    r.raise_for_status()


def actions_url(workflow_file):
    return f"https://github.com/{PIPELINE_REPO}/actions/workflows/{workflow_file}"
