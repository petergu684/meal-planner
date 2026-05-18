#!/bin/bash
# Meal Planner — Daily reminder for tomorrow's dishes
# Sends directly to Discord via bot API

export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load .env if exists
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a; source "$SCRIPT_DIR/.env"; set +a
fi

PLAN_API="${MEAL_PLANNER_URL:-http://localhost:8091}/api/plan/tomorrow-reminder"

MESSAGE=$(curl -s "$PLAN_API" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('message') or '')
" 2>/dev/null)

if [ -z "$MESSAGE" ]; then
    exit 0
fi

# Send via Discord bot API
if [ -n "$DISCORD_BOT_TOKEN" ] && [ -n "$DISCORD_CHANNEL_ID" ]; then
    # Use curl directly (avoids token escaping issues in Python)
    PAYLOAD=$(python3 -c "import json,sys; print(json.dumps({'content': sys.argv[1]}))" "$MESSAGE")
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
        -X POST "https://discord.com/api/v10/channels/${DISCORD_CHANNEL_ID}/messages" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bot ${DISCORD_BOT_TOKEN}" \
        -d "$PAYLOAD")
    echo "$(date): Discord send HTTP $HTTP_CODE"
else
    echo "$(date): DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID not set"
fi

# Also write to file for any other consumers
REMINDER_FILE="${MEAL_PLANNER_DATA:-$SCRIPT_DIR/data}/pending_reminder.txt"
echo "$MESSAGE" > "$REMINDER_FILE"
