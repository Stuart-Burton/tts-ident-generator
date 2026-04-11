#!/usr/bin/env python3
"""
Computer-Generated 16ch Slate Generator (Windows 11)

Creates a moving video background (testsrc2) with centred overlay text, optionally
with a dimmed box behind the text, plus 16 mono PCM tracks (48k/24-bit) each:

AUDIO BEHAVIOR (as requested):
  1) common spoken phrase plays ONCE at the very start on ALL 16 channels
  2) channel ID phrases play in four groups:
       - group 1: 1, 5, 9, 13 (simultaneous)
       - group 2: 2, 6, 10, 14
       - group 3: 3, 7, 11, 15
       - group 4: 4, 8, 12, 16
  3) NO tone is present until ALL spoken IDs have completed
  4) then 1kHz tone starts on ALL channels and pads to the end of the clip

Video:
  - 1920x1080 assumed
  - 50p or 59.94p (progressive) OR 50i/59.94i (interlaced)
  - Field order: TFF/BFF (interlaced only)
  - Codec choices: ProRes, DNxHD, DNxHR
  - Wrapper: MXF by default

Text:
  - Centred overlay
  - Optional dim box behind text via drawtext box=1 and boxcolor=black@alpha

GUI:
  - Tkinter form for output destination & filename, overlay text, common spoken text,
    codec, rate, scan type, field order, duration, font size, and box enable.
  - Progress log window (stdout + ffmpeg stderr).

Requirements:
  - ffmpeg on PATH OR set in GUI
  - Windows PowerShell (System.Speech TTS)
"""

from __future__ import annotations

import os
import re
import time
import queue
import shutil
import threading
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import tkinter as tk
from tkinter import ttk, filedialog, messagebox


# ---------------------------- Defaults ----------------------------

DEFAULT_W = 1920
DEFAULT_H = 1080

DEFAULT_DURATION_S = 15.0

DEFAULT_FONT_SIZE = 300
DEFAULT_BOX_ENABLED = True
DEFAULT_BOX_ALPHA = 0.5          # ~50% dim behind the text
DEFAULT_BOX_PAD_FACTOR = 0.20    # scales with fontsize (boxborderw)

DEFAULT_TONE_HZ = 1000
DEFAULT_TONE_VOLUME = 0.9        # you calibrated this as correct (-20dBFS-ish in your chain)

DEFAULT_SAMPLE_RATE = 48000
DEFAULT_SAMPLE_FMT = "pcm_s24le"

DEFAULT_WRAPPER_EXT = ".mxf"

CODEC_CHOICES = ["DNxHD", "DNxHR", "ProRes"]
RATE_CHOICES = ["50", "59.94"]
SCAN_CHOICES = ["Progressive", "Interlaced"]
FIELD_ORDER_CHOICES = ["TFF", "BFF"]


# ---------------------------- Utilities ----------------------------

def which_ffmpeg() -> Optional[str]:
    """Find ffmpeg executable."""
    p = shutil.which("ffmpeg")
    if p:
        return p
    candidates = [
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def log_time() -> str:
    return time.strftime("%H:%M:%S")


def run_subprocess(
    args: List[str],
    log_cb,
    check: bool = True,
    cwd: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Run a subprocess, streaming output to log_cb."""
    log_cb(f"$ {' '.join(args)}")
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=cwd,
        text=True,
        universal_newlines=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    out_lines: List[str] = []
    for line in proc.stdout:
        line = line.rstrip("\n")
        out_lines.append(line)
        log_cb(line)
    rc = proc.wait()
    cp = subprocess.CompletedProcess(args=args, returncode=rc, stdout="\n".join(out_lines), stderr=None)
    if check and rc != 0:
        raise RuntimeError(f"Command failed (rc={rc}): {' '.join(args)}")
    return cp


def drawtext_escape(s: str) -> str:
    """
    Escape for FFmpeg drawtext when using text='...'.
    """
    s = s.replace("\\", r"\\")
    s = s.replace("'", r"\'")
    s = s.replace(":", r"\:")
    s = s.replace("%", r"\%")
    return s


def compute_rates(rate_label: str, scan: str) -> Tuple[str, str, str]:
    """
    Return (src_rate, out_rate, descriptor).
    We generate at field-rate for interlaced (e.g. 60000/1001) and then interleave fields,
    outputting frame-rate (e.g. 30000/1001).
    """
    if rate_label == "50":
        if scan == "Interlaced":
            return "50", "25", "50i (25fps, 50 fields)"
        return "50", "50", "50p"
    else:
        # 59.94
        if scan == "Interlaced":
            return "60000/1001", "30000/1001", "59.94i (29.97fps, 59.94 fields)"
        return "60000/1001", "60000/1001", "59.94p"


# ---------------------------- Codec settings ----------------------------

@dataclass
class VideoEncodeSettings:
    vcodec: str
    pix_fmt: str
    extra: List[str]


def video_settings(codec: str, rate_label: str, scan: str) -> VideoEncodeSettings:
    """
    Choose a sensible "standard" profile. You can tune further if needed.
    """
    if codec == "DNxHD":
        if scan == "Interlaced":
            if rate_label == "50":
                return VideoEncodeSettings("dnxhd", "yuv422p", ["-b:v", "120M"])
            return VideoEncodeSettings("dnxhd", "yuv422p", ["-b:v", "145M"])
        # Progressive: DNxHR often safer for 1080p50/59.94, but we keep a best-effort here.
        if rate_label == "50":
            return VideoEncodeSettings("dnxhd", "yuv422p", ["-b:v", "175M"])
        return VideoEncodeSettings("dnxhd", "yuv422p", ["-b:v", "220M"])

    if codec == "DNxHR":
        return VideoEncodeSettings(
            "dnxhd",
            "yuv422p",
            ["-profile:v", "dnxhr_hq"]
        )

    # ProRes
    return VideoEncodeSettings(
        "prores_ks",
        "yuv422p10le",
        ["-profile:v", "3", "-vendor", "apl0"]
    )


# ---------------------------- TTS generation (Windows) ----------------------------

def generate_tts_wav(
    phrase: str,
    wav_path: Path,
    ffmpeg_path: str,
    log_cb,
    voice: Optional[str] = None,
    rate: int = 0,
    volume: int = 100,
) -> float:
    """
    Generate a mono 48k/24-bit PCM wav at wav_path from System.Speech via PowerShell.

    Returns duration seconds of the resulting wav (best-effort from ffmpeg -i parsing).
    """
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = wav_path.with_suffix(".tmp.wav")

    ps_phrase = phrase.replace('"', '""')
    ps_tmp = str(tmp).replace("\\", "\\\\")

    voice_snippet = ""
    if voice:
        ps_voice = voice.replace('"', '""')
        voice_snippet = f'$speak.SelectVoice("{ps_voice}");'

    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$speak = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        f"$speak.Rate = {rate};"
        f"$speak.Volume = {volume};"
        f"{voice_snippet}"
        f'$speak.SetOutputToWaveFile("{ps_tmp}");'
        f'$speak.Speak("{ps_phrase}");'
        "$speak.Dispose();"
    )

    powershell = os.path.join(
        os.environ.get("WINDIR", r"C:\Windows"),
        r"System32\WindowsPowerShell\v1.0\powershell.exe"
    )

    run_subprocess(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        log_cb=log_cb,
        check=True,
    )

    # Convert to 48k/24-bit mono PCM
    run_subprocess(
        [
            ffmpeg_path, "-y",
            "-i", str(tmp),
            "-ac", "1",
            "-ar", str(DEFAULT_SAMPLE_RATE),
            "-c:a", DEFAULT_SAMPLE_FMT,
            str(wav_path),
        ],
        log_cb=log_cb,
        check=True,
    )

    try:
        tmp.unlink(missing_ok=True)  # py>=3.8 on Win supports this
    except Exception:
        pass

    # Determine duration from ffmpeg -i output
    cp = subprocess.run(
        [ffmpeg_path, "-hide_banner", "-i", str(wav_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)", cp.stdout or "")
    if not m:
        return 0.0
    hh, mm, ss, frac = m.groups()
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + (int(frac) / (10 ** len(frac)))


# ---------------------------- FFmpeg build ----------------------------

def build_filter_complex_video(
    overlay_text: str,
    fontsize: int,
    scan: str,
    field_order: str,
    box_enabled: bool,
    box_alpha: float,
    box_pad_factor: float,
) -> str:
    """
    Build video filtergraph that outputs [v] label.
    Uses drawtext's built-in 'box' feature for a dimmed background behind text.
    """
    text_esc = drawtext_escape(overlay_text)
    boxborderw = max(4, int(fontsize * box_pad_factor))

    dt = (
        f"drawtext=font=Arial:"
        f"text='{text_esc}':"
        f"fontsize={fontsize}:"
        f"fontcolor=white:"
        f"x=(w-text_w)/2:"
        f"y=(h-text_h)/2"
    )
    if box_enabled:
        dt += f":box=1:boxcolor=black@{box_alpha}:boxborderw={boxborderw}"

    vf = f"[0:v]{dt}[preil]"
    if scan == "Interlaced":
        if field_order == "TFF":
            vf += ";[preil]tinterlace=mode=interleave_top,setfield=tff[v]"
        else:
            vf += ";[preil]tinterlace=mode=interleave_bottom,setfield=bff[v]"
    else:
        vf += ";[preil]null[v]"

    return vf


def build_filter_complex_audio_common_once_then_grouped(
    duration_s: float,
    tone_volume: float,
    common_duration_s: float,
    ch_durations_s: List[float],
    group_gap_s: float = 0.0,
) -> str:
    """
    Build audio filtergraph that outputs [aout1]..[aout16] with this timeline:

      0) common.wav ONCE at the very start, on all 16 channels simultaneously
      1) group 1 IDs: 1,5,9,13 (simultaneous)
      2) group 2 IDs: 2,6,10,14
      3) group 3 IDs: 3,7,11,15
      4) group 4 IDs: 4,8,12,16
      5) tone starts ONLY after (4) completes, on all channels, padding to end

    Inputs:
      #1 common.wav
      #2..#17 ch01..ch16 wavs
      #18 sine tone

    Implementation approach:
      - For each channel: create full-length silence bed
      - Mix in common at t=0 (no delay)
      - Mix in that channel's ID wav delayed to its group start time
      - Mix in tone delayed to tone_start (after last group end)
    """
    if len(ch_durations_s) != 16:
        raise ValueError("Need 16 per-channel ID durations")

    groups = [
        [1, 5, 9, 13],
        [2, 6, 10, 14],
        [3, 7, 11, 15],
        [4, 8, 12, 16],
    ]

    # Group starts happen AFTER the common phrase ends
    t = common_duration_s
    group_starts: List[float] = []
    for g in groups:
        group_starts.append(t)
        g_len = max(ch_durations_s[ch - 1] for ch in g)  # only ID duration now
        t += g_len + group_gap_s

    tone_start_s = t
    tone_start_ms = int(round(tone_start_s * 1000.0))

    parts: List[str] = []

    # Split common into 16 (independent) for mixing
    parts.append(
        "[1:a]"
        f"aresample={DEFAULT_SAMPLE_RATE},asetpts=PTS-STARTPTS,"
        "asplit=16"
        + "".join([f"[c{ch}]" for ch in range(1, 17)])
    )

    # Label each channel ID input as [id1]..[id16]
    # Inputs: #2..#17 correspond to ch01..ch16
    for ch in range(1, 17):
        idx = 1 + ch  # 2..17
        parts.append(
            f"[{idx}:a]"
            f"aresample={DEFAULT_SAMPLE_RATE},asetpts=PTS-STARTPTS"
            f"[id{ch}]"
        )

    # Full-length silence bed per channel
    for ch in range(1, 17):
        parts.append(
            f"anullsrc=r={DEFAULT_SAMPLE_RATE}:cl=mono,atrim=0:{duration_s},asetpts=PTS-STARTPTS"
            f"[sil{ch}]"
        )

    # Delay each channel's ID to its group's slot
    for ch in range(1, 17):
        g_index = next(i for i, g in enumerate(groups) if ch in g)
        start_ms = int(round(group_starts[g_index] * 1000.0))
        parts.append(f"[id{ch}]adelay={start_ms}:all=1[id_d{ch}]")

    # Tone, delayed until all speech complete
    parts.append(f"[18:a]aformat=channel_layouts=mono,volume={tone_volume},asetpts=PTS-STARTPTS[tone0]")
    parts.append(f"[tone0]adelay={tone_start_ms}:all=1,atrim=0:{duration_s},asetpts=PTS-STARTPTS[tone_del]")
    parts.append("[tone_del]asplit=16" + "".join([f"[t{ch}]" for ch in range(1, 17)]))

    # Final per-channel mix: silence + common + delayed id + delayed tone, then trim
    for ch in range(1, 17):
        parts.append(
            f"[sil{ch}][c{ch}][id_d{ch}][t{ch}]"
            f"amix=inputs=4:dropout_transition=0:normalize=0,atrim=0:{duration_s}"
            f"[aout{ch}]"
        )

    return ";".join(parts)


def build_ffmpeg_command(
    ffmpeg_path: str,
    output_path: Path,
    overlay_text: str,
    spoken_text: str,
    codec: str,
    rate_label: str,
    scan: str,
    field_order: str,
    duration_s: float,
    fontsize: int,
    box_enabled: bool,
    box_alpha: float,
    box_pad_factor: float,
    tts_dir: Path,
    log_cb,
) -> List[str]:
    """
    Generates TTS wavs and constructs the full ffmpeg command line.
    """
    src_rate, out_rate, rate_desc = compute_rates(rate_label, scan)
    log_cb(f"[info] Mode: {DEFAULT_W}x{DEFAULT_H} {rate_desc} {scan} ({field_order.lower() if scan=='Interlaced' else 'n/a'})")

    # --- Generate TTS WAV files ---
    log_cb("=== TTS generation ===")
    tts_dir.mkdir(parents=True, exist_ok=True)

    # Common (once)
    common_wav = tts_dir / "common.wav"
    common_dur = generate_tts_wav(spoken_text, common_wav, ffmpeg_path, log_cb=log_cb)
    log_cb(f"[info] common speech duration: {common_dur:.3f}s")

    # Per-channel IDs
    ch_wavs: List[Path] = []
    ch_durs: List[float] = []
    for ch in range(1, 17):
        wav = tts_dir / f"ch{ch:02d}.wav"
        d = generate_tts_wav(f"Channel {ch}.", wav, ffmpeg_path, log_cb=log_cb)
        ch_wavs.append(wav)
        ch_durs.append(d)

    # Tone input is generated full-length; we delay it in the filtergraph.
    tone_input = f"sine=frequency={DEFAULT_TONE_HZ}:sample_rate={DEFAULT_SAMPLE_RATE}:duration={duration_s}"

    # --- Video filtergraph ---
    vf = build_filter_complex_video(
        overlay_text=overlay_text,
        fontsize=fontsize,
        scan=scan,
        field_order=field_order,
        box_enabled=box_enabled,
        box_alpha=box_alpha,
        box_pad_factor=box_pad_factor,
    )

    # --- Audio filtergraph (common once, grouped IDs, tone only after all speech) ---
    af = build_filter_complex_audio_common_once_then_grouped(
        duration_s=duration_s,
        tone_volume=DEFAULT_TONE_VOLUME,
        common_duration_s=common_dur,
        ch_durations_s=ch_durs,
        group_gap_s=0.0,   # change to e.g. 0.10 if you want a small gap between groups
    )

    filter_complex = vf + ";" + af

    # --- Codec settings ---
    vs = video_settings(codec, rate_label, scan)

    # --- Build command ---
    cmd: List[str] = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        # Video source
        "-f", "lavfi",
        "-i", f"testsrc2=size={DEFAULT_W}x{DEFAULT_H}:rate={src_rate}",
        # Audio inputs
        "-i", str(common_wav),
    ]
    for w in ch_wavs:
        cmd += ["-i", str(w)]
    cmd += [
        "-f", "lavfi", "-i", tone_input,
        "-filter_complex", filter_complex,
        "-map", "[v]",
    ]
    for ch in range(1, 17):
        cmd += ["-map", f"[aout{ch}]"]

    # Duration and CFR
    cmd += [
        "-t", f"{duration_s}",
        "-fps_mode", "cfr",
        "-r", out_rate,
    ]

    if scan == "Interlaced":
        cmd += ["-flags", "+ilme+ildct"]

    # Video encode
    cmd += ["-c:v", vs.vcodec, "-pix_fmt", vs.pix_fmt]
    cmd += vs.extra

    # Audio encode
    cmd += [
        "-c:a", DEFAULT_SAMPLE_FMT,
        "-ar", str(DEFAULT_SAMPLE_RATE),
        "-ac", "1",
    ]

    cmd += [str(output_path)]
    return cmd


# ---------------------------- Progress parsing ----------------------------

_PROGRESS_RE = re.compile(r"(frame=\s*(\d+).+?time=\s*([0-9:\.]+).+?speed=\s*([0-9\.x]+))")


def parse_time_to_seconds(ts: str) -> float:
    parts = ts.split(":")
    if len(parts) != 3:
        return 0.0
    h = int(parts[0])
    m = int(parts[1])
    s = float(parts[2])
    return h * 3600 + m * 60 + s


# ---------------------------- GUI ----------------------------

class SlateGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("16ch Slate Generator")

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None
        self.stop_requested = threading.Event()

        self.ffmpeg_path_var = tk.StringVar(value=which_ffmpeg() or "")
        self.out_dir_var = tk.StringVar(value=str(Path.home() / "Videos"))
        self.base_name_var = tk.StringVar(value="SLATE")
        self.overlay_text_var = tk.StringVar(value="SLATE")
        self.spoken_text_var = tk.StringVar(value="Slate identification.")
        self.codec_var = tk.StringVar(value="DNxHD")
        self.rate_var = tk.StringVar(value="59.94")
        self.scan_var = tk.StringVar(value="Interlaced")
        self.field_order_var = tk.StringVar(value="TFF")
        self.duration_var = tk.StringVar(value=str(DEFAULT_DURATION_S))
        self.fontsize_var = tk.StringVar(value=str(DEFAULT_FONT_SIZE))
        self.box_enabled_var = tk.BooleanVar(value=DEFAULT_BOX_ENABLED)

        self._build_ui()
        self._poll_log_queue()

    def _build_ui(self):
        frm = ttk.Frame(self.root, padding=10)
        frm.grid(row=0, column=0, sticky="nsew")

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        frm.columnconfigure(1, weight=1)

        r = 0

        def add_row(label: str, widget: tk.Widget, button: Optional[tk.Widget] = None):
            nonlocal r
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", pady=2)
            widget.grid(row=r, column=1, sticky="ew", pady=2)
            if button:
                button.grid(row=r, column=2, sticky="ew", padx=(6, 0))
            r += 1

        ff_entry = ttk.Entry(frm, textvariable=self.ffmpeg_path_var)
        ff_btn = ttk.Button(frm, text="Browse...", command=self._browse_ffmpeg)
        add_row("FFmpeg path", ff_entry, ff_btn)

        out_entry = ttk.Entry(frm, textvariable=self.out_dir_var)
        out_btn = ttk.Button(frm, text="Browse...", command=self._browse_outdir)
        add_row("Output folder", out_entry, out_btn)

        add_row("Base filename", ttk.Entry(frm, textvariable=self.base_name_var))
        add_row("Overlay text", ttk.Entry(frm, textvariable=self.overlay_text_var))

        # Clarified label: common phrase happens ONCE at the start
        add_row("Speech text (common, once)", ttk.Entry(frm, textvariable=self.spoken_text_var))

        add_row("Codec", ttk.Combobox(frm, textvariable=self.codec_var, values=CODEC_CHOICES, state="readonly"))
        add_row("Frame rate", ttk.Combobox(frm, textvariable=self.rate_var, values=RATE_CHOICES, state="readonly"))
        add_row("Scan", ttk.Combobox(frm, textvariable=self.scan_var, values=SCAN_CHOICES, state="readonly"))
        add_row("Field order", ttk.Combobox(frm, textvariable=self.field_order_var, values=FIELD_ORDER_CHOICES, state="readonly"))

        add_row("Duration (s)", ttk.Entry(frm, textvariable=self.duration_var))
        add_row("Font size", ttk.Entry(frm, textvariable=self.fontsize_var))

        box_chk = ttk.Checkbutton(frm, text="Enable dim box behind text (~50%)", variable=self.box_enabled_var)
        box_chk.grid(row=r, column=1, sticky="w", pady=4)
        r += 1

        btns = ttk.Frame(frm)
        btns.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(10, 6))
        btns.columnconfigure(0, weight=1)
        btns.columnconfigure(1, weight=1)
        btns.columnconfigure(2, weight=1)

        self.run_btn = ttk.Button(btns, text="Generate", command=self._start)
        self.run_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        self.stop_btn = ttk.Button(btns, text="Stop", command=self._stop, state="disabled")
        self.stop_btn.grid(row=0, column=1, sticky="ew", padx=(0, 6))

        self.clear_btn = ttk.Button(btns, text="Clear Log", command=self._clear_log)
        self.clear_btn.grid(row=0, column=2, sticky="ew")

        self.prog_var = tk.StringVar(value="Idle")
        ttk.Label(frm, textvariable=self.prog_var).grid(row=r + 1, column=0, columnspan=3, sticky="w", pady=(2, 6))

        self.pbar = ttk.Progressbar(frm, mode="determinate")
        self.pbar.grid(row=r + 2, column=0, columnspan=3, sticky="ew", pady=(0, 8))

        self.log = tk.Text(frm, height=20, width=120)
        self.log.grid(row=r + 3, column=0, columnspan=3, sticky="nsew")
        frm.rowconfigure(r + 3, weight=1)

    def _browse_ffmpeg(self):
        p = filedialog.askopenfilename(
            title="Select ffmpeg.exe",
            filetypes=[("ffmpeg.exe", "ffmpeg.exe"), ("All files", "*.*")]
        )
        if p:
            self.ffmpeg_path_var.set(p)

    def _browse_outdir(self):
        d = filedialog.askdirectory(title="Select output folder")
        if d:
            self.out_dir_var.set(d)

    def _clear_log(self):
        self.log.delete("1.0", "end")

    def _stop(self):
        self.stop_requested.set()
        self._log("[warn] Stop requested. Current step will finish then abort.")

    def _log(self, msg: str):
        self.log_queue.put(f"[{log_time()}] {msg}")

    def _poll_log_queue(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.log.insert("end", line + "\n")
                self.log.see("end")

                m = _PROGRESS_RE.search(line)
                if m:
                    frame = int(m.group(2))
                    t = parse_time_to_seconds(m.group(3))
                    speed = m.group(4)
                    self.prog_var.set(f"Encoding: frame {frame}, time {t:.2f}s, speed {speed}")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log_queue)

    def _start(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Busy", "A job is already running.")
            return

        ffmpeg_path = self.ffmpeg_path_var.get().strip()
        if not ffmpeg_path or not os.path.exists(ffmpeg_path):
            messagebox.showerror("FFmpeg not found", "Please select a valid ffmpeg.exe path.")
            return

        out_dir = Path(self.out_dir_var.get().strip())
        if not out_dir.exists():
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                messagebox.showerror("Invalid output folder", str(e))
                return

        base = self.base_name_var.get().strip()
        if not base:
            messagebox.showerror("Filename required", "Please enter a base filename.")
            return

        self.stop_requested.clear()

        self.run_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.pbar["value"] = 0
        self.pbar["maximum"] = 100
        self.prog_var.set("Starting...")

        self.worker_thread = threading.Thread(target=self._worker, daemon=True)
        self.worker_thread.start()

    def _worker(self):
        try:
            ffmpeg_path = self.ffmpeg_path_var.get().strip()
            out_dir = Path(self.out_dir_var.get().strip())
            base = self.base_name_var.get().strip()

            overlay_text = self.overlay_text_var.get().strip()
            spoken_text = self.spoken_text_var.get().strip()

            codec = self.codec_var.get().strip()
            rate_label = self.rate_var.get().strip()
            scan = self.scan_var.get().strip()
            field_order = self.field_order_var.get().strip()

            try:
                duration_s = float(self.duration_var.get().strip())
                if duration_s <= 0:
                    raise ValueError
            except Exception:
                raise ValueError("Duration must be a positive number.")

            try:
                fontsize = int(self.fontsize_var.get().strip())
                if fontsize <= 0:
                    raise ValueError
            except Exception:
                raise ValueError("Font size must be a positive integer.")

            box_enabled = bool(self.box_enabled_var.get())

            output_path = out_dir / f"{base}{DEFAULT_WRAPPER_EXT}"
            tts_dir = out_dir / "tts"

            self._log("=== Slate Generator ===")
            self._log(f"Output: {output_path}")

            if self.stop_requested.is_set():
                raise RuntimeError("Stopped.")

            self._log("=== FFmpeg build ===")

            cmd = build_ffmpeg_command(
                ffmpeg_path=ffmpeg_path,
                output_path=output_path,
                overlay_text=overlay_text,
                spoken_text=spoken_text,
                codec=codec,
                rate_label=rate_label,
                scan=scan,
                field_order=field_order,
                duration_s=duration_s,
                fontsize=fontsize,
                box_enabled=box_enabled,
                box_alpha=DEFAULT_BOX_ALPHA,
                box_pad_factor=DEFAULT_BOX_PAD_FACTOR,
                tts_dir=tts_dir,
                log_cb=self._log,
            )

            if self.stop_requested.is_set():
                raise RuntimeError("Stopped.")

            self._log("=== FFmpeg command ===")
            self._log("$ " + " ".join(cmd))

            self._run_ffmpeg_with_progress(cmd, duration_s)

            self._log("[done] Completed successfully.")
            self._set_progress_done()

        except Exception as e:
            self._log(f"[ERROR] {e}")
            self._set_progress_error()
        finally:
            self.root.after(0, lambda: self.run_btn.config(state="normal"))
            self.root.after(0, lambda: self.stop_btn.config(state="disabled"))

    def _run_ffmpeg_with_progress(self, cmd: List[str], duration_s: float):
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            universal_newlines=True,
            bufsize=1,
        )
        assert proc.stdout is not None

        last_pct = 0.0
        for line in proc.stdout:
            if self.stop_requested.is_set():
                proc.terminate()
                self._log("[warn] ffmpeg terminated.")
                break

            line = line.rstrip("\n")
            self._log(line)

            m = _PROGRESS_RE.search(line)
            if m:
                t = parse_time_to_seconds(m.group(3))
                pct = 0.0 if duration_s <= 0 else min(100.0, max(0.0, (t / duration_s) * 100.0))
                if pct >= last_pct + 0.5:
                    last_pct = pct
                    self.root.after(0, lambda v=pct: self._set_progress_value(v))

        rc = proc.wait()
        if self.stop_requested.is_set():
            raise RuntimeError("Stopped.")
        if rc != 0:
            raise RuntimeError(f"ffmpeg failed (rc={rc}). See log above.")

    def _set_progress_value(self, pct: float):
        self.pbar["value"] = pct

    def _set_progress_done(self):
        self.root.after(0, lambda: self.pbar.config(value=100))
        self.root.after(0, lambda: self.prog_var.set("Done"))

    def _set_progress_error(self):
        self.root.after(0, lambda: self.prog_var.set("Error (see log)"))


def main():
    root = tk.Tk()
    _ = SlateGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
