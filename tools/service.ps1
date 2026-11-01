param(
    [ValidateSet('start', 'stop', 'restart', 'status')]
    [string]$Action = 'start',
    [switch]$NoBrowser,
    [switch]$OnlyWhenIdle
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExecutable = Join-Path $projectRoot '.venv\Scripts\python.exe'
$configFile = Join-Path $projectRoot 'config.json'
$stateFile = Join-Path $projectRoot 'logs\service.json'
$logFile = Join-Path $projectRoot 'logs\bridge.log'
$config = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
$port = if ($config.bridge.port) { [int]$config.bridge.port } else { 8795 }
$baseUrl = 'http://127.0.0.1:{0}' -f $port
$launchArguments = '-u -m src.main --no-browser --host 127.0.0.1 --port {0}' -f $port

function Get-LocalJson([string]$url) {
    $request = [System.Net.HttpWebRequest]::Create($url)
    $request.Proxy = $null
    $request.Timeout = 2000
    $response = $request.GetResponse()
    try {
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
        try { return ($reader.ReadToEnd() | ConvertFrom-Json) }
        finally { $reader.Dispose() }
    } finally { $response.Dispose() }
}

function Get-ProjectHealth {
    try {
        $health = Get-LocalJson ($baseUrl + '/api/health')
        if ($health.ok -and $health.log -and
            [IO.Path]::GetFullPath([string]$health.log) -ieq [IO.Path]::GetFullPath($logFile)) {
            return $health
        }
    } catch { }
    return $null
}

function Get-OwnedProcess {
    if (-not (Test-Path -LiteralPath $stateFile)) { return $null }
    $state = Get-Content -LiteralPath $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $serviceProcess = Get-CimInstance Win32_Process -Filter ('ProcessId={0}' -f [int]$state.pid)
    if ($null -eq $serviceProcess) { return $null }
    if ($serviceProcess.Name -ine 'python.exe' -or
        $serviceProcess.ExecutablePath -ine $pythonExecutable -or
        $serviceProcess.CommandLine -notlike ('*' + $launchArguments + '*') -or
        $serviceProcess.CreationDate.ToUniversalTime().Ticks.ToString() -ne $state.createdUtcTicks) {
        throw 'The saved PID belongs to another process; no process was changed.'
    }
    return $serviceProcess
}

function Start-DanmakuDependency {
    $source = $config.danmaku_source
    if (-not $source.enabled -or [string]$source.url -notlike 'ws://127.0.0.1:8757/*') { return }
    $dyhubScript = Join-Path (Split-Path -Parent $projectRoot) 'dyhub\scripts\service.ps1'
    if (-not (Test-Path -LiteralPath $dyhubScript)) {
        Write-Host 'DyHub is optional and was not found. Start your collector separately.'
        return
    }
    try {
        $null = Get-LocalJson 'http://127.0.0.1:8757/api/stats'
        Write-Host 'DyHub is already running on port 8757.'
    } catch {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $dyhubScript -Action start
        if ($LASTEXITCODE -ne 0) { Write-Host 'DyHub did not start. The stage is still available.' }
    }
}

function Start-Bridge {
    if (Get-ProjectHealth) {
        Write-Host ('Already running: ' + $baseUrl + '/stage')
        return
    }
    if (Get-OwnedProcess) { throw 'Bridge process is running but unhealthy. Check logs or restart it.' }
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        throw ('Port {0} is occupied by another service. No process was changed.' -f $port)
    }
    if (-not (Test-Path -LiteralPath $pythonExecutable)) { throw 'Project .venv is missing.' }
    New-Item -ItemType Directory -Path (Join-Path $projectRoot 'logs') -Force | Out-Null
    $env:VIDU_CONF = $configFile
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $localFfmpeg = Join-Path $projectRoot 'tools\ffmpeg'
    if (Test-Path -LiteralPath (Join-Path $localFfmpeg 'ffmpeg.exe')) {
        $env:PATH = $localFfmpeg + ';' + $env:PATH
    }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $stdout = Join-Path $projectRoot ('logs\startup-' + $stamp + '.log')
    $stderr = Join-Path $projectRoot ('logs\startup-' + $stamp + '.error.log')
    $child = Start-Process -FilePath $pythonExecutable -ArgumentList $launchArguments `
        -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $created = Get-CimInstance Win32_Process -Filter ('ProcessId={0}' -f $child.Id)
    if (-not $created) { throw ('Bridge exited. See ' + $stderr) }
    @{
        pid = $child.Id
        createdUtcTicks = $created.CreationDate.ToUniversalTime().Ticks.ToString()
        url = $baseUrl + '/stage'
        stdout = $stdout
        stderr = $stderr
    } | ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding UTF8
    $deadline = (Get-Date).AddSeconds(40)
    while ((Get-Date) -lt $deadline) {
        if (Get-ProjectHealth) {
            Write-Host ('Started: ' + $baseUrl + '/stage')
            return
        }
        $child.Refresh()
        if ($child.HasExited) { throw ('Bridge exited. See ' + $stderr) }
        Start-Sleep -Milliseconds 400
    }
    throw ('Bridge startup timed out. See ' + $stderr)
}

function Stop-Bridge {
    if ($OnlyWhenIdle) {
        $currentHealth = Get-ProjectHealth
        if ($currentHealth -and $currentHealth.phase -ne 'idle') {
            throw 'A live session is active; restart deferred.'
        }
    }
    $serviceProcess = Get-OwnedProcess
    if ($serviceProcess) {
        if ((Get-ProjectHealth) -and -not $OnlyWhenIdle) {
            try {
                Invoke-RestMethod -Uri ($baseUrl + '/api/live/stop') -Method Post -TimeoutSec 15 | Out-Null
            } catch { Write-Host 'Could not confirm live-session shutdown; check the Vidu console if a session was active.' }
        }
        if ($OnlyWhenIdle) {
            $latestHealth = Get-ProjectHealth
            if ($latestHealth -and $latestHealth.phase -ne 'idle') {
                throw 'A new session started; restart deferred.'
            }
        }
        & "$env:SystemRoot\System32\taskkill.exe" /PID $serviceProcess.ProcessId /T /F | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Could not stop the verified project process.' }
    } elseif (Get-ProjectHealth) {
        throw 'This bridge was started elsewhere. Close its original launcher to stop it.'
    }
    if (Test-Path -LiteralPath $stateFile) { Remove-Item -LiteralPath $stateFile }
    Write-Host 'Bridge stopped. DyHub was left running.'
}

$lockName = 'Local\CyberXiaoyaBridge-' + $port
$startupLock = New-Object System.Threading.Mutex($false, $lockName)
$ownsLock = $false
try {
    try { $ownsLock = $startupLock.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { $ownsLock = $true }
    if (-not $ownsLock) { throw 'Another launcher is busy. Try again in a moment.' }
    switch ($Action) {
        'start' { Start-DanmakuDependency; Start-Bridge }
        'restart' { Stop-Bridge; Start-DanmakuDependency; Start-Bridge }
        'stop' { Stop-Bridge }
        'status' {
            $health = Get-ProjectHealth
            if (-not $health) { throw 'Project is not running or is not healthy.' }
            [ordered]@{
                url = $baseUrl + '/stage'
                build = $health.build
                phase = $health.phase
                tts_engine = $health.tts_engine
                danmaku_source = $health.danmaku_source
                vidu_key_configured = [bool]$config.vidu.api_key
                llm_key_file_present = Test-Path -LiteralPath (Join-Path $projectRoot $config.llm.key_file)
                tts_key_file_present = Test-Path -LiteralPath (Join-Path $projectRoot $config.tts.breezeblue_key_file)
            } | ConvertTo-Json -Depth 5 | Write-Host
        }
    }
    if ($Action -in @('start', 'restart') -and -not $NoBrowser) {
        Start-Process ($baseUrl + '/stage')
    }
} catch {
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
} finally {
    if ($ownsLock) { $startupLock.ReleaseMutex() }
    $startupLock.Dispose()
}
