"""SQLite 数据库访问与建表逻辑。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS recordings (
    id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    managed_path TEXT,
    import_kind TEXT NOT NULL CHECK(import_kind IN ('scan', 'upload')),
    original_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_path)
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    recording_id TEXT NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    language TEXT NOT NULL CHECK(language IN ('auto', 'zh', 'en')),
    transcription_model TEXT NOT NULL,
    semantic_provider TEXT NOT NULL CHECK(semantic_provider IN ('ollama', 'openai')),
    semantic_model TEXT NOT NULL,
    stage TEXT NOT NULL CHECK(stage IN (
        'queued', 'extracting', 'transcribing', 'organizing', 'completed', 'failed', 'cancelled'
    )),
    retry_stage TEXT,
    error_message TEXT,
    work_dir TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE,
    original_name TEXT NOT NULL DEFAULT '',
    content_header TEXT NOT NULL DEFAULT '',
    markdown_content TEXT NOT NULL,
    question_outline TEXT NOT NULL DEFAULT '',
    raw_segments_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tasks_stage ON tasks(stage, created_at);
CREATE INDEX IF NOT EXISTS idx_records_header ON records(content_header);
"""


class Database:
    """为每次操作创建独立连接，避免 worker 线程跨线程复用 SQLite 连接。"""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    @contextmanager
    def connection(self) -> Iterable[sqlite3.Connection]:
        """获取启用外键和 WAL 的 SQLite 连接。"""
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """初始化数据库结构，并迁移旧库以支持取消状态。"""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.executescript(SCHEMA)
            self._migrate_task_cancelled_stage(connection)
            self._migrate_records_question_outline(connection)
            self._migrate_records_keep_on_task_delete(connection)

    @staticmethod
    def _migrate_records_question_outline(connection: sqlite3.Connection) -> None:
        """为已有 records 表补充 question_outline 列。"""
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(records)").fetchall()
        }
        if "question_outline" not in columns:
            connection.execute(
                "ALTER TABLE records ADD COLUMN question_outline TEXT NOT NULL DEFAULT ''"
            )

    @staticmethod
    def _migrate_records_keep_on_task_delete(connection: sqlite3.Connection) -> None:
        """重建 records 表：删除任务不再级联删除面经，并在记录上保存来源文件名。"""
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='records'"
        ).fetchone()
        table_sql = (row["sql"] or "") if row else ""
        columns = {
            col["name"]
            for col in connection.execute("PRAGMA table_info(records)").fetchall()
        }
        if "ON DELETE CASCADE" not in table_sql and "original_name" in columns:
            return
        connection.executescript(
            """
            PRAGMA foreign_keys = OFF;
            CREATE TABLE records_new (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL UNIQUE,
                original_name TEXT NOT NULL DEFAULT '',
                content_header TEXT NOT NULL DEFAULT '',
                markdown_content TEXT NOT NULL,
                question_outline TEXT NOT NULL DEFAULT '',
                raw_segments_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO records_new (
                id, task_id, original_name, content_header, markdown_content,
                question_outline, raw_segments_json, created_at, updated_at
            )
            SELECT records.id, records.task_id,
                COALESCE(recordings.original_name, ''),
                records.content_header, records.markdown_content,
                records.question_outline, records.raw_segments_json,
                records.created_at, records.updated_at
            FROM records
            LEFT JOIN tasks ON tasks.id = records.task_id
            LEFT JOIN recordings ON recordings.id = tasks.recording_id;
            DROP TABLE records;
            ALTER TABLE records_new RENAME TO records;
            CREATE INDEX IF NOT EXISTS idx_records_header ON records(content_header);
            PRAGMA foreign_keys = ON;
            """
        )

    @staticmethod
    def _migrate_task_cancelled_stage(connection: sqlite3.Connection) -> None:
        """为已有 tasks 表补充 cancelled 状态约束。"""
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()
        if not row or not row["sql"] or "cancelled" in row["sql"]:
            return
        connection.executescript(
            """
            PRAGMA foreign_keys = OFF;
            CREATE TABLE tasks_new (
                id TEXT PRIMARY KEY,
                recording_id TEXT NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
                language TEXT NOT NULL CHECK(language IN ('auto', 'zh', 'en')),
                transcription_model TEXT NOT NULL,
                semantic_provider TEXT NOT NULL CHECK(semantic_provider IN ('ollama', 'openai')),
                semantic_model TEXT NOT NULL,
                stage TEXT NOT NULL CHECK(stage IN (
                    'queued', 'extracting', 'transcribing', 'organizing',
                    'completed', 'failed', 'cancelled'
                )),
                retry_stage TEXT,
                error_message TEXT,
                work_dir TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO tasks_new
            SELECT id, recording_id, language, transcription_model, semantic_provider,
                semantic_model, stage, retry_stage, error_message, work_dir,
                created_at, updated_at
            FROM tasks;
            DROP TABLE tasks;
            ALTER TABLE tasks_new RENAME TO tasks;
            CREATE INDEX IF NOT EXISTS idx_tasks_stage ON tasks(stage, created_at);
            PRAGMA foreign_keys = ON;
            """
        )

    def get_setting(self, key: str, default: Any = None) -> Any:
        """读取 JSON 编码的设置值。"""
        with self.connection() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        """写入 JSON 编码的设置值。"""
        encoded = json.dumps(value, ensure_ascii=False)
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO settings(key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (key, encoded),
            )
