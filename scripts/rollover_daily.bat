@echo off
rem ============================================================
rem  收盘后自动更新换月复权表（交易机）
rem
rem  由 Windows 计划任务在每个交易日 17:30 调用：
rem    schtasks /Create /TN "vnpy_rollover_daily" ^
rem      /TR "\"C:\Users\29523\Desktop\vnpy\vnpy-domestic\scripts\rollover_daily.bat\"" ^
rem      /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 17:30 /F
rem
rem  日志：scripts\logs\rollover_update_YYYYMM.log
rem  通知：读 .vntrader\secrets.yaml 里的飞书/钉钉配置
rem ============================================================
setlocal
cd /d "%~dp0.."
set PY=C:\Users\29523\miniconda3\envs\vnpy\python.exe
if not exist "%PY%" set PY=python

"%PY%" "%~dp0rollover_daily.py" %*
exit /b %ERRORLEVEL%
