# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the script

```bash
.venv/bin/python reserve.py              # interactive: pick date, time, court
.venv/bin/python reserve.py --auto       # auto-book (used by scheduled job)
.venv/bin/python reserve.py --dry-run    # show available slots, no booking
```

## Architecture

Single-file script (`reserve.py`) with a flat function structure. All configuration is read from `config.json` at startup.

**API layer** — three Lifetime Fitness endpoints, all under `https://api.lifetimefitness.com`:
- `POST /auth/v2/login` → returns `token` (JWE, used as `x-ltf-jwe`) and `ssoId` (used as `x-ltf-ssoid`)
- `GET /ux/web-schedules/v2/resources/booking/search` → available court slots for a date
- `POST /sys/registrations/V3/ux/resource` → creates a booking
- `GET /ux/web-schedules/v3/reservations` → existing reservations (to skip already-booked dates)

Every API request requires the `ocp-apim-subscription-key` header (hardcoded) plus the two auth headers from login.

**Auto/dry-run booking logic** (`main()`):
1. Fetch existing reservations for days 1–8 once upfront
2. Retry day 8 up to `retry_count` times with `retry_delay_seconds` between attempts (handles slots not yet released at exactly 9 AM)
3. If day 8 fails all retries, scan days 1–7 once in order, skipping days with existing reservations
4. `auto_pick()` selects by preferred time first, then preferred court order — returns `None` if no preferred time is available (never falls back to arbitrary slots)

**Interactive mode** skips all retry/scan logic — user selects date and slot manually, confirms before booking.

## Configuration (`config.json`)

| Key | Purpose |
|-----|---------|
| `username` / `password` | Lifetime login credentials |
| `club_id` | `"36"` = Fairfax VA |
| `sport` | `"Pickleball: Indoor"` |
| `duration` | Minutes (60 or 90) |
| `days_ahead` | How far ahead to book (8 = max allowed) |
| `preferred_times` | Ordered list, e.g. `["8:00 AM", "7:30 AM"]` — only these times will be booked |
| `preferred_courts` | Ordered preference, e.g. `["Court 3", "Court 2", "Court 1"]` |
| `member_ids` | Household member IDs for reservation lookup (find in DevTools network tab on the reservations page) |
| `retry_count` | Number of attempts for day 8 |
| `retry_delay_seconds` | Wait between day-8 retries |

## Scheduled job (macOS launchd)

Runs daily at 9 AM via `~/Library/LaunchAgents/com.user.lifetime-reserve.plist`. Output goes to `reserve.log`.

```bash
# Install / reload after editing the plist
launchctl unload ~/Library/LaunchAgents/com.user.lifetime-reserve.plist
launchctl load ~/Library/LaunchAgents/com.user.lifetime-reserve.plist

# Trigger manually
launchctl start com.user.lifetime-reserve

# Watch logs
tail -f reserve.log
```

The plist points directly to `.venv/bin/python` — no activation needed.
