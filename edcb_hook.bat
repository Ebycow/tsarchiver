@echo off
rem _EDCBX_DIRECT_
echo ---- %date% %time% ---- >> "%~dp0edcb_hook_debug.log"
set >> "%~dp0edcb_hook_debug.log"
"%~dp0.venv\Scripts\python.exe" "%~dp0enqueue.py" "%FilePath%"
