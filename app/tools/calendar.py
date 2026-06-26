import datetime
import os

import requests

from app.tools.builtin import tool_manager

# Discover all CALENDAR_<NAME>=<url> env vars at startup
CALENDARS: dict[str, str] = {
    key[9:].lower(): val
    for key, val in os.environ.items()
    if key.upper().startswith("CALENDAR_") and val
}

# Optional: comma-separated list of the owner's email addresses, used to filter declined events
USER_EMAILS: set[str] = {
    e.strip().lower()
    for e in os.environ.get("USER_EMAILS", "").split(",")
    if e.strip()
}


def _user_declined(event) -> bool:
    """Return True if any of the owner's emails has PARTSTAT=DECLINED on this event."""
    attendees = event.get("ATTENDEE")
    if attendees is None:
        return False
    if not isinstance(attendees, list):
        attendees = [attendees]
    for attendee in attendees:
        cal_address = str(attendee).lower().removeprefix("mailto:")
        if cal_address in USER_EMAILS:
            if str(attendee.params.get("PARTSTAT", "")).upper() == "DECLINED":
                return True
    return False


def _event_start(event) -> datetime.datetime:
    dt = event.get("DTSTART").dt
    if isinstance(dt, datetime.datetime):
        return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)
    return datetime.datetime.combine(dt, datetime.time.min, tzinfo=datetime.timezone.utc)


def _fetch_calendar(name: str, url: str, now: datetime.datetime, end: datetime.datetime) -> list[dict]:
    try:
        import icalendar
        import recurring_ical_events
    except ImportError as e:
        return [{"error": f"missing dependency — {e}"}]

    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        cal = icalendar.Calendar.from_ical(response.content)
        events = recurring_ical_events.of(cal).between(now, end)
    except Exception as e:
        return [{"error": f"calendar '{name}': {e}"}]

    results = []
    for event in events:
        if str(event.get("STATUS", "")).upper() == "CANCELLED":
            continue
        if USER_EMAILS and _user_declined(event):
            continue
        dt = event.get("DTSTART").dt
        results.append({
            "calendar": name,
            "summary": str(event.get("SUMMARY", "(no title)")),
            "location": str(event.get("LOCATION", "")).strip(),
            "dt": dt,
            "start": _event_start(event),
        })
    return results


def _get_calendar_events(days_ahead: int = 7, calendars: list | None = None) -> str:
    if not CALENDARS:
        return (
            "Error: no calendars configured. "
            "Add CALENDAR_<name>=<ics_url> entries to your .env file."
        )

    days_ahead = max(1, min(days_ahead, 90))
    now = datetime.datetime.now(datetime.timezone.utc)
    end = now + datetime.timedelta(days=days_ahead)

    targets = (
        {k: v for k, v in CALENDARS.items() if k in [c.lower() for c in calendars]}
        if calendars
        else CALENDARS
    )
    if not targets:
        available = ", ".join(CALENDARS)
        return f"Error: none of the requested calendars found. Available: {available}"

    all_events: list[dict] = []
    errors: list[str] = []
    for name, url in targets.items():
        items = _fetch_calendar(name, url, now, end)
        for item in items:
            if "error" in item:
                errors.append(item["error"])
            else:
                all_events.append(item)

    lines = []
    if errors:
        lines.append("Errors: " + "; ".join(errors))

    if not all_events:
        suffix = f" ({', '.join(targets)})" if len(targets) < len(CALENDARS) else ""
        lines.append(f"No events in the next {days_ahead} day(s){suffix}.")
        return "\n".join(lines)

    for event in sorted(all_events, key=lambda e: e["start"]):
        dt = event["dt"]
        if isinstance(dt, datetime.datetime):
            time_str = dt.strftime("%Y-%m-%d %H:%M %Z").strip()
        else:
            time_str = f"{dt.strftime('%Y-%m-%d')} (all day)"

        line = f"- {time_str}: {event['summary']} [{event['calendar']}]"
        if event["location"]:
            line += f" @ {event['location']}"
        lines.append(line)

    header = f"Upcoming events (next {days_ahead} day(s)):"
    return header + "\n" + "\n".join(lines)


tool_manager.register(
    name="get_calendar_events",
    fn=_get_calendar_events,
    description=(
        "Fetch upcoming events from one or more calendars. "
        "Returns events within the next N days, labelled by calendar name. "
        "Use this to check schedules, find free slots, or give a daily briefing. "
        f"Configured calendars: {', '.join(CALENDARS) or 'none'}."
    ),
    parameters={
        "type": "object",
        "properties": {
            "days_ahead": {
                "type": "integer",
                "description": "How many days ahead to look (default: 7, max: 90).",
            },
            "calendars": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Which calendars to include by name. "
                    "Omit to fetch all configured calendars."
                ),
            },
        },
        "required": [],
    },
    status_template="Fetching upcoming calendar events...",
)
