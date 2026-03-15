import sqlite3
from pathlib import Path

DEFAULT_SESSION_NAME = "默认"
AUTO_SESSION_PREFIX = "新会话"


class MemoryStore:
    def __init__(self, db_path: str = "data/memory.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, name)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_state (
                    chat_id TEXT PRIMARY KEY,
                    active_session_id INTEGER NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('system','user','assistant')),
                    content TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_session_messages_session_id_id ON session_messages(session_id, id)"
            )
            self._migrate_from_legacy_schema(conn)

    def _migrate_from_legacy_schema(self, conn: sqlite3.Connection) -> None:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchone()
        if not table:
            return
        old_count = conn.execute("SELECT COUNT(*) AS cnt FROM messages").fetchone()["cnt"]
        if old_count == 0:
            return
        rows = conn.execute("SELECT chat_id, role, content FROM messages ORDER BY id ASC").fetchall()
        for row in rows:
            chat_id = row["chat_id"]
            session_id = self._ensure_default_session(conn, chat_id)
            conn.execute(
                "INSERT INTO session_messages(session_id, role, content) VALUES (?, ?, ?)",
                (session_id, row["role"], row["content"]),
            )
        conn.execute("DELETE FROM messages")

    def _ensure_default_session(self, conn: sqlite3.Connection, chat_id: str) -> int:
        row = conn.execute(
            """
            SELECT s.id
            FROM chat_state st
            JOIN chat_sessions s ON s.id = st.active_session_id
            WHERE st.chat_id = ?
            """,
            (chat_id,),
        ).fetchone()
        if row:
            return int(row["id"])

        session = conn.execute(
            "SELECT id FROM chat_sessions WHERE chat_id = ? ORDER BY id ASC LIMIT 1",
            (chat_id,),
        ).fetchone()
        if session:
            session_id = int(session["id"])
        else:
            conn.execute(
                "INSERT INTO chat_sessions(chat_id, name) VALUES (?, ?)",
                (chat_id, DEFAULT_SESSION_NAME),
            )
            session_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            """
            INSERT INTO chat_state(chat_id, active_session_id, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id) DO UPDATE SET active_session_id=excluded.active_session_id, updated_at=CURRENT_TIMESTAMP
            """,
            (chat_id, session_id),
        )
        return session_id

    def _require_active_session(self, conn: sqlite3.Connection, chat_id: str) -> int:
        return self._ensure_default_session(conn, chat_id)

    def add_message(self, chat_id: str, role: str, content: str) -> None:
        with self._connect() as conn:
            session_id = self._require_active_session(conn, chat_id)
            conn.execute(
                "INSERT INTO session_messages(session_id, role, content) VALUES (?, ?, ?)",
                (session_id, role, content),
            )
            conn.execute(
                "UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (session_id,),
            )

    def get_recent_messages(self, chat_id: str, limit: int) -> list[dict[str, str]]:
        with self._connect() as conn:
            session_id = self._require_active_session(conn, chat_id)
            rows = conn.execute(
                """
                SELECT role, content
                FROM session_messages
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()

        ordered = list(reversed(rows))
        return [{"role": row["role"], "content": row["content"]} for row in ordered]

    def get_all_messages(self, chat_id: str) -> list[dict[str, str]]:
        with self._connect() as conn:
            session_id = self._require_active_session(conn, chat_id)
            rows = conn.execute(
                """
                SELECT role, content
                FROM session_messages
                WHERE session_id = ?
                ORDER BY id ASC
                """,
                (session_id,),
            ).fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in rows]

    def create_session(self, chat_id: str, name: str) -> dict[str, str | int]:
        clean = (name or "").strip() or "新会话"
        with self._connect() as conn:
            suffix = 1
            candidate = clean
            while conn.execute(
                "SELECT 1 FROM chat_sessions WHERE chat_id = ? AND name = ?",
                (chat_id, candidate),
            ).fetchone():
                suffix += 1
                candidate = f"{clean}-{suffix}"
            conn.execute(
                "INSERT INTO chat_sessions(chat_id, name) VALUES (?, ?)",
                (chat_id, candidate),
            )
            session_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            conn.execute(
                """
                INSERT INTO chat_state(chat_id, active_session_id, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id) DO UPDATE SET active_session_id=excluded.active_session_id, updated_at=CURRENT_TIMESTAMP
                """,
                (chat_id, session_id),
            )
            return {"id": session_id, "name": candidate}

    def auto_rename_active_session(self, chat_id: str, new_name: str) -> tuple[bool, str]:
        target = (new_name or "").strip()
        if not target:
            return False, ""
        with self._connect() as conn:
            session_id = self._require_active_session(conn, chat_id)
            row = conn.execute(
                "SELECT name FROM chat_sessions WHERE id = ? AND chat_id = ?",
                (session_id, chat_id),
            ).fetchone()
            if not row:
                return False, ""
            current_name = str(row["name"])
            if not current_name.startswith(AUTO_SESSION_PREFIX):
                return False, current_name
            conflict = conn.execute(
                "SELECT 1 FROM chat_sessions WHERE chat_id = ? AND name = ? AND id != ?",
                (chat_id, target, session_id),
            ).fetchone()
            if conflict:
                return False, current_name
            conn.execute(
                "UPDATE chat_sessions SET name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (target, session_id),
            )
            return True, target

    def list_sessions(self, chat_id: str, limit: int = 20) -> list[dict[str, str | int | bool]]:
        with self._connect() as conn:
            active_id = self._require_active_session(conn, chat_id)
            rows = conn.execute(
                """
                SELECT s.id, s.name, s.updated_at, s.summary, COUNT(m.id) AS message_count
                FROM chat_sessions s
                LEFT JOIN session_messages m ON m.session_id = s.id
                WHERE s.chat_id = ?
                GROUP BY s.id
                ORDER BY s.updated_at DESC, s.id DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
            result: list[dict[str, str | int | bool]] = []
            for row in rows:
                result.append(
                    {
                        "id": int(row["id"]),
                        "name": str(row["name"]),
                        "message_count": int(row["message_count"] or 0),
                        "summary": str(row["summary"] or ""),
                        "is_active": int(row["id"]) == active_id,
                    }
                )
            return result

    def switch_session(self, chat_id: str, session_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM chat_sessions WHERE id = ? AND chat_id = ?",
                (session_id, chat_id),
            ).fetchone()
            if not row:
                return False
            conn.execute(
                """
                INSERT INTO chat_state(chat_id, active_session_id, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id) DO UPDATE SET active_session_id=excluded.active_session_id, updated_at=CURRENT_TIMESTAMP
                """,
                (chat_id, session_id),
            )
            conn.execute("UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (session_id,))
            return True

    def get_active_session(self, chat_id: str) -> dict[str, str | int]:
        with self._connect() as conn:
            session_id = self._require_active_session(conn, chat_id)
            row = conn.execute(
                """
                SELECT s.id, s.name, s.summary, COUNT(m.id) AS message_count
                FROM chat_sessions s
                LEFT JOIN session_messages m ON m.session_id = s.id
                WHERE s.id = ?
                GROUP BY s.id
                """,
                (session_id,),
            ).fetchone()
            return {
                "id": int(row["id"]),
                "name": str(row["name"]),
                "summary": str(row["summary"] or ""),
                "message_count": int(row["message_count"] or 0),
            }

    def rename_active_session(self, chat_id: str, new_name: str) -> tuple[bool, str]:
        target = (new_name or "").strip()
        if not target:
            return False, "会话名不能为空。"
        with self._connect() as conn:
            session_id = self._require_active_session(conn, chat_id)
            conflict = conn.execute(
                "SELECT 1 FROM chat_sessions WHERE chat_id = ? AND name = ? AND id != ?",
                (chat_id, target, session_id),
            ).fetchone()
            if conflict:
                return False, "会话名已存在，请换一个名字。"
            conn.execute(
                "UPDATE chat_sessions SET name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (target, session_id),
            )
            return True, target

    def delete_session_by_id(self, chat_id: str, session_id: int) -> tuple[bool, str, str | None]:
        with self._connect() as conn:
            target = conn.execute(
                "SELECT id, name FROM chat_sessions WHERE chat_id = ? AND id = ?",
                (chat_id, session_id),
            ).fetchone()
            if not target:
                return False, "会话不存在。", None

            target_id = int(target["id"])
            target_name = str(target["name"])
            active_id = self._require_active_session(conn, chat_id)
            deleting_active = target_id == active_id

            conn.execute("DELETE FROM session_messages WHERE session_id = ?", (target_id,))
            conn.execute("DELETE FROM chat_sessions WHERE id = ?", (target_id,))

            new_active_name: str | None = None
            if deleting_active:
                candidate = self._next_new_session_name(conn, chat_id, avoid_name=target_name)

                conn.execute(
                    "INSERT INTO chat_sessions(chat_id, name) VALUES (?, ?)",
                    (chat_id, candidate),
                )
                next_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
                conn.execute(
                    """
                    INSERT INTO chat_state(chat_id, active_session_id, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(chat_id) DO UPDATE SET active_session_id=excluded.active_session_id, updated_at=CURRENT_TIMESTAMP
                    """,
                    (chat_id, next_id),
                )
                new_active_name = candidate

            return True, target_name, new_active_name

    def _next_new_session_name(self, conn: sqlite3.Connection, chat_id: str, avoid_name: str | None = None) -> str:
        base_name = "新会话"
        suffix = 1
        candidate = base_name
        while True:
            exists = conn.execute(
                "SELECT 1 FROM chat_sessions WHERE chat_id = ? AND name = ?",
                (chat_id, candidate),
            ).fetchone()
            if not exists and (not avoid_name or candidate != avoid_name):
                return candidate
            suffix += 1
            candidate = f"{base_name}-{suffix}"

    def update_active_session_summary(self, chat_id: str, summary: str) -> None:
        with self._connect() as conn:
            session_id = self._require_active_session(conn, chat_id)
            conn.execute(
                "UPDATE chat_sessions SET summary = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (summary.strip(), session_id),
            )
