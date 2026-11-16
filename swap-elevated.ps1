# Elevated swap, ASCII-only so PowerShell 5.1 can always parse it.
$ErrorActionPreference = 'Stop'
$root = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$src  = Join-Path $root 'dist_new\ZeroFall.exe'
$dst  = Join-Path $root 'dist\ZeroFall.exe'
$log  = Join-Path $root 'dist\logs\swap-elevated.log'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

function W([string]$m) { Add-Content -LiteralPath $log -Value ("{0} {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) -Encoding utf8 }

Set-Content -LiteralPath $log -Value ("=== swap {0} ===" -f $stamp) -Encoding utf8

try {
  W ("admin={0}" -f (New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))
  if (-not (Test-Path -LiteralPath $src)) { W 'FAIL: new exe missing'; exit 1 }

  W 'stop ZeroFall'
  Get-Process ZeroFall -ErrorAction SilentlyContinue | ForEach-Object {
    W ("  kill {0}" -f $_.Id)
    Stop-Process -Id $_.Id -Force
  }
  Start-Sleep -Seconds 4
  if (Get-Process ZeroFall -ErrorAction SilentlyContinue) { W 'FAIL: ZeroFall still alive'; exit 1 }

  W 'backup old exe'
  Copy-Item -LiteralPath $dst -Destination ("{0}.bak-before-swap-{1}" -f $dst, $stamp) -Force

  W 'copy new exe'
  Copy-Item -LiteralPath $src -Destination $dst -Force
  W ("  now {0:N0} bytes {1}" -f (Get-Item -LiteralPath $dst).Length, (Get-Item -LiteralPath $dst).LastWriteTime)

  W 'start'
  Start-Process -FilePath $dst -WorkingDirectory (Join-Path $root 'dist')
  Start-Sleep -Seconds 10
  $p = Get-Process ZeroFall -ErrorAction SilentlyContinue
  if (-not $p) {
    W 'FAIL: not started, rollback'
    Copy-Item -LiteralPath ("{0}.bak-before-swap-{1}" -f $dst, $stamp) -Destination $dst -Force
    Start-Process -FilePath $dst -WorkingDirectory (Join-Path $root 'dist')
    exit 1
  }
  $p | ForEach-Object { W ("  pid {0} {1} MB" -f $_.Id, [int]($_.WorkingSet64/1MB)) }
  W 'OK'
} catch {
  W ("EXCEPTION: {0}" -f $_.Exception.Message)
  exit 1
}
