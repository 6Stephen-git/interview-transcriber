"""应用设置的默认值与读取逻辑。"""

from __future__ import annotations

from app.db import Database
from app.schemas import SettingsPayload


SETTINGS_KEY = "application_settings"


class SettingsService:
    """集中管理本地目录、并发和模型服务连接设置。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self) -> SettingsPayload:
        """读取设置；首次运行返回安全的默认值。"""
        saved = self.database.get_setting(SETTINGS_KEY, {})
        return SettingsPayload.model_validate(saved)

    def update(self, payload: SettingsPayload) -> SettingsPayload:
        """保存用户设置。API 密钥只存入本地 SQLite。"""
        self.database.set_setting(SETTINGS_KEY, payload.model_dump())
        return payload
