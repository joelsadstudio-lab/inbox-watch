---
name: cpmd-lead-agent
description: Run and update the CPMD lead-strategy agent. It works the action register built from the "CPMD Lead-to-Registration Plan" and "CPMD Lead Nurture Journey" docs - GetResponse reactivation, PDE and webinar emails, Buffer social posts, monthly scorecards - and tracks the human actions. Use when Joel says "CPMD brief", "what's blocking CPMD", "CPMD confirmed the intake dates", "approve R1", "run the CPMD agent", or passes on any answer from Graeme or Chantel.
---

# CPMD lead agent

Logic lives in `scripts/cpmd_agent.py`. State lives in three files:

| File | What it holds | Who changes it |
|---|---|---|
| `cpmd/actions.json` | Every action in the plan: kind, owner, due, expiry, gates | Only when the plan changes |
| `cpmd/settings.json` | CPMD's decisions, values (dates, links), GetResponse ids, `approved_copy`, `done` | You, from Joel's messages |
| `ledger/cpmd-agent.json` | What the agent has sent | The agent only. Never edit by hand |

Copy is in `cpmd/copy/`. `{{key}}` placeholders are filled from settings; `[[firstname]]` is a
GetResponse merge tag and stays as is.

## Hard rules

1. **Never edit copy to get past a gate.** If a placeholder has no value, the email waits. A lead
   never sees "[intake date]".
2. **`approved_copy` is a sign-off.** Add a copy id only when Joel says that specific email or post
   is approved, in this conversation. Show him the rendered text first (`status` shows why it's
   blocked; render it by reading the copy file and filling values).
3. **Decisions come from CPMD.** Set a decision to `true` only when Joel passes on CPMD's answer.
   `reactivation_lists_opted_in` is a POPIA question: never set it on an inference.
4. **Dry-run before any live run in this container**, and commit + push `ledger/` after it.

## Common jobs

- **Brief:** `python3 scripts/cpmd_agent.py brief`. Lead with overdue items and the top three
  unblockers, not the full list.
- **Joel passes on an answer:** update `cpmd/settings.json`, run `brief`, tell him what it unblocked
  and what is still waiting. Commit and push.
- **A human action is done:** add its id to `settings.done`.
- **First GetResponse setup:** `python3 scripts/cpmd_agent.py check` (needs `GETRESPONSE_API_KEY`
  and network access to api.getresponse.com) lists list names and from-field ids.

## What the agent does not do

GetResponse's API cannot build workflows, so the E1-E6 nurture, the Delete-from-Lists fix and
reply tagging are human actions (`GR1`-`GR3`). Website, Meta Ads, WhatsApp follow-up and
Graeme's own emails and posts are human by design. They appear in the brief with owner and date.
