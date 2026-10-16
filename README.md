# inbox-watch

## Content calendar → Buffer scheduler

On-demand Claude skill (`.claude/skills/schedule-content-calendar/`) that reads a client's
content calendar from Google Drive and schedules the **Approved** rows in Buffer.

Ask Claude: *"Schedule Acme's November calendar."*

### Setup
1. Buffer → Settings → API → **Generate API key** (organisation owner only). Store it as the
   environment secret `BUFFER_API_KEY`.
2. `python3 scripts/buffer_scheduler.py channels` lists the channel IDs.
3. `cp clients.example.json clients.json` and map each client's platforms to channel IDs.
4. Format client sheets per `.claude/skills/schedule-content-calendar/references/calendar-template.md`.

### Safety
- Dry run by default. `--live` is needed to create posts, and each run is capped at 60.
- `ledger/<client>.json` records every created post, so the same slot is never sent twice.
  Commit it after each run.
- Buffer's API is create-only, so corrections are made by hand in Buffer.

### Tests
```bash
python3 -m unittest discover -s tests
```
