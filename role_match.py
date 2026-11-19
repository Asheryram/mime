"""
Pick the role (track) a company should be pitched for from what its own page
says, instead of from whichever search happened to find it.

Each role lists keywords in tracks.json (editable on the dashboard's Roles
page). A company gets the role whose keywords its page mentions most, counted
as *distinct* keywords so one word repeated fifty times can't win on its own.
A role only wins with a clear lead: at least MIN_MATCHES distinct keywords and
strictly more than any other role. Otherwise a search tagged with a role
("| software") settles it, unless the site leans to a different role. Only
those conflicts, and companies from untagged searches with no clear fit, are
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


def pick_role(text, tracks, fallback, fallback_source="the search's role", search_role=None):
    """
    Returns (role, reason, clear). When `clear` is False no role clearly led:
    `role` is only the pre-selected guess (`fallback`) and the caller must have
    a human confirm it. `reason` is a short line shown on the dashboard.

    `search_role` is the role the search that found this company was tagged
    with (None for an untagged search). It settles anything the site doesn't
    contradict: you chose it when you wrote the search, so asking again for
    every bank whose homepage says nothing about tech is noise. You're only
    asked when the site leans to a *different* role than the search's.
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

    # Reasons are read by you (dashboard, review email), so plain words.
    if search_role and search_role in tracks:
        own = hits.get(search_role, [])
        rivals = [(k, v) for k, v in ranked if k != search_role and len(v) > len(own)]
        if not rivals:
            tied = [k for k, v in ranked if k != search_role and own and len(v) == len(own)]
            if tied:
                evidence = f"site fits {' and '.join([search_role] + tied)} equally"
            elif own:
                evidence = f"site mentions {', '.join(own[:MAX_LISTED])} and nothing points elsewhere"
            else:
                evidence = "site doesn't point to another role"
            return search_role, f"{evidence}; used this search's role", True
        rival, kws = rivals[0]
        return fallback, (f"search is for {search_role} but the site leans {rival} "
                          f"({', '.join(kws[:MAX_LISTED])}); pre-selected {fallback_source}"), False

    if ranked:
        best, found = ranked[0]
        runner_up = len(ranked[1][1]) if len(ranked) > 1 else 0
        if len(ranked) > 1 and len(found) == runner_up and len(found) >= MIN_MATCHES:
            tied = " and ".join(k for k, v in ranked if len(v) == len(found))
            return fallback, f"site fits {tied} equally; pre-selected {fallback_source}", False
        only = sorted({kw for _, kws in ranked for kw in kws})
        return fallback, (f"site only mentions {', '.join(only[:MAX_LISTED])}, not enough to be sure; "
                          f"pre-selected {fallback_source}"), False
    return fallback, f"site doesn't mention any role's keywords; pre-selected {fallback_source}", False
