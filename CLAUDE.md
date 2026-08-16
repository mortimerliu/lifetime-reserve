# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the script

```bash
.venv/bin/python reserve.py                                    # interactive: pick date, time, court
.venv/bin/python reserve.py --auto                             # auto-book day 8 only (default)
.venv/bin/python reserve.py --auto --fallback                  # auto-book day 8, then scan days 1–7
.venv/bin/python reserve.py --auto --wait-until 09:00:00       # login early, book at 9AM sharp
.venv/bin/python reserve.py --dry-run                          # show available slots, no booking
.venv/bin/python reserve.py --slot "2026-03-16 04:30"          # book a specific slot directly (24h)
.venv/bin/python reserve.py --cancel 2026-08-03                # cancel the reservation on a date
.venv/bin/python reserve.py --list                            # list current upcoming reservations
```

## Running tests

```bash
.venv/bin/pip install -r requirements-dev.txt   # one-time: pytest (+ requests)
.venv/bin/python -m pytest -q                    # run the suite
```

Tests are pure/mocked — no network, no `config.json`, no secrets. CI runs the same
command on push/PR (`.github/workflows/tests.yml`). Dev deps are never installed on the
VPS (it uses `requirements.txt` only).

## Architecture

The code is a package, `lifetime_reserve/`, with two thin root shims (`reserve.py`,
`server.py`) kept so cron / launchd / systemd / the Slack subprocess keep invoking
`python3 reserve.py ...` / `python3 server.py` unchanged. Running from the repo root
puts the package on the path — no install step, VPS deploy stays a plain `git pull`.

```
reserve.py / server.py        # 2-line entry shims → lifetime_reserve.cli / .slackbot.server
lifetime_reserve/
├── config.py                 # typed frozen Config dataclass, load_config, ConfigError (defaults live here)
├── protocol.py               # <<<REPORT>>> markers (engine↔slackbot stdout contract)
├── slots.py                  # PURE: collect_slots, rank_slots, auto_pick, pick_by_time, to_api_time, fmt_slots
├── reports.py                # PURE: build_*_report, emit_report
├── notify.py                 # Slack posting: post_message/update_message (explicit channel) + notify()
├── api/{errors,client}.py    # LifetimeClient — session + auth + all API endpoints
├── modes.py                  # run_auto/run_date/run_slot/run_cancel/run_dry_run/run_interactive
├── cli.py                    # parse_args, login-retry, wait-until, dispatch, main (the only sys.exit layer)
└── slackbot/
    ├── parsing.py            # PURE: parse_date_token, parse_command_text, strip_mention
    ├── logparse.py           # PURE: extract_report, extract_log_lines, truncate
    ├── verify.py             # HMAC signature verification
    ├── dispatch.py           # subprocess runner + Slack reply builders (run_and_report_threaded)
    ├── events.py             # Events API: dedup (thread-safe), should_process_event, handle_event
    └── server.py             # HTTP handler: /events (Events API)
tests/                        # pytest suite (FakeSession / FakeClient; no network) — see "Running tests"
```

Config handlers take a `LifetimeClient` + `Config`; the mode handlers raise
`ValueError`/`ConfigError` on bad input and `cli.main` decides process exit.

**Slack transport.** The Events API (`/events`) is the sole Slack interface: threaded
replies for channel `@mention` + DM, deployed and live in production. Commands: `reserve`
(optionally with a date / `date HH:MM`), `cancel <date>`, `list`, plus a `verbose`
modifier — parsed by `parsing.parse_command_text` / `events.build_command`. The old slash
commands (`/reserve`, `/cancel`, `/list`) were removed in favor of the Events API.

**API layer** (`api/client.py`) — endpoints under `https://api.lifetimefitness.com`:
- `POST /auth/v2/login` → returns `token` (JWE, used as `x-ltf-jwe`) and `ssoId` (used as `x-ltf-ssoid`)
- `GET /ux/web-schedules/v2/resources/booking/search` → available court slots for a date
- `POST /sys/registrations/V3/ux/resource` → creates a booking (`regStatus: pending`)
- `PUT /sys/registrations/V3/ux/resource/{regId}/complete` → accepts waiver, moves booking to `completed` (required — pending bookings don't appear in the reservations list)
- `GET /ux/web-schedules/v3/reservations` → existing reservations (to skip already-booked dates)

Every API request requires the `ocp-apim-subscription-key` header (hardcoded) plus the two auth headers from login.

**Auto booking logic** (`modes.run_auto()`):
1. Search day 8 **once** at 9 AM sharp, then retry **only the booking step** up to `retry_count` times:
   - On 5xx (server overload): retry booking the same slot — avoids releasing the slot between attempts — after waiting `retry_delay_seconds`
   - On 4xx (slot lost): re-search, then take the next-best preferred slot **immediately, with no backoff** — the slot in hand is freshly searched and the 9 AM list drains within seconds, so sleeping here just hands it to someone else. A slot that returned 4xx is added to a `tried` set and never attempted again, so a stale search result can't send the loop back into the same failure
2. If day 8 yields no booking, exit by default. Pass `--fallback` to instead fetch existing reservations for days 1–(N-1) and scan in order, skipping already-booked days. Each day is tried once; errors on individual days are caught and skipped rather than aborting the scan.
3. `rank_slots()` orders every slot at a preferred time (time preference first, then court preference); `auto_pick()` is its first element, or `None` when no preferred time is available (never falls back to arbitrary slots). `modes._book_best_available()` walks that ranking — up to `MAX_BOOKING_ATTEMPTS` — so one lost race doesn't sink the whole date. Used by `run_date`, `run_slot` (narrowed to the one requested time, all courts) and the `--fallback` scan.

**Error handling**: `raise_for_status_with_body()` wraps `raise_for_status()` to include the API response body in exception messages. A rejected `/complete` raises `BookingIncompleteError` (a `requests.HTTPError` subclass carrying `reg_id`): the registration stays pending and is **not** a reservation, so callers must report failure and move to another slot. Returning the pending booking instead — as the code did until 2026-08-16 — made every caller report a booking that did not exist (three silently lost days: 08-02, 08-05, 08-16). Retrying after that rejection cannot double-book: **Lifetime allows one booking per profile per day** and rejects the second itself. Don't add a verification lookup to the recovery path — a single-date `get_reservations` costs ~600ms (vs ~340ms for the re-search), and slots drain within seconds of 9 AM.

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

## Syncing code to the VPS

**Always sync via git — never `scp`.** Push local changes to `origin/main`, then `git pull` on the VPS. This keeps a single source of truth and an auditable history, and avoids drift between machines. (`scp` was used previously only because `main` was branch-protected on the remote; it no longer is.)

```bash
# Local
git add -p && git commit -m "..." && git push

# On the VPS
cd /root/lifetime-reserve && git pull
```

If the pull changed Slack-server code (`server.py` or `lifetime_reserve/slackbot/`), restart the bot so it picks up the change:

```bash
systemctl restart reserve-server.service   # the Slack server runs as a systemd service on the VPS
```

The booking cron invokes `reserve.py` fresh each run, so it needs no restart.

**Never commit sensitive info.** Secrets and machine-specific details stay out of git and are delivered out-of-band via `scp`. Gitignored, never pushed:

- `config.json` — Lifetime credentials, Slack bot token, signing secret
- `.vps_env` — VPS host IP and SSH key path (see `.vps_env.example` for the template; `check_vps_log.sh` sources it — used locally only, not needed on the VPS)
- `.claude/settings.local.json` — personal Claude Code permissions

Because these are gitignored, `git pull` on the VPS will **not** deliver them. When syncing, `scp` any changed config/secret files separately, e.g.:

```bash
scp -i ~/.ssh/hetzner_lifetime_reserve config.json root@<VPS_IP>:/root/lifetime-reserve/config.json
```

Before committing, scan the diff for tokens, passwords, keys, host IPs, and SSH details. If something sensitive was already pushed, **rotate it** rather than relying on a history rewrite — on a public repo, rewriting history does not truly un-publish (caches, forks, commits reachable by SHA remain).

## Scheduling options

GitHub Actions cron has unpredictable queue delays (minutes) and is **not suitable** for this time-critical task. Use one of the options below instead.

**Current setup:** the daily booking runs on the VPS via cron (Option 2), and the Slack server runs there as a systemd service (`reserve-server.service`). Option 1 (launchd) is documented as an alternative but is not currently in use.

### Option 1: macOS launchd (alternative) — requires MacBook on and awake at 9 AM

Plist installed at `~/Library/LaunchAgents/com.user.lifetime-reserve.plist`. Logs go to `logs/YYYY-MM-DD.log` (one file per day).

```bash
# Install / reload after editing the plist
launchctl unload ~/Library/LaunchAgents/com.user.lifetime-reserve.plist
launchctl load ~/Library/LaunchAgents/com.user.lifetime-reserve.plist

# Trigger manually
launchctl start com.user.lifetime-reserve

# Watch today's log
tail -f logs/$(date +%Y-%m-%d).log
```

The plist points directly to `.venv/bin/python` — no activation needed.

### Option 2: VPS with cron (current, ~$4/month, most reliable)

Any cheap VPS (Hetzner CX22, DigitalOcean Droplet). Cron fires within seconds of schedule.

```bash
sudo timedatectl set-timezone America/New_York
sudo apt install python3 python3-pip git -y
git clone https://github.com/HongruLiu-personal/lifetime-reserve.git
cd lifetime-reserve && pip3 install requests
nano config.json   # paste your config

crontab -e
# Add:
55 8 * * * cd /root/lifetime-reserve && python3 reserve.py --auto --wait-until 09:00:00
```

System timezone handles DST automatically.

Daily log files in `logs/` are self-rotating (one file per day). Clean up old files with `find logs/ -mtime +30 -delete`. Check VPS logs remotely using `check_vps_log.sh`:

```bash
./check_vps_log.sh              # today's log (default)
./check_vps_log.sh 2026-03-24   # specific date
./check_vps_log.sh follow       # live stream today
./check_vps_log.sh ls           # list log files
./check_vps_log.sh all          # all logs concatenated
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
