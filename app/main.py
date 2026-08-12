"""FastAPI 本地面经转录平台入口。"""

from __future__ import annotations

import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import AppPaths, get_app_paths
from app.db import Database
from app.repository import Repository
from app.schemas import (
    RecordUpdateRequest,
    RetryRequest,
    ScanTaskRequest,
    SettingsPayload,
    TaskOptions,
)
from app.services.settings import SettingsService
from app.services.transcription import find_recordings, validate_recording_file
from app.workers import TaskManager


def _service(request: Request) -> tuple[Repository, SettingsService, TaskManager, AppPaths]:
    """取得 FastAPI 生命周期中初始化的应用服务。"""
    return (
        request.app.state.repository,
        request.app.state.settings,
        request.app.state.task_manager,
        request.app.state.paths,
    )


Services = Annotated[
    tuple[Repository, SettingsService, TaskManager, AppPaths], Depends(_service)
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化本地存储与队列，关闭时停止 worker。"""
    paths = get_app_paths()
    paths.create_directories()
    database = Database(paths.database_path)
    database.initialize()
    repository = Repository(database)
    settings = SettingsService(database)
    manager = TaskManager(repository, settings)
    app.state.paths = paths
    app.state.repository = repository
    app.state.settings = settings
    app.state.task_manager = manager
    manager.enqueue_pending()
    manager.cleanup_completed_artifacts()
    yield
    manager.shutdown()


app = FastAPI(title="面经转录平台", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.get("/")
def index() -> FileResponse:
    """返回单页本地网页应用。"""
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    """提供本地启动探针。"""
    return {"status": "ok"}


@app.get("/api/settings")
def get_settings(services: Services) -> SettingsPayload:
    """读取本地应用设置。"""
    _, settings, _, _ = services
    return settings.get()


@app.put("/api/settings")
def update_settings(payload: SettingsPayload, services: Services) -> SettingsPayload:
    """保存目录、并发与模型服务连接设置。"""
    _, settings, _, _ = services
    return settings.update(payload)


@app.get("/api/imports/scan")
def scan_recordings(services: Services) -> dict:
    """扫描已配置的 OBS 录制目录，并返回可导入文件。"""
    _, settings, _, _ = services
    recording_directory = settings.get().recording_directory
    if not recording_directory:
        raise HTTPException(status_code=400, detail="请先在设置中填写 OBS 录制目录。")
    try:
        files = find_recordings(Path(recording_directory))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "files": [
            {
                "path": str(file),
                "name": file.name,
                "size": file.stat().st_size,
                "modified_at": file.stat().st_mtime,
            }
            for file in files
        ]
    }


@app.post("/api/tasks")
def create_scan_task(payload: ScanTaskRequest, services: Services) -> dict:
    """将扫描目录中的文件作为原位引用任务加入队列。"""
    repository, settings, manager, paths = services
    source = Path(payload.source_path)
    scan_root_text = settings.get().recording_directory
    if not scan_root_text:
        raise HTTPException(status_code=400, detail="请先配置 OBS 录制目录。")
    scan_root = Path(scan_root_text).expanduser().resolve()
    try:
        source = source.expanduser().resolve()
        source.relative_to(scan_root)
        source = validate_recording_file(source)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    recording = repository.create_recording(source, "scan")
    task = repository.create_task(recording["id"], payload, paths.work_dir / uuid.uuid4().hex)
    manager.enqueue(task["id"])
    return task


@app.post("/api/imports/upload")
async def upload_recordings(
    files: Annotated[list[UploadFile], File(...)],
    services: Services,
    language: Annotated[str, Form()] = "auto",
    transcription_model: Annotated[str, Form()] = "small",
    semantic_provider: Annotated[str, Form()] = "ollama",
    semantic_model: Annotated[str, Form()] = "deepseek-v4-flash",
) -> dict:
    """接收拖拽文件，复制到应用管理目录并为每个文件创建任务。"""
    repository, _, manager, paths = services
    try:
        options = TaskOptions(
            language=language,
            transcription_model=transcription_model,
            semantic_provider=semantic_provider,
            semantic_model=semantic_model,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    tasks = []
    for upload in files:
        original_name = upload.filename or "recording"
        suffix = Path(original_name).suffix.lower()
        if suffix not in {".mkv", ".mp4", ".mov", ".flv", ".ts"}:
            raise HTTPException(status_code=400, detail=f"不支持 {original_name} 格式。")
        destination = paths.managed_recordings_dir / f"{uuid.uuid4().hex}{suffix}"
        try:
            with destination.open("wb") as target:
                shutil.copyfileobj(upload.file, target)
            source = validate_recording_file(destination)
            recording = repository.create_recording(source, "upload", source)
            task = repository.create_task(
                recording["id"], options, paths.work_dir / uuid.uuid4().hex
            )
            manager.enqueue(task["id"])
            tasks.append(task)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            destination.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            await upload.close()
    return {"tasks": tasks}


@app.get("/api/tasks")
def list_tasks(services: Services) -> dict:
    """返回任务列表和每项当前处理阶段。"""
    repository, _, _, _ = services
    return {"tasks": repository.list_tasks()}


@app.post("/api/tasks/{task_id}/retry")
def retry_task(task_id: str, payload: RetryRequest, services: Services) -> dict:
    """从失败、停止阶段重试，或对已完成任务重新语义整理。"""
    _, _, manager, _ = services
    try:
        return manager.retry(task_id, payload.options)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str, services: Services) -> dict:
    """停止排队或运行中的转录/整理任务。"""
    _, _, manager, _ = services
    try:
        return manager.cancel(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str, services: Services) -> dict:
    """删除已结束的队列任务；已生成的面经文档保留。运行中的任务需先停止。"""
    repository, _, _, _ = services
    try:
        task = repository.get_task(task_id)
        if task["stage"] in {"queued", "extracting", "transcribing", "organizing"}:
            raise HTTPException(status_code=400, detail="请先停止任务，再删除。")
        repository.delete_task(task_id)
        return {"ok": True}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/records")
def list_records(services: Services, q: str = "") -> dict:
    """仅在用户编辑的内容头中匹配关键词。"""
    repository, _, _, _ = services
    return {"records": repository.list_records(q)}


@app.get("/api/records/{record_id}")
def get_record(record_id: str, services: Services) -> dict:
    """读取一篇完整 Markdown 面经。"""
    repository, _, _, _ = services
    try:
        return repository.get_record(record_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/api/records/{record_id}")
def update_record(
    record_id: str, payload: RecordUpdateRequest, services: Services
) -> dict:
    """保存用户对内容头和 Markdown 的手动编辑。"""
    repository, _, _, _ = services
    try:
        return repository.update_record(
            record_id, payload.content_header, payload.markdown_content
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/api/records/{record_id}")
def delete_record(record_id: str, services: Services) -> dict:
    """删除面经记录及其关联任务。"""
    repository, _, _, _ = services
    try:
        repository.delete_record(record_id)
        return {"ok": True}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
