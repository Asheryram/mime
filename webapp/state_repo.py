"""
GitHub-API-backed access to the private companion repo (outreach_queue.csv,
outreach_log.csv, search_queries.txt, tracks.json, one CV per track) and to the pipeline repo's
Actions runs. Vercel functions are stateless with no persistent disk, so every
read and write goes over HTTPS to GitHub instead of a local git clone. The
companion repo stays the single source of truth shared with the scheduled
GitHub Actions workflows.
"""
import os
import re
import csv
import io
import json
import base64
import copy
from urllib.parse import urlparse

import requests

REPO = os.environ.get("STATE_REPO", "Asheryram/mime-state")
PIPELINE_REPO = os.environ.get("PIPELINE_REPO", "Asheryram/mime")
API = "https://api.github.com"
BRANCH = "main"

QUEUE_PATH = "outreach_queue.csv"
LOG_PATH = "outreach_log.csv"
QUERIES_PATH = "search_queries.txt"
TRACKS_PATH = "tracks.json"

# The shipped roles, used until tracks.json is first saved from the dashboard.
# applicant.py reads this same file, so the pipeline and the dashboard can't
# disagree about what the defaults are.
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tracks_default.json"), encoding="utf-8") as _f:
    DEFAULT_TRACKS = json.load(_f)

QUEUE_FIELDS = [
    "company_name", "website", "email", "company_type",
    "recipient_name", "company_address", "status",
    "date_added", "date_sent", "date_followup", "message_id",
    "track", "lead_reviewed", "replied", "bounced", "review_notified",
    "role_reason", "role_review", "email_note", "reply_note",
    "referral_email", "referral_by", "referral_attach", "referral_status",
    "referral_date", "referral_message_id", "referral_replied", "referral_note",
    "outcome", "outcome_date", "checkback_date", "checkback_status", "checkback_sent_date",
    "checkback_message_id", "checkback_replied", "checkback_note", "checkback_notified",
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


# --- Whose data ---------------------------------------------------------------
# The owner's files live at the repo root, as they always have. An invited
# user's live under tenants/<id>/. Every read and write goes through _scoped(),
# set per request from the signed-in session (app.py), so a page can't reach
# another user's files by building a path: there is no unscoped entry point.
import contextvars

_TENANT = contextvars.ContextVar("tenant", default="")
TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


def set_tenant(tenant_id):
    tenant_id = tenant_id or ""
    if tenant_id and not TENANT_ID_RE.match(tenant_id):
        raise ValueError("bad tenant id")
    _TENANT.set(tenant_id)


def current_tenant():
    return _TENANT.get()


def _scoped(path):
    t = _TENANT.get()
    return f"tenants/{t}/{path}" if t else path


def _contents_url(path):
    return f"{API}/repos/{REPO}/contents/{path}"


def _get_file_bytes(path):
    """Returns (raw_bytes, sha), or (None, None) if the file doesn't exist yet."""
    return _raw_get(_scoped(path))


def _put_file_bytes(path, raw_bytes, sha, message):
    t = _TENANT.get()
    return _raw_put(_scoped(path), raw_bytes, sha, f"[{t}] {message}" if t else message)


def _raw_get(path):
    r = requests.get(_contents_url(path), headers=_headers(), params={"ref": BRANCH}, timeout=15)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    data = r.json()
    return base64.b64decode(data["content"]), data["sha"]


def _raw_put(path, raw_bytes, sha, message):
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


# --- Roles (tracks) and their CVs -------------------------------------------

def _backfill(tracks):
    """A tracks.json saved before keywords existed gets the defaults for built-in roles."""
    for key, t in tracks.items():
        if "keywords" not in t:
            t["keywords"] = [] if _TENANT.get() else list(DEFAULT_TRACKS.get(key, {}).get("keywords", []))
    return tracks


def _starting_tracks():
    """What a missing tracks.json means: the owner's built-in roles for the
    owner; nothing at all for an invited user (never the owner's pitch)."""
    return {} if _TENANT.get() else copy.deepcopy(DEFAULT_TRACKS)


def get_tracks():
    """Returns (tracks_dict, saved). saved is False while the defaults are in use."""
    raw, _ = _get_file_bytes(TRACKS_PATH)
    if raw is None:
        return _starting_tracks(), False
    return _backfill(json.loads(raw.decode("utf-8"))), True


def mutate_tracks(fn, message, attempts=3):
    """
    Read-modify-write tracks.json pinned to the sha we read, like mutate_queue.
    The first save writes the defaults out, which is what makes them editable.
    """
    for attempt in range(attempts):
        raw, sha = _get_file_bytes(TRACKS_PATH)
        tracks = _backfill(json.loads(raw.decode("utf-8"))) if raw is not None else _starting_tracks()
        result = fn(tracks)
        body = (json.dumps(tracks, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            _put_file_bytes(TRACKS_PATH, body, sha, message)
            return result
        except ConflictError:
            if attempt == attempts - 1:
                raise


def get_cv(cv_path):
    """Returns (pdf_bytes, sha) for one track's CV, or (None, None) if not uploaded."""
    return _get_file_bytes(cv_path)


def cv_size(cv_path):
    """Size in bytes of a track's CV, or None if it isn't there. Metadata only, no download."""
    return _raw_size(_scoped(cv_path))


def _raw_size(path):
    r = requests.get(_contents_url(path), headers=_headers(), params={"ref": BRANCH}, timeout=15)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json().get("size")


def save_cv(cv_path, pdf_bytes, message):
    _, sha = _get_file_bytes(cv_path)
    _put_file_bytes(cv_path, pdf_bytes, sha, message)


# --- History and pipeline runs ----------------------------------------------

def recent_commits(limit=20):
    """Every change to the companion repo, newest first: pipeline runs and dashboard edits alike."""
    t = _TENANT.get()
    params = {"sha": BRANCH, "per_page": limit}
    if t:
        params["path"] = f"tenants/{t}"  # only commits that touched their folder
    r = requests.get(f"{API}/repos/{REPO}/commits", headers=_headers(), params=params, timeout=15)
    r.raise_for_status()
    out = []
    for c in r.json():
        msg = c["commit"]["message"].split("\n")[0]
        if not t and re.match(r"^\[[a-z0-9-]+\] ", msg):
            continue  # an invited user's own edit: theirs, not the owner's feed
        out.append({
            "message": re.sub(r"^\[[a-z0-9-]+\] ", "", msg),
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


def dispatch_workflow(workflow_file, inputs=None):
    body = {"ref": BRANCH}
    inputs = dict(inputs or {})
    if _TENANT.get():
        # An invited user's "Run now" runs only them, never the owner's pipeline.
        inputs["tenant"] = _TENANT.get()
    if inputs:
        body["inputs"] = inputs
    r = requests.post(
        f"{API}/repos/{PIPELINE_REPO}/actions/workflows/{workflow_file}/dispatches",
        headers=_headers(), json=body, timeout=15,
    )
    if r.status_code in (403, 404):
        raise TokenPermissionError(PIPELINE_REPO)
    r.raise_for_status()


def actions_url(workflow_file):
    return f"https://github.com/{PIPELINE_REPO}/actions/workflows/{workflow_file}"


# --- Invited users' profile and keys ------------------------------------------------
# profile.json: who the applications are from (name, Gmail, links...). Plain.
# secrets.enc: their Gmail app password and Exa/Apify keys, encrypted with
# SECRETS_KEY (secretbox.py). profile.json records only WHICH keys are saved
# ("keys_set"), so pages can show "saved" without ever decrypting.
PROFILE_PATH = "profile.json"
SECRETS_PATH = "secrets.enc"


def get_profile():
    raw, _ = _get_file_bytes(PROFILE_PATH)
    return json.loads(raw.decode("utf-8")) if raw else {}


def save_profile(profile, message):
    _, sha = _get_file_bytes(PROFILE_PATH)
    _put_file_bytes(PROFILE_PATH, (json.dumps(profile, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
                    sha, message)


def update_secrets(changes, message):
    """
    changes: {field: new value, or None to remove}. Decrypts what's stored,
    applies the changes, re-encrypts. Returns the sorted list of fields now set.
    """
    import secretbox
    raw, sha = _get_file_bytes(SECRETS_PATH)
    current = secretbox.decrypt(raw) if raw else {}
    for k, v in changes.items():
        if v is None:
            current.pop(k, None)
        else:
            current[k] = v
    _put_file_bytes(SECRETS_PATH, secretbox.encrypt(current), sha, message)
    return sorted(k for k, v in current.items() if v)


def get_secrets():
    """Decrypted keys, for the one place that needs them (testing the Gmail login)."""
    import secretbox
    raw, _ = _get_file_bytes(SECRETS_PATH)
    return secretbox.decrypt(raw) if raw else {}
