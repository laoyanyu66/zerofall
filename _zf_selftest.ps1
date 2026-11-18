param(
    # 只跑指定的用例（按文件名，不带 .py）。改完一两处想快速复验时省 10 分钟：
    #   .\_zf_selftest.ps1 -Only test_profile_health,test_fix_only
    [string[]]$Only
)
$ErrorActionPreference = 'Continue'
# ZeroFall 全量自检跑批（显式解释器：PATH 里的 python 已被运行时顶掉）
#
# 重要：本文件必须存成「UTF-8 带 BOM」。
# Windows PowerShell 5.1 读无 BOM 的 UTF-8 会按 GBK 解释中文字符串，
# 直接把引号吃掉，报 The string is missing the terminator: " 之类的解析错误。
# _zf_selftest.cmd 会在落到 5.1 之前自己把 BOM 补上，所以从 .cmd 起不会踩这个坑；
# 直接双击 .ps1 或用 pwsh 7 跑也没问题。只有"用 5.1 直接 -File 本文件"才需要留意。
# 项目根从脚本自身位置推导，别写死绝对路径（换机器 / 挪目录就失效）。
$root = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$PY = $env:ZF_PYTHON
if (-not $PY -or -not (Test-Path $PY)) {
    # 不写死某个用户的安装路径：先按常见位置探一遍，探不到再退回 PATH。
    $cands = @()
    if ($env:LOCALAPPDATA) {
        foreach ($v in 'Python313', 'Python312', 'Python311', 'Python310') {
            $cands += (Join-Path $env:LOCALAPPDATA ('Programs\Python\' + $v + '\python.exe'))
        }
    }
    foreach ($v in 'Python313', 'Python312', 'Python311') {
        $cands += ('C:\' + $v + '\python.exe')
    }
    foreach ($c in $cands) {
        if (Test-Path $c) { $PY = $c; break }
    }
}
if (-not $PY -or -not (Test-Path $PY)) {
    # PATH 上的 python 被 Doubao 运行时顶掉了，所以不能直接写 python。
    # 私有安装找不到时退回到 PATH，至少让人看到真错误而不是"命令不存在"。
    $PY = (Get-Command python -ErrorAction SilentlyContinue).Source
}
$logDir = Join-Path $root 'logs\_selftest'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$env:QT_QPA_PLATFORM = 'offscreen'
$env:PYTHONIOENCODING = 'utf-8'

$files = Get-ChildItem (Join-Path $root 'tests') -Filter 'test_*.py' | Sort-Object Name
if ($Only) {
    # 用 -File 从命令行传 `-Only a,b` 时，PowerShell 把整串当成**一个**字符串，
    # 不会按逗号拆成数组 —— 这里自己拆，两种传法都能用。
    $onlyNames = @()
    foreach ($one in $Only) { $onlyNames += ($one -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }
    $files = @($files | Where-Object { $onlyNames -contains $_.BaseName })
    if (-not $files) {
        Write-Host ("没有匹配的用例: " + ($onlyNames -join ', '))
        exit 2
    }
}
# 单测超时：默认 240s。但 test_fix_only 要真跑一次 ACP 修复会话，
# 它内部的 prompt 超时是 1500s —— 用 240s 去卡会把"模型慢"误判成 TIMEOUT。
$defaultTimeoutSec = 240
$timeoutOverrides = @{ 'test_fix_only' = 600; 'test_e2e_push_update' = 900 }

# 为什么不用 Start-Process：Windows PowerShell 5.1 里 -PassThru 返回的对象**没有进程句柄**，
# ExitCode 永远是空。于是 58 个用例全被判成"失败"，可输出里明明写着"失败 0 项" ——
# 不是代码回归，是取退出码的方式错了。实测三种写法：
#   Start-Process -PassThru              → ExitCode 空（无论是否再调 WaitForExit / 摸 .Handle）
#   Start-Process -PassThru -Wait        → ExitCode 正确，但没有超时控制，跑飞的用例会挂死
#   [System.Diagnostics.Process] 自己起  → ExitCode 正确，且 WaitForExit(毫秒) 仍有超时
# 所以这里走第三种。输出必须异步读：子进程写满管道缓冲区会死锁，
# 那些输出几万行的用例（UI/快照类）会直接挂住到超时。
function Invoke-TestProcess {
    param(
        [string]$Exe,
        [string]$ArgLine,
        [string]$WorkDir,
        [string]$OutFile,
        [string]$ErrFile,
        [int]$TimeoutSec
    )
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    $psi.Arguments = $ArgLine
    $psi.WorkingDirectory = $WorkDir
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = $utf8
    $psi.StandardErrorEncoding = $utf8

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    [void]$proc.Start()
    $tOut = $proc.StandardOutput.ReadToEndAsync()
    $tErr = $proc.StandardError.ReadToEndAsync()

    $finished = $proc.WaitForExit($TimeoutSec * 1000)
    if (-not $finished) {
        try { $proc.Kill() } catch {}
        $code = 'TIMEOUT'
    } else {
        $code = $proc.ExitCode
    }
    $outText = ''
    $errText = ''
    try { $outText = $tOut.GetAwaiter().GetResult() } catch {}
    try { $errText = $tErr.GetAwaiter().GetResult() } catch {}
    try { [System.IO.File]::WriteAllText($OutFile, $outText, $utf8) } catch {}
    try { [System.IO.File]::WriteAllText($ErrFile, $errText, $utf8) } catch {}
    try { $proc.Dispose() } catch {}
    return $code
}

$results = @()
$i = 0
foreach ($f in $files) {
    $i++
    $name = $f.BaseName
    $limit = if ($timeoutOverrides.ContainsKey($name)) { $timeoutOverrides[$name] } else { $defaultTimeoutSec }
    $out = Join-Path $logDir ($name + '.out.txt')
    $err = Join-Path $logDir ($name + '.err.txt')
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $argLine = '"' + $f.FullName + '"'
    $code = Invoke-TestProcess -Exe $PY -ArgLine $argLine -WorkDir $root `
        -OutFile $out -ErrFile $err -TimeoutSec $limit
    $sw.Stop()
    $txt = if (Test-Path $out) { Get-Content $out -Raw -Encoding utf8 } else { '' }
    $etxt = if (Test-Path $err) { Get-Content $err -Raw -Encoding utf8 } else { '' }
    # SKIP 是环境缺件（上游模型不可用 / 没凭据），不是代码回归：
    # 用例自己打印「判定: SKIP ...」并以 0 退出。这里单列出来，别混进通过数。
    $skip = ($code -eq 0) -and ($txt -match '判定:\s*SKIP')
    $m = [regex]::Matches($txt, '(通过\s*\d+\s*项?\s*/\s*失败\s*\d+\s*项?|通过\s*\d+\s*/\s*\d+\s*项|结果：\s*\d+\s*通过\s*/\s*\d+\s*失败|\d+\s*通过\s*/\s*\d+\s*失败)')
    $summary = if ($m.Count -gt 0) { $m[$m.Count - 1].Value } elseif ($skip) { 'SKIP' } elseif ($code -eq 0) { 'OK' } else { 'NO-SUMMARY' }
    $results += [pscustomobject]@{
        idx     = $i
        test    = $name
        exit    = $code
        skip    = $skip
        summary = $summary
        secs    = [math]::Round($sw.Elapsed.TotalSeconds, 1)
        errTail = (($etxt -split "`n" | Where-Object { $_.Trim() } | Select-Object -Last 3) -join ' | ')
    }
    Write-Host ("[{0}/{1}] {2,-34} exit={3,-8} {4,-24} {5}s" -f $i, $files.Count, $name, $code, $summary, [math]::Round($sw.Elapsed.TotalSeconds,1))
}

$results | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $logDir '_results.json') -Encoding utf8
# @() 是必须的：只有 1 条红的时，管道会把结果展开成单个对象，
# 5.1 里标量没有 .Count —— 汇总行就会印成"红/超时  个"（数字空着）。
$bad = @($results | Where-Object { $_.exit -ne 0 })
$skips = @($results | Where-Object { $_.skip })
Write-Host "`n===== DONE ====="
Write-Host ("总 {0} 个，红/超时 {1} 个，SKIP {2} 个" -f $results.Count, $bad.Count, $skips.Count)
if ($bad.Count) { $bad | Format-Table -AutoSize idx, test, exit, summary, errTail | Out-String -Width 220 | Write-Host }
if ($skips.Count) { Write-Host ("SKIP（环境缺件，非代码回归）: " + (($skips | ForEach-Object { $_.test }) -join ', ')) }
