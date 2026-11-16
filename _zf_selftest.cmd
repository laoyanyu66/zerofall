@echo off
rem ---------------------------------------------------------------------------
rem ZeroFall full self-test launcher.
rem
rem Why this file exists: the runner it starts (_zf_selftest.ps1) carries Chinese
rem comments, so Windows PowerShell 5.1 needs it saved as UTF-8 WITH BOM. Without
rem the BOM, 5.1 decodes it as GBK, eats a quote and dies with
rem   The string is missing the terminator: "
rem Editors and patch tools silently strip that BOM, which makes the runner fail
rem for a reason that has nothing to do with the tests. So this launcher
rem re-attaches the BOM itself before falling back to 5.1. pwsh (7+) reads
rem UTF-8 without a BOM correctly, so it skips the fix-up.
rem
rem Everything below is addressed by absolute path under %SystemRoot%. This is not
rem paranoia: on this machine PATH is replaced by a bundled runtime, and neither
rem `where` nor `powershell` nor `cmd` resolves through it. A launcher that trusts
rem PATH just reports "not recognized" instead of running the tests.
rem
rem Keep THIS file pure ASCII and BOM-free so it survives any encoding round-trip.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "SYS32=%SystemRoot%\System32"
if not exist "%SYS32%\cmd.exe" set "SYS32=C:\Windows\System32"
set "PS51=%SYS32%\WindowsPowerShell\v1.0\powershell.exe"
set "RUNNER=%~dp0_zf_selftest.ps1"

if not exist "%RUNNER%" (
  echo [launcher] runner not found: %RUNNER%
  exit /b 1
)

rem --- prefer pwsh 7+: it reads UTF-8 without a BOM --------------------------
set "PWSH="
if exist "%ProgramFiles%\PowerShell\7\pwsh.exe" set "PWSH=%ProgramFiles%\PowerShell\7\pwsh.exe"
if not defined PWSH if exist "%SYS32%\..\..\Program Files\PowerShell\7\pwsh.exe" set "PWSH=%SYS32%\..\..\Program Files\PowerShell\7\pwsh.exe"

if defined PWSH (
  "%PWSH%" -NoProfile -ExecutionPolicy Bypass -File "%RUNNER%" %*
  exit /b %errorlevel%
)

rem --- 5.1 fallback: make sure the runner starts with EF BB BF ---------------
if not exist "%PS51%" (
  echo [launcher] neither pwsh 7 nor Windows PowerShell 5.1 found; cannot run.
  exit /b 1
)

set "ZF_RUNNER=%RUNNER%"
"%PS51%" -NoProfile -ExecutionPolicy Bypass -Command "$p=$env:ZF_RUNNER; $b=[System.IO.File]::ReadAllBytes($p); if ($b.Length -lt 3 -or $b[0] -ne 0xEF -or $b[1] -ne 0xBB -or $b[2] -ne 0xBF) { [System.IO.File]::WriteAllBytes($p, [byte[]](0xEF,0xBB,0xBF) + $b); Write-Host '[launcher] runner had no UTF-8 BOM; re-attached it' }"
if errorlevel 1 (
  echo [launcher] could not verify the runner encoding - aborting instead of
  echo [launcher] letting PowerShell 5.1 misparse Chinese comments as GBK.
  exit /b 1
)

"%PS51%" -NoProfile -ExecutionPolicy Bypass -File "%RUNNER%" %*
exit /b %errorlevel%
