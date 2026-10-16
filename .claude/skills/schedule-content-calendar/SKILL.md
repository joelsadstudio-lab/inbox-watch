---
name: schedule-content-calendar
description: Schedule a client's approved content-calendar posts into Buffer. Reads the client's calendar Google Sheet from Drive, validates every row (approval status, date/time, platform mapping, caption limits, media links), shows a dry-run report, then creates the posts in Buffer after one confirmation. Use when Joel says things like "schedule Acme for November", "push the content calendar to Buffer", "load next week's posts for <client>", or "what's ready to schedule for <client>".
---

# Schedule content calendar → Buffer

Deterministic work (parsing, timezone maths, validation, API calls, duplicate protection)
lives in `scripts/buffer_scheduler.py`. Your job is to fetch the calendar, run the script,
explain the result, and get the go-ahead. **Never hand-craft Buffer API calls.**

## Hard rules

1. **Buffer's API is create-only.** No edit, no delete. A wrong post can only be fixed by hand
   in Buffer. That's why there is a dry run, a confirmation, and a ledger.
2. **Only rows with Status = `Approved` are scheduled.** Don't infer approval from anything else
   and don't "fix" a caption or a date yourself. Report problems; Joel or the client fixes the sheet.
3. **Always dry-run first and show the report.** Run `--live` only after Joel says yes to the
   specific client, date range, and post count in this conversation.
4. **Commit and push `ledger/` right after every live run.** This container is ephemeral. If the
   ledger is lost, the next run re-sends everything as duplicates.

## Workflow

### 1. Pin down scope
Get the **client** and the **date range** (default: from now to the end of next month). If the
client isn't in `clients.json`, go to *Onboarding a client* below.

### 2. Get the calendar
- Drive connector: `search_files` with `title = '<drive_sheet from clients.json>'` and
  `mimeType = 'application/vnd.google-apps.spreadsheet'`, then `read_file_content`.
  Use the `sheet_tab` tab.
- If the connector is down or can't find the sheet: ask Joel for a CSV export
  (File → Download → CSV). Don't guess the file.

### 3. Write the CSV
Write the rows **in the date range** to
`<scratchpad>/<client-slug>-<YYYY-MM-DD>.csv`, with the sheet's original headers and values
copied verbatim. Keep every row in range, including unapproved ones. The script reports them as
SKIP, which tells Joel what's still waiting on the client. Copy each caption exactly as written:
line breaks, emojis, hashtags, links. See `references/calendar-template.md` for the accepted
columns.

### 4. Dry run
```bash
python3 scripts/buffer_scheduler.py validate --client "<Client>" --csv <path>
```
Show Joel the report table and a short summary:
- **READY**: N posts, and the date span they cover
- **ERROR**: grouped by cause, e.g. "3 Instagram rows have no media"
- **SKIP**: not approved / already scheduled / in the past, counts only
- Any ⚠ warnings. Drive links must be shared as "Anyone with the link", or Buffer fails at
  publish time, not at schedule time.

Then ask: "Schedule **N** posts for **<Client>** (<first date> → <last date>) in Buffer?"

### 5. Live run (after yes)
```bash
python3 scripts/buffer_scheduler.py schedule --client "<Client>" --csv <path> --live
```
Needs `BUFFER_API_KEY` in the environment. If it's missing, stop and tell Joel to add it as an
environment secret. Don't ask for it to be pasted into chat.

### 6. Close out
- `git add ledger/ && git commit -m "Schedule <Client> <range>: N posts" && git push`
- Report SCHEDULED / FAILED counts. For FAILED rows, quote Buffer's message.
- Tell Joel which sheet rows to change to `Scheduled`. The Drive connector can't edit sheet
  cells.

## Onboarding a client
1. `python3 scripts/buffer_scheduler.py channels` lists every Buffer channel with its ID.
   If the run fails with a GraphQL schema error, Buffer has changed its beta API. Report it;
   don't patch the query by guessing.
2. Add an entry to `clients.json` (copy from `clients.example.json`): Drive sheet name, tab,
   timezone (default `Africa/Johannesburg`), and platform → channel ID.
3. Commit `clients.json`.

## Known limits
- Buffer beta allows about 100 requests per 15 minutes. The script sends 1 request per second and caps a run
  at 60 posts (`--max-posts`).
- Media must be a public direct URL. Canva edit links and Drive folders are rejected. Drive
  file links are rewritten to direct download.
- Changing a post that's already scheduled: edit it in Buffer and update the sheet. The ledger keys on
  client + platform + time, so the script won't re-send it. To move a post to a new time,
  delete it in Buffer first, then re-run. The new time is a new slot.
