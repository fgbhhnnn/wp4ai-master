@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "SCRIPT_DIR=%~dp0"
set "PWSH_EXE="
set "NO_PAUSE=0"
set "BUILD_ARGS="

:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="--no-pause" (
  set "NO_PAUSE=1"
) else (
  set BUILD_ARGS=%BUILD_ARGS% "%~1"
)
shift
goto parse_args

:args_done
for /f "delims=" %%I in ('where pwsh 2^>nul') do (
  set "PWSH_EXE=%%I"
  goto :pwsh_found
)

if exist "%ProgramFiles%\PowerShell\7\pwsh.exe" set "PWSH_EXE=%ProgramFiles%\PowerShell\7\pwsh.exe"
if not defined PWSH_EXE if exist "%ProgramW6432%\PowerShell\7\pwsh.exe" set "PWSH_EXE=%ProgramW6432%\PowerShell\7\pwsh.exe"
if not defined PWSH_EXE if exist "%LocalAppData%\Microsoft\WindowsApps\pwsh.exe" set "PWSH_EXE=%LocalAppData%\Microsoft\WindowsApps\pwsh.exe"

:pwsh_found
if not defined PWSH_EXE (
  echo.
  echo [ERROR] PowerShell 7 pwsh.exe was not found.
  echo [HINT ] Install PowerShell 7, then reopen terminal and run again.
  echo [HINT ] Typical path: "%ProgramFiles%\PowerShell\7\pwsh.exe"
  goto :failed
)

echo [INFO ] Using PowerShell: "%PWSH_EXE%"
echo [INFO ] Running script: "%SCRIPT_DIR%build.ps1"
if defined BUILD_ARGS (
  echo [INFO ] Forward args:%BUILD_ARGS%
)
echo.

"%PWSH_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%build.ps1" %BUILD_ARGS%
if errorlevel 1 (
  echo.
  echo [ERROR] Build failed, check logs above.
  goto :failed
)

echo.
echo [OK] Build completed.
goto :done

:failed
if "%NO_PAUSE%"=="1" (
  exit /b 1
)
pause
exit /b 1

:done
if "%NO_PAUSE%"=="1" (
  exit /b 0
)
pause
exit /b 0
