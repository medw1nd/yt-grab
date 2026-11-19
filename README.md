# YT / Twitch Grab

Локальний завантажувач: YouTube та Twitch → MP4 (1080p, до 60 fps) або MP3, з вибором відрізка.

## Запуск з вихідного коду
    pip install -r requirements.txt
    python app.py

## Структура
- `core.py` — завантаження (yt-dlp + ffmpeg), без GUI
- `app.py`  — інтерфейс (PySide6)

## Оновлення yt-dlp
YouTube часто змінюється; якщо перестало працювати: `pip install -U yt-dlp` і перезібрати.
