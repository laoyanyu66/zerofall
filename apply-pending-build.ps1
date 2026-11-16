# Apply pending build at logon, then launch ZeroFall.
# Runs BEFORE the app starts, so dist\ZeroFall.exe is not locked yet.
# ASCII-only on purpose: Windows PowerShell 5.1 may read this as ANSI.
$ErrorActionPreference = 'SilentlyContinue'
$root = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$new  = Join-Path $root 'dist_new\ZeroFall.exe'
$old  = Join-Path $root 'dist\ZeroFall.exe'
$log  = Join-Path $root 'dist\logs\autoswap.log'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

function W([string]$m) {
    Add-Content -LiteralPath $log -Value ("{0} {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) -Encoding utf8
}

W '--- autoswap check ---'
if (Test-Path -LiteralPath $new) {
    if (-not (Get-Process ZeroFall -ErrorAction SilentlyContinue)) {
        W 'stop: none running, safe to copy'
        Copy-Item -LiteralPath $old -Destination ("{0}.bak-autoswap-{1}" -f $old, $stamp) -Force
        Copy-Item -LiteralPath $new -Destination $old -Force
        $szOld = (Get-Item -LiteralPath $old).Length
        $szNew = (Get-Item -LiteralPath $new).Length
        if ($szOld -eq $szNew) {
            Move-Item -LiteralPath $new -Destination ("{0}.applied-{1}" -f $new, $stamp) -Force
            W ("APPLIED {0:N0} bytes" -f $szOld)
        } else {
            W ("COPY-MISMATCH old={0} new={1}" -f $szOld, $szNew)
        }
    } else {
        W 'ZeroFall running, skip copy (will retry next logon)'
    }
} else {
    W 'no pending build'
}
Start-Process -FilePath $old -WorkingDirectory (Join-Path $root 'dist')
