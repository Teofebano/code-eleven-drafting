import asyncio, csv, io, json, os, random, sqlite3, time, uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import Cookie, Depends, FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from passlib.context import CryptContext
from jose import JWTError, jwt

# ── CONFIG ───────────────────────────────────────────────────────────────
SECRET_KEY          = os.getenv("SECRET_KEY", "code-eleven-secret-change-in-prod-2025")
ALGORITHM           = "HS256"
TOKEN_EXPIRE_HOURS  = 24
DB_PATH             = Path("data/draft.db")
MCQ_TIMER_SECONDS   = 15
ADMIN_DEFAULT_PW    = os.getenv("ADMIN_PASSWORD", "codeeleven2025")
PLAYER_ACCESS_KEY   = os.getenv("PLAYER_ACCESS_KEY", "players2025")

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ── DB ───────────────────────────────────────────────────────────────────
def get_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    conn = get_db(); c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS admins (
        id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL, created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS captains (
        id TEXT PRIMARY KEY, name TEXT NOT NULL,
        password_hash TEXT NOT NULL, created_at TEXT DEFAULT (datetime('now'))
    );
    -- master player registry
    CREATE TABLE IF NOT EXISTS players_db (
        id TEXT PRIMARY KEY, name TEXT NOT NULL,
        position TEXT NOT NULL DEFAULT 'MID',
        batch_year INTEGER NOT NULL, city TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(name, batch_year)
    );
    -- events
    CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY, name TEXT NOT NULL,
        description TEXT DEFAULT '', status TEXT DEFAULT 'setup',
        created_at TEXT DEFAULT (datetime('now'))
    );
    -- per-event player roster (links players_db to event)
    CREATE TABLE IF NOT EXISTS event_players (
        id TEXT PRIMARY KEY,
        event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        player_db_id TEXT REFERENCES players_db(id) ON DELETE SET NULL,
        name TEXT NOT NULL, position TEXT NOT NULL,
        batch_year INTEGER NOT NULL,
        taken_by TEXT REFERENCES captains(id) ON DELETE SET NULL,
        pick_order INTEGER DEFAULT NULL
    );
    -- per-event questions pool
    CREATE TABLE IF NOT EXISTS questions (
        id TEXT PRIMARY KEY, text TEXT NOT NULL,
        option_a TEXT NOT NULL, option_b TEXT NOT NULL,
        option_c TEXT NOT NULL, option_d TEXT NOT NULL,
        correct_index INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    );
    -- game state keyed by event
    CREATE TABLE IF NOT EXISTS game_state (
        event_id TEXT NOT NULL, key TEXT NOT NULL,
        value TEXT, PRIMARY KEY(event_id, key)
    );
    -- draft history per event
    CREATE TABLE IF NOT EXISTS draft_history (
        id TEXT PRIMARY KEY, event_id TEXT NOT NULL,
        captain_id TEXT NOT NULL, captain_name TEXT NOT NULL,
        player_id TEXT NOT NULL, player_name TEXT NOT NULL,
        player_position TEXT NOT NULL, player_year INTEGER NOT NULL,
        group_id TEXT NOT NULL, pick_number INTEGER NOT NULL,
        picked_at TEXT DEFAULT (datetime('now'))
    );
    -- MCQ answers per round
    CREATE TABLE IF NOT EXISTS mcq_answers (
        id TEXT PRIMARY KEY, round_id TEXT NOT NULL,
        captain_id TEXT NOT NULL, chosen_index INTEGER,
        is_correct INTEGER NOT NULL, answered_at_ms INTEGER NOT NULL
    );
    -- fixtures
    CREATE TABLE IF NOT EXISTS fixtures (
        id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        home_captain_id TEXT NOT NULL REFERENCES captains(id),
        away_captain_id TEXT NOT NULL REFERENCES captains(id),
        match_date TEXT DEFAULT NULL,
        home_score INTEGER DEFAULT NULL,
        away_score INTEGER DEFAULT NULL,
        status TEXT DEFAULT 'scheduled',
        created_at TEXT DEFAULT (datetime('now'))
    );
    -- per-fixture player events (goals, assists, cleansheets)
    CREATE TABLE IF NOT EXISTS fixture_events (
        id TEXT PRIMARY KEY,
        fixture_id TEXT NOT NULL REFERENCES fixtures(id) ON DELETE CASCADE,
        event_id TEXT NOT NULL,
        player_id TEXT NOT NULL,
        player_name TEXT NOT NULL,
        captain_id TEXT NOT NULL,
        event_type TEXT NOT NULL,  -- 'goal','assist','cleansheet'
        minute INTEGER DEFAULT NULL
    );
    """)
    # seed admin
    if not c.execute("SELECT id FROM admins LIMIT 1").fetchone():
        c.execute("INSERT INTO admins(id,username,password_hash) VALUES(?,?,?)",
                  (str(uuid.uuid4()), "admin", pwd_ctx.hash(ADMIN_DEFAULT_PW)))
    # migrate legacy data
    _migrate_legacy(c)
    conn.commit(); conn.close()

def _migrate_legacy(c):
    """Move old players/draft_history into a 'Legacy' event if they exist."""
    try:
        old_players = c.execute("SELECT * FROM players").fetchall()
        if not old_players:
            return
        # check if already migrated
        if c.execute("SELECT id FROM events WHERE name='Legacy'").fetchone():
            return
        eid = str(uuid.uuid4())
        c.execute("INSERT INTO events(id,name,description,status) VALUES(?,?,?,?)",
                  (eid,"Legacy","Migrated from previous version","done"))
        for p in old_players:
            pid = str(uuid.uuid4())
            # upsert into players_db
            c.execute("""INSERT OR IGNORE INTO players_db(id,name,position,batch_year)
                         VALUES(?,?,?,?)""",
                      (str(uuid.uuid4()), p["name"], p["position"] if "position" in p.keys() else "MID",
                       p["batch_year"] if "batch_year" in p.keys() else p.get("year",2000)))
            db_row = c.execute("SELECT id FROM players_db WHERE name=? AND batch_year=?",
                               (p["name"], p["batch_year"] if "batch_year" in p.keys() else p.get("year",2000))).fetchone()
            c.execute("""INSERT INTO event_players(id,event_id,player_db_id,name,position,batch_year,taken_by)
                         VALUES(?,?,?,?,?,?,?)""",
                      (pid, eid, db_row["id"] if db_row else None,
                       p["name"],
                       p["position"] if "position" in p.keys() else "MID",
                       p["batch_year"] if "batch_year" in p.keys() else 2000,
                       p["taken_by"] if p["taken_by"] else None))
        # migrate draft history
        try:
            old_hist = c.execute("SELECT * FROM draft_history").fetchall()
            for h in old_hist:
                if "event_id" not in h.keys():
                    c.execute("""INSERT OR IGNORE INTO draft_history
                        (id,event_id,captain_id,captain_name,player_id,player_name,
                         player_position,player_year,group_id,pick_number,picked_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (str(uuid.uuid4()), eid,
                         h["captain_id"], h["captain_name"], h["player_id"],
                         h["player_name"], h["player_position"], h["player_year"],
                         h["group_id"], h["pick_number"], h["picked_at"]))
        except Exception:
            pass
    except Exception as e:
        print(f"Migration note: {e}")

# ── GAME STATE ───────────────────────────────────────────────────────────
def gs_get(conn, event_id, key, default=None):
    row = conn.execute("SELECT value FROM game_state WHERE event_id=? AND key=?",
                       (event_id, key)).fetchone()
    if row is None: return default
    try: return json.loads(row["value"])
    except: return row["value"]

def gs_set(conn, event_id, key, value):
    conn.execute("INSERT OR REPLACE INTO game_state(event_id,key,value) VALUES(?,?,?)",
                 (event_id, key, json.dumps(value)))
    conn.commit()

# ── AUTH ─────────────────────────────────────────────────────────────────
def make_token(sub, role, name=""):
    exp = datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS)
    return jwt.encode({"sub": sub, "role": role, "name": name, "exp": exp}, SECRET_KEY, ALGORITHM)

def decode_token(token):
    try: return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except: return None

def require_admin(token: str = Cookie(default=None)):
    if not token: raise HTTPException(401, "Not authenticated")
    d = decode_token(token)
    if not d or d.get("role") != "admin": raise HTTPException(403, "Admin only")
    return d

def require_captain(token: str = Cookie(default=None)):
    if not token: raise HTTPException(401, "Not authenticated")
    d = decode_token(token)
    if not d or d.get("role") not in ("captain","admin"): raise HTTPException(403, "Captain required")
    return d

def require_any(token: str = Cookie(default=None)):
    if not token: raise HTTPException(401, "Not authenticated")
    d = decode_token(token)
    if not d: raise HTTPException(401, "Invalid token")
    return d

# ── WEBSOCKET ─────────────────────────────────────────────────────────────
class WSManager:
    def __init__(self): self.conns: dict[str, list[WebSocket]] = {}
    async def connect(self, ws, room):
        await ws.accept()
        self.conns.setdefault(room, []).append(ws)
    def disconnect(self, ws, room):
        if room in self.conns:
            self.conns[room] = [c for c in self.conns[room] if c != ws]
    async def broadcast(self, room, data):
        dead = []
        for ws in self.conns.get(room, []):
            try: await ws.send_json(data)
            except: dead.append(ws)
        for ws in dead: self.disconnect(ws, room)
    def count(self, room): return len(self.conns.get(room, []))

mgr = WSManager()
mcq_tasks: dict[str, asyncio.Task] = {}

# ── MCQ TIMER ─────────────────────────────────────────────────────────────
async def mcq_countdown(event_id, round_id, group_id):
    await asyncio.sleep(MCQ_TIMER_SECONDS)
    conn = get_db()
    try:
        if gs_get(conn, event_id, "current_round_id") != round_id: return
        if gs_get(conn, event_id, "phase") != "mcq": return
        answers = conn.execute("SELECT * FROM mcq_answers WHERE round_id=?", (round_id,)).fetchall()
        correct = sorted([a for a in answers if a["is_correct"]], key=lambda a: a["answered_at_ms"])
        wrong   = sorted([a for a in answers if not a["is_correct"]], key=lambda a: a["answered_at_ms"])
        all_caps = [r["id"] for r in conn.execute("SELECT id FROM captains").fetchall()]
        answered = {a["captain_id"] for a in answers}
        no_ans   = [c for c in all_caps if c not in answered]
        order    = [a["captain_id"] for a in correct] + [a["captain_id"] for a in wrong] + no_ans
        gs_set(conn, event_id, "draft_order", order)
        gs_set(conn, event_id, "current_picker_index", 0)
        gs_set(conn, event_id, "phase", "draft")
        await mgr.broadcast(f"event:{event_id}", {"type":"phase_change","phase":"draft","draft_order":order,"group_id":group_id})
    finally: conn.close()

# ── GROUPS ────────────────────────────────────────────────────────────────
GROUP_ORDER  = ["g1","g2","g3"]
GROUP_LABELS = {"g1":"≤ 2004","g2":"2005 – 2018","g3":"> 2018"}
def get_group(y):
    if y <= 2004: return "g1"
    if y <= 2018: return "g2"
    return "g3"

# ── APP ───────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app):
    init_db(); yield

app = FastAPI(lifespan=lifespan, title="Code Eleven Draft")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── PAGES ─────────────────────────────────────────────────────────────────
async def _html(path):
    async with aiofiles.open(path, "r") as f: return await f.read()

@app.get("/", response_class=HTMLResponse)
async def pg_index(): return await _html("templates/index.html")
@app.get("/admin", response_class=HTMLResponse)
async def pg_admin(): return await _html("templates/admin.html")
@app.get("/captain", response_class=HTMLResponse)
async def pg_captain(): return await _html("templates/captain.html")
@app.get("/results", response_class=HTMLResponse)
async def pg_results(): return await _html("templates/results.html")

# ── AUTH ──────────────────────────────────────────────────────────────────
@app.post("/api/auth/admin-login")
async def auth_admin(username: str = Form(...), password: str = Form(...)):
    conn = get_db()
    row = conn.execute("SELECT * FROM admins WHERE username=?", (username,)).fetchone()
    conn.close()
    if not row or not pwd_ctx.verify(password, row["password_hash"]):
        raise HTTPException(401, "Invalid credentials")
    resp = JSONResponse({"ok":True,"name":row["username"]})
    resp.set_cookie("token", make_token(row["id"],"admin",row["username"]),
                    httponly=True, samesite="lax", max_age=TOKEN_EXPIRE_HOURS*3600)
    return resp

@app.post("/api/auth/captain-login")
async def auth_captain(captain_id: str = Form(...), password: str = Form(...)):
    conn = get_db()
    row = conn.execute("SELECT * FROM captains WHERE id=?", (captain_id,)).fetchone()
    conn.close()
    if not row or not pwd_ctx.verify(password, row["password_hash"]):
        raise HTTPException(401, "Invalid credentials")
    resp = JSONResponse({"ok":True,"name":row["name"],"captain_id":row["id"]})
    resp.set_cookie("token", make_token(row["id"],"captain",row["name"]),
                    httponly=True, samesite="lax", max_age=TOKEN_EXPIRE_HOURS*3600)
    return resp

@app.post("/api/auth/player-login")
async def auth_player(access_key: str = Form(...)):
    if access_key != PLAYER_ACCESS_KEY: raise HTTPException(401, "Invalid key")
    resp = JSONResponse({"ok":True})
    resp.set_cookie("token", make_token("player","player","Player"),
                    httponly=True, samesite="lax", max_age=TOKEN_EXPIRE_HOURS*3600)
    return resp

@app.post("/api/auth/logout")
async def auth_logout():
    resp = JSONResponse({"ok":True}); resp.delete_cookie("token"); return resp

@app.get("/api/auth/me")
async def auth_me(token: str = Cookie(default=None)):
    if not token: return JSONResponse({"role":None})
    d = decode_token(token)
    if not d: return JSONResponse({"role":None})
    return JSONResponse({"role":d.get("role"),"name":d.get("name"),"sub":d.get("sub"),"ok":True})

# ── ADMINS ────────────────────────────────────────────────────────────────
@app.get("/api/admins")
async def list_admins(auth=Depends(require_admin)):
    conn = get_db()
    rows = conn.execute("SELECT id,username,created_at FROM admins").fetchall()
    conn.close(); return [dict(r) for r in rows]

@app.post("/api/admins")
async def create_admin(username: str = Form(...), password: str = Form(...), auth=Depends(require_admin)):
    conn = get_db()
    try:
        conn.execute("INSERT INTO admins(id,username,password_hash) VALUES(?,?,?)",
                     (str(uuid.uuid4()), username, pwd_ctx.hash(password)))
        conn.commit()
    except sqlite3.IntegrityError: raise HTTPException(400, "Username taken")
    finally: conn.close()
    return {"ok":True}

@app.delete("/api/admins/{aid}")
async def delete_admin(aid: str, auth=Depends(require_admin)):
    conn = get_db(); conn.execute("DELETE FROM admins WHERE id=?", (aid,)); conn.commit(); conn.close()
    return {"ok":True}

# ── CAPTAINS ──────────────────────────────────────────────────────────────
@app.get("/api/captains")
async def list_captains(token: str = Cookie(default=None)):
    conn = get_db()
    d = decode_token(token) if token else None
    if d and d.get("role") == "admin":
        rows = conn.execute("SELECT id,name,created_at FROM captains ORDER BY name").fetchall()
    else:
        rows = conn.execute("SELECT id,name FROM captains ORDER BY name").fetchall()
    conn.close(); return [dict(r) for r in rows]

@app.post("/api/captains")
async def create_captain(name: str = Form(...), password: str = Form(...), auth=Depends(require_admin)):
    conn = get_db()
    conn.execute("INSERT INTO captains(id,name,password_hash) VALUES(?,?,?)",
                 (str(uuid.uuid4()), name, pwd_ctx.hash(password)))
    conn.commit(); conn.close()
    await mgr.broadcast("global", {"type":"captains_updated"})
    return {"ok":True}

@app.put("/api/captains/{cid}/password")
async def update_captain_pw(cid: str, password: str = Form(...), auth=Depends(require_admin)):
    conn = get_db()
    conn.execute("UPDATE captains SET password_hash=? WHERE id=?", (pwd_ctx.hash(password), cid))
    conn.commit(); conn.close(); return {"ok":True}

@app.delete("/api/captains/{cid}")
async def delete_captain(cid: str, auth=Depends(require_admin)):
    conn = get_db(); conn.execute("DELETE FROM captains WHERE id=?", (cid,)); conn.commit(); conn.close()
    await mgr.broadcast("global", {"type":"captains_updated"})
    return {"ok":True}

# ── PLAYERS DB ────────────────────────────────────────────────────────────
@app.get("/api/players-db")
async def list_players_db(q: str = "", auth=Depends(require_admin)):
    conn = get_db()
    if q:
        rows = conn.execute("""SELECT * FROM players_db WHERE name LIKE ? OR city LIKE ?
                               ORDER BY name""", (f"%{q}%", f"%{q}%")).fetchall()
    else:
        rows = conn.execute("SELECT * FROM players_db ORDER BY name").fetchall()
    conn.close(); return [dict(r) for r in rows]

@app.post("/api/players-db")
async def create_player_db(name: str = Form(...), position: str = Form(...),
                            batch_year: int = Form(...), city: str = Form(default=""),
                            auth=Depends(require_admin)):
    conn = get_db()
    try:
        conn.execute("INSERT INTO players_db(id,name,position,batch_year,city) VALUES(?,?,?,?,?)",
                     (str(uuid.uuid4()), name.strip(), position.upper(), batch_year, city.strip()))
        conn.commit()
    except sqlite3.IntegrityError: raise HTTPException(400, "Player already exists (same name+year)")
    finally: conn.close()
    return {"ok":True}

@app.put("/api/players-db/{pid}")
async def update_player_db(pid: str, name: str = Form(...), position: str = Form(...),
                            batch_year: int = Form(...), city: str = Form(default=""),
                            auth=Depends(require_admin)):
    conn = get_db()
    conn.execute("UPDATE players_db SET name=?,position=?,batch_year=?,city=? WHERE id=?",
                 (name.strip(), position.upper(), batch_year, city.strip(), pid))
    conn.commit(); conn.close(); return {"ok":True}

@app.delete("/api/players-db/{pid}")
async def delete_player_db(pid: str, auth=Depends(require_admin)):
    conn = get_db(); conn.execute("DELETE FROM players_db WHERE id=?", (pid,)); conn.commit(); conn.close()
    return {"ok":True}

@app.post("/api/players-db/csv")
async def import_players_db_csv(file: UploadFile = File(...), auth=Depends(require_admin)):
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        if not rows: raise HTTPException(400, "Empty CSV")
        def fc(row, *keys):
            for k in row:
                if k.strip().lower().replace(" ","_") in keys: return k
            return None
        s = rows[0]
        nc = fc(s,"name","player_name","player")
        yc = fc(s,"batch_year","year","batch","angkatan")
        pc = fc(s,"position","pos","posisi")
        cc = fc(s,"city","kota","domisili")
        if not all([nc,yc,pc]): raise HTTPException(400, f"Need: name, batch_year, position. Got: {list(s.keys())}")
        conn = get_db()
        added = updated = errors = 0
        for i, row in enumerate(rows, 1):
            try:
                n = row[nc].strip(); y = int(row[yc].strip())
                p = row[pc].strip().upper()
                city = row[cc].strip() if cc and cc in row else ""
                if not n: continue
                existing = conn.execute("SELECT id,city FROM players_db WHERE name=? AND batch_year=?", (n,y)).fetchone()
                if existing:
                    # update position; only update city if csv has it and db is empty
                    new_city = city if city else existing["city"]
                    conn.execute("UPDATE players_db SET position=?,city=? WHERE id=?",
                                 (p, new_city, existing["id"]))
                    updated += 1
                else:
                    conn.execute("INSERT INTO players_db(id,name,position,batch_year,city) VALUES(?,?,?,?,?)",
                                 (str(uuid.uuid4()), n, p, y, city))
                    added += 1
            except Exception: errors += 1
        conn.commit(); conn.close()
        return {"ok":True,"added":added,"updated":updated,"errors":errors}
    except HTTPException: raise
    except Exception as e: raise HTTPException(400, f"Parse error: {e}")

# ── EVENTS ────────────────────────────────────────────────────────────────
@app.get("/api/events")
async def list_events(token: str = Cookie(default=None)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM events ORDER BY created_at DESC").fetchall()
    conn.close(); return [dict(r) for r in rows]

@app.post("/api/events")
async def create_event(name: str = Form(...), description: str = Form(default=""), auth=Depends(require_admin)):
    conn = get_db()
    eid = str(uuid.uuid4())
    conn.execute("INSERT INTO events(id,name,description) VALUES(?,?,?)", (eid, name.strip(), description.strip()))
    conn.commit(); conn.close()
    await mgr.broadcast("global", {"type":"events_updated"})
    return {"ok":True,"id":eid}

@app.put("/api/events/{eid}")
async def update_event(eid: str, name: str = Form(...), description: str = Form(default=""), auth=Depends(require_admin)):
    conn = get_db()
    conn.execute("UPDATE events SET name=?,description=? WHERE id=?", (name.strip(), description.strip(), eid))
    conn.commit(); conn.close()
    await mgr.broadcast("global", {"type":"events_updated"})
    return {"ok":True}

@app.delete("/api/events/{eid}")
async def delete_event(eid: str, auth=Depends(require_admin)):
    conn = get_db(); conn.execute("DELETE FROM events WHERE id=?", (eid,)); conn.commit(); conn.close()
    await mgr.broadcast("global", {"type":"events_updated"})
    return {"ok":True}

# ── EVENT PLAYERS ─────────────────────────────────────────────────────────
@app.get("/api/events/{eid}/players")
async def list_event_players(eid: str, token: str = Cookie(default=None)):
    conn = get_db()
    rows = conn.execute("""
        SELECT ep.*, c.name as captain_name
        FROM event_players ep LEFT JOIN captains c ON ep.taken_by=c.id
        WHERE ep.event_id=? ORDER BY ep.batch_year
    """, (eid,)).fetchall()
    conn.close(); return [dict(r) for r in rows]

@app.post("/api/events/{eid}/players")
async def add_event_player(eid: str, name: str = Form(...), position: str = Form(...),
                            batch_year: int = Form(...), auth=Depends(require_admin)):
    conn = get_db()
    db_row = conn.execute("SELECT id FROM players_db WHERE name=? AND batch_year=?",
                          (name.strip(), batch_year)).fetchone()
    pid = str(uuid.uuid4())
    conn.execute("INSERT INTO event_players(id,event_id,player_db_id,name,position,batch_year) VALUES(?,?,?,?,?,?)",
                 (pid, eid, db_row["id"] if db_row else None, name.strip(), position.upper(), batch_year))
    conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}", {"type":"players_updated"})
    return {"ok":True}

@app.post("/api/events/{eid}/players/from-db")
async def add_player_from_db(eid: str, body: dict, auth=Depends(require_admin)):
    player_db_ids = body.get("player_db_ids", [])
    conn = get_db()
    added = 0
    for dbid in player_db_ids:
        p = conn.execute("SELECT * FROM players_db WHERE id=?", (dbid,)).fetchone()
        if not p: continue
        exists = conn.execute("SELECT id FROM event_players WHERE event_id=? AND player_db_id=?",
                              (eid, dbid)).fetchone()
        if exists: continue
        conn.execute("INSERT INTO event_players(id,event_id,player_db_id,name,position,batch_year) VALUES(?,?,?,?,?,?)",
                     (str(uuid.uuid4()), eid, dbid, p["name"], p["position"], p["batch_year"]))
        added += 1
    conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}", {"type":"players_updated"})
    return {"ok":True,"added":added}

@app.post("/api/events/{eid}/players/csv")
async def upload_event_players_csv(eid: str, file: UploadFile = File(...), auth=Depends(require_admin)):
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        if not rows: raise HTTPException(400, "Empty CSV")
        def fc(row, *keys):
            for k in row:
                if k.strip().lower().replace(" ","_") in keys: return k
            return None
        s = rows[0]
        nc = fc(s,"name","player_name","player")
        yc = fc(s,"batch_year","year","batch","angkatan")
        pc = fc(s,"position","pos","posisi")
        cc = fc(s,"city","kota","domisili")
        if not all([nc,yc,pc]): raise HTTPException(400, f"Need: name, batch_year, position. Got: {list(s.keys())}")
        conn = get_db(); added = db_added = errors = 0; errs = []
        for i, row in enumerate(rows, 1):
            try:
                n = row[nc].strip(); y = int(row[yc].strip())
                p = row[pc].strip().upper()
                city = row[cc].strip() if cc and cc in row else ""
                if not n: continue
                # upsert players_db
                existing_db = conn.execute("SELECT id,city FROM players_db WHERE name=? AND batch_year=?", (n,y)).fetchone()
                if existing_db:
                    new_city = city if city else existing_db["city"]
                    conn.execute("UPDATE players_db SET position=?,city=? WHERE id=?", (p, new_city, existing_db["id"]))
                    dbid = existing_db["id"]
                else:
                    dbid = str(uuid.uuid4())
                    conn.execute("INSERT INTO players_db(id,name,position,batch_year,city) VALUES(?,?,?,?,?)",
                                 (dbid, n, p, y, city))
                    db_added += 1
                # add to event if not there
                exists = conn.execute("SELECT id FROM event_players WHERE event_id=? AND player_db_id=?",
                                      (eid, dbid)).fetchone()
                if not exists:
                    conn.execute("INSERT INTO event_players(id,event_id,player_db_id,name,position,batch_year) VALUES(?,?,?,?,?,?)",
                                 (str(uuid.uuid4()), eid, dbid, n, p, y))
                    added += 1
            except Exception as e: errs.append(f"Row {i}: {e}"); errors += 1
        conn.commit(); conn.close()
        await mgr.broadcast(f"event:{eid}", {"type":"players_updated"})
        return {"ok":True,"added_to_event":added,"added_to_db":db_added,"errors":errors,"error_details":errs}
    except HTTPException: raise
    except Exception as e: raise HTTPException(400, f"Parse error: {e}")

@app.put("/api/events/{eid}/players/{pid}/assign")
async def assign_event_player(eid: str, pid: str, body: dict, auth=Depends(require_admin)):
    captain_id = body.get("captain_id")
    conn = get_db()
    conn.execute("UPDATE event_players SET taken_by=? WHERE id=? AND event_id=?", (captain_id, pid, eid))
    conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}", {"type":"players_updated"})
    return {"ok":True}

@app.delete("/api/events/{eid}/players/{pid}")
async def delete_event_player(eid: str, pid: str, auth=Depends(require_admin)):
    conn = get_db()
    conn.execute("DELETE FROM event_players WHERE id=? AND event_id=?", (pid, eid))
    conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}", {"type":"players_updated"})
    return {"ok":True}

@app.delete("/api/events/{eid}/players")
async def clear_event_players(eid: str, auth=Depends(require_admin)):
    conn = get_db()
    conn.execute("DELETE FROM event_players WHERE event_id=?", (eid,))
    conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}", {"type":"players_updated"})
    return {"ok":True}

# ── QUESTIONS ─────────────────────────────────────────────────────────────
@app.get("/api/questions")
async def list_questions(auth=Depends(require_admin)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM questions ORDER BY created_at").fetchall()
    conn.close(); return [dict(r) for r in rows]

@app.post("/api/questions")
async def create_question(text: str = Form(...),
    option_a: str = Form(...), option_b: str = Form(...),
    option_c: str = Form(...), option_d: str = Form(...),
    correct_index: int = Form(...), auth=Depends(require_admin)):
    conn = get_db()
    conn.execute("INSERT INTO questions(id,text,option_a,option_b,option_c,option_d,correct_index) VALUES(?,?,?,?,?,?,?)",
                 (str(uuid.uuid4()), text, option_a, option_b, option_c, option_d, correct_index))
    conn.commit(); conn.close(); return {"ok":True}

@app.delete("/api/questions/{qid}")
async def delete_question(qid: str, auth=Depends(require_admin)):
    conn = get_db(); conn.execute("DELETE FROM questions WHERE id=?", (qid,)); conn.commit(); conn.close()
    return {"ok":True}

# ── GAME STATE ────────────────────────────────────────────────────────────
@app.get("/api/events/{eid}/game")
async def get_game(eid: str, token: str = Cookie(default=None)):
    conn = get_db()
    event = conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
    if not event: raise HTTPException(404, "Event not found")
    d = decode_token(token) if token else None
    phase         = gs_get(conn, eid, "phase", "lobby")
    group_index   = gs_get(conn, eid, "current_group_index", -1)
    current_group = gs_get(conn, eid, "current_group_id")
    draft_order   = gs_get(conn, eid, "draft_order", [])
    picker_index  = gs_get(conn, eid, "current_picker_index", 0)
    round_id      = gs_get(conn, eid, "current_round_id")
    qid           = gs_get(conn, eid, "current_question_id")
    q_data = None
    if qid:
        q = conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
        if q:
            q_data = {"id":q["id"],"text":q["text"],"options":[q["option_a"],q["option_b"],q["option_c"],q["option_d"]]}
            if d and d.get("role") == "admin": q_data["correct_index"] = q["correct_index"]
    answers = []
    if round_id:
        answers = [dict(a) for a in conn.execute("SELECT * FROM mcq_answers WHERE round_id=?", (round_id,)).fetchall()]
    players = [dict(p) for p in conn.execute("""
        SELECT ep.*, c.name as captain_name FROM event_players ep
        LEFT JOIN captains c ON ep.taken_by=c.id
        WHERE ep.event_id=? ORDER BY ep.batch_year
    """, (eid,)).fetchall()]
    captains = [dict(c) for c in conn.execute("SELECT id,name FROM captains ORDER BY name").fetchall()]
    history  = [dict(h) for h in conn.execute(
        "SELECT * FROM draft_history WHERE event_id=? ORDER BY pick_number", (eid,)).fetchall()]
    conn.close()
    return {"phase":phase,"current_group":current_group,"group_index":group_index,
            "draft_order":draft_order,"current_picker_index":picker_index,
            "question":q_data,"answers":answers,"round_id":round_id,
            "players":players,"captains":captains,"history":history,
            "event":dict(event)}

@app.post("/api/events/{eid}/game/start-round")
async def start_round(eid: str, auth=Depends(require_admin)):
    conn = get_db()
    qs = conn.execute("SELECT * FROM questions").fetchall()
    if not qs: conn.close(); raise HTTPException(400, "Add questions first")
    caps = conn.execute("SELECT id FROM captains").fetchall()
    if not caps: conn.close(); raise HTTPException(400, "Add captains first")
    cur = gs_get(conn, eid, "current_group_index", -1)
    nxt = cur + 1
    if nxt >= 3: conn.close(); raise HTTPException(400, "All 3 rounds complete")
    gid = GROUP_ORDER[nxt]; q = random.choice(qs); rid = str(uuid.uuid4())
    gs_set(conn, eid, "phase", "mcq")
    gs_set(conn, eid, "current_group_index", nxt)
    gs_set(conn, eid, "current_group_id", gid)
    gs_set(conn, eid, "current_question_id", q["id"])
    gs_set(conn, eid, "current_round_id", rid)
    gs_set(conn, eid, "draft_order", [])
    gs_set(conn, eid, "current_picker_index", 0)
    conn.close()
    if eid in mcq_tasks and not mcq_tasks[eid].done(): mcq_tasks[eid].cancel()
    mcq_tasks[eid] = asyncio.create_task(mcq_countdown(eid, rid, gid))
    await mgr.broadcast(f"event:{eid}", {"type":"round_started","group_id":gid,"group_index":nxt,
        "round_id":rid,"question":{"id":q["id"],"text":q["text"],
        "options":[q["option_a"],q["option_b"],q["option_c"],q["option_d"]]},"timer_seconds":MCQ_TIMER_SECONDS})
    return {"ok":True,"group_id":gid}

@app.post("/api/events/{eid}/game/set-draft-order")
async def set_draft_order(eid: str, body: dict, auth=Depends(require_admin)):
    conn = get_db()
    gs_set(conn, eid, "draft_order", body.get("order",[]))
    gs_set(conn, eid, "phase", "draft")
    conn.close()
    await mgr.broadcast(f"event:{eid}", {"type":"phase_change","phase":"draft","draft_order":body.get("order",[])})
    return {"ok":True}

@app.post("/api/events/{eid}/game/end-draft")
async def end_draft(eid: str, auth=Depends(require_admin)):
    conn = get_db()
    gs_set(conn, eid, "phase", "done")
    conn.execute("UPDATE events SET status='done' WHERE id=?", (eid,))
    conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}", {"type":"phase_change","phase":"done"})
    return {"ok":True}

@app.post("/api/events/{eid}/game/reset")
async def reset_game(eid: str, auth=Depends(require_admin)):
    if eid in mcq_tasks and not mcq_tasks[eid].done(): mcq_tasks[eid].cancel()
    conn = get_db()
    conn.execute("DELETE FROM game_state WHERE event_id=?", (eid,))
    conn.execute("UPDATE event_players SET taken_by=NULL,pick_order=NULL WHERE event_id=?", (eid,))
    conn.execute("DELETE FROM draft_history WHERE event_id=?", (eid,))
    conn.execute("DELETE FROM mcq_answers WHERE round_id IN (SELECT value FROM game_state WHERE event_id=?)", (eid,))
    conn.execute("UPDATE events SET status='setup' WHERE id=?", (eid,))
    conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}", {"type":"reset"})
    return {"ok":True}

@app.post("/api/events/{eid}/game/answer")
async def submit_answer(eid: str, body: dict, auth=Depends(require_captain)):
    captain_id = auth["sub"]
    chosen = body.get("chosen_index")
    conn = get_db()
    if gs_get(conn, eid, "phase") != "mcq": conn.close(); raise HTTPException(400, "Not MCQ phase")
    rid = gs_get(conn, eid, "current_round_id")
    if conn.execute("SELECT id FROM mcq_answers WHERE round_id=? AND captain_id=?", (rid,captain_id)).fetchone():
        conn.close(); raise HTTPException(400, "Already answered")
    qid = gs_get(conn, eid, "current_question_id")
    q = conn.execute("SELECT correct_index FROM questions WHERE id=?", (qid,)).fetchone()
    is_correct = (chosen == q["correct_index"]) if q and chosen is not None else False
    conn.execute("INSERT INTO mcq_answers(id,round_id,captain_id,chosen_index,is_correct,answered_at_ms) VALUES(?,?,?,?,?,?)",
                 (str(uuid.uuid4()), rid, captain_id, chosen, int(is_correct), int(time.time()*1000)))
    conn.commit()
    total = conn.execute("SELECT COUNT(*) as n FROM captains").fetchone()["n"]
    answered = conn.execute("SELECT COUNT(*) as n FROM mcq_answers WHERE round_id=?", (rid,)).fetchone()["n"]
    gid = gs_get(conn, eid, "current_group_id"); conn.close()
    await mgr.broadcast(f"event:{eid}", {"type":"captain_answered","captain_id":captain_id,"is_correct":is_correct,"chosen_index":chosen})
    if answered >= total:
        if eid in mcq_tasks and not mcq_tasks[eid].done(): mcq_tasks[eid].cancel()
        await mcq_countdown(eid, rid, gid)
    return {"ok":True,"is_correct":is_correct}

@app.post("/api/events/{eid}/game/pick")
async def pick_player(eid: str, body: dict, auth=Depends(require_captain)):
    captain_id = auth["sub"]
    player_id = body.get("player_id")
    conn = get_db()
    if gs_get(conn, eid, "phase") != "draft": conn.close(); raise HTTPException(400, "Not draft phase")
    order = gs_get(conn, eid, "draft_order", [])
    pidx  = gs_get(conn, eid, "current_picker_index", 0)
    if not order or pidx >= len(order): conn.close(); raise HTTPException(400, "No draft order")
    if order[pidx] != captain_id and auth.get("role") != "admin":
        conn.close(); raise HTTPException(403, "Not your turn")
    p = conn.execute("SELECT * FROM event_players WHERE id=? AND event_id=?", (player_id, eid)).fetchone()
    if not p: conn.close(); raise HTTPException(404, "Player not found")
    if p["taken_by"]: conn.close(); raise HTTPException(400, "Already taken")
    pick_num = conn.execute("SELECT COUNT(*) as n FROM draft_history WHERE event_id=?", (eid,)).fetchone()["n"] + 1
    cap = conn.execute("SELECT name FROM captains WHERE id=?", (captain_id,)).fetchone()
    conn.execute("UPDATE event_players SET taken_by=?,pick_order=? WHERE id=?", (captain_id, pick_num, player_id))
    conn.execute("""INSERT INTO draft_history(id,event_id,captain_id,captain_name,player_id,player_name,
                    player_position,player_year,group_id,pick_number) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                 (str(uuid.uuid4()), eid, captain_id, cap["name"] if cap else captain_id,
                  player_id, p["name"], p["position"], p["batch_year"],
                  gs_get(conn, eid, "current_group_id","g1"), pick_num))
    gs_set(conn, eid, "current_picker_index", (pidx+1) % len(order))
    conn.close()
    await mgr.broadcast(f"event:{eid}", {"type":"player_picked","player_id":player_id,
        "player_name":p["name"],"captain_id":captain_id,"pick_number":pick_num,
        "next_picker_index":(pidx+1)%len(order)})
    return {"ok":True}

# ── FIXTURES ──────────────────────────────────────────────────────────────
@app.get("/api/events/{eid}/fixtures")
async def list_fixtures(eid: str, token: str = Cookie(default=None)):
    conn = get_db()
    rows = conn.execute("""
        SELECT f.*, ch.name as home_name, ca.name as away_name
        FROM fixtures f
        JOIN captains ch ON f.home_captain_id=ch.id
        JOIN captains ca ON f.away_captain_id=ca.id
        WHERE f.event_id=? ORDER BY f.match_date, f.created_at
    """, (eid,)).fetchall()
    result = []
    for r in rows:
        fix = dict(r)
        events = conn.execute("""SELECT * FROM fixture_events WHERE fixture_id=? ORDER BY minute""",
                              (r["id"],)).fetchall()
        fix["events"] = [dict(e) for e in events]
        result.append(fix)
    conn.close(); return result

@app.post("/api/events/{eid}/fixtures")
async def create_fixture(eid: str, home_captain_id: str = Form(...),
    away_captain_id: str = Form(...), match_date: str = Form(default=""), auth=Depends(require_admin)):
    conn = get_db()
    fid = str(uuid.uuid4())
    conn.execute("INSERT INTO fixtures(id,event_id,home_captain_id,away_captain_id,match_date) VALUES(?,?,?,?,?)",
                 (fid, eid, home_captain_id, away_captain_id, match_date or None))
    conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}", {"type":"fixtures_updated"})
    return {"ok":True,"id":fid}

@app.put("/api/events/{eid}/fixtures/{fid}/result")
async def update_fixture_result(eid: str, fid: str, body: dict, auth=Depends(require_admin)):
    conn = get_db()
    conn.execute("UPDATE fixtures SET home_score=?,away_score=?,status='played' WHERE id=? AND event_id=?",
                 (body.get("home_score"), body.get("away_score"), fid, eid))
    conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}", {"type":"fixtures_updated"})
    return {"ok":True}

@app.delete("/api/events/{eid}/fixtures/{fid}")
async def delete_fixture(eid: str, fid: str, auth=Depends(require_admin)):
    conn = get_db()
    conn.execute("DELETE FROM fixtures WHERE id=? AND event_id=?", (fid, eid))
    conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}", {"type":"fixtures_updated"})
    return {"ok":True}

@app.post("/api/events/{eid}/fixtures/{fid}/events")
async def add_fixture_event(eid: str, fid: str, body: dict, auth=Depends(require_admin)):
    conn = get_db()
    conn.execute("""INSERT INTO fixture_events(id,fixture_id,event_id,player_id,player_name,captain_id,event_type,minute)
                    VALUES(?,?,?,?,?,?,?,?)""",
                 (str(uuid.uuid4()), fid, eid,
                  body.get("player_id",""), body.get("player_name",""),
                  body.get("captain_id",""), body.get("event_type","goal"),
                  body.get("minute")))
    conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}", {"type":"fixtures_updated"})
    return {"ok":True}

@app.delete("/api/events/{eid}/fixtures/{fid}/events/{evid}")
async def delete_fixture_event(eid: str, fid: str, evid: str, auth=Depends(require_admin)):
    conn = get_db()
    conn.execute("DELETE FROM fixture_events WHERE id=?", (evid,))
    conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}", {"type":"fixtures_updated"})
    return {"ok":True}

@app.get("/api/events/{eid}/standings")
async def get_standings(eid: str, token: str = Cookie(default=None)):
    conn = get_db()
    caps = {c["id"]: c["name"] for c in conn.execute("SELECT id,name FROM captains").fetchall()}
    fixtures = conn.execute("""SELECT * FROM fixtures WHERE event_id=? AND status='played'""", (eid,)).fetchall()
    stats = {cid: {"name":name,"p":0,"w":0,"d":0,"l":0,"gf":0,"ga":0,"pts":0}
             for cid,name in caps.items()}
    for f in fixtures:
        h,a = f["home_captain_id"], f["away_captain_id"]
        hs,as_ = f["home_score"] or 0, f["away_score"] or 0
        if h not in stats: stats[h] = {"name":caps.get(h,h),"p":0,"w":0,"d":0,"l":0,"gf":0,"ga":0,"pts":0}
        if a not in stats: stats[a] = {"name":caps.get(a,a),"p":0,"w":0,"d":0,"l":0,"gf":0,"ga":0,"pts":0}
        stats[h]["p"]+=1; stats[h]["gf"]+=hs; stats[h]["ga"]+=as_
        stats[a]["p"]+=1; stats[a]["gf"]+=as_; stats[a]["ga"]+=hs
        if hs > as_:   stats[h]["w"]+=1; stats[h]["pts"]+=3; stats[a]["l"]+=1
        elif hs < as_: stats[a]["w"]+=1; stats[a]["pts"]+=3; stats[h]["l"]+=1
        else:          stats[h]["d"]+=1; stats[h]["pts"]+=1; stats[a]["d"]+=1; stats[a]["pts"]+=1
    for s in stats.values(): s["gd"] = s["gf"] - s["ga"]
    table = sorted(stats.values(), key=lambda s: (-s["pts"],-s["gd"],-s["gf"],s["name"]))
    # player stats
    evts = conn.execute("SELECT * FROM fixture_events WHERE event_id=?", (eid,)).fetchall()
    pstats: dict = {}
    for e in evts:
        pid = e["player_id"]; pn = e["player_name"]
        if pid not in pstats: pstats[pid] = {"name":pn,"goals":0,"assists":0,"cleansheets":0}
        if e["event_type"] == "goal":       pstats[pid]["goals"]      += 1
        elif e["event_type"] == "assist":   pstats[pid]["assists"]    += 1
        elif e["event_type"] == "cleansheet": pstats[pid]["cleansheets"] += 1
    conn.close()
    return {"table":table,"player_stats":sorted(pstats.values(), key=lambda p:(-p["goals"],-p["assists"],-p["cleansheets"],p["name"]))}

# ── RESULTS ENDPOINT ──────────────────────────────────────────────────────
@app.get("/api/events/{eid}/results")
async def get_results(eid: str, token: str = Cookie(default=None)):
    if not token: raise HTTPException(401, "Not authenticated")
    d = decode_token(token)
    if not d or d.get("role") not in ("player","captain","admin"): raise HTTPException(403, "Login required")
    conn = get_db()
    event = conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
    if not event: raise HTTPException(404, "Event not found")
    caps = conn.execute("SELECT id,name FROM captains ORDER BY name").fetchall()
    players = conn.execute("""
        SELECT ep.*, c.name as captain_name FROM event_players ep
        LEFT JOIN captains c ON ep.taken_by=c.id WHERE ep.event_id=? ORDER BY ep.batch_year
    """, (eid,)).fetchall()
    phase = gs_get(conn, eid, "phase", "lobby")
    conn.close()
    return {"phase":phase,"event":dict(event),
            "captains":[dict(c) for c in caps],"players":[dict(p) for p in players]}

# ── EXPORT ────────────────────────────────────────────────────────────────
@app.get("/api/events/{eid}/export/history")
async def export_history(eid: str, auth=Depends(require_admin)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM draft_history WHERE event_id=? ORDER BY pick_number", (eid,)).fetchall()
    conn.close()
    out = io.StringIO(); w = csv.writer(out)
    w.writerow(["Pick #","Captain","Player","Position","Batch Year","Group","Picked At"])
    gl = {"g1":"≤2004","g2":"2005-2018","g3":">2018"}
    for r in rows: w.writerow([r["pick_number"],r["captain_name"],r["player_name"],
                                r["player_position"],r["player_year"],gl.get(r["group_id"],r["group_id"]),r["picked_at"]])
    out.seek(0)
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition":"attachment; filename=draft-history.csv"})

@app.get("/api/events/{eid}/export/teams")
async def export_teams(eid: str, auth=Depends(require_admin)):
    conn = get_db()
    players = conn.execute("""SELECT ep.*,c.name as captain_name FROM event_players ep
        LEFT JOIN captains c ON ep.taken_by=c.id WHERE ep.event_id=? ORDER BY ep.batch_year""",(eid,)).fetchall()
    conn.close()
    out = io.StringIO(); w = csv.writer(out)
    w.writerow(["Captain","Player","Position","Batch Year","Group"])
    gl = {"g1":"≤2004","g2":"2005-2018","g3":">2018"}
    for p in players:
        g = get_group(p["batch_year"])
        w.writerow([p["captain_name"] or "Undrafted",p["name"],p["position"],p["batch_year"],gl.get(g,"")])
    out.seek(0)
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition":"attachment; filename=teams.csv"})

# ── RESTORE ───────────────────────────────────────────────────────────────
@app.post("/api/events/{eid}/restore/teams")
async def restore_teams(eid: str, file: UploadFile = File(...), auth=Depends(require_admin)):
    content = await file.read()
    try:
        text = content.decode("utf-8-sig"); reader = csv.DictReader(io.StringIO(text)); rows = list(reader)
        if not rows: raise HTTPException(400, "Empty CSV")
        conn = get_db()
        caps = {c["name"]: c["id"] for c in conn.execute("SELECT id,name FROM captains").fetchall()}
        added = assigned = 0; skipped = []
        for i, row in enumerate(rows, 1):
            try:
                cn = (row.get("Captain") or "").strip(); pn = (row.get("Player") or row.get("name") or "").strip()
                pos = (row.get("Position") or row.get("position") or "MID").strip().upper()
                by  = int((row.get("Batch Year") or row.get("batch_year") or "0").strip())
                if not pn or not by: skipped.append(f"Row {i}: missing data"); continue
                db_r = conn.execute("SELECT id FROM players_db WHERE name=? AND batch_year=?",(pn,by)).fetchone()
                if not db_r:
                    dbid = str(uuid.uuid4())
                    conn.execute("INSERT INTO players_db(id,name,position,batch_year) VALUES(?,?,?,?)",(dbid,pn,pos,by))
                else: dbid = db_r["id"]
                ep = conn.execute("SELECT id FROM event_players WHERE event_id=? AND player_db_id=?",(eid,dbid)).fetchone()
                if not ep:
                    epid = str(uuid.uuid4())
                    conn.execute("INSERT INTO event_players(id,event_id,player_db_id,name,position,batch_year) VALUES(?,?,?,?,?,?)",
                                 (epid,eid,dbid,pn,pos,by))
                    added += 1
                    ep_id = epid
                else: ep_id = ep["id"]
                cid = caps.get(cn) if cn and cn.lower() != "undrafted" else None
                if cn and cn.lower() != "undrafted" and not cid:
                    skipped.append(f"Row {i}: captain '{cn}' not found")
                else:
                    conn.execute("UPDATE event_players SET taken_by=? WHERE id=?",(cid, ep_id))
                    if cid: assigned += 1
            except Exception as e: skipped.append(f"Row {i}: {e}")
        conn.commit(); conn.close()
        await mgr.broadcast(f"event:{eid}", {"type":"players_updated"})
        return {"ok":True,"added":added,"assigned":assigned,"skipped":skipped}
    except HTTPException: raise
    except Exception as e: raise HTTPException(400, f"Error: {e}")

# ── WEBSOCKET ─────────────────────────────────────────────────────────────
@app.websocket("/ws/draft")
async def ws_draft(ws: WebSocket, token: str = "", room: str = "global"):
    await mgr.connect(ws, room)
    try:
        while True: await ws.receive_text()
    except WebSocketDisconnect: mgr.disconnect(ws, room)
