"""OBS 文件格式和双轨 SRT 片段转换测试。"""

from pathlib import Path

import pytest

from app.services.transcription import _read_srt_segments, validate_recording_file


def test_validate_recording_accepts_supported_obs_containers(tmp_path: Path) -> None:
    """首期允许的 OBS 容器格式应可进入转录管线。"""
    for suffix in (".mkv", ".mp4", ".mov", ".flv", ".ts"):
        recording = tmp_path / f"meeting{suffix}"
        recording.touch()
        assert validate_recording_file(recording) == recording.resolve()


def test_validate_recording_rejects_unsupported_container(tmp_path: Path) -> None:
    """不在产品范围内的格式应在转录前被拒绝。"""
    recording = tmp_path / "meeting.mp3"
    recording.touch()
    with pytest.raises(ValueError, match="不支持"):
        validate_recording_file(recording)


def test_srt_segments_keep_internal_timing_and_speaker(tmp_path: Path) -> None:
    """原始片段需要保存内部时间戳，以支持后续重新整理。"""
    subtitle = tmp_path / "speaker.srt"
    subtitle.write_text(
        "1\n00:00:01,250 --> 00:00:03,500\n你好，介绍一下项目。\n\n",
        encoding="utf-8",
    )
    segments = _read_srt_segments(subtitle, "面试官")
    assert segments == [
        {
            "speaker": "面试官",
            "start_ms": 1250,
            "end_ms": 3500,
            "text": "你好，介绍一下项目。",
        }
    ]
