"""受并发上限约束的本地转录与语义整理后台任务。"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.repository import Repository
from app.schemas import TaskOptions
from app.services.semantic import organize_with_ollama, organize_with_openai
from app.services.settings import SettingsService
from app.services.transcription import TaskCancelled, transcribe_dual_track

logger = logging.getLogger(__name__)


class TaskManager:
    """分离本地 ASR 与 API 整理的并发池，避免模型任务挤占系统资源。"""

    def __init__(self, repository: Repository, settings: SettingsService) -> None:
        self.repository = repository
        self.settings = settings
        config = settings.get()
        self._local_executor = ThreadPoolExecutor(
            max_workers=config.local_concurrency, thread_name_prefix="asr"
        )
        self._semantic_executor = ThreadPoolExecutor(
            max_workers=config.api_concurrency, thread_name_prefix="semantic"
        )
        self._lock = threading.Lock()
        self._scheduled: set[str] = set()
        self._cancel_flags: dict[str, threading.Event] = {}

    def enqueue(self, task_id: str, resume_stage: str | None = None) -> None:
        """调度一个排队任务；已调度任务不会重复提交。"""
        with self._lock:
            if task_id in self._scheduled:
                return
            self._scheduled.add(task_id)
            self._cancel_flags[task_id] = threading.Event()
        task = self.repository.get_task(task_id)
        if resume_stage == "organizing" or task["retry_stage"] == "organizing":
            future = self._semantic_executor.submit(self._organize, task_id)
            future.add_done_callback(lambda _: self._forget(task_id))
        else:
            # 转录成功后会衔接到整理任务，因此不在此处立即清除调度状态。
            self._local_executor.submit(self._transcribe, task_id)

    def enqueue_pending(self) -> None:
        """应用启动后恢复数据库中尚未处理的队列任务。"""
        for task in self.repository.list_queued_tasks():
            self.enqueue(task["id"])

    def retry(self, task_id: str, options: TaskOptions | None = None) -> dict:
        """重置失败、已停止或已完成任务后按阶段继续执行。"""
        failed_task = self.repository.get_task(task_id)
        if failed_task["stage"] not in {"failed", "cancelled", "completed"}:
            raise ValueError("仅失败、已停止或已完成的任务可以重试/重新整理。")
        resume_stage = failed_task["retry_stage"]
        if failed_task["stage"] == "completed":
            # 已完成任务默认只重跑语义整理，避免重复占用本地 ASR。
            resume_stage = "organizing"
        task = self.repository.reset_task(task_id, options)
        self.enqueue(task_id, resume_stage=resume_stage)
        return task

    def cancel(self, task_id: str) -> dict:
        """停止排队或运行中的任务。"""
        task = self.repository.get_task(task_id)
        if task["stage"] in {"completed", "failed", "cancelled"}:
            raise ValueError("该任务已结束，无需停止。")
        with self._lock:
            flag = self._cancel_flags.get(task_id)
            if flag is None:
                flag = threading.Event()
                self._cancel_flags[task_id] = flag
            flag.set()
        if task["stage"] == "queued":
            return self.repository.update_task(
                task_id,
                "cancelled",
                error_message="用户已停止",
                retry_stage=None,
            )
        # 运行中的任务由 worker 协作退出后再写入 cancelled。
        return self.repository.update_task(
            task_id,
            task["stage"],
            error_message="正在停止…",
            retry_stage=task.get("retry_stage"),
        )

    def is_cancelled(self, task_id: str) -> bool:
        """查询任务是否已被请求停止。"""
        with self._lock:
            flag = self._cancel_flags.get(task_id)
            return bool(flag and flag.is_set())

    def shutdown(self) -> None:
        """停止接收新任务并等待正在执行的工作完成。"""
        self._local_executor.shutdown(wait=False, cancel_futures=False)
        self._semantic_executor.shutdown(wait=False, cancel_futures=False)

    def _forget(self, task_id: str) -> None:
        """在 future 完成后允许该任务未来被再次重试。"""
        with self._lock:
            self._scheduled.discard(task_id)
            self._cancel_flags.pop(task_id, None)

    def cleanup_completed_artifacts(self) -> None:
        """清理所有已完成任务的中间产物与可删除的托管录像（应用启动时调用）。"""
        try:
            tasks = self.repository.list_tasks()
            for task in tasks:
                if task["stage"] != "completed":
                    continue
                self._cleanup_completed_task(self.repository.get_task(task["id"]))
        except Exception:
            logger.exception("清理已完成任务产物失败")

    def _cleanup_completed_task(self, task: dict) -> None:
        """删除任务中间产物；上传导入的原始录像在无未完成任务时一并删除。"""
        self._delete_work_dir(task)
        self._delete_managed_copy_if_safe(task)

    def _delete_work_dir(self, task: dict) -> None:
        """删除任务工作目录（wav / srt / raw_segments.json 等中间产物）。"""
        work_dir = Path(task["work_dir"])
        if work_dir.is_dir():
            shutil.rmtree(work_dir, ignore_errors=True)

    def _delete_managed_copy_if_safe(self, task: dict) -> None:
        """删除上传导入的托管录像副本；同源录像仍有未完成任务时保留。"""
        managed = task.get("managed_path")
        if not managed:
            return
        try:
            if any(
                other["recording_id"] == task["recording_id"]
                and other["stage"] != "completed"
                for other in self.repository.list_tasks()
            ):
                return
        except Exception:
            logger.exception("检查同源任务状态失败: %s", task["id"])
            return
        try:
            Path(managed).unlink(missing_ok=True)
            logger.info("已删除托管录像副本: %s", managed)
        except OSError:
            logger.warning("删除托管录像副本失败: %s", managed)

    def _transcribe(self, task_id: str) -> None:
        """执行提取和 ASR，成功后将语义整理交给独立并发池。"""
        stage = "extracting"
        try:
            if self.is_cancelled(task_id):
                raise TaskCancelled("任务已停止")
            task = self.repository.get_task(task_id)
            source = Path(task["managed_path"] or task["source_path"])

            def update_stage(next_stage: str) -> None:
                nonlocal stage
                if self.is_cancelled(task_id):
                    raise TaskCancelled("任务已停止")
                stage = next_stage
                self.repository.update_task(task_id, next_stage)

            segments = transcribe_dual_track(
                source_path=source,
                work_dir=Path(task["work_dir"]),
                language=task["language"],
                model_name=task["transcription_model"],
                on_stage=update_stage,
                should_cancel=lambda: self.is_cancelled(task_id),
            )
            if self.is_cancelled(task_id):
                raise TaskCancelled("任务已停止")
            raw_path = Path(task["work_dir"]) / "raw_segments.json"
            raw_path.write_text(json.dumps(segments, ensure_ascii=False), encoding="utf-8")
            self.repository.update_task(task_id, "organizing")
            future = self._semantic_executor.submit(self._organize, task_id)
            future.add_done_callback(lambda _: self._forget(task_id))
        except TaskCancelled:
            logger.info("转录任务已停止: %s", task_id)
            self.repository.update_task(
                task_id,
                "cancelled",
                error_message="用户已停止",
                retry_stage=stage,
            )
            self._forget(task_id)
        except Exception as exc:  # noqa: BLE001 - 错误需持久化给前端展示
            logger.exception("转录任务失败: %s", task_id)
            self.repository.update_task(
                task_id, "failed", error_message=str(exc), retry_stage=stage
            )
            self._forget(task_id)

    def _organize(self, task_id: str) -> None:
        """读取已完成的原始转录并调用所选模型生成 Markdown 面经。"""
        try:
            if self.is_cancelled(task_id):
                raise TaskCancelled("任务已停止")
            task = self.repository.get_task(task_id)
            raw_path = Path(task["work_dir"]) / "raw_segments.json"
            if raw_path.exists():
                segments = json.loads(raw_path.read_text(encoding="utf-8"))
            else:
                # 任务完成后工作目录已被清理，回退到数据库保存的片段副本
                try:
                    segments = self.repository.get_record_by_task(task_id)["raw_segments"]
                except KeyError as exc:
                    raise RuntimeError("缺少原始转录产物，无法继续语义整理。") from exc
            config = self.settings.get()
            if task["semantic_provider"] == "ollama":
                markdown = organize_with_ollama(
                    config.ollama_url, task["semantic_model"], segments
                )
            else:
                markdown = organize_with_openai(
                    config.openai_base_url,
                    config.openai_api_key,
                    task["semantic_model"],
                    segments,
                )
            if self.is_cancelled(task_id):
                raise TaskCancelled("任务已停止")
            self.repository.save_record(
                task_id, markdown, segments, original_name=task["original_name"]
            )
            self.repository.update_task(task_id, "completed")
            try:
                self._cleanup_completed_task(self.repository.get_task(task_id))
            except Exception:
                logger.exception("清理已完成任务产物失败: %s", task_id)
        except TaskCancelled:
            logger.info("整理任务已停止: %s", task_id)
            self.repository.update_task(
                task_id,
                "cancelled",
                error_message="用户已停止",
                retry_stage="organizing",
            )
        except Exception as exc:  # noqa: BLE001 - 错误需持久化给前端展示
            logger.exception("整理任务失败: %s", task_id)
            self.repository.update_task(
                task_id, "failed", error_message=str(exc), retry_stage="organizing"
            )
