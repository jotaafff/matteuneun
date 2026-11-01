# -*- coding: utf-8 -*-
"""마뜨는구간삭제 - human-speech inactivity detector and manual cut assistant.

Windows desktop MVP. Requires FFmpeg/ffprobe on PATH, plus packages in requirements.txt.
Original media is never modified. Selected intervals are only removed on export.
"""
from __future__ import annotations

import json
import math
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "마뜨는구간삭제"
APP_VERSION = "0.1.0"
SAMPLE_RATE = 16000
FRAME_MS = 10
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts", ".mts"}


def app_data_dir() -> Path:
    if os.name == "nt":
        root = Path(os.getenv("LOCALAPPDATA", str(Path.home())))
    else:
        root = Path(os.getenv("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    path = root / "MatteuneunDelete"
    path.mkdir(parents=True, exist_ok=True)
    return path


APP_DATA = app_data_dir()
DOWNLOAD_DIR = APP_DATA / "downloads"
PREVIEW_DIR = APP_DATA / "previews"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
PREVIEW_DIR.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Segment:
    index: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def fmt_time(value: float) -> str:
    value = max(0.0, value)
    centiseconds = int(round(value * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    seconds, cs = divmod(remainder, 100)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{cs:02d}"


def run_command(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    result = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "알 수 없는 FFmpeg 오류").strip()
        raise RuntimeError(detail[-3500:])
    return result


def require_ffmpeg() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError(
            "FFmpeg 또는 ffprobe를 찾을 수 없습니다. FFmpeg를 설치한 뒤 PATH에 추가하고 프로그램을 다시 실행하세요.\n"
            "설치 안내는 README_KO.md의 'FFmpeg 설치' 항목을 참고하세요."
        )
    return ffmpeg, ffprobe


def probe_media(path: str | Path) -> dict:
    _, ffprobe = require_ffmpeg()
    result = run_command([
        ffprobe, "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,width,height,r_frame_rate",
        "-of", "json", str(path),
    ])
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("영상 정보를 읽을 수 없습니다.") from exc
    duration = float(data.get("format", {}).get("duration") or 0)
    streams = data.get("streams", [])
    has_video = any(s.get("codec_type") == "video" for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if duration <= 0 or not has_video:
        raise RuntimeError("유효한 영상 스트림 또는 재생 시간을 확인할 수 없습니다.")
    return {"duration": duration, "has_video": has_video, "has_audio": has_audio, "streams": streams}


def extract_audio_16k(source: str | Path, wav_path: str | Path) -> None:
    ffmpeg, _ = require_ffmpeg()
    run_command([
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
        "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(wav_path),
    ])


def _smooth_vad_flags(flags: list[bool]) -> list[bool]:
    """Bridge isolated 10-20 ms voiced blips inside non-speech runs."""
    if len(flags) < 3:
        return flags
    smoothed = flags[:]
    i = 0
    n = len(flags)
    while i < n:
        j = i + 1
        while j < n and flags[j] == flags[i]:
            j += 1
        run_len = j - i
        if flags[i] and run_len <= 2 and i > 0 and j < n and not flags[i - 1] and not flags[j]:
            for k in range(i, j):
                smoothed[k] = False
        i = j
    return smoothed


def detect_non_speech_segments(
    wav_path: str | Path,
    duration: float,
    min_delete_seconds: float = 0.30,
    protect_seconds: float = 0.10,
    aggressiveness: int = 2,
) -> list[Segment]:
    """Detect non-voiced stretches with 10 ms WebRTC VAD frames.

    Times are rounded to the nearest 10 ms. ``protect_seconds`` is retained on
    both ends of every detected non-speech run to reduce the risk of clipping
    consonants or breaths adjacent to speech.
    """
    try:
        import webrtcvad
    except ImportError as exc:
        raise RuntimeError("음성 감지 패키지가 없습니다. install_windows.bat을 실행하세요.") from exc

    if not 0 <= aggressiveness <= 3:
        raise ValueError("VAD 민감도는 0~3이어야 합니다.")

    with wave.open(str(wav_path), "rb") as wav_file:
        if wav_file.getnchannels() != 1 or wav_file.getsampwidth() != 2 or wav_file.getframerate() != SAMPLE_RATE:
            raise RuntimeError("내부 오디오 형식이 올바르지 않습니다. 16kHz 모노 PCM이 필요합니다.")
        pcm = wav_file.readframes(wav_file.getnframes())

    samples_total = len(pcm) // 2
    total_frames = math.ceil(samples_total / FRAME_SAMPLES)
    vad = webrtcvad.Vad(aggressiveness)
    flags: list[bool] = []
    for frame_index in range(total_frames):
        start_byte = frame_index * FRAME_SAMPLES * 2
        end_byte = start_byte + FRAME_SAMPLES * 2
        frame = pcm[start_byte:end_byte]
        if len(frame) < FRAME_SAMPLES * 2:
            frame += b"\x00" * (FRAME_SAMPLES * 2 - len(frame))
        flags.append(bool(vad.is_speech(frame, SAMPLE_RATE)))

    flags = _smooth_vad_flags(flags)
    raw_runs: list[tuple[int, int]] = []
    index = 0
    while index < len(flags):
        if flags[index]:
            index += 1
            continue
        run_start = index
        while index < len(flags) and not flags[index]:
            index += 1
        raw_runs.append((run_start, index))

    segments: list[Segment] = []
    for frame_start, frame_end in raw_runs:
        raw_start = frame_start * FRAME_MS / 1000.0
        raw_end = min(frame_end * FRAME_MS / 1000.0, duration)
        cut_start = raw_start + max(0.0, protect_seconds)
        cut_end = raw_end - max(0.0, protect_seconds)
        cut_start = min(max(0.0, cut_start), duration)
        cut_end = min(max(0.0, cut_end), duration)
        cut_start = round(cut_start + 1e-8, 2)
        cut_end = round(cut_end + 1e-8, 2)
        if cut_end - cut_start >= min_delete_seconds and cut_end > cut_start:
            segments.append(Segment(len(segments) + 1, cut_start, cut_end))
    return segments


def validate_youtube_url(url: str) -> None:
    parsed = urlparse(url.strip())
    allowed = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() not in allowed:
        raise RuntimeError("youtube.com 또는 youtu.be 주소를 입력하세요.")


def download_youtube(url: str, progress: Callable[[str], None]) -> Path:
    validate_youtube_url(url)
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError("유튜브 URL 분석에 필요한 yt-dlp가 없습니다. install_windows.bat을 실행하세요.") from exc

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    before = {p.resolve() for p in DOWNLOAD_DIR.glob("*") if p.is_file()}

    def hook(info: dict) -> None:
        status = info.get("status")
        if status == "downloading":
            pct = info.get("_percent_str", "").strip()
            speed = info.get("_speed_str", "").strip()
            eta = info.get("_eta_str", "").strip()
            progress(f"유튜브 영상 다운로드 중: {pct} {speed} ETA {eta}".strip())
        elif status == "finished":
            progress("다운로드 완료. 영상 파일을 정리하고 있습니다.")

    options = {
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": str(DOWNLOAD_DIR / "%(title).80s [%(id)s].%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreconfig": True,
        "progress_hooks": [hook],
        "windowsfilenames": True,
    }
    progress("유튜브 영상 정보를 확인하고 있습니다.")
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url.strip(), download=True)
            prepared = Path(ydl.prepare_filename(info))
    except Exception as exc:
        raise RuntimeError(
            "유튜브 영상 가져오기에 실패했습니다. 비공개·연령 제한·지역 제한 영상이거나 YouTube 측 변경일 수 있습니다.\n"
            "본인에게 다운로드 권한이 있는 영상인지 확인하고 yt-dlp를 업데이트해 보세요.\n\n"
            f"상세 오류: {exc}"
        ) from exc

    candidates: list[Path] = []
    if prepared.exists():
        candidates.append(prepared)
    mp4_candidate = prepared.with_suffix(".mp4")
    if mp4_candidate.exists() and mp4_candidate not in candidates:
        candidates.append(mp4_candidate)
    new_files = [p for p in DOWNLOAD_DIR.glob("*") if p.is_file() and p.resolve() not in before]
    candidates.extend(sorted(new_files, key=lambda p: p.stat().st_mtime, reverse=True))
    for candidate in candidates:
        if candidate.suffix.lower() in VIDEO_EXTENSIONS and candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    raise RuntimeError("다운로드는 완료된 것으로 보이나 결과 영상 파일을 찾지 못했습니다.")


def _filter_escape(path: str | Path) -> str:
    # Used only for filter arguments containing quoted paths (concat demuxer list).
    return str(path).replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")


def render_ranges(
    source: str | Path,
    ranges: Iterable[tuple[float, float]],
    output: str | Path,
    *,
    video_crf: int = 19,
    preset: str = "medium",
) -> Path:
    """Re-encode and concatenate the requested source-time ranges, frame accurately."""
    ffmpeg, _ = require_ffmpeg()
    meta = probe_media(source)
    cleaned = [(max(0.0, float(s)), min(meta["duration"], float(e))) for s, e in ranges]
    cleaned = [(s, e) for s, e in cleaned if e - s >= 0.02]
    if not cleaned:
        raise RuntimeError("내보낼 영상 구간이 없습니다. 삭제 대상을 확인하세요.")

    graph: list[str] = []
    concat_inputs: list[str] = []
    for i, (start, end) in enumerate(cleaned):
        graph.append(f"[0:v:0]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{i}]")
        if meta["has_audio"]:
            graph.append(f"[0:a:0]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{i}]")
            concat_inputs.append(f"[v{i}][a{i}]")
        else:
            concat_inputs.append(f"[v{i}]")

    if meta["has_audio"]:
        graph.append("".join(concat_inputs) + f"concat=n={len(cleaned)}:v=1:a=1[vout][aout]")
    else:
        graph.append("".join(concat_inputs) + f"concat=n={len(cleaned)}:v=1:a=0[vout]")

    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
        "-filter_complex", ";".join(graph), "-map", "[vout]",
    ]
    if meta["has_audio"]:
        command += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
    command += [
        "-c:v", "libx264", "-preset", preset, "-crf", str(video_crf),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output_path),
    ]
    try:
        run_command(command)
    except Exception as exc:
        if output_path.exists():
            output_path.unlink(missing_ok=True)
        raise RuntimeError(f"영상 편집에 실패했습니다.\n{exc}") from exc
    return output_path


def make_side_by_side(before_path: Path, after_path: Path, output_path: Path, removed_duration: float) -> Path:
    ffmpeg, _ = require_ffmpeg()
    pad = max(0.0, removed_duration) + 0.15
    graph = (
        "[0:v]fps=30,scale=640:360:force_original_aspect_ratio=decrease,"
        "pad=640:360:(ow-iw)/2:(oh-ih)/2,setsar=1[v0];"
        f"[1:v]fps=30,scale=640:360:force_original_aspect_ratio=decrease,"
        f"pad=640:360:(ow-iw)/2:(oh-ih)/2,setsar=1,tpad=stop_mode=clone:stop_duration={pad:.3f}[v1];"
        "[v0][v1]hstack=inputs=2:shortest=1[v]"
    )
    run_command([
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(before_path), "-i", str(after_path),
        "-filter_complex", graph, "-map", "[v]", "-an",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(output_path),
    ])
    return output_path


def open_file_in_player(path: Path) -> None:
    if not path.exists():
        raise RuntimeError("재생 파일이 아직 생성되지 않았습니다.")
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class ScrollableFrame(ttk.Frame):
    def __init__(self, parent: tk.Widget, **kwargs):
        super().__init__(parent, **kwargs)
        self.canvas = tk.Canvas(self, highlightthickness=0, background="#f3f5f8")
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.window_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._update_scrollregion)
        self.canvas.bind("<Configure>", self._resize_inner)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _update_scrollregion(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _resize_inner(self, event):
        self.canvas.itemconfigure(self.window_id, width=event.width)

    def _on_mousewheel(self, event):
        try:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except tk.TclError:
            pass


class CompareDialog(tk.Toplevel):
    def __init__(self, parent: "MainWindow", segment: Segment):
        super().__init__(parent)
        self.parent = parent
        self.segment = segment
        self.title(f"구간 {segment.index} 삭제 전후 비교")
        self.geometry("660x330")
        self.transient(parent)
        self.grab_set()
        self.configure(padx=18, pady=16)

        start = max(0.0, segment.start - 7.0)
        end = min(parent.duration, segment.end + 7.0)
        self.context_start = start
        self.context_end = end
        self.before_path = PREVIEW_DIR / f"seg_{segment.index:04d}_before.mp4"
        self.after_path = PREVIEW_DIR / f"seg_{segment.index:04d}_after.mp4"
        self.side_path = PREVIEW_DIR / f"seg_{segment.index:04d}_compare.mp4"

        ttk.Label(self, text=f"구간 {segment.index} · {fmt_time(segment.start)} ~ {fmt_time(segment.end)}", font=("Malgun Gothic", 13, "bold")).pack(anchor="w")
        ttk.Label(
            self,
            text=f"비교 범위: {fmt_time(start)} ~ {fmt_time(end)}  (삭제 구간 앞뒤 최대 7초 포함)",
            foreground="#526071",
        ).pack(anchor="w", pady=(5, 14))

        panel = ttk.Frame(self)
        panel.pack(fill="x", pady=5)
        for column, (title, detail) in enumerate([
            ("삭제 전", "원본 구간 전체"),
            ("삭제 후", "해당 구간을 제거한 연결 영상"),
        ]):
            cell = ttk.LabelFrame(panel, text=title, padding=12)
            cell.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 8 if column == 0 else 0))
            ttk.Label(cell, text=detail, wraplength=240).pack(anchor="w", pady=(0, 8))
            ttk.Button(cell, text=f"{title} 영상 재생", command=lambda t=column: self.play_one(t)).pack(fill="x")
            panel.columnconfigure(column, weight=1)

        self.status = ttk.Label(self, text="비교 영상을 준비하는 중입니다.", foreground="#526071")
        self.status.pack(anchor="w", pady=(12, 6))
        self.compare_button = ttk.Button(self, text="분할 화면으로 비교 재생", command=self.play_side_by_side, state="disabled")
        self.compare_button.pack(fill="x")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._start_render()

    def _start_render(self):
        def work():
            try:
                before_ranges = [(self.context_start, self.context_end)]
                after_ranges = [
                    (self.context_start, self.segment.start),
                    (self.segment.end, self.context_end),
                ]
                render_ranges(self.parent.source_path, before_ranges, self.before_path, video_crf=22, preset="ultrafast")
                self.parent.ui_queue.put(("compare_progress", self, "삭제 후 영상을 생성하고 있습니다."))
                after_ranges = [(s, e) for s, e in after_ranges if e - s >= 0.02]
                if not after_ranges:
                    raise RuntimeError("비교 범위 안에 삭제 후 남길 영상이 없습니다.")
                render_ranges(self.parent.source_path, after_ranges, self.after_path, video_crf=22, preset="ultrafast")
                make_side_by_side(self.before_path, self.after_path, self.side_path, self.segment.duration)
                self.parent.ui_queue.put(("compare_done", self, None))
            except Exception as exc:
                self.parent.ui_queue.put(("compare_done", self, str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def play_one(self, which: int):
        path = self.before_path if which == 0 else self.after_path
        if not path.exists():
            messagebox.showinfo(APP_NAME, "비교 영상 생성이 완료될 때까지 기다리세요.", parent=self)
            return
        try:
            open_file_in_player(path)
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)

    def play_side_by_side(self):
        try:
            open_file_in_player(self.side_path)
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)

    def set_status(self, text: str, done: bool = False):
        self.status.configure(text=text)
        if done:
            self.compare_button.configure(state="normal")

    def close(self):
        self.grab_release()
        self.destroy()


class MainWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"{APP_NAME}  v{APP_VERSION}")
        self.root.geometry("1120x820")
        self.root.minsize(900, 680)
        self.root.configure(background="#f3f5f8")
        self.ui_queue: queue.Queue = queue.Queue()
        self.source_path: str | None = None
        self.duration = 0.0
        self.segments: list[Segment] = []
        self.delete_vars: dict[int, tk.BooleanVar] = {}
        self.row_widgets: list[tk.Widget] = []
        self.worker_busy = False
        self.preview_dialogs: set[CompareDialog] = set()

        self.url_var = tk.StringVar()
        self.file_var = tk.StringVar()
        self.min_gap_var = tk.StringVar(value="0.30")
        self.protect_var = tk.StringVar(value="0.10")
        self.vad_mode_var = tk.StringVar(value="2")
        self.status_var = tk.StringVar(value="유튜브 URL을 입력하거나 영상 파일을 선택하세요.")
        self.summary_var = tk.StringVar(value="아직 분석된 영상이 없습니다.")

        self._configure_style()
        self._build_ui()
        self.root.after(120, self._poll_queue)

    def _configure_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#f3f5f8")
        style.configure("TLabel", background="#f3f5f8", foreground="#17212f", font=("Malgun Gothic", 10))
        style.configure("Title.TLabel", font=("Malgun Gothic", 20, "bold"), foreground="#14233b")
        style.configure("Sub.TLabel", foreground="#536174", font=("Malgun Gothic", 9))
        style.configure("Section.TLabelframe", background="#ffffff")
        style.configure("Section.TLabelframe.Label", background="#ffffff", foreground="#14233b", font=("Malgun Gothic", 10, "bold"))
        style.configure("TButton", font=("Malgun Gothic", 9), padding=(10, 7))
        style.configure("Accent.TButton", font=("Malgun Gothic", 9, "bold"), foreground="#ffffff", background="#1d4ed8")
        style.map("Accent.TButton", background=[("active", "#1e40af"), ("disabled", "#9ca3af")])
        style.configure("Danger.TButton", foreground="#ffffff", background="#b42318")
        style.map("Danger.TButton", background=[("active", "#8f1d15")])

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 13))
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="사람의 말소리가 없는 구간을 0.01초 단위로 찾아보고, 확인한 구간만 직접 삭제합니다.", style="Sub.TLabel").pack(anchor="w", pady=(3, 0))

        source_box = ttk.LabelFrame(outer, text="1. 영상 입력", style="Section.TLabelframe", padding=12)
        source_box.pack(fill="x", pady=(0, 10))
        ttk.Label(source_box, text="유튜브 URL").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        url_entry = ttk.Entry(source_box, textvariable=self.url_var)
        url_entry.grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(source_box, text="또는 영상 파일").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(source_box, textvariable=self.file_var).grid(row=1, column=1, sticky="ew", pady=4)
        self.browse_button = ttk.Button(source_box, text="파일 선택", command=self.browse_file)
        self.browse_button.grid(row=1, column=2, padx=(8, 0), pady=4)
        source_box.columnconfigure(1, weight=1)
        self.analyze_button = ttk.Button(source_box, text="음성 없는 구간 분석", style="Accent.TButton", command=self.start_analysis)
        self.analyze_button.grid(row=2, column=1, sticky="e", pady=(8, 0))

        settings = ttk.LabelFrame(outer, text="2. 분석 설정", style="Section.TLabelframe", padding=12)
        settings.pack(fill="x", pady=(0, 10))
        ttk.Label(settings, text="삭제 후보 최소 길이(초)").grid(row=0, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.min_gap_var, width=8).grid(row=0, column=1, padx=(8, 18))
        ttk.Label(settings, text="음성 보호 여백(초, 양쪽)").grid(row=0, column=2, sticky="w")
        ttk.Entry(settings, textvariable=self.protect_var, width=8).grid(row=0, column=3, padx=(8, 18))
        ttk.Label(settings, text="VAD 민감도").grid(row=0, column=4, sticky="w")
        ttk.Combobox(settings, textvariable=self.vad_mode_var, values=["0", "1", "2", "3"], width=4, state="readonly").grid(row=0, column=5, padx=(8, 0))
        ttk.Label(settings, text="민감도 0: 음성 판정을 넓게 / 3: 엄격하게 판정. 음악·효과음은 음성으로 오인될 수 있습니다.", style="Sub.TLabel").grid(row=1, column=0, columnspan=6, sticky="w", pady=(8, 0))

        results_header = ttk.Frame(outer)
        results_header.pack(fill="x", pady=(0, 6))
        ttk.Label(results_header, text="3. 분석 결과", font=("Malgun Gothic", 12, "bold")).pack(side="left")
        ttk.Label(results_header, textvariable=self.summary_var, style="Sub.TLabel").pack(side="left", padx=(12, 0))

        self.results_scroll = ScrollableFrame(outer)
        self.results_scroll.pack(fill="both", expand=True)
        self.empty_label = ttk.Label(self.results_scroll.inner, text="분석 결과가 여기에 표시됩니다.", padding=20, style="Sub.TLabel")
        self.empty_label.pack(anchor="w")

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(10, 0))
        self.select_all_button = ttk.Button(actions, text="모든 구간 삭제 대상으로 선택", command=lambda: self.set_all_selected(True), state="disabled")
        self.select_all_button.pack(side="left")
        self.clear_all_button = ttk.Button(actions, text="전체 선택 해제", command=lambda: self.set_all_selected(False), state="disabled")
        self.clear_all_button.pack(side="left", padx=(6, 0))
        self.export_button = ttk.Button(actions, text="선택 구간 삭제 후 영상 저장", style="Danger.TButton", command=self.start_export, state="disabled")
        self.export_button.pack(side="right")

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(9, 0))
        self.progress = ttk.Progressbar(footer, mode="indeterminate", length=180)
        self.progress.pack(side="right")
        ttk.Label(footer, textvariable=self.status_var, style="Sub.TLabel").pack(side="left", fill="x", expand=True)

    def browse_file(self):
        path = filedialog.askopenfilename(
            title="분석할 영상 선택",
            filetypes=[("영상 파일", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.ts *.mts"), ("모든 파일", "*.*")],
        )
        if path:
            self.file_var.set(path)
            self.url_var.set("")

    def _set_busy(self, busy: bool, message: str):
        self.worker_busy = busy
        self.status_var.set(message)
        if busy:
            self.progress.start(10)
            for widget in (self.analyze_button, self.browse_button, self.export_button, self.select_all_button, self.clear_all_button):
                widget.configure(state="disabled")
        else:
            self.progress.stop()
            self.analyze_button.configure(state="normal")
            self.browse_button.configure(state="normal")
            enabled = bool(self.segments)
            self.select_all_button.configure(state="normal" if enabled else "disabled")
            self.clear_all_button.configure(state="normal" if enabled else "disabled")
            self.export_button.configure(state="normal" if enabled else "disabled")

    def _launch_worker(self, operation: str, worker: Callable[[], object]):
        self._set_busy(True, "작업을 시작합니다.")

        def run():
            try:
                result = worker()
                self.ui_queue.put((operation + "_done", result, None))
            except Exception as exc:
                self.ui_queue.put((operation + "_done", None, str(exc)))

        threading.Thread(target=run, daemon=True).start()

    def start_analysis(self):
        if self.worker_busy:
            return
        url = self.url_var.get().strip()
        local = self.file_var.get().strip()
        if local:
            source_kind, source_value = "file", local
            if not Path(local).is_file():
                messagebox.showerror(APP_NAME, "선택한 영상 파일을 찾을 수 없습니다.")
                return
        elif url:
            source_kind, source_value = "url", url
        else:
            messagebox.showinfo(APP_NAME, "유튜브 URL을 입력하거나 영상 파일을 선택하세요.")
            return
        try:
            min_gap = float(self.min_gap_var.get())
            protect = float(self.protect_var.get())
            mode = int(self.vad_mode_var.get())
            if not (0.0 <= min_gap <= 3600.0):
                raise ValueError
            if not (0.0 <= protect <= 10.0):
                raise ValueError
            if mode not in range(4):
                raise ValueError
        except ValueError:
            messagebox.showerror(APP_NAME, "분석 설정값을 확인하세요. 최소 길이는 0~3600초, 보호 여백은 0~10초여야 합니다.")
            return

        def worker():
            self.ui_queue.put(("status", "", "FFmpeg와 입력 영상을 확인하고 있습니다."))
            require_ffmpeg()
            if source_kind == "url":
                source = download_youtube(source_value, lambda msg: self.ui_queue.put(("status", "", msg)))
            else:
                source = Path(source_value)
            media = probe_media(source)
            if not media["has_audio"]:
                raise RuntimeError("이 영상에는 오디오 트랙이 없습니다.")
            self.ui_queue.put(("status", "", "오디오를 추출하고 10ms 프레임으로 분석하고 있습니다."))
            audio_temp = Path(tempfile.gettempdir()) / f"matteuneun_audio_{os.getpid()}_{threading.get_ident()}.wav"
            try:
                extract_audio_16k(source, audio_temp)
                found = detect_non_speech_segments(audio_temp, media["duration"], min_gap, protect, mode)
            finally:
                audio_temp.unlink(missing_ok=True)
            return {"source": str(source), "duration": media["duration"], "segments": found}

        self._launch_worker("analysis", worker)

    def _clear_results(self):
        for widget in self.results_scroll.inner.winfo_children():
            widget.destroy()
        self.row_widgets.clear()
        self.delete_vars.clear()

    def _show_results(self, payload: dict):
        self._clear_results()
        self.source_path = payload["source"]
        self.duration = payload["duration"]
        self.segments = payload["segments"]
        count = len(self.segments)
        self.summary_var.set(f"전체 {count}개 구간 · 영상 길이 {fmt_time(self.duration)}")
        if count == 0:
            ttk.Label(
                self.results_scroll.inner,
                text="설정한 기준을 만족하는 구간을 찾지 못했습니다. VAD 민감도 또는 최소 길이를 조정해 다시 분석해 보세요.",
                padding=20,
                wraplength=780,
            ).pack(anchor="w")
        else:
            for segment in self.segments:
                self._add_segment_row(segment)
        self._set_busy(False, "분석이 완료되었습니다. 삭제할 구간은 각 행에서 직접 선택하세요.")

    def _add_segment_row(self, segment: Segment):
        card = tk.Frame(self.results_scroll.inner, bg="#ffffff", highlightbackground="#d7dee8", highlightthickness=1, padx=12, pady=9)
        card.pack(fill="x", padx=4, pady=4)
        left = tk.Frame(card, bg="#ffffff")
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text=f"구간 {segment.index:03d}", bg="#ffffff", fg="#14233b", font=("Malgun Gothic", 10, "bold")).pack(anchor="w")
        tk.Label(
            left,
            text=f"시작 {fmt_time(segment.start)}    종료 {fmt_time(segment.end)}    지속 시간 {segment.duration:.2f}초",
            bg="#ffffff", fg="#46556a", font=("Malgun Gothic", 9),
        ).pack(anchor="w", pady=(3, 0))
        right = tk.Frame(card, bg="#ffffff")
        right.pack(side="right")
        ttk.Button(right, text="삭제 전후 비교", command=lambda s=segment: self.open_compare(s)).pack(side="left", padx=(0, 8))
        selected = tk.BooleanVar(value=False)
        self.delete_vars[segment.index] = selected
        ttk.Checkbutton(right, text="삭제 대상으로 선택", variable=selected, command=self._update_selection_count).pack(side="left")
        self.row_widgets.append(card)

    def _update_selection_count(self):
        selected = sum(1 for value in self.delete_vars.values() if value.get())
        self.status_var.set(f"삭제 대상으로 선택된 구간: {selected}개. 원본은 아직 변경되지 않았습니다.")

    def set_all_selected(self, state: bool):
        for value in self.delete_vars.values():
            value.set(state)
        self._update_selection_count()

    def open_compare(self, segment: Segment):
        if not self.source_path or self.worker_busy:
            return
        dialog = CompareDialog(self, segment)
        self.preview_dialogs.add(dialog)
        dialog.bind("<Destroy>", lambda _e, d=dialog: self.preview_dialogs.discard(d))

    def start_export(self):
        if self.worker_busy or not self.source_path:
            return
        selected_segments = [s for s in self.segments if self.delete_vars.get(s.index) and self.delete_vars[s.index].get()]
        if not selected_segments:
            messagebox.showinfo(APP_NAME, "삭제 대상으로 선택된 구간이 없습니다.")
            return
        output = filedialog.asksaveasfilename(
            title="편집 영상 저장",
            defaultextension=".mp4",
            initialfile=f"{Path(self.source_path).stem}_마뜨는구간삭제.mp4",
            filetypes=[("MP4 영상", "*.mp4")],
        )
        if not output:
            return
        if Path(output).resolve() == Path(self.source_path).resolve():
            messagebox.showerror(APP_NAME, "원본 파일에는 덮어쓸 수 없습니다. 다른 이름으로 저장하세요.")
            return

        def worker():
            media_duration = self.duration
            deletions = sorted((s.start, s.end) for s in selected_segments)
            merged: list[list[float]] = []
            for start, end in deletions:
                if merged and start <= merged[-1][1] + 0.001:
                    merged[-1][1] = max(merged[-1][1], end)
                else:
                    merged.append([start, end])
            keep_ranges: list[tuple[float, float]] = []
            cursor = 0.0
            for start, end in merged:
                if start - cursor >= 0.02:
                    keep_ranges.append((cursor, start))
                cursor = max(cursor, end)
            if media_duration - cursor >= 0.02:
                keep_ranges.append((cursor, media_duration))
            self.ui_queue.put(("status", "", f"선택한 {len(selected_segments)}개 구간을 제거해 영상으로 저장하고 있습니다."))
            return str(render_ranges(self.source_path, keep_ranges, output))

        self._launch_worker("export", worker)

    def _poll_queue(self):
        try:
            while True:
                event = self.ui_queue.get_nowait()
                kind = event[0]
                if kind == "status":
                    self.status_var.set(event[2])
                elif kind == "analysis_done":
                    payload, error = event[1], event[2]
                    if error:
                        self._set_busy(False, "분석에 실패했습니다.")
                        messagebox.showerror(APP_NAME, error)
                    else:
                        self._show_results(payload)
                elif kind == "export_done":
                    output, error = event[1], event[2]
                    self._set_busy(False, "영상 내보내기가 완료되었습니다." if not error else "영상 내보내기에 실패했습니다.")
                    if error:
                        messagebox.showerror(APP_NAME, error)
                    else:
                        messagebox.showinfo(APP_NAME, f"편집 영상 저장 완료\n\n{output}")
                        try:
                            open_file_in_player(Path(output))
                        except Exception:
                            pass
                elif kind == "compare_progress":
                    dialog, message = event[1], event[2]
                    try:
                        if dialog.winfo_exists():
                            dialog.set_status(message)
                    except tk.TclError:
                        pass
                elif kind == "compare_done":
                    dialog, error = event[1], event[2]
                    try:
                        if dialog.winfo_exists():
                            if error:
                                dialog.set_status("비교 영상 생성 실패: " + error)
                            else:
                                dialog.set_status("비교 영상 준비 완료. 분할 화면 또는 각 영상을 재생할 수 있습니다.", done=True)
                    except tk.TclError:
                        pass
        except queue.Empty:
            pass
        self.root.after(120, self._poll_queue)

    def close(self):
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.1)
    except tk.TclError:
        pass
    window = MainWindow(root)
    root.protocol("WM_DELETE_WINDOW", window.close)
    root.mainloop()


if __name__ == "__main__":
    main()
