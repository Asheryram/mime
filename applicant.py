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
# Friend mode: when a profile.json sits in the working directory, this run is
# for an invited user (see tenants.py), and every personal detail below comes
# from it. In that mode nothing falls back to the owner's data: no built-in
# roles, no built-in searches, no sector letters. A friend with nothing set up
# sends nothing, rather than sending the owner's pitch under their name.
PROFILE_FILE = "profile.json"
TENANT_MODE = os.path.exists(PROFILE_FILE)
_profile = {}
if TENANT_MODE:
    with open(PROFILE_FILE, encoding="utf-8") as _f:
        _profile = json.load(_f)
    if not (_profile.get("full_name") or "").strip():
        raise ValueError("profile.json: full_name is required.")

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

# Where companies are assumed to be when a search doesn't say (letter address).
DEFAULT_COMPANY_LOCATION = "Accra, Ghana"


def _label(url):
    return re.sub(r"^https?://(www\.)?", "", url or "").rstrip("/")


if TENANT_MODE:
    FULL_NAME = _profile["full_name"].strip()
    HEADLINE = ROLE_TITLE = (_profile.get("headline") or "").strip()
    LOCATION = (_profile.get("location") or "").strip()
    LINKEDIN_URL = (_profile.get("linkedin") or "").strip()
    LINKEDIN_LABEL = _label(LINKEDIN_URL)
    # "Portfolio" in the profile: GitHub, Behance, a personal site...
    GITHUB_URL = (_profile.get("portfolio") or "").strip()
    GITHUB_LABEL = _label(GITHUB_URL)
    FILE_SLUG = re.sub(r"[^A-Za-z0-9-]+", "_", FULL_NAME).strip("_") or "Applicant"
    DEFAULT_COMPANY_LOCATION = LOCATION or ""
    # Phone and email arrive in the environment from tenants.py, like the owner's.

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


def _default_keywords():
    with open(DEFAULT_TRACKS_FILE, encoding="utf-8") as f:
        return {k: t.get("keywords", []) for k, t in json.load(f).items()}


def load_tracks(path=None):
    """
    Read and validate the tracks file. A malformed tracks.json raises rather
    than falling back to the defaults: silently sending the old pitches after
    you edited them is worse than a failed run that tells you why.
    """
    if path is None:
        if TENANT_MODE and not os.path.exists(TRACKS_FILE):
            # A friend's roles are theirs or nothing: never the owner's.
            raise ValueError("No roles set up yet (tracks.json). Add one on the dashboard's Roles page.")
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
        if "keywords" not in t and not TENANT_MODE:
            # A tracks.json saved before keywords existed: give the built-in
            # roles their default keywords rather than silently none.
            t["keywords"] = list(_default_keywords().get(key, []))
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
    if TENANT_MODE and not t.get("letter"):
        # The letter-less fallback is the owner's sector letters, written about
        # the owner's own experience: never for anyone else.
        return f"track '{key}' has no cover letter written"
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
        + (f"Phone: {PHONE}\n" if PHONE else "")
        + (f"LinkedIn: {LINKEDIN_URL}\n" if LINKEDIN_URL else "")
        + (f"{'GitHub' if 'github.com' in GITHUB_URL else 'Portfolio'}: {GITHUB_URL}\n" if GITHUB_URL else "")
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


# What a referral can carry. "both" is the default and almost always right;
# "letter" alone only makes sense when they already have your CV.
REFERRAL_ATTACHMENTS = {
    "both": "My CV and cover letter are attached.",
    "cv": "My CV is attached.",
    "letter": "My cover letter is attached, and I am happy to send my CV as well.",
}


def referral_subject(track=None):
    return f"Application: {get_track(track)['role_title']} \u2013 {FULL_NAME}"


def build_referral_body(company_name, track=None, referred_by="", attach="both"):
    """
    The email for when a company replies "send your application to X".
    Shorter than the cold email: they asked for it, so it opens with who
    pointed you there and gets to the point. Built from the role's own pitch
    (credentials, top highlights), so it matches the CV it carries.
    """
    t = get_track(track)
    who = (referred_by or "").strip()
    opener = (f"{who} from your team kindly suggested I send my application to this address."
              if who else "I was asked to send my application to this address.")
    highlights = "\n".join(f"- {h}" for h in t["highlights"][:3])
    return (
        f"Dear {company_name} Recruitment Team,\n\n"
        f"{opener}\n\n"
        f"My name is {FULL_NAME}. I am {t['credentials']}. I would like to be considered "
        f"for {t['interest_area']} roles at {company_name}.\n\n"
        f"Some highlights of my work:\n"
        f"{highlights}\n\n"
        f"{REFERRAL_ATTACHMENTS.get(attach, REFERRAL_ATTACHMENTS['both'])} "
        f"Thank you for your time and consideration.\n\n"
        f"{signature_block()}"
    )


def checkin_subject(track=None):
    return f"Checking in: {get_track(track)['role_title']} \u2013 {FULL_NAME}"


def build_checkin_body(company_name, track=None, replied_month=""):
    """
    For a company that said "not now, maybe later": a short, no-pressure
    check-in months on. It reminds them they replied (so it reads as a
    continuation, not a new cold email), carries the role's top highlights,
    and attaches only the CV: they've already read your letter.
    """
    t = get_track(track)
    when = f"In {replied_month} you" if replied_month else "You"
    highlights = "\n".join(f"- {h}" for h in t["highlights"][:2])
    return (
        f"Dear {company_name} team,\n\n"
        f"{when} kindly replied that {company_name} wasn't ready to recruit yet, and that you "
        f"might be in touch when you were. I wanted to check in in case that has changed: "
        f"I am still very interested in {t['interest_area']} roles at {company_name}.\n\n"
        f"As a reminder, I am {t['credentials']}. Some recent highlights:\n"
        f"{highlights}\n\n"
        f"My current CV is attached. If now isn't the right time either, that's no problem at all; "
        f"I'd be glad to hear from you whenever you're ready.\n\n"
        f"Thank you, and best wishes with everything at {company_name}.\n\n"
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
