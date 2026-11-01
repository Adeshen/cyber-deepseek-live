$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'service.ps1') -Action start -NoBrowser
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$chromeExecutable = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
if (-not (Test-Path -LiteralPath $chromeExecutable)) { throw 'Google Chrome was not found.' }
$browserProfile = Join-Path $projectRoot '.broadcast-browser'
$browserArguments = @(
    ('--user-data-dir="{0}"' -f $browserProfile),
    '--app=http://127.0.0.1:8795/stage?openOutput=1',
    # Keep the captured video rendering when Live Companion covers the window.
    '--disable-backgrounding-occluded-windows',
    '--disable-background-timer-throttling',
    '--disable-renderer-backgrounding',
    '--disable-features=CalculateNativeWinOcclusion',
    '--disable-popup-blocking',
    '--no-first-run',
    '--no-default-browser-check'
)
# These are the two interactive windows the user explicitly needs to capture.
Start-Process -FilePath $chromeExecutable -ArgumentList $browserArguments -WindowStyle Normal | Out-Null
Write-Host 'Live Companion window source: select the window titled Da Fei Yu live output (Chinese title).'
