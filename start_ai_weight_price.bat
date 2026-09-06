@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m erp.ai_weight_price %*
pause
