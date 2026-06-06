"""
RBT On-Call Calendar Generator
Fetches the master RBT calendar and produces individual ICS files per employee.

Handles:
  - RRULE recurring events (any interval)
  - EXDATE exclusions (swapped dates removed from a series)
  - RECURRENCE-ID overrides (a specific occurrence was modified/moved)
  - Standalone single events (including swap replacements)
  - Multi-day vacation blocks
"""

import urllib.request
import urllib.parse
import json
import os
import re
from datetime import datetime, date, timedelta

with open("config.json") as f:
    CONFIG = json.load(f)

CALENDAR_URL = CONFIG.get("calendar_url", "")
OUTPUT_DIR   = "docs"


# ── Microsoft Graph API ───────────────────────────────────────────────────────

def get_access_token():
    """Exchange refresh token for a fresh access token."""
    data = urllib.parse.urlencode({
        "client_id":     os.environ["MS_CLIENT_ID"],
        "client_secret": os.environ["MS_CLIENT_SECRET"],
        "refresh_token": os.environ["MS_REFRESH_TOKEN"],
        "grant_type":    "refresh_token",
        "scope":         "Calendars.ReadWrite offline_access",
    }).encode()
    req = urllib.request.Request(
        f"https://login.microsoftonline.com/{os.environ['MS_TENANT_ID']}/oauth2/v2.0/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())["access_token"]


GRAPH_CALENDAR_ID = "AAMkADgyNTI3NzVmLTM4NDQtNDA3Ny04YzQwLWRlYTM5ZGE2YjI5YQBGAAAAAACcdVw86RQWTLoPvL7v1c0PBwBcnq76AxVQS6O842XYqNawAAAAAAEGAABcnq76AxVQS6O842XYqNawAAAHInXdAAA="

def fetch_calendar_graph(access_token):
    """Fetch all calendar events from Graph API (2-year window)."""
    start = date.today().strftime("%Y-%m-%dT00:00:00")
    end   = (date.today() + timedelta(days=730)).strftime("%Y-%m-%dT00:00:00")
    url   = (f"https://graph.microsoft.com/v1.0/me/calendars/{GRAPH_CALENDAR_ID}/calendarView"
             f"?startDateTime={start}&endDateTime={end}"
             f"&$top=999&$select=subject,start,end,id,isAllDay")
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    events = []
    while url:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
        events.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return events


def graph_to_ics(events):
    """Convert Graph API JSON events to ICS text for existing parser."""
    blocks = []
    for ev in events:
        subject = ev.get("subject", "").replace("\n", " ")
        uid     = ev.get("id", "")
        s       = ev.get("start", {})
        e       = ev.get("end", {})
        # Use date portion only
        dtstart = (s.get("date") or s.get("dateTime", "")[:10]).replace("-", "")
        dtend   = (e.get("date") or e.get("dateTime", "")[:10]).replace("-", "")
        if not dtstart:
            continue
        if not dtend or dtend == dtstart:
            d     = datetime.strptime(dtstart, "%Y%m%d").date()
            dtend = (d + timedelta(days=1)).strftime("%Y%m%d")
        blocks.append(
            f"BEGIN:VEVENT\n"
            f"UID:{uid}\n"
            f"SUMMARY:{subject}\n"
            f"DTSTART;VALUE=DATE:{dtstart}\n"
            f"DTEND;VALUE=DATE:{dtend}\n"
            f"END:VEVENT"
        )
    return "BEGIN:VCALENDAR\nVERSION:2.0\n" + "\n".join(blocks) + "\nEND:VCALENDAR"


# ── ICS URL fallback ──────────────────────────────────────────────────────────

def fetch_calendar(url):
    with urllib.request.urlopen(url) as r:
        return r.read().decode("utf-8")


def unfold_lines(raw):
    """ICS line folding: continuation lines start with a space or tab."""
    lines = []
    for line in raw.splitlines():
        if line and line[0] in (' ', '\t') and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def parse_events(raw):
    """Return list of dicts, one per VEVENT."""
    events = []
    current = {}
    in_event = False

    for line in unfold_lines(raw):
        if line.strip() == "BEGIN:VEVENT":
            in_event = True
            current = {"_raw_lines": []}
        elif line.strip() == "END:VEVENT":
            events.append(current)
            in_event = False
            current = {}
        elif in_event:
            current["_raw_lines"].append(line)
            # Split key (with params) from value
            if ":" in line:
                key_part, _, value = line.partition(":")
                key = key_part.split(";")[0].upper()
                # Keep first occurrence of each key (except EXDATE — accumulate)
                if key == "EXDATE":
                    current.setdefault("EXDATE", []).append(value.strip())
                elif key not in current:
                    current[key] = value.strip()

    return events


def date8(val):
    """Extract YYYYMMDD string from a DTSTART/DTEND/UNTIL value."""
    if val:
        return val.replace("Z", "")[:8]
    return ""


def parse_date(val):
    try:
        return datetime.strptime(date8(val), "%Y%m%d").date()
    except Exception:
        return None


# ── Filtering ────────────────────────────────────────────────────────────────

def matches(event, name):
    summary = event.get("SUMMARY", "").lower()
    # Use word-boundary match so "Will" doesn't match inside "will be on call"
    return bool(re.search(r'\b' + re.escape(name.lower()) + r'\b', summary))

def is_oncall(event, name):
    summary = event.get("SUMMARY", "").lower()
    return matches(event, name) and ("on call" in summary or "oncall" in summary)

def is_vacation(event, name):
    return matches(event, name) and "vacation" in event.get("SUMMARY", "").lower()


# ── Recurrence expansion ──────────────────────────────────────────────────────

def expand_event(event, recurrence_overrides):
    """
    Expand a recurring event into individual (date_str, event_dict) pairs.
    Skips dates excluded by EXDATE or overridden by RECURRENCE-ID.
    For non-recurring events returns a single pair.
    """
    dtstart  = event.get("DTSTART", "")
    rrule    = event.get("RRULE", "")
    uid      = event.get("UID", "")
    exdates  = set()

    for ex in event.get("EXDATE", []):
        exdates.add(date8(ex))

    # Dates overridden by a RECURRENCE-ID event for the same UID
    overridden = recurrence_overrides.get(uid, set())

    if not rrule:
        # Single event
        d = date8(dtstart)
        if d and d not in overridden:
            yield d, event
        return

    # Parse RRULE
    freq     = "DAILY"
    interval = 1
    until    = None

    for part in rrule.split(";"):
        k, _, v = part.partition("=")
        if k == "FREQ":     freq     = v
        elif k == "INTERVAL": interval = int(v)
        elif k == "UNTIL":
            until = parse_date(v)

    if freq != "DAILY":
        # Non-daily recurrence — just yield the start date
        d = date8(dtstart)
        if d and d not in exdates and d not in overridden:
            yield d, event
        return

    start = parse_date(dtstart)
    if not start:
        return

    if until is None:
        until = date.today() + timedelta(days=730)

    cur = start
    while cur <= until:
        d = cur.strftime("%Y%m%d")
        if d not in exdates and d not in overridden:
            yield d, event
        cur += timedelta(days=interval)


# ── ICS building ─────────────────────────────────────────────────────────────

def make_event_block(event, dtstart_val, dtend_val, uid_suffix=""):
    lines = ["BEGIN:VEVENT"]
    seen_keys = set()
    for line in event.get("_raw_lines", []):
        key = line.split(":")[0].split(";")[0].upper()
        if key in ("RRULE", "EXDATE", "RECURRENCE-ID"):
            continue
        if key == "DTSTART":
            if "DTSTART" not in seen_keys:
                lines.append(f"DTSTART;VALUE=DATE:{dtstart_val}")
                seen_keys.add("DTSTART")
        elif key == "DTEND":
            if "DTEND" not in seen_keys:
                lines.append(f"DTEND;VALUE=DATE:{dtend_val}")
                seen_keys.add("DTEND")
        elif key == "UID":
            if "UID" not in seen_keys:
                uid_val = line.split(":", 1)[-1].strip()
                lines.append(f"UID:{uid_val}{uid_suffix}")
                seen_keys.add("UID")
        elif key not in seen_keys:
            lines.append(line)
            seen_keys.add(key)
    # Ensure DTSTART/DTEND present
    if "DTSTART" not in seen_keys:
        lines.append(f"DTSTART;VALUE=DATE:{dtstart_val}")
    if "DTEND" not in seen_keys:
        lines.append(f"DTEND;VALUE=DATE:{dtend_val}")
    lines.append("END:VEVENT")
    return "\n".join(lines)


def make_oncall_block(event, date_str, uid_suffix=""):
    d     = parse_date(date_str)
    end_d = (d + timedelta(days=1)) if d else None
    return make_event_block(
        event,
        date_str[:8],
        end_d.strftime("%Y%m%d") if end_d else date_str[:8],
        uid_suffix
    )


def make_vacation_block(event):
    """Preserve original multi-day span for vacation events."""
    dtstart = date8(event.get("DTSTART", ""))
    dtend   = date8(event.get("DTEND", ""))
    if not dtend:
        d     = parse_date(dtstart)
        dtend = (d + timedelta(days=1)).strftime("%Y%m%d") if d else dtstart
    return make_event_block(event, dtstart, dtend)


def build_ics(all_events, name):
    """
    Build a complete ICS for one employee.
    Steps:
      1. Find all RECURRENCE-ID overrides (modified occurrences) for this person's UIDs
      2. Expand recurring events, skipping overridden/excluded dates
      3. Include override events (RECURRENCE-ID) as standalone occurrences
      4. Include standalone on-call and vacation events
    """

    # Step 1 — collect RECURRENCE-ID overrides keyed by UID + original date
    recurrence_overrides = {}  # uid -> set of YYYYMMDD strings that are overridden
    override_events      = []  # the replacement events

    for ev in all_events:
        if "RECURRENCE-ID" in ev:
            uid  = ev.get("UID", "")
            orig = date8(ev.get("RECURRENCE-ID", ""))
            recurrence_overrides.setdefault(uid, set()).add(orig)
            # If this override is for our person, keep it
            if is_oncall(ev, name) or is_vacation(ev, name):
                override_events.append(ev)

    # Step 2 & 3 — expand recurring + singles, then add overrides
    output_blocks = []
    seen_dates    = set()  # deduplicate on-call dates

    for ev in all_events:
        if "RECURRENCE-ID" in ev:
            continue  # handled separately above

        if is_oncall(ev, name):
            for i, (d, src_ev) in enumerate(expand_event(ev, recurrence_overrides)):
                if d not in seen_dates:
                    output_blocks.append(make_oncall_block(src_ev, d, uid_suffix=f"-{d}"))
                    seen_dates.add(d)

        elif is_vacation(ev, name):
            output_blocks.append(make_vacation_block(ev))

    # Add RECURRENCE-ID override events for this person
    for ev in override_events:
        if is_oncall(ev, name):
            d = date8(ev.get("DTSTART", ""))
            if d and d not in seen_dates:
                output_blocks.append(make_oncall_block(ev, d, uid_suffix=f"-override-{d}"))
                seen_dates.add(d)
        elif is_vacation(ev, name):
            output_blocks.append(make_vacation_block(ev))

    # ── Safety net ────────────────────────────────────────────────────────────
    # If any RECURRENCE-ID event reassigns a date to someone *else*, strip it
    # from this person's calendar even if the base expansion included it.
    # This catches any edge case where the UID-based skip above didn't fire.
    swapped_away = set()
    for ev in all_events:
        if "RECURRENCE-ID" not in ev:
            continue
        d = date8(ev.get("DTSTART", ""))
        if d not in seen_dates:
            continue
        # There's a RECURRENCE-ID event on one of our dates.
        # If it is NOT an event for this person → the date was swapped away.
        if not is_oncall(ev, name) and not is_vacation(ev, name):
            swapped_away.add(d)
            print(f"    [{name}] swap detected: {d} reassigned to '{ev.get('SUMMARY', '?')}' — removing from {name}'s calendar")

    if swapped_away:
        seen_dates -= swapped_away
        output_blocks = [
            b for b in output_blocks
            if not any(f"DTSTART;VALUE=DATE:{d}" in b for d in swapped_away)
        ]

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//{name} Schedule//EN",
        f"CALNAME:{name} Schedule",
        f"X-WR-CALNAME:{name} Schedule",
        f"X-WR-CALDESC:{name} on-call and vacation schedule — auto-updated daily from RBT calendar",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
    ] + output_blocks + ["END:VCALENDAR"]

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def get_all_names():
    """Return a deduplicated list of first names from config employees + full directory."""
    names = set()
    # On-call rotation employees
    for emp in CONFIG.get("employees", []):
        names.add(emp["name"])
    # Full directory (all staff)
    dir_path = os.path.join(OUTPUT_DIR, "directory.json")
    if os.path.exists(dir_path):
        with open(dir_path) as f:
            directory = json.load(f)
        for emp in directory.get("field_employees", []) + directory.get("office_employees", []):
            # Extract first name only (matches how calendar events are named)
            first = emp["name"].split()[0].capitalize()
            names.add(first)
    return sorted(names)


def load_rotation_orders():
    """Load per-year rotation orders from file."""
    path = os.path.join(OUTPUT_DIR, "rotation_orders.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_rotation_orders(orders):
    """Save per-year rotation orders to file."""
    path = os.path.join(OUTPUT_DIR, "rotation_orders.json")
    with open(path, "w") as f:
        json.dump(orders, f, indent=2)
    print(f"  Rotation orders saved to {path}")


def get_order_for_year(year, active_employees, orders):
    """Return the rotation order for a given year, creating a new shuffle if needed."""
    import random
    key = str(year)
    names = [e["name"] for e in active_employees]
    if key not in orders:
        shuffled = names[:]
        random.shuffle(shuffled)
        orders[key] = shuffled
        print(f"  New rotation order for {year}: {', '.join(shuffled)}")
    else:
        # Ensure any new employees are appended
        existing = orders[key]
        new_names = [n for n in names if n not in existing]
        if new_names:
            existing.extend(new_names)
            orders[key] = existing
    return [e for e in active_employees if e["name"] in orders[key]], orders[key]


def topup_oncall_calendar(token, cal_id, active_employees, rotation_days=1):
    """
    Maintains a rolling 365-day on-call window.
    Every run:
      1. Deletes events beyond today + 365 days
      2. Fills any gap between the last assigned day and today + 365 days
    Net result: always exactly 365 days scheduled, no more, no less.
    """
    today    = date.today()
    end_date = today + timedelta(days=364)  # inclusive last day = today + 364

    # Fetch ALL future on-call events (look a bit beyond 365 to catch over-runs)
    look_end = today + timedelta(days=400)
    start_str = today.strftime("%Y-%m-%dT00:00:00")
    end_str   = look_end.strftime("%Y-%m-%dT00:00:00")
    url = (f"https://graph.microsoft.com/v1.0/me/calendars/{cal_id}/calendarView"
           f"?startDateTime={start_str}&endDateTime={end_str}"
           f"&$top=999&$select=id,subject,start")
    all_events = []
    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
        for ev in data.get("value", []):
            s = ev.get("subject", "").lower()
            if ("on call" in s or "oncall" in s) and "vacation" not in s:
                dt = ev["start"].get("date") or ev["start"].get("dateTime", "")[:10]
                all_events.append({"id": ev["id"], "date": dt, "subject": ev["subject"]})
        url = data.get("@odata.nextLink")

    # Step 1 — delete anything beyond today + 364
    to_delete = [ev for ev in all_events if ev["date"] > end_date.strftime("%Y-%m-%d")]
    if to_delete:
        print(f"  Trimming {len(to_delete)} events beyond day 365...")
        for i in range(0, len(to_delete), 20):
            chunk = to_delete[i:i+20]
            body = json.dumps({"requests": [
                {"id": str(j+1), "method": "DELETE", "url": f"/me/events/{ev['id']}"}
                for j, ev in enumerate(chunk)
            ]}).encode()
            req = urllib.request.Request("https://graph.microsoft.com/v1.0/$batch", data=body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            urllib.request.urlopen(req)

    # Step 2 — find last covered date within window
    within_window = [ev for ev in all_events if ev["date"] <= end_date.strftime("%Y-%m-%d")]
    occupied = set(ev["date"] for ev in within_window)

    if not occupied:
        last_covered = today - timedelta(days=1)
    else:
        last_covered = datetime.strptime(max(occupied), "%Y-%m-%d").date()

    fill_start = last_covered + timedelta(days=rotation_days)
    if fill_start > end_date:
        print(f"  Calendar fully covered to {end_date} — no fill needed")
        return

    # Step 3 — determine who continues the rotation
    n = len(active_employees)
    last_person_idx = 0
    if within_window:
        # Find person assigned on the last covered day
        last_ev = max(within_window, key=lambda x: x["date"])
        last_name = last_ev["subject"].split()[0].lower()
        for i, emp in enumerate(active_employees):
            if emp["name"].lower() == last_name:
                last_person_idx = (i + 1) % n
                break

    print(f"  Filling {(end_date - fill_start).days + 1} days from {fill_start} to {end_date}...")

    # Load/build per-year rotation orders
    orders = load_rotation_orders()
    orders_changed = False

    # Pre-generate orders for any years we'll be scheduling
    years_needed = set()
    cur = fill_start
    while cur <= end_date:
        years_needed.add(cur.year)
        cur += timedelta(days=rotation_days)
    for yr in sorted(years_needed):
        if str(yr) not in orders:
            get_order_for_year(yr, active_employees, orders)
            orders_changed = True

    events_to_create = []
    current = fill_start
    current_year = None
    year_order = []
    year_idx = last_person_idx

    while current <= end_date:
        date_str = current.strftime("%Y-%m-%d")

        # Switch rotation order on Jan 1 of a new year
        if current.year != current_year:
            current_year = current.year
            _, year_order = get_order_for_year(current_year, active_employees, orders)
            # On Jan 1 reset idx to 0 so we start fresh with the new shuffled order
            if current.month == 1 and current.day == 1:
                year_idx = 0
                print(f"  Jan 1 {current_year} — using new rotation order")

        if date_str not in occupied:
            name  = year_order[year_idx % len(year_order)]
            end_d = (current + timedelta(days=rotation_days)).strftime("%Y-%m-%d")
            events_to_create.append({
                "subject":  f"{name} On Call",
                "start":    {"dateTime": f"{date_str}T00:00:00", "timeZone": "America/Toronto"},
                "end":      {"dateTime": f"{end_d}T00:00:00",   "timeZone": "America/Toronto"},
                "isAllDay": True
            })
        year_idx += 1
        current += timedelta(days=rotation_days)

    if orders_changed:
        save_rotation_orders(orders)

    pushed = 0
    for i in range(0, len(events_to_create), 4):
        batch = events_to_create[i:i+4]
        body = json.dumps({"requests": [
            {"id": str(j+1), "method": "POST",
             "url": f"/me/calendars/{cal_id}/events",
             "headers": {"Content-Type": "application/json"},
             "body": ev}
            for j, ev in enumerate(batch)
        ]}).encode()
        req = urllib.request.Request("https://graph.microsoft.com/v1.0/$batch", data=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"})
        with urllib.request.urlopen(req) as r:
            result = json.loads(r.read())
        pushed += sum(1 for resp in result.get("responses", []) if resp.get("status", 0) < 300)

    print(f"  Rolling window top-up: {pushed} events added, {len(to_delete)} trimmed")


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    use_graph = all(os.environ.get(k) for k in
                    ("MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_TENANT_ID", "MS_REFRESH_TOKEN"))

    if use_graph:
        print("Fetching calendar via Microsoft Graph API (2-year window)...")
        token        = get_access_token()
        graph_events = fetch_calendar_graph(token)
        raw          = graph_to_ics(graph_events)
        print(f"  {len(graph_events)} events fetched from Graph API")
    else:
        print("Fetching calendar via ICS URL (fallback)...")
        raw = fetch_calendar(CALENDAR_URL)

    events = parse_events(raw)
    print(f"  {len(events)} total events parsed")

    all_names = get_all_names()
    print(f"  Generating ICS for {len(all_names)} employees: {', '.join(all_names)}")

    for name in all_names:
        ics  = build_ics(events, name)

        filename = f"{OUTPUT_DIR}/{name.lower()}_schedule.ics"
        with open(filename, "w") as f:
            f.write(ics)

        oncall_count   = len([e for e in events if is_oncall(e, name)])
        vacation_count = len([e for e in events if is_vacation(e, name)])
        override_count = len([e for e in events if "RECURRENCE-ID" in e and (is_oncall(e, name) or is_vacation(e, name))])
        print(f"  {name}: {oncall_count} on-call src events, {vacation_count} vacation, {override_count} overrides -> {filename}")

    # Daily top-up: ensure 12 months of on-call always exists in the calendar
    if use_graph:
        cal_id = os.environ.get("GRAPH_CAL_ID", GRAPH_CALENDAR_ID)
        active_emps = [e for e in CONFIG.get("employees", []) if e.get("active", True)]
        if active_emps:
            print("\nChecking 12-month on-call coverage...")
            import traceback
            try:
                topup_oncall_calendar(token, cal_id, active_emps)
            except Exception as e:
                print(f"  Top-up error: {e}")
                traceback.print_exc()


if __name__ == "__main__":
    main()
