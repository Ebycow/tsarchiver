@echo off
rem _EDCBX_DIRECT_
echo ---- %date% %time% ---- >> "C:\path\to\tsarchiver\edcb_hook_debug.log"
set >> "C:\path\to\tsarchiver\edcb_hook_debug.log"
"C:\path\to\tsarchiver\.venv\Scripts\python.exe" "C:\path\to\tsarchiver\enqueue.py" "%FilePath%"
