"""FastAPI 本地设置与健康检查接口测试。"""

from fastapi.testclient import TestClient

from app.main import app
from app.schemas import TaskOptions


def test_health_and_settings_are_local(tmp_path, monkeypatch) -> None:
    """应用应在指定本地数据目录初始化，并允许保存设置。"""
    monkeypatch.setenv("INTERVIEW_APP_DATA_DIR", str(tmp_path / "data"))
    with TestClient(app) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        response = client.put(
            "/api/settings",
            json={
                "recording_directory": str(tmp_path / "obs"),
                "local_concurrency": 1,
                "api_concurrency": 3,
            "transcription_models": ["small", "medium"],
            "semantic_models": ["deepseek-v4-flash", "deepseek-v4-pro"],
                "ollama_url": "http://127.0.0.1:11434",
                "openai_base_url": "https://example.test/v1",
                "openai_api_key": "local-test-key",
            },
        )
        assert response.status_code == 200
        settings = client.get("/api/settings").json()
        assert settings["api_concurrency"] == 3
        assert settings["transcription_models"] == ["small", "medium"]
        assert settings["semantic_models"] == ["deepseek-v4-flash", "deepseek-v4-pro"]


def test_cancel_and_delete_task_endpoints(tmp_path, monkeypatch) -> None:
    """排队任务可停止；结束后可删除；运行中不可直接删除。"""
    monkeypatch.setenv("INTERVIEW_APP_DATA_DIR", str(tmp_path / "data"))
    with TestClient(app) as client:
        repository = client.app.state.repository
        source = tmp_path / "recording.mkv"
        source.touch()
        recording = repository.create_recording(source, "scan")
        task = repository.create_task(
            recording["id"], TaskOptions(), tmp_path / "work" / "task"
        )

        cancel = client.post(f"/api/tasks/{task['id']}/cancel")
        assert cancel.status_code == 200
        assert cancel.json()["stage"] == "cancelled"

        delete = client.delete(f"/api/tasks/{task['id']}")
        assert delete.status_code == 200
        assert client.get("/api/tasks").json()["tasks"] == []

        recording2 = repository.create_recording(source, "scan")
        running = repository.create_task(
            recording2["id"], TaskOptions(), tmp_path / "work" / "running"
        )
        repository.update_task(running["id"], "transcribing")
        blocked = client.delete(f"/api/tasks/{running['id']}")
        assert blocked.status_code == 400


def test_delete_record_endpoint(tmp_path, monkeypatch) -> None:
    """面经删除接口应移除文档及其任务。"""
    monkeypatch.setenv("INTERVIEW_APP_DATA_DIR", str(tmp_path / "data"))
    with TestClient(app) as client:
        repository = client.app.state.repository
        source = tmp_path / "done.mkv"
        source.touch()
        recording = repository.create_recording(source, "scan")
        task = repository.create_task(
            recording["id"], TaskOptions(), tmp_path / "work" / "done"
        )
        repository.update_task(task["id"], "organizing")
        record = repository.save_record(task["id"], "## 题\n\n答", [])
        repository.update_task(task["id"], "completed")

        response = client.delete(f"/api/records/{record['id']}")
        assert response.status_code == 200
        assert client.get("/api/records").json()["records"] == []
