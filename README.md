# Discourse-ICS-importer-by-REST-API
Import and continuously sync events from an iCalendar (ICS) feed into a Discourse category via the Discourse REST API—idempotent, timer-friendly, and safe to run on a schedule.

---

## What it does

- **Parses an ICS feed** and creates/updates one Discourse topic per event.
- **De-duplication by UID:** each event is keyed by its ICS `UID` using a stable hash-based tag, so the same event is updated rather than duplicated.
- **Timer-safe:** designed for periodic runs (e.g., every hour). A file lock prevents overlapping executions.
- **Respectful tag handling:** tags are set on **first create** only; subsequent updates leave your manually-added tags intact.
- **Timezone aware:** formats times using your Discourse site timezone for human-readable bodies.

## How it works (high level)

1. Fetch ICS → parse `VEVENT`s (SUMMARY/DTSTART/DTEND/LOCATION/DESCRIPTION, etc.).
2. Compute a stable per-event identifier (hash derived from `UID`) → used as an **ID tag**.
3. For each event:
   - If an existing topic with that ID tag exists → **update** the first post body.
   - Else → **create** a new topic with default tags + the ID tag.
4. Exit cleanly if another run is in progress (non-blocking lock).

## Requirements

- Python 3
- A Discourse API key with permission to create topics (and, if needed on first run, to use/create tags) in the target category.
- Your Discourse base URL and API username.

## Configuration

Set your environment in an `.env` file (example):

```
DISCOURSE_BASE_URL=https://discuss.example.com

DISCOURSE_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
DISCOURSE_API_USERNAME=system
```

ICS & Discourse target

```
ICS_URL=https://calendar.example.com/my.ics

CATEGORY_ID=42
SITE_TZ=Europe/London
```

Optional: comma-separated list

```
DEFAULT_TAGS=events,calendar
```


> Tip: `SITE_TZ` is used to render friendly times in the post body.

## Run it

From the repo directory:

```bash
python3 ics_to_discourse.py --ics "$ICS_URL" \
  --category-id "$CATEGORY_ID" \
  --site-tz "$SITE_TZ" \
  --static-tags "${DEFAULT_TAGS:-}"
```

