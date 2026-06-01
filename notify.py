import json, os, urllib.request, urllib.parse, base64
from datetime import date

with open("config.json") as f:
    CONFIG = json.load(f)

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
    sid      = os.environ.get("TWILIO_ACCOUNT_SID", "")
    token    = os.environ.get("TWILIO_AUTH_TOKEN", "")
    from_num = os.environ.get("TWILIO_FROM", "")
    today    = date.today().strftime("%Y%m%d")

    # TEST MODE: send test text to a single employee
    test_user = os.environ.get("TEST_USER", "").strip().lower()
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

        if f"DTSTART;VALUE=DATE:{today}" in content:
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

if __name__ == "__main__":
    main()
