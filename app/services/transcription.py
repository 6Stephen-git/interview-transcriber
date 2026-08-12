"""复用 Windows faster-whisper 能力的双音轨转录服务。"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import srt

from app.config import SUPPORTED_RECORDING_SUFFIXES

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from process_mkv import (  # noqa: E402
    extract_tracks,
    load_model,
    require_ffmpeg,
    safe_filter_srt,
    transcribe_wav,
)


class TaskCancelled(RuntimeError):
    """用户主动停止任务时抛出。"""


def validate_recording_file(source_path: Path) -> Path:
    """验证上传或扫描的文件是否是可处理的 OBS 录制容器。"""
    source_path = source_path.expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"录制文件不存在: {source_path}")
    if source_path.suffix.lower() not in SUPPORTED_RECORDING_SUFFIXES:
        formats = "、".join(sorted(SUPPORTED_RECORDING_SUFFIXES))
        raise ValueError(f"不支持 {source_path.suffix} 格式，仅支持：{formats}")
    return source_path


def find_recordings(directory: Path) -> list[Path]:
    """枚举录制目录一层中的 OBS 文件，按修改时间由新到旧排序。"""
    directory = directory.expanduser().resolve()
    if not directory.is_dir():
        raise NotADirectoryError(f"录制目录不存在: {directory}")
    files = [
        item
        for item in directory.iterdir()
        if item.is_file() and item.suffix.lower() in SUPPORTED_RECORDING_SUFFIXES
    ]
    return sorted(files, key=lambda item: item.stat().st_mtime, reverse=True)


def _read_srt_segments(srt_path: Path, speaker: str) -> list[dict[str, Any]]:
    """读取 SRT，并转换为供语义整理与重新处理使用的原始片段。"""
    if not srt_path.exists() or srt_path.stat().st_size == 0:
        return []
    subtitles = list(srt.parse(srt_path.read_text(encoding="utf-8")))
    return [
        {
            "speaker": speaker,
            "start_ms": int(item.start.total_seconds() * 1000),
            "end_ms": int(item.end.total_seconds() * 1000),
            "text": item.content.strip(),
        }
        for item in subtitles
        if item.content.strip()
    ]


def transcribe_dual_track(
    source_path: Path,
    work_dir: Path,
    language: str,
    model_name: str,
    compute_type: str = "int8",
    on_stage: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """提取 OBS 前两条音轨并转录，返回保留内部时间戳的合并片段。"""

    def ensure_not_cancelled() -> None:
        if should_cancel and should_cancel():
            raise TaskCancelled("任务已停止")

    source_path = validate_recording_file(source_path)
    work_dir.mkdir(parents=True, exist_ok=True)
    me_wav = work_dir / "me.wav"
    others_wav = work_dir / "interviewer.wav"
    me_srt = work_dir / "me.srt"
    others_srt = work_dir / "interviewer.srt"
    me_filtered = work_dir / "me.filtered.srt"
    others_filtered = work_dir / "interviewer.filtered.srt"

    ensure_not_cancelled()
    ffmpeg = require_ffmpeg()
    if not me_wav.exists() or not others_wav.exists():
        if on_stage:
            on_stage("extracting")
        extract_tracks(ffmpeg, source_path, me_wav, others_wav)

    ensure_not_cancelled()
    if on_stage:
        on_stage("transcribing")
    model = load_model(model_name, compute_type)
    whisper_language = None if language == "auto" else language
    if not me_srt.exists():
        ensure_not_cancelled()
        try:
            transcribe_wav(
                model,
                me_wav,
                me_srt,
                whisper_language,
                label="我",
                should_cancel=should_cancel,
            )
        except RuntimeError as exc:
            if str(exc) == "任务已停止":
                raise TaskCancelled("任务已停止") from exc
            raise
    if not others_srt.exists():
        ensure_not_cancelled()
        try:
            transcribe_wav(
                model,
                others_wav,
                others_srt,
                whisper_language,
                label="面试官",
                should_cancel=should_cancel,
            )
        except RuntimeError as exc:
            if str(exc) == "任务已停止":
                raise TaskCancelled("任务已停止") from exc
            raise

    ensure_not_cancelled()
    if not me_filtered.exists():
        safe_filter_srt(me_srt, me_filtered)
    if not others_filtered.exists():
        safe_filter_srt(others_srt, others_filtered)

    segments = _read_srt_segments(me_filtered, "我") + _read_srt_segments(
        others_filtered, "面试官"
    )
    return sorted(segments, key=lambda item: item["start_ms"])
