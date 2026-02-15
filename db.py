import sqlite3
from pathlib import Path

DB_PATH = Path("leads.sqlite")


def init_db():
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
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO leads (name, phone, email, message, meta) VALUES (?, ?, ?, ?, ?)",
            (name, phone, email, message, meta),
        )
        con.commit()
