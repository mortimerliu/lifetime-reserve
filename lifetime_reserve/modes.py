"""Mode handlers — the reservation engine's per-command logic.

Each handler takes a logged-in `LifetimeClient` and the config dict, and returns
structured results (dict or tuple) that the CLI turns into a report. Input-validation
failures raise `ValueError` / `ConfigError` — the library never calls `sys.exit`; the
CLI decides process exit. (`run_interactive` still exits on "nothing to do" because it
is inherently tty-bound.)
"""

import logging
import sys
import time
from datetime import date, datetime, timedelta

import requests

from lifetime_reserve.config import ConfigError
from lifetime_reserve.slots import (
    collect_slots, to_api_time, auto_pick, pick_by_time, fmt_slots, rank_slots)

log = logging.getLogger(__name__)

# How many preferred slots to attempt on a single date before reporting failure.
# Bounds the damage if every candidate is rejected for an account-level reason
# (each rejected attempt leaves a pending registration behind).
MAX_BOOKING_ATTEMPTS = 5


def _slot_key(slot):
    """Identity of a slot across re-searches (resourceId changes per search response)."""
    return (slot["time"], slot.get("resourceName", ""))


def _next_candidate(slots, preferred_times, preferred_courts, tried):
    """Best preference-ranked slot we have not already failed on. None when exhausted."""
    for candidate in rank_slots(slots, preferred_times, preferred_courts):
        if _slot_key(candidate) not in tried:
            return candidate
    return None


def _book_best_available(client, config, target_date, slots, preferred_times=None):
    """Try preferred slots best-first until one is actually booked. Returns (slot, reason).

    A lost race on the best slot no longer sinks the whole date — the next preferred
    slot is attempted, up to MAX_BOOKING_ATTEMPTS. `reason` is None on success and
    carries the last failure otherwise. `preferred_times` narrows the search to specific
    times (a caller that asked for one exact time still gets every court at that time).
    """
    if preferred_times is None:
        preferred_times = config.preferred_times
    candidates = rank_slots(slots, preferred_times, config.preferred_courts)
    if not candidates:
        log.warning("No slots available at preferred times — skipping booking")
        return None, "no preferred time available"

    reason = None
    for slot in candidates[:MAX_BOOKING_ATTEMPTS]:
        log.info("Booking %s %s ...", slot["time"], slot["resourceName"])
        try:
            booking = client.book_court(slot["resourceId"], slot["start"], config.duration)
        except Exception as e:
            log.error("Booking failed: %s", e)
            reason = f"booking failed: {e}"
            continue
        log.info("Confirmed: regId=%s, status=%s, location=%s",
                 booking["regId"], booking["regStatus"], booking.get("location", ""))
        return slot, None
    return None, reason


def fetch_upcoming(client, config):
    """Fetch upcoming reservation labels for the horizon. Empty list on error / no member_ids."""
    member_ids = config.member_ids
    if not member_ids:
        return []
    days_ahead = config.days_ahead
    today = date.today()
    try:
        _, labels = client.reserved_dates_and_labels(
            member_ids,
            today + timedelta(days=1),
            today + timedelta(days=days_ahead),
        )
        return labels
    except Exception as e:
        log.warning("Could not fetch upcoming reservations: %s", e)
        return []


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

def run_interactive(client, config):
    club_id = config.club_id
    sport = config.sport
    duration = config.duration
    days_ahead = config.days_ahead

    target_date = prompt_date(days_ahead)
    log.info("Searching courts for %s ...", target_date.strftime("%A %Y-%m-%d"))
    result = client.search_courts(club_id, sport, target_date, duration)
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
    booking = client.book_court(slot["resourceId"], slot["start"], duration)
    log.info("Confirmed: regId=%s, status=%s, location=%s",
             booking["regId"], booking["regStatus"], booking.get("location", ""))


def run_slot(client, config, slot_datetime_str):
    """Book a specific date/time directly. Returns (dt, booked_slot_or_None, reason_or_None).

    Raises ValueError on a malformed --slot string.
    """
    try:
        dt = datetime.strptime(slot_datetime_str, "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError(
            "Invalid --slot format. Use: YYYY-MM-DD HH:MM (e.g. '2026-03-16 04:30')")

    target_date = dt.date()
    api_time = to_api_time(dt.strftime("%H:%M"))
    club_id = config.club_id
    sport = config.sport
    duration = config.duration

    log.info("Searching courts for %s at %s ...", target_date.strftime("%A %Y-%m-%d"), api_time)
    try:
        result = client.search_courts(club_id, sport, target_date, duration)
        slots = collect_slots(result)
    except Exception as e:
        log.error("Search failed: %s", e)
        return dt, None, f"search failed: {e}"

    slot = pick_by_time(slots, api_time)
    if slot is None:
        available = fmt_slots(slots) if slots else "none"
        log.error("No slot available at %s. Available: %s", api_time, available)
        return dt, None, f"slot not available (options: {available})"

    # Every court at the requested time, in court-preference order — losing one court
    # to a competitor should not fail the request while another court is still open.
    booked, reason = _book_best_available(
        client, config, target_date, slots, preferred_times=[api_time])
    return dt, booked, reason


def run_auto(client, config, fallback=False):
    club_id = config.club_id
    sport = config.sport
    duration = config.duration
    days_ahead = config.days_ahead
    preferred_times = config.preferred_times
    preferred_courts = config.preferred_courts
    retry_count = config.retry_count
    retry_delay = config.retry_delay_seconds

    today = date.today()
    member_ids = config.member_ids
    reserved_dates = set()
    reservation_labels = []

    if member_ids:
        reserved_dates, reservation_labels = client.reserved_dates_and_labels(
            member_ids,
            today + timedelta(days=1),
            today + timedelta(days=days_ahead),
        )
        log.info("Already reserved dates: %s", sorted(reserved_dates) or "none")
    else:
        log.warning("member_ids not in config — skipping reservation check")

    def try_date(target_date):
        """Search and book a single date. Returns the booked slot dict, or None if skipped/no slot."""
        date_str = target_date.strftime("%Y-%m-%d")
        if date_str in reserved_dates:
            log.info("Skipping %s — already have a reservation", date_str)
            return None

        log.info("Searching %s ...", target_date.strftime("%A %Y-%m-%d"))
        result = client.search_courts(club_id, sport, target_date, duration)
        slots = collect_slots(result)

        if not slots:
            log.info("No courts available on %s", date_str)
            return None

        log.info("Available: %s", fmt_slots(slots))

        booked, reason = _book_best_available(client, config, target_date, slots)
        if booked is None:
            log.info("No booking on %s — %s", date_str, reason)
        return booked

    # Priority 1: day 8 — search once, then retry only the booking step
    # Retrying book (not search) on 5xx means we keep the slot locked across attempts
    # rather than re-competing after each server error.
    day8 = today + timedelta(days=days_ahead)
    day8_str = day8.strftime("%Y-%m-%d")

    if day8_str in reserved_dates:
        log.info("Day %d (%s) already reserved — skipping booking attempt", days_ahead, day8_str)
        return {"status": "already_reserved", "target_date": day8, "booked_slot": None,
                "day8_slots": [], "reservation_labels": reservation_labels}

    log.info("Searching %s ...", day8.strftime("%A %Y-%m-%d"))
    try:
        result = client.search_courts(club_id, sport, day8, duration)
        day8_slots = collect_slots(result)
    except Exception as e:
        log.error("Search failed for %s: %s", day8_str, e)
        day8_slots = []

    slot = None
    slot_picked_initially = False
    tried = set()          # slots already lost — never re-picked after a re-search
    if day8_slots:
        log.info("Available: %s", fmt_slots(day8_slots))
        slot = auto_pick(day8_slots, preferred_times, preferred_courts)
        if slot is None:
            log.info("No preferred slot on %s", day8_str)
        else:
            slot_picked_initially = True
    else:
        log.info("No courts available on %s", day8_str)

    booked_slot = None
    booked_date = None

    if slot is not None:
        # Back off only when the server told us to (5xx). After a 4xx we already hold a
        # freshly-searched slot, and the 9 AM list drains in seconds — sleeping there
        # just hands the slot to someone else.
        backoff_before_retry = False
        for attempt in range(1, retry_count + 1):
            if attempt > 1 and backoff_before_retry:
                log.info("Day %d booking retry %d/%d in %ds ...",
                         days_ahead, attempt, retry_count, retry_delay)
                time.sleep(retry_delay)
            elif attempt > 1:
                log.info("Day %d booking retry %d/%d (no backoff — fresh slot) ...",
                         days_ahead, attempt, retry_count)
            try:
                log.info("Booking %s %s (attempt %d/%d) ...",
                         slot["time"], slot["resourceName"], attempt, retry_count)
                booking = client.book_court(slot["resourceId"], slot["start"], duration)
                log.info("Confirmed: regId=%s, status=%s, location=%s",
                         booking["regId"], booking["regStatus"], booking.get("location", ""))
                booked_slot = slot
                booked_date = day8
                break
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                log.error("Day %d booking attempt %d/%d failed: %s",
                          days_ahead, attempt, retry_count, e)
                if status is not None and status < 500:
                    # 4xx: this slot is gone for good — never retry it, take the next best
                    backoff_before_retry = False
                    tried.add(_slot_key(slot))
                    log.info("Slot lost — re-searching %s for another preferred slot ...", day8_str)
                    try:
                        result = client.search_courts(club_id, sport, day8, duration)
                        new_slots = collect_slots(result)
                        if new_slots:
                            log.info("Available: %s", fmt_slots(new_slots))
                    except Exception as search_e:
                        log.error("Re-search failed: %s", search_e)
                        new_slots = []
                    slot = _next_candidate(new_slots, preferred_times, preferred_courts, tried)
                    if slot is None:
                        log.info("No preferred slot left after re-search — done with day %d",
                                 days_ahead)
                        break
                else:
                    # 5xx (or no response at all): server trouble — keep the same slot
                    # and give it a moment before trying again
                    backoff_before_retry = True
            except Exception as e:
                log.error("Day %d booking attempt %d/%d failed: %s",
                          days_ahead, attempt, retry_count, e)
                backoff_before_retry = True

    if booked_slot is not None:
        return {"status": "booked", "target_date": booked_date, "booked_slot": booked_slot,
                "day8_slots": day8_slots, "reservation_labels": reservation_labels}

    # Determine day-8 failure reason (used in report if no fallback booking either)
    if not day8_slots:
        day8_status = "no_courts"
    elif not slot_picked_initially:
        day8_status = "no_preferred"
    else:
        day8_status = "booking_failed"  # slot was picked but all retries failed

    if not fallback:
        log.info("No booking on day %d — fallback scan disabled (use --fallback to scan days 1–%d)",
                 days_ahead, days_ahead - 1)
        return {"status": day8_status, "target_date": day8, "booked_slot": None,
                "day8_slots": day8_slots, "reservation_labels": reservation_labels}

    # Priority 2: scan days 1–7 once (no retry)
    log.info("No booking on day %d — scanning days 1–%d ...", days_ahead, days_ahead - 1)
    for i in range(1, days_ahead):
        target = today + timedelta(days=i)
        try:
            booked = try_date(target)
            if booked is not None:
                return {"status": "booked", "target_date": target, "booked_slot": booked,
                        "day8_slots": day8_slots, "reservation_labels": reservation_labels}
        except Exception as e:
            log.error("Error trying %s: %s — skipping", target.strftime("%Y-%m-%d"), e)

    log.info("No preferred slots found on any day (1–%d).", days_ahead)
    return {"status": day8_status, "target_date": day8, "booked_slot": None,
            "day8_slots": day8_slots, "reservation_labels": reservation_labels}


def run_dry_run(client, config):
    club_id = config.club_id
    sport = config.sport
    duration = config.duration
    days_ahead = config.days_ahead
    preferred_times = config.preferred_times
    preferred_courts = config.preferred_courts

    today = date.today()

    member_ids = config.member_ids
    if member_ids:
        reserved_dates, _ = client.reserved_dates_and_labels(
            member_ids,
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

        result = client.search_courts(club_id, sport, target_date, duration)
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


def run_date(client, config, date_str):
    """Search a specific date and book best preferred slot.
    Returns (target_date, booked_slot_or_None, reason_or_None, slots). Raises ValueError
    on a malformed date."""
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"Invalid date: {date_str}. Use YYYY-MM-DD.")
    club_id = config.club_id
    sport = config.sport
    duration = config.duration

    log.info("Searching %s ...", target_date.strftime("%A %Y-%m-%d"))
    try:
        result = client.search_courts(club_id, sport, target_date, duration)
        slots = collect_slots(result)
    except Exception as e:
        log.error("Search failed: %s", e)
        return target_date, None, f"search failed: {e}", []

    if not slots:
        log.info("No courts available on %s", date_str)
        return target_date, None, "no courts available", []
    log.info("Available: %s", fmt_slots(slots))
    booked, reason = _book_best_available(client, config, target_date, slots)
    if booked is None:
        log.info("No booking on %s — %s", date_str, reason)
    return target_date, booked, reason, slots


def run_cancel(client, config, date_str):
    """Cancel reservation(s) on a specific date. Returns (target_date, cancelled_labels, found_count).

    Raises ValueError on a malformed date, ConfigError if member_ids is missing.
    """
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"Invalid date: {date_str}. Use YYYY-MM-DD.")
    member_ids = config.member_ids
    if not member_ids:
        raise ConfigError("member_ids not configured — cannot look up reservation")
    reservations = client.get_reservations(member_ids, target_date, target_date)
    if not reservations:
        log.info("No reservation found on %s", date_str)
        return target_date, [], 0
    cancelled = []
    for res in reservations:
        attendee_id = res.get("attendee_id")
        if not attendee_id:
            log.error("Cannot cancel %s: attendee ID missing from API response", res["label"])
            continue
        log.info("Cancelling %s (attendeeId=%s) ...", res["label"], attendee_id)
        try:
            client.cancel_attendee(attendee_id)
            log.info("Cancelled successfully")
            cancelled.append(res["label"])
        except Exception as e:
            log.error("Cancel failed for %s: %s", res["label"], e)
    return target_date, cancelled, len(reservations)
