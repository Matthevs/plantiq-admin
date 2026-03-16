"""
PlantIQ Admin — Licence Management Server
Run this separately from PlantIQ. It manages licence keys, usage tracking, and billing.

Evoke Digital Engineering
"""

import os
import sys
import json
import time
import uuid
import sqlite3
import hashlib
import logging
from pathlib import Path
from datetime import datetime, timedelta

# ── Config ──
_script_dir = os.path.dirname(os.path.abspath(__file__))
# Use /data/ on Railway (persistent volume), local folder otherwise
if os.path.exists("/data"):
    DB_PATH = "/data/plantiq_licences.db"
else:
    DB_PATH = os.path.join(_script_dir, "plantiq_licences.db")
ADMIN_HOST = "127.0.0.1"
ADMIN_PORT = 8601
ADMIN_USERNAME = "Admin"
ADMIN_PASSWORD = "Cadline2020!"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [admin] %(levelname)s: %(message)s")
logger = logging.getLogger("admin")


# ═══════════════════════════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS licences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            licence_key TEXT UNIQUE NOT NULL,
            client_name TEXT NOT NULL,
            company TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            daily_limit INTEGER DEFAULT 20,
            is_active INTEGER DEFAULT 1,
            notes TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            licence_key TEXT NOT NULL,
            query_text TEXT DEFAULT '',
            timestamp TEXT NOT NULL,
            tokens_estimated INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_usage_key_ts ON usage_log(licence_key, timestamp)
    """)
    conn.commit()
    conn.close()
    logger.info(f"Database ready: {DB_PATH}")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def generate_key():
    """Generate a licence key like PIQ-XXXX-XXXX-XXXX"""
    raw = uuid.uuid4().hex[:12].upper()
    return f"PIQ-{raw[:4]}-{raw[4:8]}-{raw[8:12]}"


# Duration presets in days
DURATIONS = {
    "1_week": 7,
    "2_weeks": 14,
    "3_weeks": 21,
    "1_month": 30,
    "2_months": 60,
    "3_months": 90,
    "6_months": 180,
    "1_year": 365,
    "2_years": 730,
    "3_years": 1095,
    "5_years": 1825,
    "lifetime": 36500,
}


# ═══════════════════════════════════════════════════════════════
#  LICENCE OPERATIONS
# ═══════════════════════════════════════════════════════════════

def create_licence(client_name, company="", duration_key="1_month", daily_limit=20, notes=""):
    days = DURATIONS.get(duration_key, 30)
    key = generate_key()
    now = datetime.now()
    expires = now + timedelta(days=days)

    conn = get_db()
    conn.execute("""
        INSERT INTO licences (licence_key, client_name, company, created_at, expires_at, daily_limit, is_active, notes)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?)
    """, (key, client_name, company, now.isoformat(), expires.isoformat(), daily_limit, notes))
    conn.commit()
    conn.close()
    return key


def get_all_licences():
    conn = get_db()
    rows = conn.execute("SELECT * FROM licences ORDER BY created_at DESC").fetchall()
    result = []
    now = datetime.now()
    for r in rows:
        d = dict(r)
        expires = datetime.fromisoformat(d["expires_at"])
        d["is_expired"] = now > expires
        d["days_remaining"] = max(0, (expires - now).days)
        # Today's usage
        today = now.strftime("%Y-%m-%d")
        usage_today = conn.execute(
            "SELECT COUNT(*) as cnt FROM usage_log WHERE licence_key = ? AND timestamp LIKE ?",
            (d["licence_key"], f"{today}%")
        ).fetchone()["cnt"]
        d["usage_today"] = usage_today
        # Total usage
        total = conn.execute(
            "SELECT COUNT(*) as cnt FROM usage_log WHERE licence_key = ?",
            (d["licence_key"],)
        ).fetchone()["cnt"]
        d["usage_total"] = total
        # Estimated cost (4p per query)
        d["cost_estimate"] = round(total * 0.04, 2)
        result.append(d)
    conn.close()
    return result


def toggle_licence(licence_key, active):
    conn = get_db()
    conn.execute("UPDATE licences SET is_active = ? WHERE licence_key = ?", (1 if active else 0, licence_key))
    conn.commit()
    conn.close()


def update_licence(licence_key, daily_limit=None, notes=None, duration_key=None):
    conn = get_db()
    if daily_limit is not None:
        conn.execute("UPDATE licences SET daily_limit = ? WHERE licence_key = ?", (daily_limit, licence_key))
    if notes is not None:
        conn.execute("UPDATE licences SET notes = ? WHERE licence_key = ?", (notes, licence_key))
    if duration_key:
        days = DURATIONS.get(duration_key, 30)
        created = conn.execute("SELECT created_at FROM licences WHERE licence_key = ?", (licence_key,)).fetchone()
        if created:
            created_dt = datetime.fromisoformat(created["created_at"])
            new_expires = created_dt + timedelta(days=days)
            conn.execute("UPDATE licences SET expires_at = ? WHERE licence_key = ?", (new_expires.isoformat(), licence_key))
    conn.commit()
    conn.close()


def delete_licence(licence_key):
    conn = get_db()
    conn.execute("DELETE FROM licences WHERE licence_key = ?", (licence_key,))
    conn.execute("DELETE FROM usage_log WHERE licence_key = ?", (licence_key,))
    conn.commit()
    conn.close()


def validate_licence(licence_key):
    """Called by PlantIQ to check if a key is valid. Returns dict with status."""
    conn = get_db()
    row = conn.execute("SELECT * FROM licences WHERE licence_key = ?", (licence_key,)).fetchone()
    if not row:
        conn.close()
        return {"valid": False, "error": "Invalid licence key"}

    d = dict(row)
    now = datetime.now()
    expires = datetime.fromisoformat(d["expires_at"])

    if not d["is_active"]:
        conn.close()
        return {"valid": False, "error": "Licence has been deactivated"}

    if now > expires:
        conn.close()
        return {"valid": False, "error": f"Licence expired on {expires.strftime('%d %B %Y')}"}

    # Check daily usage
    today = now.strftime("%Y-%m-%d")
    usage_today = conn.execute(
        "SELECT COUNT(*) as cnt FROM usage_log WHERE licence_key = ? AND timestamp LIKE ?",
        (licence_key, f"{today}%")
    ).fetchone()["cnt"]
    conn.close()

    if usage_today >= d["daily_limit"]:
        return {"valid": False, "error": f"Daily limit reached ({d['daily_limit']} queries). Resets tomorrow."}

    return {
        "valid": True,
        "client_name": d["client_name"],
        "company": d["company"],
        "daily_limit": d["daily_limit"],
        "usage_today": usage_today,
        "days_remaining": max(0, (expires - now).days),
        "expires_at": d["expires_at"],
    }


def log_usage(licence_key, query_text=""):
    conn = get_db()
    conn.execute(
        "INSERT INTO usage_log (licence_key, query_text, timestamp, tokens_estimated) VALUES (?, ?, ?, ?)",
        (licence_key, query_text[:200], datetime.now().isoformat(), 4000)
    )
    conn.commit()
    conn.close()


def get_usage_history(licence_key, days=30):
    conn = get_db()
    since = (datetime.now() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT query_text, timestamp FROM usage_log WHERE licence_key = ? AND timestamp > ? ORDER BY timestamp DESC",
        (licence_key, since)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════
#  FASTAPI ADMIN SERVER
# ═══════════════════════════════════════════════════════════════

from fastapi import FastAPI, HTTPException, Request, Response, Cookie
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

app = FastAPI(title="PlantIQ Admin")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# ── Session management ──
_active_sessions = set()

def _make_token():
    return hashlib.sha256(f"{uuid.uuid4()}{time.time()}".encode()).hexdigest()

def _check_auth(request: Request):
    token = request.cookies.get("plantiq_session")
    if not token or token not in _active_sessions:
        return False
    return True

class LoginReq(BaseModel):
    username: str
    password: str


class CreateLicenceReq(BaseModel):
    client_name: str
    company: str = ""
    duration: str = "1_month"
    daily_limit: int = 20
    notes: str = ""

class UpdateLicenceReq(BaseModel):
    daily_limit: Optional[int] = None
    notes: Optional[str] = None
    duration: Optional[str] = None

class ValidateReq(BaseModel):
    licence_key: str

class LogUsageReq(BaseModel):
    licence_key: str
    query_text: str = ""


# ── API Endpoints ──

@app.get("/", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    if not _check_auth(request):
        return LOGIN_HTML
    return ADMIN_HTML

@app.post("/api/login")
async def login(req: LoginReq, response: Response):
    if req.username == ADMIN_USERNAME and req.password == ADMIN_PASSWORD:
        token = _make_token()
        _active_sessions.add(token)
        response.set_cookie("plantiq_session", token, httponly=True, samesite="lax", max_age=86400)
        return {"status": "ok"}
    raise HTTPException(status_code=401, detail="Invalid username or password")

@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get("plantiq_session")
    if token:
        _active_sessions.discard(token)
    response = RedirectResponse("/")
    response.delete_cookie("plantiq_session")
    return response

@app.get("/api/licences")
async def list_licences(request: Request):
    if not _check_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"licences": get_all_licences()}


@app.post("/api/licences")
async def create(req: CreateLicenceReq, request: Request):
    if not _check_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    key = create_licence(req.client_name, req.company, req.duration, req.daily_limit, req.notes)
    return {"licence_key": key, "status": "created"}


@app.patch("/api/licences/{key}")
async def update(key: str, req: UpdateLicenceReq, request: Request):
    if not _check_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    update_licence(key, req.daily_limit, req.notes, req.duration)
    return {"status": "updated"}


@app.post("/api/licences/{key}/activate")
async def activate(key: str, request: Request):
    if not _check_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    toggle_licence(key, True)
    return {"status": "activated"}


@app.post("/api/licences/{key}/deactivate")
async def deactivate(key: str, request: Request):
    if not _check_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    toggle_licence(key, False)
    return {"status": "deactivated"}


@app.delete("/api/licences/{key}")
async def remove(key: str, request: Request):
    if not _check_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    delete_licence(key)
    return {"status": "deleted"}


@app.get("/api/licences/{key}/usage")
async def usage(key: str, days: int = 30, request: Request = None):
    if not _check_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"usage": get_usage_history(key, days)}


# These endpoints are called by PlantIQ (not the admin UI)
@app.post("/api/validate")
async def validate(req: ValidateReq):
    return validate_licence(req.licence_key)


@app.post("/api/log-usage")
async def log(req: LogUsageReq):
    log_usage(req.licence_key, req.query_text)
    return {"status": "logged"}


# ═══════════════════════════════════════════════════════════════
#  LOGIN PAGE HTML
# ═══════════════════════════════════════════════════════════════

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PlantIQ Admin — Login</title>
<style>
:root { --bg: #0d0e12; --card: #131419; --box: #191c23; --blue: #6078B4; --green: #22c55e; --red: #ef4444; --text: #e2e8f0; --muted: #8892a8; --border: #2a2e3a; }
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', Tahoma, sans-serif; background: var(--bg); color: var(--text); display: flex; align-items: center; justify-content: center; min-height: 100vh; }
.login-card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 40px; width: 380px; text-align: center; }
.logo { font-size: 32px; font-weight: 900; margin-bottom: 4px; }
.logo span:first-child { color: var(--blue); }
.logo span:last-child { color: #fff; }
.subtitle { font-size: 10px; color: var(--muted); letter-spacing: 3px; text-transform: uppercase; margin-bottom: 30px; }
.field { margin-bottom: 16px; text-align: left; }
.field label { display: block; font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 4px; }
.field input { width: 100%; padding: 10px 14px; background: var(--box); border: 1px solid var(--border); border-radius: 6px; color: var(--text); font-size: 14px; outline: none; }
.field input:focus { border-color: var(--blue); }
.btn { width: 100%; padding: 12px; background: var(--blue); color: #fff; border: none; border-radius: 6px; font-size: 13px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; cursor: pointer; margin-top: 8px; }
.btn:hover { opacity: 0.9; }
.error { color: var(--red); font-size: 12px; margin-top: 12px; display: none; }
.footer { font-size: 10px; color: var(--muted); margin-top: 24px; }
</style>
</head>
<body>
<div class="login-card">
    <div class="logo"><span>Plant</span><span>IQ</span></div>
    <div class="subtitle">Admin Login</div>
    <div class="field">
        <label>Username</label>
        <input type="text" id="username" placeholder="Enter username" autofocus>
    </div>
    <div class="field">
        <label>Password</label>
        <input type="password" id="password" placeholder="Enter password" onkeydown="if(event.key==='Enter')doLogin()">
    </div>
    <button class="btn" onclick="doLogin()">Sign In</button>
    <div class="error" id="errorMsg">Invalid username or password</div>
    <div class="footer">Evoke Digital Engineering &copy; 2026</div>
</div>
<script>
async function doLogin() {
    const user = document.getElementById('username').value.trim();
    const pass = document.getElementById('password').value;
    if (!user || !pass) return;
    try {
        const r = await fetch('/api/login', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({username: user, password: pass})
        });
        if (r.ok) {
            window.location.href = '/';
        } else {
            document.getElementById('errorMsg').style.display = 'block';
        }
    } catch(e) {
        document.getElementById('errorMsg').style.display = 'block';
    }
}
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════
#  ADMIN DASHBOARD HTML
# ═══════════════════════════════════════════════════════════════

ADMIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PlantIQ Admin — Licence Management</title>
<style>
:root {
    --bg: #0d0e12;
    --card: #131419;
    --box: #191c23;
    --blue: #6078B4;
    --green: #22c55e;
    --red: #ef4444;
    --amber: #f59e0b;
    --text: #e2e8f0;
    --muted: #8892a8;
    --border: #2a2e3a;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', Tahoma, sans-serif; background: var(--bg); color: var(--text); padding: 30px; }
h1 { font-size: 28px; font-weight: 700; color: var(--blue); margin-bottom: 4px; }
h1 span { color: #fff; }
.subtitle { font-size: 12px; color: var(--muted); letter-spacing: 2px; text-transform: uppercase; margin-bottom: 30px; }
.stats-row { display: flex; gap: 16px; margin-bottom: 30px; }
.stat-card { background: var(--box); padding: 16px 20px; border-radius: 8px; flex: 1; }
.stat-num { font-size: 28px; font-weight: 700; color: #fff; }
.stat-label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; margin-top: 4px; }
.section-title { font-size: 16px; font-weight: 700; color: var(--blue); margin-bottom: 16px; letter-spacing: 1px; }
.create-form { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 24px; margin-bottom: 30px; }
.form-row { display: flex; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
.form-field { flex: 1; min-width: 150px; }
.form-field label { display: block; font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 4px; }
.form-field input, .form-field select { width: 100%; padding: 8px 12px; background: var(--box); border: 1px solid var(--border); border-radius: 4px; color: var(--text); font-size: 13px; outline: none; }
.form-field input:focus, .form-field select:focus { border-color: var(--blue); }
.btn { padding: 8px 20px; border: none; border-radius: 4px; font-size: 12px; font-weight: 600; cursor: pointer; letter-spacing: 1px; text-transform: uppercase; transition: opacity 0.2s; }
.btn:hover { opacity: 0.85; }
.btn-blue { background: var(--blue); color: #fff; }
.btn-green { background: var(--green); color: #fff; }
.btn-red { background: var(--red); color: #fff; }
.btn-small { padding: 4px 10px; font-size: 10px; }
.licence-table { width: 100%; border-collapse: collapse; margin-top: 12px; }
.licence-table th { text-align: left; padding: 10px 12px; font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; border-bottom: 2px solid var(--blue); }
.licence-table td { padding: 10px 12px; font-size: 13px; border-bottom: 1px solid var(--border); }
.licence-table tr:hover td { background: rgba(96,120,180,0.06); }
.key-text { font-family: 'Consolas', monospace; font-size: 12px; color: var(--blue); }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 10px; font-weight: 600; }
.badge-active { background: rgba(34,197,94,0.15); color: var(--green); }
.badge-expired { background: rgba(239,68,68,0.15); color: var(--red); }
.badge-disabled { background: rgba(136,146,168,0.15); color: var(--muted); }
.usage-bar { width: 80px; height: 6px; background: var(--box); border-radius: 3px; display: inline-block; vertical-align: middle; margin-right: 6px; }
.usage-fill { height: 100%; border-radius: 3px; background: var(--green); }
.usage-fill.warn { background: var(--amber); }
.usage-fill.full { background: var(--red); }
.new-key-display { background: var(--box); border: 2px solid var(--green); border-radius: 8px; padding: 16px; margin: 16px 0; text-align: center; }
.new-key-display .key { font-family: 'Consolas', monospace; font-size: 22px; color: var(--green); letter-spacing: 2px; }
.new-key-display .copy-hint { font-size: 11px; color: var(--muted); margin-top: 6px; }
.cost-text { font-family: monospace; color: var(--amber); }
</style>
</head>
<body>
<div style="display:flex;justify-content:space-between;align-items:flex-start">
    <div>
        <h1>Plant<span>IQ</span> Admin</h1>
        <div class="subtitle">Licence Management &amp; Usage Tracking</div>
    </div>
    <a href="/logout" class="btn btn-small" style="background:var(--box);color:var(--muted);text-decoration:none;margin-top:8px">LOGOUT</a>
</div>

<div class="stats-row">
    <div class="stat-card"><div class="stat-num" id="statTotal">0</div><div class="stat-label">Total Licences</div></div>
    <div class="stat-card"><div class="stat-num" id="statActive" style="color:var(--green)">0</div><div class="stat-label">Active</div></div>
    <div class="stat-card"><div class="stat-num" id="statExpired" style="color:var(--red)">0</div><div class="stat-label">Expired</div></div>
    <div class="stat-card"><div class="stat-num cost-text" id="statCost">£0.00</div><div class="stat-label">Total API Cost</div></div>
    <div class="stat-card"><div class="stat-num" id="statQueries">0</div><div class="stat-label">Queries Today</div></div>
</div>

<div class="section-title">Create New Licence</div>
<div class="create-form">
    <div class="form-row">
        <div class="form-field"><label>Client Name *</label><input type="text" id="newName" placeholder="e.g. John Smith"></div>
        <div class="form-field"><label>Company</label><input type="text" id="newCompany" placeholder="e.g. ACWA Power"></div>
        <div class="form-field"><label>Duration</label>
            <select id="newDuration">
                <option value="1_week">1 Week</option>
                <option value="2_weeks">2 Weeks</option>
                <option value="3_weeks">3 Weeks</option>
                <option value="1_month" selected>1 Month</option>
                <option value="2_months">2 Months</option>
                <option value="3_months">3 Months</option>
                <option value="6_months">6 Months</option>
                <option value="1_year">1 Year</option>
                <option value="2_years">2 Years</option>
                <option value="3_years">3 Years</option>
                <option value="5_years">5 Years</option>
                <option value="lifetime">Lifetime</option>
            </select>
        </div>
        <div class="form-field"><label>Daily Limit</label><input type="number" id="newLimit" value="20" min="1" max="500"></div>
    </div>
    <div class="form-row">
        <div class="form-field" style="flex:3"><label>Notes</label><input type="text" id="newNotes" placeholder="Optional notes..."></div>
        <div class="form-field" style="flex:0;align-self:flex-end"><button class="btn btn-blue" onclick="createLicence()">Create Licence</button></div>
    </div>
    <div id="newKeyResult"></div>
</div>

<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
    <div class="section-title" style="margin-bottom:0">All Licences</div>
    <button class="btn btn-blue" onclick="loadLicences()" style="padding:6px 16px;font-size:11px">&#8635; REFRESH</button>
</div>
<table class="licence-table">
    <thead>
        <tr>
            <th>Client</th>
            <th>Key</th>
            <th>Status</th>
            <th>Expires</th>
            <th>Usage Today</th>
            <th>Total Queries</th>
            <th>Cost</th>
            <th>Actions</th>
        </tr>
    </thead>
    <tbody id="licenceTableBody"></tbody>
</table>

<script>
const API = '';

async function loadLicences() {
    const r = await fetch(API + '/api/licences');
    const data = await r.json();
    const licences = data.licences || [];

    // Stats
    document.getElementById('statTotal').textContent = licences.length;
    document.getElementById('statActive').textContent = licences.filter(l => l.is_active && !l.is_expired).length;
    document.getElementById('statExpired').textContent = licences.filter(l => l.is_expired).length;
    const totalCost = licences.reduce((s, l) => s + (l.cost_estimate || 0), 0);
    document.getElementById('statCost').textContent = '\\u00a3' + totalCost.toFixed(2);
    const todayQueries = licences.reduce((s, l) => s + (l.usage_today || 0), 0);
    document.getElementById('statQueries').textContent = todayQueries;

    // Table
    const tbody = document.getElementById('licenceTableBody');
    tbody.innerHTML = licences.map(l => {
        let badge = '';
        if (!l.is_active) badge = '<span class="badge badge-disabled">Disabled</span>';
        else if (l.is_expired) badge = '<span class="badge badge-expired">Expired</span>';
        else badge = '<span class="badge badge-active">Active</span>';

        const pct = l.daily_limit > 0 ? Math.min(100, (l.usage_today / l.daily_limit) * 100) : 0;
        const fillClass = pct >= 100 ? 'full' : pct >= 70 ? 'warn' : '';
        const usageBar = `<div class="usage-bar"><div class="usage-fill ${fillClass}" style="width:${pct}%"></div></div>${l.usage_today}/${l.daily_limit}`;

        const expires = new Date(l.expires_at).toLocaleDateString('en-GB', {day:'numeric',month:'short',year:'numeric'});
        const daysLeft = l.days_remaining > 0 ? `(${l.days_remaining}d)` : '';

        const toggleBtn = l.is_active
            ? `<button class="btn btn-red btn-small" onclick="toggle('${l.licence_key}',false)">Disable</button>`
            : `<button class="btn btn-green btn-small" onclick="toggle('${l.licence_key}',true)">Enable</button>`;

        return `<tr>
            <td><strong>${esc(l.client_name)}</strong>${l.company ? '<br><span style="font-size:11px;color:var(--muted)">' + esc(l.company) + '</span>' : ''}</td>
            <td><span class="key-text">${l.licence_key}</span></td>
            <td>${badge}</td>
            <td>${expires} <span style="font-size:11px;color:var(--muted)">${daysLeft}</span></td>
            <td>${usageBar}</td>
            <td>${l.usage_total}</td>
            <td class="cost-text">\\u00a3${(l.cost_estimate || 0).toFixed(2)}</td>
            <td>${toggleBtn} <button class="btn btn-small" style="background:var(--box);color:var(--red)" onclick="deleteLicence('${l.licence_key}')">Delete</button></td>
        </tr>`;
    }).join('');
}

async function createLicence() {
    const name = document.getElementById('newName').value.trim();
    if (!name) { alert('Client name required'); return; }
    const body = {
        client_name: name,
        company: document.getElementById('newCompany').value.trim(),
        duration: document.getElementById('newDuration').value,
        daily_limit: parseInt(document.getElementById('newLimit').value) || 20,
        notes: document.getElementById('newNotes').value.trim()
    };
    const r = await fetch(API + '/api/licences', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const data = await r.json();
    if (data.licence_key) {
        document.getElementById('newKeyResult').innerHTML = `
            <div class="new-key-display">
                <div class="key">${data.licence_key}</div>
                <div class="copy-hint">Copy this key and give it to the client. They enter it in PlantIQ Settings.</div>
                <button class="btn btn-green" style="margin-top:8px" onclick="navigator.clipboard.writeText('${data.licence_key}')">Copy to Clipboard</button>
            </div>`;
        document.getElementById('newName').value = '';
        document.getElementById('newCompany').value = '';
        document.getElementById('newNotes').value = '';
        loadLicences();
    }
}

async function toggle(key, active) {
    const endpoint = active ? 'activate' : 'deactivate';
    await fetch(API + '/api/licences/' + key + '/' + endpoint, { method: 'POST' });
    loadLicences();
}

async function deleteLicence(key) {
    if (!confirm('Delete this licence and all usage data?')) return;
    await fetch(API + '/api/licences/' + key, { method: 'DELETE' });
    loadLicences();
}

function esc(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// Auto-refresh every 30 seconds
loadLicences();
setInterval(loadLicences, 30000);
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

# Always init DB (needed when Railway runs via Procfile)
init_db()

if __name__ == "__main__":
    import uvicorn

    print()
    print("  PlantIQ Admin — Licence Management")
    print("  Evoke Digital Engineering")
    print()
    print(f"  Dashboard:  http://{ADMIN_HOST}:{ADMIN_PORT}")
    print(f"  Database:   {DB_PATH}")
    print()
    print("  Press Ctrl+C to stop")
    print("  " + "=" * 42)
    print()

    uvicorn.run("plantiq_admin:app", host=ADMIN_HOST, port=ADMIN_PORT, reload=False, log_level="info")
