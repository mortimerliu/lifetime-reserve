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
0 9 * * * cd /root/lifetime-reserve && python3 reserve.py --auto >> reserve.log 2>&1
```

System timezone handles DST automatically.

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
