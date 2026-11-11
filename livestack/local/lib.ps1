# Shared helpers for local\test_local.ps1 and local\run_local.ps1.
# Dot-source it:   . (Join-Path $PSScriptRoot 'lib.ps1')

function Fwd([string]$p) { $p -replace '\\', '/' }

function Wait-For([scriptblock]$cond, [int]$timeoutSec) {
  $sw = [Diagnostics.Stopwatch]::StartNew()
  while ($sw.Elapsed.TotalSeconds -lt $timeoutSec) { if (& $cond) { return $true }; Start-Sleep -Milliseconds 500 }
  return $false
}

# Renders config\mediamtx.yml.tmpl for Windows, exactly like server/boot.sh does on
# the VPS, except for the ports, the ladder command (run through Git Bash) and the
# recording folder. Anything else in the template is used unchanged, so the local
# run tests the real production config.
function Write-MediaMtxConfig {
  param(
    [Parameter(Mandatory)][string]$Root,
    [Parameter(Mandatory)][string]$OutFile,
    [Parameter(Mandatory)][string]$User,
    [Parameter(Mandatory)][string]$Pass,
    [Parameter(Mandatory)][hashtable]$Ports,      # Rtmp, Rtsp, Hls, Api, Metrics
    [Parameter(Mandatory)][string]$GitBash,
    [Parameter(Mandatory)][string]$RecordDir,
    [switch]$NoRecord
  )
  $ladder = "'`"$(Fwd $GitBash)`" `"$(Fwd (Join-Path $Root 'server\ladder.sh'))`"'"
  $cfg = (Get-Content (Join-Path $Root 'config\mediamtx.yml.tmpl') -Raw).
    Replace('${PUBLISH_USER}', $User).Replace('${PUBLISH_PASS}', $Pass).
    Replace('rtmpAddress: :1935', "rtmpAddress: :$($Ports.Rtmp)").
    Replace('127.0.0.1:8554', "127.0.0.1:$($Ports.Rtsp)").
    Replace('127.0.0.1:8888', "127.0.0.1:$($Ports.Hls)").
    Replace('127.0.0.1:9997', "127.0.0.1:$($Ports.Api)").
    Replace('127.0.0.1:9998', "127.0.0.1:$($Ports.Metrics)").
    Replace('/opt/livestack/server/ladder.sh', $ladder).
    Replace('/var/lib/livestack/recordings', (Fwd $RecordDir))
  if ($NoRecord) { $cfg = [regex]::Replace($cfg, '(?m)^  record: yes', '  record: no') }
  foreach ($p in $Ports.Values) {
    if ($cfg -notmatch ":$p\b") { throw "config template changed: port $p was not applied (check config\mediamtx.yml.tmpl)" }
  }
  [IO.File]::WriteAllText($OutFile, $cfg, (New-Object Text.UTF8Encoding($false)))
}

# Environment that MediaMTX passes on to server/ladder.sh.
function Set-LadderEnv([string]$Ffmpeg, [string]$HlsRoot, [string]$RunDir, [int]$RtspPort) {
  $env:FFMPEG_BIN = Fwd $Ffmpeg
  $env:HLS_ROOT   = Fwd $HlsRoot
  $env:RUN_DIR    = Fwd $RunDir
  $env:RTSP_BASE  = "rtsp://127.0.0.1:$RtspPort"
}

# Returns "name (pid N)" of the process listening on a TCP port, or $null if it is free.
function Get-PortOwner([int]$Port) {
  $c = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $c) { return $null }
  $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
  return "$(if ($p) { $p.ProcessName } else { 'unknown' }) (pid $($c.OwningProcess))"
}

# FFmpeg ladder processes reading from our RTSP port (optionally one stream path).
function Get-LadderProcs([int]$RtspPort, [string]$Path = '') {
  @(Get-CimInstance Win32_Process -Filter "Name='ffmpeg.exe'" |
    Where-Object { $_.CommandLine -match [regex]::Escape("127.0.0.1:$RtspPort/$Path") })
}

# On Windows, children of MediaMTX (bash -> ffmpeg) are not killed with it.
# Only touches ladders on *our* RTSP port, and ladder.sh shells whose MediaMTX is
# gone, so a second local stack (test vs. live mode) is left alone.
function Stop-LadderProcs([int]$RtspPort) {
  Get-LadderProcs $RtspPort | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Milliseconds 300
  Get-CimInstance Win32_Process -Filter "Name='bash.exe'" |
    Where-Object { $_.CommandLine -match 'ladder\.sh' -and -not (Get-Process -Id $_.ParentProcessId -ErrorAction SilentlyContinue) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

# Last reported encoder speed (e.g. 1.03) from the ladder's -progress file, or $null.
function Get-LadderSpeed([string]$RunDir, [string]$Path) {
  $f = Join-Path $RunDir ("progress-" + ($Path -replace '/', '_'))
  if (-not (Test-Path $f)) { return $null }
  $m = Select-String -Path $f -Pattern '^speed=\s*([0-9.]+)x' | Select-Object -Last 1
  if ($m) { return [double]$m.Matches[0].Groups[1].Value } else { return $null }
}
