"""
Automatic reply and bounce detection from the sending Gmail inbox.

Run from the repo root:  python3 -m unittest discover tests
A fake IMAP server stands in for Gmail and records every command, so the tests
also prove the mailbox is only ever opened read-only and never marked read.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import inbox_check  # noqa: E402
from test_roles import InTempDir, FakeSMTP, write_queue, write_file  # noqa: E402

ME = "asher@gmail.com"


def mail(frm, subject="Re: Software Engineering Application", date="Fri, 21 Nov 2026 10:00:00 +0000",
         in_reply_to="", references="", extra="", body=""):
    head = (f"From: {frm}\r\nTo: {ME}\r\nSubject: {subject}\r\nDate: {date}\r\n"
            f"Message-ID: <{abs(hash((frm, subject, date))) }@mx>\r\n")
    if in_reply_to:
        head += f"In-Reply-To: {in_reply_to}\r\n"
    if references:
        head += f"References: {references}\r\n"
    head += extra
    return head.encode(), body.encode()


class FakeIMAP:
    """Just enough of imaplib.IMAP4_SSL, recording what was asked of it."""
    def __init__(self, messages, fail_login=False):
        self.messages = messages          # list of (headers_bytes, body_bytes)
        self.fail_login = fail_login
        self.log = []

    def __call__(self, host):            # used as imap_factory
        self.log.append(("connect", host))
        return self

    def login(self, user, password):
        self.log.append(("login", user))
        if self.fail_login:
            raise inbox_check.imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        return "OK", [b""]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"']

    def select(self, mailbox, readonly=False):
        self.log.append(("select", mailbox, readonly))
        return "OK", [str(len(self.messages)).encode()]

    def search(self, charset, *criteria):
        self.log.append(("search",) + criteria)
        return "OK", [" ".join(str(i + 1) for i in range(len(self.messages))).encode()]

    def fetch(self, ids, what):
        self.log.append(("fetch", what))
        out = []
        for n in str(ids).split(","):
            head, body = self.messages[int(n) - 1]
            part = head if "HEADER" in what else body
            out.append((f"{n} (BODY[] {{{len(part)}}}".encode(), part))
            out.append(b")")
        return "OK", out

    def logout(self):
        self.log.append(("logout",))


ROWS = [
    {"company_name": "Acme", "email": "hr@acme.com", "status": "Sent", "message_id": "<m-acme@gmail.com>",
     "date_sent": "2026-11-20 07:30:00"},
    {"company_name": "Hubtel", "email": "careers@hubtel.com.gh", "status": "Followup-Sent",
     "message_id": "<m-hubtel@gmail.com>", "date_sent": "2026-11-12 07:30:00"},
    {"company_name": "Small Shop", "email": "smallshop.gh@gmail.com", "status": "Sent",
     "message_id": "<m-shop@gmail.com>", "date_sent": "2026-11-20 07:30:00"},
    {"company_name": "Gone Co", "email": "jobs@gone.co", "status": "Sent", "message_id": "<m-gone@gmail.com>",
     "date_sent": "2026-11-20 07:30:00"},
    {"company_name": "Quiet Co", "email": "hr@quiet.co", "status": "Sent", "message_id": "<m-quiet@gmail.com>",
     "date_sent": "2026-11-20 07:30:00"},
]


def rows():
    return [dict(r, replied="", bounced="") for r in ROWS]


class ClassifyTests(unittest.TestCase):
    def run_on(self, messages, rs=None):
        rs = rs or rows()
        import email
        parsed = [(email.message_from_bytes(h), b.decode() or None) for h, b in messages]
        found = inbox_check.classify(parsed, rs, ME)
        return {rs[i]["company_name"]: kind for i, (kind, _) in found.items()}, found

    def test_threaded_reply(self):
        got, _ = self.run_on([mail("Jane Doe <jane.doe@somewhere-else.org>", in_reply_to="<m-acme@gmail.com>")])
        self.assertEqual(got, {"Acme": "replied"})

    def test_reply_to_a_followup_still_counts(self):
        # A follow-up's References carry the original Message-ID.
        got, _ = self.run_on([mail("x@elsewhere.io", in_reply_to="<fu-1@gmail.com>",
                                   references="<m-hubtel@gmail.com> <fu-1@gmail.com>")])
        self.assertEqual(got, {"Hubtel": "replied"})

    def test_colleague_from_same_company_with_fresh_subject(self):
        got, found = self.run_on([mail("Kofi <kofi.mensah@hubtel.com>", subject="Your application")])
        self.assertEqual(got, {"Hubtel": "replied"})          # .com vs .com.gh: same organisation
        self.assertIn("kofi.mensah@hubtel.com", list(found.values())[0][1])

    def test_free_mail_company_needs_exact_address(self):
        got, _ = self.run_on([mail("random.person@gmail.com", subject="hello")])
        self.assertEqual(got, {})
        got, _ = self.run_on([mail("smallshop.gh@gmail.com", subject="Re: your email")])
        self.assertEqual(got, {"Small Shop": "replied"})

    def test_auto_replies_and_acknowledgements_dont_count(self):
        msgs = [
            mail("hr@acme.com", subject="Automatic reply: Software Engineering Application"),
            mail("hr@acme.com", subject="Re: application", extra="Auto-Submitted: auto-replied\r\n"),
            mail("hr@acme.com", subject="Re: application", extra="X-Autoreply: yes\r\n"),
            mail("hr@acme.com", subject="Out of Office"),
            mail("no-reply@acme.com", subject="Thanks for applying to Acme"),
            mail("hr@acme.com", subject="newsletter", extra="Precedence: bulk\r\n"),
        ]
        got, _ = self.run_on(msgs)
        self.assertEqual(got, {})

    def test_mail_from_before_you_wrote_is_ignored(self):
        got, _ = self.run_on([mail("hr@acme.com", subject="Our November newsletter",
                                   date="Mon, 10 Nov 2026 09:00:00 +0000")])
        self.assertEqual(got, {})

    def test_your_own_sent_mail_is_ignored(self):
        got, _ = self.run_on([mail(ME, subject="Software Engineering Application", in_reply_to="<m-acme@gmail.com>")])
        self.assertEqual(got, {})

    def test_bounce_by_failed_recipients_header(self):
        got, _ = self.run_on([mail("Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
                                   subject="Delivery Status Notification (Failure)",
                                   extra="X-Failed-Recipients: jobs@gone.co\r\n")])
        self.assertEqual(got, {"Gone Co": "bounced"})

    def test_bounce_by_quoted_message_id_in_body(self):
        got, _ = self.run_on([mail("postmaster@outlook.com", subject="Undeliverable",
                                   body="Original message headers:\nMessage-ID: <m-gone@gmail.com>\n")])
        self.assertEqual(got, {"Gone Co": "bounced"})

    def test_only_open_rows_are_considered(self):
        rs = rows()
        rs[0]["replied"] = "yes"
        self.assertNotIn("Acme", [r["company_name"] for r in inbox_check.open_rows(rs)])
        rs[1]["status"] = "Approved"
        self.assertNotIn("Hubtel", [r["company_name"] for r in inbox_check.open_rows(rs)])


class ImapTests(unittest.TestCase):
    def test_read_only_and_peek_only(self):
        fake = FakeIMAP([mail("hr@acme.com", subject="Re: application"),
                         mail("mailer-daemon@googlemail.com", subject="Delivery failure",
                              extra="X-Failed-Recipients: jobs@gone.co\r\n", body="bounce text")])
        rs = rows()
        replied, bounced = inbox_check.check_inbox(rs, ME, "pw", imap_factory=fake)
        self.assertEqual((replied, bounced), (["Acme"], ["Gone Co"]))
        selects = [e for e in fake.log if e[0] == "select"]
        self.assertTrue(selects and all(e[2] is True for e in selects), "mailbox must be opened read-only")
        self.assertIn('"[Gmail]/All Mail"', selects[0][1], "searches All Mail so archived replies count")
        fetches = [e[1] for e in fake.log if e[0] == "fetch"]
        self.assertTrue(all("BODY.PEEK" in f for f in fetches), "PEEK only: never marks mail as read")
        self.assertEqual(sum("TEXT" in f for f in fetches), 1, "only the bounce notice's body is read")
        self.assertEqual(fake.log[-1], ("logout",))
        self.assertEqual(rs[0]["reply_note"].split(":")[0], "auto-detected")

    def test_nothing_open_means_no_login(self):
        fake = FakeIMAP([])
        self.assertEqual(inbox_check.check_inbox([], ME, "pw", imap_factory=fake), ([], []))
        self.assertEqual(fake.log, [])


class PipelineTests(InTempDir):
    def setUp(self):
        super().setUp()
        os.environ["SENDER_EMAIL"], os.environ["GMAIL_APP_PASSWORD"] = ME, "x" * 16

    def queue_rows(self):
        return [dict(r, recipient_name="The Team", date_sent="2020-01-01 09:00:00", track="devops")
                for r in ROWS if r["company_name"] in ("Acme", "Quiet Co")]

    REAL_FETCH = staticmethod(inbox_check.fetch_messages)

    def patch_imap(self, op, fake):
        real = PipelineTests.REAL_FETCH
        op.inbox_check.fetch_messages = lambda u, p, since, **kw: real(u, p, since, imap_factory=fake)
        self.addCleanup(setattr, op.inbox_check, "fetch_messages", real)

    def test_detected_reply_stops_the_followup(self):
        applicant, op, fp = self.reload_pipeline()
        import smtp_client
        smtp = FakeSMTP(); smtp.install(smtp_client)
        fp.time.sleep = lambda s: None
        write_file(applicant.cv_path_for("devops"), b"%PDF-devops")
        write_queue(op, self.queue_rows())
        self.patch_imap(op, FakeIMAP([mail("hr@acme.com", subject="Re: application",
                                           date="Fri, 21 Nov 2026 10:00:00 +0000")]))
        op.run_check_inbox()
        q = {r["company_name"]: r for r in op.load_queue()}
        self.assertEqual(q["Acme"]["replied"], "yes")
        self.assertIn("reply from hr@acme.com", q["Acme"]["reply_note"])
        fp.run_followups(dry_run=False)
        self.assertEqual([to for to, _ in smtp.sent], ["hr@quiet.co"], "only the one who didn't reply is chased")

    def test_failed_check_skips_followups_but_not_new_sends(self):
        applicant, op, fp = self.reload_pipeline()
        import smtp_client
        smtp = FakeSMTP(); smtp.install(smtp_client)
        op.time.sleep = fp.time.sleep = lambda s: None
        write_file(applicant.cv_path_for("devops"), b"%PDF-devops")
        write_queue(op, self.queue_rows() + [
            {"company_name": "New Co", "email": "a@new.co", "status": "Approved", "track": "devops",
             "recipient_name": "The Team"}])
        self.patch_imap(op, FakeIMAP([], fail_login=True))
        op.run_check_inbox()
        self.assertTrue(os.path.exists(op.INBOX_CHECK_FAILED_FILE))
        op.run_send_emails()
        fp.run_followups(dry_run=False)
        self.assertEqual([to for to, _ in smtp.sent], ["a@new.co"])

        # The next successful check clears it.
        self.patch_imap(op, FakeIMAP([]))
        op.run_check_inbox()
        self.assertFalse(os.path.exists(op.INBOX_CHECK_FAILED_FILE))

    def test_manual_marks_are_never_undone(self):
        _, op, _ = self.reload_pipeline()
        rs = self.queue_rows()
        rs[1]["replied"] = "yes"   # you marked Quiet Co by hand
        write_queue(op, rs)
        self.patch_imap(op, FakeIMAP([]))
        op.run_check_inbox()
        self.assertEqual({r["company_name"]: r["replied"] for r in op.load_queue()}, {"Acme": "", "Quiet Co": "yes"})


class WorkflowOrderTests(unittest.TestCase):
    def test_inbox_checked_before_anything_is_sent(self):
        with open(os.path.join(ROOT, ".github", "workflows", "send-pipeline.yml")) as f:
            wf = f.read()
        self.assertLess(wf.index("check-inbox"), wf.index("outreach_pipeline.py send"))
        self.assertLess(wf.index("check-inbox"), wf.index("followup_pipeline.py run"))
        with open(os.path.join(ROOT, ".github", "workflows", "scrape-pipeline.yml")) as f:
            self.assertIn("check-inbox", f.read())


if __name__ == "__main__":
    unittest.main()
