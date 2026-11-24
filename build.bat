@echo off
rem Gera dist\Downme.exe: um arquivo unico, roda em qualquer PC Windows
rem sem precisar de Python, Node ou ffmpeg instalados.
cd /d "%~dp0"
if exist dist rmdir /s /q dist

if not exist .venv (
  python -m venv .venv || goto :erro
)
.venv\Scripts\python -m pip install -q -r requirements.txt -r requirements-build.txt || goto :erro

rem --- binarios embutidos: ffmpeg (compacto, via pip) e node (runtime JS do yt-dlp)
if exist build\bin rmdir /s /q build\bin
mkdir build\bin
for /f "delims=" %%F in ('.venv\Scripts\python -c "import imageio_ffmpeg as f; print(f.get_ffmpeg_exe())"') do copy /y "%%F" build\bin\ffmpeg.exe >nul || goto :erro
for /f "delims=" %%N in ('where node') do (copy /y "%%N" build\bin\node.exe >nul & goto :node_ok)
echo [ERRO] Node.js nao encontrado. Instale em https://nodejs.org e rode de novo.
goto :erro
:node_ok

if not exist assets\icon.ico .venv\Scripts\python tools\make_icon.py || goto :erro

.venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed --name Downme ^
  --icon assets\icon.ico ^
  --add-data "static;static" ^
  --add-binary "build\bin\ffmpeg.exe;bin" ^
  --add-binary "build\bin\node.exe;bin" ^
  --collect-all yt_dlp_ejs ^
  desktop.py || goto :erro

echo.
echo Pronto: dist\Downme.exe
exit /b 0

:erro
echo.
echo [ERRO] O build falhou.
exit /b 1
