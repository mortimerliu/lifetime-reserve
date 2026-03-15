# Lifetime Fitness Pickleball Court Auto-Reservation

Automatically books pickleball courts at Lifetime Fitness. Supports interactive selection, fully automated booking, and dry-run mode.

## Setup

**Prerequisites:** Python 3.12+

```bash
python3 -m venv .venv
.venv/bin/pip install requests
```

Copy `config.json` and fill in your credentials (see [Configuration](#configuration)).

## Usage

```bash
.venv/bin/python reserve.py                            # interactive: pick date, time, court
.venv/bin/python reserve.py --auto                     # auto-book best available slot
.venv/bin/python reserve.py --dry-run                  # show available slots, no booking
.venv/bin/python reserve.py --slot "2026-03-16 04:30"  # book a specific slot directly (24h format)
```

## Configuration

Edit `config.json`:

| Key | Description |
|-----|-------------|
| `username` / `password` | Lifetime Fitness login credentials |
| `club_id` | Club ID (`"36"` = Fairfax, VA) |
| `sport` | `"Pickleball: Indoor"` |
| `duration` | Session length in minutes (`60` or `90`) |
| `days_ahead` | How far ahead to book (max `8`) |
| `preferred_times` | Ordered list of preferred times, e.g. `["8:00 AM", "7:30 AM"]` — only these times will be booked in auto mode |
| `preferred_courts` | Court preference order, e.g. `["Court 3", "Court 2", "Court 1"]` |
| `member_ids` | Household member IDs for reservation lookup (find via DevTools on the Lifetime reservations page) |
| `retry_count` | Number of retry attempts for day-8 booking |
| `retry_delay_seconds` | Seconds to wait between retries |

## Auto-booking logic

In `--auto` mode:

1. Checks existing reservations for days 1–8 to avoid double-booking.
2. Tries to book day 8 (furthest out), retrying up to `retry_count` times — handles slots not yet released at exactly 9 AM.
3. If day 8 fails all retries, scans days 1–7 in order, skipping already-booked days.
4. Picks the first slot matching `preferred_times` and `preferred_courts` order. Never falls back to non-preferred times.

## Scheduling

GitHub Actions has unpredictable queue delays and is **not suitable** for this time-critical task. Use one of the options below.

### Option 1: macOS launchd (requires Mac on and awake at 9 AM)

A plist is provided at `com.user.lifetime-reserve.plist`. Copy it to `~/Library/LaunchAgents/` and update the paths to match your machine, then:

```bash
launchctl load ~/Library/LaunchAgents/com.user.lifetime-reserve.plist

# Trigger manually
launchctl start com.user.lifetime-reserve

# Watch logs
tail -f reserve.log
```

### Option 2: VPS with cron (~$4/month, most reliable)

Any cheap VPS works (e.g. Hetzner CAX11 ~$4/mo). Cron fires within seconds of schedule.

```bash
timedatectl set-timezone America/New_York
apt update && apt install -y python3 python3-pip git
git clone https://github.com/mortimerliu/lifetime-reserve.git
cd lifetime-reserve && pip3 install requests
nano config.json   # paste your config

crontab -e
# Add:
0 9 * * * cd /root/lifetime-reserve && python3 reserve.py --auto >> reserve.log 2>&1
```

### Option 3: GitHub Actions (manual trigger only)

The schedule is disabled in `.github/workflows/reserve.yml` due to queue delays, but the workflow supports manual runs:

```bash
gh workflow run reserve.yml
gh run list --limit 1
```

The `CONFIG_JSON` secret in the repo contains your `config.json` contents.
