# ============================================================
#  跟屏看桥的日志(logs\bridge.log)
#  ------------------------------------------------------------
#  为什么需要: 一键启动脚本用最小化窗口拉起桥, 事故现场(完整堆栈/断线原因)
#  以前只留在那个窗口里, 事后无从查证。现在桥把所有输出同时落盘,
#  本脚本就是它的"窗口"。
#
#  用法: powershell -NoProfile -ExecutionPolicy Bypass -File tools\watch-bridge-log.ps1
#        powershell ... -File tools\watch-bridge-log.ps1 -Tail 200     # 先看最后 200 行
#        powershell ... -File tools\watch-bridge-log.ps1 -Grep "断流|异常|落后"
#  结束: Ctrl+C
#  注意: 本文件必须保存为 UTF-8 with BOM, 否则 PowerShell 5.1 中文乱码
# ============================================================
param(
    [int]$Tail = 80,
    [string]$Grep = '',
    [string]$Path = ''
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
if (-not $Path) { $Path = Join-Path $root 'logs\bridge.log' }

if (-not (Test-Path $Path)) {
    Write-Host "  日志还不存在: $Path" -ForegroundColor Yellow
    Write-Host '  桥(None 启动过 / 没重启过)还没写日志 —— 用启动脚本 -Restart 重启一次桥即可。' -ForegroundColor DarkGray
    return
}

Write-Host ''
Write-Host '  ╔══════════════════════════════════════════════════════════╗' -ForegroundColor Cyan
Write-Host '  ║   组件版数字人桥 · 日志跟屏                              ║' -ForegroundColor Cyan
Write-Host '  ╚══════════════════════════════════════════════════════════╝' -ForegroundColor Cyan
Write-Host "  文件: $Path" -ForegroundColor Gray
if ($Grep) { Write-Host "  过滤: $Grep" -ForegroundColor Gray }
Write-Host '  关注关键词: 喂流落后 / 事件循环卡顿 / 断流 / 强制挂断 / WS 断开 / [exc] / ⚠' -ForegroundColor DarkGray
Write-Host '  (Ctrl+C 结束)' -ForegroundColor DarkGray
Write-Host ''

# 先打尾部历史(带上下文, 便于认定"断流发生在哪一步")
$hist = Get-Content $Path -Tail $Tail -Encoding UTF8
foreach ($line in $hist) {
    if ($Grep -and ($line -notmatch $Grep)) { continue }
    Write-Host $line
}

# 再持续跟随(轮询读取, 兼容 PS 5.1)
$pos = (Get-Item $Path).Length
while ($true) {
    Start-Sleep -Milliseconds 400
    if (-not (Test-Path $Path)) { continue }
    $len = (Get-Item $Path).Length
    if ($len -lt $pos) { $pos = 0 }            # 轮转了 → 从头读新文件
    if ($len -eq $pos) { continue }
    try {
        $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        $fs.Position = $pos
        $sr = New-Object IO.StreamReader($fs, [Text.Encoding]::UTF8)
        $text = $sr.ReadToEnd()
        $pos = $fs.Position
        $sr.Close(); $fs.Close()
    } catch { continue }
    foreach ($line in ($text -split "`r?`n")) {
        if (-not $line) { continue }
        if ($Grep -and ($line -notmatch $Grep)) { continue }
        if ($line -match '⚠|❌|\[exc\]|Traceback|断流|落') { Write-Host $line -ForegroundColor Yellow }
        else { Write-Host $line }
    }
}
