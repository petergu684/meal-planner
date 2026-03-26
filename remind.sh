#!/bin/bash
# Meal Planner — Daily reminder for tomorrow's dishes
# Fetches the reminder and writes it to a file for the main agent to pick up

export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load .env if exists
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a; source "$SCRIPT_DIR/.env"; set +a
fi

PLAN_API="${MEAL_PLANNER_URL:-http://localhost:8091}/api/plan/tomorrow-reminder"
REMINDER_FILE="${MEAL_PLANNER_DATA:-$SCRIPT_DIR/data}/pending_reminder.txt"

MESSAGE=$(curl -s "$PLAN_API" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('message') or '')
" 2>/dev/null)

if [ -z "$MESSAGE" ]; then
    exit 0
fi

# Write to pending file — the main agent picks this up on next heartbeat
echo "$MESSAGE" > "$REMINDER_FILE"
echo "$(date): Reminder written to $REMINDER_FILE"
