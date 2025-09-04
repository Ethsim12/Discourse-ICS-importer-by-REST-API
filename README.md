# Discourse-ICS-importer-by-REST-API
Import and continuously sync events from an iCalendar (ICS) feed into a Discourse category via the Discourse REST API—idempotent, logs with systemd, and safe to run on a timer.

---

## What it does

- **Parses an ICS feed** and creates/updates one Discourse topic per event. (If the same time/date & optionally location, UID deosn't matter)
- De-duplication by UID is priortised: each event is keyed by its ICS `UID` using a stable hash-based tag and the full mash invisible in the event topic first post's body, so the same event is updated rather than duplicated, starting with easiest way of checking.
- **Timer-safe:** designed for periodic runs (e.g., every 4 hours). A file lock prevents overlapping executions.
- **Respectful tag handling:** optional tags are set on **first create** only; subsequent updates leave your manually-added tags intact, but may create new tags if the ics feed is "noisy".
- **Timezone aware:** formats times using the timezone of your choosing for human-readable bodies.

## How it works (high level)

1. Fetch ICS → parse `VEVENT`s (SUMMARY, DTSTART, DTEND, LOCATION, DESCRIPTION, etc.).
2. Compute a stable per-event identifier (hash derived from ICS `UID`) → used as a hidden marker + ID tag.
3. For each event:
   - First, search Discourse topics by UID marker/tag via `/search.json`.  
   - If a match is found → **update** the first post body.  
   - If no match → scan recent topics (`/latest.json`, ~8 pages) for an existing event with the same start/end/location.  
     - If found → **adopt** that topic and add the UID marker.  
     - Else → **create** a new topic with default tags + UID tag.
4. A non-blocking file lock (`flock`) ensures that only one run executes at a time. If another run is already in progress, this one exits cleanly.

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
