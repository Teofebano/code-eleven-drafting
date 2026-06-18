import asyncio, csv, io, json, os, random, sqlite3, string, time, uuid
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
SECRET_KEY         = os.getenv("SECRET_KEY", "code-eleven-secret-change-in-prod-2025")
ALGORITHM          = "HS256"
TOKEN_EXPIRE_HOURS = 24
DB_PATH            = Path("data/draft.db")
MCQ_TIMER_SECONDS  = 15
ADMIN_DEFAULT_PW   = os.getenv("ADMIN_PASSWORD", "codeeleven2025")

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ── HELPERS ──────────────────────────────────────────────────────────────
def gen_code(n=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=n))

def get_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def get_group(y):
    if y <= 2004: return "g1"
    if y <= 2018: return "g2"
    return "g3"

GROUP_ORDER  = ["g1","g2","g3"]
GROUP_LABELS = {"g1":"≤ 2004","g2":"2005 – 2018","g3":"> 2018"}

# ── DB INIT ──────────────────────────────────────────────────────────────
def init_db():
    conn = get_db(); c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS admins (
        id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL, created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS players_db (
        id TEXT PRIMARY KEY, name TEXT NOT NULL,
        position TEXT NOT NULL DEFAULT 'MID',
        batch_year INTEGER NOT NULL, city TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(name, batch_year)
    );
    CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY, name TEXT NOT NULL,
        description TEXT DEFAULT '',
        access_code TEXT UNIQUE NOT NULL,
        captain_password TEXT NOT NULL DEFAULT 'captain123',
        num_teams INTEGER NOT NULL DEFAULT 2,
        status TEXT DEFAULT 'setup',
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS captains (
        id TEXT PRIMARY KEY,
        event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        team_name TEXT DEFAULT NULL,
        team_number INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS event_players (
        id TEXT PRIMARY KEY,
        event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        player_db_id TEXT REFERENCES players_db(id) ON DELETE SET NULL,
        name TEXT NOT NULL, position TEXT NOT NULL,
        batch_year INTEGER NOT NULL,
        taken_by TEXT REFERENCES captains(id) ON DELETE SET NULL,
        pick_order INTEGER DEFAULT NULL
    );
    CREATE TABLE IF NOT EXISTS questions (
        id TEXT PRIMARY KEY, text TEXT NOT NULL,
        option_a TEXT NOT NULL, option_b TEXT NOT NULL,
        option_c TEXT NOT NULL, option_d TEXT NOT NULL,
        correct_index INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS game_state (
        event_id TEXT NOT NULL, key TEXT NOT NULL,
        value TEXT, PRIMARY KEY(event_id, key)
    );
    CREATE TABLE IF NOT EXISTS draft_history (
        id TEXT PRIMARY KEY, event_id TEXT NOT NULL,
        captain_id TEXT NOT NULL, captain_name TEXT NOT NULL,
        player_id TEXT NOT NULL, player_name TEXT NOT NULL,
        player_position TEXT NOT NULL, player_year INTEGER NOT NULL,
        group_id TEXT NOT NULL, pick_number INTEGER NOT NULL,
        picked_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS mcq_answers (
        id TEXT PRIMARY KEY, round_id TEXT NOT NULL,
        captain_id TEXT NOT NULL, chosen_index INTEGER,
        is_correct INTEGER NOT NULL, answered_at_ms INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS fixtures (
        id TEXT PRIMARY KEY,
        event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        home_captain_id TEXT NOT NULL REFERENCES captains(id),
        away_captain_id TEXT NOT NULL REFERENCES captains(id),
        match_date TEXT DEFAULT NULL,
        home_score INTEGER DEFAULT NULL,
        away_score INTEGER DEFAULT NULL,
        status TEXT DEFAULT 'scheduled',
        pitch_name TEXT DEFAULT NULL,
        pitch_url TEXT DEFAULT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS fixture_events (
        id TEXT PRIMARY KEY,
        fixture_id TEXT NOT NULL REFERENCES fixtures(id) ON DELETE CASCADE,
        event_id TEXT NOT NULL,
        player_id TEXT NOT NULL, player_name TEXT NOT NULL,
        captain_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        minute INTEGER DEFAULT NULL
    );
    """)
    # Add team_name column to captains if missing
    try:
        cap_cols2 = [r[1] for r in c.execute("PRAGMA table_info(captains)").fetchall()]
        if cap_cols2 and "team_name" not in cap_cols2:
            c.execute("ALTER TABLE captains ADD COLUMN team_name TEXT DEFAULT NULL")
    except Exception as e:
        print(f"team_name migration note: {e}")

    # Add pitch columns to fixtures if missing
    try:
        fx_cols = [r[1] for r in c.execute("PRAGMA table_info(fixtures)").fetchall()]
        if fx_cols and "pitch_name" not in fx_cols:
            c.execute("ALTER TABLE fixtures ADD COLUMN pitch_name TEXT DEFAULT NULL")
        if fx_cols and "pitch_url" not in fx_cols:
            c.execute("ALTER TABLE fixtures ADD COLUMN pitch_url TEXT DEFAULT NULL")
    except Exception as e:
        print(f"Fixtures schema fix note: {e}")

    # ── SCHEMA MIGRATIONS (safe to run repeatedly) ──────────────────────────
    # Fix events table: add missing columns from old schema
    try:
        ev_cols = [r[1] for r in c.execute("PRAGMA table_info(events)").fetchall()]
        if ev_cols:  # table exists
            if "access_code" not in ev_cols:
                c.execute("ALTER TABLE events ADD COLUMN access_code TEXT")
            if "captain_password" not in ev_cols:
                c.execute("ALTER TABLE events ADD COLUMN captain_password TEXT DEFAULT 'captain123'")
            if "num_teams" not in ev_cols:
                c.execute("ALTER TABLE events ADD COLUMN num_teams INTEGER DEFAULT 0")
    except Exception as e:
        print(f"Events schema fix note: {e}")

    # Fix captains table: recreate with event_id if missing
    try:
        cap_cols = [r[1] for r in c.execute("PRAGMA table_info(captains)").fetchall()]
        if cap_cols and "event_id" not in cap_cols:
            c.execute("ALTER TABLE captains RENAME TO captains_old")
            c.execute("""CREATE TABLE captains (
                id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                name TEXT NOT NULL,
                team_number INTEGER NOT NULL DEFAULT 1
            )""")
            c.execute("DROP TABLE captains_old")
    except Exception as e:
        print(f"Captains schema fix note: {e}")

    # Backfill access_code for any events that don't have one
    try:
        rows = c.execute("SELECT id, access_code FROM events").fetchall()
        for row in rows:
            if not row["access_code"] or str(row["access_code"]) == "None":
                code = gen_code()
                c.execute("UPDATE events SET access_code=? WHERE id=?", (code, row["id"]))
    except Exception as e:
        print(f"Access code backfill note: {e}")

    # seed admin
    if not c.execute("SELECT id FROM admins LIMIT 1").fetchone():
        c.execute("INSERT INTO admins(id,username,password_hash) VALUES(?,?,?)",
                  (str(uuid.uuid4()), "admin", pwd_ctx.hash(ADMIN_DEFAULT_PW)))
    # migrate legacy
    _migrate(c)
    conn.commit(); conn.close()

def _migrate(c):
    try:
        # check players table exists
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "players" not in tables: return
        old = c.execute("SELECT * FROM players LIMIT 1").fetchone()
        if not old: return
        if c.execute("SELECT id FROM events WHERE name='Copa de Labtek Lima 1st Edition'").fetchone(): return
        eid = str(uuid.uuid4())
        code = gen_code()
        c.execute("INSERT INTO events(id,name,description,access_code,captain_password,num_teams,status) VALUES(?,?,?,?,?,?,?)",
                  (eid,"Copa de Labtek Lima 1st Edition","Migrated from previous version",code,"captain123",0,"done"))
        # migrate players
        players = c.execute("SELECT * FROM players").fetchall()
        caps_map = {}
        for p in players:
            if p["taken_by"]:
                cid = p["taken_by"]
                if cid not in caps_map:
                    # try to get captain name from old captains table
                    try:
                        cap = c.execute("SELECT name FROM captains WHERE id=?", (cid,)).fetchone()
                        cname = cap["name"] if cap else cid
                    except: cname = cid
                    caps_map[cid] = cname
        # insert captains into new table
        new_cap_ids = {}
        for old_id, cname in caps_map.items():
            new_id = str(uuid.uuid4())
            try:
                c.execute("INSERT INTO captains(id,event_id,name,team_number) VALUES(?,?,?,?)",
                          (new_id, eid, cname, list(caps_map.keys()).index(old_id)+1))
                new_cap_ids[old_id] = new_id
            except: new_cap_ids[old_id] = old_id
        for p in players:
            pid = str(uuid.uuid4())
            db_r = c.execute("SELECT id FROM players_db WHERE name=? AND batch_year=?",
                             (p["name"], p["batch_year"])).fetchone()
            if not db_r:
                dbid = str(uuid.uuid4())
                c.execute("INSERT OR IGNORE INTO players_db(id,name,position,batch_year) VALUES(?,?,?,?)",
                          (dbid, p["name"], p["position"] or "MID", p["batch_year"]))
                db_r = c.execute("SELECT id FROM players_db WHERE name=? AND batch_year=?",
                                 (p["name"], p["batch_year"])).fetchone()
            new_cap_id = new_cap_ids.get(p["taken_by"]) if p["taken_by"] else None
            c.execute("INSERT INTO event_players(id,event_id,player_db_id,name,position,batch_year,taken_by) VALUES(?,?,?,?,?,?,?)",
                      (pid, eid, db_r["id"] if db_r else None, p["name"], p["position"] or "MID", p["batch_year"], new_cap_id))
    except Exception as e:
        print(f"Migration note: {e}")

# ── GAME STATE ────────────────────────────────────────────────────────────
def gs_get(conn, eid, key, default=None):
    row = conn.execute("SELECT value FROM game_state WHERE event_id=? AND key=?", (eid,key)).fetchone()
    if row is None: return default
    try: return json.loads(row["value"])
    except: return row["value"]

def gs_set(conn, eid, key, value):
    conn.execute("INSERT OR REPLACE INTO game_state(event_id,key,value) VALUES(?,?,?)",
                 (eid, key, json.dumps(value)))
    conn.commit()

# ── AUTH ──────────────────────────────────────────────────────────────────
def make_token(sub, role, name="", event_id=""):
    exp = datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS)
    return jwt.encode({"sub":sub,"role":role,"name":name,"event_id":event_id,"exp":exp}, SECRET_KEY, ALGORITHM)

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
    def __init__(self): self.conns: dict[str,list] = {}
    async def connect(self, ws, room):
        await ws.accept(); self.conns.setdefault(room,[]).append(ws)
    def disconnect(self, ws, room):
        if room in self.conns: self.conns[room]=[c for c in self.conns[room] if c!=ws]
    async def broadcast(self, room, data):
        dead=[]
        for ws in self.conns.get(room,[]):
            try: await ws.send_json(data)
            except: dead.append(ws)
        for ws in dead: self.disconnect(ws,room)

mgr = WSManager()
mcq_tasks: dict[str,asyncio.Task] = {}

# ── MCQ TIMER ─────────────────────────────────────────────────────────────
async def mcq_countdown(eid, round_id, group_id):
    await asyncio.sleep(MCQ_TIMER_SECONDS)
    conn = get_db()
    try:
        if gs_get(conn,eid,"current_round_id") != round_id: return
        if gs_get(conn,eid,"phase") != "mcq": return
        answers = conn.execute("SELECT * FROM mcq_answers WHERE round_id=?", (round_id,)).fetchall()
        correct = sorted([a for a in answers if a["is_correct"]], key=lambda a: a["answered_at_ms"])
        wrong   = sorted([a for a in answers if not a["is_correct"]], key=lambda a: a["answered_at_ms"])
        all_caps = [r["id"] for r in conn.execute("SELECT id FROM captains WHERE event_id=?", (eid,)).fetchall()]
        answered = {a["captain_id"] for a in answers}
        no_ans   = [c for c in all_caps if c not in answered]
        order    = [a["captain_id"] for a in correct] + [a["captain_id"] for a in wrong] + no_ans
        gs_set(conn,eid,"draft_order",order)
        gs_set(conn,eid,"current_picker_index",0)
        gs_set(conn,eid,"phase","draft")
        await mgr.broadcast(f"event:{eid}", {"type":"phase_change","phase":"draft","draft_order":order,"group_id":group_id})
    finally: conn.close()

# ── APP ───────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app):
    init_db(); yield

app = FastAPI(lifespan=lifespan, title="Code Eleven Draft")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="static"), name="static")

async def _html(p):
    async with aiofiles.open(p,"r") as f: return await f.read()

@app.get("/", response_class=HTMLResponse)
async def pg_index(): return await _html("templates/index.html")

@app.get("/admin", response_class=HTMLResponse)
async def pg_admin(): return await _html("templates/admin.html")

@app.get("/event/{eid}", response_class=HTMLResponse)
async def pg_event(eid: str): return await _html("templates/event.html")

@app.get("/captain", response_class=HTMLResponse)
async def pg_captain(): return await _html("templates/captain.html")

@app.get("/results/{eid}", response_class=HTMLResponse)
async def pg_results(eid: str): return await _html("templates/results.html")

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
async def auth_captain(event_code: str = Form(...), captain_id: str = Form(...), password: str = Form(...)):
    conn = get_db()
    ev = conn.execute("SELECT * FROM events WHERE access_code=?", (event_code.upper(),)).fetchone()
    if not ev: conn.close(); raise HTTPException(401, "Invalid event code")
    if password != ev["captain_password"]: conn.close(); raise HTTPException(401, "Wrong password")
    cap = conn.execute("SELECT * FROM captains WHERE id=? AND event_id=?", (captain_id, ev["id"])).fetchone()
    if not cap: conn.close(); raise HTTPException(401, "Captain not found")
    conn.close()
    resp = JSONResponse({"ok":True,"name":cap["name"],"captain_id":cap["id"],"event_id":ev["id"]})
    resp.set_cookie("token", make_token(cap["id"],"captain",cap["name"],ev["id"]),
                    httponly=True, samesite="lax", max_age=TOKEN_EXPIRE_HOURS*3600)
    return resp

@app.post("/api/auth/viewer-login")
async def auth_viewer(event_code: str = Form(...)):
    conn = get_db()
    ev = conn.execute("SELECT * FROM events WHERE access_code=?", (event_code.upper(),)).fetchone()
    conn.close()
    if not ev: raise HTTPException(401, "Invalid event code")
    resp = JSONResponse({"ok":True,"event_id":ev["id"],"event_name":ev["name"]})
    resp.set_cookie("token", make_token(ev["id"],"viewer","Viewer",ev["id"]),
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
    return JSONResponse({"role":d.get("role"),"name":d.get("name"),"sub":d.get("sub"),"event_id":d.get("event_id"),"ok":True})

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
    except sqlite3.IntegrityError: raise HTTPException(400,"Username taken")
    finally: conn.close()
    return {"ok":True}

@app.delete("/api/admins/{aid}")
async def delete_admin(aid: str, auth=Depends(require_admin)):
    conn = get_db(); conn.execute("DELETE FROM admins WHERE id=?", (aid,)); conn.commit(); conn.close()
    return {"ok":True}

# ── EVENTS ────────────────────────────────────────────────────────────────
@app.get("/api/events")
async def list_events(token: str = Cookie(default=None)):
    conn = get_db()
    d = decode_token(token) if token else None
    if d and d.get("role") == "admin":
        rows = conn.execute("SELECT * FROM events ORDER BY created_at DESC").fetchall()
    else:
        rows = conn.execute("SELECT id,name,status,access_code FROM events ORDER BY created_at DESC").fetchall()
    conn.close(); return [dict(r) for r in rows]

@app.get("/api/events/{eid}")
async def get_event(eid: str, token: str = Cookie(default=None)):
    conn = get_db()
    ev = conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
    if not ev: raise HTTPException(404,"Not found")
    caps = conn.execute("SELECT * FROM captains WHERE event_id=? ORDER BY team_number", (eid,)).fetchall()
    conn.close()
    return {**dict(ev), "captains": [dict(c) for c in caps]}

@app.post("/api/events")
async def create_event(
    name: str = Form(...), description: str = Form(default=""),
    num_teams: int = Form(...), captain_password: str = Form(...),
    captain_names: str = Form(...),  # JSON array string
    auth=Depends(require_admin)
):
    conn = get_db()
    eid = str(uuid.uuid4())
    code = gen_code()
    # ensure unique code
    while conn.execute("SELECT id FROM events WHERE access_code=?", (code,)).fetchone():
        code = gen_code()
    try:
        names = json.loads(captain_names)
    except: names = [n.strip() for n in captain_names.split(",") if n.strip()]

    conn.execute("INSERT INTO events(id,name,description,access_code,captain_password,num_teams,status) VALUES(?,?,?,?,?,?,?)",
                 (eid, name.strip(), description.strip(), code, captain_password, num_teams, "setup"))
    for i, cname in enumerate(names, 1):
        if not cname.strip(): continue
        cid = str(uuid.uuid4())
        conn.execute("INSERT INTO captains(id,event_id,name,team_number) VALUES(?,?,?,?)",
                     (cid, eid, cname.strip(), i))
        # auto-add captain as a player assigned to themselves
        db_r = conn.execute("SELECT id,position,batch_year FROM players_db WHERE name=?", (cname.strip(),)).fetchone()
        conn.execute("INSERT INTO event_players(id,event_id,player_db_id,name,position,batch_year,taken_by) VALUES(?,?,?,?,?,?,?)",
                     (str(uuid.uuid4()), eid, db_r["id"] if db_r else None, cname.strip(),
                      db_r["position"] if db_r else "MID", db_r["batch_year"] if db_r else 0, cid))
    conn.commit(); conn.close()
    await mgr.broadcast("global", {"type":"events_updated"})
    return {"ok":True,"id":eid,"access_code":code}

@app.put("/api/events/{eid}")
async def update_event(eid: str, name: str = Form(...), description: str = Form(default=""),
                       captain_password: str = Form(default=""), auth=Depends(require_admin)):
    conn = get_db()
    if captain_password:
        conn.execute("UPDATE events SET name=?,description=?,captain_password=? WHERE id=?",
                     (name.strip(), description.strip(), captain_password, eid))
    else:
        conn.execute("UPDATE events SET name=?,description=? WHERE id=?",
                     (name.strip(), description.strip(), eid))
    conn.commit(); conn.close()
    await mgr.broadcast("global", {"type":"events_updated"})
    return {"ok":True}

@app.post("/api/events/{eid}/regen-code")
async def regen_code(eid: str, auth=Depends(require_admin)):
    conn = get_db()
    code = gen_code()
    while conn.execute("SELECT id FROM events WHERE access_code=? AND id!=?", (code,eid)).fetchone():
        code = gen_code()
    conn.execute("UPDATE events SET access_code=? WHERE id=?", (code, eid))
    conn.commit(); conn.close()
    return {"ok":True,"access_code":code}

@app.delete("/api/events/{eid}")
async def delete_event(eid: str, auth=Depends(require_admin)):
    conn = get_db(); conn.execute("DELETE FROM events WHERE id=?", (eid,)); conn.commit(); conn.close()
    await mgr.broadcast("global", {"type":"events_updated"})
    return {"ok":True}

# ── CAPTAINS (event-scoped) ───────────────────────────────────────────────
@app.get("/api/events/{eid}/captains")
async def list_captains(eid: str, token: str = Cookie(default=None)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM captains WHERE event_id=? ORDER BY team_number", (eid,)).fetchall()
    conn.close(); return [dict(r) for r in rows]

@app.post("/api/events/{eid}/captains")
async def add_captain(eid: str, name: str = Form(...), auth=Depends(require_admin)):
    conn = get_db()
    max_n = conn.execute("SELECT MAX(team_number) as m FROM captains WHERE event_id=?", (eid,)).fetchone()["m"] or 0
    conn.execute("INSERT INTO captains(id,event_id,name,team_number) VALUES(?,?,?,?)",
                 (str(uuid.uuid4()), eid, name.strip(), max_n+1))
    conn.execute("UPDATE events SET num_teams=num_teams+1 WHERE id=?", (eid,))
    conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}", {"type":"captains_updated"})
    return {"ok":True}

@app.put("/api/events/{eid}/captains/{cid}")
async def update_captain(eid: str, cid: str, name: str = Form(...), auth=Depends(require_admin)):
    conn = get_db()
    conn.execute("UPDATE captains SET name=? WHERE id=? AND event_id=?", (name.strip(), cid, eid))
    conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}", {"type":"captains_updated"})
    return {"ok":True}

@app.delete("/api/events/{eid}/captains/{cid}")
async def delete_captain(eid: str, cid: str, auth=Depends(require_admin)):
    conn = get_db()
    conn.execute("DELETE FROM captains WHERE id=? AND event_id=?", (cid, eid))
    conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}", {"type":"captains_updated"})
    return {"ok":True}

@app.put("/api/events/{eid}/captains/{cid}/team-name")
async def rename_team(eid: str, cid: str, body: dict, auth=Depends(require_admin)):
    team_name = body.get("team_name", "").strip() or None
    conn = get_db()
    ev = conn.execute("SELECT status FROM events WHERE id=?", (eid,)).fetchone()
    if not ev: conn.close(); raise HTTPException(404, "Event not found")
    if ev["status"] != "done":
        conn.close(); raise HTTPException(400, "Team renaming only allowed after draft is complete")
    conn.execute("UPDATE captains SET team_name=? WHERE id=? AND event_id=?", (team_name, cid, eid))
    conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}", {"type":"captains_updated"})
    return {"ok": True}

# ── PLAYERS DB ────────────────────────────────────────────────────────────
@app.get("/api/players-db")
async def list_players_db(q: str = "", auth=Depends(require_admin)):
    conn = get_db()
    if q:
        rows = conn.execute("SELECT * FROM players_db WHERE name LIKE ? OR city LIKE ? ORDER BY name",
                            (f"%{q}%",f"%{q}%")).fetchall()
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
    except sqlite3.IntegrityError: raise HTTPException(400,"Player already exists")
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
        text = content.decode("utf-8-sig"); reader = csv.DictReader(io.StringIO(text)); rows = list(reader)
        if not rows: raise HTTPException(400,"Empty CSV")
        def fc(row,*keys):
            for k in row:
                if k.strip().lower().replace(" ","_") in keys: return k
            return None
        s=rows[0]; nc=fc(s,"name","player_name"); yc=fc(s,"batch_year","year","batch","angkatan")
        pc=fc(s,"position","pos","posisi"); cc=fc(s,"city","kota","domisili")
        if not all([nc,yc,pc]): raise HTTPException(400,f"Need: name, batch_year, position. Got: {list(s.keys())}")
        conn=get_db(); added=updated=errors=0
        for i,row in enumerate(rows,1):
            try:
                n=row[nc].strip(); y=int(row[yc].strip()); p=row[pc].strip().upper()
                city=row[cc].strip() if cc and cc in row else ""
                if not n: continue
                ex=conn.execute("SELECT id,city FROM players_db WHERE name=? AND batch_year=?",(n,y)).fetchone()
                if ex:
                    conn.execute("UPDATE players_db SET position=?,city=? WHERE id=?",(p, city if city else ex["city"], ex["id"]))
                    updated+=1
                else:
                    conn.execute("INSERT INTO players_db(id,name,position,batch_year,city) VALUES(?,?,?,?,?)",
                                 (str(uuid.uuid4()),n,p,y,city))
                    added+=1
            except: errors+=1
        conn.commit(); conn.close()
        return {"ok":True,"added":added,"updated":updated,"errors":errors}
    except HTTPException: raise
    except Exception as e: raise HTTPException(400,f"Parse error: {e}")

# ── EVENT PLAYERS ─────────────────────────────────────────────────────────
@app.get("/api/events/{eid}/players")
async def list_event_players(eid: str, token: str = Cookie(default=None)):
    conn = get_db()
    rows = conn.execute("""SELECT ep.*, c.name as captain_name
        FROM event_players ep LEFT JOIN captains c ON ep.taken_by=c.id
        WHERE ep.event_id=? ORDER BY ep.batch_year""", (eid,)).fetchall()
    conn.close(); return [dict(r) for r in rows]

@app.post("/api/events/{eid}/players")
async def add_event_player(eid: str, name: str = Form(...), position: str = Form(...),
                            batch_year: int = Form(...), auth=Depends(require_admin)):
    conn = get_db()
    db_r = conn.execute("SELECT id FROM players_db WHERE name=? AND batch_year=?",
                        (name.strip(),batch_year)).fetchone()
    conn.execute("INSERT INTO event_players(id,event_id,player_db_id,name,position,batch_year) VALUES(?,?,?,?,?,?)",
                 (str(uuid.uuid4()),eid, db_r["id"] if db_r else None, name.strip(), position.upper(), batch_year))
    conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}", {"type":"players_updated"})
    return {"ok":True}

@app.post("/api/events/{eid}/players/from-db")
async def add_from_db(eid: str, body: dict, auth=Depends(require_admin)):
    ids = body.get("player_db_ids",[])
    conn = get_db()
    # get captain names to exclude
    cap_names = {c["name"].lower() for c in conn.execute("SELECT name FROM captains WHERE event_id=?",(eid,)).fetchall()}
    added=skipped=0
    for dbid in ids:
        p = conn.execute("SELECT * FROM players_db WHERE id=?", (dbid,)).fetchone()
        if not p: continue
        if p["name"].lower() in cap_names: skipped+=1; continue
        ex = conn.execute("SELECT id FROM event_players WHERE event_id=? AND player_db_id=?",(eid,dbid)).fetchone()
        if ex: continue
        conn.execute("INSERT INTO event_players(id,event_id,player_db_id,name,position,batch_year) VALUES(?,?,?,?,?,?)",
                     (str(uuid.uuid4()),eid,dbid,p["name"],p["position"],p["batch_year"]))
        added+=1
    conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}", {"type":"players_updated"})
    return {"ok":True,"added":added,"skipped_captains":skipped}

@app.post("/api/events/{eid}/players/csv")
async def upload_event_csv(eid: str, file: UploadFile = File(...), auth=Depends(require_admin)):
    content = await file.read()
    try:
        text=content.decode("utf-8-sig"); reader=csv.DictReader(io.StringIO(text)); rows=list(reader)
        if not rows: raise HTTPException(400,"Empty CSV")
        def fc(row,*keys):
            for k in row:
                if k.strip().lower().replace(" ","_") in keys: return k
            return None
        s=rows[0]; nc=fc(s,"name","player_name"); yc=fc(s,"batch_year","year","batch","angkatan")
        pc=fc(s,"position","pos","posisi"); cc=fc(s,"city","kota","domisili")
        if not all([nc,yc,pc]): raise HTTPException(400,f"Need: name, batch_year, position. Got: {list(s.keys())}")
        conn=get_db()
        cap_names={c["name"].lower() for c in conn.execute("SELECT name FROM captains WHERE event_id=?",(eid,)).fetchall()}
        added_ev=added_db=skipped_cap=errors=0; errs=[]
        for i,row in enumerate(rows,1):
            try:
                n=row[nc].strip(); y=int(row[yc].strip()); p=row[pc].strip().upper()
                city=row[cc].strip() if cc and cc in row else ""
                if not n: continue
                if n.lower() in cap_names: skipped_cap+=1; continue
                ex_db=conn.execute("SELECT id,city FROM players_db WHERE name=? AND batch_year=?",(n,y)).fetchone()
                if ex_db:
                    conn.execute("UPDATE players_db SET position=?,city=? WHERE id=?",(p, city if city else ex_db["city"], ex_db["id"]))
                    dbid=ex_db["id"]
                else:
                    dbid=str(uuid.uuid4())
                    conn.execute("INSERT INTO players_db(id,name,position,batch_year,city) VALUES(?,?,?,?,?)",(dbid,n,p,y,city))
                    added_db+=1
                ex_ev=conn.execute("SELECT id FROM event_players WHERE event_id=? AND player_db_id=?",(eid,dbid)).fetchone()
                if not ex_ev:
                    conn.execute("INSERT INTO event_players(id,event_id,player_db_id,name,position,batch_year) VALUES(?,?,?,?,?,?)",
                                 (str(uuid.uuid4()),eid,dbid,n,p,y))
                    added_ev+=1
            except Exception as e: errs.append(f"Row {i}: {e}"); errors+=1
        conn.commit(); conn.close()
        await mgr.broadcast(f"event:{eid}", {"type":"players_updated"})
        return {"ok":True,"added_to_event":added_ev,"added_to_db":added_db,"skipped_captains":skipped_cap,"errors":errors}
    except HTTPException: raise
    except Exception as e: raise HTTPException(400,f"Parse error: {e}")

@app.put("/api/events/{eid}/players/{pid}/assign")
async def assign_event_player(eid: str, pid: str, body: dict, auth=Depends(require_admin)):
    conn=get_db()
    conn.execute("UPDATE event_players SET taken_by=? WHERE id=? AND event_id=?",
                 (body.get("captain_id"), pid, eid))
    conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}", {"type":"players_updated"})
    return {"ok":True}

@app.delete("/api/events/{eid}/players/{pid}")
async def delete_event_player(eid: str, pid: str, auth=Depends(require_admin)):
    conn=get_db(); conn.execute("DELETE FROM event_players WHERE id=? AND event_id=?",(pid,eid)); conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}", {"type":"players_updated"})
    return {"ok":True}

@app.delete("/api/events/{eid}/players")
async def clear_event_players(eid: str, auth=Depends(require_admin)):
    conn=get_db(); conn.execute("DELETE FROM event_players WHERE event_id=?",(eid,)); conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}", {"type":"players_updated"})
    return {"ok":True}

# ── QUESTIONS ─────────────────────────────────────────────────────────────
@app.get("/api/questions")
async def list_questions(auth=Depends(require_admin)):
    conn=get_db(); rows=conn.execute("SELECT * FROM questions ORDER BY created_at").fetchall(); conn.close()
    return [dict(r) for r in rows]

@app.post("/api/questions")
async def create_question(text: str=Form(...), option_a: str=Form(...), option_b: str=Form(...),
    option_c: str=Form(...), option_d: str=Form(...), correct_index: int=Form(...), auth=Depends(require_admin)):
    conn=get_db()
    conn.execute("INSERT INTO questions(id,text,option_a,option_b,option_c,option_d,correct_index) VALUES(?,?,?,?,?,?,?)",
                 (str(uuid.uuid4()),text,option_a,option_b,option_c,option_d,correct_index))
    conn.commit(); conn.close(); return {"ok":True}

@app.delete("/api/questions/{qid}")
async def delete_question(qid: str, auth=Depends(require_admin)):
    conn=get_db(); conn.execute("DELETE FROM questions WHERE id=?",(qid,)); conn.commit(); conn.close()
    return {"ok":True}

# ── GAME ──────────────────────────────────────────────────────────────────
@app.get("/api/events/{eid}/game")
async def get_game(eid: str, token: str = Cookie(default=None)):
    conn=get_db()
    ev=conn.execute("SELECT * FROM events WHERE id=?",(eid,)).fetchone()
    if not ev: raise HTTPException(404,"Not found")
    d=decode_token(token) if token else None
    phase        = gs_get(conn,eid,"phase","lobby")
    group_index  = gs_get(conn,eid,"current_group_index",-1)
    current_group= gs_get(conn,eid,"current_group_id")
    draft_order  = gs_get(conn,eid,"draft_order",[])
    picker_index = gs_get(conn,eid,"current_picker_index",0)
    round_id     = gs_get(conn,eid,"current_round_id")
    qid          = gs_get(conn,eid,"current_question_id")
    q_data=None
    if qid:
        q=conn.execute("SELECT * FROM questions WHERE id=?",(qid,)).fetchone()
        if q:
            q_data={"id":q["id"],"text":q["text"],"options":[q["option_a"],q["option_b"],q["option_c"],q["option_d"]]}
            if d and d.get("role")=="admin": q_data["correct_index"]=q["correct_index"]
    answers=[]
    if round_id:
        answers=[dict(a) for a in conn.execute("SELECT * FROM mcq_answers WHERE round_id=?",(round_id,)).fetchall()]
    players=[dict(p) for p in conn.execute("""SELECT ep.*,c.name as captain_name
        FROM event_players ep LEFT JOIN captains c ON ep.taken_by=c.id
        WHERE ep.event_id=? ORDER BY ep.batch_year""",(eid,)).fetchall()]
    captains=[dict(c) for c in conn.execute("SELECT * FROM captains WHERE event_id=? ORDER BY team_number",(eid,)).fetchall()]
    history=[dict(h) for h in conn.execute("SELECT * FROM draft_history WHERE event_id=? ORDER BY pick_number",(eid,)).fetchall()]
    conn.close()
    return {"phase":phase,"current_group":current_group,"group_index":group_index,
            "draft_order":draft_order,"current_picker_index":picker_index,
            "question":q_data,"answers":answers,"round_id":round_id,
            "players":players,"captains":captains,"history":history,"event":dict(ev)}

@app.post("/api/events/{eid}/game/start-round")
async def start_round(eid: str, auth=Depends(require_admin)):
    conn=get_db()
    qs=conn.execute("SELECT * FROM questions").fetchall()
    if not qs: conn.close(); raise HTTPException(400,"Add questions first")
    caps=conn.execute("SELECT id FROM captains WHERE event_id=?",(eid,)).fetchall()
    if not caps: conn.close(); raise HTTPException(400,"Add captains first")
    cur=gs_get(conn,eid,"current_group_index",-1); nxt=cur+1
    if nxt>=3: conn.close(); raise HTTPException(400,"All 3 rounds complete")
    gid=GROUP_ORDER[nxt]; q=random.choice(qs); rid=str(uuid.uuid4())
    gs_set(conn,eid,"phase","mcq"); gs_set(conn,eid,"current_group_index",nxt)
    gs_set(conn,eid,"current_group_id",gid); gs_set(conn,eid,"current_question_id",q["id"])
    gs_set(conn,eid,"current_round_id",rid); gs_set(conn,eid,"draft_order",[])
    gs_set(conn,eid,"current_picker_index",0); conn.close()
    if eid in mcq_tasks and not mcq_tasks[eid].done(): mcq_tasks[eid].cancel()
    mcq_tasks[eid]=asyncio.create_task(mcq_countdown(eid,rid,gid))
    await mgr.broadcast(f"event:{eid}",{"type":"round_started","group_id":gid,"group_index":nxt,
        "round_id":rid,"question":{"id":q["id"],"text":q["text"],"options":[q["option_a"],q["option_b"],q["option_c"],q["option_d"]]},"timer_seconds":MCQ_TIMER_SECONDS})
    return {"ok":True,"group_id":gid}

@app.post("/api/events/{eid}/game/set-draft-order")
async def set_draft_order(eid: str, body: dict, auth=Depends(require_admin)):
    conn=get_db(); gs_set(conn,eid,"draft_order",body.get("order",[])); gs_set(conn,eid,"phase","draft"); conn.close()
    await mgr.broadcast(f"event:{eid}",{"type":"phase_change","phase":"draft","draft_order":body.get("order",[])})
    return {"ok":True}

@app.post("/api/events/{eid}/game/end-draft")
async def end_draft(eid: str, auth=Depends(require_admin)):
    conn=get_db(); gs_set(conn,eid,"phase","done")
    conn.execute("UPDATE events SET status='done' WHERE id=?",(eid,)); conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}",{"type":"phase_change","phase":"done"})
    return {"ok":True}

@app.post("/api/events/{eid}/game/reset")
async def reset_game(eid: str, auth=Depends(require_admin)):
    if eid in mcq_tasks and not mcq_tasks[eid].done(): mcq_tasks[eid].cancel()
    conn=get_db()
    conn.execute("DELETE FROM game_state WHERE event_id=?",(eid,))
    conn.execute("UPDATE event_players SET taken_by=NULL,pick_order=NULL WHERE event_id=?",(eid,))
    conn.execute("DELETE FROM draft_history WHERE event_id=?",(eid,))
    conn.execute("UPDATE events SET status='setup' WHERE id=?",(eid,))
    conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}",{"type":"reset"})
    return {"ok":True}

@app.post("/api/events/{eid}/game/answer")
async def submit_answer(eid: str, body: dict, auth=Depends(require_captain)):
    captain_id=auth["sub"]; chosen=body.get("chosen_index")
    conn=get_db()
    if gs_get(conn,eid,"phase")!="mcq": conn.close(); raise HTTPException(400,"Not MCQ phase")
    rid=gs_get(conn,eid,"current_round_id")
    if conn.execute("SELECT id FROM mcq_answers WHERE round_id=? AND captain_id=?",(rid,captain_id)).fetchone():
        conn.close(); raise HTTPException(400,"Already answered")
    qid=gs_get(conn,eid,"current_question_id")
    q=conn.execute("SELECT correct_index FROM questions WHERE id=?",(qid,)).fetchone()
    is_correct=(chosen==q["correct_index"]) if q and chosen is not None else False
    conn.execute("INSERT INTO mcq_answers(id,round_id,captain_id,chosen_index,is_correct,answered_at_ms) VALUES(?,?,?,?,?,?)",
                 (str(uuid.uuid4()),rid,captain_id,chosen,int(is_correct),int(time.time()*1000)))
    conn.commit()
    total=conn.execute("SELECT COUNT(*) as n FROM captains WHERE event_id=?",(eid,)).fetchone()["n"]
    answered=conn.execute("SELECT COUNT(*) as n FROM mcq_answers WHERE round_id=?",(rid,)).fetchone()["n"]
    gid=gs_get(conn,eid,"current_group_id"); conn.close()
    await mgr.broadcast(f"event:{eid}",{"type":"captain_answered","captain_id":captain_id,"is_correct":is_correct,"chosen_index":chosen})
    if answered>=total:
        if eid in mcq_tasks and not mcq_tasks[eid].done(): mcq_tasks[eid].cancel()
        await mcq_countdown(eid,rid,gid)
    return {"ok":True,"is_correct":is_correct}

@app.post("/api/events/{eid}/game/pick")
async def pick_player(eid: str, body: dict, auth=Depends(require_captain)):
    captain_id=auth["sub"]; player_id=body.get("player_id")
    conn=get_db()
    if gs_get(conn,eid,"phase")!="draft": conn.close(); raise HTTPException(400,"Not draft phase")
    order=gs_get(conn,eid,"draft_order",[]); pidx=gs_get(conn,eid,"current_picker_index",0)
    if not order: conn.close(); raise HTTPException(400,"No draft order")
    if order[pidx]!=captain_id and auth.get("role")!="admin": conn.close(); raise HTTPException(403,"Not your turn")
    p=conn.execute("SELECT * FROM event_players WHERE id=? AND event_id=?",(player_id,eid)).fetchone()
    if not p: conn.close(); raise HTTPException(404,"Not found")
    if p["taken_by"]: conn.close(); raise HTTPException(400,"Already taken")
    pick_num=conn.execute("SELECT COUNT(*) as n FROM draft_history WHERE event_id=?",(eid,)).fetchone()["n"]+1
    cap=conn.execute("SELECT name FROM captains WHERE id=?",(captain_id,)).fetchone()
    conn.execute("UPDATE event_players SET taken_by=?,pick_order=? WHERE id=?",(captain_id,pick_num,player_id))
    conn.execute("""INSERT INTO draft_history(id,event_id,captain_id,captain_name,player_id,player_name,
        player_position,player_year,group_id,pick_number) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()),eid,captain_id,cap["name"] if cap else captain_id,
         player_id,p["name"],p["position"],p["batch_year"],gs_get(conn,eid,"current_group_id","g1"),pick_num))
    gs_set(conn,eid,"current_picker_index",(pidx+1)%len(order))
    conn.close()
    await mgr.broadcast(f"event:{eid}",{"type":"player_picked","player_id":player_id,"player_name":p["name"],
        "captain_id":captain_id,"pick_number":pick_num,"next_picker_index":(pidx+1)%len(order)})
    return {"ok":True}

# ── FIXTURES ──────────────────────────────────────────────────────────────
@app.get("/api/events/{eid}/fixtures")
async def list_fixtures(eid: str, token: str = Cookie(default=None)):
    conn=get_db()
    rows=conn.execute("""SELECT f.*,
        COALESCE(ch.team_name, ch.name) as home_name,
        COALESCE(ca.team_name, ca.name) as away_name
        FROM fixtures f JOIN captains ch ON f.home_captain_id=ch.id JOIN captains ca ON f.away_captain_id=ca.id
        WHERE f.event_id=? ORDER BY f.match_date,f.created_at""",(eid,)).fetchall()
    result=[]
    for r in rows:
        fix=dict(r)
        fix["events"]=[dict(e) for e in conn.execute("SELECT * FROM fixture_events WHERE fixture_id=? ORDER BY minute",(r["id"],)).fetchall()]
        result.append(fix)
    conn.close(); return result

@app.post("/api/events/{eid}/fixtures")
async def create_fixture(eid: str, home_captain_id: str=Form(...), away_captain_id: str=Form(...),
                          match_date: str=Form(default=""), pitch_name: str=Form(default=""), pitch_url: str=Form(default=""), auth=Depends(require_admin)):
    conn=get_db(); fid=str(uuid.uuid4())
    conn.execute("INSERT INTO fixtures(id,event_id,home_captain_id,away_captain_id,match_date,pitch_name,pitch_url) VALUES(?,?,?,?,?,?,?)",
                 (fid,eid,home_captain_id,away_captain_id,match_date or None,pitch_name.strip() or None,pitch_url.strip() or None))
    conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}",{"type":"fixtures_updated"})
    return {"ok":True,"id":fid}

@app.put("/api/events/{eid}/fixtures/{fid}/result")
async def update_fixture_result(eid: str, fid: str, body: dict, auth=Depends(require_admin)):
    conn=get_db()
    conn.execute("UPDATE fixtures SET home_score=?,away_score=?,status='played' WHERE id=? AND event_id=?",
                 (body.get("home_score"),body.get("away_score"),fid,eid))
    conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}",{"type":"fixtures_updated"})
    return {"ok":True}

@app.delete("/api/events/{eid}/fixtures/{fid}")
async def delete_fixture(eid: str, fid: str, auth=Depends(require_admin)):
    conn=get_db(); conn.execute("DELETE FROM fixtures WHERE id=? AND event_id=?",(fid,eid)); conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}",{"type":"fixtures_updated"})
    return {"ok":True}

@app.post("/api/events/{eid}/fixtures/{fid}/events")
async def add_fixture_event(eid: str, fid: str, body: dict, auth=Depends(require_admin)):
    conn=get_db()
    conn.execute("INSERT INTO fixture_events(id,fixture_id,event_id,player_id,player_name,captain_id,event_type,minute) VALUES(?,?,?,?,?,?,?,?)",
                 (str(uuid.uuid4()),fid,eid,body.get("player_id",""),body.get("player_name",""),
                  body.get("captain_id",""),body.get("event_type","goal"),body.get("minute")))
    conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}",{"type":"fixtures_updated"})
    return {"ok":True}

@app.delete("/api/events/{eid}/fixtures/{fid}/events/{evid}")
async def delete_fixture_event(eid: str, fid: str, evid: str, auth=Depends(require_admin)):
    conn=get_db(); conn.execute("DELETE FROM fixture_events WHERE id=?",(evid,)); conn.commit(); conn.close()
    await mgr.broadcast(f"event:{eid}",{"type":"fixtures_updated"})
    return {"ok":True}

@app.get("/api/events/{eid}/standings")
async def get_standings(eid: str, token: str = Cookie(default=None)):
    conn=get_db()
    caps={c["id"]:(c["team_name"] or c["name"]) for c in conn.execute("SELECT id,name,team_name FROM captains WHERE event_id=?",(eid,)).fetchall()}
    fixtures=conn.execute("SELECT * FROM fixtures WHERE event_id=? AND status='played'",(eid,)).fetchall()
    stats={cid:{"name":name,"p":0,"w":0,"d":0,"l":0,"gf":0,"ga":0,"pts":0} for cid,name in caps.items()}
    for f in fixtures:
        h,a=f["home_captain_id"],f["away_captain_id"]; hs,as_=f["home_score"] or 0,f["away_score"] or 0
        for x in [h,a]:
            if x not in stats: stats[x]={"name":caps.get(x,x),"p":0,"w":0,"d":0,"l":0,"gf":0,"ga":0,"pts":0}
        stats[h]["p"]+=1; stats[h]["gf"]+=hs; stats[h]["ga"]+=as_
        stats[a]["p"]+=1; stats[a]["gf"]+=as_; stats[a]["ga"]+=hs
        if hs>as_: stats[h]["w"]+=1; stats[h]["pts"]+=3; stats[a]["l"]+=1
        elif hs<as_: stats[a]["w"]+=1; stats[a]["pts"]+=3; stats[h]["l"]+=1
        else: stats[h]["d"]+=1; stats[h]["pts"]+=1; stats[a]["d"]+=1; stats[a]["pts"]+=1
    for s in stats.values(): s["gd"]=s["gf"]-s["ga"]
    table=sorted(stats.values(),key=lambda s:(-s["pts"],-s["gd"],-s["gf"],s["name"]))
    evts=conn.execute("SELECT * FROM fixture_events WHERE event_id=?",(eid,)).fetchall()
    pstats={}
    for e in evts:
        pid=e["player_id"]; pn=e["player_name"]
        if pid not in pstats: pstats[pid]={"name":pn,"goals":0,"assists":0,"cleansheets":0}
        if e["event_type"]=="goal": pstats[pid]["goals"]+=1
        elif e["event_type"]=="assist": pstats[pid]["assists"]+=1
        elif e["event_type"]=="cleansheet": pstats[pid]["cleansheets"]+=1
    conn.close()
    return {"table":table,"player_stats":sorted(pstats.values(),key=lambda p:(-p["goals"],-p["assists"],-p["cleansheets"],p["name"]))}

# ── RESULTS (viewer + captain) ────────────────────────────────────────────
@app.get("/api/events/{eid}/results")
async def get_results(eid: str, token: str = Cookie(default=None)):
    if not token: raise HTTPException(401,"Not authenticated")
    d=decode_token(token)
    if not d or d.get("role") not in ("viewer","captain","admin"): raise HTTPException(403,"Login required")
    conn=get_db()
    ev=conn.execute("SELECT id,name,status,description FROM events WHERE id=?",(eid,)).fetchone()
    if not ev: raise HTTPException(404,"Not found")
    caps=conn.execute("SELECT * FROM captains WHERE event_id=? ORDER BY team_number",(eid,)).fetchall()
    players=conn.execute("""SELECT ep.*,c.name as captain_name FROM event_players ep
        LEFT JOIN captains c ON ep.taken_by=c.id WHERE ep.event_id=? ORDER BY ep.batch_year""",(eid,)).fetchall()
    phase=gs_get(conn,eid,"phase","lobby")
    conn.close()
    return {"phase":phase,"event":dict(ev),"captains":[dict(c) for c in caps],"players":[dict(p) for p in players]}


@app.get("/api/events/{eid}/standings-full")
async def get_standings_full(eid: str, token: str = Cookie(default=None)):
    """Standings + fixtures for the results page (viewer-accessible)."""
    if not token: raise HTTPException(401,"Not authenticated")
    d=decode_token(token)
    if not d or d.get("role") not in ("viewer","captain","admin"): raise HTTPException(403,"Login required")
    conn=get_db()
    caps={c["id"]:(c["team_name"] or c["name"]) for c in conn.execute("SELECT id,name,team_name FROM captains WHERE event_id=?",(eid,)).fetchall()}
    fixtures_raw=conn.execute("""SELECT f.*,ch.name as home_name,ca.name as away_name
        FROM fixtures f JOIN captains ch ON f.home_captain_id=ch.id JOIN captains ca ON f.away_captain_id=ca.id
        WHERE f.event_id=? ORDER BY f.match_date,f.created_at""",(eid,)).fetchall()
    fixtures=[dict(r) for r in fixtures_raw]
    played=conn.execute("SELECT * FROM fixtures WHERE event_id=? AND status='played'",(eid,)).fetchall()
    stats={cid:{"name":name,"p":0,"w":0,"d":0,"l":0,"gf":0,"ga":0,"pts":0} for cid,name in caps.items()}
    for f in played:
        h,a=f["home_captain_id"],f["away_captain_id"]; hs,as_=f["home_score"] or 0,f["away_score"] or 0
        for x in [h,a]:
            if x not in stats: stats[x]={"name":caps.get(x,x),"p":0,"w":0,"d":0,"l":0,"gf":0,"ga":0,"pts":0}
        stats[h]["p"]+=1;stats[h]["gf"]+=hs;stats[h]["ga"]+=as_
        stats[a]["p"]+=1;stats[a]["gf"]+=as_;stats[a]["ga"]+=hs
        if hs>as_: stats[h]["w"]+=1;stats[h]["pts"]+=3;stats[a]["l"]+=1
        elif hs<as_: stats[a]["w"]+=1;stats[a]["pts"]+=3;stats[h]["l"]+=1
        else: stats[h]["d"]+=1;stats[h]["pts"]+=1;stats[a]["d"]+=1;stats[a]["pts"]+=1
    for s in stats.values(): s["gd"]=s["gf"]-s["ga"]
    table=sorted(stats.values(),key=lambda s:(-s["pts"],-s["gd"],-s["gf"],s["name"]))
    evts=conn.execute("SELECT * FROM fixture_events WHERE event_id=?",(eid,)).fetchall()
    pstats={}
    for e in evts:
        pid=e["player_id"];pn=e["player_name"]
        if pid not in pstats: pstats[pid]={"name":pn,"goals":0,"assists":0,"cleansheets":0}
        if e["event_type"]=="goal": pstats[pid]["goals"]+=1
        elif e["event_type"]=="assist": pstats[pid]["assists"]+=1
        elif e["event_type"]=="cleansheet": pstats[pid]["cleansheets"]+=1
    conn.close()
    return {"table":table,"fixtures":fixtures,
            "player_stats":sorted(pstats.values(),key=lambda p:(-p["goals"],-p["assists"],-p["cleansheets"],p["name"]))}

# ── EXPORT / RESTORE ──────────────────────────────────────────────────────
@app.get("/api/events/{eid}/export/history")
async def export_history(eid: str, auth=Depends(require_admin)):
    conn=get_db()
    rows=conn.execute("SELECT * FROM draft_history WHERE event_id=? ORDER BY pick_number",(eid,)).fetchall()
    conn.close()
    out=io.StringIO(); w=csv.writer(out)
    w.writerow(["Pick #","Captain","Player","Position","Batch Year","Group","Picked At"])
    gl={"g1":"≤2004","g2":"2005-2018","g3":">2018"}
    for r in rows: w.writerow([r["pick_number"],r["captain_name"],r["player_name"],r["player_position"],r["player_year"],gl.get(r["group_id"],r["group_id"]),r["picked_at"]])
    out.seek(0)
    return StreamingResponse(iter([out.getvalue()]),media_type="text/csv",
                             headers={"Content-Disposition":"attachment; filename=draft-history.csv"})

@app.get("/api/events/{eid}/export/teams")
async def export_teams(eid: str, auth=Depends(require_admin)):
    conn=get_db()
    players=conn.execute("""SELECT ep.*,c.name as captain_name FROM event_players ep
        LEFT JOIN captains c ON ep.taken_by=c.id WHERE ep.event_id=? ORDER BY ep.batch_year""",(eid,)).fetchall()
    conn.close()
    out=io.StringIO(); w=csv.writer(out)
    w.writerow(["Captain","Player","Position","Batch Year","Group"])
    gl={"g1":"≤2004","g2":"2005-2018","g3":">2018"}
    for p in players:
        g=get_group(p["batch_year"])
        w.writerow([p["captain_name"] or "Undrafted",p["name"],p["position"],p["batch_year"],gl.get(g,"")])
    out.seek(0)
    return StreamingResponse(iter([out.getvalue()]),media_type="text/csv",
                             headers={"Content-Disposition":"attachment; filename=teams.csv"})

@app.post("/api/events/{eid}/restore/teams")
async def restore_teams(eid: str, file: UploadFile = File(...), auth=Depends(require_admin)):
    content=await file.read()
    try:
        text=content.decode("utf-8-sig"); reader=csv.DictReader(io.StringIO(text)); rows=list(reader)
        if not rows: raise HTTPException(400,"Empty CSV")
        conn=get_db()
        caps={c["name"]:c["id"] for c in conn.execute("SELECT id,name FROM captains WHERE event_id=?",(eid,)).fetchall()}
        added=assigned=0; skipped=[]
        for i,row in enumerate(rows,1):
            try:
                cn=(row.get("Captain") or "").strip(); pn=(row.get("Player") or row.get("name") or "").strip()
                pos=(row.get("Position") or "MID").strip().upper(); by=int((row.get("Batch Year") or row.get("batch_year") or "0").strip())
                if not pn or not by: skipped.append(f"Row {i}: missing data"); continue
                db_r=conn.execute("SELECT id FROM players_db WHERE name=? AND batch_year=?",(pn,by)).fetchone()
                if not db_r:
                    dbid=str(uuid.uuid4())
                    conn.execute("INSERT INTO players_db(id,name,position,batch_year) VALUES(?,?,?,?)",(dbid,pn,pos,by))
                else: dbid=db_r["id"]
                ep=conn.execute("SELECT id FROM event_players WHERE event_id=? AND player_db_id=?",(eid,dbid)).fetchone()
                if not ep:
                    epid=str(uuid.uuid4())
                    conn.execute("INSERT INTO event_players(id,event_id,player_db_id,name,position,batch_year) VALUES(?,?,?,?,?,?)",(epid,eid,dbid,pn,pos,by))
                    added+=1; ep_id=epid
                else: ep_id=ep["id"]
                cid=caps.get(cn) if cn and cn.lower()!="undrafted" else None
                if cn and cn.lower()!="undrafted" and not cid: skipped.append(f"Row {i}: captain '{cn}' not found")
                else:
                    conn.execute("UPDATE event_players SET taken_by=? WHERE id=?",(cid,ep_id))
                    if cid: assigned+=1
            except Exception as e: skipped.append(f"Row {i}: {e}")
        conn.commit(); conn.close()
        await mgr.broadcast(f"event:{eid}",{"type":"players_updated"})
        return {"ok":True,"added":added,"assigned":assigned,"skipped":skipped}
    except HTTPException: raise
    except Exception as e: raise HTTPException(400,f"Error: {e}")

# ── WEBSOCKET ─────────────────────────────────────────────────────────────
@app.websocket("/ws/draft")
async def ws_draft(ws: WebSocket, token: str="", room: str="global"):
    await mgr.connect(ws, room)
    try:
        while True: await ws.receive_text()
    except WebSocketDisconnect: mgr.disconnect(ws, room)
