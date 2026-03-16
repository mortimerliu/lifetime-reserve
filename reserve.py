#!/usr/bin/env python3
"""
Lifetime Fitness Pickleball Court Auto-Reservation

Modes:
  Interactive (default): choose date, time, and court interactively
  Auto:                  book best slot automatically (for scheduled runs)
  Dry-run:               show available slots without booking
  Slot:                  book a specific date/time directly (no prompts)

Usage:
    .venv/bin/python reserve.py                              # interactive
    .venv/bin/python reserve.py --auto                       # auto-book from preferred_times config
    .venv/bin/python reserve.py --dry-run                    # show slots only
    .venv/bin/python reserve.py --slot "2026-03-16 04:30"   # book specific slot (24h)
"""

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta

import requests

CONFIG_FILE = "config.json"
API_BASE = "https://api.lifetimefitness.com"
APIM_KEY = "924c03ce573d473793e184219a6a19bd"
ORIGIN = "https://my.lifetime.life"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)


# ── Configuration ──────────────────────────────────────────────────────────────

def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


# ── HTTP session ───────────────────────────────────────────────────────────────

def make_session():
    s = requests.Session()
    s.headers.update({
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "origin": ORIGIN,
        "ocp-apim-subscription-key": APIM_KEY,
    })
    return s


# ── API calls ──────────────────────────────────────────────────────────────────

def login(session, username, password):
    resp = session.post(
        f"{API_BASE}/auth/v2/login",
        json={"username": username, "password": password},
        headers={"content-type": "application/json; charset=UTF-8"},
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "0":
        raise RuntimeError(f"Login failed: {data}")
    log.info("Logged in as %s", data["username"])
    return data["token"], data["ssoId"]


def auth_headers(token, sso_id):
    return {"x-ltf-jwe": token, "x-ltf-ssoid": sso_id}


def search_courts(session, token, sso_id, club_id, sport, target_date, duration):
    resp = session.get(
        f"{API_BASE}/ux/web-schedules/v2/resources/booking/search",
        params={
            "homeClub": club_id,
            "clubId": club_id,
            "sport": sport,
            "date": target_date.strftime("%Y-%m-%d"),
            "startTime": "-1",
            "duration": str(duration),
        },
        headers=auth_headers(token, sso_id),
    )
    resp.raise_for_status()
    return resp.json()


def get_reserved_dates(session, token, sso_id, member_ids, start_date, end_date):
    """Return set of YYYY-MM-DD strings that already have a court reservation."""
    params = [
        ("start", start_date.strftime("%-m/%-d/%Y")),
        ("end", end_date.strftime("%-m/%-d/%Y")),
        ("groupCamps", "true"),
        ("pageSize", "0"),
    ]
    for mid in member_ids:
        params.append(("memberIds", str(mid)))

    resp = session.get(
        f"{API_BASE}/ux/web-schedules/v3/reservations",
        params=params,
        headers=auth_headers(token, sso_id),
    )
    resp.raise_for_status()
    reserved = set()
    for item in resp.json().get("results", []):
        start = item.get("start", "")
        if start:
            reserved.add(start[:10])
    return reserved


def book_court(session, token, sso_id, resource_id, start, duration):
    """Create a booking and immediately complete it (accept waiver)."""
    resp = session.post(
        f"{API_BASE}/sys/registrations/V3/ux/resource",
        json={
            "resourceId": resource_id,
            "start": start,
            "service": None,
            "duration": str(duration),
        },
        headers={**auth_headers(token, sso_id), "content-type": "application/json"},
    )
    resp.raise_for_status()
    booking = resp.json()

    # Complete the booking (accept waiver) — moves from pending → completed
    reg_id = booking.get("regId")
    agreement_id = booking.get("agreement", {}).get("agreementId")
    if reg_id and agreement_id and not booking.get("registrationType", {}).get("skipConfirmation", True):
        complete_resp = session.put(
            f"{API_BASE}/sys/registrations/V3/ux/resource/{reg_id}/complete",
            json={"acceptedDocuments": [int(agreement_id)]},
            headers={**auth_headers(token, sso_id), "content-type": "application/json"},
        )
        complete_resp.raise_for_status()
        booking["regStatus"] = "completed"

    return booking


# ── Slot utilities ─────────────────────────────────────────────────────────────

def collect_slots(search_result):
    slots = []
    for part in search_result.get("results", {}).get("dayParts", []):
        for slot in part.get("availableTimes", []):
            slot["_part"] = part["name"]
            slots.append(slot)
    return slots


def to_api_time(hhmm_24h):
    """Convert '04:30' (24h) to '4:30 AM' (API time format)."""
    return datetime.strptime(hhmm_24h, "%H:%M").strftime("%-I:%M %p")


def auto_pick(slots, preferred_times, preferred_courts):
    """Pick best slot by preferred time then preferred court. Returns None if no match."""
    def court_rank(slot):
        name = slot.get("resourceName", "")
        try:
            return preferred_courts.index(name)
        except ValueError:
            return len(preferred_courts)

    for pref_time in preferred_times:
        candidates = [s for s in slots if s["time"] == pref_time]
        if candidates:
            candidates.sort(key=court_rank)
            return candidates[0]

    log.warning("No slots available at preferred times — skipping booking")
    return None


def pick_by_time(slots, api_time):
    """Return first available slot matching api_time (e.g. '4:30 AM')."""
    return next((s for s in slots if s["time"] == api_time), None)


# ── Interactive prompts ────────────────────────────────────────────────────────

def prompt_date(days_ahead):
    print("\nWhich date would you like to book?")
    today = date.today()
    options = [today + timedelta(days=i) for i in range(1, 15)]
    for i, d in enumerate(options, 1):
        marker = " (default)" if (d - today).days == days_ahead else ""
        print(f"  {i}) {d.strftime('%A %Y-%m-%d')}{marker}")
    print(f"  or press Enter for default (+{days_ahead} days = {today + timedelta(days=days_ahead)})")

    while True:
        raw = input("Choice: ").strip()
        if raw == "":
            return today + timedelta(days=days_ahead)
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        print("  Invalid — enter a number or press Enter.")


def prompt_slot(slots):
    if not slots:
        return None
    print("\nAvailable slots:")
    for i, s in enumerate(slots, 1):
        print(f"  {i}) {s['time']:>10}  {s['resourceName']}")
    while True:
        raw = input("Choose a slot (number): ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(slots):
            return slots[int(raw) - 1]
        print("  Invalid — enter a number from the list.")


# ── Mode handlers ──────────────────────────────────────────────────────────────

def run_interactive(session, token, sso_id, config):
    club_id = config.get("club_id", "36")
    sport = config.get("sport", "Pickleball: Indoor")
    duration = config.get("duration", 60)
    days_ahead = config.get("days_ahead", 8)

    target_date = prompt_date(days_ahead)
    print(f"\nSearching courts for {target_date.strftime('%A %Y-%m-%d')} ...")
    result = search_courts(session, token, sso_id, club_id, sport, target_date, duration)
    slots = collect_slots(result)
    if not slots:
        print(f"No courts available for {target_date}.")
        sys.exit(0)

    slot = prompt_slot(slots)
    if slot is None:
        print("No slot selected.")
        sys.exit(0)

    print(f"\nSelected: {slot['time']} — {slot['resourceName']}")
    if input("Confirm booking? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return

    print("Booking ...")
    booking = book_court(session, token, sso_id, slot["resourceId"], slot["start"], duration)
    print(f"Confirmed: regId={booking['regId']}, status={booking['regStatus']}, location={booking.get('location', '')}")


def run_slot(session, token, sso_id, config, slot_datetime_str):
    """Book a specific date/time directly. Format: 'YYYY-MM-DD HH:MM' (24h)."""
    try:
        dt = datetime.strptime(slot_datetime_str, "%Y-%m-%d %H:%M")
    except ValueError:
        log.error("Invalid --slot format. Use: YYYY-MM-DD HH:MM (e.g. '2026-03-16 04:30')")
        sys.exit(1)

    target_date = dt.date()
    api_time = to_api_time(dt.strftime("%H:%M"))
    club_id = config.get("club_id", "36")
    sport = config.get("sport", "Pickleball: Indoor")
    duration = config.get("duration", 60)

    print(f"\nSearching courts for {target_date.strftime('%A %Y-%m-%d')} at {api_time} ...")
    result = search_courts(session, token, sso_id, club_id, sport, target_date, duration)
    slots = collect_slots(result)

    slot = pick_by_time(slots, api_time)
    if slot is None:
        available = ", ".join(s["time"] for s in slots) if slots else "none"
        log.error("No slot available at %s. Available: %s", api_time, available)
        sys.exit(1)

    print(f"Booking: {slot['time']} {slot['resourceName']} ...")
    booking = book_court(session, token, sso_id, slot["resourceId"], slot["start"], duration)
    print(f"Confirmed: regId={booking['regId']}, status={booking['regStatus']}, location={booking.get('location', '')}")


def run_auto(session, token, sso_id, config):
    club_id = config.get("club_id", "36")
    sport = config.get("sport", "Pickleball: Indoor")
    duration = config.get("duration", 60)
    days_ahead = config.get("days_ahead", 8)
    preferred_times = config.get("preferred_times", [])
    preferred_courts = config.get("preferred_courts", [])
    retry_count = config.get("retry_count", 3)
    retry_delay = config.get("retry_delay_seconds", 10)

    today = date.today()
    member_ids = config.get("member_ids", [])
    reserved_dates = set()

    def fetch_reserved_dates():
        nonlocal reserved_dates
        if member_ids:
            reserved_dates = get_reserved_dates(
                session, token, sso_id, member_ids,
                today + timedelta(days=1),
                today + timedelta(days=days_ahead - 1),
            )
            log.info("Already reserved dates: %s", sorted(reserved_dates) or "none")
        else:
            log.warning("member_ids not in config — skipping reservation check")

    def try_date(target_date):
        date_str = target_date.strftime("%Y-%m-%d")
        if date_str in reserved_dates:
            log.info("Skipping %s — already have a reservation", date_str)
            return False

        print(f"\nSearching courts for {target_date.strftime('%A %Y-%m-%d')} ...")
        result = search_courts(session, token, sso_id, club_id, sport, target_date, duration)
        slots = collect_slots(result)

        if not slots:
            log.info("No courts available on %s", date_str)
            return False

        print("  Available: " + ", ".join(f"{s['time']} {s['resourceName']}" for s in slots))

        slot = auto_pick(slots, preferred_times, preferred_courts)
        if slot is None:
            log.info("No preferred slot on %s", date_str)
            return False

        print(f"  Booking: {slot['time']} {slot['resourceName']} ...")
        booking = book_court(session, token, sso_id, slot["resourceId"], slot["start"], duration)
        print(f"  Confirmed: regId={booking['regId']}, status={booking['regStatus']}, location={booking.get('location', '')}")
        return True

    # Priority 1: day 8 — retry up to retry_count times
    day8 = today + timedelta(days=days_ahead)
    for attempt in range(1, retry_count + 1):
        if attempt > 1:
            log.info("Day %d retry %d/%d in %ds ...", days_ahead, attempt, retry_count, retry_delay)
            time.sleep(retry_delay)
        try:
            if try_date(day8):
                return
        except Exception as e:
            log.error("Day %d attempt %d/%d failed: %s", days_ahead, attempt, retry_count, e)

    # Priority 2: scan days 1–7 once (no retry)
    log.info("No preferred slot on day %d after %d attempts — scanning days 1–%d ...",
             days_ahead, retry_count, days_ahead - 1)
    fetch_reserved_dates()
    for i in range(1, days_ahead):
        if try_date(today + timedelta(days=i)):
            return

    print("\nNo preferred slots found on any day (1–8).")


def run_dry_run(session, token, sso_id, config):
    club_id = config.get("club_id", "36")
    sport = config.get("sport", "Pickleball: Indoor")
    duration = config.get("duration", 60)
    days_ahead = config.get("days_ahead", 8)
    preferred_times = config.get("preferred_times", [])
    preferred_courts = config.get("preferred_courts", [])

    today = date.today()

    member_ids = config.get("member_ids", [])
    if member_ids:
        reserved_dates = get_reserved_dates(
            session, token, sso_id, member_ids,
            today + timedelta(days=1),
            today + timedelta(days=days_ahead),
        )
        log.info("Already reserved dates: %s", sorted(reserved_dates) or "none")
    else:
        reserved_dates = set()

    for i in range(1, days_ahead + 1):
        target_date = today + timedelta(days=i)
        date_str = target_date.strftime("%Y-%m-%d")

        if date_str in reserved_dates:
            print(f"\n{target_date.strftime('%A %Y-%m-%d')}: already reserved")
            continue

        result = search_courts(session, token, sso_id, club_id, sport, target_date, duration)
        slots = collect_slots(result)

        if not slots:
            print(f"\n{target_date.strftime('%A %Y-%m-%d')}: no slots available")
            continue

        slot = auto_pick(slots, preferred_times, preferred_courts)
        all_times = ", ".join(f"{s['time']} {s['resourceName']}" for s in slots)
        if slot:
            print(f"\n{target_date.strftime('%A %Y-%m-%d')}: would book {slot['time']} {slot['resourceName']}")
            print(f"  All available: {all_times}")
        else:
            print(f"\n{target_date.strftime('%A %Y-%m-%d')}: no preferred time available")
            print(f"  All available: {all_times}")


# ── Entry point ────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Lifetime Fitness Pickleball Court Reservation")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--auto", action="store_true",
                       help="Auto-book best available slot from preferred_times config")
    group.add_argument("--dry-run", action="store_true",
                       help="Show available slots without booking")
    group.add_argument("--slot", metavar="DATETIME",
                       help="Book a specific slot: 'YYYY-MM-DD HH:MM' (24h, e.g. '2026-03-16 04:30')")
    return parser.parse_args()


def main():
    args = parse_args()
    config = load_config()
    session = make_session()
    token, sso_id = login(session, config["username"], config["password"])

    if args.slot:
        run_slot(session, token, sso_id, config, args.slot)
    elif args.auto:
        run_auto(session, token, sso_id, config)
    elif args.dry_run:
        run_dry_run(session, token, sso_id, config)
    else:
        run_interactive(session, token, sso_id, config)


if __name__ == "__main__":
    main()
