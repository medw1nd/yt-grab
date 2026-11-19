"""Логіка завантаження (без GUI): yt-dlp + ffmpeg."""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import yt_dlp
from yt_dlp.utils import download_range_func


class Cancelled(Exception):
    pass


def resource_dir() -> Path:
    """Папка з вбудованими файлами (PyInstaller) або папка проєкту."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else Path(__file__).resolve().parent


def _exe(name: str) -> str:
    return name + (".exe" if os.name == "nt" else "")


def find_ffmpeg() -> Optional[str]:
    """Повертає шлях до ffmpeg: вбудований -> imageio-ffmpeg -> PATH."""
    bundled = resource_dir() / "bin" / _exe("ffmpeg")
    if bundled.exists():
        return str(bundled)
    try:
        import imageio_ffmpeg

        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and Path(p).exists():
            return p
    except Exception:
        pass
    return shutil.which("ffmpeg")


def find_deno() -> Optional[str]:
    """JS-рантайм потрібен yt-dlp для YouTube (вбудований deno або з PATH)."""
    bundled = resource_dir() / "bin" / _exe("deno")
    if bundled.exists():
        return str(bundled)
    return shutil.which("deno") or shutil.which("node")


def default_download_dir() -> str:
    d = Path.home() / "Downloads"
    return str(d if d.exists() else Path.home())


def fmt_time(sec: float) -> str:
    sec = max(0, int(round(sec)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_time(text: str) -> Optional[int]:
    """'1:02:03', '2:03', '95' -> секунди. None якщо невалідно."""
    text = text.strip()
    if not text:
        return None
    try:
        parts = [int(p) for p in text.split(":")]
    except ValueError:
        return None
    if not 1 <= len(parts) <= 3 or any(p < 0 for p in parts):
        return None
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec


@dataclass
class VideoInfo:
    url: str
    title: str
    duration: Optional[int]  # None для прямих трансляцій
    thumbnail: Optional[str]
    uploader: str
    is_live: bool


def _base_opts() -> dict:
    opts: dict = {
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "noplaylist": True,
        "windowsfilenames": True,
        "socket_timeout": 20,
        "retries": 5,
        "fragment_retries": 5,
    }
    ff = find_ffmpeg()
    if ff:
        opts["ffmpeg_location"] = ff
    js = find_deno()
    if js:
        name = "deno" if "deno" in Path(js).name.lower() else "node"
        opts["js_runtimes"] = {name: {"path": js}}
    return opts


def fetch_info(url: str) -> VideoInfo:
    opts = _base_opts()
    opts["skip_download"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise ValueError("Порожній плейліст")
        info = entries[0]
    thumb = info.get("thumbnail")
    if not thumb and info.get("thumbnails"):
        thumb = info["thumbnails"][-1].get("url")
    dur = info.get("duration")
    return VideoInfo(
        url=info.get("webpage_url") or url,
        title=info.get("title") or "Без назви",
        duration=int(dur) if dur else None,
        thumbnail=thumb,
        uploader=info.get("uploader") or info.get("channel") or "",
        is_live=bool(info.get("is_live")),
    )


# 1080p, до 60 fps, перевага H.264 + AAC (відтворюється всюди, зокрема в QuickTime)
VIDEO_FORMAT = "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"
VIDEO_SORT = ["res:1080", "fps:60", "vcodec:h264", "acodec:aac", "ext:mp4:m4a"]


def build_opts(mode: str, outdir: str, start: Optional[int], end: Optional[int]) -> dict:
    opts = _base_opts()
    suffix = ""
    if start is not None or end is not None:
        s = start or 0
        suffix = f" [{fmt_time(s).replace(':', '-')}"
        suffix += f"-{fmt_time(end).replace(':', '-')}]" if end is not None else "-]"
        opts["download_ranges"] = download_range_func(
            None, [(float(s), float(end) if end is not None else float("inf"))]
        )
        opts["force_keyframes_at_cuts"] = True
    opts["outtmpl"] = str(Path(outdir) / f"%(title).120B{suffix}.%(ext)s")
    if mode == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
    else:
        opts["format"] = VIDEO_FORMAT
        opts["format_sort"] = VIDEO_SORT
        opts["merge_output_format"] = "mp4"
    return opts


def download(
    url: str,
    mode: str,
    outdir: str,
    start: Optional[int],
    end: Optional[int],
    on_progress: Callable[[Optional[float], str], None],
    is_cancelled: Callable[[], bool],
) -> str:
    """mode: 'mp4' | 'mp3'. start/end — секунди (None = без обмеження)."""
    if not find_ffmpeg():
        raise RuntimeError("ffmpeg не знайдено")
    os.makedirs(outdir, exist_ok=True)
    opts = build_opts(mode, outdir, start, end)

    def hook(d: dict) -> None:
        if is_cancelled():
            raise Cancelled()
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            pct = (done / total * 100) if total else None
            parts = []
            if d.get("speed"):
                parts.append(f"{d['speed'] / 1048576:.1f} МБ/с")
            if d.get("eta") is not None:
                parts.append(f"залишилось {fmt_time(d['eta'])}")
            on_progress(pct, "Завантаження… " + " · ".join(parts))
        elif d["status"] == "finished":
            on_progress(100.0, "Обробка…")

    def pp_hook(d: dict) -> None:
        if d["status"] == "started":
            on_progress(None, "Склеювання / конвертація…")

    opts["progress_hooks"] = [hook]
    opts["postprocessor_hooks"] = [pp_hook]
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Cancelled:
        raise
    except Exception:
        if is_cancelled():
            raise Cancelled()
        raise
    if info.get("_type") == "playlist":
        info = (info.get("entries") or [info])[0]
    req = info.get("requested_downloads") or []
    if req and req[0].get("filepath"):
        return req[0]["filepath"]
    return outdir
