@echo off
title AGENTE DE LANCAMENTOS CONTABEIS
REM Sobe o painel web a partir da pasta do projeto (a pasta acima desta).
REM Substitui o antigo atalho do .exe (aposentado em 2026-09-30).
cd /d "%~dp0.."

set PYTHON=python
if exist "venv\Scripts\python.exe" set PYTHON=venv\Scripts\python.exe

echo ===================================================
echo   Agente Contabil (Empresa) - painel web
echo   Abra no navegador: http://127.0.0.1:8000
echo   Os documentos so sao processados quando alguem
echo   clica em "Processar entrada" na pagina.
echo   NAO FECHE ESTA JANELA - fechar encerra o painel.
echo ===================================================
echo.

REM Abre o navegador quando o servidor ja estiver de pe.
start "" /b cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8000"

%PYTHON% -m uvicorn src.web.app:app --host 127.0.0.1 --port 8000

pause
