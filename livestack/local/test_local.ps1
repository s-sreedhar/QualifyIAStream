<#
.SYNOPSIS
  Local end-to-end test of the production MediaMTX config + FFmpeg ladder on Windows.

.DESCRIPTION
  No cloud needed. It:
    1. renders config/mediamtx.yml.tmpl with local ports (19350/18554/18888/19997)
       so it never clashes with a MediaMTX already running on 1935,
    2. starts a separate MediaMTX that runs server/ladder.sh through Git Bash,
    3. pushes an FFmpeg test pattern (like OBS would),
    4. checks: master.m3u8 with 3 variants, probe.py freshness, encoder speed,
       ladder auto-restart after it is killed, publish auth, backup recording.
  Everything is written to local\.work (deleted at the next run).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File local\test_local.ps1

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File local\test_local.ps1 -KeepRunning
  # leaves the stream up; open http://127.0.0.1:18080/ in a browser (or the
  # .../hls/live/localtest/master.m3u8 link in VLC), press Enter to stop.
  # For OBS + a public link use local\run_local.ps1 instead.
#>
param(
  [string]$MediaMtx  = "$env:USERPROFILE\mediamtx.exe",
  [string]$FfmpegDir = "$env:USERPROFILE\ffmpeg-9.0.2-essentials_build\bin",
  [string]$GitBash   = "C:\Program Files\Git\bin\bash.exe",
  [string]$Python    = "python",
  [int]$Seconds      = 120,
  [switch]$KeepRunning
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib.ps1')
$Root = Split-Path -Parent $PSScriptRoot
$Work = Join-Path $PSScriptRoot '.work'
$User = 'localtest'; $Pass = 'localtest-password-123'
$Ports = @{ Rtmp = 19350; Rtsp = 18554; Hls = 18888; Api = 19997; Metrics = 19998 }
$results = New-Object System.Collections.Generic.List[object]
$procs = New-Object System.Collections.Generic.List[object]

function Result([string]$name, [bool]$ok, [string]$detail) {
  $r = if ($ok) { 'PASS' } else { 'FAIL' }
  $results.Add([pscustomobject]@{ Check = $name; Result = $r; Detail = $detail })
  Write-Host ("[{0}] {1}: {2}" -f $r, $name, $detail) -ForegroundColor $(if ($ok) { 'Green' } else { 'Red' })
}
function Ladder-Procs([string]$path) { Get-LadderProcs $Ports.Rtsp $path }
function Ready-Paths {
  try { (Invoke-RestMethod -Uri 'http://127.0.0.1:19997/v3/paths/list' -TimeoutSec 3).items | Where-Object { $_.ready } | ForEach-Object { $_.name } } catch { @() }
}

foreach ($f in @($MediaMtx, "$FfmpegDir\ffmpeg.exe", $GitBash)) { if (-not (Test-Path $f)) { throw "Not found: $f (pass -MediaMtx / -FfmpegDir / -GitBash)" } }
$ffmpeg = Join-Path $FfmpegDir 'ffmpeg.exe'

try {
  # ---------------------------------------------------------------- setup
  if (Test-Path $Work) { Remove-Item $Work -Recurse -Force }
  $hls = Join-Path $Work 'hls'; $rec = Join-Path $Work 'recordings'; $run = Join-Path $Work 'run'
  New-Item -ItemType Directory -Force $hls, $rec, $run | Out-Null

  foreach ($p in @($Ports.Values) + 18080) {
    $owner = Get-PortOwner $p
    if ($owner) { throw "port $p is already used by $owner - stop it (e.g. a run_local.ps1 or an earlier test) and retry" }
  }
  Write-MediaMtxConfig -Root $Root -OutFile (Join-Path $Work 'mediamtx.yml') -User $User -Pass $Pass `
    -Ports $Ports -GitBash $GitBash -RecordDir $rec
  Set-LadderEnv -Ffmpeg $ffmpeg -HlsRoot $hls -RunDir $run -RtspPort $Ports.Rtsp   # inherited by MediaMTX -> ladder.sh

  # ---------------------------------------------------------------- MediaMTX
  $mtx = Start-Process -FilePath $MediaMtx -ArgumentList "`"$Work\mediamtx.yml`"" -WorkingDirectory $Work `
    -RedirectStandardOutput "$Work\mediamtx.log" -RedirectStandardError "$Work\mediamtx.err.log" -PassThru -WindowStyle Hidden
  $procs.Add($mtx)
  $up = Wait-For { try { Invoke-RestMethod 'http://127.0.0.1:19997/v3/paths/list' -TimeoutSec 2 | Out-Null; $true } catch { $false } } 15
  Result 'mediamtx_start' $up $(if ($up) { 'config accepted, API up on 19997' } else { "did not start - see $Work\mediamtx.log" })
  if (-not $up) { Get-Content "$Work\mediamtx.log" -Tail 20; throw 'MediaMTX failed to start' }

  # ---------------------------------------------------------------- push test pattern (like OBS)
  $pushArgs = "-hide_banner -loglevel error -re -f lavfi -i testsrc2=size=1280x720:rate=30 -f lavfi -i sine=frequency=440:sample_rate=48000 " +
              "-c:v libx264 -preset veryfast -tune zerolatency -g 60 -pix_fmt yuv420p -c:a aac -b:a 128k -t $Seconds -f flv rtmp://127.0.0.1:19350/live/localtest"
  $push = Start-Process -FilePath $ffmpeg -ArgumentList $pushArgs -PassThru -WindowStyle Hidden -RedirectStandardError "$Work\push.log"
  $procs.Add($push)

  # ---------------------------------------------------------------- 3-variant ladder
  $out = Join-Path $hls 'live\localtest'
  $ok = Wait-For {
    $m = Join-Path $out 'master.m3u8'
    if (-not (Test-Path $m)) { return $false }
    if (([regex]::Matches((Get-Content $m -Raw), '#EXT-X-STREAM-INF')).Count -ne 3) { return $false }
    foreach ($v in '720p', '480p', '360p') {
      $p = Join-Path $out "$v\index.m3u8"
      if (-not (Test-Path $p) -or -not ((Get-Content $p -Raw) -match '\.ts')) { return $false }
    }
    $true
  } 60
  $detail = if ($ok) { ((Get-Content (Join-Path $out 'master.m3u8')) | Where-Object { $_ -match 'RESOLUTION' } | ForEach-Object { ($_ -split 'RESOLUTION=')[1].Split(',')[0] }) -join ', ' } else { "no complete output - see $Work\mediamtx.log" }
  Result 'ladder_3_variants' $ok $detail

  # ---------------------------------------------------------------- probe through HTTP
  $http = Start-Process -FilePath $Python -ArgumentList "`"$PSScriptRoot\hls_server.py`" --root `"$hls`" --port 18080" -PassThru -WindowStyle Hidden `
    -RedirectStandardError "$Work\origin.err.log"
  $procs.Add($http)
  Wait-For { try { (Invoke-WebRequest 'http://127.0.0.1:18080/healthz' -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 } catch { $false } } 10 | Out-Null
  & $Python (Join-Path $Root 'control\probe.py') 'http://127.0.0.1:18080/hls/live/localtest/master.m3u8' --once --expect-variants 3
  Result 'probe_once' ($LASTEXITCODE -eq 0) 'probe.py: 3 variants reachable, advancing, segments downloadable'
  $hdr = (Invoke-WebRequest 'http://127.0.0.1:18080/hls/live/localtest/master.m3u8' -UseBasicParsing -TimeoutSec 5).Headers
  $hOk = ($hdr['Content-Type'] -eq 'application/vnd.apple.mpegurl') -and ($hdr['Cache-Control'] -eq 'no-cache') -and ($hdr['Access-Control-Allow-Origin'] -eq '*')
  Result 'origin_headers' $hOk "m3u8: $($hdr['Content-Type']); Cache-Control: $($hdr['Cache-Control']); CORS: $($hdr['Access-Control-Allow-Origin'])"

  # ---------------------------------------------------------------- encoder speed
  Start-Sleep 5
  $prog = Join-Path $run 'progress-live_localtest'
  $speed = if (Test-Path $prog) { (Select-String -Path $prog -Pattern '^speed=\s*([0-9.]+)x' | Select-Object -Last 1).Matches[0].Groups[1].Value } else { $null }
  $sOk = $speed -and ([double]$speed -ge 0.95)
  Result 'encoder_speed' $sOk $(if ($speed) { "speed=${speed}x (needs >= 1.0x sustained; >=0.95 tolerated for a live source)" } else { 'no progress file' })

  # ---------------------------------------------------------------- resilience: kill the encoder
  $before = @(Ladder-Procs 'live/localtest')
  if ($before.Count -ge 1) {
    $oldPid = $before[0].ProcessId
    Stop-Process -Id $oldPid -Force
    $killedAt = Get-Date
    $restarted = Wait-For {
      $now = @(Ladder-Procs 'live/localtest')
      $fresh = (Get-Item (Join-Path $out '720p\index.m3u8')).LastWriteTime -gt $killedAt.AddSeconds(1)
      ($now.Count -ge 1) -and ($now[0].ProcessId -ne $oldPid) -and $fresh
    } 30
    Result 'ladder_auto_restart' $restarted $(if ($restarted) { "killed pid $oldPid; MediaMTX restarted the ladder and the playlist advanced in $([int]((Get-Date) - $killedAt).TotalSeconds)s" } else { 'ladder not restarted within 30s' })
  } else {
    Get-CimInstance Win32_Process -Filter "Name='ffmpeg.exe'" | ForEach-Object { Write-Host "  ffmpeg: $($_.CommandLine)" }
    Result 'ladder_auto_restart' $false 'ladder ffmpeg process not found'
  }

  # ---------------------------------------------------------------- publish auth (non-localhost)
  $lan = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' -and $_.PrefixOrigin -ne 'WellKnown' } | Select-Object -First 1).IPAddress
  if ($lan) {
    $authBase = "-hide_banner -loglevel error -re -f lavfi -i testsrc2=size=640x360:rate=30 -f lavfi -i sine=frequency=880:sample_rate=48000 -c:v libx264 -preset ultrafast -pix_fmt yuv420p -c:a aac -t 15 -f flv"
    $noCreds = Start-Process -FilePath $ffmpeg -ArgumentList "$authBase rtmp://${lan}:19350/live/authtest" -PassThru -WindowStyle Hidden -RedirectStandardError "$Work\auth-nocreds.log"
    $procs.Add($noCreds)
    Start-Sleep 5
    $rejected = -not ((Ready-Paths) -contains 'live/authtest')
    Stop-Process -Id $noCreds.Id -Force -ErrorAction SilentlyContinue
    Result 'auth_rejects_no_credentials' $rejected "publish from $lan without user/pass"
    $withCreds = Start-Process -FilePath $ffmpeg -ArgumentList "$authBase `"rtmp://${lan}:19350/live/authtest?user=$User&pass=$Pass`"" -PassThru -WindowStyle Hidden -RedirectStandardError "$Work\auth-creds.log"
    $procs.Add($withCreds)
    $accepted = Wait-For { (Ready-Paths) -contains 'live/authtest' } 10
    Stop-Process -Id $withCreds.Id -Force -ErrorAction SilentlyContinue
    Result 'auth_accepts_credentials' $accepted "publish from $lan with ?user=...&pass=..."
  } else { Write-Host 'No LAN IPv4 address found - skipping auth test' -ForegroundColor Yellow }

  # ---------------------------------------------------------------- backup recording
  # The file is still open by MediaMTX; NTFS directory metadata reports 0 bytes
  # until close, so read the real length through a shared stream.
  $recFile = Get-ChildItem $rec -Recurse -File -ErrorAction SilentlyContinue | Where-Object { $_.FullName -match 'localtest' } | Select-Object -First 1
  $recLen = 0
  if ($recFile) { $fs = [IO.File]::Open($recFile.FullName, 'Open', 'Read', 'ReadWrite'); $recLen = $fs.Length; $fs.Close() }
  Result 'backup_recording' ($recLen -gt 0) $(if ($recFile) { "$($recFile.Name) ($([math]::Round($recLen/1MB,1)) MB so far)" } else { 'no recording written' })

  if ($KeepRunning) {
    Write-Host "`nStream is live for up to $Seconds s from start:" -ForegroundColor Cyan
    Write-Host '  VLC / ffplay : http://127.0.0.1:18080/hls/live/localtest/master.m3u8'
    Write-Host '  Browser      : http://127.0.0.1:18080/player.html?src=/hls/live/localtest/master.m3u8'
    Read-Host 'Press Enter to stop'
  }
}
finally {
  foreach ($p in $procs) { if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } }
  Stop-LadderProcs $Ports.Rtsp
}

Write-Host "`n==== Summary ====" -ForegroundColor Cyan
$results | Format-Table -AutoSize
$failed = @($results | Where-Object Result -eq 'FAIL').Count
if ($failed) { Write-Host "$failed check(s) FAILED. Logs: $Work" -ForegroundColor Red; exit 1 }
Write-Host 'All checks passed.' -ForegroundColor Green; exit 0
