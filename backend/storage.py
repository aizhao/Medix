"""Durable local persistence for documents, ingestion jobs and conversations."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class SQLiteStore:
    def __init__(self, path: Optional[str] = None):
        self.path = Path(path or os.getenv("MEDIX_DB_PATH", "data/medix.db"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _init_schema(self) -> None:
        with self._lock, self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '新会话',
                owner_id TEXT NOT NULL DEFAULT 'anonymous',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id)
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL,
                path TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'uploaded',
                category TEXT NOT NULL DEFAULT 'unclassified', tags_json TEXT NOT NULL DEFAULT '[]',
                source_type TEXT NOT NULL DEFAULT 'internal', authority_level TEXT NOT NULL DEFAULT 'unknown', mime_type TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '', publisher TEXT NOT NULL DEFAULT '', license TEXT NOT NULL DEFAULT '',
                external_id TEXT NOT NULL DEFAULT '', corpus_id TEXT NOT NULL DEFAULT '', language TEXT NOT NULL DEFAULT '', published_at TEXT NOT NULL DEFAULT '',
                size_bytes INTEGER NOT NULL DEFAULT 0, sha256 TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1, progress INTEGER NOT NULL DEFAULT 0,
                current_stage TEXT NOT NULL DEFAULT 'uploaded', total_chunks INTEGER NOT NULL DEFAULT 0,
                indexed_chunks INTEGER NOT NULL DEFAULT 0, error_code TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '', retry_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
            CREATE TABLE IF NOT EXISTS ingest_jobs (
                id TEXT PRIMARY KEY, document_id INTEGER NOT NULL, operation TEXT NOT NULL,
                status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
                stage TEXT NOT NULL DEFAULT 'queued', error_code TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                FOREIGN KEY(document_id) REFERENCES documents(id)
            );
            CREATE INDEX IF NOT EXISTS idx_ingest_jobs_document ON ingest_jobs(document_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS document_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL, content TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'parsed',
                created_at TEXT NOT NULL, FOREIGN KEY(document_id) REFERENCES documents(id),
                UNIQUE(document_id, chunk_index)
            );
            CREATE INDEX IF NOT EXISTS idx_chunks_document ON document_chunks(document_id, chunk_index);
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                memory_type TEXT NOT NULL DEFAULT 'fact',
                content TEXT NOT NULL,
                source_session_id TEXT NOT NULL DEFAULT '',
                source_message_id INTEGER,
                confidence REAL NOT NULL DEFAULT 0.5,
                status TEXT NOT NULL DEFAULT 'active',
                version INTEGER NOT NULL DEFAULT 1,
                valid_from TEXT NOT NULL,
                expires_at TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_memories_user_status ON memories(user_id, status);
            CREATE INDEX IF NOT EXISTS idx_memories_source_session ON memories(source_session_id);
            """)
            # Migrate databases created by the initial MVP.
            columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
            if "metadata_json" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}' ")
            columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
            if "owner_id" not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'anonymous'")
            columns = {row[1] for row in db.execute("PRAGMA table_info(documents)")}
            migrations = {
                "category": "TEXT NOT NULL DEFAULT 'unclassified'", "tags_json": "TEXT NOT NULL DEFAULT '[]'",
                "source_type": "TEXT NOT NULL DEFAULT 'internal'", "authority_level": "TEXT NOT NULL DEFAULT 'unknown'", "mime_type": "TEXT NOT NULL DEFAULT ''",
                "source_url": "TEXT NOT NULL DEFAULT ''", "publisher": "TEXT NOT NULL DEFAULT ''", "license": "TEXT NOT NULL DEFAULT ''",
                "external_id": "TEXT NOT NULL DEFAULT ''", "corpus_id": "TEXT NOT NULL DEFAULT ''", "language": "TEXT NOT NULL DEFAULT ''", "published_at": "TEXT NOT NULL DEFAULT ''",
                "size_bytes": "INTEGER NOT NULL DEFAULT 0", "sha256": "TEXT NOT NULL DEFAULT ''",
                "version": "INTEGER NOT NULL DEFAULT 1", "progress": "INTEGER NOT NULL DEFAULT 0",
                "current_stage": "TEXT NOT NULL DEFAULT 'uploaded'", "total_chunks": "INTEGER NOT NULL DEFAULT 0",
                "indexed_chunks": "INTEGER NOT NULL DEFAULT 0", "error_code": "TEXT NOT NULL DEFAULT ''",
                "error_message": "TEXT NOT NULL DEFAULT ''", "retry_count": "INTEGER NOT NULL DEFAULT 0",
                "updated_at": "TEXT NOT NULL DEFAULT ''", "started_at": "TEXT", "finished_at": "TEXT",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE documents ADD COLUMN {name} {definition}")
            db.execute("UPDATE documents SET updated_at=created_at WHERE updated_at='' OR updated_at IS NULL")

    def ensure_session(self, session_id: str, title: str = "新会话", owner_id: str = "anonymous") -> None:
        now = self._now()
        with self._lock, self._connect() as db:
            db.execute("INSERT OR IGNORE INTO sessions(id,title,owner_id,created_at,updated_at) VALUES(?,?,?,?,?)", (session_id, title, owner_id, now, now))
            db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))

    def append_message(self, session_id: str, role: str, content: str, metadata: Optional[Dict[str, Any]] = None, owner_id: str = "anonymous") -> None:
        self.ensure_session(session_id, content[:30] if role == "user" else "新会话", owner_id=owner_id)
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO messages(session_id,role,content,metadata_json,created_at) VALUES(?,?,?,?,?)", (session_id, role, content, json.dumps(metadata or {}, ensure_ascii=False), self._now()))

    def add_memory(
        self,
        user_id: str,
        content: str,
        memory_type: str = "fact",
        source_session_id: str = "",
        source_message_id: Optional[int] = None,
        confidence: float = 0.5,
        expires_at: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create a user-scoped memory record."""
        now = self._now()
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "INSERT INTO memories(user_id,memory_type,content,source_session_id,source_message_id,confidence,status,version,valid_from,expires_at,metadata_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(user_id), str(memory_type), str(content), str(source_session_id), source_message_id,
                 max(0.0, min(1.0, float(confidence))), "active", 1, now, expires_at,
                 json.dumps(metadata or {}, ensure_ascii=False), now, now),
            )
            row = db.execute("SELECT * FROM memories WHERE id=?", (cursor.lastrowid,)).fetchone()
            return self._memory_dict(row)

    def list_memories(self, user_id: str, status: Optional[str] = "active") -> List[Dict[str, Any]]:
        query = "SELECT * FROM memories WHERE user_id=?"
        params: List[Any] = [str(user_id)]
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY updated_at DESC, id DESC"
        with self._connect() as db:
            return [self._memory_dict(row) for row in db.execute(query, params).fetchall()]

    def update_memory(self, memory_id: int, user_id: str, **changes: Any) -> Optional[Dict[str, Any]]:
        allowed = {"content", "memory_type", "confidence", "status", "expires_at", "metadata_json"}
        values = {key: value for key, value in changes.items() if key in allowed}
        if not values:
            return self.get_memory(memory_id, user_id)
        values["version"] = "version + 1"
        values["updated_at"] = self._now()
        assignments = []
        params: List[Any] = []
        for key, value in values.items():
            if key == "version":
                assignments.append("version=version+1")
            else:
                assignments.append(f"{key}=?")
                params.append(value)
        params.extend([int(memory_id), str(user_id)])
        with self._lock, self._connect() as db:
            db.execute(f"UPDATE memories SET {', '.join(assignments)} WHERE id=? AND user_id=?", params)
            row = db.execute("SELECT * FROM memories WHERE id=? AND user_id=?", (int(memory_id), str(user_id))).fetchone()
            return self._memory_dict(row) if row else None

    def get_memory(self, memory_id: int, user_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM memories WHERE id=? AND user_id=?", (int(memory_id), str(user_id))).fetchone()
            return self._memory_dict(row) if row else None

    def delete_memory(self, memory_id: int, user_id: str) -> bool:
        return self.update_memory(memory_id, user_id, status="deleted") is not None

    @staticmethod
    def _memory_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        data = dict(row)
        try:
            data["metadata"] = json.loads(data.pop("metadata_json", "{}") or "{}")
        except json.JSONDecodeError:
            data["metadata"] = {}
        return data

    def get_messages(self, session_id: str) -> Optional[List[Dict[str, Any]]]:
        with self._connect() as db:
            if db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone() is None:
                return None
            rows = db.execute("SELECT role,content,metadata_json,created_at FROM messages WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
            return [{"role": row["role"], "content": row["content"], "metadata": json.loads(row["metadata_json"] or "{}"), "created_at": row["created_at"]} for row in rows]

    def list_sessions(self) -> List[Dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT id,title,created_at,updated_at FROM sessions ORDER BY updated_at DESC")]

    def rename_session(self, session_id: str, title: str) -> bool:
        title = title.strip()[:80]
        if not title:
            return False
        with self._lock, self._connect() as db:
            cursor = db.execute("UPDATE sessions SET title=?,updated_at=? WHERE id=?", (title, self._now(), session_id))
            return cursor.rowcount > 0

    def delete_session(self, session_id: str) -> bool:
        with self._lock, self._connect() as db:
            if db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone() is None:
                return False
            db.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            return True

    def add_document(self, filename: str, path: str, status: str = "uploaded", **metadata: Any) -> Dict[str, Any]:
        now = self._now()
        values = (
            filename, path, status, metadata.get("category", "unclassified"),
            json.dumps(metadata.get("tags", []), ensure_ascii=False),
            metadata.get("source_type", "internal"), metadata.get("authority_level", "unknown"),
            metadata.get("mime_type", ""), metadata.get("source_url", ""), metadata.get("publisher", ""),
            metadata.get("license", ""), metadata.get("external_id", ""), metadata.get("corpus_id", ""),
            metadata.get("language", ""), metadata.get("published_at", ""),
            int(metadata.get("size_bytes", 0) or 0), metadata.get("sha256", ""), now, now, status,
        )
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "INSERT INTO documents(filename,path,status,category,tags_json,source_type,authority_level,mime_type,source_url,publisher,license,external_id,corpus_id,language,published_at,size_bytes,sha256,created_at,updated_at,current_stage) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            row = db.execute("SELECT * FROM documents WHERE id=?", (cursor.lastrowid,)).fetchone()
            return self._document_dict(row)

    def get_document(self, document_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
            return self._document_dict(row) if row else None

    def list_documents(self) -> List[Dict[str, Any]]:
        with self._connect() as db:
            return [self._document_dict(row) for row in db.execute("SELECT * FROM documents ORDER BY id DESC")]

    def find_document_by_sha256(self, sha256: str) -> Optional[Dict[str, Any]]:
        if not sha256:
            return None
        with self._connect() as db:
            row = db.execute("SELECT * FROM documents WHERE sha256=? ORDER BY id DESC LIMIT 1", (sha256,)).fetchone()
            return self._document_dict(row) if row else None

    def find_document_by_path(self, path: str) -> Optional[Dict[str, Any]]:
        """Find the latest registered document for a stable batch-import path."""
        with self._connect() as db:
            row = db.execute("SELECT * FROM documents WHERE path=? ORDER BY id DESC LIMIT 1", (path,)).fetchone()
            return self._document_dict(row) if row else None

    @staticmethod
    def _document_dict(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        try:
            data["tags"] = json.loads(data.pop("tags_json", "[]") or "[]")
        except json.JSONDecodeError:
            data["tags"] = []
        return data

    def update_document(self, document_id: int, **changes: Any) -> bool:
        allowed = {"status", "category", "tags_json", "source_type", "authority_level", "source_url", "publisher", "license", "external_id", "corpus_id", "language", "published_at", "mime_type", "size_bytes", "sha256", "version", "progress", "current_stage", "total_chunks", "indexed_chunks", "error_code", "error_message", "retry_count", "started_at", "finished_at", "updated_at"}
        values = {key: value for key, value in changes.items() if key in allowed}
        if not values:
            return False
        values.setdefault("updated_at", self._now())
        assignments = ", ".join(f"{key}=?" for key in values)
        with self._lock, self._connect() as db:
            cursor = db.execute(f"UPDATE documents SET {assignments} WHERE id=?", (*values.values(), document_id))
            return cursor.rowcount > 0

    def update_document_status(self, document_id: int, status: str, **changes: Any) -> bool:
        changes["status"] = status
        changes.setdefault("current_stage", status)
        return self.update_document(document_id, **changes)

    def create_job(self, document_id: int, operation: str) -> str:
        import uuid
        job_id, now = str(uuid.uuid4()), self._now()
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO ingest_jobs(id,document_id,operation,status,created_at,updated_at) VALUES(?,?,?,?,?,?)", (job_id, document_id, operation, "queued", now, now))
        return job_id

    def update_job(self, job_id: str, **changes: Any) -> bool:
        allowed = {"status", "progress", "stage", "error_code", "error_message", "started_at", "finished_at"}
        values = {key: value for key, value in changes.items() if key in allowed}
        if not values:
            return False
        values["updated_at"] = self._now()
        assignments = ", ".join(f"{key}=?" for key in values)
        with self._lock, self._connect() as db:
            cursor = db.execute(f"UPDATE ingest_jobs SET {assignments} WHERE id=?", (*values.values(), job_id))
            return cursor.rowcount > 0

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM ingest_jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def list_jobs(self, document_id: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._connect() as db:
            query = "SELECT * FROM ingest_jobs ORDER BY created_at DESC"
            params: tuple = ()
            if document_id is not None:
                query = "SELECT * FROM ingest_jobs WHERE document_id=? ORDER BY created_at DESC"
                params = (document_id,)
            return [dict(row) for row in db.execute(query, params)]

    def replace_chunks(self, document_id: int, chunks: List[Dict[str, Any]]) -> None:
        now = self._now()
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM document_chunks WHERE document_id=?", (document_id,))
            for index, chunk in enumerate(chunks):
                db.execute("INSERT INTO document_chunks(document_id,chunk_index,content,metadata_json,status,created_at) VALUES(?,?,?,?,?,?)", (document_id, index, str(chunk.get("content", "")), json.dumps(chunk.get("metadata", {}), ensure_ascii=False), "parsed", now))

    def list_chunks(self, document_id: int) -> List[Dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM document_chunks WHERE document_id=? ORDER BY chunk_index", (document_id,)).fetchall()
            return [dict(row, metadata=json.loads(row["metadata_json"] or "{}")) for row in rows]

    def search_chunks(self, document_id: int, query: str = "", page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        page = max(1, int(page))
        page_size = min(100, max(1, int(page_size)))
        query = query.strip()
        where = "document_id=?"
        params: List[Any] = [document_id]
        if query:
            where += " AND (content LIKE ? OR metadata_json LIKE ?)"
            pattern = f"%{query}%"
            params.extend([pattern, pattern])
        with self._connect() as db:
            total = int(db.execute(f"SELECT COUNT(*) FROM document_chunks WHERE {where}", params).fetchone()[0])
            rows = db.execute(
                f"SELECT * FROM document_chunks WHERE {where} ORDER BY chunk_index LIMIT ? OFFSET ?",
                (*params, page_size, (page - 1) * page_size),
            ).fetchall()
        return {
            "items": [dict(row, metadata=json.loads(row["metadata_json"] or "{}")) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": max(1, (total + page_size - 1) // page_size),
        }

    def delete_document(self, document_id: int) -> bool:
        with self._lock, self._connect() as db:
            if db.execute("SELECT 1 FROM documents WHERE id=?", (document_id,)).fetchone() is None:
                return False
            db.execute("DELETE FROM document_chunks WHERE document_id=?", (document_id,))
            db.execute("DELETE FROM ingest_jobs WHERE document_id=?", (document_id,))
            db.execute("DELETE FROM documents WHERE id=?", (document_id,))
            return True

    def document_stats(self) -> Dict[str, int]:
        with self._connect() as db:
            stats = {str(row["status"]): int(row["count"]) for row in db.execute("SELECT status,COUNT(*) AS count FROM documents GROUP BY status")}
            stats["total"] = sum(stats.values())
            return stats

    def recover_incomplete_jobs(self) -> int:
        now = self._now()
        with self._lock, self._connect() as db:
            cursor = db.execute("UPDATE ingest_jobs SET status='interrupted',error_code='service_restarted',error_message='服务重启，中断任务可重试',updated_at=?,finished_at=? WHERE status IN ('queued','running')", (now, now))
            db.execute("UPDATE documents SET status='interrupted',current_stage='interrupted',error_code='service_restarted',error_message='服务重启，中断任务可重试',updated_at=? WHERE status IN ('processing','indexing')", (now,))
            return cursor.rowcount
