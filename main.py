import asyncio
import csv
import io
import json
import os
import random
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import (
    Cookie, Depends, FastAPI, File, Form, HTTPException,
    UploadFile, WebSocket, WebSocketDisconnect, status
)
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from passlib.context import CryptContext
from jose import JWTError, jwt

# ── CONFIG ──────────────────────────────────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY", "code-eleven-secret-change-in-prod-2025")
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 24
DB_PATH = Path("data/draft.db")
MCQ_TIMER_SECONDS = 15
ADMIN_DEFAULT_PASSWORD = os.getenv("ADMIN_PASSWORD", "codeeleven2025")
PLAYER_ACCESS_KEY = os.getenv("PLAYER_ACCESS_KEY", "players2025")

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ── DB SETUP ─────────────────────────────────────────────────────────────
def get_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS admins (
        id TEXT PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS captains (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS players (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        position TEXT NOT NULL,
        batch_year INTEGER NOT NULL,
        taken_by TEXT DEFAULT NULL,
        pick_order INTEGER DEFAULT NULL,
        FOREIGN KEY(taken_by) REFERENCES captains(id)
    );
    CREATE TABLE IF NOT EXISTS questions (
        id TEXT PRIMARY KEY,
        text TEXT NOT NULL,
        option_a TEXT NOT NULL,
        option_b TEXT NOT NULL,
        option_c TEXT NOT NULL,
        option_d TEXT NOT NULL,
        correct_index INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS game_state (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE TABLE IF NOT EXISTS draft_history (
        id TEXT PRIMARY KEY,
        captain_id TEXT NOT NULL,
        captain_name TEXT NOT NULL,
        player_id TEXT NOT NULL,
        player_name TEXT NOT NULL,
        player_position TEXT NOT NULL,
        player_year INTEGER NOT NULL,
        group_id TEXT NOT NULL,
        pick_number INTEGER NOT NULL,
        picked_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS mcq_answers (
        id TEXT PRIMARY KEY,
        round_id TEXT NOT NULL,
        captain_id TEXT NOT NULL,
        chosen_index INTEGER,
        is_correct INTEGER NOT NULL,
        answered_at_ms INTEGER NOT NULL
    );
    """)
    # seed default admin if none
    existing = c.execute("SELECT id FROM admins LIMIT 1").fetchone()
    if not existing:
        c.execute("INSERT INTO admins (id, username, password_hash) VALUES (?, ?, ?)",
                  (str(uuid.uuid4()), "admin", pwd_ctx.hash(ADMIN_DEFAULT_PASSWORD)))
    conn.commit()
    conn.close()

# ── AUTH ─────────────────────────────────────────────────────────────────
def make_token(sub: str, role: str, name: str = ""):
    exp = datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS)
    return jwt.encode({"sub": sub, "role": role, "name": name, "exp": exp}, SECRET_KEY, ALGORITHM)

def decode_token(token: str):
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None

def require_admin(token: str = Cookie(default=None)):
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    data = decode_token(token)
    if not data or data.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return data

def require_captain(token: str = Cookie(default=None)):
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    data = decode_token(token)
    if not data or data.get("role") not in ("captain", "admin"):
        raise HTTPException(status_code=403, detail="Captain login required")
    return data

# ── GAME STATE HELPERS ───────────────────────────────────────────────────
def gs_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM game_state WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return row["value"]

def gs_set(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO game_state (key, value) VALUES (?, ?)",
                 (key, json.dumps(value)))
    conn.commit()

# ── WEBSOCKET MANAGER ─────────────────────────────────────────────────────
class WSManager:
    def __init__(self):
        self.connections: dict[str, list[WebSocket]] = {}  # room -> list of ws

    async def connect(self, ws: WebSocket, room: str):
        await ws.accept()
        self.connections.setdefault(room, []).append(ws)

    def disconnect(self, ws: WebSocket, room: str):
        if room in self.connections:
            self.connections[room] = [c for c in self.connections[room] if c != ws]

    async def broadcast(self, room: str, data: dict):
        dead = []
        for ws in self.connections.get(room, []):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws, room)

    def online_count(self, room: str) -> int:
        return len(self.connections.get(room, []))

mgr = WSManager()

# ── MCQ TIMER TASK ────────────────────────────────────────────────────────
mcq_timer_task: Optional[asyncio.Task] = None

async def mcq_countdown(round_id: str, group_id: str):
    await asyncio.sleep(MCQ_TIMER_SECONDS)
    conn = get_db()
    try:
        current_round = gs_get(conn, "current_round_id")
        if current_round != round_id:
            return  # round changed, abort
        phase = gs_get(conn, "phase")
        if phase != "mcq":
            return

        # lock answers, compute draft order:
        # 1st: correct answers sorted fastest to slowest
        # 2nd: wrong answers sorted fastest to slowest
        # 3rd: captains who did not answer at all
        answers = conn.execute(
            "SELECT * FROM mcq_answers WHERE round_id=?", (round_id,)
        ).fetchall()
        correct  = sorted([a for a in answers if a["is_correct"]],     key=lambda a: a["answered_at_ms"])
        wrong    = sorted([a for a in answers if not a["is_correct"]], key=lambda a: a["answered_at_ms"])
        all_caps = [r["id"] for r in conn.execute("SELECT id FROM captains").fetchall()]
        answered_ids = {a["captain_id"] for a in answers}
        no_answer = [cid for cid in all_caps if cid not in answered_ids]
        order = [a["captain_id"] for a in correct] + [a["captain_id"] for a in wrong] + no_answer

        gs_set(conn, "draft_order", order)
        gs_set(conn, "current_picker_index", 0)
        gs_set(conn, "phase", "draft")

        await mgr.broadcast("draft", {
            "type": "phase_change",
            "phase": "draft",
            "draft_order": order,
            "group_id": group_id,
        })
    finally:
        conn.close()

# ── LIFESPAN ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

# ── APP ───────────────────────────────────────────────────────────────────
app = FastAPI(lifespan=lifespan, title="Code Eleven Draft")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── SERVE FRONTEND ────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    async with aiofiles.open("templates/index.html", "r") as f:
        return await f.read()

@app.get("/admin", response_class=HTMLResponse)
async def serve_admin():
    async with aiofiles.open("templates/admin.html", "r") as f:
        return await f.read()

@app.get("/captain", response_class=HTMLResponse)
async def serve_captain():
    async with aiofiles.open("templates/captain.html", "r") as f:
        return await f.read()

@app.get("/results", response_class=HTMLResponse)
async def serve_results():
    async with aiofiles.open("templates/results.html", "r") as f:
        return await f.read()

# ── AUTH ENDPOINTS ────────────────────────────────────────────────────────
@app.post("/api/auth/admin-login")
async def admin_login(username: str = Form(...), password: str = Form(...)):
    conn = get_db()
    row = conn.execute("SELECT * FROM admins WHERE username=?", (username,)).fetchone()
    conn.close()
    if not row or not pwd_ctx.verify(password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = make_token(row["id"], "admin", row["username"])
    resp = JSONResponse({"ok": True, "name": row["username"]})
    resp.set_cookie("token", token, httponly=True, samesite="lax", max_age=TOKEN_EXPIRE_HOURS*3600)
    return resp

@app.post("/api/auth/captain-login")
async def captain_login(captain_id: str = Form(...), password: str = Form(...)):
    conn = get_db()
    row = conn.execute("SELECT * FROM captains WHERE id=?", (captain_id,)).fetchone()
    conn.close()
    if not row or not pwd_ctx.verify(password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = make_token(row["id"], "captain", row["name"])
    resp = JSONResponse({"ok": True, "name": row["name"], "captain_id": row["id"]})
    resp.set_cookie("token", token, httponly=True, samesite="lax", max_age=TOKEN_EXPIRE_HOURS*3600)
    return resp

@app.post("/api/auth/player-login")
async def player_login(access_key: str = Form(...)):
    if access_key != PLAYER_ACCESS_KEY:
        raise HTTPException(status_code=401, detail="Invalid access key")
    token = make_token("player", "player", "Player")
    resp = JSONResponse({"ok": True})
    resp.set_cookie("token", token, httponly=True, samesite="lax", max_age=TOKEN_EXPIRE_HOURS*3600)
    return resp

@app.post("/api/auth/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("token")
    return resp

@app.get("/api/auth/me")
async def me(token: str = Cookie(default=None)):
    if not token:
        return JSONResponse({"role": None})
    data = decode_token(token)
    if not data:
        return JSONResponse({"role": None})
    return JSONResponse({"role": data.get("role"), "name": data.get("name"), "sub": data.get("sub"), "ok": True})

# ── PUBLIC RESULTS (player view) ─────────────────────────────────────────
@app.get("/api/results")
async def get_results(token: str = Cookie(default=None)):
    """Read-only endpoint accessible to players, captains, and admins."""
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    data = decode_token(token)
    if not data or data.get("role") not in ("player", "captain", "admin"):
        raise HTTPException(status_code=403, detail="Login required")
    conn = get_db()
    captains = conn.execute("SELECT id, name FROM captains ORDER BY name").fetchall()
    players = conn.execute("""
        SELECT p.id, p.name, p.position, p.batch_year, p.taken_by, c.name as captain_name
        FROM players p LEFT JOIN captains c ON p.taken_by = c.id
        ORDER BY p.batch_year
    """).fetchall()
    phase = gs_get(conn, "phase", "lobby")
    conn.close()
    return {
        "phase": phase,
        "captains": [dict(c) for c in captains],
        "players": [dict(p) for p in players],
    }

# ── ADMIN — MANAGE ADMINS ─────────────────────────────────────────────────
@app.get("/api/admins")
async def list_admins(auth=Depends(require_admin)):
    conn = get_db()
    rows = conn.execute("SELECT id, username, created_at FROM admins").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/admins")
async def create_admin(username: str = Form(...), password: str = Form(...), auth=Depends(require_admin)):
    conn = get_db()
    try:
        conn.execute("INSERT INTO admins (id, username, password_hash) VALUES (?, ?, ?)",
                     (str(uuid.uuid4()), username, pwd_ctx.hash(password)))
        conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Username already exists")
    finally:
        conn.close()
    return {"ok": True}

@app.delete("/api/admins/{admin_id}")
async def delete_admin(admin_id: str, auth=Depends(require_admin)):
    conn = get_db()
    conn.execute("DELETE FROM admins WHERE id=?", (admin_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

# ── ADMIN — CAPTAINS ──────────────────────────────────────────────────────
@app.get("/api/captains")
async def list_captains(token: str = Cookie(default=None)):
    data = decode_token(token) if token else None
    conn = get_db()
    if data and data.get("role") == "admin":
        rows = conn.execute("SELECT id, name, created_at FROM captains").fetchall()
    else:
        rows = conn.execute("SELECT id, name FROM captains").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/captains")
async def create_captain(name: str = Form(...), password: str = Form(...), auth=Depends(require_admin)):
    conn = get_db()
    conn.execute("INSERT INTO captains (id, name, password_hash) VALUES (?, ?, ?)",
                 (str(uuid.uuid4()), name, pwd_ctx.hash(password)))
    conn.commit()
    conn.close()
    await mgr.broadcast("draft", {"type": "captains_updated"})
    return {"ok": True}

@app.put("/api/captains/{captain_id}/password")
async def update_captain_password(captain_id: str, password: str = Form(...), auth=Depends(require_admin)):
    conn = get_db()
    conn.execute("UPDATE captains SET password_hash=? WHERE id=?", (pwd_ctx.hash(password), captain_id))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.delete("/api/captains/{captain_id}")
async def delete_captain(captain_id: str, auth=Depends(require_admin)):
    conn = get_db()
    conn.execute("DELETE FROM captains WHERE id=?", (captain_id,))
    conn.commit()
    conn.close()
    await mgr.broadcast("draft", {"type": "captains_updated"})
    return {"ok": True}

# ── ADMIN — PLAYERS ───────────────────────────────────────────────────────
@app.get("/api/players")
async def list_players(token: str = Cookie(default=None)):
    conn = get_db()
    rows = conn.execute("""
        SELECT p.*, c.name as captain_name 
        FROM players p LEFT JOIN captains c ON p.taken_by = c.id
        ORDER BY p.batch_year
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/players")
async def add_player(name: str = Form(...), position: str = Form(...),
                     batch_year: int = Form(...), auth=Depends(require_admin)):
    conn = get_db()
    conn.execute("INSERT INTO players (id, name, position, batch_year) VALUES (?, ?, ?, ?)",
                 (str(uuid.uuid4()), name, position, batch_year))
    conn.commit()
    conn.close()
    await mgr.broadcast("draft", {"type": "players_updated"})
    return {"ok": True}

@app.post("/api/players/csv")
async def upload_players_csv(file: UploadFile = File(...), auth=Depends(require_admin)):
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        # normalize headers
        rows = list(reader)
        if not rows:
            raise HTTPException(status_code=400, detail="CSV is empty")

        # flexible header matching
        def find_col(row, *candidates):
            for k in row.keys():
                if k.strip().lower().replace(" ", "_") in candidates:
                    return k
            return None

        sample = rows[0]
        name_col = find_col(sample, "name", "player_name", "player")
        year_col = find_col(sample, "batch_year", "year", "batch", "angkatan")
        pos_col  = find_col(sample, "position", "pos", "posisi")

        if not all([name_col, year_col, pos_col]):
            raise HTTPException(status_code=400,
                detail=f"CSV must have columns: name, batch_year, position. Found: {list(sample.keys())}")

        conn = get_db()
        added = 0
        errors = []
        for i, row in enumerate(rows, 1):
            try:
                n = row[name_col].strip()
                y = int(row[year_col].strip())
                p = row[pos_col].strip().upper()
                if not n:
                    continue
                conn.execute("INSERT INTO players (id, name, position, batch_year) VALUES (?,?,?,?)",
                             (str(uuid.uuid4()), n, p, y))
                added += 1
            except Exception as e:
                errors.append(f"Row {i}: {e}")
        conn.commit()
        conn.close()
        await mgr.broadcast("draft", {"type": "players_updated"})
        return {"ok": True, "added": added, "errors": errors}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"CSV parse error: {e}")

@app.delete("/api/players/{player_id}")
async def delete_player(player_id: str, auth=Depends(require_admin)):
    conn = get_db()
    conn.execute("DELETE FROM players WHERE id=?", (player_id,))
    conn.commit()
    conn.close()
    await mgr.broadcast("draft", {"type": "players_updated"})
    return {"ok": True}

@app.delete("/api/players")
async def clear_players(auth=Depends(require_admin)):
    conn = get_db()
    conn.execute("DELETE FROM players")
    conn.commit()
    conn.close()
    await mgr.broadcast("draft", {"type": "players_updated"})
    return {"ok": True}

# ── ADMIN — ASSIGN PLAYER ────────────────────────────────────────────────
@app.put("/api/players/{player_id}/assign")
async def assign_player(player_id: str, body: dict, auth=Depends(require_admin)):
    captain_id = body.get("captain_id")  # None = unassign
    conn = get_db()
    player = conn.execute("SELECT * FROM players WHERE id=?", (player_id,)).fetchone()
    if not player:
        conn.close()
        raise HTTPException(status_code=404, detail="Player not found")
    if captain_id:
        cap = conn.execute("SELECT id FROM captains WHERE id=?", (captain_id,)).fetchone()
        if not cap:
            conn.close()
            raise HTTPException(status_code=404, detail="Captain not found")
    conn.execute("UPDATE players SET taken_by=? WHERE id=?", (captain_id, player_id))
    conn.commit()
    conn.close()
    await mgr.broadcast("draft", {"type": "players_updated"})
    return {"ok": True}

# ── ADMIN — QUESTIONS ─────────────────────────────────────────────────────
@app.get("/api/questions")
async def list_questions(auth=Depends(require_admin)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM questions ORDER BY created_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/questions")
async def add_question(
    text: str = Form(...),
    option_a: str = Form(...), option_b: str = Form(...),
    option_c: str = Form(...), option_d: str = Form(...),
    correct_index: int = Form(...),
    auth=Depends(require_admin)
):
    conn = get_db()
    conn.execute("""INSERT INTO questions (id, text, option_a, option_b, option_c, option_d, correct_index)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                 (str(uuid.uuid4()), text, option_a, option_b, option_c, option_d, correct_index))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.delete("/api/questions/{question_id}")
async def delete_question(question_id: str, auth=Depends(require_admin)):
    conn = get_db()
    conn.execute("DELETE FROM questions WHERE id=?", (question_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

# ── GAME STATE ────────────────────────────────────────────────────────────
@app.get("/api/game")
async def get_game(token: str = Cookie(default=None)):
    conn = get_db()
    data = decode_token(token) if token else None
    phase = gs_get(conn, "phase", "lobby")
    group_index = gs_get(conn, "current_group_index", -1)
    draft_order = gs_get(conn, "draft_order", [])
    picker_index = gs_get(conn, "current_picker_index", 0)
    round_id = gs_get(conn, "current_round_id")

    groups = ["g1", "g2", "g3"]
    current_group = groups[group_index] if 0 <= group_index < 3 else None

    # MCQ question (hide correct answer from captains)
    q_data = None
    current_q_id = gs_get(conn, "current_question_id")
    if current_q_id:
        q = conn.execute("SELECT * FROM questions WHERE id=?", (current_q_id,)).fetchone()
        if q:
            q_data = {
                "id": q["id"],
                "text": q["text"],
                "options": [q["option_a"], q["option_b"], q["option_c"], q["option_d"]],
            }
            if data and data.get("role") == "admin":
                q_data["correct_index"] = q["correct_index"]

    # answers
    answers = []
    if round_id:
        ans_rows = conn.execute("SELECT * FROM mcq_answers WHERE round_id=?", (round_id,)).fetchall()
        answers = [dict(a) for a in ans_rows]

    players = conn.execute("""
        SELECT p.*, c.name as captain_name
        FROM players p LEFT JOIN captains c ON p.taken_by = c.id
        ORDER BY p.batch_year
    """).fetchall()

    captains = conn.execute("SELECT id, name FROM captains").fetchall()
    history = conn.execute("SELECT * FROM draft_history ORDER BY pick_number").fetchall()

    conn.close()
    return {
        "phase": phase,
        "current_group": current_group,
        "group_index": group_index,
        "draft_order": draft_order,
        "current_picker_index": picker_index,
        "question": q_data,
        "answers": answers,
        "round_id": round_id,
        "players": [dict(p) for p in players],
        "captains": [dict(c) for c in captains],
        "history": [dict(h) for h in history],
        "online": mgr.online_count("draft"),
    }

# ── ADMIN — DRAFT CONTROL ─────────────────────────────────────────────────
GROUPS = {
    "g1": {"label": "≤ 2004", "filter": lambda y: y <= 2004},
    "g2": {"label": "2005 – 2018", "filter": lambda y: 2005 <= y <= 2018},
    "g3": {"label": "> 2018", "filter": lambda y: y > 2018},
}
GROUP_ORDER = ["g1", "g2", "g3"]

@app.post("/api/game/start-round")
async def start_round(auth=Depends(require_admin)):
    global mcq_timer_task
    conn = get_db()

    questions = conn.execute("SELECT * FROM questions").fetchall()
    if not questions:
        conn.close()
        raise HTTPException(status_code=400, detail="Add questions first")

    captains = conn.execute("SELECT id FROM captains").fetchall()
    if not captains:
        conn.close()
        raise HTTPException(status_code=400, detail="Add captains first")

    current_index = gs_get(conn, "current_group_index", -1)
    next_index = current_index + 1
    if next_index >= 3:
        conn.close()
        raise HTTPException(status_code=400, detail="All 3 rounds complete")

    group_id = GROUP_ORDER[next_index]
    q = random.choice(questions)
    round_id = str(uuid.uuid4())

    gs_set(conn, "phase", "mcq")
    gs_set(conn, "current_group_index", next_index)
    gs_set(conn, "current_group_id", group_id)
    gs_set(conn, "current_question_id", q["id"])
    gs_set(conn, "current_round_id", round_id)
    gs_set(conn, "draft_order", [])
    gs_set(conn, "current_picker_index", 0)
    conn.close()

    # cancel existing timer
    if mcq_timer_task and not mcq_timer_task.done():
        mcq_timer_task.cancel()
    mcq_timer_task = asyncio.create_task(mcq_countdown(round_id, group_id))

    await mgr.broadcast("draft", {
        "type": "round_started",
        "group_id": group_id,
        "group_index": next_index,
        "round_id": round_id,
        "question": {
            "id": q["id"],
            "text": q["text"],
            "options": [q["option_a"], q["option_b"], q["option_c"], q["option_d"]],
        },
        "timer_seconds": MCQ_TIMER_SECONDS,
    })
    return {"ok": True, "group_id": group_id}

@app.post("/api/game/set-draft-order")
async def set_draft_order(body: dict, auth=Depends(require_admin)):
    order = body.get("order", [])
    conn = get_db()
    gs_set(conn, "draft_order", order)
    gs_set(conn, "phase", "draft")
    conn.close()
    await mgr.broadcast("draft", {
        "type": "phase_change",
        "phase": "draft",
        "draft_order": order,
    })
    return {"ok": True}

@app.post("/api/game/end-draft")
async def end_draft(auth=Depends(require_admin)):
    conn = get_db()
    gs_set(conn, "phase", "done")
    conn.close()
    await mgr.broadcast("draft", {"type": "phase_change", "phase": "done"})
    return {"ok": True}

@app.post("/api/game/reset")
async def reset_game(auth=Depends(require_admin)):
    global mcq_timer_task
    if mcq_timer_task and not mcq_timer_task.done():
        mcq_timer_task.cancel()
    conn = get_db()
    conn.execute("DELETE FROM game_state")
    conn.execute("UPDATE players SET taken_by=NULL, pick_order=NULL")
    conn.execute("DELETE FROM draft_history")
    conn.execute("DELETE FROM mcq_answers")
    conn.commit()
    conn.close()
    await mgr.broadcast("draft", {"type": "reset"})
    return {"ok": True}

# ── CAPTAIN — ANSWER MCQ ──────────────────────────────────────────────────
@app.post("/api/game/answer")
async def submit_answer(body: dict, auth=Depends(require_captain)):
    captain_id = auth["sub"]
    chosen = body.get("chosen_index")
    conn = get_db()

    phase = gs_get(conn, "phase")
    if phase != "mcq":
        conn.close()
        raise HTTPException(status_code=400, detail="Not in MCQ phase")

    round_id = gs_get(conn, "current_round_id")
    existing = conn.execute(
        "SELECT id FROM mcq_answers WHERE round_id=? AND captain_id=?",
        (round_id, captain_id)
    ).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=400, detail="Already answered")

    q_id = gs_get(conn, "current_question_id")
    q = conn.execute("SELECT correct_index FROM questions WHERE id=?", (q_id,)).fetchone()
    is_correct = (chosen == q["correct_index"]) if q and chosen is not None else False

    conn.execute("""INSERT INTO mcq_answers (id, round_id, captain_id, chosen_index, is_correct, answered_at_ms)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                 (str(uuid.uuid4()), round_id, captain_id, chosen, int(is_correct), int(time.time() * 1000)))
    conn.commit()

    # check if all captains answered
    total_captains = conn.execute("SELECT COUNT(*) as n FROM captains").fetchone()["n"]
    answered = conn.execute(
        "SELECT COUNT(*) as n FROM mcq_answers WHERE round_id=?", (round_id,)
    ).fetchone()["n"]

    group_id = gs_get(conn, "current_group_id")
    conn.close()

    await mgr.broadcast("draft", {
        "type": "captain_answered",
        "captain_id": captain_id,
        "is_correct": is_correct,
        "chosen_index": chosen,
    })

    # auto-advance if all answered
    if answered >= total_captains:
        global mcq_timer_task
        if mcq_timer_task and not mcq_timer_task.done():
            mcq_timer_task.cancel()
        await mcq_countdown(round_id, group_id)

    return {"ok": True, "is_correct": is_correct}

# ── CAPTAIN — PICK PLAYER ─────────────────────────────────────────────────
@app.post("/api/game/pick")
async def pick_player(body: dict, auth=Depends(require_captain)):
    captain_id = auth["sub"]
    player_id = body.get("player_id")
    conn = get_db()

    phase = gs_get(conn, "phase")
    if phase != "draft":
        conn.close()
        raise HTTPException(status_code=400, detail="Not in draft phase")

    draft_order = gs_get(conn, "draft_order", [])
    picker_index = gs_get(conn, "current_picker_index", 0)
    if not draft_order or picker_index >= len(draft_order):
        conn.close()
        raise HTTPException(status_code=400, detail="No draft order set")

    current_picker = draft_order[picker_index]
    if current_picker != captain_id and auth.get("role") != "admin":
        conn.close()
        raise HTTPException(status_code=403, detail="Not your turn")

    player = conn.execute("SELECT * FROM players WHERE id=?", (player_id,)).fetchone()
    if not player:
        conn.close()
        raise HTTPException(status_code=404, detail="Player not found")
    if player["taken_by"]:
        conn.close()
        raise HTTPException(status_code=400, detail="Player already taken")

    group_id = gs_get(conn, "current_group_id")
    pick_num = conn.execute("SELECT COUNT(*) as n FROM draft_history").fetchone()["n"] + 1
    captain = conn.execute("SELECT name FROM captains WHERE id=?", (captain_id,)).fetchone()

    conn.execute("UPDATE players SET taken_by=?, pick_order=? WHERE id=?",
                 (captain_id, pick_num, player_id))
    conn.execute("""INSERT INTO draft_history
        (id, captain_id, captain_name, player_id, player_name, player_position, player_year, group_id, pick_number)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (str(uuid.uuid4()), captain_id, captain["name"] if captain else captain_id,
         player_id, player["name"], player["position"], player["batch_year"], group_id, pick_num))

    # advance picker (wrap around)
    next_index = (picker_index + 1) % len(draft_order)
    gs_set(conn, "current_picker_index", next_index)
    conn.commit()
    conn.close()

    await mgr.broadcast("draft", {
        "type": "player_picked",
        "player_id": player_id,
        "player_name": player["name"],
        "captain_id": captain_id,
        "captain_name": captain["name"] if captain else captain_id,
        "pick_number": pick_num,
        "next_picker_index": next_index,
    })
    return {"ok": True}

# ── EXPORT ────────────────────────────────────────────────────────────────
@app.get("/api/export/history")
async def export_history(auth=Depends(require_admin)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM draft_history ORDER BY pick_number").fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Pick #", "Captain", "Player", "Position", "Batch Year", "Group", "Picked At"])
    group_labels = {"g1": "≤2004", "g2": "2005-2018", "g3": ">2018"}
    for r in rows:
        writer.writerow([
            r["pick_number"], r["captain_name"], r["player_name"],
            r["player_position"], r["player_year"],
            group_labels.get(r["group_id"], r["group_id"]), r["picked_at"]
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=draft-history.csv"}
    )

@app.get("/api/export/teams")
async def export_teams(auth=Depends(require_admin)):
    conn = get_db()
    captains = conn.execute("SELECT id, name FROM captains").fetchall()
    players = conn.execute("""
        SELECT p.*, c.name as captain_name
        FROM players p LEFT JOIN captains c ON p.taken_by=c.id
        ORDER BY p.batch_year
    """).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Captain", "Player", "Position", "Batch Year", "Group"])
    group_labels = {"g1": "≤2004", "g2": "2005-2018", "g3": ">2018"}

    def get_group(y):
        if y <= 2004: return "g1"
        if y <= 2018: return "g2"
        return "g3"

    for p in players:
        writer.writerow([
            p["captain_name"] or "Undrafted",
            p["name"], p["position"], p["batch_year"],
            group_labels.get(get_group(p["batch_year"]), "")
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=teams.csv"}
    )


# ── RESTORE FROM EXPORT ───────────────────────────────────────────────────
@app.post("/api/restore/teams")
async def restore_from_teams_csv(file: UploadFile = File(...), auth=Depends(require_admin)):
    """
    Restore players + captain assignments from the teams.csv export.
    Columns: Captain, Player, Position, Batch Year, Group
    - Creates players that don't exist yet (matched by name+year+position)
    - Assigns them to captains matched by name
    - Captains must already exist in the DB
    - 'Undrafted' captain column = player added but unassigned
    """
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            raise HTTPException(status_code=400, detail="CSV is empty")

        conn = get_db()
        captains = {c["name"]: c["id"] for c in conn.execute("SELECT id, name FROM captains").fetchall()}
        existing_players = {
            (p["name"].strip().lower(), str(p["batch_year"])): p["id"]
            for p in conn.execute("SELECT id, name, batch_year FROM players").fetchall()
        }

        added = 0
        assigned = 0
        skipped = []

        for i, row in enumerate(rows, 1):
            try:
                # flexible column names
                captain_name = (row.get("Captain") or row.get("captain") or "").strip()
                player_name  = (row.get("Player")  or row.get("player")  or row.get("name") or "").strip()
                position     = (row.get("Position") or row.get("position") or row.get("pos") or "").strip().upper()
                batch_year   = int((row.get("Batch Year") or row.get("batch_year") or row.get("year") or "0").strip())

                if not player_name or not batch_year:
                    skipped.append(f"Row {i}: missing player name or year")
                    continue

                # find or create player
                key = (player_name.lower(), str(batch_year))
                if key in existing_players:
                    player_id = existing_players[key]
                else:
                    player_id = str(uuid.uuid4())
                    conn.execute("INSERT INTO players (id, name, position, batch_year) VALUES (?,?,?,?)",
                                 (player_id, player_name, position or "?", batch_year))
                    existing_players[key] = player_id
                    added += 1

                # assign to captain
                captain_id = None
                if captain_name and captain_name.lower() != "undrafted":
                    captain_id = captains.get(captain_name)
                    if not captain_id:
                        skipped.append(f"Row {i}: captain '{captain_name}' not found — player added unassigned")
                    else:
                        assigned += 1

                conn.execute("UPDATE players SET taken_by=? WHERE id=?", (captain_id, player_id))

            except Exception as e:
                skipped.append(f"Row {i}: {e}")

        conn.commit()
        conn.close()
        await mgr.broadcast("draft", {"type": "players_updated"})
        return {"ok": True, "added": added, "assigned": assigned, "skipped": skipped}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Parse error: {e}")


@app.post("/api/restore/history")
async def restore_from_history_csv(file: UploadFile = File(...), auth=Depends(require_admin)):
    """
    Restore draft history from history.csv export.
    Columns: Pick #, Captain, Player, Position, Batch Year, Group, Picked At
    Rebuilds the draft_history table entries only (does not reassign players — run teams restore first).
    """
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            raise HTTPException(status_code=400, detail="CSV is empty")

        group_map = {"≤2004": "g1", "2005-2018": "g2", ">2018": "g3"}
        conn = get_db()

        captains_by_name = {c["name"]: c["id"] for c in conn.execute("SELECT id, name FROM captains").fetchall()}
        players_by_key   = {
            (p["name"].strip().lower(), str(p["batch_year"])): p["id"]
            for p in conn.execute("SELECT id, name, batch_year FROM players").fetchall()
        }

        # clear existing history before restoring
        conn.execute("DELETE FROM draft_history")

        restored = 0
        skipped = []

        for i, row in enumerate(rows, 1):
            try:
                pick_num     = int((row.get("Pick #") or row.get("pick_number") or "0").strip())
                captain_name = (row.get("Captain") or "").strip()
                player_name  = (row.get("Player")  or "").strip()
                position     = (row.get("Position") or row.get("Pos") or "").strip()
                batch_year   = int((row.get("Batch Year") or row.get("batch_year") or "0").strip())
                group_raw    = (row.get("Group") or "").strip()
                picked_at    = (row.get("Picked At") or row.get("picked_at") or "").strip()

                captain_id = captains_by_name.get(captain_name)
                player_id  = players_by_key.get((player_name.lower(), str(batch_year)))
                group_id   = group_map.get(group_raw, "g1")

                if not captain_id:
                    skipped.append(f"Row {i}: captain '{captain_name}' not found")
                    continue
                if not player_id:
                    skipped.append(f"Row {i}: player '{player_name}' ({batch_year}) not found — run teams restore first")
                    continue

                conn.execute("""INSERT OR IGNORE INTO draft_history
                    (id, captain_id, captain_name, player_id, player_name, player_position, player_year, group_id, pick_number, picked_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), captain_id, captain_name,
                     player_id, player_name, position, batch_year,
                     group_id, pick_num, picked_at or None))
                restored += 1

            except Exception as e:
                skipped.append(f"Row {i}: {e}")

        conn.commit()
        conn.close()
        return {"ok": True, "restored": restored, "skipped": skipped}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Parse error: {e}")

# ── WEBSOCKET ─────────────────────────────────────────────────────────────
@app.websocket("/ws/draft")
async def ws_draft(ws: WebSocket, token: str = ""):
    data = decode_token(token) if token else None
    await mgr.connect(ws, "draft")
    try:
        while True:
            await ws.receive_text()  # keep-alive ping from client
    except WebSocketDisconnect:
        mgr.disconnect(ws, "draft")
