"""
Pick the role (track) a company should be pitched for from what its own page
says, instead of from whichever search happened to find it.

Each role lists keywords in tracks.json (editable on the dashboard's Roles
page). A company gets the role whose keywords its page mentions most, counted
as *distinct* keywords so one word repeated fifty times can't win on its own.
A role only wins with a clear lead: at least MIN_MATCHES distinct keywords and
strictly more than any other role. Anything weaker falls back to the role the
search was tagged with (or the default role), and the reason says so, so a
guess is never presented as a finding.
"""
import re

MIN_MATCHES = 2
MAX_LISTED = 4  # keywords quoted in the reason; the rest are counted


def _pattern(keyword):
    # Keywords contain punctuation ("node.js", "ci/cd"), so \b won't do:
    # require no letter or digit on either side instead.
    return re.compile(r"(?<![a-z0-9])" + re.escape(keyword.lower()) + r"(?![a-z0-9])")


def score(text, tracks):
    """{role: [distinct keywords found]} for every role with at least one hit."""
    text = (text or "").lower()
    hits = {}
    for key, t in tracks.items():
        found = [kw for kw in t.get("keywords", []) if kw.strip() and _pattern(kw.strip()).search(text)]
        if found:
            hits[key] = found
    return hits


def pick_role(text, tracks, fallback, fallback_source="the search's role"):
    """
    Returns (role, reason). `fallback` is used unless one role clearly leads.
    `reason` is a short human-readable line shown on the dashboard.
    """
    hits = score(text, tracks)
    ranked = sorted(hits.items(), key=lambda kv: len(kv[1]), reverse=True)
    if ranked:
        best, found = ranked[0]
        runner_up = len(ranked[1][1]) if len(ranked) > 1 else 0
        if len(found) >= MIN_MATCHES and len(found) > runner_up:
            listed = ", ".join(found[:MAX_LISTED])
            more = f" +{len(found) - MAX_LISTED} more" if len(found) > MAX_LISTED else ""
            return best, f"site mentions {listed}{more}"
        if len(ranked) > 1 and len(found) == runner_up and len(found) >= MIN_MATCHES:
            tied = " and ".join(k for k, v in ranked if len(v) == len(found))
            return fallback, f"site fits {tied} equally; used {fallback_source}"
    return fallback, f"no clear signal on site; used {fallback_source}"
