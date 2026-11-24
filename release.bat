@echo off
rem Publica uma nova versao no GitHub Releases. Os apps instalados acham
rem sozinhos e se atualizam.
rem   1. aumente VERSION em version.py e faca commit
rem   2. rode: release.bat            (notas geradas pelos commits)
rem      ou:   release.bat "o que mudou nesta versao"
cd /d "%~dp0"

for /f "delims=" %%V in ('python -c "from version import VERSION; print(VERSION)"') do set VERSION=%%V
if "%VERSION%"=="" goto :erro
set TAG=v%VERSION%

git diff --quiet HEAD || (echo [ERRO] Ha mudancas sem commit. & goto :erro)
gh release view %TAG% >nul 2>&1 && (echo [ERRO] A release %TAG% ja existe. Aumente VERSION em version.py. & goto :erro)

call build.bat || goto :erro
certutil -hashfile dist\Downme.exe SHA256 | findstr /r "^[0-9a-f]*$" > dist\Downme.exe.sha256 || goto :erro

git push origin HEAD || goto :erro
if "%~1"=="" (
  gh release create %TAG% dist\Downme.exe dist\Downme.exe.sha256 --title "Downme %VERSION%" --generate-notes || goto :erro
) else (
  gh release create %TAG% dist\Downme.exe dist\Downme.exe.sha256 --title "Downme %VERSION%" --notes "%~1" || goto :erro
)
echo.
echo Release %TAG% publicada.
exit /b 0

:erro
echo.
echo [ERRO] A release falhou.
exit /b 1
