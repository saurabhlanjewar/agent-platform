"""Encrypted local POC state, including server-side browser sessions."""
import json
import os
import sqlite3
from pathlib import Path

from cryptography.fernet import Fernet


class Vault:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = directory / "vault.key"
        if not key.exists():
            fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(Fernet.generate_key())
        self.cipher = Fernet(key.read_bytes())
        self.path = directory / "platform.sqlite3"
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS secrets (id TEXT PRIMARY KEY, value BLOB)")
        self.path.chmod(0o600)

    def put(self, key, value):
        encrypted = self.cipher.encrypt(json.dumps(value).encode())
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT OR REPLACE INTO secrets VALUES (?, ?)", (key, encrypted))

    def get(self, key, default=None):
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT value FROM secrets WHERE id = ?", (key,)).fetchone()
        return json.loads(self.cipher.decrypt(row[0])) if row else default

    def delete(self, key):
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM secrets WHERE id = ?", (key,))
