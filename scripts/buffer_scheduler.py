#!/usr/bin/env python3
"""Validate a client content calendar (CSV export) and schedule approved rows in Buffer.

Stdlib only. Dry-run by default; pass --live to actually create posts.

Commands:
  channels                          List Buffer organisations + channels (to fill clients.json)
  validate --client X --csv FILE    Report what would happen to every row
  schedule --client X --csv FILE    Same report, then create posts for READY rows (--live)

Buffer's API is create-only (no edit/delete), so every created post is recorded in
ledger/<client>.json and that slot is never sent again. Edits after scheduling are manual.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
CLIENTS_FILE = ROOT / "clients.json"
LEDGER_DIR = ROOT / "ledger"
BUFFER_ENDPOINT = "https://api.buffer.com"

# Character limits per platform. Conservative: Buffer may reject earlier.
CAPTION_LIMITS = {
    "x": 280,
    "twitter": 280,
    "bluesky": 300,
    "threads": 500,
    "pinterest": 500,
    "instagram": 2200,
    "tiktok": 2200,
    "linkedin": 3000,
    "youtube": 5000,
    "googlebusiness": 1500,
    "mastodon": 500,
    "facebook": 63206,
}
MEDIA_REQUIRED = {"instagram", "tiktok", "pinterest", "youtube"}
PLATFORM_ALIASES = {
    "ig": "instagram",
    "insta": "instagram",
    "fb": "facebook",
    "li": "linkedin",
    "twitter": "x",
    "gbp": "googlebusiness",
    "google business": "googlebusiness",
    "google business profile": "googlebusiness",
    "yt": "youtube",
}
VIDEO_EXT = (".mp4", ".mov", ".m4v", ".webm")
MIN_LEAD = timedelta(minutes=10)

# Column headers accepted (lower-cased, stripped) -> canonical field.
HEADER_MAP = {
    "date": "date",
    "post date": "date",
    "publish date": "date",
    "time": "time",
    "post time": "time",
    "publish time": "time",
    "platform": "platform",
    "channel": "platform",
    "network": "platform",
    "caption": "caption",
    "copy": "caption",
    "post copy": "caption",
    "text": "caption",
    "media": "media",
    "media url": "media",
    "media urls": "media",
    "asset": "media",
    "asset link": "media",
    "image": "media",
    "status": "status",
    "approval": "status",
}


@dataclass
class Row:
    line: int
    date: str = ""
    time: str = ""
    platform: str = ""
    caption: str = ""
    media: str = ""
    status: str = ""
    # Filled by validation
    verdict: str = ""  # READY | SKIP | ERROR | SCHEDULED | FAILED
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    due_utc: datetime | None = None
    channel_id: str = ""
    media_urls: list[str] = field(default_factory=list)
    slot_key: str = ""
    post_id: str = ""


# ---------------------------------------------------------------- config / ledger

def load_clients(path: Path = CLIENTS_FILE) -> dict:
    if not path.exists():
        sys.exit(f"Missing {path}. Copy clients.example.json to clients.json and fill it in.")
    return json.loads(path.read_text())


def get_client(clients: dict, name: str) -> dict:
    for key, cfg in clients.items():
        if key.lower() == name.lower():
            return {"name": key, **cfg}
    sys.exit(f"Unknown client '{name}'. Known: {', '.join(clients)}")


def ledger_path(client: str) -> Path:
    return LEDGER_DIR / f"{re.sub(r'[^a-z0-9]+', '-', client.lower()).strip('-')}.json"


def load_ledger(client: str) -> dict:
    p = ledger_path(client)
    return json.loads(p.read_text()) if p.exists() else {}


def save_ledger(client: str, ledger: dict) -> None:
    LEDGER_DIR.mkdir(exist_ok=True)
    ledger_path(client).write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------- parsing

def read_csv(path: Path) -> list[Row]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        rows = list(reader)
    if not rows:
        return []
    header = [HEADER_MAP.get(h.strip().lower(), "") for h in rows[0]]
    missing = {"date", "time", "platform", "caption", "status"} - set(header)
    if missing:
        sys.exit(f"CSV is missing required columns: {', '.join(sorted(missing))}. "
                 f"Found headers: {rows[0]}")
    out = []
    for i, values in enumerate(rows[1:], start=2):
        if not any(v.strip() for v in values):
            continue
        r = Row(line=i)
        for col, val in zip(header, values):
            if col:
                setattr(r, col, val.strip())
        out.append(r)
    return out


def parse_date(s: str):
    # ISO first, then South African DD/MM/YYYY. Never MM/DD.
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def parse_time(s: str):
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p", "%I%p"):
        try:
            return datetime.strptime(s.upper().replace(".", ""), fmt).time()
        except ValueError:
            pass
    return None


def normalise_platform(s: str) -> str:
    p = s.strip().lower()
    return PLATFORM_ALIASES.get(p, p.replace(" ", ""))


def normalise_media(raw: str, warnings: list[str]) -> tuple[list[str], list[str]]:
    """Return (urls, errors). Rewrites Drive share links to direct-download links."""
    urls, errors = [], []
    for u in re.split(r"[\s|,]+", raw):
        if not u:
            continue
        if "canva.com/design" in u:
            errors.append(f"Canva edit link is not media; export the design and link the file ({u[:60]})")
            continue
        m = re.search(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?(?:export=\w+&)?id=)([\w-]+)", u)
        if m:
            u = f"https://drive.google.com/uc?export=download&id={m.group(1)}"
            warnings.append("Drive file must be shared 'Anyone with the link' or Buffer can't fetch it")
        elif "drive.google.com/drive/folders" in u:
            errors.append("Drive folder link; link the individual file instead")
            continue
        if not u.startswith("https://"):
            errors.append(f"media must be a public https URL ({u[:60]})")
            continue
        urls.append(u)
    return urls, errors


def slot_key(client: str, platform: str, due_utc: datetime) -> str:
    raw = f"{client.lower()}|{platform}|{due_utc.isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- validation

def validate(rows: list[Row], client: dict, ledger: dict, now: datetime | None = None) -> list[Row]:
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(client.get("timezone", "Africa/Johannesburg"))
    channels = {normalise_platform(k): v for k, v in client.get("channels", {}).items()}
    seen: dict[str, int] = {}

    for r in rows:
        status = r.status.strip().lower()
        if status != "approved":
            r.verdict = "SKIP"
            r.reasons.append(f"not approved (status: {r.status or 'blank'})")
            continue

        platform = normalise_platform(r.platform)
        r.platform = platform
        d, t = parse_date(r.date), parse_time(r.time)
        if not r.date or d is None:
            r.reasons.append(f"bad/missing date '{r.date}' (use YYYY-MM-DD or DD/MM/YYYY)")
        if not r.time or t is None:
            r.reasons.append(f"bad/missing time '{r.time}' (use 24h HH:MM)")
        if not platform:
            r.reasons.append("missing platform")
        elif platform not in channels:
            r.reasons.append(f"no Buffer channel mapped for '{platform}' in clients.json")
        else:
            r.channel_id = channels[platform]
        if not r.caption:
            r.reasons.append("missing caption")
        limit = CAPTION_LIMITS.get(platform)
        if limit and len(r.caption) > limit:
            r.reasons.append(f"caption {len(r.caption)} chars > {platform} limit {limit}")

        r.media_urls, media_errors = normalise_media(r.media, r.warnings)
        r.reasons.extend(media_errors)
        if platform in MEDIA_REQUIRED and not r.media_urls and not media_errors:
            r.reasons.append(f"{platform} requires media")

        if d and t:
            r.due_utc = datetime.combine(d, t, tzinfo=tz).astimezone(timezone.utc)
            if r.due_utc < now + MIN_LEAD:
                r.verdict = "SKIP"
                r.reasons.append(f"in the past / under {int(MIN_LEAD.total_seconds() // 60)} min away")
                continue
            if platform:
                r.slot_key = slot_key(client["name"], platform, r.due_utc)
                if r.slot_key in ledger:
                    r.verdict = "SKIP"
                    r.post_id = ledger[r.slot_key]["post_id"]
                    r.reasons = [f"already scheduled (Buffer post {r.post_id}); edit in Buffer if changed"]
                    continue

        if r.reasons:
            r.verdict = "ERROR"
            continue

        if r.slot_key in seen:
            r.verdict = "ERROR"
            r.reasons.append(f"duplicate slot: same platform + time as row {seen[r.slot_key]}")
            continue
        seen[r.slot_key] = r.line
        r.verdict = "READY"
    return rows


# ---------------------------------------------------------------- Buffer API

def gql(query: str, variables: dict | None = None) -> dict:
    key = os.environ.get("BUFFER_API_KEY")
    if not key:
        sys.exit("BUFFER_API_KEY is not set. Generate one in Buffer > Settings > API.")
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        BUFFER_ENDPOINT,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Buffer HTTP {e.code}: {e.read().decode(errors='replace')[:500]}") from e
    if data.get("errors"):
        raise RuntimeError(f"Buffer GraphQL error: {data['errors']}")
    return data["data"]


# NOTE: written against Buffer's public-beta GraphQL docs (Oct 2026). The `channels`
# command is the cheapest way to confirm the schema still matches; run it first.
ORGS_QUERY = "query { account { organizations { id name } } }"
CHANNELS_QUERY = """
query Channels($input: ChannelsInput!) {
  channels(input: $input) { id name service }
}"""
CREATE_POST = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess { post { id dueAt status } }
    ... on MutationError { message }
  }
}"""


def build_post_input(r: Row) -> dict:
    inp = {
        "channelId": r.channel_id,
        "text": r.caption,
        "schedulingType": "automatic",
        "mode": "customScheduled",
        "dueAt": r.due_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    if r.media_urls:
        images = [{"url": u} for u in r.media_urls if not u.lower().endswith(VIDEO_EXT)]
        videos = [{"url": u} for u in r.media_urls if u.lower().endswith(VIDEO_EXT)]
        assets = {}
        if images:
            assets["images"] = images
        if videos:
            assets["videos"] = videos
        inp["assets"] = assets
    return inp


def create_post(r: Row) -> str:
    result = gql(CREATE_POST, {"input": build_post_input(r)})["createPost"]
    if "post" in result and result["post"]:
        return result["post"]["id"]
    raise RuntimeError(result.get("message") or f"unexpected response: {result}")


# ---------------------------------------------------------------- output

def report(rows: list[Row], client: dict) -> None:
    tz = ZoneInfo(client.get("timezone", "Africa/Johannesburg"))
    counts: dict[str, int] = {}
    print(f"\n## {client['name']} — {len(rows)} rows\n")
    print("| Row | Local time | Platform | Verdict | Notes |")
    print("|---|---|---|---|---|")
    for r in rows:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
        when = r.due_utc.astimezone(tz).strftime("%a %d %b %H:%M") if r.due_utc else f"{r.date} {r.time}"
        notes = "; ".join(r.reasons + [f"⚠ {w}" for w in dict.fromkeys(r.warnings)])
        if r.post_id and r.verdict == "SCHEDULED":
            notes = f"Buffer post {r.post_id}" + (f"; {notes}" if notes else "")
        print(f"| {r.line} | {when} | {r.platform} | {r.verdict} | {notes.replace('|', '/')} |")
    print("\n" + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))


# ---------------------------------------------------------------- commands

def cmd_channels(_args) -> None:
    orgs = gql(ORGS_QUERY)["account"]["organizations"]
    for org in orgs:
        print(f"\nOrganisation: {org['name']} ({org['id']})")
        for ch in gql(CHANNELS_QUERY, {"input": {"organizationId": org["id"]}})["channels"]:
            print(f"  {ch['service']:<16} {ch['name']:<40} {ch['id']}")


def cmd_validate(args, live: bool = False) -> int:
    client = get_client(load_clients(), args.client)
    ledger = load_ledger(client["name"])
    rows = validate(read_csv(Path(args.csv)), client, ledger)

    if live:
        ready = [r for r in rows if r.verdict == "READY"]
        if len(ready) > args.max_posts:
            sys.exit(f"{len(ready)} posts ready exceeds --max-posts {args.max_posts}. Raise it deliberately.")
        for i, r in enumerate(ready):
            try:
                r.post_id = create_post(r)
                r.verdict = "SCHEDULED"
                ledger[r.slot_key] = {
                    "post_id": r.post_id,
                    "platform": r.platform,
                    "due_utc": r.due_utc.isoformat(),
                    "caption_start": r.caption[:60],
                    "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                save_ledger(client["name"], ledger)  # after every post so a crash can't cause dupes
            except Exception as e:  # noqa: BLE001 - report and keep going
                r.verdict = "FAILED"
                r.reasons.append(str(e)[:300])
            if i < len(ready) - 1:
                time.sleep(1)  # Buffer beta limit ~100 req / 15 min
    report(rows, client)
    if not live and any(r.verdict == "READY" for r in rows):
        print("\nDry run. Nothing sent to Buffer. Re-run `schedule ... --live` to create these posts.")
    return 1 if any(r.verdict in ("ERROR", "FAILED") for r in rows) else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("channels")
    for name in ("validate", "schedule"):
        p = sub.add_parser(name)
        p.add_argument("--client", required=True)
        p.add_argument("--csv", required=True)
        if name == "schedule":
            p.add_argument("--live", action="store_true", help="actually create posts in Buffer")
            p.add_argument("--max-posts", type=int, default=60, help="safety cap per run")
    args = ap.parse_args(argv)
    if args.cmd == "channels":
        cmd_channels(args)
        return 0
    return cmd_validate(args, live=args.cmd == "schedule" and args.live)


if __name__ == "__main__":
    sys.exit(main())
