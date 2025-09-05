# Discourse-ICS-importer-by-REST-API
Import and continuously sync events from an iCalendar (ICS) feed into a Discourse category via the Discourse REST API—idempotent, logs with systemd, and safe to run on a timer.

---

## What it does

- **Parses an ICS feed** and creates/updates one Discourse topic per event. (If the same time/date & optionally location, UID deosn't matter)
- De-duplication by UID is priortised: each event is keyed by its ICS `UID` using a stable hash-based tag and the full mash invisible in the event topic first post's body, so the same event is updated rather than duplicated, starting with easiest way of checking.
- **Timer-safe:** designed for periodic runs (e.g., every 4 hours). A file lock prevents overlapping executions.
- **Respectful tag handling:** optional tags are set on **first create** only; subsequent updates leave your manually-added tags intact, but may create new tags if the ics feed is "noisy".
- **Timezone aware:** formats times using the timezone of your choosing for human-readable bodies.

## How it works

- Parses an ICS feed into one topic per event.
- Matches existing topics by UID tag or hidden marker.
- Falls back to time/location matching when no UID is present.
- Updates existing topics instead of creating duplicates.

## How it works (high level)

- Parses events from the ICS feed.
- Looks up by UID tag (hash) or hidden marker.
- If not found, searches /search.json by event start/end (with verification).
- Falls back to scanning /latest.json pages **only on API error**.
- Deduplication can be strict (time+location) or looser (time-only mode with `--time-only-dedupe`).
- On updates, tries to suppress topic bumps with `bypass_bump`; if the instance ignores it,
  falls back to invoking `/reset-bump-date` and logs when that happens.
- Also resets bump date after tag merges (to avoid “false bumps”).
- Creates new topics or updates existing ones without changing category or manually-edited titles.

## Usage

| Command | Description |
|---------|-------------|
| `python3 ics_to_discourse.py --ics my.ics --category-id 12` | Import events from a local `.ics` file into category 12 |
| `python3 ics_to_discourse.py --ics https://example.com/cal.ics --static-tags calendar,google` | Import events from a remote `.ics` URL and add static tags `calendar, google` |

### Optional flags

| Flag | Description |
|------|-------------|
| `--scan-pages N` | How many `/latest` pages to scan if `/search.json` fails (default: 8) |
| `--time-only-dedupe` | Treat events with the same start/end as duplicates even if location differs |
## Debugging

When running under `systemd`, follow logs live:

`journalctl -u ics-sync.service -f`

Typical log patterns:

- **Duplicate scan context**  
  `INFO: [dup-scan] site_tz=Europe/London`  
  `INFO: [dup-scan] summary=Meeting about peas - Lecture loc=office V3`  
  `INFO: [dup-scan] candidates=[('2025-10-17 10:00','2025-10-17 11:00','up physics b1'), ...]`

- **Adoption paths**  
  `INFO: [ics-sync] Adopting existing topic via time-window search: 5272`  
  `INFO: [ics-sync] Adopting existing topic by time match (time-only mode): 5272`  
  `INFO: [ics-sync] Adopting existing topic by site-wide match: 5272 (start=... end=... loc=...)`

- **Bump suppression**  
  `INFO: Invoking reset-bump-date fallback for topic 5272`  
  `WARNING: reset-bump-date failed for topic 5272: 403 Client Error: Forbidden for url: ...`  
  > If you see a 403 here, your API key is not a **global staff key**. This endpoint requires an admin/mod key tied to a staff user.

- **Topic creation**  
  `INFO: [ics-sync] Created new topic 5310 (title=... )`

These log lines make it clear *why* a topic was adopted or created, and whether the `reset-bump-date` fallback was needed.

## Requirements

- An OS with Python 3 and systemd support (tested on **Ubuntu 24.04 LTS**, ships with Python 3.12).  
- A Discourse API key with permission to create topics (and, if needed on first run, to use/create tags) in the target category.  

> ⚠️ **Important:** As per [Meta post](https://meta.discourse.org/t/syncing-ical-ics-feeds-into-discourse/379361/34), the API key must be a **global key** tied to a **staff user** (e.g. `system` or an admin account). Limited-scope keys will fail on some operations (such as reset-bump-date).

- Your Discourse base URL
- Your ics feed URL (you may need to append `.ics` to end of this)

## Configuration

Set your environment in an `.env` file (example):

```
DISCOURSE_BASE_URL=https://discuss.example.com

DISCOURSE_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
DISCOURSE_API_USERNAME=system
```

ICS & Discourse target:

```
ICS_URL=https://calendar.example.com/my.ics

CATEGORY_ID=42
SITE_TZ=Europe/London
```

Optional: comma-separated list:

```
DEFAULT_TAGS=events,calendar
```

> Tip: `SITE_TZ` is used to render friendly times in the post body.

## Install

Make sure Python 3 and Git are installed (on Ubuntu/Debian):

```
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

Clone this repository into `/opt/ics-sync` (matching the systemd examples):

```
sudo mkdir -p /opt/ics-sync
cd /opt/ics-sync
sudo git clone https://github.com/Ethsim12/Discourse-ICS-importer-by-REST-API.git
cd Discourse-ICS-importer-by-REST-API
```

Create a virtual environment and install dependencies:

```
python3 -m venv ../venv
source ../venv/bin/activate

pip install -r requirements.txt
```

Your directory layout should now look like:

```
/opt/ics-sync/
├── venv/
└── Discourse-ICS-importer-by-REST-API/
```

> Tip: secure with `chmod 700 /opt/ics-sync/`

## Run it

From the repo directory:

```
../venv/bin/python3 ics_to_discourse.py --ics "$ICS_URL" \
  --category-id "$CATEGORY_ID" \
  --site-tz "$SITE_TZ" \
  --static-tags "${DEFAULT_TAGS:-}"
```

---

## Running with systemd

If you want to run this script on a schedule, a `systemd` timer is recommended.

- Use [`flock`](https://man7.org/linux/man-pages/man1/flock.1.html) to prevent overlapping runs.  
  Without it, `systemd` may try to start a new instance while the previous one is still running.

- Example pattern (inside your service unit):

```
ExecStart=/usr/bin/flock -n /run/ics-sync.lock -c '/opt/ics-sync/venv/bin/python /path/to/ics_to_discourse.py'
```

This ensures only one run at a time. If the lock is busy, the new run exits immediately.  
By default, a timer with `Persistent=false` will not “catch up” on missed runs (e.g. if the machine was off).  
If you want catch-up behaviour, use `OnCalendar=` with `Persistent=true`.

---

### systemd (copy–paste)

Create an env file with your settings:

```
# /etc/ics-sync.env
DISCOURSE_BASE_URL=https://discuss.example.com
DISCOURSE_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
DISCOURSE_API_USERNAME=system

ICS_URL=https://calendar.example.com/my.ics
CATEGORY_ID=42
SITE_TZ=Europe/London
DISCOURSE_DEFAULT_TAGS=events,calendar
```

Create the service unit:

```
# /etc/systemd/system/ics-sync.service
[Unit]
Description=ICS → Discourse sync
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=www-data
Group=www-data
WorkingDirectory=/opt/ics-sync/Discourse-ICS-importer-by-REST-API
EnvironmentFile=/etc/ics-sync.env
ExecStart=/usr/bin/flock -n /run/ics-sync.lock -c '/opt/ics-sync/venv/bin/python /opt/ics-sync/Discourse-ICS-importer-by-REST-API/ics_to_discourse.py --ics "$ICS_URL" --category-id "$CATEGORY_ID" --site-tz "$SITE_TZ" --static-tags "${DEFAULT_TAGS:-}"'
StandardOutput=journal
StandardError=journal
```

Create the timer unit (example: every 6 hours):

```
# /etc/systemd/system/ics-sync.timer
[Unit]
Description=Run ICS → Discourse sync every 6 hours

[Timer]
OnBootSec=5m
OnUnitInactiveSec=6h
Persistent=false
Unit=ics-sync.service

[Install]
WantedBy=timers.target
```

Install & start:

```
sudo systemctl daemon-reload
sudo systemctl enable --now ics-sync.timer

# optional: run immediately
sudo systemctl start ics-sync.service

# check status
systemctl status ics-sync.timer
systemctl status ics-sync.service
```

**Tip — follow logs live:**

```
journalctl -u ics-sync.service -f     # live tail
journalctl -u ics-sync.service --since=today
journalctl -u ics-sync.timer --since=today
```

> Update `ExecStart` and `WorkingDirectory` paths if your repo/venv lives elsewhere.
