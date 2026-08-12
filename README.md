# OBS 面经转写平台

Windows 本地面试录音转写与整理平台：录制 OBS 双音轨面试/会议，自动提取音轨、转写、过滤幻觉，并用 Ollama 或 OpenAI 兼容 API（如 DeepSeek）整理成可编辑的面经问答。所有数据都保存在本地。

## 功能

- 扫描或拖拽导入 `.mkv` / `.mp4` / `.mov` / `.flv` / `.ts` 录制文件
- 自动分离「我 / 面试官」双音轨，faster-whisper（CPU）转写，支持自动、中文、英文
- 幻觉过滤、逐句时间戳、原始片段入库
- 整理模型可选 Ollama 或 OpenAI 兼容 API，每个问题带可折叠的「优质回答」编辑区
- 任务队列：提取 → 转写 → 整理，失败任务可保留中间产物并断点重试
- 整理完成后自动清理中间产物和上传的托管录像副本，只保留数据库里的面经文档（可随时重新整理）
- 单页本地网页，无需外部服务（整理 API 除外）

## 快速开始

方式一：双击 `启动面经平台.bat`。首次启动会自动创建虚拟环境、安装依赖并打开浏览器。

方式二（命令行）：

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements-windows.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

浏览器打开 <http://127.0.0.1:8000>。

## OBS 设置（一次性）

1. 在 OBS「高级音频属性」中：麦克风只勾选音轨 1，桌面/应用音频只勾选音轨 2。
2. 输出设置：录制格式选 **MKV**，并记下录制目录。
3. 在平台网页「本地设置」里填写该录制目录，保存。

## 使用流程

1. **导入**：扫描导入引用 OBS 录制目录里的原文件（不复制）；拖拽上传会把文件复制到 `data/managed-recordings/`。
2. **建任务**：每个文件生成一个任务，依次经过 `提取 → 转写 → 整理` 三个阶段。
3. **编辑文档**：整理完成后生成 Markdown 面经，可直接在网页编辑；重新整理或换模型可通过任务的「重试」完成。
4. **清理**：任务整理完成后自动删除工作目录和上传的托管录像副本，只保留数据库中的文档和原始片段（可重新整理）。

## 配置

### 应用内设置（网页「本地设置」）

| 字段 | 说明 | 默认 |
|---|---|---|
| `recording_directory` | OBS 录制目录（扫描导入用） | 空 |
| `local_concurrency` | 本地转写并发数 | 1 |
| `api_concurrency` | 整理 API 并发数 | 2 |
| `transcription_models` | 可选转写模型列表（faster-whisper） | small / medium / large-v3 |
| `semantic_models` | 可选整理模型列表 | deepseek-v4-flash / deepseek-v4-pro / qwen2.5:3b |
| `ollama_url` | Ollama 服务地址 | http://127.0.0.1:11434 |
| `openai_base_url` | OpenAI 兼容 API 地址 | https://api.openai.com/v1 |
| `openai_api_key` | API 密钥（仅存本地 SQLite，不写日志） | 空 |

### 环境变量（可选，见 [.env.example](.env.example)）

- `INTERVIEW_APP_DATA_DIR` / `INTERVIEW_APP_DATABASE` / `INTERVIEW_APP_MANAGED_DIR` / `INTERVIEW_APP_WORK_DIR`：覆盖数据目录、数据库、托管录像和工作目录路径
- `HF_HOME`：Hugging Face 模型缓存目录（`启动面经平台.bat` 固定为 `data\huggingface`）

## 数据流与存储

```
data/
├─ interviews.sqlite3         SQLite 数据库（WAL）
│   ├─ settings               应用设置（JSON）
│   ├─ recordings             录制文件登记（来源/托管路径/导入方式）
│   ├─ tasks                  任务状态与参数（阶段、重试阶段、错误信息）
│   └─ records                面经文档（Markdown、问题提纲、原始片段 JSON）
├─ managed-recordings/        上传导入的原始视频副本（整理完成后自动删除）
├─ work/<task-id>/            任务中间产物（wav/srt/raw_segments.json，完成后删除）
└─ huggingface/               faster-whisper 模型缓存
```

任务失败或取消时保留中间产物以便断点重试；任务完成后 `data/work/` 与上传副本都会被清理，重新整理直接复用数据库里的片段。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET / PUT | `/api/settings` | 读取 / 保存设置 |
| GET | `/api/imports/scan` | 扫描 OBS 录制目录 |
| POST | `/api/imports/upload` | 上传录制文件（multipart） |
| GET | `/api/tasks` | 任务列表 |
| POST | `/api/tasks` | 扫描文件建任务 |
| POST | `/api/tasks/{id}/retry` | 重试 / 重新整理 |
| POST | `/api/tasks/{id}/cancel` | 取消任务 |
| DELETE | `/api/tasks/{id}` | 删除任务（文档保留） |
| GET / PUT / DELETE | `/api/records...` | 面经文档查询 / 编辑 / 删除 |

## 目录结构

```
app/                      FastAPI 后端（main / db / repository / workers / services）
app/static/               前端页面
scripts/process_mkv.py    音轨提取、转写、过滤原语（被 app 复用）
scripts/filter_hallucinations.py  幻觉过滤
tests/                    pytest 测试
data/                     运行数据（不入库）
```

## 测试

```powershell
pytest
```

## 常见问题

- **提示 ffmpeg 未安装**：将 FFmpeg 加入 PATH 后重启终端。
- **提示音轨数量不足**：确认 OBS 里麦克风在音轨 1、桌面/应用音频在音轨 2，且录制格式为 MKV。
- **模型加载失败/卡住**：优先尝试本地缓存离线加载；联网加载超时 90 秒会自动中止，可检查代理或删除不完整缓存后重试。
- **整理失败**：确认 Ollama 已启动（`ollama_url`）或 OpenAI 兼容 API 地址、密钥、模型名正确；失败任务可修改设置后重试整理。
- **删除任务/文档**：删除只影响数据库记录，不会删除磁盘上的原始录像（扫描导入的原文件始终由你管理）。
