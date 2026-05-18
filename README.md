# 🍽️ Meal Planner

A self-hosted weekly meal planning and grocery shopping web app. Built as a single-file Python server (FastAPI) with a mobile-first UI.

> 💡 **Companion project:** Pair this with **[eink-meal-display](https://github.com/petergu684/meal-planner-eink-display)** to put your weekly plan on a battery-powered e-ink screen on your fridge. The display reads this app's SQLite database directly — no extra integration needed beyond pointing it at `data/meal_planner.db`.

## Features

- **Dish Library** — Add dishes with ingredients (per-person amounts), photos, notes, and tags. Sorted by Pinyin for Chinese dish names.
- **Weekly Planner** — Drag-and-drop meal planning (lunch & dinner, Mon–Sun). Real-time sync via SSE when multiple people edit simultaneously.
- **Grocery List** — Auto-generated from the week's plan, with aggregated ingredients. Checkable, synced in real-time across devices.
- **Meal Plan Preview** — Printable weekly table. Export as image or print.
- **Guest Menu** — A separate shareable page where friends can browse your dishes and add to a shared cart before visiting. Isolated from admin pages.
- **Tag System** — Organize dishes by tags, filter in all views. Control which tags appear on the guest menu.
- **Note Images** — Attach reference photos to dishes (recipe screenshots, plating ideas). Swipe through in enlarged view.
- **Daily Reminders** — Optional nightly notification with tomorrow's dishes and defrost reminders. Posts to Discord via a bot (requires a bot token and channel ID).
- **Auto Backup** — Database backed up every 30 minutes (only when changed), with rotation.

## Requirements

- Python 3.10+

## Quick Start

```bash
# 1. Install dependencies
pip install fastapi uvicorn aiofiles aiosqlite python-multipart Pillow pypinyin

# 2. Copy and edit config
cp .env.example .env
# At a minimum, set MEAL_PLANNER_ADMIN_PATH to a hard-to-guess value
# (this is the only gate protecting the admin UI).

# 3. Run
set -a; source .env; set +a   # export vars for this shell
python server.py
# → http://localhost:8091
```

URLs:

- **Admin** (dish library, planner, grocery): `http://localhost:8091<MEAL_PLANNER_ADMIN_PATH>`
- **Guest menu** (shareable, read-only browse + cart): `http://localhost:8091/menu`
- The root path `/` returns an empty page on purpose, so the admin URL stays unadvertised.

> ⚠️ **The admin path is your only access control.** Anyone who knows it can edit everything. Pick a long, random value (e.g. `/admin-9f3k2x`) and treat it like a password. Do **not** commit your real `.env`.

## Project Structure

```
meal-planner/
├── server.py          # Single-file app (FastAPI + inline HTML/CSS/JS)
├── backup.sh          # Database backup script (called by systemd timer)
├── remind.sh          # Daily reminder script (called by systemd timer)
├── .env               # Local configuration (gitignored)
├── .env.example       # Configuration template
├── .gitignore
├── README.md
└── data/              # All runtime data (gitignored)
    ├── meal_planner.db    # SQLite database
    ├── uploads/           # Dish photos and note images
    └── backups/           # Rotating database backups
```

## Configuration

All configuration is via environment variables (set in `.env` or export directly):

| Variable | Default | Description |
|----------|---------|-------------|
| `MEAL_PLANNER_DATA` | `./data` | Directory for database, uploads, and backups |
| `MEAL_PLANNER_PORT` | `8091` | Server port |
| `MEAL_PLANNER_ADMIN_PATH` | `/admin` | Secret URL path for the admin SPA — change this |
| `MEAL_PLANNER_URL` | `http://localhost:8091` | Used by `remind.sh` to reach the running server |
| `DISCORD_BOT_TOKEN` | *(none)* | Bot token for the daily reminder (optional) |
| `DISCORD_CHANNEL_ID` | *(none)* | Channel ID the reminder posts to (optional) |

### Setting up the Discord reminder (optional)

1. Create an application at <https://discord.com/developers/applications>, add a Bot, and copy the **Bot Token**.
2. Under **OAuth2 → URL Generator**, pick the `bot` scope and the `Send Messages` permission, then open the generated URL to invite the bot to your server.
3. In Discord, enable **Settings → Advanced → Developer Mode**, then right-click your target channel → **Copy Channel ID**.
4. Put both values into `.env` as `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID`.
5. Test it: `./remind.sh` — you should see an HTTP `200` line and a message in the channel.

## Systemd Setup (Auto-start)

### Main Server

```ini
# ~/.config/systemd/user/meal-planner.service
[Unit]
Description=Meal Planner
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/meal-planner
EnvironmentFile=/path/to/meal-planner/.env
ExecStart=/usr/bin/python3 /path/to/meal-planner/server.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
```

### Backup Timer (every 30 min)

```ini
# ~/.config/systemd/user/meal-planner-backup.timer
[Unit]
Description=Meal Planner Backup Timer

[Timer]
OnBootSec=5min
OnUnitActiveSec=30min
Persistent=true

[Install]
WantedBy=timers.target
```

```ini
# ~/.config/systemd/user/meal-planner-backup.service
[Unit]
Description=Meal Planner Backup

[Service]
Type=oneshot
EnvironmentFile=/path/to/meal-planner/.env
ExecStart=/path/to/meal-planner/backup.sh
```

### Daily Reminder Timer (9PM)

```ini
# ~/.config/systemd/user/meal-reminder.timer
[Unit]
Description=Meal Planner Daily Reminder

[Timer]
OnCalendar=*-*-* 21:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

```ini
# ~/.config/systemd/user/meal-reminder.service
[Unit]
Description=Meal Planner Daily Reminder

[Service]
Type=oneshot
EnvironmentFile=/path/to/meal-planner/.env
ExecStart=/path/to/meal-planner/remind.sh
```

Enable everything:

```bash
systemctl --user daemon-reload
systemctl --user enable --now meal-planner.service
systemctl --user enable --now meal-planner-backup.timer
systemctl --user enable --now meal-reminder.timer  # optional, needs Discord config
# If using user-level systemd without a login session, also run:
#   loginctl enable-linger $USER
# so timers keep firing after you log out.
```

## Smart Defaults

- **Weekly planner** defaults to this week (Mon–Fri) or next week (Sat–Sun)
- **Guest menu** URL is shareable — guests see dishes but can't access admin
- **Grocery checklist** syncs across all devices in real-time
- **Defrost detection** scans tomorrow's ingredients and flags proteins/seafood

## API

All endpoints are at `/api/`:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/dishes` | GET | List all dishes |
| `/api/dishes` | POST | Create a dish |
| `/api/dishes/:id` | GET/PUT/DELETE | CRUD a dish |
| `/api/tags` | GET | List all tags |
| `/api/tags/:id/visibility` | PUT | Toggle tag visibility in guest menu |
| `/api/plan?week=YYYY-MM-DD` | GET | Get week's meal plan |
| `/api/plan` | POST | Add dish to plan |
| `/api/plan/:id` | PUT/DELETE | Update/remove planned dish |
| `/api/plan/grocery?week=...` | GET | Aggregated grocery list |
| `/api/plan/tomorrow-reminder` | GET | Tomorrow's reminder message (Chinese) |
| `/api/cart` | GET | Shared guest cart |
| `/api/cart/update` | POST | Update shared cart |
| `/api/grocery/check` | POST | Toggle grocery checklist item |

## E-Ink Display Integration

You can mirror the current week's meal plan onto a battery-powered e-paper display on your fridge using the companion repo **[eink-meal-display](https://github.com/wenhao-anthropic/eink-meal-display)**. It runs an HTTP image server alongside this one — the e-paper device wakes once a day, pulls a rendered PNG of the plan, and goes back to deep sleep.

The integration is just a shared SQLite file. After setting up meal-planner:

```bash
# In the eink-meal-display repo, point its image server at this DB:
export MEAL_PLANNER_DB=/absolute/path/to/meal-planner/data/meal_planner.db
python3 eink-meal-display/sender/image_server.py
```

SQLite handles concurrent readers + a single writer correctly, so both servers can run side-by-side without contention. See the [eink-meal-display README](https://github.com/wenhao-anthropic/eink-meal-display#readme) for hardware details and ESP32 firmware setup.

## License

MIT
