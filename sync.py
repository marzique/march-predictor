"""Fetch games from the World Cup API, upsert into SQLite, recompute points."""
from datetime import datetime
from pathlib import Path

import httpx

from db import connect

GAMES_URL = "https://worldcup26.ir/get/games"
TEAMS_URL = "https://worldcup26.ir/get/teams"
FLAGS_DIR = Path(__file__).parent / "static" / "flags"


def _parse_kickoff(local_date: str) -> str | None:
    # API format: "06/11/2026 13:00"
    try:
        return datetime.strptime(local_date.strip(), "%m/%d/%Y %H:%M").isoformat()
    except (ValueError, AttributeError):
        return None


def _to_int(v):
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


async def sync_teams(force: bool = False) -> dict:
    """Fetch teams once, download each flag to static/flags/<id>.png, store rows.

    Skips entirely if teams already loaded (unless force=True). Flags that are
    already on disk are not re-downloaded.
    """
    with connect() as conn:
        have = conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    if have and not force:
        return {"teams": have, "skipped": True}

    FLAGS_DIR.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(TEAMS_URL)
        resp.raise_for_status()
        teams = resp.json().get("teams", [])

        downloaded = 0
        with connect() as conn:
            for t in teams:
                tid = t.get("id")
                if not tid:
                    continue
                local = f"/static/flags/{tid}.png"
                dest = FLAGS_DIR / f"{tid}.png"
                if not dest.exists() and t.get("flag"):
                    try:
                        img = await client.get(t["flag"])
                        img.raise_for_status()
                        dest.write_bytes(img.content)
                        downloaded += 1
                    except httpx.HTTPError:
                        local = None      # keep row, just no flag
                conn.execute(
                    """INSERT INTO teams (id, name_en, fifa_code, flag)
                       VALUES (?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET
                         name_en=excluded.name_en, fifa_code=excluded.fifa_code,
                         flag=COALESCE(excluded.flag, teams.flag)""",
                    (tid, t.get("name_en"), t.get("fifa_code"), local))
    return {"teams": len(teams), "downloaded": downloaded}


async def sync_games() -> dict:
    """Pull games, insert new ones, update scores/finished. Returns counts."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(GAMES_URL)
        resp.raise_for_status()
        games = resp.json().get("games", [])

    new, updated = 0, 0
    finished_ids = []
    with connect() as conn:
        for g in games:
            mid = g.get("_id")
            kickoff = _parse_kickoff(g.get("local_date", ""))
            if not mid or not kickoff:
                continue
            finished = 1 if str(g.get("finished", "")).upper() == "TRUE" else 0
            hs = _to_int(g.get("home_score")) if finished else None
            as_ = _to_int(g.get("away_score")) if finished else None

            row = conn.execute("SELECT id FROM matches WHERE id=?", (mid,)).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO matches
                       (id, home, away, home_id, away_id, kickoff,
                        home_score, away_score, finished, grp, matchday)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (mid, g.get("home_team_name_en", "?"), g.get("away_team_name_en", "?"),
                     g.get("home_team_id"), g.get("away_team_id"),
                     kickoff, hs, as_, finished, g.get("group"), g.get("matchday")),
                )
                new += 1
            else:
                conn.execute(
                    """UPDATE matches
                       SET home=?, away=?, home_id=?, away_id=?, kickoff=?,
                           home_score=?, away_score=?, finished=?, grp=?, matchday=?
                       WHERE id=?""",
                    (g.get("home_team_name_en", "?"), g.get("away_team_name_en", "?"),
                     g.get("home_team_id"), g.get("away_team_id"),
                     kickoff, hs, as_, finished, g.get("group"), g.get("matchday"), mid),
                )
                updated += 1
            if finished and hs is not None and as_ is not None:
                finished_ids.append(mid)

    if finished_ids:
        recompute_points(finished_ids)
    return {"new": new, "updated": updated, "finished": len(finished_ids)}


def recompute_points(match_ids=None):
    """Recompute prediction points for finished matches (all, or a subset)."""
    from db import score
    with connect() as conn:
        if match_ids:
            q = "SELECT * FROM matches WHERE finished=1 AND id IN (%s)" % \
                ",".join("?" * len(match_ids))
            matches = conn.execute(q, match_ids).fetchall()
        else:
            matches = conn.execute("SELECT * FROM matches WHERE finished=1").fetchall()
        for m in matches:
            if m["home_score"] is None or m["away_score"] is None:
                continue
            preds = conn.execute(
                "SELECT id, pred_home, pred_away FROM predictions WHERE match_id=?",
                (m["id"],)).fetchall()
            for p in preds:
                pts = score(p["pred_home"], p["pred_away"],
                            m["home_score"], m["away_score"])
                conn.execute("UPDATE predictions SET points=? WHERE id=?",
                             (pts, p["id"]))
