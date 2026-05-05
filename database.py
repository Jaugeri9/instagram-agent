import sqlite3
from contextlib import contextmanager

DB_PATH = "instagram_agent.db"


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                user_id TEXT,
                username TEXT,
                content TEXT,
                media_id TEXT,
                comment_id TEXT,
                response TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            INSERT OR IGNORE INTO settings (key, value) VALUES
                ('auto_reply_comments', 'true'),
                ('auto_dm_followers', 'false'),
                ('persona', 'You are a friendly and engaging social media assistant. Keep responses brief, warm, and authentic. Sound like a real person, not a bot. Never use generic filler phrases.'),
                ('active', 'true');
        """)


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_setting(key: str) -> str:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


def add_event(type: str, user_id: str, username: str, content: str = None,
              media_id: str = None, comment_id: str = None) -> int:
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO events (type, user_id, username, content, media_id, comment_id) VALUES (?, ?, ?, ?, ?, ?)",
            (type, user_id, username, content, media_id, comment_id)
        )
        return cursor.lastrowid


def update_event(event_id: int, response: str, status: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE events SET response = ?, status = ? WHERE id = ?",
            (response, status, event_id)
        )


def get_events(limit: int = 100) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


def get_settings_all() -> dict:
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: row["value"] for row in rows}


def get_stats() -> dict:
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) as c FROM events").fetchone()["c"]
        comments = conn.execute("SELECT COUNT(*) as c FROM events WHERE type = 'comment'").fetchone()["c"]
        follows = conn.execute("SELECT COUNT(*) as c FROM events WHERE type = 'follow'").fetchone()["c"]
        replied = conn.execute("SELECT COUNT(*) as c FROM events WHERE status = 'replied'").fetchone()["c"]
        return {"total": total, "comments": comments, "follows": follows, "replied": replied}
