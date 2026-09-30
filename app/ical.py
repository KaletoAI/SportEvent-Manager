"""iCalendar (RFC 5545) output for the personal calendar feed.

Pure formatting, no DB access: the route collects the bookings, this module
turns them into a VCALENDAR that calendar apps can subscribe to (Proton,
Google, Apple, Outlook, Thunderbird …). Event times are stored as naive
local wall-clock times, so they are emitted with TZID=Europe/Berlin plus the
matching VTIMEZONE block (no tz database needed on the server).
"""

from dataclasses import dataclass
from datetime import datetime, timezone

TZID = "Europe/Berlin"

_VTIMEZONE = [
    "BEGIN:VTIMEZONE",
    f"TZID:{TZID}",
    "BEGIN:DAYLIGHT",
    "TZOFFSETFROM:+0100",
    "TZOFFSETTO:+0200",
    "TZNAME:CEST",
    "DTSTART:19700329T020000",
    "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
    "END:DAYLIGHT",
    "BEGIN:STANDARD",
    "TZOFFSETFROM:+0200",
    "TZOFFSETTO:+0100",
    "TZNAME:CET",
    "DTSTART:19701025T030000",
    "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
    "END:STANDARD",
    "END:VTIMEZONE",
]


@dataclass
class CalEvent:
    uid: str
    start: datetime  # naive local time
    end: datetime
    summary: str
    description: str = ""
    url: str = ""
    cancelled: bool = False


def escape(text: str) -> str:
    """Escape a TEXT value (RFC 5545 §3.3.11)."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\n")
        .replace("\n", "\\n")
    )


def fold(line: str) -> str:
    """Fold a content line at 75 octets (UTF-8), never inside a character."""
    parts, current, size = [], "", 0
    for ch in line:
        n = len(ch.encode())
        limit = 75 if not parts else 74  # continuation lines start with a space
        if size + n > limit:
            parts.append(current)
            current, size = "", 0
        current += ch
        size += n
    parts.append(current)
    return "\r\n ".join(parts)


def _local(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")


def build_calendar(name: str, events: list[CalEvent]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//SportAbo Manager//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape(name)}",
        f"X-WR-TIMEZONE:{TZID}",
        # Refresh hints; most apps use their own interval anyway
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
        *_VTIMEZONE,
    ]
    for e in events:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{e.uid}",
            f"DTSTAMP:{stamp}",
            f"DTSTART;TZID={TZID}:{_local(e.start)}",
            f"DTEND;TZID={TZID}:{_local(e.end)}",
            f"SUMMARY:{escape(e.summary)}",
            f"STATUS:{'CANCELLED' if e.cancelled else 'CONFIRMED'}",
            "TRANSP:OPAQUE",
        ]
        if e.description:
            lines.append(f"DESCRIPTION:{escape(e.description)}")
        if e.url:
            lines.append(f"URL:{e.url}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "".join(fold(line) + "\r\n" for line in lines)
