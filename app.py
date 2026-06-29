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
ADMINS = {"marzique"}   # usernames allowed to set results manually — edit as needed


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
    if "is_admin" not in context:
        u = current_user(request)
        context["is_admin"] = bool(u and u["username"] in ADMINS)
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
    # Tiebreak: points -> outcome-correct % -> exact % -> alphabetical.
    # Percentages use only FINISHED matches the user predicted.
    return conn.execute(
        """SELECT u.username,
                  COALESCE(SUM(p.points), 0)                                AS total,
                  COUNT(p.id)                                               AS played,
                  SUM(CASE WHEN m.finished=1 THEN 1 ELSE 0 END)             AS fin,
                  SUM(CASE WHEN m.finished=1 AND p.points>=2 THEN 1 ELSE 0 END) AS correct,
                  SUM(CASE WHEN m.finished=1 AND p.points=6 THEN 1 ELSE 0 END)  AS exact
           FROM users u
           LEFT JOIN predictions p ON p.user_id = u.id
           LEFT JOIN matches m     ON m.id = p.match_id
           GROUP BY u.id
           ORDER BY
             COALESCE(SUM(p.points), 0) DESC,
             CASE WHEN SUM(CASE WHEN m.finished=1 THEN 1 ELSE 0 END) > 0
                  THEN 1.0 * SUM(CASE WHEN m.finished=1 AND p.points>=2 THEN 1 ELSE 0 END)
                           / SUM(CASE WHEN m.finished=1 THEN 1 ELSE 0 END)
                  ELSE 0 END DESC,
             CASE WHEN SUM(CASE WHEN m.finished=1 THEN 1 ELSE 0 END) > 0
                  THEN 1.0 * SUM(CASE WHEN m.finished=1 AND p.points=6 THEN 1 ELSE 0 END)
                           / SUM(CASE WHEN m.finished=1 THEN 1 ELSE 0 END)
                  ELSE 0 END DESC,
             LOWER(u.username)""").fetchall()


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

    # each team's finished results (chronological — matches already sorted by kickoff)
    team_results = {}
    for m in matches:
        if not m["finished"] or m["home_score"] is None or m["away_score"] is None:
            continue
        hs, as_ = m["home_score"], m["away_score"]
        team_results.setdefault(m["home_id"], []).append(
            {"res": "W" if hs > as_ else "D" if hs == as_ else "L",
             "gf": hs, "ga": as_, "opp": m["away"]})
        team_results.setdefault(m["away_id"], []).append(
            {"res": "W" if as_ > hs else "D" if as_ == hs else "L",
             "gf": as_, "ga": hs, "opp": m["home"]})

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
                  {"user": user, "items": items, "board": board,
                   "team_results": team_results})


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


def build_metrics(conn):
    """Per-user series (for charts) + per-user stats. Tiebreak-ordered. Read-only."""
    matches = conn.execute(
        "SELECT id, home, away FROM matches WHERE finished=1 ORDER BY kickoff, id"
    ).fetchall()
    users = conn.execute("SELECT id, username FROM users ORDER BY id").fetchall()
    preds = conn.execute("SELECT user_id, match_id, points FROM predictions").fetchall()

    mids = [m["id"] for m in matches]
    n = len(mids)
    # (uid, mid) -> points, only present if the user predicted that match
    pts = {(p["user_id"], p["match_id"]): p["points"] for p in preds}
    labels = ["Start"] + [f'{m["home"][:3].upper()}–{m["away"][:3].upper()}'
                          for m in matches]
    ROLL = 5   # rolling-average window (matches)

    # per-user series over the ordered matches
    udata = {}   # uid -> dict of series + stat fields
    for u in users:
        uid = u["id"]
        cum = cum_ex = 0
        deltas, cum_pts, cum_exact = [], [0], [0]
        played = correct = exact = best = 0
        worst = None
        tiers = {6: 0, 4: 0, 3: 0, 2: 0, 0: 0}
        streak = cur_streak = 0
        for mid in mids:
            has = (uid, mid) in pts
            p = pts.get((uid, mid), 0)
            cum += p
            deltas.append(p)
            cum_pts.append(cum)
            if has:
                played += 1
                tiers[p if p in tiers else 0] += 1
                if p >= 2:
                    correct += 1
                if p == 6:
                    exact += 1
                    cum_ex += 1
                best = max(best, p)
                worst = p if worst is None else min(worst, p)
                cur_streak = cur_streak + 1 if p >= 2 else 0
                streak = max(streak, cur_streak)
            else:
                cur_streak = 0
            cum_exact.append(cum_ex)
        # rolling avg points/match aligned to labels (index 0 = Start = 0)
        roll = [0]
        for i in range(1, n + 1):
            window = deltas[max(0, i - ROLL):i]
            roll.append(round(sum(window) / len(window), 2) if window else 0)
        udata[uid] = {
            "uid": uid, "name": u["username"],
            "points": cum_pts, "exact": cum_exact, "roll": roll,
            "played": played, "total": cum, "correct": correct, "nexact": exact,
            "tiers": tiers, "best": best, "worst": worst or 0, "streak": streak,
        }

    # rank + gap need cross-user values per step
    for s in range(n + 1):
        standing = sorted(users, key=lambda u: -udata[u["id"]]["points"][s])
        leader = udata[standing[0]["id"]]["points"][s] if users else 0
        rank = 0
        for i, u in enumerate(standing):
            d = udata[u["id"]]
            if i == 0 or d["points"][s] != udata[standing[i - 1]["id"]]["points"][s]:
                rank = i + 1
            d.setdefault("rank", []).append(rank)
            d.setdefault("gap", []).append(leader - d["points"][s])

    def _tiebreak(u):
        d = udata[u["id"]]
        pl = d["played"] or 1
        return (-d["total"], -(d["correct"] / pl), -(d["nexact"] / pl), d["name"].lower())
    order = sorted(users, key=_tiebreak)   # points -> correct% -> exact% -> alphabetical
    ordered = [udata[u["id"]] for u in order]

    chart = {
        "labels": labels,
        "users": [{
            "uid": d["uid"], "name": d["name"], "points": d["points"],
            "rank": d["rank"], "gap": d["gap"], "roll": d["roll"], "exact": d["exact"],
        } for d in ordered],
        "breakdown": {
            "names": [d["name"] for d in ordered],
            "uids": [d["uid"] for d in ordered],
            "tiers": {str(t): [d["tiers"][t] for d in ordered] for t in (6, 4, 3, 2, 0)},
        },
    }
    stats = [{
        "name": d["name"], "uid": d["uid"], "played": d["played"], "total": d["total"],
        "avg": round(d["total"] / d["played"], 2) if d["played"] else 0,
        "acc": round(100 * d["correct"] / d["played"]) if d["played"] else 0,
        "exact_pct": round(100 * d["nexact"] / d["played"]) if d["played"] else 0,
        "t6": d["tiers"][6], "t4": d["tiers"][4], "t3": d["tiers"][3],
        "t2": d["tiers"][2], "t0": d["tiers"][0],
        "best": d["best"], "worst": d["worst"], "streak": d["streak"],
    } for d in ordered]

    return chart, stats, n > 0


@app.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    with connect() as conn:
        _, stats, has_data = build_metrics(conn)
    return render(request, "leaderboard.html",
                  {"user": user, "stats": stats, "has_data": has_data})


@app.get("/progress", response_class=HTMLResponse)
async def progress(request: Request):
    """Line-chart metrics across finished matches. Read-only."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    with connect() as conn:
        chart, _, has_data = build_metrics(conn)
    return render(request, "progress.html",
                  {"user": user, "chart": chart, "has_data": has_data})


@app.get("/insights", response_class=HTMLResponse)
async def insights(request: Request):
    """Per-match & per-team prediction difficulty (how often the crowd got it right)."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    with connect() as conn:
        matches = conn.execute(
            """SELECT id, home, away, home_id, away_id, home_score, away_score
               FROM matches WHERE finished=1 ORDER BY kickoff""").fetchall()
        flags = {t["id"]: t["flag"] for t in conn.execute("SELECT id, flag FROM teams")}
        prows = conn.execute("SELECT match_id, points FROM predictions").fetchall()

    fin = {m["id"] for m in matches}
    by_match = {}
    for p in prows:
        if p["match_id"] in fin:
            by_match.setdefault(p["match_id"], []).append(p["points"])

    match_stats, team_agg = [], {}
    for m in matches:
        pl = by_match.get(m["id"], [])
        total = len(pl)
        correct = sum(1 for x in pl if x >= 2)
        exact = sum(1 for x in pl if x == 6)
        if total:
            match_stats.append({
                "label": f'{m["home"]} {m["home_score"]}:{m["away_score"]} {m["away"]}',
                "total": total, "correct": correct, "exact": exact,
                "acc": round(100 * correct / total)})
        for tid, name in ((m["home_id"], m["home"]), (m["away_id"], m["away"])):
            a = team_agg.setdefault(tid, {"name": name, "flag": flags.get(tid),
                                          "correct": 0, "total": 0, "exact": 0, "matches": 0})
            a["name"] = name
            a["correct"] += correct
            a["total"] += total
            a["exact"] += exact
            a["matches"] += 1

    teams = [{"name": a["name"], "flag": a["flag"], "matches": a["matches"],
              "preds": a["total"],
              "acc": round(100 * a["correct"] / a["total"]) if a["total"] else 0,
              "exact_pct": round(100 * a["exact"] / a["total"]) if a["total"] else 0}
             for a in team_agg.values() if a["total"] > 0]
    teams.sort(key=lambda x: (x["acc"], -x["preds"]))       # hardest first
    match_stats.sort(key=lambda x: (x["acc"], -x["total"]))  # hardest first

    chart = {"labels": [t["name"] for t in teams], "acc": [t["acc"] for t in teams]}
    return render(request, "insights.html",
                  {"user": user, "teams": teams, "matches": match_stats,
                   "chart": chart, "has_data": bool(teams)})


# ------------------------------------------------------------------ admin ----
def _scorers_text(raw):
    """JSON scorers column -> comma-separated string for the admin input."""
    return ", ".join(_scorers(raw))


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user["username"] not in ADMINS:
        return HTMLResponse("Forbidden", status_code=403)
    now = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        # only matches that have kicked off (or already have a result) — those need editing
        rows = conn.execute(
            "SELECT * FROM matches WHERE kickoff <= ? OR finished = 1 ORDER BY kickoff DESC",
            (now,)).fetchall()
    items = [{"m": m,
              "home_scorers": _scorers_text(m["home_scorers"]),
              "away_scorers": _scorers_text(m["away_scorers"])} for m in rows]
    return render(request, "admin.html", {"user": user, "items": items})


@app.post("/admin/result/{match_id}")
async def admin_save(request: Request, match_id: str,
                     home_score: str = Form(""), away_score: str = Form(""),
                     home_scorers: str = Form(""), away_scorers: str = Form(""),
                     action: str = Form("save")):
    user = current_user(request)
    if not user or user["username"] not in ADMINS:
        return HTMLResponse("Forbidden", status_code=403)

    from sync import parse_scorers, recompute_points
    with connect() as conn:
        m = conn.execute("SELECT id FROM matches WHERE id=?", (match_id,)).fetchone()
        if not m:
            return RedirectResponse("/admin", status_code=302)

        if action == "clear":
            # hand the match back to the API sync; reset result + points
            conn.execute(
                """UPDATE matches SET manual=0, finished=0,
                   home_score=NULL, away_score=NULL,
                   home_scorers=NULL, away_scorers=NULL WHERE id=?""", (match_id,))
            conn.execute("UPDATE predictions SET points=0 WHERE match_id=?", (match_id,))
            return RedirectResponse("/admin", status_code=302)

        # save a manual result
        try:
            hi, ai = int(home_score), int(away_score)
        except ValueError:
            return RedirectResponse("/admin", status_code=302)
        if hi < 0 or ai < 0:
            return RedirectResponse("/admin", status_code=302)
        conn.execute(
            """UPDATE matches
               SET home_score=?, away_score=?, finished=1, manual=1,
                   home_scorers=?, away_scorers=? WHERE id=?""",
            (hi, ai, parse_scorers(home_scorers), parse_scorers(away_scorers), match_id))
    recompute_points([match_id])
    return RedirectResponse("/admin", status_code=302)


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
