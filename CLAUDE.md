# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the script

```bash
.venv/bin/python reserve.py                            # interactive: pick date, time, court
.venv/bin/python reserve.py --auto                     # auto-book (used by scheduled job)
.venv/bin/python reserve.py --auto --wait-until 09:00:00  # login early, book at 9AM sharp
.venv/bin/python reserve.py --dry-run                  # show available slots, no booking
.venv/bin/python reserve.py --slot "2026-03-16 04:30"  # book a specific slot directly (24h)
```

## Architecture

Single-file script (`reserve.py`) with a flat function structure. All configuration is read from `config.json` at startup.

**API layer** — endpoints under `https://api.lifetimefitness.com`:
- `POST /auth/v2/login` → returns `token` (JWE, used as `x-ltf-jwe`) and `ssoId` (used as `x-ltf-ssoid`)
- `GET /ux/web-schedules/v2/resources/booking/search` → available court slots for a date
- `POST /sys/registrations/V3/ux/resource` → creates a booking (`regStatus: pending`)
- `PUT /sys/registrations/V3/ux/resource/{regId}/complete` → accepts waiver, moves booking to `completed` (required — pending bookings don't appear in the reservations list)
- `GET /ux/web-schedules/v3/reservations` → existing reservations (to skip already-booked dates)

Every API request requires the `ocp-apim-subscription-key` header (hardcoded) plus the two auth headers from login.

**Code structure** — four mode handler functions: `run_interactive`, `run_slot`, `run_auto`, `run_dry_run`. Shared helpers: `book_court` (create + complete), `collect_slots`, `auto_pick`, `pick_by_time`, `to_api_time`, `fmt_slots`, `validate_config`, `raise_for_status_with_body`.

**Auto booking logic** (`run_auto()`):
1. Search day 8 **once** at 9 AM sharp, then retry **only the booking step** up to `retry_count` times:
   - On 5xx (server overload): immediately retry booking the same slot — avoids releasing the slot between attempts
   - On 4xx (slot taken): re-search once for another preferred slot, then continue retrying
2. If day 8 yields no booking, fetch existing reservations for days 1–7, then scan in order skipping already-booked days. Each day is tried once; errors on individual days are caught and skipped rather than aborting the scan.
3. `auto_pick()` selects by preferred time first, then preferred court order — returns `None` if no preferred time is available (never falls back to arbitrary slots)

**Error handling**: `raise_for_status_with_body()` wraps `raise_for_status()` to include the API response body in exception messages. `/complete` failures are caught as warnings (booking stays pending) rather than raising, to prevent retry loops from double-booking.

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

## Scheduling options

GitHub Actions cron has unpredictable queue delays (minutes) and is **not suitable** for this time-critical task. Use one of the options below instead.

### Option 1: macOS launchd (current) — requires MacBook on and awake at 9 AM

Plist installed at `~/Library/LaunchAgents/com.user.lifetime-reserve.plist`. Output goes to `reserve.log`.

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

### Option 2: VPS with cron (~$4/month, most reliable)

Any cheap VPS (Hetzner CX22, DigitalOcean Droplet). Cron fires within seconds of schedule.

```bash
sudo timedatectl set-timezone America/New_York
sudo apt install python3 python3-pip git -y
git clone https://github.com/mortimerliu/lifetime-reserve.git
cd lifetime-reserve && pip3 install requests
nano config.json   # paste your config

crontab -e
# Add:
55 8 * * * cd /root/lifetime-reserve && python3 reserve.py --auto --wait-until 09:00:00 >> reserve.log 2>&1
```

System timezone handles DST automatically.

Log rotation is configured at `/etc/logrotate.d/lifetime-reserve` (daily, 30-day retention). Check VPS logs remotely using `check_vps_log.sh`:

```bash
./check_vps_log.sh          # last 50 lines (default)
./check_vps_log.sh follow   # live stream
./check_vps_log.sh all      # full log
```

### Option 3: GitHub Actions (manual trigger only)

Schedule is disabled in `.github/workflows/reserve.yml` due to queue delays. The workflow still exists for **manual runs** via the GitHub UI or:

```bash
gh workflow run reserve.yml
gh run list --limit 1   # check status
```

To re-enable the schedule, restore the `schedule:` block in the workflow file with:
```yaml
  schedule:
    - cron: '0 13 * * *'  # 9:00 AM EDT (summer)
    - cron: '0 14 * * *'  # 9:00 AM EST (winter)
```

The `CONFIG_JSON` secret is already set in the repo — it contains the full `config.json` contents.
