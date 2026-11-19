"""YT/Twitch Grab — простий локальний завантажувач (macOS / Windows)."""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import core

ACCENT = "#7c5cff"
STYLE = f"""
QWidget {{ background:#16171d; color:#e8e9ee; font-size:14px; }}
QLabel {{ background:transparent; }}
QLineEdit {{ background:#22242d; border:1px solid #333646; border-radius:8px; padding:8px 10px; }}
QLineEdit:focus {{ border:1px solid {ACCENT}; }}
QPushButton {{ background:#2a2d3a; border:none; border-radius:8px; padding:9px 16px; }}
QPushButton:hover {{ background:#343848; }}
QPushButton:disabled {{ color:#6b6e7c; background:#22242d; }}
QPushButton#primary {{ background:{ACCENT}; color:white; font-weight:600; padding:12px; }}
QPushButton#primary:hover {{ background:#8f73ff; }}
QPushButton#primary:disabled {{ background:#3a3560; color:#8d89a8; }}
QPushButton#mode {{ background:#22242d; border:1px solid #333646; padding:11px; }}
QPushButton#mode:checked {{ background:{ACCENT}; border:1px solid {ACCENT}; color:white; font-weight:600; }}
QProgressBar {{ background:#22242d; border:none; border-radius:6px; height:10px; text-align:center; }}
QProgressBar::chunk {{ background:{ACCENT}; border-radius:6px; }}
QFrame#card {{ background:#1d1f27; border-radius:12px; }}
QLabel#muted {{ color:#8b8e9e; }}
"""


class RangeSlider(QWidget):
    """Тайм-лайн з двома ручками: початок і кінець відрізка (секунди)."""

    changed = Signal(int, int)

    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(44)
        self.setMinimumWidth(300)
        self._max = 1
        self._lo = 0
        self._hi = 1
        self._drag: Optional[str] = None
        self.setEnabled(False)

    # --- публічне API
    def set_range(self, maximum: int) -> None:
        self._max = max(1, maximum)
        self._lo, self._hi = 0, self._max
        self.update()
        self.changed.emit(self._lo, self._hi)

    def values(self) -> tuple[int, int]:
        return self._lo, self._hi

    def set_values(self, lo: int, hi: int) -> None:
        lo = max(0, min(lo, self._max - 1))
        hi = max(lo + 1, min(hi, self._max))
        self._lo, self._hi = lo, hi
        self.update()

    # --- геометрія
    PAD = 14

    def _x(self, v: int) -> float:
        w = self.width() - 2 * self.PAD
        return self.PAD + w * v / self._max

    def _val(self, x: float) -> int:
        w = self.width() - 2 * self.PAD
        return int(round(min(max((x - self.PAD) / w, 0), 1) * self._max))

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cy = self.height() / 2
        on = self.isEnabled()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#2a2d3a"))
        p.drawRoundedRect(QRectF(self.PAD, cy - 4, self.width() - 2 * self.PAD, 8), 4, 4)
        x1, x2 = self._x(self._lo), self._x(self._hi)
        p.setBrush(QColor(ACCENT if on else "#3a3560"))
        p.drawRect(QRectF(x1, cy - 4, x2 - x1, 8))
        for x in (x1, x2):
            p.setBrush(QColor("#ffffff" if on else "#8d89a8"))
            p.setPen(QPen(QColor(ACCENT), 2))
            p.drawEllipse(QPointF(x, cy), 9, 9)

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if not self.isEnabled():
            return
        x = e.position().x()
        d1, d2 = abs(x - self._x(self._lo)), abs(x - self._x(self._hi))
        self._drag = "lo" if d1 <= d2 else "hi"
        self.mouseMoveEvent(e)

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if not self._drag:
            return
        v = self._val(e.position().x())
        if self._drag == "lo":
            self._lo = max(0, min(v, self._hi - 1))
        else:
            self._hi = min(self._max, max(v, self._lo + 1))
        self.update()
        self.changed.emit(self._lo, self._hi)

    def mouseReleaseEvent(self, _e) -> None:  # noqa: N802
        self._drag = None


class InfoWorker(QThread):
    done = Signal(object, object)  # VideoInfo|None, thumb bytes|None
    failed = Signal(str)

    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url

    def run(self) -> None:
        try:
            info = core.fetch_info(self.url)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(_clean_err(e))
            return
        thumb = None
        if info.thumbnail:
            try:
                with urllib.request.urlopen(info.thumbnail, timeout=10) as r:
                    thumb = r.read()
            except Exception:  # noqa: BLE001
                pass
        self.done.emit(info, thumb)


class DownloadWorker(QThread):
    progress = Signal(object, str)  # percent|None, text
    finished_ok = Signal(str)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, url, mode, outdir, start, end) -> None:
        super().__init__()
        self.args = (url, mode, outdir, start, end)
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            path = core.download(
                *self.args,
                on_progress=lambda p, t: self.progress.emit(p, t),
                is_cancelled=lambda: self._cancel,
            )
        except core.Cancelled:
            self.cancelled.emit()
        except Exception as e:  # noqa: BLE001
            self.failed.emit(_clean_err(e))
        else:
            self.finished_ok.emit(path)


def _clean_err(e: Exception) -> str:
    msg = str(e)
    for junk in ("\x1b[0;31mERROR:\x1b[0m ", "ERROR: "):
        msg = msg.replace(junk, "")
    return msg.strip()[:400]


class MainWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("YT / Twitch Grab")
        self.setMinimumWidth(560)
        self.info: Optional[core.VideoInfo] = None
        self.outdir = core.default_download_dir()
        self.info_worker: Optional[InfoWorker] = None
        self.dl_worker: Optional[DownloadWorker] = None
        self._syncing = False
        self._build()

    # ---------------------------------------------------------------- UI
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(14)

        title = QLabel("YT / Twitch Grab")
        title.setFont(QFont(self.font().family(), 20, QFont.Bold))
        root.addWidget(title)

        row = QHBoxLayout()
        self.url = QLineEdit()
        self.url.setPlaceholderText("Встав посилання на YouTube або Twitch…")
        self.url.returnPressed.connect(self.fetch)
        self.url.textChanged.connect(self._url_changed)
        paste = QPushButton("Вставити")
        paste.clicked.connect(self.paste)
        self.fetch_btn = QPushButton("Знайти")
        self.fetch_btn.clicked.connect(self.fetch)
        row.addWidget(self.url, 1)
        row.addWidget(paste)
        row.addWidget(self.fetch_btn)
        root.addLayout(row)

        # картка відео
        self.card = QFrame()
        self.card.setObjectName("card")
        cl = QHBoxLayout(self.card)
        cl.setContentsMargins(12, 12, 12, 12)
        self.thumb = QLabel()
        self.thumb.setFixedSize(160, 90)
        self.thumb.setAlignment(Qt.AlignCenter)
        self.thumb.setStyleSheet("background:#0f1015;border-radius:8px;")
        meta = QVBoxLayout()
        self.v_title = QLabel("")
        self.v_title.setWordWrap(True)
        self.v_title.setFont(QFont(self.font().family(), 13, QFont.DemiBold))
        self.v_sub = QLabel("")
        self.v_sub.setObjectName("muted")
        meta.addWidget(self.v_title)
        meta.addWidget(self.v_sub)
        meta.addStretch(1)
        cl.addWidget(self.thumb)
        cl.addLayout(meta, 1)
        self.card.hide()
        root.addWidget(self.card)

        # формат
        mrow = QHBoxLayout()
        self.btn_mp4 = QPushButton("🎬  Відео MP4 · 1080p 60fps")
        self.btn_mp3 = QPushButton("🎵  Аудіо MP3")
        grp = QButtonGroup(self)
        grp.setExclusive(True)
        for b in (self.btn_mp4, self.btn_mp3):
            b.setObjectName("mode")
            b.setCheckable(True)
            grp.addButton(b)
            mrow.addWidget(b, 1)
        self.btn_mp4.setChecked(True)
        root.addLayout(mrow)

        # тайм-лайн
        self.tl_label = QLabel("Відрізок для завантаження")
        self.tl_label.setObjectName("muted")
        root.addWidget(self.tl_label)
        self.slider = RangeSlider()
        self.slider.changed.connect(self._slider_changed)
        root.addWidget(self.slider)
        trow = QHBoxLayout()
        self.t_start = QLineEdit("0:00")
        self.t_end = QLineEdit("0:00")
        for e in (self.t_start, self.t_end):
            e.setFixedWidth(92)
            e.setAlignment(Qt.AlignCenter)
            e.setEnabled(False)
            e.editingFinished.connect(self._fields_changed)
        self.t_len = QLabel("")
        self.t_len.setObjectName("muted")
        self.t_len.setMinimumWidth(70)
        self.reset_btn = QPushButton("Усе відео")
        self.reset_btn.setEnabled(False)
        self.reset_btn.clicked.connect(self._reset_range)
        trow.addWidget(QLabel("Від"))
        trow.addWidget(self.t_start)
        trow.addWidget(QLabel("до"))
        trow.addWidget(self.t_end)
        trow.addWidget(self.t_len)
        trow.addStretch(1)
        trow.addWidget(self.reset_btn)
        root.addLayout(trow)

        # папка
        frow = QHBoxLayout()
        self.folder = QLabel()
        self.folder.setObjectName("muted")
        self._show_folder()
        pick = QPushButton("Папка…")
        pick.clicked.connect(self.pick_folder)
        frow.addWidget(self.folder, 1)
        frow.addWidget(pick)
        root.addLayout(frow)

        # кнопки/прогрес
        self.go = QPushButton("Завантажити")
        self.go.setObjectName("primary")
        self.go.setEnabled(False)
        self.go.clicked.connect(self.start_download)
        self.cancel_btn = QPushButton("Скасувати")
        self.cancel_btn.hide()
        self.cancel_btn.clicked.connect(self.cancel_download)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.hide()
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        self.open_btn = QPushButton("Показати файл")
        self.open_btn.hide()
        self.open_btn.clicked.connect(self.reveal)
        self._last_file = ""
        root.addWidget(self.go)
        root.addWidget(self.bar)
        srow = QHBoxLayout()
        srow.addWidget(self.status, 1)
        srow.addWidget(self.cancel_btn)
        srow.addWidget(self.open_btn)
        root.addLayout(srow)

        if not core.find_ffmpeg():
            self.status.setText("⚠ ffmpeg не знайдено — завантаження не працюватиме.")

    # ------------------------------------------------------ допоміжні
    def _show_folder(self) -> None:
        self.folder.setText(f"Зберігати в: {self.outdir}")

    def pick_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Папка для завантажень", self.outdir)
        if d:
            self.outdir = d
            self._show_folder()

    def paste(self) -> None:
        self.url.setText(QApplication.clipboard().text().strip())
        self.fetch()

    def _url_changed(self) -> None:
        # нове посилання — старі дані недійсні
        if self.info and self.url.text().strip() != self.info.url:
            self._clear_info()

    def _clear_info(self) -> None:
        self.info = None
        self.card.hide()
        self.slider.setEnabled(False)
        for w in (self.t_start, self.t_end, self.reset_btn):
            w.setEnabled(False)
        self.go.setEnabled(False)
        self.adjustSize()

    # ------------------------------------------------------- пошук
    def fetch(self) -> None:
        url = self.url.text().strip()
        if not url or (self.info_worker and self.info_worker.isRunning()):
            return
        self._clear_info()
        self.status.setText("Отримую інформацію про відео…")
        self.fetch_btn.setEnabled(False)
        self.info_worker = InfoWorker(url)
        self.info_worker.done.connect(self._info_ok)
        self.info_worker.failed.connect(self._info_fail)
        self.info_worker.start()

    def _info_fail(self, msg: str) -> None:
        self.fetch_btn.setEnabled(True)
        self.status.setText("⚠ " + msg)

    def _info_ok(self, info: core.VideoInfo, thumb: Optional[bytes]) -> None:
        self.fetch_btn.setEnabled(True)
        self.info = info
        self._requested_url = self.url.text().strip()
        info.url = self._requested_url  # щоб textChanged не скидав картку
        self.v_title.setText(info.title)
        dur = core.fmt_time(info.duration) if info.duration else "пряма трансляція"
        self.v_sub.setText(f"{info.uploader}\nТривалість: {dur}")
        pm = QPixmap()
        if thumb and pm.loadFromData(thumb):
            self.thumb.setPixmap(
                pm.scaled(160, 90, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            )
        else:
            self.thumb.setText("—")
        self.card.show()
        self.status.setText("")
        self.go.setEnabled(True)
        if info.duration and not info.is_live and info.duration > 1:
            self.slider.setEnabled(True)
            self.slider.set_range(info.duration)
            for w in (self.t_start, self.t_end, self.reset_btn):
                w.setEnabled(True)
            self.tl_label.setText("Відрізок для завантаження")
        else:
            self.tl_label.setText("Відрізок недоступний для прямих трансляцій")
            self.t_len.setText("")

    # ------------------------------------------------------ тайм-лайн
    def _slider_changed(self, lo: int, hi: int) -> None:
        self._syncing = True
        self.t_start.setText(core.fmt_time(lo))
        self.t_end.setText(core.fmt_time(hi))
        self._syncing = False
        self.t_len.setText(f"({core.fmt_time(hi - lo)})")

    def _fields_changed(self) -> None:
        if self._syncing or not self.info or not self.info.duration:
            return
        lo = core.parse_time(self.t_start.text())
        hi = core.parse_time(self.t_end.text())
        cur_lo, cur_hi = self.slider.values()
        lo = cur_lo if lo is None else lo
        hi = cur_hi if hi is None else hi
        self.slider.set_values(lo, hi)
        self._slider_changed(*self.slider.values())

    def _reset_range(self) -> None:
        if self.info and self.info.duration:
            self.slider.set_values(0, self.info.duration)
            self._slider_changed(*self.slider.values())

    def _selected_range(self) -> tuple[Optional[int], Optional[int]]:
        if not (self.info and self.info.duration and self.slider.isEnabled()):
            return None, None
        lo, hi = self.slider.values()
        full = lo == 0 and hi >= self.info.duration
        return (None, None) if full else (lo, hi)

    # ------------------------------------------------- завантаження
    def start_download(self) -> None:
        if not self.info:
            return
        mode = "mp3" if self.btn_mp3.isChecked() else "mp4"
        start, end = self._selected_range()
        self.go.setEnabled(False)
        self.open_btn.hide()
        self.bar.setRange(0, 0)
        self.bar.show()
        self.cancel_btn.show()
        self.status.setText("Починаю…")
        self.dl_worker = DownloadWorker(self.info.url, mode, self.outdir, start, end)
        self.dl_worker.progress.connect(self._progress)
        self.dl_worker.finished_ok.connect(self._dl_ok)
        self.dl_worker.failed.connect(self._dl_fail)
        self.dl_worker.cancelled.connect(self._dl_cancelled)
        self.dl_worker.start()

    def _progress(self, pct: Optional[float], text: str) -> None:
        if pct is None:
            self.bar.setRange(0, 0)
        else:
            self.bar.setRange(0, 100)
            self.bar.setValue(int(pct))
            text = f"{int(pct)}% · {text}"
        self.status.setText(text)

    def _end_download(self) -> None:
        self.bar.hide()
        self.cancel_btn.hide()
        self.go.setEnabled(self.info is not None)

    def _dl_ok(self, path: str) -> None:
        self._end_download()
        self._last_file = path
        self.status.setText(f"✅ Готово: {Path(path).name}")
        self.open_btn.show()

    def _dl_fail(self, msg: str) -> None:
        self._end_download()
        self.status.setText("⚠ " + msg)

    def _dl_cancelled(self) -> None:
        self._end_download()
        self.status.setText("Скасовано.")

    def cancel_download(self) -> None:
        if self.dl_worker:
            self.dl_worker.cancel()
            self.status.setText("Скасовую…")

    def reveal(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        p = Path(self._last_file)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.parent if p.exists() else self.outdir)))

    def closeEvent(self, e) -> None:  # noqa: N802
        if self.dl_worker and self.dl_worker.isRunning():
            self.dl_worker.cancel()
            self.dl_worker.wait(3000)
        e.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
