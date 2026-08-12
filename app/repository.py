"""任务、录制文件和面经文档的持久化仓储。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from app.db import Database
from app.services.semantic import extract_question_outline


class Repository:
    """将 SQL 细节集中在此处，业务层只处理领域数据。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create_recording(
        self, source_path: Path, import_kind: str, managed_path: Path | None = None
    ) -> dict[str, Any]:
        """登记录制文件；重复导入时返回已有记录。"""
        resolved_source = str(source_path.resolve())
        with self.database.connection() as connection:
            existing = connection.execute(
                "SELECT * FROM recordings WHERE source_path = ?", (resolved_source,)
            ).fetchone()
            if existing:
                return dict(existing)
            recording_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO recordings(id, source_path, managed_path, import_kind, original_name)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    recording_id,
                    resolved_source,
                    str(managed_path.resolve()) if managed_path else None,
                    import_kind,
                    source_path.name,
                ),
            )
            return dict(
                connection.execute(
                    "SELECT * FROM recordings WHERE id = ?", (recording_id,)
                ).fetchone()
            )

    def create_task(
        self, recording_id: str, options: TaskOptions, work_dir: Path
    ) -> dict[str, Any]:
        """创建排队中的任务。"""
        task_id = str(uuid.uuid4())
        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO tasks(
                    id, recording_id, language, transcription_model, semantic_provider,
                    semantic_model, stage, work_dir
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)
                """,
                (
                    task_id,
                    recording_id,
                    options.language,
                    options.transcription_model,
                    options.semantic_provider,
                    options.semantic_model,
                    str(work_dir),
                ),
            )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        """读取任务以及关联录制文件信息。"""
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT tasks.*, recordings.source_path, recordings.managed_path,
                    recordings.original_name, recordings.import_kind
                FROM tasks JOIN recordings ON recordings.id = tasks.recording_id
                WHERE tasks.id = ?
                """,
                (task_id,),
            ).fetchone()
        if not row:
            raise KeyError(f"任务不存在: {task_id}")
        return dict(row)

    def list_tasks(self) -> list[dict[str, Any]]:
        """按创建时间倒序返回任务列表。"""
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT tasks.*, recordings.original_name
                FROM tasks JOIN recordings ON recordings.id = tasks.recording_id
                ORDER BY tasks.created_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def list_queued_tasks(self) -> list[dict[str, Any]]:
        """返回等待 worker 调度的任务。"""
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT id FROM tasks WHERE stage = 'queued' ORDER BY created_at"
            ).fetchall()
        return [self.get_task(row["id"]) for row in rows]

    def update_task(
        self,
        task_id: str,
        stage: str,
        error_message: str | None = None,
        retry_stage: str | None = None,
    ) -> dict[str, Any]:
        """更新任务阶段和错误信息。"""
        with self.database.connection() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET stage = ?, error_message = ?, retry_stage = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (stage, error_message, retry_stage, task_id),
            )
        return self.get_task(task_id)

    def reset_task(self, task_id: str, options: TaskOptions | None = None) -> dict[str, Any]:
        """将失败任务放回队列，并可覆盖模型配置。"""
        task = self.get_task(task_id)
        with self.database.connection() as connection:
            if options:
                connection.execute(
                    """
                    UPDATE tasks SET language = ?, transcription_model = ?,
                        semantic_provider = ?, semantic_model = ?
                    WHERE id = ?
                    """,
                    (
                        options.language,
                        options.transcription_model,
                        options.semantic_provider,
                        options.semantic_model,
                        task_id,
                    ),
                )
            connection.execute(
                """
                UPDATE tasks
                SET stage = 'queued', error_message = NULL, retry_stage = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (task_id,),
            )
        return self.get_task(task_id)

    def save_record(
        self,
        task_id: str,
        markdown_content: str,
        raw_segments: list[dict[str, Any]],
        content_header: str = "",
        original_name: str = "",
    ) -> dict[str, Any]:
        """创建或覆盖任务对应的面经文档。"""
        record_id = str(uuid.uuid4())
        raw_json = json.dumps(raw_segments, ensure_ascii=False)
        question_outline = extract_question_outline(markdown_content)
        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO records(
                    id, task_id, original_name, content_header, markdown_content, question_outline,
                    raw_segments_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    original_name = excluded.original_name,
                    markdown_content = excluded.markdown_content,
                    question_outline = excluded.question_outline,
                    raw_segments_json = excluded.raw_segments_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    record_id,
                    task_id,
                    original_name,
                    content_header,
                    markdown_content,
                    question_outline,
                    raw_json,
                ),
            )
        return self.get_record_by_task(task_id)

    def get_record(self, record_id: str) -> dict[str, Any]:
        """读取单篇面经及其来源任务。"""
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT records.*, tasks.stage
                FROM records
                LEFT JOIN tasks ON tasks.id = records.task_id
                WHERE records.id = ?
                """,
                (record_id,),
            ).fetchone()
        if not row:
            raise KeyError(f"文档不存在: {record_id}")
        return self._record_to_dict(row)

    def get_record_by_task(self, task_id: str) -> dict[str, Any]:
        """通过任务查询其面经文档。"""
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM records WHERE task_id = ?", (task_id,)
            ).fetchone()
        if not row:
            raise KeyError(f"任务尚未生成文档: {task_id}")
        return self._record_to_dict(row)

    def list_records(self, header_query: str = "") -> list[dict[str, Any]]:
        """仅根据用户内容头匹配记录，不对正文建立搜索。"""
        query = f"%{header_query.strip()}%"
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT records.id, records.task_id, records.content_header,
                    records.created_at, records.updated_at, records.original_name
                FROM records
                WHERE records.content_header LIKE ?
                ORDER BY records.updated_at DESC
                """,
                (query,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_record(
        self, record_id: str, content_header: str, markdown_content: str
    ) -> dict[str, Any]:
        """保存用户编辑的内容头与 Markdown 正文。"""
        question_outline = extract_question_outline(markdown_content)
        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE records
                SET content_header = ?, markdown_content = ?, question_outline = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (content_header, markdown_content, question_outline, record_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"文档不存在: {record_id}")
        return self.get_record(record_id)

    def delete_record(self, record_id: str) -> None:
        """删除面经文档；关联任务一并删除。"""
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT task_id FROM records WHERE id = ?", (record_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"文档不存在: {record_id}")
            connection.execute("DELETE FROM records WHERE id = ?", (record_id,))
            connection.execute("DELETE FROM tasks WHERE id = ?", (row["task_id"],))

    def delete_task(self, task_id: str) -> None:
        """删除队列任务；已生成的面经文档保留。"""
        self.get_task(task_id)
        with self.database.connection() as connection:
            connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    @staticmethod
    def _record_to_dict(row: Any) -> dict[str, Any]:
        """将 JSON 原始片段还原为 API 可序列化数据。"""
        record = dict(row)
        record["raw_segments"] = json.loads(record.pop("raw_segments_json"))
        return record
