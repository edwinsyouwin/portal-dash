"""Upcoming events from Google Calendar, for the dashboard's agenda.

Each calendar is read through its private iCal address (Google Calendar > Settings >
the calendar > "Secret address in iCal format"), stored in .env as CALENDAR_<NAME>=<url>.
The name becomes the label shown next to that person's events.
"""

import datetime as dt
import threading
import time

import httpx
import icalendar
import recurring_ical_events

# GOOGLE_CALENDAR_<NAME> is accepted too; the longer prefix must come first.
ENV_PREFIXES = ("GOOGLE_CALENDAR_", "CALENDAR_")
DAYS_AHEAD = 31  # enough for the dashboard's month view
CACHE_SECONDS = 300

_lock = threading.Lock()
_cache = {"feeds": None, "at": 0.0, "result": None}


def feeds_from(env):
    """{label: url} for every CALENDAR_<NAME> setting, e.g. CALENDAR_ALEX -> "Alex"."""
    feeds = {}
    for key, url in sorted(env.items()):
        prefix = next((p for p in ENV_PREFIXES if key.startswith(p)), None)
        if prefix and url:
            feeds[key.removeprefix(prefix).replace("_", " ").title()] = url
    return feeds


def local(moment):
    """A calendar time as an ISO string in this Mac's timezone."""
    return moment.astimezone().isoformat(timespec="minutes")


def events_in(ics_text, start, end):
    """Events between two dates, with repeating events expanded into their occurrences."""
    calendar = icalendar.Calendar.from_ical(ics_text)
    for event in recurring_ical_events.of(calendar).between(start, end):
        if str(event.get("STATUS", "")).upper() == "CANCELLED":
            continue
        begin = event["DTSTART"].dt
        finish = event["DTEND"].dt if "DTEND" in event else begin
        all_day = not isinstance(begin, dt.datetime)
        yield {
            "uid": str(event.get("UID", "")),
            "title": str(event.get("SUMMARY", "")).strip() or "(No title)",
            "all_day": all_day,
            # All-day events are plain dates and end on the day after their last day.
            "start": begin.isoformat() if all_day else local(begin),
            "end": finish.isoformat() if all_day else local(finish),
            "location": str(event.get("LOCATION", "")).strip(),
        }


def fetch(feeds):
    today = dt.date.today()
    merged, failed = {}, []
    for label, url in feeds.items():
        try:
            response = httpx.get(url, timeout=20, follow_redirects=True)
            response.raise_for_status()
            found = list(events_in(response.text, today, today + dt.timedelta(days=DAYS_AHEAD)))
        except Exception as error:  # one broken feed must not hide the other calendars
            print(f"Calendar {label!r} failed: {error!r}")
            failed.append(label)
            continue
        for event in found:
            # An event both people are invited to appears in both feeds; show it once.
            key = (event.pop("uid"), event["start"])
            merged.setdefault(key, event | {"who": []})["who"].append(label)

    events = sorted(merged.values(), key=lambda e: (e["start"][:10], not e["all_day"], e["start"]))
    return {
        "configured": bool(feeds),
        "calendars": list(feeds),
        "failed": failed,
        "events": events,
        "fetched": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def upcoming(feeds):
    """The next week's events, refetched at most every few minutes."""
    with _lock:
        fresh = time.time() - _cache["at"] < CACHE_SECONDS
        if _cache["feeds"] != feeds or not fresh:
            result = fetch(feeds)
            # Keep showing the last good answer if every calendar is unreachable right now.
            stale = _cache["result"] if _cache["feeds"] == feeds else None
            if feeds and len(result["failed"]) == len(feeds) and stale:
                result = stale
            _cache.update(feeds=feeds, at=time.time(), result=result)
        return _cache["result"]
