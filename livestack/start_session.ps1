# Start a streaming session (Windows). Safe to re-run: it resumes. See docs\RUNBOOK.md.
#   .\start_session.ps1 [--session 20261111-1900] [--destroy-on-failure]
& python "$PSScriptRoot\control\livestack.py" start @args
exit $LASTEXITCODE
