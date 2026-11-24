"""
The "these companies need you" email sent after a scrape.

Built to be acted on from a phone in under a minute: a subject that says what
kind of help is needed, companies grouped by the one thing each needs, the
evidence for each (why the role was unclear, which address was rejected), and
a Fix button that opens that company in the dashboard's Needs you page.

HTML is table-based with inline styles, since mail clients ignore <style>
blocks and modern layout; a plain-text part carries the same content.
"""
import re
from html import escape
from urllib.parse import quote, urlparse

# Order = how quick each fix is (pick from a list, type an address, check a name).
GROUPS = [
    ("checkback", "Time to check back",
     "They said \u201cnot now\u201d and you asked to be reminded today. A short check-in is drafted: "
     "read it and send, or push it back."),
    ("role", "Choose a role",
     "No role clearly fit, so these wait for you instead of being sent on a guess. "
     "Pick the pitch and CV each should get."),
    ("email", "Find an email",
     "The scraper couldn't find a usable address on these sites. "
     "Add one and they go out with the next send."),
    ("name", "Confirm the company name",
     "The page title didn't look like a company name, so a letter could open "
     "“Dear <something odd> team”. Type the real name."),
]

ACCENT = "#c8450c"
INK = "#17181b"
MUTED = "#5b5e66"
LINE = "#e4e4e0"


def slugify(name):
    """Must match webapp/state_repo.slugify: the dashboard's URL for a company."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:120] or "company"


def host_of(url):
    host = urlparse(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def reason_of(r, known_tracks):
    """Which group a row belongs in."""
    if r.get("outcome") == "later" and r["status"] in ("Sent", "Followup-Sent"):
        return "checkback"
    if r["status"] == "Review":
        return "name"
    if r["status"] == "Skipped":
        return "email"
    return "role"


def detail_of(r, reason, known_tracks):
    """(why, hint): one line of evidence and one line of help for this row."""
    host = host_of(r.get("website"))
    if reason == "checkback":
        when = (r.get("outcome_date") or "")[:10]
        return (f"Replied {when}: not hiring yet, maybe later." if when else "Said not hiring yet, maybe later."), \
            "Check-in drafted, CV attached"
    if reason == "role":
        track = r.get("track") or ""
        if track and track not in known_tracks:
            return f"Its role “{track}” was removed.", f"Roles: {', '.join(known_tracks)}"
        # role_reason reads "<why>; pre-selected <source>": the why is the
        # evidence, the source becomes the best-guess hint.
        why, _, source = (r.get("role_reason") or "no clear fit").partition("; pre-selected ")
        why = why.strip().rstrip(".")
        guess = f"Best guess: {track or '?'}" + (f" ({source.strip()})" if source else "")
        return why[0].upper() + why[1:] + ".", guess
    if reason == "email":
        note = r.get("email_note") or ""
        why = (f"Only found {note.removeprefix('only found ')}." if note
               else "No address on the pages it crawled.")
        hint = f"Try careers@{host} or hr@{host}" if host else ""
        return why, hint
    return f"Listed as “{r['company_name']}” on {host or 'its site'}.", ""


def subject_for(counts, total):
    parts = []
    if counts.get("checkback"):
        parts.append(f"{counts['checkback']} to check back with")
    if counts.get("role"):
        parts.append(f"{counts['role']} to choose a role")
    if counts.get("email"):
        parts.append(f"{counts['email']} missing an email")
    if counts.get("name"):
        parts.append(f"{counts['name']} with an unclear name")
    head = f"{total} compan{'y needs' if total == 1 else 'ies need'} you"
    return f"{head}: {', '.join(parts)}" if parts else head


def build(rows, known_tracks, dashboard_url="", earlier_waiting=0):
    """
    rows: queue rows needing a human, not yet notified.
    Returns (subject, text, html).
    """
    base = (dashboard_url or "").rstrip("/")
    grouped = {key: [] for key, _, _ in GROUPS}
    for r in rows:
        grouped[reason_of(r, known_tracks)].append(r)
    counts = {k: len(v) for k, v in grouped.items()}
    total = len(rows)
    subject = subject_for(counts, total)

    def fix_url(r):
        if not base:
            return ""
        if reason_of(r, known_tracks) == "checkback":  # the draft lives on the company page
            return f"{base}/companies/{quote(slugify(r['company_name']))}#reply"
        return f"{base}/attention?focus={quote(slugify(r['company_name']))}"

    # ---- plain text -------------------------------------------------------
    t = [subject + ".", "Nothing goes out to these until you act. Each takes under a minute.", ""]
    for key, title, blurb in GROUPS:
        if not grouped[key]:
            continue
        t += [f"{title.upper()} ({len(grouped[key])})", blurb, ""]
        for r in grouped[key]:
            why, hint = detail_of(r, key, known_tracks)
            t.append(f"* {r['company_name']}  ({host_of(r.get('website')) or 'no website'})")
            t.append(f"  {why}" + (f" {hint}." if hint else ""))
            if fix_url(r):
                t.append(f"  Fix: {fix_url(r)}")
            t.append("")
    if earlier_waiting:
        t.append(f"Plus {earlier_waiting} from earlier emails still waiting.")
    t.append(f"Open Needs you: {base}/attention" if base
             else "Fix them on your dashboard's Needs you page.")
    text = "\n".join(t) + "\n"

    # ---- HTML ---------------------------------------------------------------
    font = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

    def button(href, label, primary=False):
        bg, fg, border = (ACCENT, "#ffffff", ACCENT) if primary else ("#ffffff", INK, "#d2d2cd")
        return (f'<a href="{escape(href)}" style="display:inline-block;padding:{"12px 20px" if primary else "8px 14px"};'
                f'background:{bg};color:{fg};border:1px solid {border};border-radius:8px;'
                f'font-weight:600;font-size:{"15px" if primary else "14px"};text-decoration:none;white-space:nowrap">'
                f'{escape(label)}</a>')

    sections = []
    for key, title, blurb in GROUPS:
        if not grouped[key]:
            continue
        cards = []
        for r in grouped[key]:
            why, hint = detail_of(r, key, known_tracks)
            host = host_of(r.get("website"))
            site = (f'<a href="{escape(r["website"])}" style="color:{MUTED};text-decoration:underline">{escape(host)}</a>'
                    if host else f'<span style="color:{MUTED}">no website</span>')
            fix = fix_url(r)
            cards.append(f"""
<tr><td style="padding:14px 16px;border:1px solid {LINE};border-radius:10px;background:#ffffff">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
    <td style="vertical-align:top">
      <div style="font-size:16px;font-weight:600;color:{INK};line-height:1.3">{escape(r['company_name'])}</div>
      <div style="font-size:13px;margin-top:2px">{site}</div>
      <div style="font-size:14px;color:{INK};margin-top:8px;line-height:1.45">{escape(why)}</div>
      {f'<div style="font-size:13px;color:{MUTED};margin-top:4px">{escape(hint)}</div>' if hint else ''}
    </td>
    {f'<td style="vertical-align:middle;padding-left:12px;width:1%">{button(fix, "Read" if key == "checkback" else "Fix")}</td>' if fix else ''}
  </tr></table>
</td></tr>
<tr><td style="height:8px;line-height:8px;font-size:0">&nbsp;</td></tr>""")
        sections.append(f"""
<tr><td style="padding:22px 0 6px">
  <div style="font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:{ACCENT}">
    {escape(title)} &middot; {len(grouped[key])}</div>
  <div style="font-size:14px;color:{MUTED};margin-top:4px;line-height:1.45">{escape(blurb)}</div>
</td></tr>
<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{''.join(cards)}</table></td></tr>""")

    cta = (f'<tr><td style="padding:24px 0 4px">{button(base + "/attention", "Open Needs you", primary=True)}</td></tr>'
           if base else
           f'<tr><td style="padding:24px 0 4px;font-size:14px;color:{INK}">Fix them on your dashboard’s <b>Needs you</b> page.</td></tr>')
    earlier = (f'<tr><td style="padding-top:10px;font-size:13px;color:{MUTED}">'
               f'Plus {earlier_waiting} from earlier emails still waiting.</td></tr>' if earlier_waiting else "")

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>{escape(subject)}</title></head>
<body style="margin:0;padding:0;background:#f6f6f4">
<div style="display:none;max-height:0;overflow:hidden">{escape(subject)}. Nothing goes out to these until you act.</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f6f6f4">
<tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;font-family:{font}">
  <tr><td style="font-size:13px;color:{MUTED};padding-bottom:6px">Outreach pipeline</td></tr>
  <tr><td style="font-size:24px;font-weight:700;color:{INK};line-height:1.25">
    {total} compan{'y needs' if total == 1 else 'ies need'} you</td></tr>
  <tr><td style="font-size:15px;color:{MUTED};padding-top:6px;line-height:1.5">
    Nothing goes out to these until you act. Each takes under a minute.</td></tr>
  {''.join(sections)}
  {cta}
  {f'<tr><td style="padding-top:14px;font-size:13px;color:{MUTED}">New to this? <a href="{escape(base)}/guide" style="color:{MUTED}">How Outbound works</a></td></tr>' if base else ''}
  {earlier}
  <tr><td style="padding-top:28px;font-size:12px;color:{MUTED};line-height:1.5;border-top:1px solid {LINE}">
    Sent after a scrape found leads it couldn't finish. Each company appears in one of these emails only;
    anything you don't fix stays in Needs you.</td></tr>
</table>
</td></tr></table>
</body></html>"""
    return subject, text, html
