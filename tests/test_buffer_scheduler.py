import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import buffer_scheduler as bs  # noqa: E402

NOW = datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc)
CLIENT = {
    "name": "Acme",
    "timezone": "Africa/Johannesburg",
    "channels": {"facebook": "ch_fb", "instagram": "ch_ig", "x": "ch_x"},
}


def row(**kw):
    base = dict(line=2, date="2026-10-20", time="09:00", platform="Facebook",
                caption="Hello", media="", status="Approved")
    base.update(kw)
    return bs.Row(**base)


def run(*rows, ledger=None):
    return bs.validate(list(rows), CLIENT, ledger or {}, now=NOW)


class ValidateTests(unittest.TestCase):
    def test_ready_converts_sast_to_utc(self):
        (r,) = run(row())
        self.assertEqual(r.verdict, "READY", r.reasons)
        self.assertEqual(r.due_utc, datetime(2026, 10, 20, 7, 0, tzinfo=timezone.utc))
        self.assertEqual(r.channel_id, "ch_fb")

    def test_unapproved_skipped(self):
        (r,) = run(row(status="Draft"))
        self.assertEqual(r.verdict, "SKIP")

    def test_south_african_date_format(self):
        (r,) = run(row(date="03/11/2026"))
        self.assertEqual(r.due_utc.month, 11)
        self.assertEqual(r.due_utc.day, 3)

    def test_past_post_skipped(self):
        (r,) = run(row(date="2026-10-16", time="09:00"))  # 07:00 UTC < NOW
        self.assertEqual(r.verdict, "SKIP")

    def test_instagram_requires_media(self):
        (r,) = run(row(platform="IG"))
        self.assertEqual(r.verdict, "ERROR")
        self.assertIn("requires media", r.reasons[0])

    def test_caption_limit(self):
        (r,) = run(row(platform="Twitter", caption="x" * 281))
        self.assertEqual(r.verdict, "ERROR")

    def test_unmapped_platform(self):
        (r,) = run(row(platform="TikTok", media="https://e.com/a.mp4"))
        self.assertEqual(r.verdict, "ERROR")
        self.assertIn("no Buffer channel", r.reasons[0])

    def test_drive_link_rewritten_with_warning(self):
        (r,) = run(row(platform="instagram", media="https://drive.google.com/file/d/abc123/view?usp=sharing"))
        self.assertEqual(r.verdict, "READY", r.reasons)
        self.assertEqual(r.media_urls, ["https://drive.google.com/uc?export=download&id=abc123"])
        self.assertTrue(r.warnings)

    def test_canva_link_rejected(self):
        (r,) = run(row(media="https://www.canva.com/design/DAF123/edit"))
        self.assertEqual(r.verdict, "ERROR")

    def test_ledger_prevents_resend_even_if_caption_changed(self):
        (first,) = run(row())
        ledger = {first.slot_key: {"post_id": "p1"}}
        (again,) = run(row(caption="Edited caption"), ledger=ledger)
        self.assertEqual(again.verdict, "SKIP")
        self.assertEqual(again.post_id, "p1")

    def test_duplicate_slot_in_sheet(self):
        a, b = run(row(line=2), row(line=3, caption="other"))
        self.assertEqual(a.verdict, "READY")
        self.assertEqual(b.verdict, "ERROR")


class InputTests(unittest.TestCase):
    def test_post_input_shape(self):
        (r,) = run(row(platform="instagram", media="https://e.com/a.jpg https://e.com/b.mp4"))
        inp = bs.build_post_input(r)
        self.assertEqual(inp["dueAt"], "2026-10-20T07:00:00.000Z")
        self.assertEqual(inp["mode"], "customScheduled")
        self.assertEqual(inp["assets"], {"images": [{"url": "https://e.com/a.jpg"}],
                                         "videos": [{"url": "https://e.com/b.mp4"}]})

    def test_csv_header_aliases(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
            fh.write("Post Date,Post Time,Channel,Copy,Asset Link,Approval\n"
                     "2026-10-20,09:00,FB,Hi,,Approved\n,,,,,\n")
        rows = bs.read_csv(Path(fh.name))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].platform, rows[0].caption), ("FB", "Hi"))


if __name__ == "__main__":
    unittest.main()
