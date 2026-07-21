@echo off
REM Wrapper for hermes on_session_end hook.
REM Hermes pipes JSON payload to stdin (subprocess.run(input=...)).
REM cmd.exe conveys stdin to the child process via standard inheritance,
REM so we just invoke python.exe — it inherits our stdin.
"C:\Users\Bew\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe" "C:\Users\Bew\ZCodeProject\scripts\hermes-session-memory.py"
exit /b %ERRORLEVEL%
