"""
Single source of truth for the applicant's identity and pitch.

Every personal detail lives here so the cold email, the follow-up, and the PDF
letterhead can never drift apart. To reuse this pipeline, edit this file - you
should not need to touch the pipeline scripts at all.

Phone and email are the exception: they are read from .env, which is
gitignored, so this file stays publishable without exposing a personal number
and inbox to search engines and address scrapers.
"""

import json
import os
import re

from config import config

# --- Identity -----------------------------------------------------------
FULL_NAME = "Asher Yram Tetteh-Abotsi"
HEADLINE = "Cloud & DevOps Engineer"
ROLE_TITLE = "Cloud & DevOps Engineer"
LOCATION = ""

# Set APPLICANT_PHONE and APPLICANT_EMAIL in .env. EMAIL falls back to the
# SENDER_EMAIL already configured for SMTP, since they are normally the same
# address.
PHONE = config.get("APPLICANT_PHONE", "")
EMAIL = config.get("APPLICANT_EMAIL") or config.get("SENDER_EMAIL", "")

LINKEDIN_URL = "https://www.linkedin.com/in/asher-tetteh-abotsi"
LINKEDIN_LABEL = "linkedin.com/in/asher-tetteh-abotsi"
GITHUB_URL = "https://github.com/Asheryram"
GITHUB_LABEL = "github.com/Asheryram"

# Prefix for generated cover letter filenames (no spaces).
FILE_SLUG = "Asher_Yram_Tetteh-Abotsi"

# Each track attaches its own CV (see "cv_path" in TRACKS below), but the
# recipient always sees this one filename. Otherwise a company receiving
# "cv_software.pdf" learns there is a DevOps version too.
CV_ATTACHMENT_NAME = f"{FILE_SLUG}_CV.pdf"

# --- Application tracks ("roles") ------------------------------------------
# One pitch per kind of role applied for. A queue row carries a track, so the
# same campaign can approach one company about DevOps work and another about
# full-stack engineering, each with its own CV, letter, and email.
#
# Tracks are data, not code. They are read from tracks.json (the companion
# state repo's copy, which the dashboard's Roles page edits and CI copies in),
# falling back to the shipped defaults in webapp/tracks_default.json until that
# file exists. To change a pitch, edit it on the dashboard, or edit tracks.json.
#
# Per-track fields (all strings unless noted):
#   active             bool. An inactive track is never sent; its rows wait.
#   cv_path            The PDF attached for this track. Tailor each CV to the
#                      role but keep the facts identical across them. A track
#                      whose CV is missing is held, never sent with another's.
#   headline           Title under your name on the cover letter letterhead.
#   role_title         "...the {role_title} position at <company>".
#   subject_area       Email subject: "<subject_area> Application - <name>".
#   interest_area      "...strong interest in <interest_area> opportunities".
#   credentials        "My name is X. I am <credentials>."
#   closing_skills     Closing line of the default letter.
#   highlights         list of 2-5 strings, the email's bullet points.
#   followup_expertise "...how my experience with <this> can support...".
#   keywords           list of words/phrases that mark a company as a fit for
#                      this role ("react", "kubernetes"). The scraper tags each
#                      new company with the role its page matches best (see
#                      role_match.py). Avoid words every company uses.
#   letter             The cover letter body: {"intro", "lead_in", "bullets":
#                      [{"title", "text"}], "closing"}, plain text, with
#                      {company} and {role} filled in per company. null keeps
#                      the sector-specific DevOps templates in
#                      cover_letter_generator.get_template_paragraphs().
#
# Every claim must be one that track's CV backs up: the letter and the CV are
# read side by side, and an unbacked claim is worse than no claim.

_HERE = os.path.dirname(os.path.abspath(__file__))
TRACKS_FILE = "tracks.json"
DEFAULT_TRACKS_FILE = os.path.join(_HERE, "webapp", "tracks_default.json")

# Mirrored in webapp/app.py's role form validation.
TRACK_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
CV_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.pdf$")

REQUIRED_TRACK_FIELDS = [
    "cv_path", "headline", "role_title", "subject_area", "interest_area",
    "credentials", "closing_skills", "highlights", "followup_expertise",
]


def load_tracks(path=None):
    """
    Read and validate the tracks file. A malformed tracks.json raises rather
    than falling back to the defaults: silently sending the old pitches after
    you edited them is worse than a failed run that tells you why.
    """
    if path is None:
        path = TRACKS_FILE if os.path.exists(TRACKS_FILE) else DEFAULT_TRACKS_FILE
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not data:
        raise ValueError(f"{path}: expected a non-empty object of tracks.")
    for key, t in data.items():
        # Keys are stored on queue rows and cv_path is used as a bare filename
        # in CI's shell; neither may carry slashes, spaces, or "..".
        if not TRACK_KEY_RE.match(key):
            raise ValueError(f"{path}: track key '{key}' must be lowercase letters, digits, - or _.")
        if not CV_FILENAME_RE.match(str(t.get("cv_path", ""))) or ".." in t["cv_path"]:
            raise ValueError(f"{path}: track '{key}' cv_path must be a plain .pdf filename.")
        missing = [k for k in REQUIRED_TRACK_FIELDS if not t.get(k)]
        if missing:
            raise ValueError(f"{path}: track '{key}' is missing {', '.join(missing)}.")
        kws = t.setdefault("keywords", [])
        if not isinstance(kws, list) or not all(isinstance(k, str) for k in kws):
            raise ValueError(f"{path}: track '{key}' keywords must be a list of strings.")
        if not isinstance(t["highlights"], list):
            raise ValueError(f"{path}: track '{key}' highlights must be a list.")
        letter = t.get("letter")
        if letter is not None:
            ok = (isinstance(letter, dict) and letter.get("intro") and letter.get("closing")
                  and isinstance(letter.get("bullets"), list)
                  and all(b.get("title") and b.get("text") for b in letter["bullets"]))
            if not ok:
                raise ValueError(f"{path}: track '{key}' letter needs intro, closing, and "
                                 f"bullets with a title and text each.")
        t.setdefault("active", True)
        t.setdefault("letter", None)
    return data


TRACKS = load_tracks()
DEFAULT_TRACK = "devops" if "devops" in TRACKS else next(iter(TRACKS))


def get_track(track=None):
    """Look up a track, falling back to the default rather than raising."""
    return TRACKS.get((track or DEFAULT_TRACK).strip().lower(), TRACKS[DEFAULT_TRACK])


def cv_path_for(track=None):
    """
    The CV to attach for a track. Unlike get_track this does NOT fall back to
    the default track's CV for an unknown name: attaching the DevOps CV to a
    software pitch is exactly the mismatch per-track CVs exist to prevent.
    Returns None when the track is unknown.
    """
    t = TRACKS.get((track or DEFAULT_TRACK).strip().lower())
    return t["cv_path"] if t else None


def track_hold_reason(track=None):
    """Why rows on this track can't be sent right now, or None if they can."""
    key = (track or DEFAULT_TRACK).strip().lower()
    t = TRACKS.get(key)
    if not t:
        return f"unknown track '{key}' (not in {TRACKS_FILE})"
    if not t.get("active", True):
        return f"track '{key}' is switched off"
    if not os.path.exists(t["cv_path"]):
        return f"no CV for track '{key}' at '{t['cv_path']}'"
    return None


def headline_for(track=None):
    return get_track(track).get("headline") or HEADLINE


# Backwards-compatible module-level values: anything that has not been made
# track-aware keeps working and gets the default track's pitch.
CV_PATH = TRACKS[DEFAULT_TRACK]["cv_path"]
CREDENTIALS = TRACKS[DEFAULT_TRACK]["credentials"]
EMAIL_HIGHLIGHTS = TRACKS[DEFAULT_TRACK]["highlights"]
EMAIL_SUBJECT = f"{TRACKS[DEFAULT_TRACK]['subject_area']} Application - {FULL_NAME}"


def subject_for(track=None):
    return f"{get_track(track)['subject_area']} Application - {FULL_NAME}"


def signature_block():
    """Plain-text sign-off shared by the cold email and the follow-up."""
    location_line = f"{LOCATION}\n" if LOCATION else ""
    return (
        f"Best regards,\n\n"
        f"{FULL_NAME}\n"
        f"{location_line}"
        f"Phone: {PHONE}\n"
        f"LinkedIn: {LINKEDIN_URL}\n"
        f"GitHub: {GITHUB_URL}\n"
    )


def build_outreach_body(company_name, track=None):
    """
    The cold email body. The follow-up quotes this verbatim, so both messages
    are generated from one template and can never disagree.
    """
    t = get_track(track)
    highlights = "\n".join(f"- {h}" for h in t["highlights"])
    return (
        f"Dear Recruiting Team at {company_name},\n\n"
        f"I hope this email finds you well.\n\n"
        f"My name is {FULL_NAME}. I am {t['credentials']}.\n\n"
        f"I am writing to express my strong interest in {t['interest_area']} "
        f"opportunities at {company_name}. To share details on how my background aligns "
        f"with your work, I have attached my CV and a tailored cover letter.\n\n"
        f"Some highlights of my work include:\n"
        f"{highlights}\n\n"
        f"I would welcome the opportunity to speak briefly with a member of your team or "
        f"your engineering leads about how my skills and experience can "
        f"add value to {company_name}.\n\n"
        f"Thank you for your time and consideration.\n\n"
        f"{signature_block()}"
    )


def build_followup_body(company_name, recipient_name, original_date, track=None):
    """The 6-day check-in, with the original message quoted underneath."""
    t = get_track(track)
    expertise = t["followup_expertise"]
    return (
        f"Dear {recipient_name},\n\n"
        f"I hope this email finds you well.\n\n"
        f"I am writing to follow up briefly on my application for {t['interest_area']} "
        f"opportunities at {company_name}, sent last week.\n\n"
        f"I remain very interested in the work your team is doing, and wanted to check "
        f"whether there are any updates, or whether you would be open to a brief "
        f"10-minute call. I would be glad to share how my experience with "
        f"{expertise} can support your team's goals.\n\n"
        f"I have attached my CV again for your convenience. Please let me know if there "
        f"is any other information I can provide.\n\n"
        f"Thank you for your time and consideration.\n\n"
        f"{signature_block()}"
        f"\n\n---\n"
        f"Original Message:\n"
        f"From: {EMAIL}\n"
        f"Sent: {original_date}\n"
        f"Subject: {subject_for(track)}\n\n"
        f"{build_outreach_body(company_name, track)}"
    )
