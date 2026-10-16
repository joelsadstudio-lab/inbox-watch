import copy
import json
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import cpmd_agent as ca  # noqa: E402

SETTINGS = json.loads((ca.CPMD / "settings.json").read_text())
ACTIONS = json.loads((ca.CPMD / "actions.json").read_text())
TODAY = date(2026, 10, 18)


def unblocked():
    """Settings with every decision and value CPMD owes us filled in."""
    s = copy.deepcopy(SETTINGS)
    s["decisions"].update(reactivation_lists_opted_in=True, pde_nov_prep_running=True, pde_page_live=True,
                          site_fixes_done=True, whatsapp_confirmed=True, existing_leads_treatment="reactivation")
    s["values"] = {k: f"<{k}>" for k in s["values"]}
    s["values"].update(webinar_nov_date="2026-11-10", webinar_nov_invite_send_date="2026-10-28")
    for code, c in s["courses"].items():
        c.update(name=c["name"] or f"{code} Programme", intake_date="2 February 2027", brochure_link=f"https://x/{code}")
    s["getresponse"]["from_field_ids"] = {"team": "FT", "graeme": "FG"}
    s["getresponse"]["pde_audience_lists"] = ["CPMD Newsletter Subscribers"]
    s["approved_copy"] = ["R1", "R2", "R3", "P1", "P2", "WEB1", "S1", "S2", "S3", "S4", "S5", "S6"]
    return s


def by_id(settings, today=TODAY, ledger=None, have_key=True):
    return {a.id: a for a in ca.evaluate_all(settings, ACTIONS, ledger or {}, today, have_key)}


class GateTests(unittest.TestCase):
    def test_nothing_sends_with_current_settings(self):
        items = by_id(SETTINGS, have_key=False)
        self.assertFalse([a.id for a in items.values() if a.state == "READY"])

    def test_unresolved_placeholder_blocks(self):
        s = unblocked()
        s["courses"]["CIB"]["intake_date"] = None
        a = by_id(s)["R1-CIB"]
        self.assertEqual(a.state, "BLOCKED")
        self.assertIn("needs courses.CIB.intake_date", a.reasons)

    def test_unapproved_copy_blocks(self):
        s = unblocked()
        s["approved_copy"].remove("R1")
        self.assertEqual(by_id(s)["R1-PFM"].state, "BLOCKED")

    def test_value_gate(self):
        s = unblocked()
        s["decisions"]["existing_leads_treatment"] = "single_broadcast"
        self.assertEqual(by_id(s)["R2-STM"].state, "BLOCKED")

    def test_ready_within_lookahead_waiting_beyond(self):
        items = by_id(unblocked())
        self.assertEqual(items["R1-CIB"].state, "READY")     # due 20 Oct, today 18 Oct
        self.assertEqual(items["R2-CIB"].state, "WAITING")   # due 27 Oct
        self.assertNotIn("{{", items["R1-CIB"].body)
        self.assertIn("[[firstname]]", items["R1-CIB"].body)  # GetResponse merge tag kept

    def test_expired_never_sends(self):
        items = by_id(unblocked(), today=date(2026, 10, 28))
        self.assertEqual(items["P1"].state, "EXPIRED")  # PDE registration closes 30 Oct

    def test_ledger_prevents_resend(self):
        items = by_id(unblocked(), ledger={"R1-CIB": {"newsletterId": "n1"}})
        self.assertEqual(items["R1-CIB"].state, "DONE")

    def test_human_overdue(self):
        items = by_id(SETTINGS, today=date(2026, 10, 26))
        self.assertEqual(items["SITE1"].state, "OVERDUE")


class PayloadTests(unittest.TestCase):
    def test_newsletter_payload(self):
        s = unblocked()
        a = by_id(s)["R1-STM"]
        cache = {"LEADS - STM": "c_stm", s["getresponse"]["registered_list"]: "c_reg"}
        now = datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)
        p = ca.newsletter_payload(a, s, cache, now)
        self.assertEqual(p["sendSettings"]["selectedCampaigns"], ["c_stm"])
        self.assertEqual(p["sendSettings"]["excludedCampaigns"], ["c_reg"])
        self.assertEqual(p["sendOn"], "2026-10-20T08:30:00+02:00")
        self.assertEqual(p["fromField"]["fromFieldId"], "FT")

    def test_graeme_sender_for_r3(self):
        s = unblocked()
        a = by_id(s, today=date(2026, 11, 1))["R3-CIB"]
        self.assertEqual(a.sender, "graeme")

    def test_past_due_sends_soon_not_in_past(self):
        s = unblocked()
        a = by_id(s, today=date(2026, 10, 21))["R1-CIB"]
        now = datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)
        self.assertGreater(ca.send_on(a, s, now), now)

    def test_html_bullets(self):
        self.assertIn("<li>a</li>", ca.to_html("x\n\n- a\n- b"))


if __name__ == "__main__":
    unittest.main()
