"""Извлечение текста из вложений Telegram: фото, документы, голосовые."""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 18 * 1024 * 1024
MAX_IMAGE_SIDE = 1600
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".log", ".py", ".js", ".ts", ".html", ".css", ".xml", ".yaml", ".yml", ".sql", ".ini"}
MAX_TEXT_CHARS = 12_000


@dataclass
class Attachment:
    kind: str                  # photo | image | document | voice | audio | video | sticker | other
    description: str           # описание для LLM словами
    data_url: Optional[str] = None
    text: Optional[str] = None
    image: Optional[bytes] = None
    image_mime: str = "image/jpeg"
    width: int = 0
    height: int = 0
    file_name: str = ""
    file: Optional[bytes] = None    # сырые байты не-картинки (видео/голос/документ и т.п.)
    file_mime: str = ""
    file_ext: str = ""


def _shrink_image(raw: bytes) -> tuple[bytes, str, int, int]:
    """Уменьшает картинку до разумного размера; возвращает (байты, mime, ширина, высота)."""
    try:
        from PIL import Image
    except ImportError:
        return raw, "image/jpeg", 0, 0
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
        width, height = image.size
        if image.mode in {"RGBA", "LA", "P"}:
            image = image.convert("RGBA")
            background = Image.new("RGB", image.size, "white")
            background.paste(image, mask=image.split()[-1])
            image = background
        elif image.mode != "RGB":
            image = image.convert("RGB")
        scale = min(1.0, MAX_IMAGE_SIDE / max(width, height))
        if scale < 1.0:
            image = image.resize((max(1, int(width * scale)), max(1, int(height * scale))))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=88)
        return buffer.getvalue(), "image/jpeg", width, height
    except Exception as exc:  # noqa: BLE001
        log.debug("Не удалось сжать картинку: %s", exc)
        return raw, "image/jpeg", 0, 0


def _audio_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return ""
    return f" ({seconds} c)"


async def _download(event, label: str) -> Optional[bytes]:
    """Скачивает сырые байты вложения (для не-картинок). None — не удалось."""
    try:
        return await event.download_media(file=bytes)
    except Exception as exc:  # noqa: BLE001
        log.debug("Не удалось скачать %s: %s", label, exc)
        return None


async def extract(event) -> Optional[Attachment]:
    """Строит Attachment по сообщению Telethon или None, если вложения нет."""
    message = event.message
    if message is None:
        return None

    if message.photo:
        try:
            raw = await event.download_media(file=bytes)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось скачать фото: %s", exc)
            raw = None
        if raw:
            data, mime, width, height = _shrink_image(raw)
            return Attachment(
                kind="photo",
                description="пользователь прислал фотографию",
                data_url=f"data:{mime};base64,{base64.b64encode(data).decode()}",
                image=data,
                image_mime=mime,
                width=width,
                height=height,
                file_name=f"photo-{int(time.time())}.jpg",
            )
        return Attachment(kind="photo", description="пользователь прислал фотографию (не удалось скачать)")

    document = message.document
    if document:
        mime = (document.mime_type or "").lower()
        name = ""
        for attr in getattr(document, "attributes", []):
            name = getattr(attr, "file_name", None) or name
        suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        is_image = mime.startswith("image/") or suffix in IMAGE_EXTENSIONS
        is_text = mime.startswith("text/") or suffix in TEXT_EXTENSIONS

        if (document.size or 0) > MAX_DOWNLOAD_BYTES:
            return Attachment(kind="document", description=f"пользователь прислал файл {name or '(без имени)'} — слишком большой, чтобы посмотреть")
        try:
            raw = await event.download_media(file=bytes)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось скачать документ: %s", exc)
            raw = None

        if raw and is_image:
            data, real_mime, width, height = _shrink_image(raw)
            return Attachment(
                kind="image",
                description=f"пользователь прислал файл {name or '(без имени)'}",
                data_url=f"data:{real_mime};base64,{base64.b64encode(data).decode()}",
                image=data,
                image_mime=real_mime,
                width=width,
                height=height,
                file_name=name or f"image-{int(time.time())}.jpg",
            )
        if raw and is_text:
            try:
                decoded = raw.decode("utf-8", errors="replace")[:MAX_TEXT_CHARS]
            except Exception:  # noqa: BLE001
                decoded = ""
            return Attachment(
                kind="document",
                description=f"пользователь прислал файл {name or '(без имени)'}",
                text=f"Содержимое файла «{name or 'без имени'}»:\n{decoded}",
                file=raw,
                file_mime=mime or "application/octet-stream",
                file_ext=suffix,
            )
        return Attachment(kind="document", description=f"пользователь прислал файл {name or '(без имени)'} ({mime or 'неизвестный тип'})",
                          file=raw, file_mime=mime or "application/octet-stream", file_ext=suffix)

    if message.voice or (message.audio and not message.video):
        duration = getattr(message, "voice", None) and message.voice.duration or getattr(message.audio, "duration", None)
        raw = await _download(event, "голосовое")
        return Attachment(kind="voice", description=f"пользователь прислал голосовое сообщение{_audio_duration(duration)} — голос не распознаётся",
                          file=raw, file_mime="audio/ogg", file_ext=".ogg")
    if message.video:
        raw = await _download(event, "видео")
        return Attachment(kind="video", description="пользователь прислал видео — видео не просматривается",
                          file=raw, file_mime="video/mp4", file_ext=".mp4")
    if message.video_note:
        raw = await _download(event, "видеокружок")
        return Attachment(kind="video", description="пользователь прислал видеокружок — видео не просматривается",
                          file=raw, file_mime="video/mp4", file_ext=".mp4")
    if message.sticker:
        raw = await _download(event, "стикер")
        return Attachment(kind="sticker", description="пользователь прислал стикер",
                          file=raw, file_mime="image/webp", file_ext=".webp")
    if message.gif:
        raw = await _download(event, "GIF")
        return Attachment(kind="video", description="пользователь прислал GIF",
                          file=raw, file_mime="image/gif", file_ext=".gif")
    if message.contact:
        contact = message.contact
        return Attachment(kind="other", description=f"пользователь прислал контакт {contact.first_name or ''} {contact.phone_number or ''}".strip())
    if message.geo:
        geo = message.geo
        return Attachment(kind="other", description=f"пользователь отправил геолокацию {geo.lat}, {geo.long}")
    return None


def describe(attachment: Attachment) -> str:
    if attachment.text:
        return f"[{attachment.description}]\n{attachment.text}"
    return f"[{attachment.description}]"
