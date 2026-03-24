# 🍽️ Meal Planner

A self-hosted weekly meal planning and grocery shopping web app. Built as a single-file Python server (FastAPI) with a mobile-first UI.

## Features

- **Dish Library** — Add dishes with ingredients (per-person amounts), photos, notes, and tags. Sorted by Pinyin for Chinese dish names.
- **Weekly Planner** — Drag-and-drop meal planning (lunch & dinner, Mon–Sun). Real-time sync via SSE when multiple people edit simultaneously.
- **Grocery List** — Auto-generated from the week's plan, with aggregated ingredients. Checkable, synced in real-time across devices.
- **Meal Plan Preview** — Printable weekly table. Export as image or print.
- **Guest Menu** — A separate shareable page where friends can browse your dishes and add to a shared cart before visiting. Isolated from admin pages.
- **Tag System** — Organize dishes by tags, filter in all views. Control which tags appear on the guest menu.
- **Note Images** — Attach reference photos to dishes (recipe screenshots, plating ideas). Swipe through in enlarged view.
- **Daily Reminders** — Optional nightly notification with tomorrow's dishes and defrost reminders. Includes a WeChat bot notification script (requires WeChat bot credentials).
- **Auto Backup** — Database backed up every 30 minutes (only when changed), with rotation.

## Requirements

- Python 3.10+

## Quick Start

```bash
# 1. Install dependencies
pip install fastapi uvicorn aiofiles aiosqlite python-multipart Pillow pypinyin

# 2. Copy and edit config
cp .env.example .env
# Edit .env with your preferred data directory and port

# 3. Run
python server.py
# → http://localhost:8091
```

The guest menu is at `/menu`.

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
| `WECHAT_TARGET` | *(none)* | WeChat user ID for daily reminders (optional) |
| `WECHAT_ACCOUNT_FILE` | *(none)* | Path to WeChat account credentials (optional) |

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

Enable everything:

```bash
systemctl --user daemon-reload
systemctl --user enable --now meal-planner.service
systemctl --user enable --now meal-planner-backup.timer
systemctl --user enable --now meal-reminder.timer  # optional, needs WeChat config
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

## License

MIT
