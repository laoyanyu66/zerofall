# 把 dist_new\ZeroFall.exe 换上并重启（需要管理员：运行中的 ZeroFall 是提权进程）
$ErrorActionPreference = 'Stop'
$root = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$src = Join-Path $root 'dist_new\ZeroFall.exe'
$dst = Join-Path $root 'dist\ZeroFall.exe'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
Write-Host ("权限：管理员={0}" -f $isAdmin)
if (-not (Test-Path $src)) { Write-Host "[失败] 找不到新构建：$src"; exit 1 }

Write-Host '[1/5] 关闭 ZeroFall'
Get-Process ZeroFall -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host ("      停止 PID {0}" -f $_.Id)
    Stop-Process -Id $_.Id -Force
}
Start-Sleep -Seconds 3
if (Get-Process ZeroFall -ErrorAction SilentlyContinue) {
    Write-Host '[失败] 还有 ZeroFall 进程没退，先手动关掉再跑'
    exit 1
}

Write-Host '[2/5] 备份旧 exe'
$bak = "$dst.bak-before-selfcheck-$stamp"
Copy-Item $dst $bak
Write-Host "      $bak"

# 每次交换留一份 40 MB 备份，不清理一个月就攒出几百 MB（实测 12 个 / 485 MB）。
# 只留最新 3 份，够回滚到最近三次；更早的没有保留价值。
$baks = Get-ChildItem (Split-Path $dst) -Filter 'ZeroFall.exe.bak-*' |
        Sort-Object LastWriteTime -Descending
if ($baks.Count -gt 3) {
    $drop = $baks | Select-Object -Skip 3
    $drop | ForEach-Object { Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue }
    $freed = [math]::Round((($drop | Measure-Object Length -Sum).Sum / 1MB), 0)
    Write-Host ("      清掉旧备份 {0} 个 / {1} MB（保留最新 3 份）" -f $drop.Count, $freed)
}

Write-Host '[3/5] 换入新 exe'
Copy-Item $src $dst -Force
Get-Item $dst | ForEach-Object { Write-Host ("      {0:N0} 字节  {1}" -f $_.Length, $_.LastWriteTime) }

Write-Host '[4/5] 启动'
Start-Process -FilePath $dst -WorkingDirectory (Join-Path $root 'dist')
Start-Sleep -Seconds 8
$p = Get-Process ZeroFall -ErrorAction SilentlyContinue
if (-not $p) { Write-Host '[失败] 没起来，回滚'; Copy-Item $bak $dst -Force; exit 1 }
$p | Format-Table Id, @{n='MB';e={[int]($_.WorkingSet64/1MB)}}, StartTime -AutoSize

Write-Host '[5/5] 看日志确认新逻辑进来'
# 注意：早期版本写成 Join-Path $root '..._' + (Get-Date) + '.log'，
# Join-Path 只吃两个参数，后面变成数组拼接，路径里会混进 '+' 和多余的日期段，
# 于是第 5 步永远报 "Cannot find path ...\zerofall_\+2026...\.log"（换版其实已经成功）。
# 新 exe 已经起来了，这行再报错只会误导人以为失败。
$log = Join-Path $root ('dist\logs\zerofall_' + (Get-Date -Format 'yyyyMMdd') + '.log')
Start-Sleep -Seconds 5
Get-Content $log -Tail 8
Write-Host ''
Write-Host '完成。等 5-10 分钟看是否出现「[变更检测] 差异前 6 条」，那一行会直接点名谁在变。'
