"""FastAPI 请求和响应模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


Language = Literal["auto", "zh", "en"]
SemanticProvider = Literal["ollama", "openai"]


class SettingsPayload(BaseModel):
    """用户可在本地设置页维护的应用配置。"""

    recording_directory: str = ""
    local_concurrency: int = Field(default=1, ge=1, le=4)
    api_concurrency: int = Field(default=2, ge=1, le=16)
    transcription_models: list[str] = Field(
        default_factory=lambda: ["small", "medium", "large-v3"],
        min_length=1,
        max_length=30,
    )
    semantic_models: list[str] = Field(
        default_factory=lambda: ["deepseek-v4-flash", "deepseek-v4-pro", "qwen2.5:3b"],
        min_length=1,
        max_length=30,
    )
    ollama_url: str = "http://127.0.0.1:11434"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""

    @field_validator("transcription_models", "semantic_models")
    @classmethod
    def normalize_model_lists(cls, models: list[str]) -> list[str]:
        """去除空白与重复项，避免下拉框出现不可选的空值。"""
        normalized = list(dict.fromkeys(model.strip() for model in models if model.strip()))
        if not normalized:
            raise ValueError("模型列表至少需要包含一个模型。")
        return normalized


class TaskOptions(BaseModel):
    """单个转录任务的模型选择。"""

    language: Language = "auto"
    transcription_model: str = Field(default="small", min_length=1, max_length=100)
    semantic_provider: SemanticProvider = "ollama"
    semantic_model: str = Field(default="deepseek-v4-flash", min_length=1, max_length=200)


class ScanTaskRequest(TaskOptions):
    """将已扫描的本地录制文件加入任务队列。"""

    source_path: str = Field(min_length=1)


class RetryRequest(BaseModel):
    """可选覆盖原任务模型选项的重试请求。"""

    options: TaskOptions | None = None


class RecordUpdateRequest(BaseModel):
    """保存用户手动编辑后的面经文档。"""

    content_header: str = Field(default="", max_length=20_000)
    markdown_content: str = Field(min_length=1, max_length=2_000_000)
