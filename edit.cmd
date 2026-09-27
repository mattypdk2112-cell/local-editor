@echo off
rem Windows launcher. Usage: edit.cmd <input.mp4> [options]
rem Runs setup the first time, then the editor through its own virtualenv.
setlocal
set "HERE=%~dp0"
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
rem Transcripts and captions carry curly quotes and emoji. Without UTF-8 mode,
rem Python on Windows reads and writes text files as cp1252 and dies on them.
set "PYTHONUTF8=1"

if not exist "%HERE%.venv\Scripts\python.exe" (
  echo First run, setting up. This takes a couple of minutes, once.
  echo.
  call "%HERE%setup.cmd" || exit /b 1
  echo.
)

where ffmpeg >nul 2>nul || (
  echo error: ffmpeg is not installed, and the editor cannot cut video without it. 1>&2
  echo Run setup.cmd to install it. 1>&2
  exit /b 1
)

"%HERE%.venv\Scripts\python.exe" "%HERE%edit.py" %*
