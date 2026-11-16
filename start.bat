@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo.
echo ==========================================
echo   CrossBorder Ops Agent
echo ==========================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] 创建虚拟环境...
    python -m venv .venv
    if errorlevel 1 goto fail
) else (
    echo [1/3] 虚拟环境已就绪
)

echo [2/3] 检查依赖...
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, pydantic, multipart" >nul 2>&1
if errorlevel 1 (
    echo       正在安装依赖，首次可能需要几分钟...
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
    if errorlevel 1 goto fail
) else (
    echo       依赖完整
)

if not exist "data\seed\ads.csv" (
    echo [3/3] 生成演示数据...
    ".venv\Scripts\python.exe" scripts\gen_seed.py
    if errorlevel 1 goto fail
) else (
    echo [3/3] 演示数据已就绪
)

if /i "%~1"=="--check" (
    echo.
    echo 环境检查通过，可以启动。
    exit /b 0
)

echo.
echo ------------------------------------------
echo   访问地址: http://127.0.0.1:8000
echo   停止服务: 按 Ctrl+C
echo ------------------------------------------
echo.
".venv\Scripts\python.exe" run.py
goto end

:fail
echo.
echo [失败] 启动中断，请检查上面的错误信息。
echo        常见原因：未安装 Python 3.10+、网络无法访问 PyPI。
pause
exit /b 1

:end
endlocal
