# Starts ComfyUI in the background (no console window) if it is not already running,
# then opens the video studio in its own app window.
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$portable = Split-Path -Parent $root
$appUrl = "http://127.0.0.1:8188/easy"

function Test-Up {
  try { Invoke-WebRequest "http://127.0.0.1:8188/system_stats" -UseBasicParsing -TimeoutSec 2 | Out-Null; return $true }
  catch { return $false }
}

if (Test-Up) {
  $target = $appUrl
} else {
  Start-Process -FilePath "$portable\python_embeded\python.exe" `
    -ArgumentList "-s", "ComfyUI\main.py", "--windows-standalone-build", "--disable-auto-launch", "--preview-method", "auto" `
    -WorkingDirectory $portable -WindowStyle Hidden `
    -RedirectStandardOutput "$root\comfyui_out.log" -RedirectStandardError "$root\comfyui_err.log"
  # the loading page waits for the server, then switches to the app
  $target = "file:///" + ("$root\loading.html" -replace "\\", "/")
}

$edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
          "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($edge) {
  Start-Process $edge -ArgumentList "--app=$target", "--window-size=1440,920"
} else {
  Start-Process $appUrl
}
