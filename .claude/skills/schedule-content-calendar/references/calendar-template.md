# Content calendar sheet: required format

One Google Sheet per client, one row per post per platform. If the same post goes to FB and IG,
that's two rows. Captions differ by platform anyway.

| Column (any alias) | Required | Format | Notes |
|---|---|---|---|
| **Date** / Post Date / Publish Date | yes | `2026-11-03` or `03/11/2026` (DD/MM) | US MM/DD is never assumed |
| **Time** / Post Time / Publish Time | yes | `09:00` (24h) or `9:00 AM` | Client's local timezone from `clients.json` |
| **Platform** / Channel / Network | yes | Facebook, Instagram, LinkedIn, X, TikTok, Threads, Pinterest, YouTube, GBP | Aliases: FB, IG, LI, Twitter, YT |
| **Caption** / Copy / Post Copy / Text | yes | Plain text | Limits: X 280, Threads 500, IG 2200, LinkedIn 3000 |
| **Media URL** / Media / Asset Link / Image | IG, TikTok, Pinterest, YouTube | Public https URL(s), separated by space, comma or `\|` | Drive file links are OK if shared "Anyone with the link" |
| **Status** / Approval | yes | `Approved` is the only status that schedules | Suggested flow: Draft → Client Review → Approved → Scheduled |

Other columns (Pillar, Notes, Owner, etc.) are ignored.
