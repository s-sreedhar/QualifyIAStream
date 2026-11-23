# Recording uploader (move to any computer)

Encodes a lecture video on THIS computer (720/480/360p adaptive HLS + poster) and publishes it to the recordings bucket.
The computer needs **no passwords and no storage keys**. The only credential is the **upload link** made in the admin.

## Install everything (one time)
**Windows** (PowerShell, in this folder):
`powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1`
It installs Python 3 and ffmpeg with winget, adds ffmpeg to PATH permanently, and prints where each tool was found. Open a NEW terminal afterwards.

**Mac / Linux:** `bash setup-mac-linux.sh`

Manual alternative: Python 3.9+ (python.org), ffmpeg + ffprobe (Windows `winget install Gyan.FFmpeg`, Mac `brew install ffmpeg`, Linux `sudo apt install ffmpeg`). No pip packages are needed.
If ffmpeg is installed but not on PATH, set `FFMPEG_DIR` to its `bin` folder, or put it in `recording-uploader/ffmpeg/bin`.

Also needed: free disk space of about 1.5x the video size, a power adapter with sleep turned off, and this folder (nothing secret inside).

Check: `python upload_recording.py --check`

## Every upload
1. Admin > LMS > Recordings > **Upload recording**. A page opens: copy its full address (contains `?t=...`). Valid 72 hours (the script renews it itself while running), one recording only.
2. Run:
   `python upload_recording.py "D:/Videos/Lecture 1.mp4" --title "Polity Lecture 1"`
   (or just `python upload_recording.py` and drag the file in). Add `--link "<address>"` or paste it when asked.
3. When it says DONE the recording is READY under LMS > Recordings; attach it to a class/lesson as usual.

If it stops (internet, power), run the same command again: encoded qualities and uploaded files are reused.
How it protects you:
- Every uploaded file is written to a progress log (`work/<video>/uploaded-<recording>.log`) the moment it arrives. A re-run skips what is already there.
- Internet outages are retried for up to 30 minutes by themselves; expired storage links are refreshed automatically.
- An encoded quality is reused only if it finished and every segment is on disk; a half-finished one is encoded again from its start (encoding is not resumable inside one quality).
- The same file is never uploaded twice by accident: after a successful upload the script remembers it (`completed.json`) and refuses to repeat it unless you add `--force`.
- One upload link = one recording. A second run with the same link updates the SAME recording (no duplicate). A new link makes a new recording.
A link older than 72 hours: make a new one; the work already done is still reused.

## Small files / quick option
The admin "Upload recording" page itself uploads one MP4 (single quality). Use this script when you want all qualities.
