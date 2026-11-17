"""
Pick the role (track) a company should be pitched for from what its own page
says, instead of from whichever search happened to find it.

Each role lists keywords in tracks.json (editable on the dashboard's Roles
page). A company gets the role whose keywords its page mentions most, counted
as *distinct* keywords so one word repeated fifty times can't win on its own.
A role only wins with a clear lead: at least MIN_MATCHES distinct keywords and
strictly more than any other role. Anything weaker is not decided here: the
search's role (or the default) is pre-selected as a guess, and the company is
flagged for you to choose on the dashboard before anything is sent.
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
    Returns (role, reason, clear). When `clear` is False no role clearly led:
    `role` is only the pre-selected guess (`fallback`) and the caller must have
    a human confirm it. `reason` is a short line shown on the dashboard.
    """
    hits = score(text, tracks)
    ranked = sorted(hits.items(), key=lambda kv: len(kv[1]), reverse=True)
    if ranked:
        best, found = ranked[0]
        runner_up = len(ranked[1][1]) if len(ranked) > 1 else 0
        if len(found) >= MIN_MATCHES and len(found) > runner_up:
            listed = ", ".join(found[:MAX_LISTED])
            more = f" +{len(found) - MAX_LISTED} more" if len(found) > MAX_LISTED else ""
            return best, f"site mentions {listed}{more}", True
        if len(ranked) > 1 and len(found) == runner_up and len(found) >= MIN_MATCHES:
            tied = " and ".join(k for k, v in ranked if len(v) == len(found))
            return fallback, f"site fits {tied} equally; pre-selected {fallback_source}", False
    if ranked:
        return fallback, (f"weak signal on site ({ranked[0][0]}: {', '.join(ranked[0][1])}); "
                          f"pre-selected {fallback_source}"), False
    return fallback, f"no clear signal on site; pre-selected {fallback_source}", False
