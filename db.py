import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.getenv("LEADS_DB_PATH", "leads.sqlite"))


def _ensure_db_dir():
    parent = DB_PATH.parent
    if str(parent) and str(parent) != ".":
        parent.mkdir(parents=True, exist_ok=True)


def init_db():
    _ensure_db_dir()
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT (datetime('now')),
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            email TEXT NOT NULL,
            message TEXT NOT NULL,
            meta TEXT
        );
        """
        )
        con.commit()


def insert_lead(name, phone, email, message, meta="{}"):
    _ensure_db_dir()
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO leads (name, phone, email, message, meta) VALUES (?, ?, ?, ?, ?)",
            (name, phone, email, message, meta),
        )
        con.commit()
