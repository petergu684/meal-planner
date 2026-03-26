#!/usr/bin/env python3
"""
Meal Planner — Grocery Shopping Planning Web App
Single-file FastAPI application with inline HTML/CSS/JS SPA.
"""

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Optional

import aiosqlite
from pypinyin import lazy_pinyin, Style
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("MEAL_PLANNER_DATA", str(BASE_DIR / "data")))
DB_PATH = DATA_DIR / "meal_planner.db"
UPLOADS_DIR = DATA_DIR / "uploads"
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(exist_ok=True)
MAX_IMAGE_DIM = 800

# ---------------------------------------------------------------------------
# SSE hub — lightweight pub/sub per week
# ---------------------------------------------------------------------------
class SSEHub:
    def __init__(self):
        self._subscribers: dict[str, list[asyncio.Queue]] = {}

    def subscribe(self, week: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(week, []).append(q)
        return q

    def unsubscribe(self, week: str, q: asyncio.Queue):
        if week in self._subscribers:
            self._subscribers[week] = [x for x in self._subscribers[week] if x is not q]
            if not self._subscribers[week]:
                del self._subscribers[week]

    async def publish(self, week: str, data: dict):
        payload = json.dumps(data)
        for q in self._subscribers.get(week, []):
            await q.put(payload)

sse_hub = SSEHub()

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    db = await get_db()
    try:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS dish (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                notes TEXT,
                image_path TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS ingredient (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dish_id INTEGER NOT NULL REFERENCES dish(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                amount REAL NOT NULL,
                unit TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tag (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                visible_in_menu INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS dish_tag (
                dish_id INTEGER NOT NULL REFERENCES dish(id) ON DELETE CASCADE,
                tag_id INTEGER NOT NULL REFERENCES tag(id) ON DELETE CASCADE,
                PRIMARY KEY (dish_id, tag_id)
            );
            CREATE TABLE IF NOT EXISTS note_image (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dish_id INTEGER NOT NULL REFERENCES dish(id) ON DELETE CASCADE,
                image_path TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS meal_plan (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                week_start DATE NOT NULL,
                day_of_week INTEGER NOT NULL,
                meal_type TEXT NOT NULL CHECK(meal_type IN ('lunch','dinner')),
                dish_id INTEGER NOT NULL REFERENCES dish(id) ON DELETE CASCADE,
                servings INTEGER NOT NULL DEFAULT 2,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(week_start, day_of_week, meal_type, dish_id)
            );
        """)
        # Migration: add visible_in_menu column if missing
        try:
            await db.execute("ALTER TABLE tag ADD COLUMN visible_in_menu INTEGER NOT NULL DEFAULT 1")
            await db.commit()
        except Exception:
            pass  # column already exists
        await db.commit()
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(lifespan=lifespan)

# Serve uploaded images
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")


# ---------------------------------------------------------------------------
# Utility: resize image
# ---------------------------------------------------------------------------
def resize_image(data: bytes, max_dim: int = MAX_IMAGE_DIM) -> bytes:
    from PIL import ImageOps
    img = Image.open(BytesIO(data))
    img = ImageOps.exif_transpose(img)  # Apply EXIF rotation (iPhone photos)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_dim:
        ratio = max_dim / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


# ===================================================================
# API: Dishes
# ===================================================================

def pinyin_sort_key(name: str) -> str:
    """Generate a sortable pinyin key for Chinese text."""
    return "".join(lazy_pinyin(name, style=Style.TONE3)).lower()


@app.get("/api/dishes")
async def list_dishes():
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT id, name, notes, image_path, created_at, updated_at FROM dish"
        )
        dishes = []
        for r in rows:
            d = dict(r)
            ings = await db.execute_fetchall(
                "SELECT id, name, amount, unit FROM ingredient WHERE dish_id=?", (d["id"],)
            )
            d["ingredients"] = [dict(i) for i in ings]
            tags = await db.execute_fetchall(
                "SELECT t.id, t.name FROM tag t JOIN dish_tag dt ON t.id=dt.tag_id WHERE dt.dish_id=?",
                (d["id"],)
            )
            d["tags"] = [dict(t) for t in tags]
            note_imgs = await db.execute_fetchall(
                "SELECT id, image_path FROM note_image WHERE dish_id=? ORDER BY id", (d["id"],))
            d["note_images"] = [dict(ni) for ni in note_imgs]
            d["pinyin"] = pinyin_sort_key(d["name"])
            dishes.append(d)
        # Sort by pinyin
        dishes.sort(key=lambda x: x["pinyin"])
        return dishes
    finally:
        await db.close()


@app.get("/api/dishes/{dish_id}")
async def get_dish(dish_id: int):
    db = await get_db()
    try:
        row = await db.execute_fetchall(
            "SELECT id, name, notes, image_path, created_at, updated_at FROM dish WHERE id=?",
            (dish_id,),
        )
        if not row:
            raise HTTPException(404, "Dish not found")
        d = dict(row[0])
        ings = await db.execute_fetchall(
            "SELECT id, name, amount, unit FROM ingredient WHERE dish_id=?", (dish_id,)
        )
        d["ingredients"] = [dict(i) for i in ings]
        tags = await db.execute_fetchall(
            "SELECT t.id, t.name FROM tag t JOIN dish_tag dt ON t.id=dt.tag_id WHERE dt.dish_id=?",
            (dish_id,)
        )
        d["tags"] = [dict(t) for t in tags]
        note_imgs = await db.execute_fetchall(
            "SELECT id, image_path FROM note_image WHERE dish_id=? ORDER BY id", (dish_id,))
        d["note_images"] = [dict(ni) for ni in note_imgs]
        d["pinyin"] = pinyin_sort_key(d["name"])
        return d
    finally:
        await db.close()


@app.post("/api/dishes")
async def create_dish(
    name: str = Form(...),
    notes: str = Form(""),
    ingredients: str = Form("[]"),
    tags: str = Form("[]"),
    image: Optional[UploadFile] = File(None),
):
    image_path = None
    if image and image.filename:
        data = await image.read()
        data = resize_image(data)
        fname = f"{uuid.uuid4().hex}.jpg"
        (UPLOADS_DIR / fname).write_bytes(data)
        image_path = f"uploads/{fname}"

    ingredient_list = json.loads(ingredients)
    tag_names = json.loads(tags)

    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO dish (name, notes, image_path) VALUES (?, ?, ?)",
            (name.strip(), notes.strip() or None, image_path),
        )
        dish_id = cur.lastrowid
        for ing in ingredient_list:
            await db.execute(
                "INSERT INTO ingredient (dish_id, name, amount, unit) VALUES (?, ?, ?, ?)",
                (dish_id, ing["name"], float(ing["amount"]), ing["unit"]),
            )
        for tag_name in tag_names:
            tag_name = tag_name.strip()
            if not tag_name:
                continue
            await db.execute("INSERT OR IGNORE INTO tag (name) VALUES (?)", (tag_name,))
            row = await db.execute_fetchall("SELECT id FROM tag WHERE name=?", (tag_name,))
            if row:
                await db.execute("INSERT OR IGNORE INTO dish_tag (dish_id, tag_id) VALUES (?, ?)",
                                 (dish_id, row[0]["id"]))
        await db.commit()
        return await get_dish(dish_id)
    except aiosqlite.IntegrityError:
        raise HTTPException(409, "A dish with that name already exists")
    finally:
        await db.close()


@app.put("/api/dishes/{dish_id}")
async def update_dish(
    dish_id: int,
    name: str = Form(...),
    notes: str = Form(""),
    ingredients: str = Form("[]"),
    tags: str = Form("[]"),
    image: Optional[UploadFile] = File(None),
    remove_image: str = Form("false"),
):
    db = await get_db()
    try:
        existing = await db.execute_fetchall("SELECT * FROM dish WHERE id=?", (dish_id,))
        if not existing:
            raise HTTPException(404, "Dish not found")
        existing = dict(existing[0])

        image_path = existing["image_path"]

        if remove_image == "true" and image_path:
            p = DATA_DIR / image_path
            if p.exists():
                p.unlink()
            image_path = None

        if image and image.filename:
            if existing["image_path"]:
                p = DATA_DIR / existing["image_path"]
                if p.exists():
                    p.unlink()
            data = await image.read()
            data = resize_image(data)
            fname = f"{uuid.uuid4().hex}.jpg"
            (UPLOADS_DIR / fname).write_bytes(data)
            image_path = f"uploads/{fname}"

        await db.execute(
            "UPDATE dish SET name=?, notes=?, image_path=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (name.strip(), notes.strip() or None, image_path, dish_id),
        )

        # Replace ingredients
        await db.execute("DELETE FROM ingredient WHERE dish_id=?", (dish_id,))
        ingredient_list = json.loads(ingredients)
        for ing in ingredient_list:
            await db.execute(
                "INSERT INTO ingredient (dish_id, name, amount, unit) VALUES (?, ?, ?, ?)",
                (dish_id, ing["name"], float(ing["amount"]), ing["unit"]),
            )

        # Replace tags
        await db.execute("DELETE FROM dish_tag WHERE dish_id=?", (dish_id,))
        tag_names = json.loads(tags)
        for tag_name in tag_names:
            tag_name = tag_name.strip()
            if not tag_name:
                continue
            await db.execute("INSERT OR IGNORE INTO tag (name) VALUES (?)", (tag_name,))
            row = await db.execute_fetchall("SELECT id FROM tag WHERE name=?", (tag_name,))
            if row:
                await db.execute("INSERT OR IGNORE INTO dish_tag (dish_id, tag_id) VALUES (?, ?)",
                                 (dish_id, row[0]["id"]))

        await db.commit()
        return await get_dish(dish_id)
    except aiosqlite.IntegrityError:
        raise HTTPException(409, "A dish with that name already exists")
    finally:
        await db.close()


@app.delete("/api/dishes/{dish_id}")
async def delete_dish(dish_id: int):
    db = await get_db()
    try:
        existing = await db.execute_fetchall("SELECT image_path FROM dish WHERE id=?", (dish_id,))
        if not existing:
            raise HTTPException(404, "Dish not found")
        img = dict(existing[0]).get("image_path")
        if img:
            p = DATA_DIR / img
            if p.exists():
                p.unlink()
        await db.execute("DELETE FROM dish WHERE id=?", (dish_id,))
        await db.commit()
        return {"ok": True}
    finally:
        await db.close()


@app.get("/api/tags")
async def list_tags():
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT t.id, t.name, t.visible_in_menu, COUNT(dt.dish_id) as dish_count "
            "FROM tag t LEFT JOIN dish_tag dt ON t.id=dt.tag_id "
            "GROUP BY t.id ORDER BY t.name"
        )
        return [dict(r) for r in rows]
    finally:
        await db.close()


@app.put("/api/tags/{tag_id}/visibility")
async def toggle_tag_visibility(tag_id: int, request: Request):
    body = await request.json()
    visible = 1 if body.get("visible_in_menu", True) else 0
    db = await get_db()
    try:
        await db.execute("UPDATE tag SET visible_in_menu=? WHERE id=?", (visible, tag_id))
        await db.commit()
        return {"ok": True}
    finally:
        await db.close()


@app.delete("/api/tags/{tag_id}")
async def delete_tag(tag_id: int):
    db = await get_db()
    try:
        row = await db.execute_fetchall(
            "SELECT COUNT(*) as cnt FROM dish_tag WHERE tag_id=?", (tag_id,))
        if row and row[0]["cnt"] > 0:
            raise HTTPException(400, "Tag is still in use")
        await db.execute("DELETE FROM tag WHERE id=?", (tag_id,))
        await db.commit()
        return {"ok": True}
    finally:
        await db.close()


@app.post("/api/dishes/{dish_id}/image")
async def upload_dish_image(dish_id: int, image: UploadFile = File(...)):
    db = await get_db()
    try:
        existing = await db.execute_fetchall("SELECT image_path FROM dish WHERE id=?", (dish_id,))
        if not existing:
            raise HTTPException(404, "Dish not found")
        old_path = dict(existing[0]).get("image_path")
        if old_path:
            p = DATA_DIR / old_path
            if p.exists():
                p.unlink()
        data = await image.read()
        data = resize_image(data)
        fname = f"{uuid.uuid4().hex}.jpg"
        (UPLOADS_DIR / fname).write_bytes(data)
        image_path = f"uploads/{fname}"
        await db.execute(
            "UPDATE dish SET image_path=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (image_path, dish_id),
        )
        await db.commit()
        return {"image_path": image_path}
    finally:
        await db.close()


@app.post("/api/dishes/{dish_id}/note-images")
async def upload_note_image(dish_id: int, image: UploadFile = File(...)):
    db = await get_db()
    try:
        existing = await db.execute_fetchall("SELECT id FROM dish WHERE id=?", (dish_id,))
        if not existing:
            raise HTTPException(404, "Dish not found")
        data = await image.read()
        data = resize_image(data, max_dim=1200)  # larger for notes — need to read details
        fname = f"note_{uuid.uuid4().hex}.jpg"
        (UPLOADS_DIR / fname).write_bytes(data)
        image_path = f"uploads/{fname}"
        cur = await db.execute(
            "INSERT INTO note_image (dish_id, image_path) VALUES (?, ?)",
            (dish_id, image_path))
        await db.commit()
        return {"id": cur.lastrowid, "image_path": image_path}
    finally:
        await db.close()


@app.delete("/api/note-images/{image_id}")
async def delete_note_image(image_id: int):
    db = await get_db()
    try:
        row = await db.execute_fetchall("SELECT image_path FROM note_image WHERE id=?", (image_id,))
        if not row:
            raise HTTPException(404, "Image not found")
        img_path = dict(row[0])["image_path"]
        p = DATA_DIR / img_path
        if p.exists():
            p.unlink()
        await db.execute("DELETE FROM note_image WHERE id=?", (image_id,))
        await db.commit()
        return {"ok": True}
    finally:
        await db.close()


# ===================================================================
# API: Meal Plan
# ===================================================================

@app.get("/api/plan")
async def get_plan(week: str = Query(...)):
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            """SELECT mp.id, mp.week_start, mp.day_of_week, mp.meal_type,
                      mp.dish_id, mp.servings, mp.version,
                      d.name as dish_name, d.image_path as dish_image, d.notes as dish_notes
               FROM meal_plan mp
               JOIN dish d ON d.id = mp.dish_id
               WHERE mp.week_start = ?
               ORDER BY mp.day_of_week, mp.meal_type""",
            (week,),
        )
        return [dict(r) for r in rows]
    finally:
        await db.close()


@app.post("/api/plan")
async def add_to_plan(request: Request):
    body = await request.json()
    week_start = body["week_start"]
    day_of_week = int(body["day_of_week"])
    meal_type = body["meal_type"]
    dish_id = int(body["dish_id"])
    servings = int(body.get("servings", 2))

    db = await get_db()
    try:
        cur = await db.execute(
            """INSERT INTO meal_plan (week_start, day_of_week, meal_type, dish_id, servings)
               VALUES (?, ?, ?, ?, ?)""",
            (week_start, day_of_week, meal_type, dish_id, servings),
        )
        await db.commit()
        plan_id = cur.lastrowid
        row = await db.execute_fetchall(
            """SELECT mp.id, mp.week_start, mp.day_of_week, mp.meal_type,
                      mp.dish_id, mp.servings, mp.version,
                      d.name as dish_name, d.image_path as dish_image, d.notes as dish_notes
               FROM meal_plan mp JOIN dish d ON d.id=mp.dish_id
               WHERE mp.id=?""",
            (plan_id,),
        )
        result = dict(row[0])
        await sse_hub.publish(week_start, {"type": "plan_updated"})
        return result
    except aiosqlite.IntegrityError:
        raise HTTPException(409, "This dish is already planned for that slot")
    finally:
        await db.close()


@app.put("/api/plan/{plan_id}")
async def update_plan(plan_id: int, request: Request):
    body = await request.json()
    servings = int(body["servings"])
    expected_version = body.get("version")

    db = await get_db()
    try:
        existing = await db.execute_fetchall(
            "SELECT version, week_start FROM meal_plan WHERE id=?", (plan_id,)
        )
        if not existing:
            raise HTTPException(404, "Plan entry not found")
        current = dict(existing[0])
        if expected_version is not None and current["version"] != expected_version:
            raise HTTPException(409, "Conflict: plan was modified by another user")

        await db.execute(
            "UPDATE meal_plan SET servings=?, version=version+1 WHERE id=?",
            (servings, plan_id),
        )
        await db.commit()
        await sse_hub.publish(current["week_start"], {"type": "plan_updated"})
        return {"ok": True}
    finally:
        await db.close()


@app.delete("/api/plan/{plan_id}")
async def delete_from_plan(plan_id: int):
    db = await get_db()
    try:
        existing = await db.execute_fetchall(
            "SELECT week_start FROM meal_plan WHERE id=?", (plan_id,)
        )
        if not existing:
            raise HTTPException(404, "Plan entry not found")
        week = dict(existing[0])["week_start"]
        await db.execute("DELETE FROM meal_plan WHERE id=?", (plan_id,))
        await db.commit()
        await sse_hub.publish(week, {"type": "plan_updated"})
        return {"ok": True}
    finally:
        await db.close()


@app.get("/api/plan/grocery")
async def get_grocery_list(week: str = Query(...)):
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            """SELECT i.name, i.amount, i.unit, mp.servings
               FROM meal_plan mp
               JOIN ingredient i ON i.dish_id = mp.dish_id
               WHERE mp.week_start = ?""",
            (week,),
        )
        # Aggregate: key=(lower name, unit)
        agg: dict[tuple[str, str], dict] = {}
        for r in rows:
            r = dict(r)
            key = (r["name"].lower().strip(), r["unit"].lower().strip())
            if key not in agg:
                agg[key] = {"name": r["name"].strip(), "unit": r["unit"], "amount": 0.0}
            # amount is per person, multiply by servings
            agg[key]["amount"] += r["amount"] * r["servings"]

        items = sorted(agg.values(), key=lambda x: x["name"].lower())
        # Round amounts nicely
        for item in items:
            amt = item["amount"]
            if amt == int(amt):
                item["amount"] = int(amt)
            else:
                item["amount"] = round(amt, 2)
        return items
    finally:
        await db.close()


# ===================================================================
# Tomorrow's meal reminder API
# ===================================================================
# Common ingredients that typically need defrosting
# Ingredients that typically need defrosting (frozen proteins/seafood)
DEFROST_KEYWORDS = [
    "肉", "排骨", "里脊", "五花", "肉沫", "肉末", "肉片", "肉丝",
    "鸡胸", "鸡腿", "鸡翅", "鸡块", "鸡肉", "鸭",
    "虾", "虾仁", "鱿鱼", "蟹", "带鱼", "三文鱼", "鳕鱼", "鱼片", "鱼",
    "牛腩", "肥牛", "牛排", "牛肉",
    "猪蹄", "猪肉",
    "羊肉", "羊排",
    "培根", "火腿", "香肠", "腊肉", "叉烧",
]
# Things that contain defrost keywords but should NOT trigger defrost
DEFROST_EXCLUDE = ["鸡蛋", "蛋", "鱼露", "鱼豆腐", "肉桂", "肉松", "鸡精", "鸡粉",
                   "虾皮", "虾酱", "虾油", "鱼香", "酱油"]

def needs_defrost(ingredient_name: str) -> bool:
    name = ingredient_name.strip()
    # Check exclusions first
    for ex in DEFROST_EXCLUDE:
        if ex in name:
            return False
    for kw in DEFROST_KEYWORDS:
        if kw in name:
            return True
    return False


@app.get("/api/plan/tomorrow-reminder")
async def tomorrow_reminder():
    """Generate a Chinese reminder message for tomorrow's meals."""
    tomorrow = date.today() + timedelta(days=1)
    dow = tomorrow.weekday()  # Monday=0
    monday = tomorrow - timedelta(days=dow)
    week_str = monday.isoformat()

    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            """SELECT mp.meal_type, d.name as dish_name, mp.servings, mp.dish_id
               FROM meal_plan mp JOIN dish d ON d.id=mp.dish_id
               WHERE mp.week_start=? AND mp.day_of_week=?
               ORDER BY mp.meal_type""",
            (week_str, dow),
        )
        if not rows:
            return {"message": None, "reason": "no meals planned"}

        meals = [dict(r) for r in rows]

        # Collect defrost items
        defrost_items = set()
        for m in meals:
            ings = await db.execute_fetchall(
                "SELECT name FROM ingredient WHERE dish_id=?", (m["dish_id"],))
            for ing in ings:
                if needs_defrost(ing["name"]):
                    defrost_items.add(ing["name"])

        # Build Chinese message
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        day_label = weekdays[dow]
        date_str = f"{tomorrow.month}月{tomorrow.day}日"

        lines = [f"🍽️ 明日菜单提醒 ({date_str} {day_label})"]
        lines.append("")

        lunch = [m for m in meals if m["meal_type"] == "lunch"]
        dinner = [m for m in meals if m["meal_type"] == "dinner"]

        if lunch:
            lines.append("🥗 午餐:")
            for m in lunch:
                lines.append(f"  · {m['dish_name']} ({m['servings']}人份)")

        if dinner:
            lines.append("🍲 晚餐:")
            for m in dinner:
                lines.append(f"  · {m['dish_name']} ({m['servings']}人份)")

        if defrost_items:
            lines.append("")
            lines.append("❄️ 今晚需要解冻:")
            for item in sorted(defrost_items):
                lines.append(f"  · {item}")

        return {"message": "\n".join(lines), "date": tomorrow.isoformat(), "meals": meals}
    finally:
        await db.close()


# ===================================================================
# SSE endpoint
# ===================================================================
@app.get("/api/plan/events")
async def plan_events(week: str = Query(...)):
    q = sse_hub.subscribe(week)

    async def event_generator():
        try:
            # Send initial heartbeat
            yield "data: {\"type\":\"connected\"}\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            sse_hub.unsubscribe(week, q)

    return StreamingResponse(event_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ===================================================================
# Grocery checklist (server-side, synced via SSE)
# ===================================================================
grocery_checks: dict[str, dict[str, bool]] = {}  # {week: {item_key: True}}
grocery_subscribers: list[tuple[str, asyncio.Queue]] = []  # [(week, queue)]

async def broadcast_grocery(week: str):
    data = json.dumps({"type": "grocery_update", "checks": grocery_checks.get(week, {})})
    for w, q in list(grocery_subscribers):
        if w == week:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass

@app.post("/api/grocery/check")
async def toggle_grocery_check(request: Request):
    body = await request.json()
    week = body["week"]
    key = body["key"]
    checked = body.get("checked", True)
    if week not in grocery_checks:
        grocery_checks[week] = {}
    if checked:
        grocery_checks[week][key] = True
    else:
        grocery_checks[week].pop(key, None)
    await broadcast_grocery(week)
    return {"ok": True}

@app.get("/api/grocery/checks")
async def get_grocery_checks(week: str = Query(...)):
    return grocery_checks.get(week, {})

@app.get("/api/grocery/events")
async def grocery_events(week: str = Query(...)):
    q = asyncio.Queue(maxsize=50)
    grocery_subscribers.append((week, q))

    async def event_generator():
        try:
            yield f"data: {json.dumps({'type': 'grocery_update', 'checks': grocery_checks.get(week, {})})}\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            if (week, q) in grocery_subscribers:
                grocery_subscribers.remove((week, q))

    return StreamingResponse(event_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ===================================================================
# Shared Guest Cart (in-memory, synced via SSE)
# ===================================================================
shared_cart: dict = {}  # {dish_id_str: {dish_id, dish_name, qty}}
cart_subscribers: list[asyncio.Queue] = []

async def broadcast_cart():
    """Notify all SSE subscribers of cart update."""
    data = json.dumps({"type": "cart_update", "cart": shared_cart})
    for q in list(cart_subscribers):
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            pass

@app.post("/api/cart/update")
async def update_shared_cart(request: Request):
    """Guest pushes their cart state to the shared cart."""
    global shared_cart
    body = await request.json()
    shared_cart = body.get("cart", {})
    await broadcast_cart()
    return {"ok": True}

@app.get("/api/cart")
async def get_shared_cart():
    return shared_cart

@app.get("/api/cart/events")
async def cart_events():
    q = asyncio.Queue(maxsize=50)
    cart_subscribers.append(q)

    async def event_generator():
        try:
            # Send current cart state on connect
            yield f"data: {json.dumps({'type': 'cart_update', 'cart': shared_cart})}\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            if q in cart_subscribers:
                cart_subscribers.remove(q)

    return StreamingResponse(event_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ===================================================================
# Guest Orders (in-memory, ephemeral)
# ===================================================================
guest_orders: list[dict] = []

@app.post("/api/guest-order")
async def submit_guest_order(request: Request):
    """Guest submits their cart as an order."""
    body = await request.json()
    name = body.get("name", "Guest").strip() or "Guest"
    items = body.get("items", [])
    if not items:
        raise HTTPException(400, "Cart is empty")
    order = {
        "id": str(uuid.uuid4())[:8],
        "name": name,
        "items": items,  # [{dish_id, dish_name, quantity}]
        "submitted_at": datetime.now().isoformat(timespec="minutes"),
    }
    guest_orders.append(order)
    # Keep only last 50 orders
    if len(guest_orders) > 50:
        guest_orders.pop(0)
    return {"ok": True, "order_id": order["id"]}

@app.get("/api/guest-orders")
async def list_guest_orders():
    """Admin fetches all guest orders."""
    return guest_orders

@app.delete("/api/guest-orders")
async def clear_guest_orders():
    """Admin clears all guest orders."""
    guest_orders.clear()
    return {"ok": True}

@app.delete("/api/guest-orders/{order_id}")
async def delete_guest_order(order_id: str):
    """Admin deletes a specific guest order."""
    for i, o in enumerate(guest_orders):
        if o["id"] == order_id:
            guest_orders.pop(i)
            return {"ok": True}
    raise HTTPException(404, "Order not found")


# ===================================================================
# SPA HTML
# ===================================================================
SPA_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<title>Meal Planner</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
<style>
/* ===== RESET & BASE ===== */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { font-size: 16px; -webkit-text-size-adjust: 100%; touch-action: manipulation; }
body {
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: #faf7f2;
    color: #2d2a24;
    min-height: 100vh;
    min-height: 100dvh;
    padding-bottom: 72px;
    line-height: 1.5;
}
a { color: inherit; text-decoration: none; }
button { cursor: pointer; border: none; background: none; font: inherit; }
input, select, textarea { font: inherit; }

/* ===== COLORS ===== */
:root {
    --bg: #faf7f2;
    --card: #ffffff;
    --primary: #e07a3a;
    --primary-dark: #c4622a;
    --primary-light: #fdf0e8;
    --green: #5a9e6f;
    --green-light: #eef6f0;
    --text: #2d2a24;
    --text-secondary: #7a756c;
    --border: #e8e3db;
    --shadow: 0 2px 8px rgba(0,0,0,0.08);
    --shadow-lg: 0 8px 32px rgba(0,0,0,0.12);
    --radius: 12px;
    --radius-sm: 8px;
}

/* ===== TOP NAV ===== */
.top-nav {
    position: sticky;
    top: 0;
    z-index: 100;
    background: var(--card);
    border-bottom: 1px solid var(--border);
    padding: 12px 16px;
    display: flex;
    align-items: center;
    gap: 12px;
    backdrop-filter: blur(10px);
    -webkit-backdrop-filter: blur(10px);
}
.top-nav .back-btn {
    width: 36px; height: 36px;
    display: flex; align-items: center; justify-content: center;
    border-radius: 50%;
    background: var(--bg);
    color: var(--primary);
    font-size: 20px;
    flex-shrink: 0;
}
.top-nav .nav-title {
    font-size: 18px;
    font-weight: 700;
    flex: 1;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.top-nav .nav-actions { display: flex; gap: 8px; }
.top-nav .nav-action-btn {
    height: 32px;
    display: flex; align-items: center; justify-content: center;
    border-radius: 8px;
    background: var(--primary-light);
    color: var(--primary);
    font-size: 13px;
    font-weight: 600;
    padding: 0 12px;
    white-space: nowrap;
    border: none;
    cursor: pointer;
}

/* ===== BOTTOM TAB BAR ===== */
.tab-bar {
    position: fixed;
    bottom: 0; left: 0; right: 0;
    z-index: 100;
    background: var(--card);
    border-top: 1px solid var(--border);
    display: flex;
    padding-bottom: env(safe-area-inset-bottom, 0px);
    height: calc(60px + env(safe-area-inset-bottom, 0px));
}
.tab-bar a {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 2px;
    padding: 8px 4px;
    font-size: 10px;
    font-weight: 600;
    color: var(--text-secondary);
    transition: color 0.2s;
    min-height: 44px;
}
.tab-bar a.active { color: var(--primary); }
.tab-bar a .tab-icon { font-size: 22px; line-height: 1; }

/* ===== PAGE CONTAINER ===== */
.page { display: none; padding: 16px; max-width: 600px; margin: 0 auto; }
.page.active { display: block; }

/* ===== HOME PAGE ===== */
.home-hero {
    text-align: center;
    padding: 24px 0 16px;
}
.home-hero h1 {
    font-size: 28px; font-weight: 800;
    background: linear-gradient(135deg, var(--primary), #d45d8a);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
}
.home-hero p { color: var(--text-secondary); margin-top: 4px; }
.quick-actions {
    display: flex;
    flex-direction: column;
    gap: 10px;
    margin-top: 20px;
}
.qa-row {
    display: flex;
    align-items: center;
    background: var(--card);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    overflow: hidden;
    min-height: 48px;
}
.qa-row:active { transform: scale(0.98); }
.qa-main {
    flex: 1;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 16px;
    font-weight: 600;
    font-size: 15px;
    cursor: pointer;
}
.qa-main .qa-icon { font-size: 22px; flex-shrink: 0; }
.qa-side-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 48px;
    height: 48px;
    border-left: 1px solid var(--border);
    cursor: pointer;
    font-size: 18px;
    opacity: 0.6;
    transition: opacity 0.15s;
}
.qa-side-btn:hover { opacity: 1; }

.home-week-preview {
    margin-top: 24px;
    background: var(--card);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    overflow: hidden;
}
.home-week-header {
    padding: 14px 16px;
    font-weight: 700;
    font-size: 16px;
    border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
}
.home-week-body { padding: 8px; }
.home-day-row {
    display: flex;
    align-items: flex-start;
    padding: 8px;
    border-bottom: 1px solid var(--border);
}
.home-day-row:last-child { border-bottom: none; }
.home-day-label {
    width: 42px;
    font-size: 12px;
    font-weight: 700;
    color: var(--primary);
    padding-top: 2px;
    flex-shrink: 0;
}
.home-day-meals { flex: 1; display: flex; flex-direction: column; gap: 2px; }
.home-meal-item {
    font-size: 13px;
    color: var(--text);
    display: flex;
    align-items: center;
    gap: 4px;
}
.home-meal-item .meal-badge {
    font-size: 10px;
    font-weight: 700;
    color: var(--text-secondary);
    width: 14px;
    text-align: center;
}
.home-meal-empty {
    font-size: 12px;
    color: var(--border);
    font-style: italic;
}

/* ===== DISHES PAGE ===== */
.search-bar {
    display: flex;
    align-items: center;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 24px;
    padding: 8px 16px;
    margin-bottom: 16px;
    gap: 8px;
}
.search-bar input {
    flex: 1;
    border: none;
    outline: none;
    background: transparent;
    font-size: 15px;
}
.search-bar .search-icon { color: var(--text-secondary); font-size: 16px; }

.dish-grid {
    display: flex;
    flex-direction: column;
    gap: 8px;
}
.dish-card {
    background: var(--card);
    border-radius: var(--radius-sm);
    box-shadow: var(--shadow);
    overflow: hidden;
    cursor: pointer;
    transition: transform 0.15s;
    display: flex;
    flex-direction: row;
    align-items: center;
}
.dish-card:active { transform: scale(0.98); }
.dish-card-img {
    width: 64px;
    height: 64px;
    object-fit: cover;
    background: var(--border);
    display: block;
    flex-shrink: 0;
    border-radius: var(--radius-sm) 0 0 var(--radius-sm);
}
.dish-card-img-placeholder {
    width: 64px;
    height: 64px;
    background: linear-gradient(135deg, var(--primary-light), var(--green-light));
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 28px;
    flex-shrink: 0;
}
.dish-card-body { padding: 10px 12px; flex: 1; min-width: 0; }
.dish-card-name { font-weight: 700; font-size: 14px; margin-bottom: 2px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.dish-card-meta { font-size: 12px; color: var(--text-secondary); }

.fab {
    position: fixed;
    bottom: calc(72px + env(safe-area-inset-bottom, 0px));
    right: 20px;
    width: 56px; height: 56px;
    border-radius: 50%;
    background: var(--primary);
    color: white;
    font-size: 28px;
    display: flex; align-items: center; justify-content: center;
    box-shadow: var(--shadow-lg);
    z-index: 50;
    transition: transform 0.15s;
}
.fab:active { transform: scale(0.92); }

/* ===== FORM STYLES ===== */
.form-group { margin-bottom: 16px; }
.form-label {
    display: block;
    font-size: 13px;
    font-weight: 600;
    color: var(--text-secondary);
    margin-bottom: 6px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}
.form-input {
    width: 100%;
    padding: 12px 14px;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    font-size: 16px;
    background: var(--card);
    transition: border-color 0.2s;
    -webkit-appearance: none;
}
.form-input:focus { outline: none; border-color: var(--primary); }
textarea.form-input { min-height: 150px; max-height: 50vh; resize: vertical; overflow-y: auto; }

.image-upload-area {
    border: 2px dashed var(--border);
    border-radius: var(--radius);
    padding: 24px;
    text-align: center;
    cursor: pointer;
    transition: border-color 0.2s;
    position: relative;
    overflow: hidden;
}
.image-upload-area:hover { border-color: var(--primary); }
.image-upload-area img {
    max-width: 100%;
    max-height: 200px;
    border-radius: var(--radius-sm);
}
.image-upload-area .upload-text { color: var(--text-secondary); font-size: 14px; margin-top: 8px; }
.image-upload-area input[type="file"] { display: none; }
.remove-image-btn {
    position: absolute; top: 8px; right: 8px;
    width: 28px; height: 28px;
    border-radius: 50%;
    background: rgba(0,0,0,0.6);
    color: white;
    font-size: 16px;
    display: flex; align-items: center; justify-content: center;
}

/* Ingredients editor */
/* Tags editor */
.tags-editor { margin-bottom: 8px; }
.tags-selected {
    display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px;
}
.tag-chip {
    display: inline-flex; align-items: center; gap: 4px;
    background: var(--primary-light); color: var(--primary);
    padding: 4px 10px; border-radius: 14px;
    font-size: 13px; font-weight: 600;
}
.tag-chip .tag-remove {
    cursor: pointer; font-size: 16px; opacity: 0.6;
    margin-left: 2px; line-height: 1;
}
.tag-chip .tag-remove:hover { opacity: 1; }
.tags-input-wrap { position: relative; }
.tags-suggestions {
    display: none; position: absolute; top: 100%; left: 0; right: 0;
    background: var(--card); border: 1px solid var(--border);
    border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1);
    max-height: 180px; overflow-y: auto; z-index: 100;
}
.tags-suggestions.open { display: block; }
.tag-suggestion {
    padding: 10px 14px; cursor: pointer; font-size: 14px;
    border-bottom: 1px solid var(--border);
}
.tag-suggestion:last-child { border-bottom: none; }
.tag-suggestion:active { background: var(--primary-light); }
.tag-suggestion.create-new { color: var(--primary); font-weight: 600; }

/* Tag filter pills in dish library */
.tag-filters {
    display: flex; gap: 6px; flex-wrap: wrap;
    margin-bottom: 12px; padding: 0 2px;
}
.tag-filter-btn {
    padding: 5px 12px; border-radius: 14px;
    border: 1px solid var(--border);
    background: var(--card); color: var(--text);
    font-size: 12px; cursor: pointer;
    transition: all 0.15s;
}
.tag-filter-btn.active {
    background: var(--primary); color: white; border-color: var(--primary);
}

/* Note images */
.note-images-grid {
    display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px;
}
.note-img-thumb {
    position: relative; width: 72px; height: 72px;
    border-radius: 8px; overflow: hidden;
    cursor: pointer;
}
.note-img-thumb img {
    width: 100%; height: 100%; object-fit: cover; display: block;
}
.note-img-remove {
    position: absolute; top: 2px; right: 2px;
    width: 22px; height: 22px; border-radius: 50%;
    background: rgba(0,0,0,0.6); color: white;
    display: flex; align-items: center; justify-content: center;
    font-size: 14px; cursor: pointer; border: none;
}
.add-note-img-btn {
    display: inline-block; margin-top: 8px;
    padding: 8px 14px; border-radius: 8px;
    border: 1px dashed var(--border);
    background: none; color: var(--text-secondary);
    font-size: 13px; cursor: pointer;
}
/* Note image popup */
.note-img-popup {
    display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.85); z-index: 9999;
    align-items: center; justify-content: center;
}
.note-img-popup.open { display: flex; }
.note-img-popup img {
    max-width: 92vw; max-height: 88vh; border-radius: 8px; object-fit: contain;
}
.note-img-popup-close {
    position: absolute; top: 16px; right: 16px;
    width: 40px; height: 40px; border-radius: 50%;
    background: rgba(255,255,255,0.2); border: none;
    color: white; font-size: 24px; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    z-index: 10;
}
.note-img-nav {
    position: absolute; top: 50%; transform: translateY(-50%);
    width: 44px; height: 44px; border-radius: 50%;
    background: rgba(255,255,255,0.2); border: none;
    color: white; font-size: 28px; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    z-index: 10;
}
.note-img-prev { left: 12px; }
.note-img-next { right: 12px; }
.note-img-nav:active { background: rgba(255,255,255,0.4); }
.note-img-counter {
    position: absolute; bottom: 20px; left: 50%; transform: translateX(-50%);
    background: rgba(0,0,0,0.5); color: white;
    padding: 4px 12px; border-radius: 12px;
    font-size: 13px; z-index: 10;
}

.ingredients-editor { margin-bottom: 16px; }
.ing-row {
    display: flex;
    gap: 8px;
    align-items: center;
    margin-bottom: 8px;
}
.ing-row .ing-name { flex: 2; }
.ing-row .ing-amount { flex: 1; }
.ing-row .ing-unit { flex: 1; }
.ing-row .ing-remove {
    width: 36px; height: 36px;
    border-radius: 50%;
    background: #fde8e8;
    color: #d44;
    display: flex; align-items: center; justify-content: center;
    font-size: 18px;
    flex-shrink: 0;
}
.ing-row input, .ing-row select {
    padding: 10px;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--card);
    font-size: 14px;
    width: 100%;
}
.ing-row input:focus, .ing-row select:focus { outline: none; border-color: var(--primary); }
.add-ing-btn {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 10px 16px;
    background: var(--green-light);
    color: var(--green);
    border-radius: var(--radius-sm);
    font-weight: 600;
    font-size: 14px;
}
.btn-primary {
    width: 100%;
    padding: 14px;
    background: var(--primary);
    color: white;
    border-radius: var(--radius);
    font-size: 16px;
    font-weight: 700;
    transition: background 0.2s;
    min-height: 48px;
}
.btn-primary:active { background: var(--primary-dark); }
.btn-danger {
    width: 100%;
    padding: 14px;
    background: #fde8e8;
    color: #c33;
    border-radius: var(--radius);
    font-size: 16px;
    font-weight: 700;
    margin-top: 8px;
}
.btn-secondary {
    padding: 10px 20px;
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    font-size: 14px;
    font-weight: 600;
}

/* ===== PLANNER ===== */
.week-nav {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 16px;
    background: var(--card);
    border-radius: var(--radius);
    padding: 10px 12px;
    box-shadow: var(--shadow);
}
.week-nav button {
    width: 40px; height: 40px;
    border-radius: 50%;
    background: var(--primary-light);
    color: var(--primary);
    font-size: 18px;
    display: flex; align-items: center; justify-content: center;
    min-height: 44px; min-width: 44px;
}
.week-nav .week-label { font-weight: 700; font-size: 15px; text-align: center; }

.plan-day {
    background: var(--card);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    margin-bottom: 12px;
    overflow: hidden;
}
.plan-day-header {
    padding: 10px 14px;
    font-weight: 700;
    font-size: 14px;
    background: var(--bg);
    border-bottom: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.plan-day-header .day-date { font-weight: 400; color: var(--text-secondary); font-size: 12px; }
.plan-meals { display: flex; flex-direction: column; }
.plan-meal-slot {
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 10px;
}
.plan-meal-slot:last-child { border-bottom: none; }
.meal-type-label {
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    color: var(--text-secondary);
    width: 52px;
    flex-shrink: 0;
}
.meal-slot-content { flex: 1; display: flex; flex-direction: column; gap: 6px; }
.meal-slot-empty {
    padding: 8px 12px;
    border: 1px dashed var(--border);
    border-radius: var(--radius-sm);
    text-align: center;
    color: var(--text-secondary);
    font-size: 13px;
    cursor: pointer;
    min-height: 44px;
    display: flex; align-items: center; justify-content: center;
}
.meal-slot-empty:hover { border-color: var(--primary); color: var(--primary); }
.planned-dish {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 6px 10px;
    background: var(--primary-light);
    border-radius: var(--radius-sm);
    cursor: pointer;
}
.planned-dish:active { opacity: 0.8; }
.planned-dish.dragging { opacity: 0.4; }
.plan-meal-slot.drag-over { background: rgba(0,180,216,0.08); outline: 2px dashed var(--primary); outline-offset: -2px; border-radius: 8px; }
.planned-dish-img {
    width: 40px; height: 40px;
    border-radius: 6px;
    object-fit: cover;
    flex-shrink: 0;
}
.planned-dish-img-placeholder {
    width: 40px; height: 40px;
    border-radius: 6px;
    background: linear-gradient(135deg, var(--primary-light), var(--green-light));
    display: flex; align-items: center; justify-content: center;
    font-size: 18px;
    flex-shrink: 0;
}
.planned-dish-info { flex: 1; min-width: 0; }
.planned-dish-name {
    font-weight: 600; font-size: 14px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.planned-dish-servings { font-size: 12px; color: var(--text-secondary); }

/* ===== DISH PICKER OVERLAY ===== */
.overlay {
    position: fixed;
    inset: 0;
    z-index: 200;
    display: none;
}
.overlay.open { display: flex; flex-direction: column; }
.overlay-backdrop {
    position: absolute; inset: 0;
    background: rgba(0,0,0,0.4);
}
.overlay-content {
    position: absolute;
    bottom: 0;
    left: 0;
    right: 0;
    top: 0;
    background: var(--card);
    border-radius: 0;
    display: flex;
    flex-direction: column;
    z-index: 1;
    animation: slideUp 0.2s ease-out;
}
@keyframes slideUp {
    from { transform: translateY(100%); }
    to { transform: translateY(0); }
}
.overlay-header {
    padding: 16px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
}
.overlay-header h3 { font-size: 18px; font-weight: 700; }
.overlay-close {
    width: 32px; height: 32px;
    border-radius: 50%;
    background: var(--bg);
    display: flex; align-items: center; justify-content: center;
    font-size: 18px;
}
.overlay-body {
    flex: 1;
    overflow-y: auto;
    padding: 0 16px 16px;
    -webkit-overflow-scrolling: touch;
}
.picker-sticky-top {
    position: sticky;
    top: 0;
    background: var(--card);
    z-index: 2;
    padding-top: 16px;
    padding-bottom: 8px;
}
.picker-grid {
    display: flex;
    flex-direction: column;
    gap: 6px;
}
.picker-dish {
    display: flex;
    align-items: center;
    gap: 10px;
    background: var(--bg);
    border-radius: var(--radius-sm);
    padding: 8px;
    cursor: pointer;
    transition: transform 0.12s;
}
.picker-add-btn:active { transform: scale(0.9); }
.picker-dish-img {
    width: 48px; height: 48px;
    border-radius: 8px;
    object-fit: cover; display: block; flex-shrink: 0;
}
.picker-dish-img-ph {
    width: 48px; height: 48px;
    border-radius: 8px;
    background: linear-gradient(135deg, var(--primary-light), var(--green-light));
    display: flex; align-items: center; justify-content: center;
    font-size: 22px; flex-shrink: 0;
}
.picker-dish-name {
    font-size: 14px;
    font-weight: 600;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    flex: 1; min-width: 0;
}
.picker-add-btn {
    flex-shrink: 0;
    width: 36px; height: 36px;
    border-radius: 50%;
    border: none;
    background: var(--primary);
    color: white;
    font-size: 20px;
    font-weight: 700;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
}
.picker-tag-filters {
    display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 10px;
}
.picker-search {
    margin-bottom: 12px;
    padding: 10px 14px;
    width: 100%;
    border: 1px solid var(--border);
    border-radius: 20px;
    font-size: 15px;
    background: var(--bg);
}
.picker-search:focus { outline: none; border-color: var(--primary); }

/* Servings overlay */
.servings-overlay-body {
    padding: 16px;
    display: flex;
    flex-direction: column;
    overflow-y: auto;
}
.servings-overlay-body h4 { font-size: 16px; margin-bottom: 12px; text-align: center; }
.servings-notes {
    font-size: 14px;
    line-height: 1.5;
    color: var(--text-secondary);
    white-space: pre-wrap;
    word-break: break-word;
    background: var(--bg);
    border-radius: 10px;
    padding: 12px 14px;
    margin-bottom: 16px;
    max-height: calc(1.5em * 15 + 24px); /* 15 lines + padding */
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
}
.servings-controls {
    text-align: center;
    flex-shrink: 0;
}
.servings-stepper {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 16px;
    margin-bottom: 20px;
}
.servings-stepper button {
    width: 48px; height: 48px;
    border-radius: 50%;
    background: var(--primary-light);
    color: var(--primary);
    font-size: 22px;
    font-weight: 700;
    display: flex; align-items: center; justify-content: center;
    min-height: 44px; min-width: 44px;
}
.servings-stepper .servings-val {
    font-size: 32px; font-weight: 800; min-width: 48px; text-align: center;
}

/* ===== PREVIEW & GROCERY ===== */
.preview-table {
    width: 100%;
    border-collapse: collapse;
    background: var(--card);
    border-radius: var(--radius);
    overflow: hidden;
    box-shadow: var(--shadow);
    font-size: 13px;
}
.preview-table th {
    background: var(--primary);
    color: white;
    padding: 10px 8px;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}
.preview-table td {
    padding: 8px;
    border-bottom: 1px solid var(--border);
    vertical-align: top;
}
.preview-table tr:last-child td { border-bottom: none; }
.preview-table .day-col {
    font-weight: 700;
    color: var(--primary);
    white-space: nowrap;
}
.preview-dish-entry { margin-bottom: 4px; }
.preview-dish-entry:last-child { margin-bottom: 0; }
.preview-dish-name { font-weight: 600; }
.preview-dish-servings { color: var(--text-secondary); font-size: 11px; }

.export-btns {
    display: flex;
    gap: 8px;
    margin: 16px 0;
    flex-wrap: wrap;
}
.export-btn {
    flex: 1;
    min-width: 100px;
    padding: 12px 16px;
    border-radius: var(--radius-sm);
    font-weight: 600;
    font-size: 14px;
    text-align: center;
    min-height: 44px;
    display: flex; align-items: center; justify-content: center; gap: 6px;
}
.export-btn.print-btn { background: var(--primary); color: white; }
.export-btn.img-btn { background: var(--green); color: white; }
.export-btn.copy-btn { background: #5b7bbd; color: white; }

/* ===== GROCERY LIST ===== */
.grocery-list { list-style: none; }
.grocery-item {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 14px;
    background: var(--card);
    border-radius: var(--radius-sm);
    margin-bottom: 6px;
    box-shadow: var(--shadow);
    min-height: 44px;
}
.grocery-item.checked { opacity: 0.5; }
.grocery-item.checked .grocery-name { text-decoration: line-through; }
.grocery-cb {
    width: 22px; height: 22px;
    border-radius: 6px;
    border: 2px solid var(--border);
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0;
    cursor: pointer;
    min-width: 44px; min-height: 44px;
    justify-content: center;
}
.grocery-cb.checked { background: var(--green); border-color: var(--green); color: white; }
.grocery-name { flex: 1; font-size: 15px; }
.grocery-amount { font-weight: 600; color: var(--primary); font-size: 14px; white-space: nowrap; }

.empty-state {
    text-align: center;
    padding: 40px 20px;
    color: var(--text-secondary);
}
.empty-state .empty-icon { font-size: 48px; margin-bottom: 12px; }
.empty-state p { font-size: 14px; }

/* ===== TOAST ===== */
.toast-container {
    position: fixed;
    top: 16px;
    left: 50%; transform: translateX(-50%);
    z-index: 300;
    display: flex;
    flex-direction: column;
    gap: 8px;
    pointer-events: none;
}
.toast {
    background: var(--text);
    color: white;
    padding: 12px 20px;
    border-radius: 24px;
    font-size: 14px;
    font-weight: 600;
    box-shadow: var(--shadow-lg);
    animation: toastIn 0.3s ease-out;
    pointer-events: auto;
}
.toast.error { background: #c33; }
@keyframes toastIn {
    from { opacity: 0; transform: translateY(-10px); }
    to { opacity: 1; transform: translateY(0); }
}

/* Print */
@media print {
    .top-nav, .tab-bar, .export-btns, .fab, .week-nav,
    .home-hero, .quick-actions, .home-week-preview,
    #sharedCartSection { display: none !important; }
    body { padding: 0; background: white; color: black; -webkit-print-color-adjust: exact; }
    .page { display: none !important; max-width: none; padding: 10px; }
    .page.active { display: block !important; }
    .preview-table, .grocery-item { color: black; }
}
</style>
</head>
<body>

<!-- Toast container -->
<div class="toast-container" id="toastContainer"></div>

<!-- ===== TOP NAV (dynamic) ===== -->
<nav class="top-nav" id="topNav">
    <span class="nav-title" id="navTitle">Meal Planner</span>
    <div class="nav-actions" id="navActions"></div>
</nav>

<!-- ===== PAGES ===== -->

<!-- HOME -->
<div class="page" id="page-home">
    <div class="home-hero">
        <h1>Meal Planner</h1>
        <p>Plan your week, simplify your shopping</p>
    </div>
    <div class="quick-actions">
        <div class="qa-row" onclick="navigate('preview')">
            <div class="qa-main">
                <span class="qa-icon">&#128196;</span>
                Meal Plan Preview
            </div>
        </div>
        <div class="qa-row">
            <div class="qa-main" onclick="window.open('/menu','_blank')">
                <span class="qa-icon">&#127869;</span>
                Guest Menu
            </div>
            <div class="qa-side-btn" onclick="event.stopPropagation();copyMenuLink()" title="Copy link"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg></div>
        </div>
    </div>
    <div class="home-week-preview" id="sharedCartSection" style="display:none;">
        <div class="home-week-header">
            <span>&#128722; Guest Menu Cart</span>
            <a onclick="clearSharedCart()" style="color:var(--danger);font-size:13px;font-weight:600;">Clear</a>
        </div>
        <div id="sharedCartBody"></div>
        <div id="sharedCartActions" style="padding:8px 12px 12px;display:flex;gap:8px;flex-wrap:wrap;">
            <button onclick="copyCartTextAdmin()" style="flex:1;padding:10px;border:none;border-radius:8px;background:#5b7bbd;color:white;font-weight:600;font-size:13px;cursor:pointer;">&#128203; Copy Text</button>
            <button onclick="saveCartImageAdmin()" style="flex:1;padding:10px;border:none;border-radius:8px;background:#2d9a4e;color:white;font-weight:600;font-size:13px;cursor:pointer;">&#128190; Save Image</button>
            <button onclick="cartGroceryAdmin()" style="flex:1;padding:10px;border:none;border-radius:8px;background:var(--primary);color:white;font-weight:600;font-size:13px;cursor:pointer;">&#128722; Grocery List</button>
        </div>
        <div id="cartGroceryModal" style="display:none;padding:12px 16px;border-top:1px solid var(--border);"></div>
    </div>
    <div class="home-week-preview">
        <div class="home-week-header">
            <span>🏷️ Tag Management</span>
        </div>
        <div id="tagManageBody" style="padding:8px 0;"></div>
    </div>
</div>

<!-- DISHES -->
<div class="page" id="page-dishes">
    <div class="search-bar">
        <span class="search-icon">&#128269;</span>
        <input type="text" placeholder="Search dishes..." id="dishSearchInput" oninput="filterDishes()">
    </div>
    <div class="tag-filters" id="tagFilters"></div>
    <div class="dish-grid" id="dishGrid"></div>
    <div class="empty-state" id="dishesEmpty" style="display:none;">
        <div class="empty-icon">&#127858;</div>
        <p>No dishes yet. Tap + to add one!</p>
    </div>
    <button class="fab" onclick="navigate('dish-new')">+</button>
</div>

<!-- DISH FORM (new/edit) -->
<div class="page" id="page-dish-form">
    <form id="dishForm" onsubmit="saveDish(event)">
        <div class="form-group">
            <label class="form-label">Dish Name</label>
            <input type="text" class="form-input" id="dishName" placeholder="e.g. Chicken Stir Fry" required>
        </div>
        <div class="form-group">
            <label class="form-label">Notes (optional)</label>
            <textarea class="form-input" id="dishNotes" placeholder="Cooking instructions, air fryer temp, etc." oninput="autoResizeTextarea(this)"></textarea>
            <div class="note-images-section" id="noteImagesSection" style="display:none;">
                <div class="note-images-grid" id="noteImagesGrid"></div>
            </div>
            <button type="button" class="add-note-img-btn" id="addNoteImgBtn" style="display:none;" onclick="document.getElementById('noteImageInput').click()">📷 Add Note Photo</button>
            <input type="file" id="noteImageInput" accept="image/*" multiple style="display:none;" onchange="uploadNoteImages(this)">
        </div>
        <div class="form-group">
            <label class="form-label">Photo (optional)</label>
            <div class="image-upload-area" id="imageUploadArea" onclick="document.getElementById('dishImage').click()">
                <div id="imagePreview">
                    <div style="font-size:36px;margin-bottom:4px;">&#128247;</div>
                    <div class="upload-text">Tap to add a photo</div>
                </div>
                <input type="file" id="dishImage" accept="image/*" onchange="previewImage(this)">
            </div>
        </div>
        <div class="form-group">
            <label class="form-label">Tags</label>
            <div class="tags-editor" id="tagsEditor">
                <div class="tags-selected" id="tagsSelected"></div>
                <div class="tags-input-wrap">
                    <input type="text" class="form-input" id="tagInput" placeholder="Type to search or add tag..." autocomplete="off" oninput="filterTagSuggestions()" onfocus="showTagSuggestions()">
                    <div class="tags-suggestions" id="tagSuggestions"></div>
                </div>
            </div>
        </div>
        <div class="form-group">
            <label class="form-label">Ingredients (amount per person)</label>
            <div class="ingredients-editor" id="ingredientsEditor"></div>
            <button type="button" class="add-ing-btn" onclick="addIngredientRow()">+ Add Ingredient</button>
        </div>
        <button type="submit" class="btn-primary" id="dishFormSubmit">Save Dish</button>
        <button type="button" class="btn-danger" id="dishDeleteBtn" style="display:none;" onclick="deleteDish()">Delete Dish</button>
    </form>
</div>

<!-- WEEKLY PLANNER -->
<div class="page" id="page-plan">
    <div class="week-nav">
        <button onclick="changeWeek(-1)">&#8249;</button>
        <div class="week-label" id="weekLabel"></div>
        <button onclick="changeWeek(1)">&#8250;</button>
    </div>
    <div id="planBody"></div>
</div>

<!-- PREVIEW -->
<div class="page" id="page-preview">
    <div class="week-nav">
        <button onclick="changePreviewWeek(-1)">&#8249;</button>
        <div class="week-label" id="previewWeekLabel"></div>
        <button onclick="changePreviewWeek(1)">&#8250;</button>
    </div>
    <div class="export-btns">
        <button class="export-btn print-btn" onclick="doPrint()">&#128424; Print</button>
        <button class="export-btn img-btn" onclick="exportPreviewImage()">&#128190; Save Image</button>
    </div>
    <div id="previewContent"></div>
</div>

<!-- GROCERY -->
<div class="page" id="page-grocery">
    <div class="week-nav">
        <button onclick="changeGroceryWeek(-1)">&#8249;</button>
        <div class="week-label" id="groceryWeekLabel"></div>
        <button onclick="changeGroceryWeek(1)">&#8250;</button>
    </div>
    <div class="export-btns">
        <button class="export-btn print-btn" onclick="doPrint()">&#128424; Print</button>
        <button class="export-btn img-btn" onclick="exportGroceryImage()">&#128190; Save Image</button>
        <button class="export-btn copy-btn" onclick="copyGroceryText()">&#128203; Copy Text</button>
    </div>
    <div id="groceryContent"></div>
</div>

<!-- ===== OVERLAYS ===== -->

<!-- Dish picker -->
<div class="overlay" id="dishPickerOverlay">
    <div class="overlay-backdrop" onclick="closeDishPicker()"></div>
    <div class="overlay-content">
        <div class="overlay-header">
            <h3>Choose a Dish</h3>
            <button class="overlay-close" onclick="closeDishPicker()">&times;</button>
        </div>
        <div class="overlay-body">
            <div class="picker-sticky-top">
                <input type="text" class="picker-search" placeholder="Search dishes..." id="pickerSearch" oninput="filterPicker()">
                <div class="picker-tag-filters" id="pickerTagFilters"></div>
            </div>
            <div class="picker-grid" id="pickerGrid"></div>
        </div>
    </div>
</div>

<!-- Servings overlay -->
<div class="overlay" id="servingsOverlay">
    <div class="overlay-backdrop" onclick="closeServingsOverlay()"></div>
    <div class="overlay-content">
        <div class="overlay-header">
            <button class="overlay-back" id="servingsBackBtn" onclick="servingsGoBack()" style="display:none;border:none;background:none;font-size:24px;cursor:pointer;color:var(--text);padding:4px 8px 4px 0;">&#8249;</button>
            <h3 id="servingsTitle" style="flex:1;">Set Servings</h3>
            <button class="overlay-close" onclick="closeServingsOverlay()">&times;</button>
        </div>
        <div class="servings-overlay-body">
            <h4 id="servingsDishName"></h4>
            <div id="servingsNotes" class="servings-notes" style="display:none;"></div>
            <div class="servings-controls">
                <div class="servings-stepper">
                    <button onclick="adjustServings(-1)">&minus;</button>
                    <span class="servings-val" id="servingsVal">2</span>
                    <button onclick="adjustServings(1)">+</button>
                </div>
                <button class="btn-primary" onclick="confirmServings()" id="servingsConfirmBtn">Add to Plan</button>
                <button class="btn-danger" id="servingsRemoveBtn" style="display:none;" onclick="removePlannedDish()">Remove from Plan</button>
            </div>
        </div>
    </div>
</div>

<!-- ===== TAB BAR ===== -->
<nav class="tab-bar" id="tabBar">
    <a onclick="navigate('home')" data-tab="home">
        <span class="tab-icon">&#127968;</span>
        Home
    </a>
    <a onclick="navigate('dishes')" data-tab="dishes">
        <span class="tab-icon">&#127858;</span>
        Dishes
    </a>
    <a onclick="navigate('plan')" data-tab="plan">
        <span class="tab-icon">&#128197;</span>
        Plan
    </a>
    <a onclick="navigate('grocery')" data-tab="grocery">
        <span class="tab-icon">&#128722;</span>
        Grocery
    </a>
</nav>

<script>
/* ===================================================================
   STATE
   =================================================================== */
const DAY_NAMES = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
const DAY_SHORT = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
const UNITS = ['g','kg','ml','L','cups','tbsp','tsp','pieces','lbs','oz','bunch','can','package'];

let allDishes = [];
let allTags = [];
let selectedFormTags = [];  // tags selected in the dish form
let activeTagFilter = null; // tag filter in dish library
let currentWeek = null;  // initialized after getDefaultPlanWeek is defined
let previewWeek = null;
let groceryWeek = null;
let currentPlan = [];
let editingDishId = null;
let removeImageFlag = false;

// Dish picker state
let pickerCallback = null;

// Servings overlay state
let servingsMode = 'add'; // 'add' or 'edit'
let servingsDishId = null;
let servingsPlanId = null;
let servingsValue = 2;
let servingsSlotInfo = null; // {week_start, day_of_week, meal_type}

// SSE
let eventSource = null;

/* ===================================================================
   ROUTING
   =================================================================== */
function navigate(page, param) {
    const pages = document.querySelectorAll('.page');
    pages.forEach(p => p.classList.remove('active'));

    // Reset top nav
    const topNav = document.getElementById('topNav');
    const navTitle = document.getElementById('navTitle');
    const navActions = document.getElementById('navActions');
    navActions.innerHTML = '';
    let backTarget = null;

    // Tab bar highlighting
    document.querySelectorAll('.tab-bar a').forEach(a => a.classList.remove('active'));

    switch(page) {
        case 'home':
            document.getElementById('page-home').classList.add('active');
            navTitle.textContent = 'Meal Planner';
            document.querySelector('[data-tab="home"]').classList.add('active');
            loadHomePage();
            break;
        case 'dishes':
            document.getElementById('page-dishes').classList.add('active');
            navTitle.textContent = 'Dish Library';
            document.querySelector('[data-tab="dishes"]').classList.add('active');
            loadDishes();
            break;
        case 'dish-new':
            document.getElementById('page-dish-form').classList.add('active');
            navTitle.textContent = 'New Dish';
            backTarget = 'dishes';
            editingDishId = null;
            removeImageFlag = false;
            resetDishForm();
            break;
        case 'dish-edit':
            document.getElementById('page-dish-form').classList.add('active');
            navTitle.textContent = 'Edit Dish';
            backTarget = 'dishes';
            editingDishId = param;
            removeImageFlag = false;
            loadDishForEdit(param);
            break;
        case 'plan':
            document.getElementById('page-plan').classList.add('active');
            navTitle.textContent = 'Weekly Planner';
            document.querySelector('[data-tab="plan"]').classList.add('active');
            navActions.innerHTML = '<button class="nav-action-btn" onclick="navigate(\'preview\')" title="Preview">Preview</button>';
            loadPlan();
            break;
        case 'preview':
            document.getElementById('page-preview').classList.add('active');
            navTitle.textContent = 'Meal Plan Preview';
            backTarget = 'plan';
            previewWeek = new Date(currentWeek);
            loadPreview();
            break;
        case 'grocery':
            document.getElementById('page-grocery').classList.add('active');
            navTitle.textContent = 'Grocery List';
            document.querySelector('[data-tab="grocery"]').classList.add('active');
            groceryWeek = new Date(currentWeek);
            loadGrocery();
            break;
    }

    // Show/hide back button
    const existingBack = topNav.querySelector('.back-btn');
    if (existingBack) existingBack.remove();
    if (backTarget) {
        const btn = document.createElement('button');
        btn.className = 'back-btn';
        btn.innerHTML = '&#8249;';
        btn.onclick = () => navigate(backTarget);
        topNav.insertBefore(btn, navTitle);
    }

    // Show/hide tab bar and fab
    const tabBar = document.getElementById('tabBar');
    const fab = document.querySelector('.fab');
    if (['home','dishes','plan','grocery'].includes(page)) {
        tabBar.style.display = 'flex';
    } else {
        tabBar.style.display = 'none';
    }
    if (fab) fab.style.display = page === 'dishes' ? 'flex' : 'none';

    window.scrollTo(0, 0);
}

/* ===================================================================
   UTILITY
   =================================================================== */
function getMonday(d) {
    const dt = new Date(d);
    const day = dt.getDay();
    const diff = dt.getDate() - day + (day === 0 ? -6 : 1);
    dt.setDate(diff);
    dt.setHours(0, 0, 0, 0);
    return dt;
}

function getDefaultPlanWeek() {
    // Mon-Fri: show this week. Sat-Sun: show next week.
    const today = new Date();
    const dow = today.getDay(); // 0=Sun, 6=Sat
    const monday = getMonday(today);
    if (dow === 0 || dow === 6) {
        monday.setDate(monday.getDate() + 7);
    }
    return monday;
}

function formatDate(d) {
    return d.getFullYear() + '-' +
        String(d.getMonth()+1).padStart(2,'0') + '-' +
        String(d.getDate()).padStart(2,'0');
}

function formatDateShort(d) {
    return (d.getMonth()+1) + '/' + d.getDate();
}

function formatWeekRange(monday) {
    const sun = new Date(monday);
    sun.setDate(sun.getDate() + 6);
    const mStr = monday.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    const sStr = sun.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    return mStr + ' – ' + sStr;
}

function showToast(msg, isError) {
    const container = document.getElementById('toastContainer');
    const el = document.createElement('div');
    el.className = 'toast' + (isError ? ' error' : '');
    el.textContent = msg;
    container.appendChild(el);
    setTimeout(() => { el.remove(); }, 3000);
}

function dishEmoji(name) {
    const n = name.toLowerCase();
    if (n.includes('chicken')) return '&#127831;';
    if (n.includes('salad')) return '&#129367;';
    if (n.includes('pasta') || n.includes('noodle')) return '&#127837;';
    if (n.includes('pizza')) return '&#127829;';
    if (n.includes('soup')) return '&#127858;';
    if (n.includes('fish') || n.includes('salmon') || n.includes('tuna')) return '&#127843;';
    if (n.includes('rice')) return '&#127834;';
    if (n.includes('taco') || n.includes('burrito')) return '&#127790;';
    if (n.includes('burger')) return '&#127828;';
    if (n.includes('steak') || n.includes('beef')) return '&#129385;';
    if (n.includes('sandwich')) return '&#129386;';
    if (n.includes('egg')) return '&#129370;';
    if (n.includes('cake') || n.includes('dessert')) return '&#127856;';
    return '&#127869;';
}

function copyToClip(text, successMsg) {
    // Try modern API first
    if (navigator.clipboard && navigator.clipboard.writeText && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(() => {
            showToast(successMsg || 'Copied!');
        }).catch(() => fallbackCopy(text, successMsg));
    } else {
        fallbackCopy(text, successMsg);
    }
}
function fallbackCopy(text, successMsg) {
    // iOS Safari compatible fallback — must use textarea for newlines
    const ta = document.createElement('textarea');
    ta.setAttribute('readonly', 'readonly');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.top = '0';
    ta.style.left = '0';
    ta.style.width = '1px';
    ta.style.height = '1px';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus();
    ta.setSelectionRange(0, text.length);
    let ok = false;
    try { ok = document.execCommand('copy'); } catch(e) {}
    document.body.removeChild(ta);
    if (ok) {
        showToast(successMsg || 'Copied!');
    } else {
        showToast('Could not copy — long-press to copy: ' + text);
    }
}

async function openAsImage(element) {
    if (typeof html2canvas === 'undefined') {
        const s = document.createElement('script');
        s.src = 'https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js';
        document.head.appendChild(s);
        await new Promise(r => s.onload = r);
    }
    try {
        const canvas = await html2canvas(element, { backgroundColor: '#ffffff', scale: 2 });
        const dataUrl = canvas.toDataURL('image/png');
        const w = window.open('');
        if (w) {
            w.document.write('<html><head><title>Image</title><meta name="viewport" content="width=device-width,initial-scale=1"></head><body style="margin:0;display:flex;justify-content:center;align-items:center;min-height:100vh;background:#000;"><img src="' + dataUrl + '" style="max-width:100%;height:auto;"></body></html>');
            w.document.close();
        } else {
            showToast('Pop-up blocked — allow pop-ups for this site');
        }
    } catch(e) { showToast('Failed to generate image', true); }
}

function copyMenuLink() {
    const url = window.location.origin + '/menu';
    copyToClip(url, 'Menu link copied!');
}

/* ===================================================================
   HOME PAGE
   =================================================================== */
async function loadHomePage() {
    loadSharedCart();
    connectAdminCartSSE();
    loadTagManagement();
}

let adminCartData = {};  // {id: {dish_name, qty}}
let adminCartSSE = null;

function connectAdminCartSSE() {
    if (adminCartSSE) adminCartSSE.close();
    adminCartSSE = new EventSource('/api/cart/events');
    adminCartSSE.onmessage = function(e) {
        try {
            const data = JSON.parse(e.data);
            if (data.type === 'cart_update') {
                adminCartData = data.cart || {};
                renderSharedCart();
            }
        } catch(ex) {}
    };
    adminCartSSE.onerror = function() {
        setTimeout(connectAdminCartSSE, 3000);
    };
}

async function loadSharedCart() {
    try {
        const res = await fetch('/api/cart');
        adminCartData = await res.json();
        renderSharedCart();
    } catch(e) { console.error(e); }
}

function renderSharedCart() {
    const section = document.getElementById('sharedCartSection');
    const body = document.getElementById('sharedCartBody');
    const actions = document.getElementById('sharedCartActions');
    const ids = Object.keys(adminCartData);
    if (!ids.length) {
        section.style.display = 'none';
        return;
    }
    section.style.display = '';
    let html = '';
    for (const id of ids) {
        const item = adminCartData[id];
        html += '<div style="padding:8px 16px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;">';
        html += '<span style="font-weight:600;">' + escapeHtml(item.dish_name) + '</span>';
        html += '<span style="opacity:0.6;">&times;' + item.qty + '</span>';
        html += '</div>';
    }
    body.innerHTML = html;
}

async function clearSharedCart() {
    if (!confirm('Clear the shared cart?')) return;
    await fetch('/api/cart/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cart: {} }),
    });
}

function copyCartTextAdmin() {
    const ids = Object.keys(adminCartData);
    if (!ids.length) { showToast('Cart is empty', true); return; }
    let lines = [];
    for (const id of ids) {
        const item = adminCartData[id];
        lines.push(item.dish_name + ' × ' + item.qty);
    }
    copyToClip(lines.join('\n'), 'Cart copied!');
}

async function saveCartImageAdmin() {
    const body = document.getElementById('sharedCartBody');
    await openAsImage(body);
}

async function cartGroceryAdmin() {
    const modal = document.getElementById('cartGroceryModal');
    const ids = Object.keys(adminCartData);
    if (!ids.length) { showToast('Cart is empty', true); return; }

    // Fetch all dishes to get ingredients
    const res = await fetch('/api/dishes');
    const dishes = await res.json();

    // Aggregate ingredients
    const grocery = {};
    for (const id of ids) {
        const item = adminCartData[id];
        const dish = dishes.find(d => d.id === parseInt(id));
        if (!dish) continue;
        for (const ing of (dish.ingredients || [])) {
            const key = ing.name.toLowerCase() + '|' + (ing.unit || '');
            if (!grocery[key]) {
                grocery[key] = { name: ing.name, unit: ing.unit || '', amount: 0 };
            }
            grocery[key].amount += (ing.amount || 0) * item.qty;
        }
    }

    const items = Object.values(grocery).sort((a, b) => a.name.localeCompare(b.name));
    if (!items.length) {
        modal.innerHTML = '<p style="opacity:0.6;">No ingredients found for cart dishes.</p>';
        modal.style.display = '';
        return;
    }

    let html = '<div style="font-weight:700;margin-bottom:8px;">Grocery List (from cart)</div>';
    html += '<div id="cartGroceryList">';
    for (const g of items) {
        const amt = g.amount % 1 === 0 ? g.amount : g.amount.toFixed(1);
        html += '<div style="padding:4px 0;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;">';
        html += '<span>' + escapeHtml(g.name) + '</span>';
        html += '<span style="opacity:0.6;">' + amt + ' ' + escapeHtml(g.unit) + '</span>';
        html += '</div>';
    }
    html += '</div>';
    html += '<div style="display:flex;gap:8px;margin-top:10px;">';
    html += '<button onclick="copyCartGroceryText()" style="flex:1;padding:8px;border:none;border-radius:8px;background:#5b7bbd;color:white;font-weight:600;font-size:13px;cursor:pointer;">&#128203; Copy</button>';
    html += '<button onclick="saveCartGroceryImage()" style="flex:1;padding:8px;border:none;border-radius:8px;background:#2d9a4e;color:white;font-weight:600;font-size:13px;cursor:pointer;">&#128190; Image</button>';
    html += '</div>';
    modal.innerHTML = html;
    modal.style.display = '';
}

async function loadTagManagement() {
    try {
        const res = await fetch('/api/tags');
        const tags = await res.json();
        const body = document.getElementById('tagManageBody');
        if (!tags.length) {
            body.innerHTML = '<div style="padding:8px 16px;opacity:0.5;font-size:14px;">No tags yet. Create tags when adding dishes.</div>';
            return;
        }
        let html = '';
        for (const t of tags) {
            const checked = t.visible_in_menu ? 'checked' : '';
            html += '<div style="display:flex;align-items:center;padding:8px 16px;border-bottom:1px solid var(--border);">';
            html += '<span style="flex:1;font-weight:600;font-size:14px;">' + escapeHtml(t.name) + '</span>';
            html += '<span style="font-size:12px;opacity:0.5;margin-right:12px;">' + t.dish_count + ' dish' + (t.dish_count !== 1 ? 'es' : '') + '</span>';
            html += '<label style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text-secondary);cursor:pointer;">';
            html += '<input type="checkbox" ' + checked + ' onchange="toggleTagMenu(' + t.id + ', this.checked)" style="width:18px;height:18px;cursor:pointer;"> Menu';
            html += '</label>';
            html += '</div>';
        }
        body.innerHTML = html;
    } catch(e) {
        console.error(e);
    }
}

async function toggleTagMenu(tagId, visible) {
    try {
        await fetch('/api/tags/' + tagId + '/visibility', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ visible_in_menu: visible }),
        });
    } catch(e) {
        showToast('Failed to update tag', true);
    }
}

function copyCartGroceryText() {
    let lines = [];
    const listEl = document.getElementById('cartGroceryList');
    if (listEl) {
        listEl.querySelectorAll('div[style]').forEach(row => {
            const spans = row.querySelectorAll('span');
            if (spans.length === 2) {
                lines.push(spans[0].textContent + ' — ' + spans[1].textContent.trim());
            }
        });
    }
    if (!lines.length) { showToast('Nothing to copy', true); return; }
    copyToClip(lines.join('\n'), 'Grocery list copied!');
}

async function saveCartGroceryImage() {
    const list = document.getElementById('cartGroceryList');
    await openAsImage(list);
}

/* ===================================================================
   DISHES
   =================================================================== */
async function loadDishes() {
    try {
        const [dishRes, tagRes] = await Promise.all([fetch('/api/dishes'), fetch('/api/tags')]);
        allDishes = await dishRes.json();
        allTags = await tagRes.json();
        renderTagFilters();
        filterDishes();
    } catch(e) {
        console.error(e);
    }
}

function renderTagFilters() {
    const container = document.getElementById('tagFilters');
    if (!allTags.length) { container.innerHTML = ''; return; }
    let html = '<button class="tag-filter-btn' + (!activeTagFilter ? ' active' : '') + '" onclick="setTagFilter(null)">All</button>';
    for (const t of allTags) {
        html += '<button class="tag-filter-btn' + (activeTagFilter === t.name ? ' active' : '') + '" onclick="setTagFilter(\'' + escapeHtml(t.name).replace(/'/g, "\\\\'") + '\')">' + escapeHtml(t.name) + '</button>';
    }
    container.innerHTML = html;
}

function setTagFilter(tagName) {
    activeTagFilter = tagName;
    renderTagFilters();
    filterDishes();
}

function renderDishes(dishes) {
    const grid = document.getElementById('dishGrid');
    const empty = document.getElementById('dishesEmpty');
    if (dishes.length === 0) {
        grid.innerHTML = '';
        empty.style.display = 'block';
        return;
    }
    empty.style.display = 'none';
    grid.innerHTML = dishes.map(d => {
        const img = d.image_path
            ? '<img class="dish-card-img" src="/' + escapeHtml(d.image_path) + '" alt="">'
            : '<div class="dish-card-img-placeholder">' + dishEmoji(d.name) + '</div>';
        const ingCount = d.ingredients ? d.ingredients.length : 0;
        const tagStr = d.tags && d.tags.length ? d.tags.map(t => t.name).join(', ') : '';
        return '<div class="dish-card" onclick="navigate(\'dish-edit\',' + d.id + ')">' +
            img +
            '<div class="dish-card-body">' +
            '<div class="dish-card-name">' + escapeHtml(d.name) + '</div>' +
            '<div class="dish-card-meta">' + ingCount + ' ingredient' + (ingCount!==1?'s':'') +
            (tagStr ? ' · ' + escapeHtml(tagStr) : '') +
            '</div></div></div>';
    }).join('');
}

function filterDishes() {
    const q = document.getElementById('dishSearchInput').value.toLowerCase();
    let filtered = allDishes.filter(d => d.name.toLowerCase().includes(q));
    if (activeTagFilter) {
        filtered = filtered.filter(d => d.tags && d.tags.some(t => t.name === activeTagFilter));
    }
    renderDishes(filtered);
}

/* ===================================================================
   DISH FORM
   =================================================================== */
function resetDishForm() {
    document.getElementById('dishName').value = '';
    document.getElementById('dishNotes').value = '';
    document.getElementById('dishImage').value = '';
    document.getElementById('imagePreview').innerHTML =
        '<div style="font-size:36px;margin-bottom:4px;">&#128247;</div><div class="upload-text">Tap to add a photo</div>';
    document.getElementById('ingredientsEditor').innerHTML = '';
    document.getElementById('dishDeleteBtn').style.display = 'none';
    document.getElementById('dishFormSubmit').textContent = 'Save Dish';
    selectedFormTags = [];
    renderFormTags();
    // Note images: hide for new dish (no dish_id yet to upload to)
    document.getElementById('noteImagesGrid').innerHTML = '';
    document.getElementById('noteImagesSection').style.display = 'none';
    document.getElementById('addNoteImgBtn').style.display = 'none';
    addIngredientRow();
}

async function loadDishForEdit(id) {
    try {
        const res = await fetch('/api/dishes/' + id);
        const dish = await res.json();
        document.getElementById('dishName').value = dish.name;
        document.getElementById('dishNotes').value = dish.notes || '';
        autoResizeTextarea(document.getElementById('dishNotes'));
        document.getElementById('dishImage').value = '';

        if (dish.image_path) {
            document.getElementById('imagePreview').innerHTML =
                '<img src="/' + escapeHtml(dish.image_path) + '" alt="">' +
                '<button type="button" class="remove-image-btn" onclick="removeCurrentImage(event)">&times;</button>';
        } else {
            document.getElementById('imagePreview').innerHTML =
                '<div style="font-size:36px;margin-bottom:4px;">&#128247;</div><div class="upload-text">Tap to add a photo</div>';
        }

        const editor = document.getElementById('ingredientsEditor');
        editor.innerHTML = '';
        if (dish.ingredients && dish.ingredients.length > 0) {
            dish.ingredients.forEach(ing => addIngredientRow(ing));
        } else {
            addIngredientRow();
        }

        // Load tags
        selectedFormTags = dish.tags ? dish.tags.map(t => t.name) : [];
        renderFormTags();

        // Load note images
        renderNoteImages(dish.note_images || []);
        document.getElementById('addNoteImgBtn').style.display = '';

        document.getElementById('dishDeleteBtn').style.display = 'block';
        document.getElementById('dishFormSubmit').textContent = 'Update Dish';
    } catch(e) {
        showToast('Failed to load dish', true);
        navigate('dishes');
    }
}

function addIngredientRow(data) {
    const editor = document.getElementById('ingredientsEditor');
    const row = document.createElement('div');
    row.className = 'ing-row';
    const unitOptions = UNITS.map(u => '<option value="' + u + '"' + (data && data.unit === u ? ' selected' : '') + '>' + u + '</option>').join('');
    row.innerHTML =
        '<input class="ing-name" type="text" placeholder="Ingredient" value="' + (data ? escapeHtml(data.name) : '') + '">' +
        '<input class="ing-amount" type="number" step="any" min="0" placeholder="Amt" value="' + (data ? data.amount : '') + '">' +
        '<select class="ing-unit">' + unitOptions + '</select>' +
        '<button type="button" class="ing-remove" onclick="this.parentElement.remove()">&times;</button>';
    editor.appendChild(row);
}

function getIngredients() {
    const rows = document.querySelectorAll('#ingredientsEditor .ing-row');
    const ings = [];
    rows.forEach(r => {
        const name = r.querySelector('.ing-name').value.trim();
        const amount = parseFloat(r.querySelector('.ing-amount').value);
        const unit = r.querySelector('.ing-unit').value;
        if (name && !isNaN(amount) && amount > 0) {
            ings.push({ name, amount, unit });
        }
    });
    return ings;
}

function autoResizeTextarea(el) {
    el.style.height = 'auto';
    const maxH = window.innerHeight * 0.5;
    el.style.height = Math.min(el.scrollHeight, maxH) + 'px';
}

function previewImage(input) {
    if (input.files && input.files[0]) {
        const reader = new FileReader();
        reader.onload = function(e) {
            document.getElementById('imagePreview').innerHTML =
                '<img src="' + e.target.result + '" alt="">' +
                '<button type="button" class="remove-image-btn" onclick="removeCurrentImage(event)">&times;</button>';
        };
        reader.readAsDataURL(input.files[0]);
    }
}

function removeCurrentImage(e) {
    e.stopPropagation();
    removeImageFlag = true;
    document.getElementById('dishImage').value = '';
    document.getElementById('imagePreview').innerHTML =
        '<div style="font-size:36px;margin-bottom:4px;">&#128247;</div><div class="upload-text">Tap to add a photo</div>';
}

async function saveDish(e) {
    e.preventDefault();
    const name = document.getElementById('dishName').value.trim();
    if (!name) return;

    const ingredients = getIngredients();
    const formData = new FormData();
    formData.append('name', name);
    formData.append('notes', document.getElementById('dishNotes').value);
    formData.append('ingredients', JSON.stringify(ingredients));
    formData.append('tags', JSON.stringify(selectedFormTags));

    const imageInput = document.getElementById('dishImage');
    if (imageInput.files && imageInput.files[0]) {
        formData.append('image', imageInput.files[0]);
    }

    if (editingDishId && removeImageFlag) {
        formData.append('remove_image', 'true');
    }

    const url = editingDishId ? '/api/dishes/' + editingDishId : '/api/dishes';
    const method = editingDishId ? 'PUT' : 'POST';

    try {
        const res = await fetch(url, { method, body: formData });
        if (!res.ok) {
            const err = await res.json();
            showToast(err.detail || 'Failed to save', true);
            return;
        }
        showToast(editingDishId ? 'Dish updated!' : 'Dish created!');
        navigate('dishes');
    } catch(e) {
        showToast('Network error', true);
    }
}

/* ===================================================================
   TAG FORM FUNCTIONS
   =================================================================== */
function renderFormTags() {
    const container = document.getElementById('tagsSelected');
    container.innerHTML = selectedFormTags.map((t, i) =>
        '<span class="tag-chip">' + escapeHtml(t) +
        '<span class="tag-remove" onclick="removeFormTag(' + i + ')">&times;</span></span>'
    ).join('');
}

function removeFormTag(index) {
    selectedFormTags.splice(index, 1);
    renderFormTags();
}

function addFormTag(name) {
    name = name.trim();
    if (!name || selectedFormTags.includes(name)) return;
    selectedFormTags.push(name);
    renderFormTags();
    document.getElementById('tagInput').value = '';
    hideTagSuggestions();
}

function renderTagRow(t) {
    const eName = escapeHtml(t.name).replace(/'/g, "\\\\'");
    let html = '<div class="tag-suggestion" style="display:flex;align-items:center;">';
    html += '<span style="flex:1;cursor:pointer;" onclick="addFormTag(\'' + eName + '\')">' + escapeHtml(t.name) + '</span>';
    if (t.dish_count === 0) {
        html += '<span class="tag-delete-btn" onclick="event.stopPropagation();deleteTag(' + t.id + ',\'' + eName + '\')" title="Delete unused tag" style="cursor:pointer;color:var(--danger);font-size:18px;padding:0 4px;opacity:0.5;">&times;</span>';
    }
    html += '</div>';
    return html;
}

function filterTagSuggestions() {
    const input = document.getElementById('tagInput');
    const q = input.value.trim().toLowerCase();
    const container = document.getElementById('tagSuggestions');

    if (!q) { hideTagSuggestions(); return; }

    const matches = allTags
        .filter(t => t.name.toLowerCase().includes(q) && !selectedFormTags.includes(t.name))
        .slice(0, 8);

    let html = matches.map(t => renderTagRow(t)).join('');

    const exactMatch = allTags.some(t => t.name.toLowerCase() === q);
    if (!exactMatch && q.length > 0) {
        html += '<div class="tag-suggestion create-new" onclick="addFormTag(\'' + escapeHtml(input.value.trim()).replace(/'/g, "\\\\'") + '\')">+ Create "' + escapeHtml(input.value.trim()) + '"</div>';
    }

    if (html) {
        container.innerHTML = html;
        container.classList.add('open');
    } else {
        hideTagSuggestions();
    }
}

function showTagSuggestions() {
    const q = document.getElementById('tagInput').value.trim();
    if (q) { filterTagSuggestions(); return; }

    const available = allTags.filter(t => !selectedFormTags.includes(t.name)).slice(0, 10);
    if (!available.length) return;

    const container = document.getElementById('tagSuggestions');
    container.innerHTML = available.map(t => renderTagRow(t)).join('');
    container.classList.add('open');
}

async function deleteTag(tagId, tagName) {
    try {
        const res = await fetch('/api/tags/' + tagId, { method: 'DELETE' });
        if (!res.ok) {
            const err = await res.json();
            showToast(err.detail || 'Cannot delete tag', true);
            return;
        }
        allTags = allTags.filter(t => t.id !== tagId);
        showToast('Tag deleted');
        renderTagFilters();
        // Refresh or close dropdown
        const available = allTags.filter(t => !selectedFormTags.includes(t.name));
        const input = document.getElementById('tagInput');
        const q = input ? input.value.trim() : '';
        if (q) {
            filterTagSuggestions();
        } else if (available.length > 0) {
            showTagSuggestions();
        } else {
            hideTagSuggestions();
        }
    } catch(e) {
        showToast('Failed to delete tag', true);
    }
}

function hideTagSuggestions() {
    document.getElementById('tagSuggestions').classList.remove('open');
}

// Close suggestions when clicking outside
document.addEventListener('click', function(e) {
    if (!e.target.closest('.tags-input-wrap')) {
        hideTagSuggestions();
    }
});

/* ===================================================================
   NOTE IMAGES
   =================================================================== */
let currentNoteImages = [];

function renderNoteImages(images) {
    currentNoteImages = images || [];
    const grid = document.getElementById('noteImagesGrid');
    const section = document.getElementById('noteImagesSection');
    if (!currentNoteImages.length) {
        section.style.display = 'none';
        grid.innerHTML = '';
        return;
    }
    section.style.display = '';
    grid.innerHTML = currentNoteImages.map((ni, i) =>
        '<div class="note-img-thumb">' +
        '<img src="/' + escapeHtml(ni.image_path) + '" onclick="openNoteImgPopup(' + i + ')">' +
        '<button class="note-img-remove" type="button" onclick="confirmDeleteNoteImage(' + ni.id + ')">&times;</button>' +
        '</div>'
    ).join('');
}

async function uploadNoteImages(input) {
    if (!editingDishId) {
        showToast('Save the dish first, then add note photos', true);
        return;
    }
    const files = input.files;
    if (!files.length) return;

    for (const file of files) {
        const form = new FormData();
        form.append('image', file);
        try {
            const res = await fetch('/api/dishes/' + editingDishId + '/note-images', {
                method: 'POST', body: form });
            if (!res.ok) { showToast('Upload failed', true); continue; }
            const data = await res.json();
            currentNoteImages.push(data);
        } catch(e) {
            showToast('Upload error', true);
        }
    }
    renderNoteImages(currentNoteImages);
    input.value = '';
}

function confirmDeleteNoteImage(imageId) {
    if (!confirm('Delete this note photo?')) return;
    deleteNoteImage(imageId);
}

async function deleteNoteImage(imageId) {
    try {
        const res = await fetch('/api/note-images/' + imageId, { method: 'DELETE' });
        if (!res.ok) { showToast('Delete failed', true); return; }
        currentNoteImages = currentNoteImages.filter(ni => ni.id !== imageId);
        renderNoteImages(currentNoteImages);
    } catch(e) {
        showToast('Delete error', true);
    }
}

let noteImgPopupIndex = 0;
let noteImgTouchStartX = 0;
let noteImgTouchMoved = false;

function openNoteImgPopup(index) {
    noteImgPopupIndex = index;
    updateNoteImgPopup();
    const popup = document.getElementById('noteImgPopup');
    popup.classList.add('open');
}

function updateNoteImgPopup() {
    if (noteImgPopupIndex < 0) noteImgPopupIndex = 0;
    if (noteImgPopupIndex >= currentNoteImages.length) noteImgPopupIndex = currentNoteImages.length - 1;
    const ni = currentNoteImages[noteImgPopupIndex];
    if (!ni) return;
    document.getElementById('noteImgPopupImg').src = '/' + ni.image_path;
    // Update counter
    const counter = document.getElementById('noteImgCounter');
    if (counter) {
        if (currentNoteImages.length > 1) {
            counter.textContent = (noteImgPopupIndex + 1) + ' / ' + currentNoteImages.length;
            counter.style.display = '';
        } else {
            counter.style.display = 'none';
        }
    }
    // Show/hide nav arrows
    const prevBtn = document.getElementById('noteImgPrev');
    const nextBtn = document.getElementById('noteImgNext');
    if (prevBtn) prevBtn.style.display = noteImgPopupIndex > 0 ? '' : 'none';
    if (nextBtn) nextBtn.style.display = noteImgPopupIndex < currentNoteImages.length - 1 ? '' : 'none';
}

function noteImgPrev() { noteImgPopupIndex--; updateNoteImgPopup(); }
function noteImgNext() { noteImgPopupIndex++; updateNoteImgPopup(); }

function closeNoteImgPopup() {
    document.getElementById('noteImgPopup').classList.remove('open');
}

// Swipe support for note image popup
document.addEventListener('DOMContentLoaded', function() {
    const popup = document.getElementById('noteImgPopup');
    if (!popup) return;
    popup.addEventListener('touchstart', function(e) {
        noteImgTouchStartX = e.touches[0].clientX;
        noteImgTouchMoved = false;
    }, { passive: true });
    popup.addEventListener('touchmove', function(e) {
        const dx = e.touches[0].clientX - noteImgTouchStartX;
        if (Math.abs(dx) > 30) noteImgTouchMoved = true;
    }, { passive: true });
    popup.addEventListener('touchend', function(e) {
        if (!noteImgTouchMoved) return;
        const dx = e.changedTouches[0].clientX - noteImgTouchStartX;
        if (dx > 50 && noteImgPopupIndex > 0) {
            noteImgPrev();
        } else if (dx < -50 && noteImgPopupIndex < currentNoteImages.length - 1) {
            noteImgNext();
        }
    }, { passive: true });
});

async function deleteDish() {
    if (!editingDishId) return;
    if (!confirm('Delete this dish? This will also remove it from any meal plans.')) return;
    try {
        const res = await fetch('/api/dishes/' + editingDishId, { method: 'DELETE' });
        if (!res.ok) {
            showToast('Failed to delete', true);
            return;
        }
        showToast('Dish deleted');
        navigate('dishes');
    } catch(e) {
        showToast('Network error', true);
    }
}

/* ===================================================================
   WEEKLY PLANNER
   =================================================================== */
async function loadPlan() {
    const weekStr = formatDate(currentWeek);
    updateWeekLabel();

    // Connect SSE
    connectSSE(weekStr);

    try {
        const [planRes, dishRes] = await Promise.all([
            fetch('/api/plan?week=' + weekStr),
            fetch('/api/dishes')
        ]);
        currentPlan = await planRes.json();
        allDishes = await dishRes.json();
        renderPlan();
    } catch(e) {
        console.error(e);
    }
}

function renderPlan() {
    const body = document.getElementById('planBody');
    let html = '';
    for (let d = 0; d < 7; d++) {
        const dayDate = new Date(currentWeek);
        dayDate.setDate(dayDate.getDate() + d);
        html += '<div class="plan-day">';
        html += '<div class="plan-day-header"><span>' + DAY_NAMES[d] + '</span><span class="day-date">' + formatDateShort(dayDate) + '</span></div>';
        html += '<div class="plan-meals">';
        for (const mt of ['lunch','dinner']) {
            const meals = currentPlan.filter(p => p.day_of_week === d && p.meal_type === mt);
            html += '<div class="plan-meal-slot" data-day="' + d + '" data-meal="' + mt + '" ondragover="onDragOver(event)" ondrop="onDrop(event)">';
            html += '<div class="meal-type-label">' + mt.charAt(0).toUpperCase() + mt.slice(1) + '</div>';
            html += '<div class="meal-slot-content">';
            if (meals.length === 0) {
                html += '<div class="meal-slot-empty" onclick="openDishPicker(' + d + ',\'' + mt + '\')">+ Add dish</div>';
            } else {
                for (const m of meals) {
                    const img = m.dish_image
                        ? '<img class="planned-dish-img" src="/' + escapeHtml(m.dish_image) + '" alt="">'
                        : '<div class="planned-dish-img-placeholder">' + dishEmoji(m.dish_name) + '</div>';
                    html += '<div class="planned-dish" draggable="true" data-plan-id="' + m.id + '" data-dish-id="' + m.dish_id + '" data-servings="' + m.servings + '" data-version="' + m.version + '" data-day="' + d + '" data-meal="' + mt + '" ondragstart="onDragStart(event)" ontouchstart="onTouchDragStart(event)" onclick="editPlannedDish(' + m.id + ',' + m.dish_id + ',\'' + escapeHtml(m.dish_name) + '\',' + m.servings + ',' + m.version + ')">' +
                        img +
                        '<div class="planned-dish-info">' +
                        '<div class="planned-dish-name">' + escapeHtml(m.dish_name) + '</div>' +
                        '<div class="planned-dish-servings">' + m.servings + ' serving' + (m.servings!==1?'s':'') + '</div>' +
                        '</div></div>';
                }
                html += '<div class="meal-slot-empty" onclick="openDishPicker(' + d + ',\'' + mt + '\')" style="margin-top:4px;font-size:12px;">+ Add another</div>';
            }
            html += '</div></div>';
        }
        html += '</div></div>';
    }
    body.innerHTML = html;
}

/* ===================================================================
   DRAG & DROP for meal plan
   =================================================================== */
let dragData = null;
let touchDragEl = null;
let touchDragClone = null;
let touchMoved = false;

// Desktop drag
function onDragStart(e) {
    const el = e.target.closest('.planned-dish');
    if (!el) return;
    dragData = {
        planId: parseInt(el.dataset.planId),
        dishId: parseInt(el.dataset.dishId),
        servings: parseInt(el.dataset.servings),
        version: parseInt(el.dataset.version),
        fromDay: parseInt(el.dataset.day),
        fromMeal: el.dataset.meal,
    };
    el.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', '');
}

function onDragOver(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    const slot = e.target.closest('.plan-meal-slot');
    // Clear all drag-over states first
    document.querySelectorAll('.plan-meal-slot.drag-over').forEach(s => s.classList.remove('drag-over'));
    if (slot) slot.classList.add('drag-over');
}

function onDrop(e) {
    e.preventDefault();
    document.querySelectorAll('.plan-meal-slot.drag-over').forEach(s => s.classList.remove('drag-over'));
    document.querySelectorAll('.planned-dish.dragging').forEach(s => s.classList.remove('dragging'));
    if (!dragData) return;

    const slot = e.target.closest('.plan-meal-slot');
    if (!slot) { dragData = null; return; }

    const toDay = parseInt(slot.dataset.day);
    const toMeal = slot.dataset.meal;

    // Don't move to same slot
    if (toDay === dragData.fromDay && toMeal === dragData.fromMeal) {
        dragData = null;
        return;
    }

    moveMealPlan(dragData, toDay, toMeal);
    dragData = null;
}

// Touch drag for mobile
let touchHoldTimer = null;
let touchDragActive = false;

function onTouchDragStart(e) {
    touchMoved = false;
    touchDragActive = false;
    const el = e.target.closest('.planned-dish');
    if (!el) return;

    const touch = e.touches[0];
    const startX = touch.clientX;
    const startY = touch.clientY;
    touchDragEl = el;

    // Use a hold timer — 300ms hold activates drag mode.
    // This avoids conflict with scrolling.
    touchHoldTimer = setTimeout(function() {
        touchDragActive = true;
        dragData = {
            planId: parseInt(el.dataset.planId),
            dishId: parseInt(el.dataset.dishId),
            servings: parseInt(el.dataset.servings),
            version: parseInt(el.dataset.version),
            fromDay: parseInt(el.dataset.day),
            fromMeal: el.dataset.meal,
        };
        // Create clone immediately on hold
        touchDragClone = el.cloneNode(true);
        touchDragClone.style.cssText = 'position:fixed;z-index:9999;pointer-events:none;opacity:0.85;width:' + el.offsetWidth + 'px;transform:scale(1.05);box-shadow:0 8px 24px rgba(0,0,0,0.25);border-radius:10px;';
        touchDragClone.style.left = (startX - 30) + 'px';
        touchDragClone.style.top = (startY - 30) + 'px';
        document.body.appendChild(touchDragClone);
        el.classList.add('dragging');
        // Haptic feedback if available
        if (navigator.vibrate) navigator.vibrate(30);
    }, 300);

    function onTouchMove(ev) {
        const t = ev.touches[0];
        const dx = Math.abs(t.clientX - startX);
        const dy = Math.abs(t.clientY - startY);

        if (!touchDragActive) {
            // If moved before hold timer fires, cancel drag — let scroll happen
            if (dx > 8 || dy > 8) {
                cleanup(false);
            }
            return;
        }

        // Drag is active — prevent scroll
        ev.preventDefault();
        touchMoved = true;

        if (touchDragClone) {
            touchDragClone.style.left = (t.clientX - 30) + 'px';
            touchDragClone.style.top = (t.clientY - 30) + 'px';
        }

        // Highlight drop target
        document.querySelectorAll('.plan-meal-slot.drag-over').forEach(s => s.classList.remove('drag-over'));
        // Temporarily hide clone to find element underneath
        if (touchDragClone) touchDragClone.style.display = 'none';
        const target = document.elementFromPoint(t.clientX, t.clientY);
        if (touchDragClone) touchDragClone.style.display = '';
        if (target) {
            const slot = target.closest('.plan-meal-slot');
            if (slot) slot.classList.add('drag-over');
        }
    }

    function cleanup(doDrop) {
        clearTimeout(touchHoldTimer);
        touchHoldTimer = null;
        document.removeEventListener('touchmove', onTouchMove);
        document.removeEventListener('touchend', onTouchEnd);
        document.removeEventListener('touchcancel', onTouchCancel);
        document.querySelectorAll('.plan-meal-slot.drag-over').forEach(s => s.classList.remove('drag-over'));

        if (touchDragClone) {
            touchDragClone.remove();
            touchDragClone = null;
        }
        if (touchDragEl) {
            touchDragEl.classList.remove('dragging');
        }

        if (!doDrop) {
            dragData = null;
            touchDragEl = null;
            touchDragActive = false;
        }
    }

    function onTouchEnd(ev) {
        const wasDragging = touchDragActive && touchMoved && dragData;
        const savedDragData = dragData ? Object.assign({}, dragData) : null;
        cleanup(false);

        if (!wasDragging || !savedDragData) return;

        const t = ev.changedTouches[0];
        const target = document.elementFromPoint(t.clientX, t.clientY);
        if (target) {
            const slot = target.closest('.plan-meal-slot');
            if (slot) {
                const toDay = parseInt(slot.dataset.day);
                const toMeal = slot.dataset.meal;
                if (toDay !== savedDragData.fromDay || toMeal !== savedDragData.fromMeal) {
                    moveMealPlan(savedDragData, toDay, toMeal);
                }
            }
        }
    }

    function onTouchCancel() {
        cleanup(false);
    }

    document.addEventListener('touchmove', onTouchMove, { passive: false });
    document.addEventListener('touchend', onTouchEnd);
    document.addEventListener('touchcancel', onTouchCancel);
}

async function moveMealPlan(data, toDay, toMeal) {
    const weekStr = formatDate(currentWeek);
    // Immediately hide the original dish element so no ghost remains
    const origEl = document.querySelector('.planned-dish[data-plan-id="' + data.planId + '"]');
    if (origEl) origEl.style.display = 'none';
    try {
        // Delete from old slot
        const delRes = await fetch('/api/plan/' + data.planId, { method: 'DELETE' });
        if (!delRes.ok) { showToast('Move failed (delete)', true); loadPlan(); return; }
        // Add to new slot
        const res = await fetch('/api/plan', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                week_start: weekStr,
                day_of_week: toDay,
                meal_type: toMeal,
                dish_id: data.dishId,
                servings: data.servings,
            }),
        });
        if (!res.ok) { showToast('Move failed (add)', true); loadPlan(); return; }
        // Reload plan
        const planRes = await fetch('/api/plan?week=' + weekStr);
        currentPlan = await planRes.json();
        renderPlan();
    } catch(e) {
        showToast('Move failed', true);
        loadPlan();
    }
}

function updateWeekLabel() {
    document.getElementById('weekLabel').textContent = formatWeekRange(currentWeek);
}

function changeWeek(dir) {
    currentWeek.setDate(currentWeek.getDate() + dir * 7);
    loadPlan();
}

/* SSE */
function connectSSE(weekStr) {
    if (eventSource) { eventSource.close(); }
    eventSource = new EventSource('/api/plan/events?week=' + weekStr);
    eventSource.onmessage = function(e) {
        const data = JSON.parse(e.data);
        if (data.type === 'plan_updated') {
            // Reload plan silently
            fetch('/api/plan?week=' + weekStr)
                .then(r => r.json())
                .then(plan => { currentPlan = plan; renderPlan(); });
        }
    };
    eventSource.onerror = function() {
        // Reconnect after a delay
        setTimeout(() => {
            if (document.getElementById('page-plan').classList.contains('active')) {
                connectSSE(weekStr);
            }
        }, 3000);
    };
}

/* Dish Picker */
let pickerActiveTag = null;

function openDishPicker(dayOfWeek, mealType) {
    servingsSlotInfo = {
        week_start: formatDate(currentWeek),
        day_of_week: dayOfWeek,
        meal_type: mealType
    };
    pickerActiveTag = null;
    document.getElementById('pickerSearch').value = '';
    renderPickerTagFilters();
    filterPicker();
    document.getElementById('dishPickerOverlay').classList.add('open');
}

function closeDishPicker() {
    document.getElementById('dishPickerOverlay').classList.remove('open');
}

function renderPickerTagFilters() {
    const container = document.getElementById('pickerTagFilters');
    // Collect unique tags
    const tagSet = {};
    for (const d of allDishes) {
        if (d.tags) d.tags.forEach(t => { tagSet[t.name] = true; });
    }
    const tags = Object.keys(tagSet).sort();
    if (!tags.length) { container.innerHTML = ''; return; }
    let html = '<button class="tag-filter-btn' + (!pickerActiveTag ? ' active' : '') + '" onclick="setPickerTagFilter(null)">All</button>';
    for (const t of tags) {
        html += '<button class="tag-filter-btn' + (pickerActiveTag === t ? ' active' : '') + '" onclick="setPickerTagFilter(\'' + escapeHtml(t).replace(/'/g, "\\\\'") + '\')">' + escapeHtml(t) + '</button>';
    }
    container.innerHTML = html;
}

function setPickerTagFilter(tag) {
    pickerActiveTag = tag;
    renderPickerTagFilters();
    filterPicker();
}

function renderPickerGrid(dishes) {
    const grid = document.getElementById('pickerGrid');
    if (dishes.length === 0) {
        grid.innerHTML = '<div class="empty-state"><p>No dishes match</p></div>';
        return;
    }
    grid.innerHTML = dishes.map(d => {
        const img = d.image_path
            ? '<img class="picker-dish-img" src="/' + escapeHtml(d.image_path) + '" alt="">'
            : '<div class="picker-dish-img-ph">' + dishEmoji(d.name) + '</div>';
        return '<div class="picker-dish">' +
            img + '<div class="picker-dish-name">' + escapeHtml(d.name) + '</div>' +
            '<button class="picker-add-btn" onclick="pickDish(' + d.id + ',\'' + escapeHtml(d.name).replace(/'/g, "\\\\'") + '\')">+</button>' +
            '</div>';
    }).join('');
}

function filterPicker() {
    const q = document.getElementById('pickerSearch').value.toLowerCase();
    let filtered = allDishes.filter(d => d.name.toLowerCase().includes(q));
    if (pickerActiveTag) {
        filtered = filtered.filter(d => d.tags && d.tags.some(t => t.name === pickerActiveTag));
    }
    renderPickerGrid(filtered);
}

async function loadDishNotes(dishId) {
    const notesEl = document.getElementById('servingsNotes');
    notesEl.style.display = 'none';
    notesEl.textContent = '';
    try {
        const dish = allDishes.find(d => d.id === dishId);
        if (dish && dish.notes) {
            notesEl.textContent = dish.notes;
            notesEl.style.display = '';
        } else {
            // Fetch if not in local cache
            const res = await fetch('/api/dishes/' + dishId);
            if (res.ok) {
                const d = await res.json();
                if (d.notes) {
                    notesEl.textContent = d.notes;
                    notesEl.style.display = '';
                }
            }
        }
    } catch(e) {}
}

function pickDish(dishId, dishName) {
    // Don't close picker — hide it behind servings overlay
    servingsDishId = dishId;
    servingsValue = 2;
    servingsMode = 'add';
    document.getElementById('servingsDishName').textContent = dishName;
    document.getElementById('servingsVal').textContent = '2';
    document.getElementById('servingsConfirmBtn').textContent = 'Add to Plan';
    document.getElementById('servingsRemoveBtn').style.display = 'none';
    document.getElementById('servingsBackBtn').style.display = '';
    document.getElementById('servingsOverlay').classList.add('open');
    loadDishNotes(dishId);
}

function editPlannedDish(planId, dishId, dishName, servings, version) {
    servingsPlanId = planId;
    servingsDishId = dishId;
    servingsValue = servings;
    servingsMode = 'edit';
    document.getElementById('servingsDishName').textContent = dishName;
    document.getElementById('servingsVal').textContent = String(servings);
    document.getElementById('servingsConfirmBtn').textContent = 'Update Servings';
    document.getElementById('servingsRemoveBtn').style.display = 'block';
    document.getElementById('servingsBackBtn').style.display = 'none';
    document.getElementById('servingsOverlay').classList.add('open');
    servingsOverlayVersion = version;
    loadDishNotes(dishId);
}

var servingsOverlayVersion = null;

function adjustServings(dir) {
    servingsValue = Math.max(1, servingsValue + dir);
    document.getElementById('servingsVal').textContent = String(servingsValue);
}

function servingsGoBack() {
    document.getElementById('servingsOverlay').classList.remove('open');
    // Picker is still open behind — just closing servings reveals it
}

function closeServingsOverlay() {
    document.getElementById('servingsOverlay').classList.remove('open');
    closeDishPicker();
}

async function confirmServings() {
    closeServingsOverlay();
    if (servingsMode === 'add') {
        try {
            const res = await fetch('/api/plan', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    week_start: servingsSlotInfo.week_start,
                    day_of_week: servingsSlotInfo.day_of_week,
                    meal_type: servingsSlotInfo.meal_type,
                    dish_id: servingsDishId,
                    servings: servingsValue
                })
            });
            if (!res.ok) {
                const err = await res.json();
                showToast(err.detail || 'Failed to add', true);
                return;
            }
            showToast('Dish added!');
            // SSE will refresh, but also reload manually
            const planRes = await fetch('/api/plan?week=' + servingsSlotInfo.week_start);
            currentPlan = await planRes.json();
            renderPlan();
        } catch(e) {
            showToast('Network error', true);
        }
    } else {
        // Edit mode — update servings
        try {
            const res = await fetch('/api/plan/' + servingsPlanId, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ servings: servingsValue, version: servingsOverlayVersion })
            });
            if (!res.ok) {
                const err = await res.json();
                if (res.status === 409) {
                    showToast('Conflict: someone else edited this. Refreshing...', true);
                } else {
                    showToast(err.detail || 'Failed to update', true);
                }
                // Reload plan
                const planRes = await fetch('/api/plan?week=' + formatDate(currentWeek));
                currentPlan = await planRes.json();
                renderPlan();
                return;
            }
            showToast('Servings updated!');
            const planRes = await fetch('/api/plan?week=' + formatDate(currentWeek));
            currentPlan = await planRes.json();
            renderPlan();
        } catch(e) {
            showToast('Network error', true);
        }
    }
}

async function removePlannedDish() {
    closeServingsOverlay();
    try {
        const res = await fetch('/api/plan/' + servingsPlanId, { method: 'DELETE' });
        if (!res.ok) {
            showToast('Failed to remove', true);
            return;
        }
        showToast('Dish removed');
        const planRes = await fetch('/api/plan?week=' + formatDate(currentWeek));
        currentPlan = await planRes.json();
        renderPlan();
    } catch(e) {
        showToast('Network error', true);
    }
}

/* ===================================================================
   PREVIEW
   =================================================================== */
async function loadPreview() {
    updatePreviewWeekLabel();
    const weekStr = formatDate(previewWeek);
    try {
        const res = await fetch('/api/plan?week=' + weekStr);
        const plan = await res.json();
        renderPreview(plan);
    } catch(e) {
        console.error(e);
    }
}

function updatePreviewWeekLabel() {
    document.getElementById('previewWeekLabel').textContent = formatWeekRange(previewWeek);
}

function changePreviewWeek(dir) {
    previewWeek.setDate(previewWeek.getDate() + dir * 7);
    loadPreview();
}

function renderPreview(plan) {
    const el = document.getElementById('previewContent');
    if (plan.length === 0) {
        el.innerHTML = '<div class="empty-state"><div class="empty-icon">&#128196;</div><p>No meals planned for this week</p></div>';
        return;
    }
    let html = '<table class="preview-table" id="previewTable"><thead><tr><th>Day</th><th>Lunch</th><th>Dinner</th></tr></thead><tbody>';
    for (let d = 0; d < 7; d++) {
        const dayDate = new Date(previewWeek);
        dayDate.setDate(dayDate.getDate() + d);
        const lunches = plan.filter(p => p.day_of_week === d && p.meal_type === 'lunch');
        const dinners = plan.filter(p => p.day_of_week === d && p.meal_type === 'dinner');
        html += '<tr>';
        html += '<td class="day-col">' + DAY_SHORT[d] + '<br><small>' + formatDateShort(dayDate) + '</small></td>';
        html += '<td>' + (lunches.length ? lunches.map(l =>
            '<div class="preview-dish-entry"><span class="preview-dish-name">' + escapeHtml(l.dish_name) + '</span><br><span class="preview-dish-servings">' + l.servings + ' servings</span></div>'
        ).join('') : '<span style="color:var(--text-secondary)">—</span>') + '</td>';
        html += '<td>' + (dinners.length ? dinners.map(l =>
            '<div class="preview-dish-entry"><span class="preview-dish-name">' + escapeHtml(l.dish_name) + '</span><br><span class="preview-dish-servings">' + l.servings + ' servings</span></div>'
        ).join('') : '<span style="color:var(--text-secondary)">—</span>') + '</td>';
        html += '</tr>';
    }
    html += '</tbody></table>';
    el.innerHTML = html;
}

function doPrint() {
    // Open a clean print window with just the active page content
    const activePage = document.querySelector('.page.active');
    if (!activePage) return;
    const content = activePage.querySelector('.preview-table, #previewContent, #groceryContent, #groceryList');
    if (!content) { window.print(); return; }
    const w = window.open('', '_blank');
    if (!w) { window.print(); return; }
    w.document.write('<html><head><title>Print</title>');
    w.document.write('<meta name="viewport" content="width=device-width,initial-scale=1">');
    w.document.write('<style>');
    w.document.write('body{font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;padding:20px;color:#000;}');
    w.document.write('table{width:100%;border-collapse:collapse;} th,td{border:1px solid #ccc;padding:8px;text-align:left;font-size:13px;vertical-align:top;}');
    w.document.write('th{background:#f5f5f5;font-weight:600;}');
    w.document.write('.grocery-item{padding:6px 0;border-bottom:1px solid #eee;display:flex;justify-content:space-between;}');
    w.document.write('.grocery-name{font-weight:600;} .grocery-amount{opacity:0.7;}');
    w.document.write('</style></head><body>');
    w.document.write(content.outerHTML);
    w.document.write('</body></html>');
    w.document.close();
    // Wait for content to render then print
    setTimeout(function() { w.print(); }, 300);
}

async function exportPreviewImage() {
    const table = document.getElementById('previewTable');
    if (!table) { showToast('Nothing to export', true); return; }
    await openAsImage(table);
}

/* ===================================================================
   GROCERY LIST
   =================================================================== */
async function loadGrocery() {
    updateGroceryWeekLabel();
    const weekStr = formatDate(groceryWeek);
    try {
        // Fetch checks and items in parallel
        const [checkRes, itemsRes] = await Promise.all([
            fetch('/api/grocery/checks?week=' + weekStr),
            fetch('/api/plan/grocery?week=' + weekStr),
        ]);
        groceryChecks = await checkRes.json();
        const items = await itemsRes.json();
        renderGrocery(items);
        connectGrocerySSE();
    } catch(e) {
        console.error(e);
    }
}

function updateGroceryWeekLabel() {
    document.getElementById('groceryWeekLabel').textContent = formatWeekRange(groceryWeek);
}

function changeGroceryWeek(dir) {
    groceryWeek.setDate(groceryWeek.getDate() + dir * 7);
    loadGrocery();
}

let groceryChecks = {};  // server-synced checks
let groceryItems = [];   // current items for re-render
let grocerySSE = null;
let ignoreSseGrocery = false;

function connectGrocerySSE() {
    if (grocerySSE) grocerySSE.close();
    const week = formatDate(groceryWeek);
    grocerySSE = new EventSource('/api/grocery/events?week=' + week);
    grocerySSE.onmessage = function(e) {
        if (ignoreSseGrocery) return;
        try {
            const data = JSON.parse(e.data);
            if (data.type === 'grocery_update') {
                groceryChecks = data.checks || {};
                applyGroceryChecks();
            }
        } catch(ex) {}
    };
    grocerySSE.onerror = function() {
        setTimeout(connectGrocerySSE, 3000);
    };
}

function applyGroceryChecks() {
    document.querySelectorAll('#groceryList .grocery-item').forEach(el => {
        const key = el.dataset.key;
        const shouldBeChecked = !!groceryChecks[key];
        const isChecked = el.classList.contains('checked');
        if (shouldBeChecked !== isChecked) {
            el.classList.toggle('checked');
            el.querySelector('.grocery-cb').classList.toggle('checked');
        }
    });
}

function renderGrocery(items) {
    groceryItems = items;
    const el = document.getElementById('groceryContent');
    if (items.length === 0) {
        el.innerHTML = '<div class="empty-state"><div class="empty-icon">&#128722;</div><p>No ingredients for this week.<br>Plan some meals first!</p></div>';
        return;
    }
    let html = '<ul class="grocery-list" id="groceryList">';
    for (const item of items) {
        const key = item.name.toLowerCase() + '|' + item.unit;
        const isChecked = groceryChecks[key] ? ' checked' : '';
        html += '<li class="grocery-item' + isChecked + '" data-key="' + escapeHtml(key) + '" onclick="toggleGroceryItem(this)">' +
            '<div class="grocery-cb' + isChecked + '">&#10003;</div>' +
            '<span class="grocery-name">' + escapeHtml(item.name) + '</span>' +
            '<span class="grocery-amount">' + item.amount + ' ' + escapeHtml(item.unit) + '</span>' +
            '</li>';
    }
    html += '</ul>';
    el.innerHTML = html;
}

function toggleGroceryItem(el) {
    const key = el.dataset.key;
    const nowChecked = !el.classList.contains('checked');
    el.classList.toggle('checked');
    el.querySelector('.grocery-cb').classList.toggle('checked');
    // Sync to server
    ignoreSseGrocery = true;
    if (nowChecked) {
        groceryChecks[key] = true;
    } else {
        delete groceryChecks[key];
    }
    fetch('/api/grocery/check', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ week: formatDate(groceryWeek), key: key, checked: nowChecked }),
    }).finally(() => { setTimeout(() => { ignoreSseGrocery = false; }, 300); });
}

async function exportGroceryImage() {
    const list = document.getElementById('groceryList');
    if (!list) { showToast('Nothing to export', true); return; }
    await openAsImage(list);
}

function copyGroceryText() {
    const items = document.querySelectorAll('#groceryList .grocery-item');
    if (!items.length) { showToast('Nothing to copy', true); return; }
    let lines = [];
    items.forEach(li => {
        const name = li.querySelector('.grocery-name').textContent;
        const amount = li.querySelector('.grocery-amount').textContent;
        lines.push(name + ' — ' + amount);
    });
    copyToClip(lines.join('\n'), 'Copied to clipboard!');
}

/* ===================================================================
   HTML ESCAPE
   =================================================================== */
function escapeHtml(str) {
    if (!str) return '';
    const d = document.createElement('div');
    d.appendChild(document.createTextNode(str));
    return d.innerHTML;
}

/* ===================================================================
   INIT
   =================================================================== */
// Initialize week defaults (Sat/Sun → next week, otherwise this week)
currentWeek = getDefaultPlanWeek();
previewWeek = getDefaultPlanWeek();
groceryWeek = getDefaultPlanWeek();

navigate('home');
</script>

<!-- Note image popup -->
<div class="note-img-popup" id="noteImgPopup" onclick="closeNoteImgPopup()">
    <button class="note-img-popup-close" onclick="event.stopPropagation();closeNoteImgPopup()">&times;</button>
    <button class="note-img-nav note-img-prev" id="noteImgPrev" onclick="event.stopPropagation();noteImgPrev()" style="display:none;">&#8249;</button>
    <img id="noteImgPopupImg" src="" alt="Note" onclick="event.stopPropagation()">
    <button class="note-img-nav note-img-next" id="noteImgNext" onclick="event.stopPropagation();noteImgNext()" style="display:none;">&#8250;</button>
    <div class="note-img-counter" id="noteImgCounter" style="display:none;"></div>
</div>

</body>
</html>"""



# ===================================================================
# GUEST MENU HTML
# ===================================================================
MENU_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Menu</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { font-size: 16px; -webkit-text-size-adjust: 100%; touch-action: manipulation; }
body {
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: #faf7f2;
    color: #2d2a24;
    min-height: 100vh;
    line-height: 1.5;
    padding-bottom: 80px;
}
button { cursor: pointer; border: none; background: none; font: inherit; }

:root {
    --bg: #faf7f2;
    --card: #ffffff;
    --primary: #e07a3a;
    --primary-dark: #c4622a;
    --primary-light: #fdf0e8;
    --green: #5a9e6f;
    --green-light: #eef6f0;
    --text: #2d2a24;
    --text-secondary: #7a756c;
    --border: #e8e3db;
    --shadow: 0 2px 8px rgba(0,0,0,0.08);
    --shadow-lg: 0 8px 32px rgba(0,0,0,0.12);
    --radius: 12px;
    --radius-sm: 8px;
}

/* Header */
.menu-header {
    background: linear-gradient(135deg, #e07a3a, #d45d8a);
    color: white;
    text-align: center;
    padding: 32px 16px 24px;
}
.menu-header h1 { font-size: 28px; font-weight: 800; margin-bottom: 4px; }
.menu-header p { opacity: 0.9; font-size: 15px; }

/* Search */
.menu-search {
    max-width: 600px;
    margin: -18px auto 0;
    padding: 0 16px;
    position: relative;
    z-index: 2;
}
.menu-search input {
    width: 100%;
    padding: 12px 16px 12px 40px;
    border: none;
    border-radius: 24px;
    font-size: 15px;
    background: var(--card);
    box-shadow: var(--shadow-lg);
    outline: none;
}
.menu-search .search-icon {
    position: absolute;
    left: 30px;
    top: 50%;
    transform: translateY(-50%);
    color: var(--text-secondary);
    font-size: 16px;
    pointer-events: none;
}

/* Menu grid */
.menu-container { max-width: 600px; margin: 0 auto; padding: 8px 16px 20px; }
.menu-grid {
    display: flex;
    flex-direction: column;
    gap: 6px;
}
.menu-item {
    display: flex;
    align-items: center;
    background: var(--card);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    overflow: hidden;
    padding: 10px;
    gap: 10px;
}
.menu-item-img-wrap {
    flex-shrink: 0;
    width: 80px; height: 80px;
    border-radius: 10px;
    overflow: hidden;
    cursor: pointer;
}
.menu-item-img {
    width: 80px; height: 80px;
    object-fit: cover;
    display: block;
    background: var(--border);
}
.menu-item-img-ph {
    width: 80px; height: 80px;
    background: linear-gradient(135deg, var(--primary-light), var(--green-light));
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 28px;
}
.menu-item-body { flex: 1; min-width: 0; }
.menu-item-name { font-weight: 700; font-size: 14px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

/* Quantity controls - compact */
.menu-item-qty {
    flex-shrink: 0;
    display: flex;
    align-items: center;
}

/* Image popup */
.img-popup-overlay {
    display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.85); z-index: 9999;
    align-items: center; justify-content: center;
}
.img-popup-overlay.open { display: flex; }
.img-popup-overlay img {
    max-width: 90vw; max-height: 85vh; border-radius: 12px; object-fit: contain;
}
.img-popup-close {
    position: absolute; top: 16px; right: 16px;
    width: 40px; height: 40px; border-radius: 50%;
    background: rgba(255,255,255,0.2); border: none;
    color: white; font-size: 24px; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
}
.qty-controls {
    display: flex;
    align-items: center;
    gap: 6px;
}
.qty-btn {
    width: 30px; height: 30px;
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 16px; font-weight: 700;
    border: none; cursor: pointer;
    min-width: 30px; min-height: 30px;
}
.qty-btn.minus { background: var(--border); color: var(--text-secondary); }
.qty-btn.plus { background: var(--primary); color: white; }
.qty-val { font-size: 14px; font-weight: 700; min-width: 20px; text-align: center; }

/* Floating cart */
.cart-fab {
    position: fixed;
    bottom: 20px;
    right: 20px;
    width: 60px; height: 60px;
    border-radius: 50%;
    background: var(--primary);
    color: white;
    font-size: 26px;
    display: flex; align-items: center; justify-content: center;
    box-shadow: var(--shadow-lg);
    z-index: 100;
    transition: transform 0.15s;
}
.cart-fab:active { transform: scale(0.92); }
.cart-badge {
    position: absolute;
    top: -4px; right: -4px;
    background: #c33;
    color: white;
    font-size: 12px;
    font-weight: 700;
    min-width: 22px; height: 22px;
    border-radius: 11px;
    display: flex; align-items: center; justify-content: center;
    padding: 0 5px;
}

/* Cart overlay */
.cart-overlay {
    position: fixed;
    inset: 0;
    z-index: 200;
    display: none;
}
.cart-overlay.open { display: flex; flex-direction: column; }
.cart-backdrop {
    position: absolute; inset: 0;
    background: rgba(0,0,0,0.4);
}
.cart-panel {
    position: relative;
    margin-top: auto;
    background: var(--card);
    border-radius: 20px 20px 0 0;
    max-height: 85vh;
    display: flex;
    flex-direction: column;
    z-index: 1;
    animation: cartSlideUp 0.25s ease-out;
}
@keyframes cartSlideUp {
    from { transform: translateY(100%); }
    to { transform: translateY(0); }
}
.cart-header {
    padding: 16px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
}
.cart-header h3 { font-size: 18px; font-weight: 700; }
.cart-close {
    width: 32px; height: 32px;
    border-radius: 50%;
    background: var(--bg);
    display: flex; align-items: center; justify-content: center;
    font-size: 18px;
}
.cart-body {
    flex: 1;
    overflow-y: auto;
    padding: 16px;
    -webkit-overflow-scrolling: touch;
}
.cart-item {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 0;
    border-bottom: 1px solid var(--border);
}
.cart-item:last-child { border-bottom: none; }
.cart-item-name { flex: 1; font-weight: 600; font-size: 14px; }
.cart-item-qty { display: flex; align-items: center; gap: 8px; }
.cart-item-qty .qty-btn { width: 28px; height: 28px; min-width: 28px; min-height: 28px; font-size: 14px; }
.cart-item-qty .qty-val { font-size: 14px; min-width: 20px; }
.cart-item-remove {
    color: #c33; font-size: 18px; padding: 4px;
    cursor: pointer;
}
.cart-summary {
    padding: 12px 0;
    border-top: 1px solid var(--border);
    margin-top: 8px;
    font-weight: 700;
    font-size: 15px;
    color: var(--text-secondary);
}
.cart-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    padding-top: 12px;
}
.cart-action-btn {
    flex: 1;
    min-width: 100px;
    padding: 12px 10px;
    border-radius: var(--radius-sm);
    font-weight: 600;
    font-size: 13px;
    text-align: center;
    display: flex; align-items: center; justify-content: center; gap: 4px;
    color: white;
}
.cart-action-btn.copy-btn { background: #5b7bbd; }
.cart-action-btn.img-btn { background: var(--green); }
.cart-action-btn.grocery-btn { background: var(--primary); }
.cart-action-btn.clear-btn { background: #c33; }

.cart-empty {
    text-align: center;
    padding: 40px 20px;
    color: var(--text-secondary);
}
.cart-empty .empty-icon { font-size: 48px; margin-bottom: 12px; }

/* Grocery modal inside cart */
.grocery-modal {
    display: none;
    margin-top: 16px;
    padding: 16px;
    background: var(--bg);
    border-radius: var(--radius);
}
.grocery-modal.open { display: block; }
.grocery-modal h4 { font-size: 15px; font-weight: 700; margin-bottom: 10px; }
.grocery-modal-list { list-style: none; }
.grocery-modal-item {
    display: flex;
    justify-content: space-between;
    padding: 6px 0;
    border-bottom: 1px solid var(--border);
    font-size: 14px;
}
.grocery-modal-item:last-child { border-bottom: none; }
.grocery-modal-actions {
    display: flex; gap: 8px; margin-top: 12px;
}
.grocery-modal-actions button {
    flex: 1;
    padding: 10px;
    border-radius: var(--radius-sm);
    font-weight: 600;
    font-size: 13px;
    color: white;
    display: flex; align-items: center; justify-content: center; gap: 4px;
}

/* Toast */
.toast-container {
    position: fixed;
    top: 16px;
    left: 50%; transform: translateX(-50%);
    z-index: 300;
    display: flex;
    flex-direction: column;
    gap: 8px;
    pointer-events: none;
}
.toast {
    background: var(--text);
    color: white;
    padding: 12px 20px;
    border-radius: 24px;
    font-size: 14px;
    font-weight: 600;
    box-shadow: var(--shadow-lg);
    animation: toastIn 0.3s ease-out;
    pointer-events: auto;
}
@keyframes toastIn {
    from { opacity: 0; transform: translateY(-10px); }
    to { opacity: 1; transform: translateY(0); }
}

.menu-empty {
    text-align: center;
    padding: 60px 20px;
    color: var(--text-secondary);
}
.menu-empty .empty-icon { font-size: 48px; margin-bottom: 12px; }
</style>
</head>
<body>

<div class="toast-container" id="toastContainer"></div>

<div class="menu-header">
    <h1>Our Menu</h1>
    <p>Browse dishes and build your order</p>
</div>

<div class="menu-search">
    <span class="search-icon">&#128269;</span>
    <input type="text" placeholder="Search dishes..." id="menuSearchInput" oninput="filterMenu()">
</div>
<div class="menu-tag-filters" id="menuTagFilters" style="display:flex;gap:6px;flex-wrap:wrap;padding:8px 16px 0;"></div>

<div class="menu-container">
    <div class="menu-grid" id="menuGrid"></div>
    <div class="menu-empty" id="menuEmpty" style="display:none;">
        <div class="empty-icon">&#127869;</div>
        <p>No dishes available</p>
    </div>
</div>

<!-- Floating cart button -->
<button class="cart-fab" id="cartFab" onclick="openCart()" style="display:none;">
    &#128722;
    <span class="cart-badge" id="cartBadge">0</span>
</button>

<!-- Cart overlay -->
<div class="cart-overlay" id="cartOverlay">
    <div class="cart-backdrop" onclick="closeCart()"></div>
    <div class="cart-panel">
        <div class="cart-header">
            <h3>Your Cart</h3>
            <button class="cart-close" onclick="closeCart()">&times;</button>
        </div>
        <div class="cart-body" id="cartBody"></div>
    </div>
</div>

<script>
let allMenuDishes = [];
let cart = {};  // { dishId: { dish, qty } }
let menuActiveTag = null;

// Load cart from localStorage
try {
    const saved = localStorage.getItem('mealPlannerCart');
    if (saved) cart = JSON.parse(saved);
} catch(e) {}

function saveCart() {
    localStorage.setItem('mealPlannerCart', JSON.stringify(cart));
    updateCartBadge();
}

let syncDebounce = null;
let ignoreSseUpdate = false;
function syncCartToServer() {
    clearTimeout(syncDebounce);
    ignoreSseUpdate = true;
    syncDebounce = setTimeout(() => {
        // Build a serializable cart (strip dish object, keep name)
        const sendCart = {};
        for (const id in cart) {
            sendCart[id] = {
                dish_name: cart[id].dish ? cart[id].dish.name : ('Dish #' + id),
                qty: cart[id].qty,
            };
        }
        fetch('/api/cart/update', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ cart: sendCart }),
        }).finally(() => {
            setTimeout(() => { ignoreSseUpdate = false; }, 500);
        });
    }, 200);
}

function updateCartBadge() {
    let total = 0;
    for (const id in cart) total += cart[id].qty;
    const badge = document.getElementById('cartBadge');
    const fab = document.getElementById('cartFab');
    badge.textContent = String(total);
    fab.style.display = total > 0 ? 'flex' : 'none';
}

async function openAsImageGuest(element) {
    if (typeof html2canvas === 'undefined') {
        const s = document.createElement('script');
        s.src = 'https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js';
        document.head.appendChild(s);
        await new Promise(r => s.onload = r);
    }
    try {
        const canvas = await html2canvas(element, { backgroundColor: '#ffffff', scale: 2 });
        const dataUrl = canvas.toDataURL('image/png');
        const w = window.open('');
        if (w) {
            w.document.write('<html><head><title>Image</title><meta name="viewport" content="width=device-width,initial-scale=1"></head><body style="margin:0;display:flex;justify-content:center;align-items:center;min-height:100vh;background:#000;"><img src="' + dataUrl + '" style="max-width:100%;height:auto;"></body></html>');
            w.document.close();
        } else {
            showToast('Pop-up blocked — allow pop-ups');
        }
    } catch(e) { showToast('Failed to generate image'); }
}

function showToast(msg) {
    const container = document.getElementById('toastContainer');
    const el = document.createElement('div');
    el.className = 'toast';
    el.textContent = msg;
    container.appendChild(el);
    setTimeout(() => { el.remove(); }, 3000);
}

function escapeHtml(str) {
    if (!str) return '';
    const d = document.createElement('div');
    d.appendChild(document.createTextNode(str));
    return d.innerHTML;
}

function dishEmoji(name) {
    const n = name.toLowerCase();
    if (n.includes('chicken')) return '&#127831;';
    if (n.includes('salad')) return '&#129367;';
    if (n.includes('pasta') || n.includes('noodle')) return '&#127837;';
    if (n.includes('pizza')) return '&#127829;';
    if (n.includes('soup')) return '&#127858;';
    if (n.includes('fish') || n.includes('salmon') || n.includes('tuna')) return '&#127843;';
    if (n.includes('rice')) return '&#127834;';
    if (n.includes('taco') || n.includes('burrito')) return '&#127790;';
    if (n.includes('burger')) return '&#127828;';
    if (n.includes('steak') || n.includes('beef')) return '&#129385;';
    if (n.includes('sandwich')) return '&#129386;';
    if (n.includes('egg')) return '&#129370;';
    if (n.includes('cake') || n.includes('dessert')) return '&#127856;';
    return '&#127869;';
}

let menuVisibleTags = new Set();  // tags visible in guest menu

async function loadMenu() {
    try {
        const [dishRes, tagRes] = await Promise.all([fetch('/api/dishes'), fetch('/api/tags')]);
        allMenuDishes = await dishRes.json();
        const allTags = await tagRes.json();
        menuVisibleTags = new Set(allTags.filter(t => t.visible_in_menu).map(t => t.name));
        // Clean cart
        const ids = new Set(allMenuDishes.map(d => String(d.id)));
        for (const id in cart) {
            if (!ids.has(id)) delete cart[id];
        }
        saveCart();
        renderMenuTagFilters();
        filterMenu();
    } catch(e) {
        console.error(e);
    }
}

function renderMenuTagFilters() {
    const container = document.getElementById('menuTagFilters');
    // Only show tags that are marked visible in menu
    const tagSet = {};
    for (const d of allMenuDishes) {
        if (d.tags) d.tags.forEach(t => {
            if (menuVisibleTags.has(t.name)) tagSet[t.name] = true;
        });
    }
    const tags = Object.keys(tagSet).sort();
    if (!tags.length) { container.innerHTML = ''; return; }
    const btnStyle = 'padding:5px 12px;border-radius:14px;font-size:12px;cursor:pointer;border:1px solid #ddd;';
    let html = '<button style="' + btnStyle + (menuActiveTag ? 'background:white;color:#333;' : 'background:var(--primary);color:white;border-color:var(--primary);') + '" onclick="setMenuTagFilter(null)">All</button>';
    for (const t of tags) {
        const active = menuActiveTag === t;
        html += '<button style="' + btnStyle + (active ? 'background:var(--primary);color:white;border-color:var(--primary);' : 'background:white;color:#333;') + '" onclick="setMenuTagFilter(\'' + escapeHtml(t).replace(/'/g, "\\\\'") + '\')">' + escapeHtml(t) + '</button>';
    }
    container.innerHTML = html;
}

function setMenuTagFilter(tag) {
    menuActiveTag = tag;
    renderMenuTagFilters();
    filterMenu();
}

function renderMenu(dishes) {
    const grid = document.getElementById('menuGrid');
    const empty = document.getElementById('menuEmpty');
    if (dishes.length === 0) {
        grid.innerHTML = '';
        empty.style.display = 'block';
        return;
    }
    empty.style.display = 'none';
    grid.innerHTML = dishes.map(d => {
        const imgSrc = d.image_path ? '/' + escapeHtml(d.image_path) : '';
        const imgHtml = imgSrc
            ? '<div class="menu-item-img-wrap" onclick="openImagePopup(\'' + imgSrc + '\')"><img class="menu-item-img" src="' + imgSrc + '" alt=""></div>'
            : '<div class="menu-item-img-wrap"><div class="menu-item-img-ph">' + dishEmoji(d.name) + '</div></div>';
        const qty = cart[d.id] ? cart[d.id].qty : 0;
        return '<div class="menu-item">' +
            imgHtml +
            '<div class="menu-item-body">' +
            '<div class="menu-item-name">' + escapeHtml(d.name) + '</div>' +
            '</div>' +
            '<div class="menu-item-qty">' +
            '<div class="qty-controls">' +
            '<button class="qty-btn minus" onclick="changeQty(' + d.id + ',-1)">-</button>' +
            '<span class="qty-val" id="qty-' + d.id + '">' + qty + '</span>' +
            '<button class="qty-btn plus" onclick="changeQty(' + d.id + ',1)">+</button>' +
            '</div>' +
            '</div>' +
            '</div>';
    }).join('');
}

function changeQty(dishId, dir) {
    const dish = allMenuDishes.find(d => d.id === dishId);
    if (!dish) return;
    let current = cart[dishId] ? cart[dishId].qty : 0;
    current = Math.max(0, current + dir);
    if (current === 0) {
        delete cart[dishId];
    } else {
        cart[dishId] = { dish: dish, qty: current };
    }
    saveCart();
    syncCartToServer();
    const el = document.getElementById('qty-' + dishId);
    if (el) el.textContent = String(current);
}

function filterMenu() {
    const q = document.getElementById('menuSearchInput').value.toLowerCase();
    let filtered = allMenuDishes.filter(d => d.name.toLowerCase().includes(q));
    if (menuActiveTag) {
        filtered = filtered.filter(d => d.tags && d.tags.some(t => t.name === menuActiveTag));
    }
    renderMenu(filtered);
}

function openCart() {
    renderCartBody();
    document.getElementById('cartOverlay').classList.add('open');
}

function closeCart() {
    document.getElementById('cartOverlay').classList.remove('open');
}

function renderCartBody() {
    const body = document.getElementById('cartBody');
    const ids = Object.keys(cart);
    if (ids.length === 0) {
        body.innerHTML = '<div class="cart-empty"><div class="empty-icon">&#128722;</div><p>Your cart is empty.<br>Add dishes from the menu!</p></div>';
        return;
    }
    let html = '';
    let totalServings = 0;
    for (const id of ids) {
        const item = cart[id];
        totalServings += item.qty;
        html += '<div class="cart-item">' +
            '<span class="cart-item-name">' + escapeHtml(item.dish.name) + '</span>' +
            '<div class="cart-item-qty">' +
            '<button class="qty-btn minus" onclick="cartChangeQty(' + id + ',-1)">-</button>' +
            '<span class="qty-val">' + item.qty + '</span>' +
            '<button class="qty-btn plus" onclick="cartChangeQty(' + id + ',1)">+</button>' +
            '</div>' +
            '<span class="cart-item-remove" onclick="cartRemove(' + id + ')">&times;</span>' +
            '</div>';
    }
    html += '<div class="cart-summary">' + ids.length + ' dish' + (ids.length !== 1 ? 'es' : '') + ' &middot; ' + totalServings + ' total servings</div>';
    html += '<div class="cart-actions">' +
        '<button class="cart-action-btn clear-btn" onclick="clearCart()">&#128465; Clear Cart</button>' +
        '</div>';
    body.innerHTML = html;
}

function cartChangeQty(dishId, dir) {
    if (!cart[dishId]) return;
    cart[dishId].qty = Math.max(1, cart[dishId].qty + dir);
    saveCart();
    syncCartToServer();
    renderCartBody();
    const el = document.getElementById('qty-' + dishId);
    if (el) el.textContent = String(cart[dishId] ? cart[dishId].qty : 0);
}

function cartRemove(dishId) {
    delete cart[dishId];
    saveCart();
    syncCartToServer();
    renderCartBody();
    const el = document.getElementById('qty-' + dishId);
    if (el) el.textContent = '0';
    if (Object.keys(cart).length === 0) {
        closeCart();
    }
}

function clearCart() {
    cart = {};
    saveCart();
    syncCartToServer();
    renderCartBody();
    closeCart();
    filterMenu();
    showToast('Cart cleared');
}

function copyCartText() {
    const ids = Object.keys(cart);
    if (ids.length === 0) return;
    let text = 'Order\n\n';
    let total = 0;
    for (const id of ids) {
        const item = cart[id];
        text += item.dish.name + ' x' + item.qty + '\n';
        total += item.qty;
    }
    text += '\nTotal: ' + total + ' servings\n';
    navigator.clipboard.writeText(text).then(() => {
        showToast('Copied to clipboard!');
    }).catch(() => {
        const ta = document.createElement('textarea');
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        showToast('Copied to clipboard!');
    });
}

async function saveCartImage() {
    const body = document.getElementById('cartBody');
    if (!body) return;
    await openAsImageGuest(body);
}

function showGroceryList() {
    const modal = document.getElementById('groceryModal');
    if (!modal) return;
    const ids = Object.keys(cart);
    if (ids.length === 0) return;

    // Aggregate ingredients
    const agg = {};
    for (const id of ids) {
        const item = cart[id];
        const dish = item.dish;
        const qty = item.qty;
        if (dish.ingredients) {
            for (const ing of dish.ingredients) {
                const key = ing.name.toLowerCase().trim() + '|' + ing.unit.toLowerCase().trim();
                if (!agg[key]) {
                    agg[key] = { name: ing.name.trim(), unit: ing.unit, amount: 0 };
                }
                agg[key].amount += ing.amount * qty;
            }
        }
    }
    const items = Object.values(agg).sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase()));

    if (items.length === 0) {
        modal.innerHTML = '<h4>Grocery List</h4><p style="color:var(--text-secondary);font-size:14px;">No ingredients found for selected dishes.</p>';
        modal.classList.add('open');
        return;
    }

    // Round amounts
    for (const item of items) {
        if (item.amount === Math.floor(item.amount)) {
            item.amount = Math.floor(item.amount);
        } else {
            item.amount = Math.round(item.amount * 100) / 100;
        }
    }

    let html = '<h4>Grocery List</h4>';
    html += '<ul class="grocery-modal-list" id="groceryModalList">';
    for (const item of items) {
        html += '<li class="grocery-modal-item"><span>' + escapeHtml(item.name) + '</span><span style="font-weight:600;color:var(--primary);">' + item.amount + ' ' + escapeHtml(item.unit) + '</span></li>';
    }
    html += '</ul>';
    html += '<div class="grocery-modal-actions">';
    html += '<button style="background:#5b7bbd;" onclick="copyGroceryText()">&#128203; Copy</button>';
    html += '<button style="background:var(--green);" onclick="saveGroceryImage()">&#128190; Image</button>';
    html += '</div>';
    modal.innerHTML = html;
    modal.classList.add('open');
}

function copyGroceryText() {
    const items = document.querySelectorAll('#groceryModalList .grocery-modal-item');
    if (!items.length) return;
    let text = 'Grocery List\n\n';
    items.forEach(li => {
        const spans = li.querySelectorAll('span');
        text += spans[0].textContent + ' — ' + spans[1].textContent + '\n';
    });
    navigator.clipboard.writeText(text).then(() => {
        showToast('Grocery list copied!');
    }).catch(() => {
        const ta = document.createElement('textarea');
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        showToast('Grocery list copied!');
    });
}

async function saveGroceryImage() {
    const list = document.getElementById('groceryModalList');
    if (!list) return;
    await openAsImageGuest(list);
}

// Image popup
function openImagePopup(src) {
    const overlay = document.getElementById('imgPopup');
    const img = document.getElementById('imgPopupImg');
    img.src = src;
    overlay.classList.add('open');
}
function closeImagePopup() {
    document.getElementById('imgPopup').classList.remove('open');
}

// Init
loadMenu();
updateCartBadge();

// SSE for shared cart sync
let cartSSE = null;
function connectCartSSE() {
    if (cartSSE) cartSSE.close();
    cartSSE = new EventSource('/api/cart/events');
    cartSSE.onmessage = function(e) {
        try {
            const data = JSON.parse(e.data);
            if (data.type === 'cart_update' && !ignoreSseUpdate) {
                // Merge server cart (has dish_name) with local dish objects
                const serverCart = data.cart || {};
                const newCart = {};
                for (const id in serverCart) {
                    const sc = serverCart[id];
                    const localDish = allMenuDishes.find(d => d.id === parseInt(id));
                    newCart[id] = {
                        dish: localDish || { id: parseInt(id), name: sc.dish_name },
                        qty: sc.qty,
                    };
                }
                cart = newCart;
                saveCart();
                filterMenu();
                renderCartBody();
                updateCartBadge();
            }
        } catch(ex) {}
    };
    cartSSE.onerror = function() {
        setTimeout(connectCartSSE, 3000);
    };
}
connectCartSSE();
</script>

<!-- Image popup overlay -->
<div class="img-popup-overlay" id="imgPopup" onclick="closeImagePopup()">
    <button class="img-popup-close" onclick="event.stopPropagation();closeImagePopup()">&times;</button>
    <img id="imgPopupImg" src="" alt="Dish" onclick="event.stopPropagation()">
</div>

</body>
</html>"""

ADMIN_BASE = "/kitchen-admin-7x9k"


@app.get(ADMIN_BASE, response_class=HTMLResponse)
@app.get(ADMIN_BASE + "/", response_class=HTMLResponse)
async def admin_index():
    return SPA_HTML


@app.get("/")
async def root_page():
    return HTMLResponse("")


@app.get("/menu", response_class=HTMLResponse)
async def guest_menu(request: Request):
    return MENU_HTML


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("MEAL_PLANNER_PORT", "8091"))
    uvicorn.run(app, host="0.0.0.0", port=port)
