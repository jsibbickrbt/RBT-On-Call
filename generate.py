import urllib.request
import json
import os
from datetime import datetime, date, timedelta

with open("config.json") as f:
    CONFIG = json.load(f)

CALENDAR_URL = CONFIG["calendar_url"]
OUTPUT_DIR = "docs"

CARRIER_GATEWAYS = {
    "bell":    "txt.bell.ca",
    "rogers":  "pcs.rogers.com",
    "telus":   "msg.telus.com",
    "fido":    "fido.ca",
    "freedom": "txt.freedommobile.ca",
    "virgin":  "vmobile.ca",
    "koodo":   "msg.koodo.com",
    "chatr":   "pcs.rogers.com",
    "public":  "msg.telus.com"
}

def fetch_calendar(url):
    with urllib.request.urlopen(url) as response:
        return response.read().decode("utf-8")

def parse_events(raw):
    events = []
    in_event = False
    current = []
    for line in raw.splitlines():
        if line.strip() == "BEGIN:VEVENT":
            in_event = True
            current = [line]
        elif line.strip() == "END:VEVENT":
            current.append(line)
            events.append("\n".join(current))
            in_event = False
            current = []
        elif in_event:
            current.append(line)
    return events

def get_summary(event_text):
    for line in event_text.splitlines():
        if line.startswith("SUMMARY:"):
            return line[len("SUMMARY:"):].strip()
    return ""

def is_oncall(event_text, name):
    summary = get_summary(event_text).lower()
    return name.lower() in summary and ("on call" in summary or "oncall" in summary)

def is_vacation(event_text, name):
    summary = get_summary(event_text).lower()
    return name.lower() in summary and "vacation" in summary

def expand_rrule(event_text):
    dtstart = None
    rrule = None
    exdates = []
    for line in event_text.splitlines():
        if line.startswith("DTSTART"):
            dtstart = line.split(":")[-1].strip()
        elif line.startswith("RRULE:"):
            rrule = line[len("RRULE:"):]
        elif line.startswith("EXDATE"):
            exdates.append(line.split(":")[-1].strip()[:8])
    if not dtstart or not rrule:
        return [dtstart] if dtstart else []
    freq = interval = None
    interval = 1
    until = None
    for part in rrule.split(";"):
        if part.startswith("FREQ="):    freq = part[5:]
        elif part.startswith("INTERVAL="): interval = int(part[9:])
        elif part.startswith("UNTIL="):
            try: until = datetime.strptime(part[6:].replace("Z","")[:8], "%Y%m%d").date()
            except: pass
    if freq != "DAILY":
        return [dtstart]
    try:
        start = datetime.strptime(dtstart[:8], "%Y%m%d").date()
    except:
        return [dtstart]
    if until is None:
        until = date.today() + timedelta(days=730)
    dates = []
    cur = start
    while cur <= until:
        s = cur.strftime("%Y%m%d")
        if s not in exdates:
            dates.append(s)
        cur += timedelta(days=interval)
    return dates

def make_single_event(event_text, date_str, uid_suffix=""):
    try:
        d = datetime.strptime(date_str[:8], "%Y%m%d").date()
        dtstart_val = d.strftime("%Y%m%d")
        dtend_val   = (d + timedelta(days=1)).strftime("%Y%m%d")
    except:
        dtstart_val = dtend_val = date_str[:8]
    lines = ["BEGIN:VEVENT"]
    for line in event_text.splitlines():
        if line in ("BEGIN:VEVENT", "END:VEVENT"): continue
        key = line.split(":")[0].split(";")[0]
        if key in ("RRULE", "EXDATE"): continue
        if key == "DTSTART": lines.append(f"DTSTART;VALUE=DATE:{dtstart_val}")
        elif key == "DTEND": lines.append(f"DTEND;VALUE=DATE:{dtend_val}")
        elif key == "UID":
            uid_val = line.split(":", 1)[-1].strip()
            lines.append(f"UID:{uid_val}-{date_str}{uid_suffix}")
        else: lines.append(line)
    lines.append("END:VEVENT")
    return "\n".join(lines)

def make_multiday_event(event_text):
    lines = ["BEGIN:VEVENT"]
    for line in event_text.splitlines():
        if line in ("BEGIN:VEVENT", "END:VEVENT"): continue
        key = line.split(":")[0].split(";")[0]
        if key in ("RRULE", "EXDATE"): continue
        lines.append(line)
    lines.append("END:VEVENT")
    return "\n".join(lines)

def build_ics(events, name):
    output_events = []
    for event_text in events:
        if is_oncall(event_text, name):
            has_rrule = any(l.startswith("RRULE:") for l in event_text.splitlines())
            if has_rrule:
                for i, d in enumerate(expand_rrule(event_text)):
                    output_events.append(make_single_event(event_text, d, f"-{i}"))
            else:
                for line in event_text.splitlines():
                    if line.startswith("DTSTART"):
                        output_events.append(make_single_event(event_text, line.split(":")[-1].strip()[:8]))
                        break
        elif is_vacation(event_text, name):
            output_events.append(make_multiday_event(event_text))

    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        f"PRODID:-//{name} Schedule//EN",
        f"CALNAME:{name} Schedule",
        f"X-WR-CALNAME:{name} Schedule",
        f"X-WR-CALDESC:{name} on-call and vacation schedule",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
    ]
    lines += output_events
    lines.append("END:VCALENDAR")
    return "\n".join(lines)

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print("Fetching RBT calendar...")
    raw = fetch_calendar(CALENDAR_URL)
    events = parse_events(raw)
    print(f"  {len(events)} total events")

    for emp in CONFIG["employees"]:
        if not emp.get("active", True):
            continue
        name = emp["name"]
        ics = build_ics(events, name)
        filename = f"{OUTPUT_DIR}/{name.lower()}_schedule.ics"
        with open(filename, "w") as f:
            f.write(ics)
        oncall_count = sum(1 for e in events if is_oncall(e, name))
        vacation_count = sum(1 for e in events if is_vacation(e, name))
        print(f"  {name}: {oncall_count} on-call, {vacation_count} vacation events -> {filename}")

if __name__ == "__main__":
    main()
