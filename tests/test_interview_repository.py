"""本地面经平台的 SQLite 仓储测试。"""

from pathlib import Path

from app.db import Database
from app.repository import Repository
from app.schemas import TaskOptions


def test_record_lifecycle_and_header_search(tmp_path: Path) -> None:
    """任务完成后应保存文档，并且仅从内容头中进行匹配。"""
    database = Database(tmp_path / "interviews.sqlite3")
    database.initialize()
    repository = Repository(database)
    source = tmp_path / "obs-recording.mkv"
    source.touch()

    recording = repository.create_recording(source, "scan")
    task = repository.create_task(
        recording["id"], TaskOptions(), tmp_path / "work" / "task"
    )
    repository.update_task(task["id"], "organizing")
    saved = repository.save_record(
        task["id"],
        "## 自我介绍\n\n**面试官**：请介绍自己。",
        [{"speaker": "面试官", "start_ms": 0, "end_ms": 1000, "text": "请介绍自己。"}],
    )

    repository.update_record(
        saved["id"],
        "字节跳动 后端一面",
        "## 自我介绍\n\n更新后的正文不应该参与内容头搜索。",
    )
    assert len(repository.list_records("字节")) == 1
    assert repository.list_records("正文") == []
    record = repository.get_record(saved["id"])
    assert record["raw_segments"][0]["speaker"] == "面试官"
    assert record["markdown_content"].startswith("## 自我介绍")
    assert record["question_outline"] == ""

    repository.delete_record(saved["id"])
    assert repository.list_records() == []
    try:
        repository.get_task(task["id"])
        assert False, "关联任务应一并删除"
    except KeyError:
        pass


def test_delete_task_keeps_record(tmp_path: Path) -> None:
    """删除队列任务不应删除已完成的面经文档。"""
    database = Database(tmp_path / "interviews.sqlite3")
    database.initialize()
    repository = Repository(database)
    source = tmp_path / "recording.mkv"
    source.touch()
    recording = repository.create_recording(source, "scan")
    task = repository.create_task(
        recording["id"], TaskOptions(), tmp_path / "work" / "task"
    )
    repository.update_task(task["id"], "organizing")
    saved = repository.save_record(
        task["id"], "## 题目\n\n正文", [], original_name="recording.mkv"
    )
    repository.update_task(task["id"], "completed")
    repository.delete_task(task["id"])
    assert repository.list_records() == [
        {
            "id": saved["id"],
            "task_id": task["id"],
            "content_header": "",
            "created_at": saved["created_at"],
            "updated_at": saved["updated_at"],
            "original_name": "recording.mkv",
        }
    ]
    record = repository.get_record(saved["id"])
    assert record["markdown_content"] == "## 题目\n\n正文"
    assert record["original_name"] == "recording.mkv"
    try:
        repository.get_task(task["id"])
        assert False, "任务应已删除"
    except KeyError:
        pass


def test_old_schema_migration_preserves_records_and_filenames(tmp_path: Path) -> None:
    """旧库（任务删除会级联删面经）初始化后应迁移，且文件名回填到记录上。"""
    import sqlite3

    db_path = tmp_path / "interviews.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE recordings (
            id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL,
            managed_path TEXT,
            import_kind TEXT NOT NULL,
            original_name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_path)
        );
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            recording_id TEXT NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
            language TEXT NOT NULL,
            transcription_model TEXT NOT NULL,
            semantic_provider TEXT NOT NULL,
            semantic_model TEXT NOT NULL,
            stage TEXT NOT NULL,
            retry_stage TEXT,
            error_message TEXT,
            work_dir TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE records (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE CASCADE,
            content_header TEXT NOT NULL DEFAULT '',
            markdown_content TEXT NOT NULL,
            question_outline TEXT NOT NULL DEFAULT '',
            raw_segments_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO recordings (id, source_path, import_kind, original_name)
        VALUES ('rec-1', '/tmp/old.mkv', 'scan', 'old.mkv');
        INSERT INTO tasks (id, recording_id, language, transcription_model,
            semantic_provider, semantic_model, stage, work_dir)
        VALUES ('task-1', 'rec-1', 'zh', 'small', 'ollama', 'qwen', 'completed', '/tmp/work');
        INSERT INTO records (id, task_id, content_header, markdown_content, raw_segments_json)
        VALUES ('rec-1', 'task-1', '旧面经', '## 旧文档', '[]');
        """
    )
    connection.close()

    database = Database(db_path)
    database.initialize()
    repository = Repository(database)

    repository.delete_task("task-1")
    records = repository.list_records()
    assert len(records) == 1
    assert records[0]["original_name"] == "old.mkv"
    assert records[0]["content_header"] == "旧面经"
    record = repository.get_record("rec-1")
    assert record["markdown_content"] == "## 旧文档"


def test_failed_task_resets_without_losing_model_options(tmp_path: Path) -> None:
    """失败任务重试后应回到队列，并保留其原有模型配置。"""
    database = Database(tmp_path / "interviews.sqlite3")
    database.initialize()
    repository = Repository(database)
    source = tmp_path / "recording.mkv"
    source.touch()
    recording = repository.create_recording(source, "scan")
    task = repository.create_task(
        recording["id"],
        TaskOptions(language="zh", transcription_model="medium"),
        tmp_path / "work" / "task",
    )
    repository.update_task(task["id"], "failed", "模型超时", "organizing")

    reset = repository.reset_task(task["id"])
    assert reset["stage"] == "queued"
    assert reset["retry_stage"] is None
    assert reset["transcription_model"] == "medium"
