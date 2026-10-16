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

## CPMD lead agent

Works the CPMD lead-strategy plan: `cpmd/actions.json` lists every action, `cpmd/settings.json`
holds CPMD's decisions and approvals, `scripts/cpmd_agent.py` sends what is ready.

```bash
python3 scripts/cpmd_agent.py brief      # overdue, due this week, what unblocks what
python3 scripts/cpmd_agent.py status     # every action and why it is or isn't ready
python3 scripts/cpmd_agent.py run        # dry run; --live sends
python3 scripts/cpmd_agent.py check      # GetResponse list and from-field ids
```

An email or post is sent only when its decisions are set, every `{{placeholder}}` has a value,
its copy id is in `approved_copy`, and it hasn't expired. Sends are recorded in
`ledger/cpmd-agent.json`.

**Schedule:** `.github/workflows/cpmd-agent.yml` runs weekdays at 06:15 SAST. Add the secrets
`GETRESPONSE_API_KEY` and `BUFFER_API_KEY`, commit a `clients.json` with the CPMD Buffer channels,
and set the repo variable `CPMD_AGENT_LIVE=true` to let it send. Without that variable it only
dry-runs and writes `out/cpmd-brief.md`.
