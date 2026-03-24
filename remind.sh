#!/bin/bash
# Meal Planner — Daily reminder for tomorrow's dishes via WeChat
# Configuration via environment variables (set in .env or systemd)

export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load .env if exists
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a; source "$SCRIPT_DIR/.env"; set +a
fi

PLAN_API="${MEAL_PLANNER_URL:-http://localhost:8091}/api/plan/tomorrow-reminder"
WECHAT_TO="${WECHAT_TARGET:-}"
WECHAT_ACCOUNT="${WECHAT_ACCOUNT_FILE:-}"

MESSAGE=$(curl -s "$PLAN_API" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('message') or '')
" 2>/dev/null)

if [ -z "$MESSAGE" ]; then
    exit 0
fi

if [ -z "$WECHAT_TO" ] || [ -z "$WECHAT_ACCOUNT" ]; then
    echo "WECHAT_TARGET or WECHAT_ACCOUNT_FILE not set. Printing to stdout:"
    echo "$MESSAGE"
    exit 0
fi

python3 - "$WECHAT_ACCOUNT" "$WECHAT_TO" "$MESSAGE" << 'PYEOF'
import sys, json, uuid, random, base64
import urllib.request

account_file = sys.argv[1]
to = sys.argv[2]
text = sys.argv[3]

with open(account_file) as f:
    acct = json.load(f)
token = acct["token"]
base_url = acct.get("baseUrl", "https://ilinkai.weixin.qq.com")

body = {
    "msg": {
        "from_user_id": "",
        "to_user_id": to,
        "client_id": str(uuid.uuid4()),
        "message_type": 2,
        "message_state": 2,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    },
    "base_info": {"channel_version": "1.0.0"},
}

payload = json.dumps(body).encode("utf-8")
uin = base64.b64encode(str(random.randint(0, 2**32-1)).encode()).decode()

req = urllib.request.Request(
    f"{base_url}/ilink/bot/sendmessage",
    data=payload,
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": uin,
    },
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        print(f"WeChat send OK: {resp.status}")
except Exception as e:
    print(f"WeChat send failed: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
