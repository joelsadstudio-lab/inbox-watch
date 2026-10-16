#!/usr/bin/env python3
"""CPMD lead-strategy agent: works the action register in cpmd/actions.json.

Stdlib only. Dry-run by default; pass --live to send anything.

Commands:
  status                 Every action and its state (READY / BLOCKED / WAITING / DONE / ...)
  brief                  Weekly brief for Joel: overdue, due this week, what unblocks what
  run [--live]           Execute READY agent actions: GetResponse broadcasts, Buffer posts, reports
  check                  GetResponse: list lists (campaigns) and from-fields to fill settings.json

The agent only acts when every gate passes:
  - each decision the action names is set in cpmd/settings.json
  - every {{placeholder}} in its copy has a value (no "[intake date]" ever reaches a lead)
  - the copy id is in settings.approved_copy (a person signed it off)
  - it is not past its expiry date (a PDE email after registration closes is noise)
Every send is recorded in ledger/cpmd-agent.json, so nothing is sent twice. Commit it.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
CPMD = ROOT / "cpmd"
COPY_DIR = CPMD / "copy"
OUT_DIR = ROOT / "out"
LEDGER_FILE = ROOT / "ledger" / "cpmd-agent.json"
GR_ENDPOINT = "https://api.getresponse.com/v3"
LOOKAHEAD_DAYS = 3        # GetResponse broadcasts are created this far ahead, scheduled with sendOn
MAX_SENDS_PER_RUN = 12    # safety cap: one full reactivation wave is 4
PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")
SOCIAL_PLATFORMS = {"facebook": "facebook", "instagram": "facebook", "linkedin": "linkedin"}
SOCIAL_TIME = "09:00"


@dataclass
class Action:
    id: str
    title: str
    kind: str
    owner: str
    raw: dict
    course: str | None = None
    due: date | None = None
    expires: date | None = None
    state: str = ""  # READY | BLOCKED | WAITING | DONE | EXPIRED | OVERDUE | OPEN
    reasons: list[str] = field(default_factory=list)
    subject: str = ""
    body: str = ""
    sender: str = "team"


# ---------------------------------------------------------------- loading

def load_json(path: Path, default=None):
    if not path.exists():
        if default is not None:
            return default
        sys.exit(f"Missing {path}")
    return json.loads(path.read_text())


def save_ledger(ledger: dict) -> None:
    LEDGER_FILE.parent.mkdir(exist_ok=True)
    LEDGER_FILE.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n")


def context(settings: dict, course: str | None = None) -> dict:
    """Placeholder values. Missing and null are the same thing: unresolved."""
    ctx = {k: v for k, v in settings.get("values", {}).items() if v}
    for code, c in settings.get("courses", {}).items():
        for key in ("intake_date", "brochure_link", "name"):
            if c.get(key):
                ctx[f"{key}_{code}"] = c[key]
    if course:
        c = settings["courses"][course]
        for key, ph in (("name", "course_name"), ("intake_date", "intake_date"), ("course_line", "course_line")):
            if c.get(key):
                ctx[ph] = c[key]
    return ctx


def render(text: str, ctx: dict) -> tuple[str, list[str]]:
    missing = sorted({m for m in PLACEHOLDER.findall(text) if m not in ctx})
    return PLACEHOLDER.sub(lambda m: str(ctx.get(m.group(1), m.group(0))), text), missing


COURSE_KEYS = {"course_name": "name", "intake_date": "intake_date", "course_line": "course_line"}


def setting_path(placeholder: str, course: str | None) -> str:
    """Where in settings.json a missing placeholder gets its value."""
    if course and placeholder in COURSE_KEYS:
        return f"courses.{course}.{COURSE_KEYS[placeholder]}"
    m = re.fullmatch(r"(intake_date|brochure_link|name)_([A-Z]+)", placeholder)
    if m:
        return f"courses.{m.group(2)}.{m.group(1)}"
    return f"values.{placeholder}"


def parse_copy(copy_id: str) -> tuple[dict, str]:
    raw = (COPY_DIR / f"{copy_id}.md").read_text()
    head, _, body = raw.partition("\n---\n")
    meta = dict(line.split(":", 1) for line in head.splitlines() if ":" in line)
    return {k.strip(): v.strip() for k, v in meta.items()}, body.strip()


def parse_day(value, ctx: dict) -> tuple[date | None, list[str]]:
    if not value:
        return None, []
    text, missing = render(value, ctx)
    if missing:
        return None, missing
    try:
        return date.fromisoformat(text), []
    except ValueError:
        return None, [f"bad date '{text}'"]


def expand(actions: list[dict]) -> list[dict]:
    out = []
    for a in actions:
        for course in a.get("courses") or [None]:
            item = dict(a, course=course)
            if course:
                item["id"] = f"{a['id']}-{course}"
            out.append(item)
    return out


# ---------------------------------------------------------------- evaluation

def gate_failures(gates: list, decisions: dict) -> list[str]:
    fails = []
    for g in gates:
        if isinstance(g, list):
            key, want = g
            if decisions.get(key) != want:
                fails.append(f"decision {key} must be '{want}' (is {decisions.get(key)!r})")
        elif decisions.get(g) is not True:
            fails.append(f"decision {g} not confirmed")
    return fails


def evaluate(raw: dict, settings: dict, ledger: dict, today: date, have_key: bool) -> Action:
    a = Action(id=raw["id"], title=raw["title"], kind=raw["kind"], owner=raw["owner"], raw=raw,
               course=raw.get("course"))
    ctx = context(settings, a.course)
    social = load_json(COPY_DIR / "social.json") if a.kind == "buffer_post" else {}
    due_src = social[raw["copy"]]["date"] if a.kind == "buffer_post" else raw.get("due")
    a.due, due_missing = parse_day(due_src, ctx)
    a.expires, exp_missing = parse_day(raw.get("expires"), ctx)

    if a.id in settings.get("done", []) or a.id in ledger:
        a.state = "DONE"
        return a

    if a.kind == "human":
        if due_missing:
            a.state, a.reasons = "OPEN", [f"date waits on {', '.join(due_missing)}"]
        elif a.due and a.due < today:
            a.state = "OVERDUE"
        else:
            a.state = "OPEN"
        return a

    if a.expires and a.expires < today:
        a.state = "EXPIRED"
        return a
    if a.kind == "buffer_post" and a.due and a.due < today:
        a.state = "EXPIRED"
        return a

    reasons = gate_failures(raw.get("gates", []), settings.get("decisions", {}))
    missing = set(due_missing + exp_missing)

    if a.kind == "getresponse_broadcast":
        meta, body = parse_copy(raw["copy"])
        a.sender = meta.get("from", "team")
        a.subject, m1 = render(meta.get("subject", ""), ctx)
        a.body, m2 = render(body, ctx)
        missing.update(m1 + m2)
        if not settings["getresponse"]["from_field_ids"].get(a.sender):
            reasons.append(f"needs getresponse.from_field_ids.{a.sender}")
        if raw.get("audience") == "pde_audience_lists" and not settings["getresponse"].get("pde_audience_lists"):
            reasons.append("needs getresponse.pde_audience_lists (the PDE lists have 0 contacts)")
        if not have_key:
            reasons.append("GETRESPONSE_API_KEY not set")
    elif a.kind == "buffer_post":
        texts = social[raw["copy"]]
        for platform in ("facebook", "linkedin"):
            missing.update(render(texts[platform], ctx)[1])

    reasons += [f"needs {setting_path(m, a.course)}" for m in sorted(missing)]
    if a.kind != "report" and raw.get("copy") not in settings.get("approved_copy", []):
        reasons.append(f"copy {raw.get('copy')} not in approved_copy")

    if reasons:
        a.state, a.reasons = "BLOCKED", reasons
    elif a.due and a.due > today + timedelta(days=LOOKAHEAD_DAYS if a.kind != "report" else 0):
        a.state = "WAITING"
    else:
        a.state = "READY"
    return a


def evaluate_all(settings, actions, ledger, today, have_key) -> list[Action]:
    return [evaluate(r, settings, ledger, today, have_key) for r in expand(actions)]


# ---------------------------------------------------------------- GetResponse

def gr(method: str, path: str, payload: dict | None = None, params: dict | None = None):
    key = os.environ.get("GETRESPONSE_API_KEY")
    if not key:
        raise RuntimeError("GETRESPONSE_API_KEY is not set")
    url = GR_ENDPOINT + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, method=method, headers={
        "X-Auth-Token": f"api-key {key}", "Content-Type": "application/json"},
        data=json.dumps(payload).encode() if payload is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            return json.loads(body) if body else {}, dict(resp.headers)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GetResponse {e.code} on {method} {path}: {e.read().decode()[:300]}") from e


def campaign_id(name: str, cache: dict) -> str:
    if name not in cache:
        found, _ = gr("GET", "/campaigns", params={"query[name]": name})
        exact = [c for c in found if c.get("name") == name]
        if len(exact) != 1:
            raise RuntimeError(f"GetResponse list '{name}' matched {len(exact)} lists, expected exactly 1")
        cache[name] = exact[0]["campaignId"]
    return cache[name]


def to_html(body: str) -> str:
    parts = []
    for block in body.split("\n\n"):
        lines = block.splitlines()
        if all(l.startswith("- ") for l in lines):
            parts.append("<ul>" + "".join(f"<li>{html.escape(l[2:])}</li>" for l in lines) + "</ul>")
        else:
            parts.append("<p>" + "<br>".join(html.escape(l) for l in lines) + "</p>")
    return "\n".join(parts)


def send_on(a: Action, settings: dict, now: datetime) -> datetime:
    tz = ZoneInfo(settings["timezone"])
    hh, mm = map(int, settings["send_time"].split(":"))
    when = datetime.combine(a.due or now.astimezone(tz).date(), datetime.min.time(), tz).replace(hour=hh, minute=mm)
    return max(when, now + timedelta(minutes=15))


def newsletter_payload(a: Action, settings: dict, cache: dict, now: datetime) -> dict:
    g = settings["getresponse"]
    lists = ([settings["courses"][a.course]["list"]] if a.raw.get("audience") == "course_list"
             else g["pde_audience_lists"])
    selected = [campaign_id(n, cache) for n in lists]
    sender = g["from_field_ids"][a.sender]
    return {
        "name": f"cpmd-agent {a.id}",
        "subject": a.subject,
        "fromField": {"fromFieldId": sender},
        "replyTo": {"fromFieldId": sender},
        "campaign": {"campaignId": selected[0]},
        "content": {"plain": a.body, "html": to_html(a.body)},
        "sendOn": send_on(a, settings, now).isoformat(timespec="seconds"),
        "sendSettings": {
            "selectedCampaigns": selected,
            "selectedSegments": [], "selectedSuppressions": [], "selectedContacts": [],
            "excludedCampaigns": [campaign_id(g["registered_list"], cache)],
            "excludedSegments": [],
            "timeTravel": "false", "perfectTiming": "false",
        },
    }


# ---------------------------------------------------------------- Buffer (reuses buffer_scheduler.py)

def social_csv(ready: list[Action], settings: dict) -> Path:
    social = load_json(COPY_DIR / "social.json")
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / "cpmd-social.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Date", "Time", "Platform", "Caption", "Media URL", "Status"])
        for a in ready:
            post = social[a.raw["copy"]]
            for platform, source in SOCIAL_PLATFORMS.items():
                text, _ = render(post[source], context(settings))
                w.writerow([post["date"], SOCIAL_TIME, platform, text, post.get(f"media_{platform}", ""), "Approved"])
    return path


# ---------------------------------------------------------------- reports

def scorecard(a: Action, settings: dict, live: bool) -> Path:
    rows = []
    lists = [c["list"] for c in settings["courses"].values()] + [settings["getresponse"]["registered_list"]]
    cache: dict = {}
    for name in lists:
        count = "n/a (dry run)"
        if live:
            try:
                _, headers = gr("GET", "/contacts", params={"query[campaignId]": campaign_id(name, cache), "perPage": 1})
                count = headers.get("TotalCount", "n/a")
            except RuntimeError as e:
                count = f"error: {str(e)[:80]}"
        rows.append(f"| {name} | {count} |")
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / f"scorecard-{a.due}.md"
    path.write_text("\n".join([
        f"# CPMD scorecard, {a.due}", "",
        "## From GetResponse (pulled by the agent)", "", "| List | Contacts |", "|---|---|", *rows, "",
        "## To fill in by hand (no system holds these yet)", "",
        "| Measure | Value |", "|---|---|",
        "| Paid registrations this month, per course | |",
        "| Registration revenue vs marketing spend | |",
        "| Leads contacted within 1 business hour (target 80% by end Nov) | |",
        "| Leads reached in a two-way conversation | |",
        "| Qualified leads (start within 3 months, or employer-funded) | |",
        "| Lost leads with a recorded reason | |",
        "| Reactivation replies and registrations | |",
        "| Webinar registrations / attendance | |",
        "| Employer conversations opened | |",
        "| Paid cost per qualified lead (Meta) | |", "",
    ]))
    return path


# ---------------------------------------------------------------- output

def print_status(items: list[Action]) -> None:
    print("| Action | Kind | Owner | Due | State | Why |")
    print("|---|---|---|---|---|---|")
    for a in items:
        why = "; ".join(a.reasons).replace("|", "/")
        print(f"| {a.id} | {a.kind} | {a.owner} | {a.due or '?'} | {a.state} | {why} |")
    counts: dict[str, int] = {}
    for a in items:
        counts[a.state] = counts.get(a.state, 0) + 1
    print("\n" + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))


def unblockers(items: list[Action]) -> list[tuple[str, list[str]]]:
    """Which single setting unblocks which actions, most impactful first."""
    by_reason: dict[str, list[str]] = {}
    for a in items:
        if a.state == "BLOCKED":
            for r in a.reasons:
                by_reason.setdefault(r, []).append(a.id)
    return sorted(by_reason.items(), key=lambda kv: -len(kv[1]))


def brief(items: list[Action], today: date) -> str:
    week = today + timedelta(days=7)
    overdue = [a for a in items if a.state == "OVERDUE"]
    soon = [a for a in items if a.state in ("OPEN", "READY", "WAITING", "BLOCKED") and a.due and today <= a.due <= week]
    lines = [f"# CPMD agent brief, {today:%a %d %b %Y}", ""]
    lines += ["## Overdue (people)", ""] + ([f"- **{a.owner}**: {a.title} (due {a.due:%d %b})" for a in overdue] or ["- None"])
    lines += ["", "## Due in the next 7 days", ""]
    lines += [f"- {a.due:%a %d %b} · {a.owner} · {a.title}{' ' + a.course if a.course else ''}{' · BLOCKED' if a.state == 'BLOCKED' else ''}" for a in sorted(soon, key=lambda x: x.due)] or ["- Nothing"]
    lines += ["", "## What unblocks the agent", "", "Each line is one answer from CPMD or one edit to `cpmd/settings.json`.", ""]
    lines += [f"- `{r}` → {len(ids)} action(s): {', '.join(ids)}" for r, ids in unblockers(items)] or ["- Nothing blocked"]
    expired = [a.id for a in items if a.state == "EXPIRED"]
    if expired:
        lines += ["", "## Expired without sending", "", f"- {', '.join(expired)}"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- commands

def cmd_run(items: list[Action], settings: dict, ledger: dict, live: bool, now: datetime) -> int:
    ready = [a for a in items if a.state == "READY"]
    failed = 0
    emails = [a for a in ready if a.kind == "getresponse_broadcast"]
    if len(emails) > MAX_SENDS_PER_RUN:
        sys.exit(f"{len(emails)} broadcasts ready exceeds the cap of {MAX_SENDS_PER_RUN}. Check settings.json.")
    cache: dict = {}
    for a in emails:
        if not live:
            print(f"DRY  {a.id}: '{a.subject}' to {a.course or 'PDE lists'}")
            continue
        try:
            payload = newsletter_payload(a, settings, cache, now)
            result, _ = gr("POST", "/newsletters", payload)
            ledger[a.id] = {"newsletterId": result.get("newsletterId"), "sendOn": payload["sendOn"],
                            "subject": a.subject, "created_at": now.isoformat(timespec="seconds")}
            save_ledger(ledger)  # after every send so a crash can't cause a duplicate
            print(f"SENT {a.id}: newsletter {result.get('newsletterId')} scheduled {payload['sendOn']}")
        except Exception as e:  # noqa: BLE001 - report and keep going
            failed += 1
            print(f"FAIL {a.id}: {str(e)[:300]}")

    posts = [a for a in ready if a.kind == "buffer_post"]
    if posts:
        path = social_csv(posts, settings)
        cmd = [sys.executable, str(ROOT / "scripts" / "buffer_scheduler.py"),
               "schedule" if live else "validate", "--client", "CPMD", "--csv", str(path)]
        if live:
            cmd.append("--live")
        print(f"\nBuffer: {len(posts)} post(s) → {path.relative_to(ROOT)}")
        failed += subprocess.call(cmd) != 0

    for a in (x for x in ready if x.kind == "report"):
        path = scorecard(a, settings, live)
        print(f"REPORT {a.id}: {path.relative_to(ROOT)}")
        if live:
            ledger[a.id] = {"file": str(path.relative_to(ROOT)), "created_at": now.isoformat(timespec="seconds")}
            save_ledger(ledger)

    if not ready:
        print("Nothing ready. `brief` shows what is blocking.")
    elif not live:
        print("\nDry run. Nothing sent. Add --live to send.")
    return 1 if failed else 0


def cmd_check() -> int:
    lists, _ = gr("GET", "/campaigns", params={"perPage": 100})
    print("Lists (campaigns):")
    for c in lists:
        print(f"  {c['campaignId']:<12} {c['name']}")
    fields, _ = gr("GET", "/from-fields")
    print("\nFrom-fields (put the ids in settings.getresponse.from_field_ids):")
    for f in fields:
        print(f"  {f['fromFieldId']:<12} {f.get('name', '')} <{f.get('email', '')}>")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("status", "brief", "run"):
        p = sub.add_parser(name)
        p.add_argument("--today", help="override today's date (YYYY-MM-DD), for testing")
        if name == "run":
            p.add_argument("--live", action="store_true", help="actually send")
        if name == "brief":
            p.add_argument("--write", action="store_true", help="also write out/cpmd-brief.md")
    sub.add_parser("check")
    args = ap.parse_args(argv)
    if args.cmd == "check":
        return cmd_check()

    settings = load_json(CPMD / "settings.json")
    actions = load_json(CPMD / "actions.json")
    ledger = load_json(LEDGER_FILE, default={})
    now = datetime.now(timezone.utc)
    today = date.fromisoformat(args.today) if args.today else now.astimezone(ZoneInfo(settings["timezone"])).date()
    items = evaluate_all(settings, actions, ledger, today, bool(os.environ.get("GETRESPONSE_API_KEY")))

    if args.cmd == "status":
        print_status(items)
        return 0
    if args.cmd == "brief":
        text = brief(items, today)
        print(text)
        if args.write:
            OUT_DIR.mkdir(exist_ok=True)
            (OUT_DIR / "cpmd-brief.md").write_text(text)
        return 0
    return cmd_run(items, settings, ledger, args.live, now)


if __name__ == "__main__":
    sys.exit(main())
