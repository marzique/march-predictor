"""JSON backup / restore for the SQLite DB.

Every write dumps the whole DB to JSON (tiny: users + matches + predictions).
- backups/latest.json        always the newest snapshot (use this to restore)
- backups/snapshot-*.json    timestamped history, last MAX_SNAPSHOTS kept

Restore from a terminal if the DB is ever lost:
    python backup.py restore                      # restores backups/latest.json
    python backup.py restore backups/snapshot-20260612-001500.json
"""
import json
import sys
from datetime import datetime
from pathlib import Path

from db import connect, init_db

BACKUP_DIR = Path(__file__).parent / "backups"
TABLES = ["users", "teams", "matches", "predictions", "comments"]
MAX_SNAPSHOTS = 50


def dump_to_json(label: str = "auto") -> Path:
    """Write a full snapshot of every table to JSON. Returns the snapshot path."""
    BACKUP_DIR.mkdir(exist_ok=True)
    with connect() as conn:
        data = {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t}").fetchall()]
                for t in TABLES}

    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "label": label,
        "counts": {t: len(rows) for t, rows in data.items()},
        "tables": data,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    # always-current copy for easy restore
    (BACKUP_DIR / "latest.json").write_text(text, encoding="utf-8")
    # timestamped history
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    snap = BACKUP_DIR / f"snapshot-{stamp}.json"
    snap.write_text(text, encoding="utf-8")

    _prune()
    return snap


def _prune():
    snaps = sorted(BACKUP_DIR.glob("snapshot-*.json"))
    for old in snaps[:-MAX_SNAPSHOTS]:
        old.unlink(missing_ok=True)


def list_backups():
    return sorted(BACKUP_DIR.glob("snapshot-*.json"))


def restore_from_json(path: Path | str = None) -> dict:
    """Replace all table contents from a JSON snapshot. Returns row counts."""
    path = Path(path) if path else (BACKUP_DIR / "latest.json")
    if not path.exists():
        raise FileNotFoundError(f"No backup at {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    tables = payload.get("tables", {})

    init_db()
    counts = {}
    with connect() as conn:
        for t in TABLES:
            rows = tables.get(t, [])
            if not rows:
                counts[t] = 0
                continue
            cols = list(rows[0].keys())
            placeholders = ",".join("?" * len(cols))
            collist = ",".join(cols)

            def clean(col, val):           # trim stray whitespace from id keys
                if col in ("match_id", "id") and isinstance(val, str):
                    return val.strip()
                return val

            conn.executemany(
                f"INSERT OR REPLACE INTO {t} ({collist}) VALUES ({placeholders})",
                [tuple(clean(c, r[c]) for c in cols) for r in rows],
            )
            counts[t] = len(rows)
    return counts


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "dump"
    if cmd == "dump":
        p = dump_to_json("manual")
        print(f"Backed up to {p}")
    elif cmd == "restore":
        src = sys.argv[2] if len(sys.argv) > 2 else None
        res = restore_from_json(src)
        print(f"Restored: {res}")
    elif cmd == "list":
        for b in list_backups():
            print(b.name)
    else:
        print(__doc__)
