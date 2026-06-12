"""SQLite storage + scoring. Single file DB, zero config."""
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "data.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS matches (
    id         TEXT PRIMARY KEY,          -- _id from API
    home       TEXT NOT NULL,
    away       TEXT NOT NULL,
    home_id    TEXT,                      -- team id -> teams.id (for flag)
    away_id    TEXT,
    kickoff    TEXT NOT NULL,             -- ISO 8601
    home_score INTEGER,
    away_score INTEGER,
    finished   INTEGER NOT NULL DEFAULT 0,
    grp        TEXT,
    matchday   TEXT,
    home_scorers TEXT,                     -- JSON list of "Name 67'"
    away_scorers TEXT
);
CREATE TABLE IF NOT EXISTS teams (
    id        TEXT PRIMARY KEY,           -- team id (matches games' home_team_id)
    name_en   TEXT,
    fifa_code TEXT,
    flag      TEXT                        -- local path, e.g. /static/flags/2.png
);
CREATE TABLE IF NOT EXISTS predictions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL,
    match_id  TEXT NOT NULL,
    pred_home INTEGER NOT NULL,
    pred_away INTEGER NOT NULL,
    points    INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user_id, match_id)
);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")   # better concurrent reads/writes
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn):
    """Add columns to an existing DB created before they existed."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(matches)")}
    for col in ("home_scorers", "away_scorers"):
        if col not in cols:
            conn.execute(f"ALTER TABLE matches ADD COLUMN {col} TEXT")


# ---------------------------------------------------------------- scoring ----
def score(pred_home, pred_away, real_home, real_away):
    """Points for one prediction vs final result. Highest matching tier wins.

    6  exact score
    4  correct outcome + correct goal difference (covers nailed draws)
    3  correct outcome + one side's goals exactly right
    2  correct outcome only (winner / draw)
    0  wrong outcome
    """
    def outcome(h, a):
        return (h > a) - (h < a)   # 1 home win, -1 away win, 0 draw

    if outcome(pred_home, pred_away) != outcome(real_home, real_away):
        return 0
    if pred_home == real_home and pred_away == real_away:
        return 6
    if (pred_home - pred_away) == (real_home - real_away):
        return 4
    if pred_home == real_home or pred_away == real_away:
        return 3
    return 2
