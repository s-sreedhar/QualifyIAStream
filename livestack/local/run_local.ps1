<#
.SYNOPSIS
  Live mode on your PC: OBS -> MediaMTX -> FFmpeg ladder -> local origin -> (optional) public tunnel.

.DESCRIPTION
  The same pipeline as the production server, running on this PC with the
  production config (config\mediamtx.yml.tmpl + server\ladder.sh):

    OBS --RTMP :1935--> MediaMTX --RTSP (localhost)--> FFmpeg ladder (720p/480p/360p HLS)
        --> local\.live\hls --> hls_server.py :8090 (stands in for nginx)
        --> cloudflared quick tunnel (https://<random>.trycloudflare.com, with -Tunnel)

  OBS settings (same PC, no password needed from localhost):
    Server:     rtmp://localhost:1935/live
    Stream key: test

  Press Q (or Ctrl+C) to stop. Everything it started is stopped again.
  The tunnel URL is public but random and temporary - for testing only.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File local\run_local.ps1
  # local only: watch at http://localhost:8090/

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File local\run_local.ps1 -Tunnel
  # also prints a public https://....trycloudflare.com link (phone on 4G, a friend, ...)

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File local\run_local.ps1 -Tunnel -TestPattern -Seconds 120
  # no OBS needed: FFmpeg sends a test pattern; stops after 120 s
#>
param(
  [string]$MediaMtx    = "$env:USERPROFILE\mediamtx.exe",
  [string]$FfmpegDir   = "$env:USERPROFILE\ffmpeg-9.0.2-essentials_build\bin",
  [string]$GitBash     = "C:\Program Files\Git\bin\bash.exe",
  [string]$Python      = "python",
  [string]$Cloudflared = "",
  [int]$RtmpPort       = 1935,
  [int]$HttpPort       = 8090,
  [string]$Stream      = "live/test",      # OBS: server rtmp://localhost:1935/live + key test
  [switch]$Tunnel,                          # public URL through a Cloudflare quick tunnel
  [switch]$Lan,                             # let phones on your Wi-Fi open http://<PC-IP>:8090
  [switch]$TestPattern,                     # push an FFmpeg test pattern instead of OBS
  [switch]$NoRecord,                        # skip the server-side backup recording
  [switch]$StopExisting,                    # stop another mediamtx.exe that holds our ports
  [int]$Seconds        = 0                  # 0 = run until Q / Ctrl+C
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib.ps1')
$Root  = Split-Path -Parent $PSScriptRoot
$Work  = Join-Path $PSScriptRoot '.live'
$Ports = @{ Rtmp = $RtmpPort; Rtsp = 28554; Hls = 28888; Api = 29997; Metrics = 29998 }
# Credentials for publishing from ANOTHER machine (localhost needs none).
$User  = 'local'; $Pass = 'local-' + -join ((48..57) + (97..122) | Get-Random -Count 16 | ForEach-Object { [char]$_ })
$procs = New-Object System.Collections.Generic.List[object]
$push = $null; $public = $null

function Say([string]$m, [string]$c = 'Gray') { Write-Host $m -ForegroundColor $c }

# ------------------------------------------------------------------ prerequisites
$ffmpeg = Join-Path $FfmpegDir 'ffmpeg.exe'
foreach ($f in @($MediaMtx, $ffmpeg, $GitBash)) { if (-not (Test-Path $f)) { throw "Not found: $f (pass -MediaMtx / -FfmpegDir / -GitBash)" } }
if ($Stream -notmatch '^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*$') { throw "invalid -Stream '$Stream' (letters, digits, _ - and /)" }

if ($Tunnel) {
  if (-not $Cloudflared) {
    $cands = @((Join-Path (Split-Path -Parent $Root) 'tools\cloudflared.exe'))
    $onPath = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($onPath) { $cands += $onPath.Source }
    $Cloudflared = $cands | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
  }
  if (-not $Cloudflared -or -not (Test-Path $Cloudflared)) {
    throw "cloudflared not found. Install it (docs/LOCAL_TESTING.md, 'Install cloudflared') or pass -Cloudflared <path>."
  }
}

foreach ($p in @($Ports.Values) + $HttpPort) {
  $owner = Get-PortOwner $p
  if (-not $owner) { continue }
  if ($StopExisting -and $owner -like 'mediamtx*') {
    $owningPid = (Get-NetTCPConnection -State Listen -LocalPort $p | Select-Object -First 1).OwningProcess
    Say "stopping $owner on port $p (-StopExisting)" Yellow
    Stop-Process -Id $owningPid -Force; Start-Sleep 1; continue
  }
  $hint = ''
  if ($owner -like 'mediamtx*') { $hint = ' Another MediaMTX is running: close its window, or re-run with -StopExisting.' }
  elseif ($p -eq $HttpPort) { $hint = ' Pick another port with -HttpPort.' }
  throw "port $p is already used by $owner.$hint"
}

try {
  # ---------------------------------------------------------------- workspace
  $hls = Join-Path $Work 'hls'; $run = Join-Path $Work 'run'; $rec = Join-Path $Work 'recordings'; $logs = Join-Path $Work 'logs'
  foreach ($d in $hls, $run, $logs) { if (Test-Path $d) { Remove-Item $d -Recurse -Force } }   # recordings are kept
  New-Item -ItemType Directory -Force $hls, $run, $rec, $logs | Out-Null

  Write-MediaMtxConfig -Root $Root -OutFile (Join-Path $Work 'mediamtx.yml') -User $User -Pass $Pass `
    -Ports $Ports -GitBash $GitBash -RecordDir $rec -NoRecord:$NoRecord
  Set-LadderEnv -Ffmpeg $ffmpeg -HlsRoot $hls -RunDir $run -RtspPort $Ports.Rtsp

  # ---------------------------------------------------------------- 1. MediaMTX (ingest + ladder hook)
  $mtx = Start-Process -FilePath $MediaMtx -ArgumentList "`"$Work\mediamtx.yml`"" -WorkingDirectory $Work `
    -RedirectStandardOutput "$logs\mediamtx.log" -RedirectStandardError "$logs\mediamtx.err.log" -PassThru -WindowStyle Hidden
  $procs.Add($mtx)
  $api = "http://127.0.0.1:$($Ports.Api)/v3/paths/list"
  if (-not (Wait-For { try { Invoke-RestMethod $api -TimeoutSec 2 | Out-Null; $true } catch { $false } } 15)) {
    Get-Content "$logs\mediamtx.log" -Tail 20; throw "MediaMTX did not start - see $logs\mediamtx.log"
  }
  Say "[ok] MediaMTX listening for RTMP on :$RtmpPort" Green

  # ---------------------------------------------------------------- 2. local origin (nginx stand-in)
  $bind = if ($Lan) { '0.0.0.0' } else { '127.0.0.1' }
  $srv = Start-Process -FilePath $Python -ArgumentList "`"$PSScriptRoot\hls_server.py`" --root `"$hls`" --port $HttpPort --bind $bind" `
    -PassThru -WindowStyle Hidden -RedirectStandardOutput "$logs\origin.log" -RedirectStandardError "$logs\origin.err.log"
  $procs.Add($srv)
  $base = "http://127.0.0.1:$HttpPort"
  if (-not (Wait-For { try { (Invoke-WebRequest "$base/healthz" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 } catch { $false } } 10)) {
    Get-Content "$logs\origin.err.log" -Tail 20; throw "local origin did not start - see $logs\origin.err.log"
  }
  Say "[ok] local origin on $base" Green

  # ---------------------------------------------------------------- 3. tunnel (optional)
  $public = $null
  if ($Tunnel) {
    $cf = Start-Process -FilePath $Cloudflared -ArgumentList "tunnel --no-autoupdate --url $base" -PassThru -WindowStyle Hidden `
      -RedirectStandardOutput "$logs\tunnel.log" -RedirectStandardError "$logs\tunnel.err.log"
    $procs.Add($cf)
    $found = Wait-For {
      $t = Get-Content "$logs\tunnel.err.log", "$logs\tunnel.log" -Raw -ErrorAction SilentlyContinue
      # cloudflared logs its control API URL (api.trycloudflare.com) on failure;
      # do not mistake that fixed endpoint for the public tunnel hostname.
      $m = [regex]::Match(($t -join "`n"), 'https://(?!api\.trycloudflare\.com)[a-z0-9-]+\.trycloudflare\.com')
      if ($m.Success) { $script:public = $m.Value; $true } else { $false }
    } 45
    if (-not $found) { Get-Content "$logs\tunnel.err.log" -Tail 15; throw "tunnel did not come up - see $logs\tunnel.err.log" }
    # The hostname needs a few seconds to resolve; wait until the tunnel answers.
    $reach = Wait-For { try { (Invoke-WebRequest "$public/healthz" -UseBasicParsing -TimeoutSec 5).StatusCode -eq 200 } catch { $false } } 60
    Say "[$(if ($reach) { 'ok' } else { '..' })] tunnel $public $(if (-not $reach) { '(not reachable yet - DNS can take a minute)' })" $(if ($reach) { 'Green' } else { 'Yellow' })
  }

  # ---------------------------------------------------------------- 4. source
  if ($TestPattern) {
    $dur = if ($Seconds -gt 0) { "-t $Seconds" } else { '' }
    $pushArgs = "-hide_banner -loglevel error -re -f lavfi -i testsrc2=size=1280x720:rate=30 -f lavfi -i sine=frequency=440:sample_rate=48000 " +
                "-c:v libx264 -preset veryfast -tune zerolatency -g 60 -pix_fmt yuv420p -c:a aac -b:a 128k $dur -f flv rtmp://127.0.0.1:$RtmpPort/$Stream"
    $push = Start-Process -FilePath $ffmpeg -ArgumentList $pushArgs -PassThru -WindowStyle Hidden -RedirectStandardError "$logs\testpattern.log"
    $procs.Add($push)
  }

  # ---------------------------------------------------------------- instructions
  $app, $key = $Stream.Split('/', 2)
  $hlsPath = "/hls/$Stream/master.m3u8"
  $lanIp = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' -and $_.PrefixOrigin -ne 'WellKnown' } | Select-Object -First 1).IPAddress
  Say ''
  Say '================ READY ================' Cyan
  if ($TestPattern) { Say "Source       : FFmpeg test pattern -> rtmp://127.0.0.1:$RtmpPort/$Stream" }
  else {
    Say 'OBS (this PC): Settings -> Stream -> Service: Custom'
    Say "                Server     : rtmp://localhost:$RtmpPort/$app"
    Say "                Stream key : $(if ($key) { $key } else { '(empty)' })"
    Say '              then click Start Streaming.'
    Say "OBS (other PC on Wi-Fi): Server rtmp://${lanIp}:$RtmpPort/$app  key ${key}?user=$User&pass=$Pass" DarkGray
  }
  Say "Watch here   : $base/            (stream list + player)"
  Say "               $base/player.html?src=$hlsPath"
  Say "VLC          : $base$hlsPath"
  if ($Lan -and $lanIp) { Say "Wi-Fi phone  : http://${lanIp}:$HttpPort/player.html?src=$hlsPath   (allow Python in Windows Firewall if asked)" }
  if ($public) {
    Say "PUBLIC (any device, any network):" Cyan
    Say "               $public/player.html?src=$hlsPath" Cyan
    Say "               $public$hlsPath   (VLC / probe.py)" Cyan
  }
  Say "Logs         : $logs"
  Say 'The first video appears ~8-12 s after you start streaming. Press Q to stop.' Yellow
  Say ''

  # ---------------------------------------------------------------- status loop
  $sw = [Diagnostics.Stopwatch]::StartNew(); $last = ''
  while ($true) {
    if ($Seconds -gt 0 -and $sw.Elapsed.TotalSeconds -ge $Seconds) { break }
    try { if ([Console]::KeyAvailable -and [Console]::ReadKey($true).Key -eq 'Q') { break } } catch { }
    foreach ($p in $procs) {
      if ($p.HasExited -and $p -ne $push) { throw "$($p.ProcessName) (pid $($p.Id)) exited unexpectedly - see $logs" }
    }
    $ready = try { @((Invoke-RestMethod $api -TimeoutSec 2).items | Where-Object { $_.ready } | ForEach-Object { $_.name }) } catch { @() }
    $variants = @('720p', '480p', '360p' | Where-Object { Test-Path (Join-Path $hls "$Stream\$_\index.m3u8") })
    $speed = Get-LadderSpeed $run $Stream
    $line = if ($ready -contains $Stream) {
      "LIVE  $Stream  ladder: $(if ($variants) { $variants -join '/' } else { 'starting' })  speed: $(if ($speed) { '{0:N2}x' -f $speed } else { '-' })"
    } else { "waiting for OBS to publish to rtmp://localhost:$RtmpPort/$Stream ..." }
    if ($line -ne $last) {
      $color = if ($speed -and $speed -lt 0.97) { 'Red' } elseif ($ready -contains $Stream) { 'Green' } else { 'Yellow' }
      Say ("{0:HH:mm:ss}  {1}{2}" -f (Get-Date), $line, $(if ($speed -and $speed -lt 0.97) { '  <- encoder too slow: lower OBS resolution/fps' } else { '' })) $color
      $last = $line
    }
    Start-Sleep -Seconds 2
  }
}
finally {
  Say 'stopping...' Yellow
  foreach ($p in $procs) { if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } }
  Stop-LadderProcs $Ports.Rtsp
  if (-not $NoRecord -and (Test-Path $rec)) {
    $latest = Get-ChildItem $rec -Recurse -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($latest) { Say "Backup recording: $($latest.FullName)  (try: python control\replay.py encode `"$($latest.FullName)`" E:\temp\replay.mp4)" }
  }
  Say 'stopped.' Green
}
