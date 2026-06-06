import json, os, urllib.request, urllib.parse, base64, re
from datetime import date, datetime, timedelta

with open("config.json") as f:
    CONFIG = json.load(f)

def is_oncall_today(ics_content, today):
    """Return True only if the ICS has an on-call (not vacation) event on today."""
    in_event = False
    summary = ""
    has_today = False
    for line in ics_content.splitlines():
        if line.strip() == "BEGIN:VEVENT":
            in_event = True
            summary = ""
            has_today = False
        elif line.strip() == "END:VEVENT":
            if in_event and has_today:
                s = summary.lower()
                if ("on call" in s or "oncall" in s) and "vacation" not in s:
                    return True
            in_event = False
        elif in_event:
            if line.upper().startswith("SUMMARY"):
                summary = line.split(":", 1)[-1].strip()
            elif f"DTSTART;VALUE=DATE:{today}" in line:
                has_today = True
    return False

def find_conflicts(ics_content):
    """
    Parse an ICS file and return a list of on-call dates (YYYYMMDD strings)
    that fall within a vacation range. Only returns future dates.
    """
    today = date.today().strftime("%Y%m%d")

    # Parse all events into {summary, dtstart, dtend}
    events = []
    in_event = False
    ev = {}
    for line in ics_content.splitlines():
        if line.strip() == "BEGIN:VEVENT":
            in_event = True
            ev = {}
        elif line.strip() == "END:VEVENT":
            if in_event and ev:
                events.append(ev)
            in_event = False
        elif in_event:
            if ":" in line:
                key = line.split(";")[0].split(":")[0].upper().strip()
                val = line.split(":", 1)[-1].strip()
                if key in ("SUMMARY", "DTSTART", "DTEND"):
                    ev[key] = val

    # Collect on-call dates and vacation ranges (future only)
    oncall_dates = set()
    vacation_ranges = []

    for ev in events:
        summary = ev.get("SUMMARY", "").lower()
        dtstart = ev.get("DTSTART", "").replace("Z", "")[:8]
        dtend   = ev.get("DTEND",   "").replace("Z", "")[:8]
        if not dtstart:
            continue

        is_oc  = ("on call" in summary or "oncall" in summary) and "vacation" not in summary
        is_vac = "vacation" in summary

        if is_oc and dtstart >= today:
            oncall_dates.add(dtstart)
        elif is_vac and dtstart:
            vacation_ranges.append((dtstart, dtend or dtstart))

    # Find conflicts
    conflicts = []
    for d in sorted(oncall_dates):
        for vac_start, vac_end in vacation_ranges:
            if vac_start <= d < vac_end:
                conflicts.append(d)
                break

    return conflicts


def format_date(d8):
    """Format YYYYMMDD to readable date string."""
    try:
        return datetime.strptime(d8, "%Y%m%d").strftime("%b %d, %Y")
    except Exception:
        return d8


def send_sms(sid, token, from_num, to_num, body):
    data = urllib.parse.urlencode({"From": from_num, "To": to_num, "Body": body}).encode()
    req = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    creds = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req.add_header("Authorization", f"Basic {creds}")
    resp = urllib.request.urlopen(req)
    return resp.status

def main():
    sid             = os.environ.get("TWILIO_ACCOUNT_SID", "")
    token           = os.environ.get("TWILIO_AUTH_TOKEN", "")
    from_num        = os.environ.get("TWILIO_FROM", "")
    today           = date.today().strftime("%Y%m%d")
    send_calendar   = os.environ.get("SEND_CALENDAR",   "").strip().lower()
    check_conflicts = os.environ.get("CHECK_CONFLICTS", "").strip().lower()

    # SEND CALENDAR LINK MODE
    if send_calendar:
        base_url = "https://jsibbickrbt.github.io/RBT-On-Call"
        for emp in CONFIG["employees"]:
            if emp["name"].lower() == send_calendar and emp.get("active", True):
                name  = emp["name"]
                phone = emp.get("phone", "")
                if phone:
                    ics_url = f"{base_url}/{name.lower()}_schedule.ics"
                    webcal  = ics_url.replace("https://", "webcal://")
                    msg = (f"Hi {name}! Here is your RBT on-call calendar link.\n\n"
                           f"On iPhone/iPad: tap the link to subscribe in Calendar\n{webcal}\n\n"
                           f"On Android/other: add this URL in your calendar app\n{ics_url}")
                    status = send_sms(sid, token, from_num, phone, msg)
                    print(f"Calendar link sent to {name} ({phone}): {status}")
                else:
                    print(f"No phone for {name}")
                return
        print(f"Employee '{send_calendar}' not found")
        return

    # CONFLICT CHECK MODE
    if check_conflicts == "true":
        # Find Jordan's phone to send the report to
        jordan_phone = ""
        for emp in CONFIG["employees"]:
            if emp["name"].lower() == "jordan" and emp.get("active", True):
                jordan_phone = emp.get("phone", "")
                break

        if not jordan_phone:
            print("No phone found for Jordan — cannot send conflict report")
            return

        all_conflicts = []
        for emp in CONFIG["employees"]:
            if not emp.get("active", True):
                continue
            name     = emp["name"]
            phone    = emp.get("phone", "")
            ics_file = f"docs/{name.lower()}_schedule.ics"
            if not os.path.exists(ics_file):
                print(f"  No ICS for {name}, skipping conflict check")
                continue
            with open(ics_file) as f:
                content = f.read()
            conflicts = find_conflicts(content)
            if conflicts:
                dates_str = ", ".join(format_date(d) for d in conflicts)
                all_conflicts.append(f"{name}: {dates_str}")
                print(f"  {name} has conflicts on: {dates_str}")
                # Text the individual
                if phone:
                    try:
                        personal_msg = (f"⚠️ Hi {name}, you are scheduled on call during your vacation on: "
                                        f"{dates_str}. Please coordinate with Jordan.")
                        send_sms(sid, token, from_num, phone, personal_msg)
                        print(f"  Personal conflict alert sent to {name}")
                    except Exception as e:
                        print(f"  Failed to text {name}: {e}")
            else:
                print(f"  {name}: no conflicts")

        if all_conflicts:
            msg = "⚠️ On-Call/Vacation Conflicts:\n" + "\n".join(all_conflicts)
            send_sms(sid, token, from_num, jordan_phone, msg)
            print(f"Conflict report sent to Jordan ({jordan_phone})")
        else:
            print("No conflicts found — no text sent")
        return

    # TEST MODE
    test_user = os.environ.get("TEST_USER", "").strip().lower()
    if test_user == "office":
        office = CONFIG.get("office", {})
        phone  = office.get("phone", "")
        if phone:
            # Check ICS files to find who is actually on call today
            oncall_today = []
            for emp in CONFIG["employees"]:
                if not emp.get("active", True):
                    continue
                ics_file = f"docs/{emp['name'].lower()}_schedule.ics"
                if os.path.exists(ics_file):
                    with open(ics_file) as f:
                        content = f.read()
                    if is_oncall_today(content, today):
                        oncall_today.append(emp["name"])
            if oncall_today:
                names = ", ".join(oncall_today)
                msg = f"Test - On call today: {names}"
            else:
                msg = "Test - Nobody is on call today"
            status = send_sms(sid, token, from_num, phone, msg)
            print(f"Test text sent to office ({phone}): {status} — {msg}")
        else:
            print("No office phone configured")
        return
    if test_user == "all":
        for emp in CONFIG["employees"]:
            if not emp.get("active", True):
                continue
            name  = emp["name"]
            phone = emp.get("phone", "")
            if phone:
                try:
                    send_sms(sid, token, from_num, phone, f"Test: {name}, you are on call today!")
                    print(f"Test text sent to {name} ({phone})")
                except Exception as e:
                    print(f"Failed to text {name}: {e}")
            else:
                print(f"No phone for {name}")
        return

    if test_user:
        for emp in CONFIG["employees"]:
            if emp["name"].lower() == test_user and emp.get("active", True):
                name  = emp["name"]
                phone = emp.get("phone", "")
                if phone:
                    status = send_sms(sid, token, from_num, phone, f"Test: {name}, you are on call today!")
                    print(f"Test text sent to {name} ({phone}): {status}")
                else:
                    print(f"No phone for {name}")
                return
        print(f"Employee '{test_user}' not found")
        return

    # NORMAL MODE
    oncall_today = []

    for emp in CONFIG["employees"]:
        if not emp.get("active", True):
            continue
        name  = emp["name"]
        phone = emp.get("phone", "")

        ics_file = f"docs/{name.lower()}_schedule.ics"
        if not os.path.exists(ics_file):
            print(f"  No ICS for {name}, skipping")
            continue

        with open(ics_file) as f:
            content = f.read()

        if is_oncall_today(content, today):
            oncall_today.append(name)
            print(f"  {name} is on call today")
            if phone:
                try:
                    send_sms(sid, token, from_num, phone, f"Reminder: {name}, you are on call today!")
                    print(f"    Texted {name} at {phone}")
                except Exception as e:
                    print(f"    Failed to text {name}: {e}")
        else:
            print(f"  {name} is NOT on call today")

    # Notify office with everyone on call
    office = CONFIG.get("office", {})
    if office.get("active") and office.get("phone") and oncall_today:
        names = ", ".join(oncall_today)
        try:
            send_sms(sid, token, from_num, office["phone"], f"On call today: {names}")
            print(f"  Office notified: {names}")
        except Exception as e:
            print(f"  Failed to notify office: {e}")

    if not oncall_today:
        print("  Nobody on call today")

    # Duplicate on-call check — alert Jordan if any future date has 2+ people assigned
    if len(oncall_today) > 1:
        names = ", ".join(oncall_today)
        print(f"  ⚠️ DUPLICATE: Multiple people on call today: {names}")
        jordan_phone_now = ""
        for emp in CONFIG["employees"]:
            if emp["name"].lower() == "jordan" and emp.get("active", True):
                jordan_phone_now = emp.get("phone", "")
                break
        if jordan_phone_now:
            try:
                send_sms(sid, token, from_num, jordan_phone_now,
                         f"⚠️ Schedule conflict: Multiple people on call today ({names}). Please fix the calendar.")
                print(f"  Duplicate alert sent to Jordan")
            except Exception as e:
                print(f"  Failed to send duplicate alert: {e}")

    # Daily conflict check — always runs and texts Jordan if any conflicts found
    jordan_phone = ""
    for emp in CONFIG["employees"]:
        if emp["name"].lower() == "jordan" and emp.get("active", True):
            jordan_phone = emp.get("phone", "")
            break

    all_conflicts = []
    for emp in CONFIG["employees"]:
        if not emp.get("active", True):
            continue
        name     = emp["name"]
        phone    = emp.get("phone", "")
        ics_file = f"docs/{name.lower()}_schedule.ics"
        if not os.path.exists(ics_file):
            continue
        with open(ics_file) as f:
            content = f.read()
        conflicts = find_conflicts(content)
        if conflicts:
            dates_str = ", ".join(format_date(d) for d in conflicts)
            all_conflicts.append(f"{name}: {dates_str}")
            print(f"  Conflict — {name} on call during vacation: {dates_str}")
            # Text the individual
            if phone:
                try:
                    personal_msg = (f"⚠️ Hi {name}, you are scheduled on call during your vacation on: "
                                    f"{dates_str}. Please coordinate with Jordan.")
                    send_sms(sid, token, from_num, phone, personal_msg)
                    print(f"  Personal conflict alert sent to {name}")
                except Exception as e:
                    print(f"  Failed to text {name}: {e}")

    if all_conflicts and jordan_phone:
        msg = "⚠️ On-Call/Vacation Conflicts:\n" + "\n".join(all_conflicts)
        try:
            send_sms(sid, token, from_num, jordan_phone, msg)
            print(f"  Conflict report sent to Jordan")
        except Exception as e:
            print(f"  Failed to send conflict report: {e}")

if __name__ == "__main__":
    main()
