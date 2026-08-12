"""应用路径与默认配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_MANAGED_DIR = APP_DATA_DIR / "managed-recordings"
DEFAULT_WORK_DIR = APP_DATA_DIR / "work"
DEFAULT_DATABASE_PATH = APP_DATA_DIR / "interviews.sqlite3"
SUPPORTED_RECORDING_SUFFIXES = {".mkv", ".mp4", ".mov", ".flv", ".ts"}


@dataclass(frozen=True)
class AppPaths:
    """集中保存可被环境变量覆盖的本地存储路径。"""

    data_dir: Path
    database_path: Path
    managed_recordings_dir: Path
    work_dir: Path

    def create_directories(self) -> None:
        """创建应用运行所需的目录。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.managed_recordings_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)


def get_app_paths() -> AppPaths:
    """从环境变量读取应用路径，未配置时使用项目内 data 目录。"""
    data_dir = Path(os.getenv("INTERVIEW_APP_DATA_DIR", APP_DATA_DIR)).expanduser()
    database_path = Path(
        os.getenv("INTERVIEW_APP_DATABASE", data_dir / DEFAULT_DATABASE_PATH.name)
    ).expanduser()
    managed_dir = Path(
        os.getenv("INTERVIEW_APP_MANAGED_DIR", data_dir / DEFAULT_MANAGED_DIR.name)
    ).expanduser()
    work_dir = Path(
        os.getenv("INTERVIEW_APP_WORK_DIR", data_dir / DEFAULT_WORK_DIR.name)
    ).expanduser()
    return AppPaths(
        data_dir=data_dir,
        database_path=database_path,
        managed_recordings_dir=managed_dir,
        work_dir=work_dir,
    )
