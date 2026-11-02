#!/usr/bin/env python3
"""
Ad-creative video resizer (runs locally, needs ffmpeg).

Takes ONE source video (e.g. 1080x1080) and exports every ad resolution
(1080x1920, 1080x1080, 1920x1080, 768x1024, 1024x768, 720x1280, 1280x720,
1080x1350) with a hard cap on each output's file size.

Usage
-----
  GUI:  python resize_video.py
  CLI:  python resize_video.py input.mp4 --max-mb 4
        python resize_video.py input.mp4 --max-mb 2 --mode crop -o out_dir
        python resize_video.py input.mp4 --max-mb 4 --only 1080x1920 1280x720

Fit modes (for sizes whose aspect ratio differs from the source):
  blur   - source centred on a blurred, zoomed copy of itself (default)
  pad    - source centred on black bars
  crop   - centre-crop to fill the frame
  stretch- distort to fill (not recommended)
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

RESOLUTIONS = [
    (1080, 1920), (1080, 1080), (1920, 1080), (768, 1024),
    (1024, 768), (720, 1280), (1280, 720), (1080, 1350),
]
AUDIO_KBPS = 96
SAFETY = 0.96          # aim slightly under the cap
MAX_RETRIES = 4        # bitrate reductions if output still exceeds the cap
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def need(tool):
    path = shutil.which(tool)
    if not path:
        sys.exit(f"ERROR: '{tool}' not found. Install ffmpeg "
                 "(https://ffmpeg.org/download.html) and add it to PATH.")
    return path


def probe(path):
    out = subprocess.run(
        [need("ffprobe"), "-v", "error", "-show_streams", "-show_format",
         "-of", "json", path],
        capture_output=True, text=True, check=True, creationflags=NO_WINDOW)
    info = json.loads(out.stdout)
    streams = info["streams"]
    has_audio = any(s["codec_type"] == "audio" for s in streams)
    duration = float(info["format"]["duration"])
    return duration, has_audio


def build_filter(w, h, mode):
    if mode == "blur":
        return (f"[0:v]split=2[a][b];"
                f"[a]scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h},boxblur=30:5[bg];"
                f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p[v]")
    if mode == "pad":
        return (f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,"
                f"format=yuv420p[v]")
    if mode == "crop":
        return (f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h},setsar=1,format=yuv420p[v]")
    return f"[0:v]scale={w}:{h},setsar=1,format=yuv420p[v]"   # stretch


def run(cmd, log=None):
    p = subprocess.run(cmd, capture_output=True, text=True,
                       creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-1500:])
    return p


def encode(src, dst, w, h, mode, max_bytes, duration, has_audio, log):
    ffmpeg = need("ffmpeg")
    flt = build_filter(w, h, mode)
    audio = (["-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k"] if has_audio
             else ["-an"])
    base = [ffmpeg, "-y", "-v", "error", "-i", src,
            "-filter_complex", flt, "-map", "[v]"]
    if has_audio:
        base += ["-map", "0:a:0"]
    tail = ["-c:v", "libx264", "-preset", "slow", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart"] + audio

    # Pass A: quality-first (CRF) with a bitrate ceiling.
    audio_bps = AUDIO_KBPS * 1000 if has_audio else 0
    target_v = max(int((max_bytes * 8 * SAFETY) / duration) - audio_bps,
                   50_000)
    run(base + tail + ["-crf", "21", "-maxrate", str(target_v),
                       "-bufsize", str(target_v * 2), dst])
    if os.path.getsize(dst) <= max_bytes:
        return os.path.getsize(dst)

    # Pass B: two-pass at target bitrate, shrinking until it fits.
    for attempt in range(MAX_RETRIES + 1):
        log(f"    over cap, two-pass @ {target_v // 1000} kbps")
        with tempfile.TemporaryDirectory() as td:
            pl = os.path.join(td, "pass")
            vb = ["-b:v", str(target_v)]
            run(base + ["-c:v", "libx264", "-preset", "slow",
                        "-pix_fmt", "yuv420p"] + vb +
                ["-pass", "1", "-passlogfile", pl, "-an", "-f", "null",
                 os.devnull])
            run(base + tail + vb + ["-pass", "2", "-passlogfile", pl, dst])
        size = os.path.getsize(dst)
        if size <= max_bytes:
            return size
        target_v = int(target_v * (max_bytes / size) * 0.93)
        if target_v < 30_000:
            break
    raise RuntimeError(f"could not fit {w}x{h} under the size cap "
                       "(try a higher cap or a shorter video)")


def process(src, out_dir, max_mb, mode, only=None, log=print, stop=lambda: False):
    duration, has_audio = probe(src)
    max_bytes = int(max_mb * 1024 * 1024)
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src))[0]
    sizes = [(w, h) for (w, h) in RESOLUTIONS
             if not only or f"{w}x{h}" in only]
    log(f"Source: {duration:.1f}s, audio={'yes' if has_audio else 'no'}; "
        f"cap {max_mb} MB; mode {mode}")
    results = []
    for w, h in sizes:
        if stop():
            log("Stopped.")
            break
        dst = os.path.join(out_dir, f"{stem}_{w}x{h}.mp4")
        log(f"- {w}x{h} ...")
        try:
            size = encode(src, dst, w, h, mode, max_bytes, duration,
                          has_audio, log)
            log(f"    OK  {size / 1048576:.2f} MB -> {dst}")
            results.append((w, h, size, None))
        except Exception as e:                      # keep going on failure
            log(f"    FAILED: {e}")
            results.append((w, h, 0, str(e)))
    log("Done.")
    return results


# ------------------------------- GUI -------------------------------------
def gui():
    import threading
    import tkinter as tk
    from tkinter import filedialog, ttk, scrolledtext

    root = tk.Tk()
    root.title("Ad Video Resizer")
    root.geometry("640x640")
    src_v, out_v = tk.StringVar(), tk.StringVar()
    cap_v, mode_v = tk.StringVar(value="4"), tk.StringVar(value="blur")
    stop_flag = {"v": False}

    def row(label, var, cmd):
        f = ttk.Frame(root); f.pack(fill="x", padx=10, pady=4)
        ttk.Label(f, text=label, width=14).pack(side="left")
        ttk.Entry(f, textvariable=var).pack(side="left", fill="x", expand=True)
        ttk.Button(f, text="Browse", command=cmd).pack(side="left", padx=4)

    def pick_src():
        p = filedialog.askopenfilename(
            filetypes=[("Video", "*.mp4 *.mov *.mkv *.avi *.webm"), ("All", "*.*")])
        if p:
            src_v.set(p)
            if not out_v.get():
                out_v.set(os.path.join(os.path.dirname(p), "resized"))

    def pick_out():
        p = filedialog.askdirectory()
        if p:
            out_v.set(p)

    row("Source video", src_v, pick_src)
    row("Output folder", out_v, pick_out)

    f = ttk.Frame(root); f.pack(fill="x", padx=10, pady=4)
    ttk.Label(f, text="Max size (MB)", width=14).pack(side="left")
    ttk.Entry(f, textvariable=cap_v, width=8).pack(side="left")
    ttk.Label(f, text="   Fit mode").pack(side="left")
    ttk.Combobox(f, textvariable=mode_v, width=10, state="readonly",
                 values=["blur", "pad", "crop", "stretch"]).pack(side="left", padx=6)

    box = ttk.LabelFrame(root, text="Resolutions")
    box.pack(fill="x", padx=10, pady=6)
    checks = {}
    for i, (w, h) in enumerate(RESOLUTIONS):
        v = tk.BooleanVar(value=True)
        checks[f"{w}x{h}"] = v
        ttk.Checkbutton(box, text=f"{w}x{h}", variable=v).grid(
            row=i // 4, column=i % 4, sticky="w", padx=8, pady=2)

    logbox = scrolledtext.ScrolledText(root, height=14, state="disabled")
    logbox.pack(fill="both", expand=True, padx=10, pady=6)

    def log(msg):
        def w():
            logbox.configure(state="normal")
            logbox.insert("end", msg + "\n")
            logbox.see("end")
            logbox.configure(state="disabled")
        root.after(0, w)

    btn = ttk.Button(root, text="Export all")
    stopb = ttk.Button(root, text="Stop")

    def work(src, out, cap, mode, only):
        try:
            process(src, out, cap, mode, only, log, lambda: stop_flag["v"])
        except Exception as e:
            log(f"ERROR: {e}")
        root.after(0, lambda: btn.configure(state="normal"))

    def start():
        try:
            cap = float(cap_v.get())
        except ValueError:
            return log("Max size must be a number.")
        if not os.path.isfile(src_v.get()):
            return log("Choose a source video.")
        only = [k for k, v in checks.items() if v.get()]
        stop_flag["v"] = False
        btn.configure(state="disabled")
        threading.Thread(target=work, daemon=True, args=(
            src_v.get(), out_v.get(), cap, mode_v.get(), only)).start()

    btn.configure(command=start)
    stopb.configure(command=lambda: stop_flag.update(v=True))
    btn.pack(side="left", padx=10, pady=8)
    stopb.pack(side="left")
    root.mainloop()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="source video (omit to open the GUI)")
    ap.add_argument("-o", "--out", help="output folder (default: ./resized next to input)")
    ap.add_argument("--max-mb", type=float, default=4.0,
                    help="max file size per output in MB (default 4)")
    ap.add_argument("--mode", choices=["blur", "pad", "crop", "stretch"],
                    default="blur")
    ap.add_argument("--only", nargs="+", metavar="WxH",
                    help="export only these sizes, e.g. 1080x1920 1280x720")
    a = ap.parse_args()
    if not a.input:
        return gui()
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.input)), "resized")
    res = process(a.input, out, a.max_mb, a.mode, a.only)
    sys.exit(1 if any(r[3] for r in res) else 0)


if __name__ == "__main__":
    main()
