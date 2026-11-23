# One-time setup for the recording uploader on Windows. Run from this folder:
#   powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1
# Installs Python and ffmpeg with winget if missing, then checks everything. Needs no passwords.
$ErrorActionPreference = 'Continue'
function Has($n) { return [bool](Get-Command $n -ErrorAction SilentlyContinue) }

if (-not (Has 'winget')) { Write-Host 'winget is missing. Install "App Installer" from the Microsoft Store, then run this again.' -ForegroundColor Red; exit 1 }

if (-not (Has 'python')) {
  Write-Host 'Installing Python 3...'
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
}
if (-not (Has 'ffmpeg') -or -not (Has 'ffprobe')) {
  Write-Host 'Installing ffmpeg...'
  winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
}

# Refresh PATH for this window so the checks below see the new programs.
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')

# winget keeps ffmpeg in its own folder; make sure it is on the user's PATH permanently.
$pkg = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
$ff = Get-ChildItem $pkg -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if ($ff -and -not (Has 'ffmpeg')) {
  $dir = $ff.DirectoryName
  [Environment]::SetEnvironmentVariable('Path', ([Environment]::GetEnvironmentVariable('Path','User') + ';' + $dir), 'User')
  $env:Path += ';' + $dir
  Write-Host "Added ffmpeg to PATH: $dir"
}

Write-Host ''
$ok = $true
foreach ($t in 'python','ffmpeg','ffprobe') {
  if (Has $t) { Write-Host "OK   $t  ->  $((Get-Command $t).Source)" -ForegroundColor Green } else { Write-Host "MISSING $t" -ForegroundColor Red; $ok = $false }
}
if ($ok) {
  Write-Host "`nAll set. Open a NEW terminal, then run:`n  python upload_recording.py `"D:\Videos\Lecture.mp4`" --title `"My lecture`""
} else {
  Write-Host "`nClose this window, open a NEW terminal and run this script once more. Still missing? Install the missing tool by hand." -ForegroundColor Yellow
}
