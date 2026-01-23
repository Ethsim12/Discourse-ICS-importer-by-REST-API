#!/usr/bin/env python3
"""
Sync ICS -> Discourse topics (create/update by UID, with site-wide deduplication fallback).

Key behaviours:
- Attempts idempotent updates by ICS UID (via UID-derived tag and/or hidden marker).
- If no UID match is found (e.g. noisy or unstable feeds), falls back to site-wide deduplication.
- By default, fallback deduplication matches (start, end, location).
- With --time-only-dedupe, fallback deduplication matches (start, end) only, tolerating location changes.
- Preserves human-edited titles on update.
- Does NOT change category on update.
- Merges tags on update (never drops moderator/manual tags).
- Updates the first post only when the visible content changes (hidden marker ignored).
- Embeds an invisible UID marker in the first post on create and on content updates; adopted topics may be retrofitted with the marker, depending on whether the retrofit path (currently commented out) is enabled.

Env (recommended):
  DISCOURSE_BASE_URL       e.g. "https://forum.example.com"
  DISCOURSE_API_KEY        your admin/mod API key
  DISCOURSE_API_USERNAME   e.g. "system" or your staff username
  DISCOURSE_CATEGORY_ID    default numeric category id for CREATE only (override with --category-id)
  DISCOURSE_DEFAULT_TAGS   comma separated list, e.g. "calendar,events"

Usage:
  python3 ics_to_discourse.py --ics my.ics --category-id 12
  python3 ics_to_discourse.py --ics https://example.com/cal.ics --static-tags calendar,google
"""

from __future__ import annotations

import os
import sys
import re
import time
import random
import argparse
import logging
import hashlib
from datetime import datetime, timedelta, time as dtime
from typing import Any, Dict, Iterable, List, Set, Tuple

import requests
from dateutil import tz
from icalendar import Calendar

# --------------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------------
log = logging.getLogger("ics2disc")
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------
BASE         = os.environ.get("DISCOURSE_BASE_URL", "").rstrip("/")
API_KEY      = os.environ.get("DISCOURSE_API_KEY", "")
API_USER     = os.environ.get("DISCOURSE_API_USERNAME", "system")
ENV_CAT_ID   = os.environ.get("DISCOURSE_CATEGORY_ID", "")
DEFAULT_TAGS = [t.strip() for t in os.environ.get("DISCOURSE_DEFAULT_TAGS", "").split(",") if t.strip()]

SITE_TZ_DEFAULT = os.environ.get("SITE_TZ", "Europe/London")

# --------------------------------------------------------------------------------------
# HTTP helpers with retry/backoff
# --------------------------------------------------------------------------------------
def session() -> requests.Session:
    if not BASE or not API_KEY or not API_USER:
        log.error(
            "Missing DISCOURSE_* env vars. Need DISCOURSE_BASE_URL, DISCOURSE_API_KEY, DISCOURSE_API_USERNAME."
        )
        sys.exit(2)
    s = requests.Session()
    s.headers.update({
        "Api-Key": API_KEY,
        "Api-Username": API_USER,
        "Accept": "application/json",
    })
    return s

def _request_with_backoff(s: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    """Retry on 429 / transient 5xx with exponential backoff + jitter."""
    delay = 1.0
    for _ in range(6):  # ~1 + 2 + 4 + 8 + 16 + 30
        r = s.request(method, url, timeout=60, **kwargs)
        if r.status_code != 429 and r.status_code < 500:
            try:
                r.raise_for_status()
            except requests.HTTPError:
                # Try to log Discourse's error payloads for debugging (e.g. 422)
                try:
                    err = r.json()
                except Exception:
                    err = {"body": r.text[:800]}
                ctx = kwargs.get("_request_context")
                if ctx:
                    log.error("HTTP %s %s failed (%s): %s | ctx=%s",
                              method, url, r.status_code, err, ctx)
                else:
                    log.error("HTTP %s %s failed (%s): %s",
                              method, url, r.status_code, err)
                raise

            time.sleep(0.2)  # be gentle even on success
            return r
        retry_after = r.headers.get("Retry-After")
        wait = float(retry_after) if retry_after else delay
        time.sleep(wait + random.uniform(0, 0.5))
        delay = min(delay * 2, 30.0)
    r.raise_for_status()
    return r


def get_json(s: requests.Session, path: str, **params) -> Dict[str, Any]:
    r = _request_with_backoff(s, "GET", f"{BASE}{path}", params=params)
    return r.json()

def post_form(s: requests.Session, path: str, data: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    r = _request_with_backoff(s, "POST", f"{BASE}{path}", data=data)
    # Some endpoints might return empty; tolerate it.
    if not r.content or not r.content.strip():
        return {}
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text}


def put_form(s: requests.Session, path: str, data: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    r = _request_with_backoff(s, "PUT", f"{BASE}{path}", data=data)
    if not r.content or not r.content.strip():
        return {}
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text}

def _reset_bump_date(s: requests.Session, topic_id: int) -> None:
    """
    Undo Latest bump caused by metadata/tag changes.
    Requires staff credentials (your API user/key already are).
    """
    try:
        # Empty form body; endpoint is PUT /t/{id}/reset-bump-date
       # put_form(s, f"/t/{topic_id}/reset-bump-date", [])
    except Exception as e:
        # Non-fatal: log and carry on
        log.warning("reset-bump-date failed for topic %s: %s", topic_id, e)

def post_json(s: requests.Session, path: str, json: Dict[str, Any]) -> Dict[str, Any]:
    r = _request_with_backoff(s, "POST", f"{BASE}{path}", json=json)
    return r.json()



# --------------------------------------------------------------------------------------
# Search helpers (UID tag, marker)
# --------------------------------------------------------------------------------------
def search_topic_by_marker_via_search(s: requests.Session, marker_token: str) -> int | None:
    """Fallback: search for the hidden HTML marker using /search.json."""
    q = f"\"{marker_token}\""
    data = get_json(s, "/search.json", q=q)
    topics = data.get("topics", [])
    if topics:
        return topics[0].get("id")
    return None

def _uid_tag_variants(uid: str) -> List[str]:
    """Try multiple hash inputs so case/whitespace changes don't break lookups."""
    raw = str(uid or "")
    candidates = [raw, raw.strip(), raw.strip().lower()]
    out: List[str] = []
    seen: Set[str] = set()
    for u in candidates:
        h = hashlib.sha1(u.encode("utf-8")).hexdigest()[:10]
        tag = f"ics-{h}"
        if tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out

def search_topic_by_uid_tag_then_marker(s: requests.Session, uid: str, marker_token: str) -> int | None:
    # Try tag variants first
    for tag in _uid_tag_variants(uid):
        data = get_json(s, "/search.json", q=f"tag:{tag}")
        topics = data.get("topics") or data.get("topic_list", {}).get("topics", [])
        if topics:
            return topics[0].get("id")
    # Fallback to marker search
    return search_topic_by_marker_via_search(s, marker_token)

# --------------------------------------------------------------------------------------
# Topic read/update helpers
# --------------------------------------------------------------------------------------
def read_topic_full(s: requests.Session, topic_id: int) -> Dict[str, Any]:
    return get_json(s, f"/t/{topic_id}.json", include_raw="true")

def first_post_id_and_raw(topic_json: Dict[str, Any]) -> Tuple[int | None, str]:
    posts = topic_json.get("post_stream", {}).get("posts", [])
    if not posts:
        return None, ""
    p0 = posts[0]
    return p0.get("id"), p0.get("raw", "")

def update_first_post_raw(
    s: requests.Session,
    post_id: int,
    new_raw: str,
    *,
    bypass_bump: bool = False,
    topic_id: int | None = None,
) -> Dict[str, Any]:
    """
    Update the first post body. When bypass_bump=True, ask Discourse not to bump the topic.
    If the instance ignores that hint, and topic_id is provided, reset the bump date as a fallback.
    """



    # Inject reminders="bumpTopic.5.minutes" if the opening [event ...] has no reminder(s)
    try:
        if "[event" in (new_raw or "") and not re.search(r'\breminders?\s*=', new_raw, flags=re.I):
            new_raw = re.sub(
                r'(\[event\b[^\]]*)\]',
                r'\1 reminders="bumpTopic.5.minutes"]',
                new_raw,
                count=1,
                flags=re.I,
            )
    except Exception as e:
        log.warning("Failed to inject bump reminder: %s", e)




  
    fields: List[Tuple[str, Any]] = [("post[raw]", new_raw)]
    if bypass_bump:
        # Must be top-level, not post[bypass_bump]
        fields.append(("bypass_bump", "true"))
    resp = put_form(s, f"/posts/{post_id}.json", fields)
#    if bypass_bump and topic_id:
#        log.info("Invoking reset-bump-date fallback for topic %s", topic_id)
#        try:
#            _reset_bump_date(s, topic_id)
#        except Exception:
#            # Non-fatal; proceed even if reset fails
#            pass
    return resp

def update_topic_tags(s: requests.Session, topic_id: int, merged_tags: Iterable[str]) -> Dict[str, Any]:
    tags = list(merged_tags)
    # Build the full form payload once, then PUT once.
    fields: List[Tuple[str, Any]] = [("tags[]", t) for t in tags]
    resp = put_form(s, f"/t/{topic_id}.json", fields)
    # Tag updates bump the topic; immediately undo that bump.
    #_reset_bump_date(s, topic_id)
    return resp

# --------------------------------------------------------------------------------------
# Event block parsing & normalization
# --------------------------------------------------------------------------------------
EVENT_TAG_RE = re.compile(r"\[event\s+([^\]]+)\]", re.IGNORECASE | re.DOTALL)
ATTR_RE      = re.compile(r'([a-zA-Z0-9_-]+)\s*=\s*"([^"]*)"')

def parse_event_attrs(raw_text: str) -> Dict[str, str]:
    m = EVENT_TAG_RE.search(raw_text or "")
    if not m:
        return {}
    attrs = {k.lower(): v for k, v in ATTR_RE.findall(m.group(1))}
    return attrs

def norm(s: str | None) -> str:
    return (s or "").strip().lower()

def norm_location(s: str | None) -> str:
    """
    Normalize location strings so 'up physics c05,up physics c05' -> 'up physics c05',
    lowercase, collapse whitespace, de-dup comma-separated parts (keep order).
    """
    s = (s or "").lower()
    parts = [re.sub(r"\s+", " ", p.strip()) for p in s.split(",") if p.strip()]
    seen: List[str] = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return ", ".join(seen)

def close_enough_loc(a: str, b: str) -> bool:
    """Treat empty as wildcard; otherwise accept exact or containment."""
    if not a or not b:
        return True
    return a == b or (a in b) or (b in a)

# --------------------------------------------------------------------------------------
# Time-window search (via /search.json) with verification
# --------------------------------------------------------------------------------------
def _verify_event_hit(s: requests.Session,
                      tid: int,
                      candidate_triples: Set[Tuple[str, str, str]],
                      loc_now: str,
                      time_only: bool) -> bool:
    """Fetch topic, parse [event] attrs, and verify time/location match."""
    tjson = get_json(s, f"/t/{tid}.json", include_raw=1)
    posts = tjson.get("post_stream", {}).get("posts", []) or []
    if not posts:
        return False
    first_raw = posts[0].get("raw", "") or ""
    attrs = parse_event_attrs(first_raw)
    if not attrs:
        return False
    trip = (norm(attrs.get("start")), norm(attrs.get("end")), norm_location(attrs.get("location")))
    if time_only:
        return (trip[0], trip[1]) in {(t[0], t[1]) for t in candidate_triples} and close_enough_loc(trip[2], loc_now)
    return trip in candidate_triples

def search_by_timewindow_then_verify(s: requests.Session,
                                     start_now: str,
                                     end_now: str,
                                     loc_now: str,
                                     candidate_triples: Set[Tuple[str, str, str]],
                                     time_only: bool) -> Tuple[int | None, bool]:
    """
    Try a broad /search.json query using exact time strings, then verify each hit.
    Returns (topic_id, api_error) where api_error=True means we should fallback to /latest.json.
    """
    # Build a reasonably selective exact-phrase query; verification will do the real matching.
    q_parts = []
    if start_now: q_parts.append(f"\"{start_now}\"")
    if end_now:   q_parts.append(f"\"{end_now}\"")
    q = " ".join(q_parts) or "\"\""  # never empty
    try:
        data = get_json(s, "/search.json", q=q)
        topics = data.get("topics") or data.get("topic_list", {}).get("topics", []) or []
        for t in topics:
            tid = t.get("id")
            if tid and _verify_event_hit(s, tid, candidate_triples, loc_now, time_only):
                return tid, False
        return None, False  # no hits → do NOT fallback to /latest.json
    except Exception:
        # Only an API error should trigger the /latest.json fallback.
        return None, True

# --------------------------------------------------------------------------------------
# Time handling (cover legacy encoding)
# --------------------------------------------------------------------------------------
def _parse_local_dt_string(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M")
    except Exception:
        return None

def _site_offset_minutes(dt_local: datetime, site_tz: str) -> int:
    tzinfo = tz.gettz(site_tz or SITE_TZ_DEFAULT)
    aware = dt_local.replace(tzinfo=tzinfo)
    off = aware.utcoffset()
    return int(off.total_seconds() // 60) if off else 0

def _shift_by_offset(s: str, site_tz: str) -> str | None:
    """
    Produce a legacy-encoding variant: local string shifted by local UTC offset,
    matching the old behavior "treat floating as UTC, then convert to local".
    """
    dt = _parse_local_dt_string(s or "")
    if not dt:
        return None
    minutes = _site_offset_minutes(dt, site_tz)
    if minutes == 0:
        return None
    shifted = dt + timedelta(minutes=minutes)
    return shifted.strftime("%Y-%m-%d %H:%M")

# --------------------------------------------------------------------------------------
# ICS I/O and rendering
# --------------------------------------------------------------------------------------
def read_ics(path_or_url: str) -> Calendar:
    if re.match(r"^https?://", path_or_url, re.I):
        s = requests.Session()
        delay = 1.0
        for _ in range(6):
            try:
                r = s.get(path_or_url, timeout=60)
                if r.status_code != 429 and r.status_code < 500:
                    r.raise_for_status()
                    return Calendar.from_ical(r.content)
            except requests.RequestException:
                pass
            time.sleep(delay + random.uniform(0, 0.5))
            delay = min(delay * 2, 30.0)
        r = s.get(path_or_url, timeout=60)
        r.raise_for_status()
        return Calendar.from_ical(r.content)
    else:
        with open(path_or_url, "rb") as f:
            return Calendar.from_ical(f.read())

def to_local_iso(dt, tzname: str = SITE_TZ_DEFAULT) -> str:
    """
    Return 'YYYY-MM-DD HH:MM' in provided timezone. Accepts date or datetime.

    - If datetime has tzinfo, convert to site tz.
    - If datetime is naive (floating), interpret as site tz (NO 'assume UTC' step).
    - If date, render 00:00 in site tz.
    """
    target = tz.gettz(tzname)
    if hasattr(dt, "dt"):
        dt = dt.dt
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=target)
        else:
            dt = dt.astimezone(target)
    else:
        dt = datetime.combine(dt, dtime(0, 0, 0, 0, tzinfo=target))
    return dt.strftime("%Y-%m-%d %H:%M")

def short_uid_tag(uid: str) -> str:
    return f"ics-{hashlib.sha1(uid.encode('utf-8')).hexdigest()[:10]}"

def build_marker(uid: str) -> str:
    return f"ICSUID:{hashlib.sha1(uid.encode('utf-8')).hexdigest()[:16]}"

def strip_marker(raw: str) -> str:
    return re.sub(r"<!--\s*ICSUID:[0-9a-f]{16}\s*-->\s*", "", raw or "", flags=re.I)

def make_event_block(ev, site_tz: str, include_details: bool = True) -> Tuple[str, str, str]:
    uid = str(ev.get("UID"))
    summary = str(ev.get("SUMMARY", "Untitled event"))
    location = str(ev.get("LOCATION", "")).strip()
    url = str(ev.get("URL", "")).strip()
    desc = str(ev.get("DESCRIPTION", "")).strip()

    dtstart = ev.get("DTSTART")
    dtend = ev.get("DTEND")
    start_str = to_local_iso(dtstart, site_tz) if dtstart else ""
    end_str = to_local_iso(dtend, site_tz) if dtend else ""

    event_open = f'[event start="{start_str}"'
    if end_str:
        event_open += f' end="{end_str}"'
    event_open += f' status="public" name="{summary}"'
    if location:
        event_open += f' location="{location}"'
    event_open += f' reminders="bumpTopic.5.minutes"'
    event_open += f' timezone="{site_tz}"]'

    body_lines: List[str] = []
    if include_details:
        if location:
            body_lines.append(f"**Location:** {location}")
        if url:
            body_lines.append(f"**Link:** {url}")
        if desc:
            body_lines.append("")
            body_lines.append(desc)

    content = "\n".join([event_open] + body_lines + ["[/event]"])
    return summary, content, uid

# --------------------------------------------------------------------------------------
# Search helpers (description-first candidates)
# --------------------------------------------------------------------------------------
def _extract_searchable_lines(event_block_md: str) -> list[str]:
    """
    From the rendered event block markdown, extract the lines we want to search on:
    everything inside [event] ... [/event] up to (but not including) any line
    that starts with 'Last Updated:' (case-insensitive).
    """
    m = re.search(r"\[event[^\]]*\](.*)\[/event\]", event_block_md, flags=re.I | re.S)
    body = (m.group(1) if m else event_block_md) or ""
    lines = []
    for line in body.splitlines():
        if re.match(r"(?i)^\s*Last Updated\s*:", line):
            break
        line = line.strip()
        if line:
            lines.append(line)
    return lines

def _tokenish(s: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9/_-]{2,}", s)

def build_description_queries(event_block_md: str, title: str, max_phrases: int = 6) -> list[str]:
    lines = _extract_searchable_lines(event_block_md)
    phrases: list[str] = []
    if title and len(title.strip()) >= 6:
        phrases.append(f"\"{title.strip()}\"")
    for ln in lines:
        low = ln.lower()
        if "taught by" in low or "module" in low or "location" in low:
            phrases.append(f"\"{ln}\"")
        elif 1 <= len(_tokenish(ln)) <= 6:
            phrases.append(f"\"{ln}\"")
        if len(phrases) >= max_phrases:
            break
    if not phrases:
        for ln in lines[:2]:
            phrases.append(f"\"{ln}\"")
    seen = set()
    out = []
    for p in phrases:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out

def search_candidate_topic_ids_by_description(
    s: requests.Session,
    phrases: list[str],
    max_ids: int = 400,
    max_pages_per_query: int = 6,
) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    def run_query(q: str):
        for page in range(1, max_pages_per_query + 1):
            try:
                data = get_json(s, "/search.json", q=q, page=page)
            except Exception:
                break
            topics = data.get("topics") or data.get("topic_list", {}).get("topics", []) or []
            if not topics:
                break
            for t in topics:
                tid = t.get("id")
                if isinstance(tid, int) and tid not in seen:
                    seen.add(tid)
                    ids.append(tid)
                    if len(ids) >= max_ids:
                        return
        return
    if len(phrases) >= 2:
        run_query(" ".join(phrases[:3]))
    for p in phrases:
        if len(ids) >= max_ids:
            break
        run_query(p)
    return ids

def verify_candidate_ids_by_event(
    s: requests.Session,
    cand_ids: list[int],
    candidate_triples: set[tuple[str, str, str]],
    loc_now: str,
    time_only: bool,
) -> int | None:
    time_only_pairs = {(t[0], t[1]) for t in candidate_triples}
    for tid in cand_ids:
        tjson = get_json(s, f"/t/{tid}.json", include_raw=1)
        posts = tjson.get("post_stream", {}).get("posts", []) or []
        if not posts:
            continue
        first_raw = posts[0].get("raw", "") or ""
        attrs = parse_event_attrs(first_raw)
        if not attrs:
            continue
        trip = (norm(attrs.get("start")), norm(attrs.get("end")), norm_location(attrs.get("location")))
        if time_only:
            if (trip[0], trip[1]) in time_only_pairs and close_enough_loc(trip[2], loc_now):
                return tid
        else:
            if trip in candidate_triples:
                return tid
    return None

# --------------------------------------------------------------------------------------
# Create (with site-wide duplicate detection) or adopt
# --------------------------------------------------------------------------------------
def create_or_adopt_topic(
    s: requests.Session,
    category_id: int | str,
    title: str,
    raw: str,
    tags: Iterable[str],
    *,
    pages_to_scan: int = 8,
    time_only: bool = False,
) -> Tuple[int, bool]:
    """
    Creates a new topic unless a topic already exists anywhere on the site
    whose first post contains a [event ...] with the same start/end/location.
    Also tolerates legacy time-encoding (floating treated as UTC then converted).

    Returns (topic_id, was_created). If was_created=False, the caller should retrofit
    the new UID tag + marker into the adopted topic.
    """
    new_attrs = parse_event_attrs(raw)
    site_tz = new_attrs.get("timezone", "") or SITE_TZ_DEFAULT

    start_now = norm(new_attrs.get("start"))
    end_now   = norm(new_attrs.get("end"))
    loc_now   = norm_location(new_attrs.get("location"))

    # Legacy time variants
    start_legacy = _shift_by_offset(new_attrs.get("start", ""), site_tz)
    end_legacy   = _shift_by_offset(new_attrs.get("end", ""),   site_tz)

    candidate_triples: Set[Tuple[str, str, str]] = set()
    candidate_triples.add((start_now, end_now, loc_now))
    if start_legacy:
        candidate_triples.add((norm(start_legacy), end_now, loc_now))
    if end_legacy:
        candidate_triples.add((start_now, norm(end_legacy), loc_now))
    if start_legacy and end_legacy:
        candidate_triples.add((norm(start_legacy), norm(end_legacy), loc_now))
      
    log.info("[dup-scan] site_tz=%s", site_tz)
    log.info("[dup-scan] summary=%s loc=%s", new_attrs.get("name") or title, loc_now or "(none)")
    log.info("[dup-scan] candidates=%s", sorted(candidate_triples))
    
    # 1) Prefer /search.json time-window search (verify each hit)
    tid, api_error = search_by_timewindow_then_verify(
        s,
        start_now,
        end_now,
        loc_now,
        candidate_triples,
        time_only=time_only,
    )
    if tid:
        log.info(f"[ics-sync] Adopting existing topic via time-window search: {tid}")
        return tid, False


    # 1b) If no time-window hit, try description-first candidate search
    desc_phrases = build_description_queries(raw, title)
    if desc_phrases:
        cand_ids = search_candidate_topic_ids_by_description(
            s, desc_phrases, max_ids=400, max_pages_per_query=6
        )
        if cand_ids:
            tid2 = verify_candidate_ids_by_event(
                s, cand_ids, candidate_triples, loc_now, time_only
            )
            if tid2:
                log.info(f"[ics-sync] Adopting existing topic via description search: {tid2}")
                return tid2, False

    # 2) Fallback: scan /latest.json pages regardless of API error
    if pages_to_scan > 0:
        time_only_candidates: Set[Tuple[str, str]] = {(t[0], t[1]) for t in candidate_triples}
        for page in range(max(1, pages_to_scan)):
            data = get_json(s, "/latest.json", page=page, no_definitions="true")
            topics = data.get("topic_list", {}).get("topics", []) or []
            if not topics:
                break
            for t in topics:
                tid2 = t["id"]
                tjson = get_json(s, f"/t/{tid2}.json", include_raw=1)
                posts = tjson.get("post_stream", {}).get("posts", []) or []
                if not posts:
                    continue
                first_raw = posts[0].get("raw", "") or ""
                attrs = parse_event_attrs(first_raw)
                if not attrs:
                    continue
                trip = (
                    norm(attrs.get("start")),
                    norm(attrs.get("end")),
                    norm_location(attrs.get("location")),
                )
                log.debug("[dup-scan] tid=%s trip=%s", tid2, trip)
                if time_only and (trip[0], trip[1]) in time_only_candidates and close_enough_loc(trip[2], loc_now):
                    log.info(f"[ics-sync] Adopting existing topic by time match (time-only mode): {tid2}")
                    return tid2, False
                if trip in candidate_triples:
                    log.info(
                        f"[ics-sync] Adopting existing topic by site-wide match: {tid2} "
                        f"(start={trip[0]} end={trip[1]} loc={trip[2]})"
                    )
                    return tid2, False
    else:
        log.info("[ics-sync] pages_to_scan=0 → skipping /latest.json fallback.")

   

    # Else: create a new topic
    fields: List[Tuple[str, Any]] = [
        ("title", title),
        ("raw", raw),
        ("category", int(category_id)),
        ("archetype", "regular"),
    ]
    for t in tags:
        fields.append(("tags[]", t))
    data = post_form(s, "/posts.json", fields)

    tid = data.get("topic_id")
  
    logging.info(f"[ics-sync] Created new topic {tid} (title={title})")
    
    return tid, True

# --------------------------------------------------------------------------------------
# Main sync logic
# --------------------------------------------------------------------------------------
def _norm_tags(x) -> List[str]:
    if not x:
        return []
    if isinstance(x, (list, tuple, set)):
        return [str(t).strip() for t in x if str(t).strip()]
    return [t.strip() for t in str(x).split(",") if t.strip()]

def sync_event(s: requests.Session, ev, args) -> Tuple[int | None, bool]:
    site_tz = args.site_tz
    summary, event_block, uid = make_event_block(ev, site_tz)

    # Unique tokens
    marker_token = build_marker(uid)   # used inside body as HTML comment
    uid_tag = short_uid_tag(uid)       # Discourse tag used for lookups

    marker_html = f"<!-- {marker_token} -->"
    fresh_raw = f"{marker_html}\n{event_block}\n"

    # 1) Try to find an existing topic by UID tag variants or marker
    topic_id = search_topic_by_uid_tag_then_marker(s, uid, marker_token)

    if topic_id:
        # UPDATE path
        topic = read_topic_full(s, topic_id)
        post_id, old_raw = first_post_id_and_raw(topic)

        old_clean = strip_marker(old_raw)
        fresh_clean = strip_marker(fresh_raw)

        if old_clean.strip() != fresh_clean.strip():
            log.info("Updating topic %s first post.", topic_id)

            # Decide if the change is "meaningful": start/end/location changed?
            old_attrs = parse_event_attrs(old_clean)
            new_attrs = parse_event_attrs(fresh_clean)

            def _norm_time(x): return norm(x or "")
            def _norm_loc(x):  return norm_location(x or "")

            meaningful = (
                _norm_time(old_attrs.get("start")) != _norm_time(new_attrs.get("start"))
                or _norm_time(old_attrs.get("end")) != _norm_time(new_attrs.get("end"))
                or _norm_loc(old_attrs.get("location")) != _norm_loc(new_attrs.get("location"))
            )

            # Bump only when meaningful data changed; otherwise bypass bump
            update_first_post_raw(
                s, post_id, fresh_raw,
                bypass_bump=not meaningful,
                topic_id=topic_id
            )

        else:
            log.info("No body change for topic %s.", topic_id)

        # Merge tags, ensuring UID tag is present
        existing_tags = topic.get("tags", []) or []
        desired_tags = set(existing_tags)
        desired_tags.update(_norm_tags(DEFAULT_TAGS))
        desired_tags.update(_norm_tags(args.static_tags))
        #desired_tags.add(uid_tag)

        if set(existing_tags) != desired_tags:
            merged = sorted(desired_tags)
            log.info("Merging tags on topic %s -> %s", topic_id, ", ".join(merged))
            update_topic_tags(s, topic_id, merged)
        else:
            log.info("Tags unchanged for topic %s.", topic_id)

        # Do not change title or category on update
        return topic_id, False

    # 2) CREATE or ADOPT path (site-wide dedupe)
    category_id = args.category_id or ENV_CAT_ID
    if not category_id:
        log.error("Missing category id for CREATE (use --category-id or DISCOURSE_CATEGORY_ID). Skipping UID=%s", uid)
        return None, False

    tags = set()
    tags.update(_norm_tags(DEFAULT_TAGS))
    tags.update(_norm_tags(args.static_tags))
    #tags.add(uid_tag)
    tags = sorted(tags)

    title = summary
    topic_id, was_created = create_or_adopt_topic(
        s,
        category_id,
        title,
        fresh_raw,
        tags,
        pages_to_scan=args.scan_pages,
        time_only=args.time_only_dedupe,
    )

    if was_created:
        log.info("Created topic %s for UID=%s", topic_id, uid)
        return topic_id, True

    # Adopted an existing topic → retrofit UID tag + hidden marker (don't change visible body)
    topic = read_topic_full(s, topic_id)
    existing_tags = topic.get("tags", []) or []
    desired = set(existing_tags)
    desired.update(_norm_tags(DEFAULT_TAGS))
    desired.update(_norm_tags(args.static_tags))
    ###desired.add(uid_tag)
    if set(existing_tags) != desired:
        update_topic_tags(s, topic_id, sorted(desired))

    post_id, old_raw = first_post_id_and_raw(topic)
    if post_id:
        if marker_token.lower() not in (old_raw or "").lower(): #it may be that the same UID marker is in the topic for non-noisy feed, hence this commit has no effect. Therefore, i will commit to main rather than a branch
            #new_raw = f"<!-- {marker_token} -->\n{old_raw or ''}"
            # IMPORTANT: keep this quiet
            update_first_post_raw(
                s, post_id, old_raw,
                bypass_bump=True,
                topic_id=topic_id
            )

    log.info("Adopted topic %s for UID=%s (retrofit tag+marker).", topic_id, uid)
    return topic_id, False


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Sync an ICS into Discourse topics (idempotent by UID).")
    ap.add_argument("--ics", required=True, help="Path or URL to .ics")
    ap.add_argument("--category-id", help="Numeric category id (CREATE only; update never moves category)")
    ap.add_argument("--site-tz", default=SITE_TZ_DEFAULT, help=f"Timezone name for rendering times (default: {SITE_TZ_DEFAULT})")
    ap.add_argument("--static-tags", default="", help="Comma separated static tags to add on create/update (merged with existing)")
    ap.add_argument("--scan-pages", type=int, default=8, help="How many /latest pages to scan site-wide for duplicates (default: 8)")
    ap.add_argument("--time-only-dedupe", action="store_true", default=False,
                    help="Treat events with same start/end as duplicates regardless of location (location becomes 'close' check)")
    args = ap.parse_args()

    args.static_tags = [t.strip() for t in args.static_tags.split(",") if t.strip()]

    s = session()
    cal = read_ics(args.ics)

    count = 0
    created = 0
    for ev in cal.walk("VEVENT"):
        try:
            _, was_created = sync_event(s, ev, args)
            count += 1
            if was_created:
                created += 1
        except Exception as e:
            log.error("Error syncing event: %s", e, exc_info=True)

    log.info("Done. Processed %d events (%d created, %d updated).", count, created, count - created)

if __name__ == "__main__":
    main()
