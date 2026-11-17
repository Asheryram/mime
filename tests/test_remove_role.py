"""
Removing a role (e.g. folding sysadmin into devops) without stranding or
mis-sending the companies tagged with it.

Run from the repo root:  python3 -m unittest discover tests
"""
import importlib
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))
os.environ.setdefault("FLASK_SECRET_KEY", "test")

from test_roles import InTempDir, FakeStateRepo, FakeSMTP, write_queue, write_file  # noqa: E402


def queue_csv(state_repo, rows):
    lines = [",".join(state_repo.QUEUE_FIELDS)]
    for r in rows:
        lines.append(",".join(r.get(f, "") for f in state_repo.QUEUE_FIELDS))
    return ("\n".join(lines) + "\n").encode()


class DefaultsTests(unittest.TestCase):
    def test_sysadmin_folded_into_devops(self):
        with open(os.path.join(ROOT, "webapp", "tracks_default.json")) as f:
            d = json.load(f)
        self.assertEqual(set(d), {"devops", "software"})
        dv = d["devops"]
        for kw in ("helpdesk", "managed it", "active directory", "kubernetes"):
            self.assertIn(kw, dv["keywords"])
        self.assertEqual(len(dv["keywords"]), len(set(dv["keywords"])), "no duplicate keywords")
        self.assertTrue(2 <= len(dv["highlights"]) <= 5, "dashboard form allows 2-5")
        self.assertTrue(any("Cisco" in h for h in dv["highlights"]))
        self.assertIn("security hardening", dv["followup_expertise"])


class PipelineOrphanTests(InTempDir):
    def test_row_on_removed_role_is_never_approved_or_sent(self):
        applicant, op, _ = self.reload_pipeline()
        import smtp_client
        fake = FakeSMTP(); fake.install(smtp_client)
        op.time.sleep = lambda s: None
        write_file(applicant.cv_path_for("devops"), b"%PDF-devops")
        write_queue(op, [
            {"company_name": "Old Sys", "email": "a@oldsys.co", "status": "Pending", "track": "sysadmin",
             "recipient_name": "The Team"},
            {"company_name": "Dev Co", "email": "a@dev.co", "status": "Pending", "track": "devops",
             "recipient_name": "The Team"},
        ])
        op.run_auto_approve()
        self.assertEqual({r["company_name"]: r["status"] for r in op.load_queue()},
                         {"Old Sys": "Pending", "Dev Co": "Approved"})
        op.run_send_emails()
        self.assertEqual([to for to, _ in fake.sent], ["a@dev.co"])
        n = len(fake.sent)
        op.run_notify_needs_review()
        self.assertIn("its role 'sysadmin' was removed", fake.sent[n][1].get_content())


class DashboardRemoveTests(unittest.TestCase):
    def setUp(self):
        import state_repo, app as webapp
        importlib.reload(state_repo); importlib.reload(webapp)
        self.sr = state_repo
        self.fake = FakeStateRepo(state_repo)
        self.c = webapp.app.test_client()
        with self.c.session_transaction() as s:
            s["authed"] = True

    def rows(self):
        return {r["company_name"]: r for r in self.sr._parse_queue(self.fake.files["outreach_queue.csv"].decode())}

    def seed(self, with_saved_sysadmin):
        if with_saved_sysadmin:
            tracks = json.loads(json.dumps(self.sr.DEFAULT_TRACKS))
            tracks["devops"]["keywords"] = ["kubernetes"]
            tracks["sysadmin"] = dict(tracks["devops"], cv_path="cv_sysadmin.pdf", keywords=["helpdesk", "kubernetes"])
            self.fake.files["tracks.json"] = json.dumps(tracks).encode()
        self.fake.files["outreach_queue.csv"] = queue_csv(self.sr, [
            {"company_name": "Sys Pending", "website": "https://sp.co", "email": "a@sp.co", "status": "Approved",
             "track": "sysadmin", "recipient_name": "T"},
            {"company_name": "Sys Sent", "website": "https://ss.co", "email": "a@ss.co", "status": "Sent",
             "track": "sysadmin", "recipient_name": "T"},
            {"company_name": "Dev Co", "website": "https://dev.co", "email": "a@dev.co", "status": "Approved",
             "track": "devops", "recipient_name": "T"},
        ])
        self.fake.files["search_queries.txt"] = b"# c\nIT firms Accra | 10 | sysadmin\nSaaS | 5 | software\nbanks\n"

    def test_remove_saved_role_moves_everything(self):
        self.seed(with_saved_sysadmin=True)
        self.assertEqual(self.c.get("/roles/sysadmin").status_code, 200)
        r = self.c.post("/roles/sysadmin/delete", data={"move_to": "devops", "merge_keywords": "on"},
                        follow_redirects=True)
        self.assertIn(b"Moved 2 companies to", r.data)
        self.assertIn(b"1 already emailed", r.data)
        tracks = json.loads(self.fake.files["tracks.json"])
        self.assertNotIn("sysadmin", tracks)
        self.assertEqual(tracks["devops"]["keywords"], ["kubernetes", "helpdesk"], "merged without duplicates")
        rows = self.rows()
        self.assertEqual({rows[n]["track"] for n in ("Sys Pending", "Sys Sent", "Dev Co")}, {"devops"})
        self.assertEqual(rows["Sys Pending"]["role_reason"], "moved from sysadmin")
        self.assertEqual(self.fake.files["search_queries.txt"],
                         b"# c\nIT firms Accra | 10 | devops\nSaaS | 5 | software\nbanks\n")
        self.assertIn('href="/companies/sys-pending"', self.c.get("/upcoming").data.decode())

    def test_remove_refuses_bad_target_and_last_role(self):
        self.seed(with_saved_sysadmin=True)
        self.c.post("/roles/sysadmin/delete", data={"move_to": "sysadmin"})
        self.c.post("/roles/sysadmin/delete", data={"move_to": "nope"})
        self.assertIn("sysadmin", json.loads(self.fake.files["tracks.json"]))
        self.assertEqual(self.rows()["Sys Pending"]["track"], "sysadmin")
        only = {"devops": self.sr.DEFAULT_TRACKS["devops"]}
        self.fake.files["tracks.json"] = json.dumps(only).encode()
        self.c.post("/roles/devops/delete", data={"move_to": "devops"})
        self.assertIn("devops", json.loads(self.fake.files["tracks.json"]))

    def test_rows_left_on_a_removed_role_wait_and_can_be_moved_in_one_go(self):
        self.seed(with_saved_sysadmin=False)  # defaults in use; sysadmin no longer among them
        att = self.c.get("/attention").data.decode()
        self.assertIn("its role \\u0027sysadmin\\u0027 was removed", att)  # JSON-escaped quotes
        self.assertNotIn('href="/companies/sys-pending"', self.c.get("/upcoming").data.decode())
        roles = self.c.get("/roles").data.decode()
        self.assertIn("which no longer exists", roles)
        r = self.c.post("/roles/move-orphans", data={"from": "sysadmin", "move_to": "devops"}, follow_redirects=True)
        self.assertIn(b"Moved 2 companies and 1 search from", r.data)
        self.assertEqual({row["track"] for row in self.rows().values()}, {"devops"})
        self.assertNotIn("which no longer exists", self.c.get("/roles").data.decode())

    def test_choosing_a_role_in_needs_you_also_fixes_a_removed_role(self):
        self.seed(with_saved_sysadmin=False)
        r = self.c.post("/companies/sys-pending/resolve", data={"reason": "role", "track": "devops"},
                        headers={"Accept": "application/json"})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.rows()["Sys Pending"]["track"], "devops")


if __name__ == "__main__":
    unittest.main()
