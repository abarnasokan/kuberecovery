"""SQLite persistence for incidents, checkpoints, and a chronological audit log."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .domain import RecoveryError


class Store:
    def __init__(self, path, recover_interrupted=False):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS incidents (
                    id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checkpoints (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL,
                    incident_id TEXT, action TEXT NOT NULL, actor TEXT NOT NULL, details TEXT NOT NULL
                );
            """)
            # An interrupted recovery is never silently replayed: its cluster outcome is unknown.
            rows = (
                db.execute("SELECT id, data FROM incidents").fetchall()
                if recover_interrupted
                else []
            )
            for row in rows:
                data = json.loads(row[1])
                if data["status"] == "executing":
                    data.update(
                        status="verification_required",
                        error="Server stopped during recovery. Inspect cluster state; do not blindly retry.",
                    )
                    db.execute("UPDATE incidents SET data=? WHERE id=?", (json.dumps(data), row[0]))
                    db.execute(
                        "INSERT INTO audit(timestamp, incident_id, action, actor, details) VALUES(strftime('%s','now'),?,?,?,?)",
                        (
                            row[0],
                            "execution_interrupted",
                            "system",
                            json.dumps({"outcome": "unknown"}),
                        ),
                    )

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def incidents(self):
        with self.connection() as db:
            return [
                json.loads(row[0])
                for row in db.execute("SELECT data FROM incidents ORDER BY rowid DESC")
            ]

    def incident(self, identifier):
        with self.connection() as db:
            row = db.execute("SELECT data FROM incidents WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise RecoveryError("Incident not found.", 404)
        return json.loads(row[0])

    def by_key(self, key):
        with self.connection() as db:
            row = db.execute("SELECT data FROM incidents WHERE request_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_incident(self, data, audit):
        with self.connection() as db:
            db.execute(
                "INSERT INTO incidents(id, request_key, data) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (data["id"], data["request_key"], json.dumps(data)),
            )
            self._audit(db, data["id"], audit)

    def checkpoint(self):
        with self.connection() as db:
            row = db.execute("SELECT data FROM checkpoints WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def save_checkpoint(self, data, audit):
        with self.connection() as db:
            db.execute(
                "INSERT INTO checkpoints(id,data) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (json.dumps(data),),
            )
            self._audit(db, None, audit)

    def audit(self):
        with self.connection() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM audit ORDER BY seq DESC LIMIT 200").fetchall()
            return [{**dict(row), "details": json.loads(row["details"])} for row in rows]

    def record(self, incident_id, audit):
        with self.connection() as db:
            self._audit(db, incident_id, audit)

    @staticmethod
    def _audit(db, incident_id, audit):
        timestamp, action, actor, details = audit
        db.execute(
            "INSERT INTO audit(timestamp,incident_id,action,actor,details) VALUES(?,?,?,?,?)",
            (timestamp, incident_id, action, actor, json.dumps(details)),
        )
