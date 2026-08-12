"""后台 worker 的阶段重试行为测试。"""

from unittest.mock import Mock

from app.schemas import SettingsPayload
from app.workers import TaskManager


def test_semantic_failure_retries_without_retranscribing() -> None:
    """整理阶段失败时，重试应直接调度整理阶段而非重新占用 ASR。"""
    repository = Mock()
    settings = Mock()
    settings.get.return_value = SettingsPayload()
    repository.get_task.return_value = {
        "id": "task-1",
        "stage": "failed",
        "retry_stage": "organizing",
    }
    repository.reset_task.return_value = {"id": "task-1", "stage": "queued"}
    manager = TaskManager(repository, settings)
    manager.enqueue = Mock()

    try:
        manager.retry("task-1")
        manager.enqueue.assert_called_once_with("task-1", resume_stage="organizing")
    finally:
        manager.shutdown()


def test_cancel_marks_queued_task_immediately() -> None:
    """排队中的任务应立即标记为已停止。"""
    repository = Mock()
    settings = Mock()
    settings.get.return_value = SettingsPayload()
    repository.get_task.return_value = {
        "id": "task-2",
        "stage": "queued",
        "retry_stage": None,
    }
    repository.update_task.return_value = {"id": "task-2", "stage": "cancelled"}
    manager = TaskManager(repository, settings)
    try:
        result = manager.cancel("task-2")
        assert result["stage"] == "cancelled"
        repository.update_task.assert_called_once_with(
            "task-2",
            "cancelled",
            error_message="用户已停止",
            retry_stage=None,
        )
        assert manager.is_cancelled("task-2")
    finally:
        manager.shutdown()


def test_cancelled_task_can_retry() -> None:
    """已停止任务应允许从记录的阶段重试。"""
    repository = Mock()
    settings = Mock()
    settings.get.return_value = SettingsPayload()
    repository.get_task.return_value = {
        "id": "task-3",
        "stage": "cancelled",
        "retry_stage": "transcribing",
    }
    repository.reset_task.return_value = {"id": "task-3", "stage": "queued"}
    manager = TaskManager(repository, settings)
    manager.enqueue = Mock()
    try:
        manager.retry("task-3")
        manager.enqueue.assert_called_once_with("task-3", resume_stage="transcribing")
    finally:
        manager.shutdown()


def test_completed_task_reorganizes_without_asr() -> None:
    """已完成任务重新整理时应直接进入语义阶段。"""
    repository = Mock()
    settings = Mock()
    settings.get.return_value = SettingsPayload()
    repository.get_task.return_value = {
        "id": "task-4",
        "stage": "completed",
        "retry_stage": None,
    }
    repository.reset_task.return_value = {"id": "task-4", "stage": "queued"}
    manager = TaskManager(repository, settings)
    manager.enqueue = Mock()
    try:
        manager.retry("task-4")
        manager.enqueue.assert_called_once_with("task-4", resume_stage="organizing")
    finally:
        manager.shutdown()


def test_cleanup_completed_artifacts_deletes_workdir_and_managed_copy(tmp_path) -> None:
    """已完成任务的中间产物与托管录像副本应被清理。"""
    repository = Mock()
    settings = Mock()
    settings.get.return_value = SettingsPayload()
    work_dir = tmp_path / "work" / "task"
    work_dir.mkdir(parents=True)
    (work_dir / "raw_segments.json").write_text("[]", encoding="utf-8")
    managed = tmp_path / "managed.mkv"
    managed.write_bytes(b"x")
    task = {
        "id": "task-c",
        "recording_id": "rec-1",
        "stage": "completed",
        "work_dir": str(work_dir),
        "managed_path": str(managed),
    }
    repository.list_tasks.return_value = [task]
    repository.get_task.return_value = task
    manager = TaskManager(repository, settings)
    try:
        manager.cleanup_completed_artifacts()
    finally:
        manager.shutdown()
    assert not work_dir.exists()
    assert not managed.exists()


def test_cleanup_keeps_managed_copy_when_sibling_task_unfinished(tmp_path) -> None:
    """同源录像仍有未完成任务时，托管录像副本应保留。"""
    repository = Mock()
    settings = Mock()
    settings.get.return_value = SettingsPayload()
    work_dir = tmp_path / "work" / "task"
    work_dir.mkdir(parents=True)
    managed = tmp_path / "managed.mkv"
    managed.write_bytes(b"x")
    completed = {
        "id": "task-done",
        "recording_id": "rec-2",
        "stage": "completed",
        "work_dir": str(work_dir),
        "managed_path": str(managed),
    }
    queued = {
        "id": "task-wait",
        "recording_id": "rec-2",
        "stage": "queued",
        "work_dir": str(tmp_path / "work" / "wait"),
        "managed_path": str(managed),
    }
    repository.list_tasks.return_value = [completed, queued]
    repository.get_task.return_value = completed
    manager = TaskManager(repository, settings)
    try:
        manager.cleanup_completed_artifacts()
    finally:
        manager.shutdown()
    assert not work_dir.exists()
    assert managed.exists()
