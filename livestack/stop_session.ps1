# Stop a session (Windows): verify the replay in R2, then destroy the server.
#   .\stop_session.ps1
#   .\stop_session.ps1 --replay-key replays/2026/11/20261111-1900.mp4 --replay-file D:\obs\replay.mp4
& python "$PSScriptRoot\control\livestack.py" stop @args
exit $LASTEXITCODE
