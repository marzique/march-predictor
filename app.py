"""Match prediction MVP — FastAPI + SQLite + Jinja2."""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import bcrypt
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from backup import dump_to_json, list_backups
from db import connect, init_db
from sync import sync_games, sync_teams

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
SYNC_INTERVAL = 300  # seconds


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    try:
        await sync_teams()          # one-time: download flags, store teams
        await sync_games()
    except Exception as e:          # don't crash startup if API is down
        print(f"[startup sync failed] {e}")
    task = asyncio.create_task(_sync_loop())
    yield
    task.cancel()


async def _sync_loop():
    while True:
        await asyncio.sleep(SYNC_INTERVAL)
        try:
            res = await sync_games()
            print(f"[sync] {res}")
        except Exception as e:
            print(f"[sync failed] {e}")


app = FastAPI(lifespan=lifespan)
app.add_middleware(SessionMiddleware,
                   secret_key="9920f005fc3bcba4dd46955a942b9bbdf21e33a2f482034302207f34d1815895")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


# ----------------------------------------------------------------- helpers ----
def render(request: Request, name: str, ctx: dict = None, status_code: int = 200):
    """Render a Jinja template directly — avoids TemplateResponse signature
    differences between Starlette versions (venv vs ~/.local)."""
    context = {"request": request, **(ctx or {})}
    html = templates.env.get_template(name).render(context)
    return HTMLResponse(html, status_code=status_code)


def current_user(request: Request):
    uid = request.session.get("uid")
    if not uid:
        return None
    with connect() as conn:
        return conn.execute("SELECT id, username FROM users WHERE id=?", (uid,)).fetchone()


def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def check_pw(pw: str, h: str) -> bool:
    return bcrypt.checkpw(pw.encode(), h.encode())


def _scorers(raw):
    """Parse the JSON scorers column into a list (empty on null/bad data)."""
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return []


def fetch_leaderboard(conn):
    return conn.execute(
        """SELECT u.username,
                  COALESCE(SUM(p.points), 0) AS total,
                  COUNT(p.id)                AS played
           FROM users u
           LEFT JOIN predictions p ON p.user_id = u.id
           GROUP BY u.id
           ORDER BY total DESC, played DESC, u.username""").fetchall()


# ------------------------------------------------------------------- routes ----
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    now = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        matches = conn.execute("SELECT * FROM matches ORDER BY kickoff").fetchall()
        rows = conn.execute(
            "SELECT match_id, pred_home, pred_away, points FROM predictions WHERE user_id=?",
            (user["id"],)).fetchall()
        flags = {t["id"]: t["flag"]
                 for t in conn.execute("SELECT id, flag FROM teams").fetchall()}
        board = fetch_leaderboard(conn)
        # everyone's predictions per match (only shown for locked matches)
        all_preds = conn.execute(
            """SELECT p.match_id, u.username, p.pred_home, p.pred_away, p.points
               FROM predictions p JOIN users u ON u.id = p.user_id
               ORDER BY p.points DESC, u.username""").fetchall()
        ccounts = {r["match_id"]: r["n"] for r in conn.execute(
            "SELECT match_id, COUNT(*) AS n FROM comments GROUP BY match_id").fetchall()}
        all_usernames = [r["username"] for r in
                         conn.execute("SELECT username FROM users ORDER BY username")]
    nusers = len(all_usernames)
    preds = {r["match_id"]: r for r in rows}
    others = {}
    pred_users = {}
    for r in all_preds:
        others.setdefault(r["match_id"], []).append(r)
        pred_users.setdefault(r["match_id"], set()).add(r["username"])

    items = []
    for m in matches:
        locked = m["kickoff"] <= now or m["finished"]
        did = pred_users.get(m["id"], set())
        items.append({"m": m, "pred": preds.get(m["id"]), "locked": bool(locked),
                      "home_flag": flags.get(m["home_id"]),
                      "away_flag": flags.get(m["away_id"]),
                      "home_scorers": _scorers(m["home_scorers"]),
                      "away_scorers": _scorers(m["away_scorers"]),
                      "others": others.get(m["id"], []) if locked else [],
                      "ncomments": ccounts.get(m["id"], 0),
                      "ncount": len(did), "nusers": nusers,
                      "predictors": sorted(did, key=str.lower),
                      "missing": [u for u in all_usernames if u not in did]})
    return render(request, "matches.html",
                  {"user": user, "items": items, "board": board})


@app.post("/predict/{match_id}")
async def predict(request: Request, match_id: str,
                  pred_home: int = Form(...), pred_away: int = Form(...)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if pred_home < 0 or pred_away < 0:
        return RedirectResponse("/", status_code=302)

    now = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        m = conn.execute("SELECT kickoff, finished FROM matches WHERE id=?",
                         (match_id,)).fetchone()
        if m and m["kickoff"] > now and not m["finished"]:   # only future matches
            conn.execute(
                """INSERT INTO predictions (user_id, match_id, pred_home, pred_away)
                   VALUES (?,?,?,?)
                   ON CONFLICT(user_id, match_id)
                   DO UPDATE SET pred_home=excluded.pred_home,
                                 pred_away=excluded.pred_away""",
                (user["id"], match_id, pred_home, pred_away))
    return RedirectResponse("/", status_code=302)


@app.post("/predict-bulk")
async def predict_bulk(request: Request):
    """Save every filled-in prediction on the page in one shot."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    form = await request.form()
    now = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        matches = {m["id"]: m for m in
                   conn.execute("SELECT id, kickoff, finished FROM matches").fetchall()}
        for key in form:
            if not key.startswith("h_"):
                continue
            mid = key[2:]
            h = (form.get(f"h_{mid}") or "").strip()
            a = (form.get(f"a_{mid}") or "").strip()
            if h == "" or a == "":          # skip matches left blank
                continue
            try:
                hi, ai = int(h), int(a)
            except ValueError:
                continue
            if hi < 0 or ai < 0:
                continue
            m = matches.get(mid)
            if m and m["kickoff"] > now and not m["finished"]:   # future only
                conn.execute(
                    """INSERT INTO predictions (user_id, match_id, pred_home, pred_away)
                       VALUES (?,?,?,?)
                       ON CONFLICT(user_id, match_id)
                       DO UPDATE SET pred_home=excluded.pred_home,
                                     pred_away=excluded.pred_away""",
                    (user["id"], mid, hi, ai))
    return RedirectResponse("/", status_code=302)


@app.get("/comments/{match_id}")
async def get_comments(request: Request, match_id: str):
    if not current_user(request):
        return JSONResponse({"error": "auth"}, status_code=401)
    with connect() as conn:
        rows = conn.execute(
            """SELECT u.username, c.body, c.created_at
               FROM comments c JOIN users u ON u.id = c.user_id
               WHERE c.match_id = ? ORDER BY c.id""", (match_id,)).fetchall()
    return JSONResponse([dict(r) for r in rows])


@app.post("/comment/{match_id}")
async def add_comment(request: Request, match_id: str, body: str = Form(...)):
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "auth"}, status_code=401)
    body = body.strip()[:500]
    if not body:
        return JSONResponse({"error": "empty"}, status_code=400)
    created = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        m = conn.execute("SELECT id FROM matches WHERE id=?", (match_id,)).fetchone()
        if not m:
            return JSONResponse({"error": "no match"}, status_code=404)
        conn.execute(
            "INSERT INTO comments (match_id, user_id, body, created_at) VALUES (?,?,?,?)",
            (match_id, user["id"], body, created))
    return JSONResponse({"username": user["username"], "body": body, "created_at": created})


@app.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    with connect() as conn:
        rows = fetch_leaderboard(conn)
    return render(request, "leaderboard.html", {"user": user, "rows": rows})


@app.get("/progress", response_class=HTMLResponse)
async def progress(request: Request):
    """Cumulative points per user across finished matches (in date order)."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    with connect() as conn:
        matches = conn.execute(
            "SELECT id, home, away FROM matches WHERE finished=1 ORDER BY kickoff, id"
        ).fetchall()
        users = conn.execute("SELECT id, username FROM users").fetchall()
        preds = conn.execute(
            "SELECT user_id, match_id, points FROM predictions").fetchall()

    pts = {(p["user_id"], p["match_id"]): p["points"] for p in preds}
    labels = ["Start"] + [f'{m["home"][:3].upper()}–{m["away"][:3].upper()}'
                          for m in matches]

    series = {}     # uid -> (username, cumulative list)
    for u in users:
        run, data = 0, [0]
        for m in matches:
            run += pts.get((u["id"], m["id"]), 0)
            data.append(run)
        series[u["id"]] = (u["username"], data)

    # legend/draw order: highest final total first
    datasets = [{"label": series[uid][0], "data": series[uid][1]}
                for uid in sorted(series, key=lambda k: -series[k][1][-1])]
    chart = {"labels": labels, "datasets": datasets}
    return render(request, "progress.html",
                  {"user": user, "chart": chart, "has_data": bool(matches)})


# ------------------------------------------------------------------- auth ----
@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return render(request, "login.html", {"error": None})


@app.post("/login")
async def login(request: Request,
                username: str = Form(...), password: str = Form(...),
                action: str = Form("login")):
    username = username.strip()
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()

        if action == "register":
            if not username or not password:
                return _login_err(request, "Username and password required.")
            if row:
                return _login_err(request, "Username taken.")
            conn.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                (username, hash_pw(password), datetime.now().isoformat()))
            new = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
            request.session["uid"] = new["id"]
            return RedirectResponse("/", status_code=302)

        # login
        if not row or not check_pw(password, row["password_hash"]):
            return _login_err(request, "Wrong username or password.")
        request.session["uid"] = row["id"]
        return RedirectResponse("/", status_code=302)


def _login_err(request, msg):
    return render(request, "login.html", {"error": msg}, status_code=400)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@app.post("/sync")
async def manual_sync(request: Request):
    if not current_user(request):
        return RedirectResponse("/login", status_code=302)
    await sync_games()
    return RedirectResponse("/", status_code=302)


@app.get("/backup", response_class=HTMLResponse)
async def backup_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    snap = dump_to_json("manual")             # run a backup on visit
    return render(request, "backup.html",
                  {"user": user, "saved": snap.name, "count": len(list_backups())})
