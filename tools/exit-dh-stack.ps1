# exit-dh-stack.ps1 - 数字人全链路一键退出
# 范围(仅直播组件版链路, 不碰其他项目):
#   [0] 优雅挂断: 桥还活着时先 POST /api/live/stop (避免 Vidu 侧会话悬空计费)
#   [1] 桥: python -m src.main --no-browser (任意 python, 含残留双实例)
#   [2] 弹幕 reader: multi-danmaku-tts reader.py + tray.ps1 + tts_worker.py
#   [3] douyinLive.exe (抖音弹幕本地服务)
#   [4] 调试 Edge (CDP 9223, 视频号中控台)
#   [5] OmniVoice TTS (WSL2 demo_flashinfer.py, 释放显存)
# 故意不动: DSH(3080) / DSH语音桥(8765) / NapCat / Docker(DUIX) / WSL 本体
# 用法:  exit-dh-stack.ps1            真退出
#        exit-dh-stack.ps1 -ListOnly  只看会杀什么, 不杀
param([switch]$ListOnly)

$ErrorActionPreference = "Continue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

function Test-Port([int]$port) {
    try {
        $c = New-Object Net.Sockets.TcpClient
        $iar = $c.BeginConnect('127.0.0.1', $port, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(1200, $false)
        if ($ok -and $c.Connected) { $c.Close(); return $true }
        $c.Close(); return $false
    } catch { return $false }
}

Write-Host ""
Write-Host "=== 数字人全链路 退出$(if($ListOnly){' [预览模式 -ListOnly]'}) ===" -ForegroundColor Cyan

# ── [0] 优雅挂断(先礼后兵) ─────────────────────────────────────
if (Test-Port 8795) {
    if ($ListOnly) {
        Write-Host "[0] 桥在线(:8795) -> 将先发 POST /api/live/stop 优雅挂断" -ForegroundColor Yellow
    } else {
        try {
            Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8795/api/live/stop' -TimeoutSec 4 | Out-Null
            Write-Host "[0] 已发优雅挂断 /api/live/stop (Vidu 会话正常收尾)" -ForegroundColor Green
            Start-Sleep -Seconds 2
        } catch { Write-Host "[0] 挂断请求失败($($_.Exception.Message)), 直接强杀" -ForegroundColor Yellow }
    }
} else {
    Write-Host "[0] 桥未运行(:8795 无监听), 跳过优雅挂断" -ForegroundColor DarkGray
}

# ── [1-4] Windows 侧进程 ──────────────────────────────────────
# (label, 判定函数);  只按命令行精确匹配, 防误杀无关 python
$procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
$kill = New-Object System.Collections.Generic.List[object]
foreach ($p in $procs) {
    $cl = [string]$p.CommandLine; $nm = [string]$p.Name
    if     ($cl -match '-m\s+src\.main' -or $cl -match 'src\.main.*--no-browser') { $kill.Add(@{P=$p; Tag='数字人桥 (vidu)'}) }
    elseif ($nm -match '^(python|pwsh|powershell)\.exe$' -and $cl -match 'multi-danmaku-tts.*reader\.py|reader\.py') { $kill.Add(@{P=$p; Tag='弹幕 reader'}) }
    elseif ($nm -match '^(python|pwsh|powershell)\.exe$' -and $cl -match 'tts_worker\.py') { $kill.Add(@{P=$p; Tag='本地 TTS worker'}) }
    elseif ($nm -match '^(python|pwsh|powershell)\.exe$' -and $cl -match 'tray\.ps1') { $kill.Add(@{P=$p; Tag='弹幕托盘'}) }
    elseif ($nm -match '^(pwsh|powershell)\.exe$' -and $cl -match 'watch-wechat\.ps1|watch-bridge-log\.ps1') { $kill.Add(@{P=$p; Tag='监控脚本'}) }
    elseif ($nm -eq 'douyinLive.exe') { $kill.Add(@{P=$p; Tag='douyinLive (抖音弹幕服务)'}) }
    elseif ($nm -match 'msedge\.exe' -and $cl -match 'remote-debugging-port=9223') { $kill.Add(@{P=$p; Tag='调试 Edge (CDP 9223, 视频号)'}) }
}

if ($kill.Count -eq 0) {
    Write-Host "[1-4] Windows 侧: 没有发现链路进程(都已退出)" -ForegroundColor DarkGray
} else {
    foreach ($k in $kill) {
        $p = $k.P; $desc = "{0}  PID={1}" -f $k.Tag, $p.ProcessId
        if ($ListOnly) {
            Write-Host ("[杀] " + $desc) -ForegroundColor Yellow
        } else {
            taskkill /PID $p.ProcessId /T /F | Out-Null
            if ($LASTEXITCODE -eq 0) { Write-Host ("[√] 已退出 " + $desc) -ForegroundColor Green }
            else { Write-Host ("[X] 退出失败 " + $desc) -ForegroundColor Red }
        }
    }
}

# ── [5] WSL2 OmniVoice TTS ────────────────────────────────────
if ($ListOnly) {
    if (Test-Port 9877) { Write-Host "[杀] OmniVoice TTS (WSL2 :9877, demo_flashinfer.py)" -ForegroundColor Yellow }
    else { Write-Host "[5] OmniVoice TTS 未运行(:9877 无监听)" -ForegroundColor DarkGray }
} else {
    if (Test-Port 9877) {
        wsl -d Ubuntu-22.04 -- bash -lc "pkill -f demo_flashinfer.py; exit 0" 2>$null | Out-Null
        Start-Sleep -Seconds 2
        if (Test-Port 9877) { Write-Host "[X] OmniVoice 还没退干净(:9877 仍监听), 手动查: wsl pkill -9 -f demo_flashinfer.py" -ForegroundColor Red }
        else { Write-Host "[√] OmniVoice TTS 已退出 (WSL2, 显存随进程释放)" -ForegroundColor Green }
    } else {
        Write-Host "[5] OmniVoice TTS 未运行(:9877 无监听)" -ForegroundColor DarkGray
    }
}

# ── 汇总 ─────────────────────────────────────────────────────
if (-not $ListOnly) {
    Start-Sleep -Seconds 1
    $left = @()
    if (Test-Port 8795) { $left += '桥 :8795' }
    if (Test-Port 9877) { $left += 'OmniVoice :9877' }
    if ($left.Count -eq 0) { Write-Host "`n=== 全链路已退出 ✓ (8795 / 9877 均已释放) ===" -ForegroundColor Cyan }
    else { Write-Host ("`n=== 仍有残留: " + ($left -join ', ') + " ===") -ForegroundColor Red }
}
Write-Host ""
Write-Host "未触碰: DSH(3080) / DSH语音桥(8765) / NapCat / Docker(DUIX) / WSL 本体" -ForegroundColor DarkGray
