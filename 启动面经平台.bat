@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 面经转录平台

if not exist "venv\Scripts\python.exe" (
  echo 未找到虚拟环境 venv，正在创建...
  python -m venv venv
  if errorlevel 1 (
    echo 创建失败，请确认已安装 Python 并加入 PATH。
    pause
    exit /b 1
  )
)

"venv\Scripts\python.exe" -c "import fastapi, uvicorn, multipart" >nul 2>&1
if errorlevel 1 (
  echo 缺少面经平台依赖，正在自动安装...
  "venv\Scripts\python.exe" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn -r requirements-windows.txt
  if errorlevel 1 (
    echo 安装失败。可手动执行：
    echo venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn -r requirements-windows.txt
    pause
    exit /b 1
  )
)

set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
rem 将 Hugging Face 模型缓存固定放在 D 盘；首次迁移后目录应为 data\huggingface\hub\...
set "HF_HOME=%~dp0data\huggingface"
start "" "http://127.0.0.1:8000"
echo 正在启动面经转录平台，请勿关闭本窗口。
"venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
