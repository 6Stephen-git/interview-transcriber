#!/usr/bin/env python3
"""Windows 双音轨转写流水线（平台共用）。

提供音轨提取、faster-whisper 转写和幻觉过滤等原语，供 app 的任务流水线复用。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

# Ensure sibling modules under scripts/ are importable when run as a file
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from filter_hallucinations import filter_srt_file

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None  # type: ignore[misc, assignment]

try:
    from zhconv import convert as zh_convert
except ImportError:
    zh_convert = None  # type: ignore[misc, assignment]


AUDIO_FILTER = "dynaudnorm=f=150:g=15:p=0.75,highpass=f=80,lowpass=f=8000"
SIMPLIFIED_PROMPT = "以下是普通话的简体中文会议记录语音转写。"


def to_simplified(text: str) -> str:
    """当 zhconv 可用时将繁体中文转为简体。"""
    if not text or zh_convert is None:
        return text
    return zh_convert(text, "zh-cn")


def format_timestamp_srt(seconds: float) -> str:
    """Convert seconds to SRT timestamp format (HH:MM:SS,mmm)."""
    total_seconds = int(seconds)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    millis = int(round((seconds - total_seconds) * 1000))
    if millis >= 1000:
        millis = 999
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_srt(segments: list[dict], output_path: Path) -> None:
    with open(output_path, "w", encoding="utf-8") as f:
        for i, segment in enumerate(segments, start=1):
            start_time = format_timestamp_srt(segment["start"])
            end_time = format_timestamp_srt(segment["end"])
            text = to_simplified(segment["text"].strip())
            f.write(f"{i}\n")
            f.write(f"{start_time} --> {end_time}\n")
            f.write(f"{text}\n\n")


def require_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            "ffmpeg not found on PATH. Install FFmpeg and reopen the terminal."
        )
    return ffmpeg


def count_audio_streams(mkv_path: Path) -> int:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        # Fall back to ffmpeg -i parsing if ffprobe is missing
        result = subprocess.run(
            [require_ffmpeg(), "-i", str(mkv_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return len(re.findall(r"Stream #\d+:\d+.*Audio:", result.stderr))

    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(mkv_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return len(lines)


def extract_tracks(ffmpeg: str, mkv_path: Path, me_wav: Path, others_wav: Path) -> None:
    stream_count = count_audio_streams(mkv_path)
    if stream_count < 2:
        raise RuntimeError(
            f"Expected at least 2 audio tracks in {mkv_path}, found {stream_count}.\n"
            "Configure OBS Advanced Audio Properties so Mic is Track 1 only and "
            "Desktop/Application Audio is Track 2 only, and record as MKV."
        )

    cmd = [
        ffmpeg,
        "-nostdin",
        "-y",
        "-i",
        str(mkv_path),
        "-map",
        "0:a:0",
        "-af",
        AUDIO_FILTER,
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(me_wav),
        "-map",
        "0:a:1",
        "-af",
        AUDIO_FILTER,
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(others_wav),
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg extraction failed:\n{result.stderr[-2000:]}")

    if not me_wav.is_file() or not others_wav.is_file():
        raise RuntimeError("ffmpeg finished but WAV files were not created.")
    if me_wav.stat().st_size == 0 or others_wav.stat().st_size == 0:
        raise RuntimeError("Extracted WAV files are empty; check the source MKV.")


def _find_local_whisper_dir(model_name: str) -> Path | None:
    """Return a local faster-whisper snapshot dir if the model is already cached."""
    hub = Path.home() / ".cache" / "huggingface" / "hub"
    # Common cache folder names used by faster-whisper / CTranslate2
    candidates = [
        hub / f"models--Systran--faster-whisper-{model_name}",
        hub / f"models--guillaumekln--faster-whisper-{model_name}",
    ]
    for root in candidates:
        snaps = root / "snapshots"
        if not snaps.is_dir():
            continue
        for snap in snaps.iterdir():
            if (snap / "model.bin").exists() and (snap / "config.json").exists():
                return snap
    return None


def load_model(model_name: str, compute_type: str) -> WhisperModel:
    if WhisperModel is None:
        raise RuntimeError(
            "faster-whisper 未安装。请执行：pip install -r requirements-windows.txt"
        )
    print(f"[2/5] 加载模型 '{model_name}'（CPU / {compute_type}）...", flush=True)

    local_dir = _find_local_whisper_dir(model_name)
    # Prefer offline load when cache exists — avoids hanging on Hub/mirror timeouts.
    load_targets: list[tuple[str, dict]] = []
    if local_dir is not None:
        load_targets.append((str(local_dir), {"local_files_only": True}))
    load_targets.append((model_name, {"local_files_only": True}))
    load_targets.append((model_name, {"local_files_only": False}))

    import threading

    last_error: BaseException | None = None
    for source, kwargs in load_targets:
        mode = "离线" if kwargs.get("local_files_only") else "联网下载/校验"
        print(f"      尝试{mode}加载: {source}", flush=True)
        done = threading.Event()
        error: list[BaseException] = []
        result: list[WhisperModel] = []
        started = time.time()

        def _load(
            src: str = source,
            opts: dict = kwargs,
        ) -> None:
            try:
                result.append(
                    WhisperModel(
                        src,
                        device="cpu",
                        compute_type=compute_type,
                        **opts,
                    )
                )
            except BaseException as exc:  # noqa: BLE001
                error.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=_load, daemon=True)
        thread.start()
        while not done.wait(timeout=5):
            elapsed = int(time.time() - started)
            print(f"      仍在加载（{mode}）… 已等待 {elapsed}s", flush=True)
            # Network attempts shouldn't hang forever
            if not kwargs.get("local_files_only") and elapsed >= 90:
                print("      联网加载超时，中止本次尝试。", flush=True)
                break

        if result:
            print(f"      模型就绪（{time.time() - started:.1f}s）", flush=True)
            return result[0]

        if error:
            last_error = error[0]
            print(f"      本次失败: {error[0]}", flush=True)
        elif not kwargs.get("local_files_only"):
            last_error = TimeoutError("联网加载超过 90 秒")
            print(f"      本次失败: {last_error}", flush=True)

    raise RuntimeError(
        "模型加载失败。可检查 Clash 代理(7890)是否开启，或删除不完整缓存后重试。"
        + (f" 最后错误: {last_error}" if last_error else "")
    )


def _wav_duration_seconds(wav_path: Path) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(wav_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def transcribe_wav(
    model: WhisperModel,
    wav_path: Path,
    srt_path: Path,
    language: str | None,
    label: str = "",
    should_cancel: Callable[[], bool] | None = None,
) -> Path:
    tag = label or wav_path.name
    duration = _wav_duration_seconds(wav_path)
    dur_txt = f"，约 {duration / 60:.1f} 分钟" if duration else ""
    print(f"      正在转写 {tag}{dur_txt} ...", flush=True)
    started = time.time()
    transcribe_kwargs: dict = {
        "language": language,
        "beam_size": 5,
        "vad_filter": True,
        "condition_on_previous_text": False,
    }
    # Bias Whisper toward Simplified Chinese for zh*
    if language and language.lower().startswith("zh"):
        transcribe_kwargs["initial_prompt"] = SIMPLIFIED_PROMPT

    segments_iter, info = model.transcribe(str(wav_path), **transcribe_kwargs)
    total = duration or getattr(info, "duration", None) or 0.0
    segments: list[dict] = []
    last_pct = -1
    for seg in segments_iter:
        if should_cancel and should_cancel():
            raise RuntimeError("任务已停止")
        if seg.text and seg.text.strip():
            segments.append(
                {
                    "start": seg.start,
                    "end": seg.end,
                    "text": to_simplified(seg.text),
                }
            )
        if total > 0:
            pct = min(99, int((seg.end / total) * 100))
            if pct >= last_pct + 5 or pct >= 99:
                last_pct = pct
                print(f"\r      {tag} 进度: {pct}%   ", end="", flush=True)
    if total > 0:
        print(f"\r      {tag} 进度: 100%  ", flush=True)

    elapsed = time.time() - started
    if not segments:
        print(f"      {tag}: 未检测到语音（{elapsed:.1f}s）", flush=True)
        srt_path.write_text("", encoding="utf-8")
    else:
        write_srt(segments, srt_path)
        print(
            f"      {tag}: {len(segments)} 句，耗时 {elapsed:.1f}s -> {srt_path.name}",
            flush=True,
        )
    return srt_path


def safe_filter_srt(input_srt: Path, output_srt: Path) -> None:
    """Filter hallucinations; empty/missing input becomes an empty output."""
    if not input_srt.is_file() or input_srt.stat().st_size == 0:
        output_srt.write_text("", encoding="utf-8")
        return
    ok = filter_srt_file(str(input_srt), str(output_srt))
    if not ok:
        # Fall back to unfiltered copy so the pipeline can still finish
        output_srt.write_text(input_srt.read_text(encoding="utf-8"), encoding="utf-8")
