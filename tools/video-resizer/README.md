# Ad Video Resizer

Turns one source video (e.g. 1080x1080) into all ad sizes with a file-size cap per output:
1080x1920, 1080x1080, 1920x1080, 768x1024, 1024x768, 720x1280, 1280x720, 1080x1350.

## Setup (once)
1. Install Python 3 (https://python.org) and ffmpeg (https://ffmpeg.org/download.html, must be on PATH).
2. Windows quick install of ffmpeg: `winget install ffmpeg`

## Use
- Window: `python resize_video.py`
- Command line: `python resize_video.py input.mp4 --max-mb 4`
  - `--mode blur|pad|crop|stretch` (default `blur`: video centred over a blurred copy of itself)
  - `--only 1080x1920 1280x720` to export a subset
  - `-o folder` output folder (default `resized/` next to the input)

Each output is encoded at the best quality that fits under `--max-mb` (CRF first, then two-pass
bitrate targeting if needed). Output is H.264/AAC MP4 with faststart.
