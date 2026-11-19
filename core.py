"""Логіка завантаження (без GUI): yt-dlp + ffmpeg."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin
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
        "concurrent_fragment_downloads": 16,
        "http_chunk_size": 10485760,
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
        is_live=bool(info.get("is_live")) and not dur,
    )


# 1080p, до 60 fps, перевага H.264 + AAC (відтворюється всюди, зокрема в QuickTime)
VIDEO_FORMAT = "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"
VIDEO_SORT = ["res:1080", "fps:60", "vcodec:h264", "acodec:aac", "ext:mp4:m4a"]


# ---------------------------------------------------------------- швидкий HLS
def _http_get(url: str, headers: dict, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _parse_media_playlist(text: str, base: str):
    """Повертає [(uri, duration)], map_uri або raise ValueError якщо формат складний."""
    segs, cur, map_uri = [], None, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-KEY") and "METHOD=NONE" not in line:
            raise ValueError("encrypted")
        if line.startswith("#EXT-X-BYTERANGE"):
            raise ValueError("byterange")
        if line.startswith("#EXT-X-MAP"):
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                map_uri = urljoin(base, m.group(1))
        elif line.startswith("#EXTINF:"):
            cur = float(line[8:].split(",")[0])
        elif line and not line.startswith("#") and cur is not None:
            segs.append((urljoin(base, line), cur))
            cur = None
    if not segs:
        raise ValueError("empty")
    return segs, map_uri


def _hls_range_download(
    info: dict, mode: str, outdir: str, start: int, end: Optional[int],
    suffix: str, on_progress, is_cancelled,
) -> str:
    """Паралельно качає лише потрібні сегменти HLS і склеює їх через ffmpeg."""
    fmt = (info.get("requested_formats") or [info])[0]
    headers = dict(fmt.get("http_headers") or info.get("http_headers") or {})
    url = fmt["url"]
    text = _http_get(url, headers).decode("utf-8", "replace")
    if "#EXT-X-STREAM-INF" in text:
        raise ValueError("master playlist")
    segs, map_uri = _parse_media_playlist(text, url)

    hi = float("inf") if end is None else float(end)
    picked, t, first_start = [], 0.0, None
    for uri, dur in segs:
        if t + dur > start and t < hi:
            if first_start is None:
                first_start = t
            picked.append(uri)
        t += dur
    if not picked:
        raise ValueError("range outside video")
    offset = start - (first_start or 0.0)
    length = (min(hi, t) - start)

    tmp = tempfile.mkdtemp(prefix="ytgrab_")
    try:
        files = []
        jobs = []
        if map_uri:
            jobs.append((0, map_uri))
        base_i = 1 if map_uri else 0
        for i, u in enumerate(picked):
            jobs.append((base_i + i, u))
        total = len(jobs)
        done = 0
        bytes_done = 0
        t0 = time.time()
        lock = threading.Lock()

        def fetch(idx_url):
            idx, u = idx_url
            if is_cancelled():
                raise Cancelled()
            last = None
            for _ in range(4):
                try:
                    data = _http_get(u, headers)
                    p = os.path.join(tmp, f"{idx:06d}.seg")
                    with open(p, "wb") as f:
                        f.write(data)
                    return idx, p, len(data)
                except Exception as e:  # noqa: BLE001
                    last = e
                    if is_cancelled():
                        raise Cancelled()
                    time.sleep(1)
            raise last  # type: ignore[misc]

        results = {}
        with ThreadPoolExecutor(max_workers=16) as ex:
            futs = [ex.submit(fetch, j) for j in jobs]
            try:
                for fu in as_completed(futs):
                    idx, p, n = fu.result()
                    results[idx] = p
                    with lock:
                        done += 1
                        bytes_done += n
                    sp = bytes_done / max(time.time() - t0, 0.001)
                    eta = (total - done) / done * (time.time() - t0)
                    on_progress(
                        done / total * 90,
                        f"Завантаження… {sp / 1048576:.1f} МБ/с · залишилось {fmt_time(eta)}",
                    )
            except BaseException:
                for f in futs:
                    f.cancel()
                raise
        files = [results[i] for i in sorted(results)]

        on_progress(None, "Склеювання / конвертація…")
        listfile = os.path.join(tmp, "list.txt")
        with open(listfile, "w") as f:
            for p in files:
                f.write("file '" + p.replace("'", "'\\''") + "'\n")
        title = yt_dlp.utils.sanitize_filename(info.get("title") or "video")[:120]
        ext = "mp3" if mode == "mp3" else "mp4"
        out = Path(outdir) / f"{title}{suffix}.{ext}"
        n = 1
        while out.exists():
            out = Path(outdir) / f"{title}{suffix} ({n}).{ext}"
            n += 1
        cmd = [find_ffmpeg() or "ffmpeg", "-y", "-loglevel", "error",
               "-f", "concat", "-safe", "0", "-i", listfile,
               "-ss", f"{offset:.3f}", "-t", f"{length:.3f}"]
        if mode == "mp3":
            cmd += ["-vn", "-c:a", "libmp3lame", "-b:a", "192k"]
        else:
            cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart"]
        cmd.append(str(out))
        flags = 0x08000000 if os.name == "nt" else 0
        r = subprocess.run(cmd, capture_output=True, text=True, creationflags=flags)
        if r.returncode != 0:
            raise RuntimeError("ffmpeg: " + r.stderr.strip()[-300:])
        on_progress(100.0, "Готово")
        return str(out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _try_fast_hls(url, mode, outdir, start, end, on_progress, is_cancelled) -> Optional[str]:
    """Повертає шлях до файлу або None, якщо швидкий шлях не підходить."""
    opts = build_opts(mode, outdir, None, None)
    opts["skip_download"] = True
    opts.pop("postprocessors", None)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if info.get("_type") == "playlist":
            info = (info.get("entries") or [info])[0]
        fmts = info.get("requested_formats") or [info]
        if len(fmts) != 1 or fmts[0].get("protocol") not in ("m3u8", "m3u8_native"):
            return None
        s = start or 0
        suffix = _suffix(s, end)
        return _hls_range_download(info, mode, outdir, s, end, suffix, on_progress, is_cancelled)
    except Cancelled:
        raise
    except Exception:  # noqa: BLE001
        if is_cancelled():
            raise Cancelled()
        return None  # впадемо на звичайний шлях


def _suffix(s: int, end: Optional[int]) -> str:
    out = f" [{fmt_time(s).replace(':', '-')}"
    out += f"-{fmt_time(end).replace(':', '-')}]" if end is not None else "-]"
    return out


def build_opts(mode: str, outdir: str, start: Optional[int], end: Optional[int]) -> dict:
    opts = _base_opts()
    suffix = ""
    if start is not None or end is not None:
        s = start or 0
        suffix = _suffix(s, end)
        opts["download_ranges"] = download_range_func(
            None, [(float(s), float(end) if end is not None else float("inf"))]
        )
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
    if start is not None or end is not None:
        on_progress(None, "Готую завантаження…")
        fast_path = _try_fast_hls(url, mode, outdir, start, end, on_progress, is_cancelled)
        if fast_path:
            return fast_path
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
