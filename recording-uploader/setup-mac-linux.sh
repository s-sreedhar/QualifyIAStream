#!/usr/bin/env bash
# One-time setup for the recording uploader on macOS / Linux:  bash setup-mac-linux.sh
set -e
have() { command -v "$1" >/dev/null 2>&1; }

if ! have python3 || ! have ffmpeg || ! have ffprobe; then
  if [ "$(uname)" = "Darwin" ]; then
    have brew || { echo "Install Homebrew first: https://brew.sh"; exit 1; }
    have python3 || brew install python
    have ffmpeg || brew install ffmpeg
  elif have apt-get; then
    sudo apt-get update && sudo apt-get install -y python3 ffmpeg
  elif have dnf; then
    sudo dnf install -y python3 ffmpeg
  else
    echo "Install python3 and ffmpeg with your package manager, then run this again."; exit 1
  fi
fi
for t in python3 ffmpeg ffprobe; do have "$t" && echo "OK   $t -> $(command -v $t)" || { echo "MISSING $t"; exit 1; }; done
echo; echo "All set. Run:  python3 upload_recording.py \"/path/to/Lecture.mp4\" --title \"My lecture\""
