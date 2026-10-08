import json
import sqlite3
from pathlib import Path
from uuid import uuid4


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, question TEXT NOT NULL,
                result TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
            db.execute("CREATE INDEX IF NOT EXISTS runs_owner ON runs(owner)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def save(self, owner, question, result):
        run_id = str(uuid4())
        with self.connect() as db:
            db.execute(
                "INSERT INTO runs(id, owner, question, result) VALUES (?, ?, ?, ?)",
                (run_id, owner, question, json.dumps(result)),
            )
        return {"id": run_id, "question": question, **result}

    def get(self, owner, run_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT question, result FROM runs WHERE owner=? AND id=?", (owner, run_id)
            ).fetchone()
        return {"id": run_id, "question": row[0], **json.loads(row[1])} if row else None

    def list(self, owner):
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, question, created_at FROM runs WHERE owner=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 50",
                (owner,),
            ).fetchall()
        return [{"id": r[0], "question": r[1], "created_at": r[2]} for r in rows]
