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
HTTP_TIMEOUT = (5, 10)  # (connect, read) in seconds

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


def validate_config(config):
    required = ["username", "password", "club_id", "sport", "duration", "days_ahead"]
    missing = [k for k in required if k not in config]
    if missing:
        log.error("Missing required config keys: %s", ", ".join(missing))
        sys.exit(1)


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
        timeout=HTTP_TIMEOUT,
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
        timeout=HTTP_TIMEOUT,
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
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    reserved = set()
    for item in resp.json().get("results", []):
        start = item.get("start", "")
        if start:
            reserved.add(start[:10])
    return reserved


def raise_for_status_with_body(resp):
    """Like raise_for_status() but includes the response body in the exception message."""
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        body = resp.text[:500] if resp.text else "(empty)"
        raise requests.HTTPError(f"{e} — body: {body}", response=resp) from None


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
        timeout=HTTP_TIMEOUT,
    )
    raise_for_status_with_body(resp)
    booking = resp.json()

    # Complete the booking (accept waiver) — moves from pending → completed
    reg_id = booking.get("regId")
    agreement_id = booking.get("agreement", {}).get("agreementId")
    if reg_id and agreement_id and not booking.get("registrationType", {}).get("skipConfirmation", True):
        complete_resp = session.put(
            f"{API_BASE}/sys/registrations/V3/ux/resource/{reg_id}/complete",
            json={"acceptedDocuments": [int(agreement_id)]},
            headers={**auth_headers(token, sso_id), "content-type": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
        try:
            raise_for_status_with_body(complete_resp)
            booking["regStatus"] = "completed"
        except requests.HTTPError as e:
            # Booking exists but waiver confirmation failed — slot is ours (pending).
            # Don't raise: returning here stops the retry loop from re-booking the same slot.
            log.warning("Booking created (regId=%s) but /complete failed: %s", reg_id, e)
            log.warning("Slot is pending — check your reservations page manually")

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


def fmt_slots(slots):
    return ", ".join(f"{s['time']} {s['resourceName']}" for s in slots)


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
    log.info("Searching courts for %s ...", target_date.strftime("%A %Y-%m-%d"))
    result = search_courts(session, token, sso_id, club_id, sport, target_date, duration)
    slots = collect_slots(result)
    if not slots:
        log.info("No courts available for %s", target_date)
        sys.exit(0)

    slot = prompt_slot(slots)
    if slot is None:
        print("No slot selected.")
        sys.exit(0)

    print(f"\nSelected: {slot['time']} — {slot['resourceName']}")
    if input("Confirm booking? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return

    log.info("Booking %s %s ...", slot["time"], slot["resourceName"])
    booking = book_court(session, token, sso_id, slot["resourceId"], slot["start"], duration)
    log.info("Confirmed: regId=%s, status=%s, location=%s",
             booking["regId"], booking["regStatus"], booking.get("location", ""))


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

    log.info("Searching courts for %s at %s ...", target_date.strftime("%A %Y-%m-%d"), api_time)
    result = search_courts(session, token, sso_id, club_id, sport, target_date, duration)
    slots = collect_slots(result)

    slot = pick_by_time(slots, api_time)
    if slot is None:
        available = fmt_slots(slots) if slots else "none"
        log.error("No slot available at %s. Available: %s", api_time, available)
        sys.exit(1)

    log.info("Booking %s %s ...", slot["time"], slot["resourceName"])
    booking = book_court(session, token, sso_id, slot["resourceId"], slot["start"], duration)
    log.info("Confirmed: regId=%s, status=%s, location=%s",
             booking["regId"], booking["regStatus"], booking.get("location", ""))


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
        """Search and book a single date. Returns True if booked, False if skipped/no slot."""
        date_str = target_date.strftime("%Y-%m-%d")
        if date_str in reserved_dates:
            log.info("Skipping %s — already have a reservation", date_str)
            return False

        log.info("Searching %s ...", target_date.strftime("%A %Y-%m-%d"))
        result = search_courts(session, token, sso_id, club_id, sport, target_date, duration)
        slots = collect_slots(result)

        if not slots:
            log.info("No courts available on %s", date_str)
            return False

        log.info("Available: %s", fmt_slots(slots))

        slot = auto_pick(slots, preferred_times, preferred_courts)
        if slot is None:
            log.info("No preferred slot on %s", date_str)
            return False

        log.info("Booking %s %s ...", slot["time"], slot["resourceName"])
        booking = book_court(session, token, sso_id, slot["resourceId"], slot["start"], duration)
        log.info("Confirmed: regId=%s, status=%s, location=%s",
                 booking["regId"], booking["regStatus"], booking.get("location", ""))
        return True

    # Priority 1: day 8 — search once, then retry only the booking step
    # Retrying book (not search) on 5xx means we keep the slot locked across attempts
    # rather than re-competing after each server error.
    day8 = today + timedelta(days=days_ahead)
    day8_str = day8.strftime("%Y-%m-%d")

    log.info("Searching %s ...", day8.strftime("%A %Y-%m-%d"))
    try:
        result = search_courts(session, token, sso_id, club_id, sport, day8, duration)
        slots = collect_slots(result)
    except Exception as e:
        log.error("Search failed for %s: %s", day8_str, e)
        slots = []

    slot = None
    if slots:
        log.info("Available: %s", fmt_slots(slots))
        slot = auto_pick(slots, preferred_times, preferred_courts)
        if slot is None:
            log.info("No preferred slot on %s", day8_str)
    else:
        log.info("No courts available on %s", day8_str)

    if slot is not None:
        for attempt in range(1, retry_count + 1):
            if attempt > 1:
                log.info("Day %d booking retry %d/%d in %ds ...",
                         days_ahead, attempt, retry_count, retry_delay)
                time.sleep(retry_delay)
            try:
                log.info("Booking %s %s (attempt %d/%d) ...",
                         slot["time"], slot["resourceName"], attempt, retry_count)
                booking = book_court(session, token, sso_id,
                                     slot["resourceId"], slot["start"], duration)
                log.info("Confirmed: regId=%s, status=%s, location=%s",
                         booking["regId"], booking["regStatus"], booking.get("location", ""))
                return
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                log.error("Day %d booking attempt %d/%d failed: %s",
                          days_ahead, attempt, retry_count, e)
                if status is not None and status < 500:
                    # 4xx: slot is gone — re-search for another preferred slot
                    log.info("Slot taken — re-searching %s ...", day8_str)
                    try:
                        result = search_courts(session, token, sso_id,
                                               club_id, sport, day8, duration)
                        slots = collect_slots(result)
                        if slots:
                            log.info("Available: %s", fmt_slots(slots))
                        slot = auto_pick(slots, preferred_times, preferred_courts) if slots else None
                    except Exception as search_e:
                        log.error("Re-search failed: %s", search_e)
                        slot = None
                    if slot is None:
                        log.info("No preferred slot after re-search — done with day %d", days_ahead)
                        break
                # 5xx: keep same slot, retry booking
            except Exception as e:
                log.error("Day %d booking attempt %d/%d failed: %s",
                          days_ahead, attempt, retry_count, e)

    # Priority 2: scan days 1–7 once (no retry)
    log.info("No booking on day %d — scanning days 1–%d ...", days_ahead, days_ahead - 1)
    fetch_reserved_dates()
    for i in range(1, days_ahead):
        target = today + timedelta(days=i)
        try:
            if try_date(target):
                return
        except Exception as e:
            log.error("Error trying %s: %s — skipping", target.strftime("%Y-%m-%d"), e)

    log.info("No preferred slots found on any day (1–%d).", days_ahead)


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
        label = target_date.strftime("%A %Y-%m-%d")

        if date_str in reserved_dates:
            log.info("%s: already reserved", label)
            continue

        result = search_courts(session, token, sso_id, club_id, sport, target_date, duration)
        slots = collect_slots(result)

        if not slots:
            log.info("%s: no slots available", label)
            continue

        slot = auto_pick(slots, preferred_times, preferred_courts)
        all_times = fmt_slots(slots)
        if slot:
            log.info("%s: would book %s %s", label, slot["time"], slot["resourceName"])
            log.info("  All available: %s", all_times)
        else:
            log.info("%s: no preferred time available", label)
            log.info("  All available: %s", all_times)


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
    parser.add_argument("--wait-until", metavar="HH:MM:SS",
                        help="Login immediately, then wait until this time before booking (e.g. 09:00:00)")
    return parser.parse_args()


def main():
    args = parse_args()
    log.info("=" * 60)
    mode = "auto" if args.auto else "dry-run" if args.dry_run else f"slot({args.slot})" if args.slot else "interactive"
    log.info("Run started — mode: %s", mode)
    config = load_config()
    validate_config(config)
    session = make_session()

    # Login with retry — transient network errors at 8:55 AM shouldn't abort the whole run
    for attempt in range(1, 4):
        try:
            token, sso_id = login(session, config["username"], config["password"])
            break
        except Exception as e:
            log.error("Login attempt %d/3 failed: %s", attempt, e)
            if attempt == 3:
                log.error("All login attempts failed — exiting")
                sys.exit(1)
            time.sleep(2)

    if args.wait_until:
        try:
            target_time = datetime.strptime(args.wait_until, "%H:%M:%S").time()
        except ValueError:
            log.error("Invalid --wait-until format. Use HH:MM:SS (e.g. 09:00:00)")
            sys.exit(1)
        now = datetime.now()
        target_dt = datetime.combine(now.date(), target_time)
        wait_seconds = (target_dt - now).total_seconds()
        if wait_seconds > 0:
            log.info("Logged in early — waiting %.2fs until %s", wait_seconds, args.wait_until)
            # Use monotonic clock for drift-free polling; datetime.now() only for initial gap.
            mono_target = time.monotonic() + wait_seconds
            while True:
                remaining = mono_target - time.monotonic()
                if remaining <= 0.020:
                    break  # hand off to spin for final 20 ms
                time.sleep(min(remaining - 0.020, 0.5))
            # Spin for the last ~20 ms to avoid scheduler overshoot
            while time.monotonic() < mono_target:
                pass
            log.info("Reached target time %s (overshoot: %.1f ms)",
                     args.wait_until, (time.monotonic() - mono_target) * 1000)
        else:
            log.warning("--wait-until time %s is in the past, proceeding immediately", args.wait_until)

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
