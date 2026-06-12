# Match Result Predictor

Superfast MVP. FastAPI + SQLite + Jinja2. Friends log in, predict future matches, earn points, climb the leaderboard.

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 → Register a user → predict.

## Share with friends (ngrok)

```bash
ngrok http 8000
```

Send the `https://...ngrok...` link.

## How it works

- **Games** fetched from `https://worldcup26.ir/get/games` on startup + every 5 min. New `_id`s inserted; finished matches get scores and trigger point recompute.
- **Predictions** editable only while a match is in the future. Locks at kickoff.
- **Storage**: single `data.db` SQLite file (WAL mode for concurrent friends).

## Backups

Every write (prediction, registration), every sync, and every startup dumps the whole DB to JSON in `backups/`:
- `backups/latest.json` — newest snapshot
- `backups/snapshot-*.json` — history (last 50)

Restore if the DB is ever lost:
```bash
python backup.py restore                                # from latest.json
python backup.py restore backups/snapshot-20260612-001500.json
python backup.py dump      # manual snapshot
python backup.py list      # list snapshots
```

## Points

| Pts | Condition |
|-----|-----------|
| 6 | exact score |
| 4 | right outcome + right goal difference |
| 3 | right outcome + one team's goals exact |
| 2 | right outcome (win/draw) |
| 0 | wrong outcome |

Highest matching tier wins (not additive). Tune in `db.py:score()`.
