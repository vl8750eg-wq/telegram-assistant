"""Обработчики Telegram: приём сообщений, дебаунс, ответы, команды владельца."""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from collections import deque
from typing import Deque, Dict, List, Optional

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import SetTypingRequest
from telethon.tl.types import SendMessageTypingAction, User

from config import Config
from llm import LLM
from sheets import SheetsSink
from store import Store
from supabase_sink import SupabaseSink
import attachments

log = logging.getLogger(__name__)

DISCLOSURE = "\n\n— отвечает ИИ-ассистент"

HELP_TEXT = """Команды (пишите в «Избранное»):
/status — состояние и статистика
/pause — пауза для всех (ассистент молчит)
/resume — снова отвечать
/reset — очистить память всех диалогов
/prompt <текст> — временно заменить системный промпт
/prompt reset — вернуть промпт из .env
/block <id|@username> — не отвечать этому человеку
/unblock <id|@username> — снять блок
/takeover <минуты> — сколько молчать после вашего ручного ответа"""


class Assistant:
    def __init__(self, client: TelegramClient, cfg: Config, store: Store, llm: LLM,
                 sink: Optional["SheetsSink"] = None,
                 db_sink: Optional["SupabaseSink"] = None) -> None:
        self.client = client
        self.cfg = cfg
        self.store = store
        self.llm = llm
        self.sink = sink
        self.db_sink = db_sink
        self.me: Optional[User] = None

        self._buffers: Dict[int, List[str]] = {}
        self._timers: Dict[int, asyncio.Task] = {}
        self._locks: Dict[int, asyncio.Lock] = {}
        self._sent_ids: Deque[int] = deque(maxlen=500)
        self._recent_sends: Deque[tuple] = deque(maxlen=200)
        self._tasks: set[asyncio.Task] = set()
        # не качать медиа из активных пабликов десятками одновременно
        self._media_semaphore = asyncio.Semaphore(3)

    # ------------------------------------------------------------------ setup

    async def start(self) -> None:
        self.me = await self.client.get_me()
        log.info("Аккаунт: %s (@%s) id=%s", self.me.first_name, self.me.username, self.me.id)
        self.client.add_event_handler(self._on_incoming, events.NewMessage(incoming=True))
        self.client.add_event_handler(self._on_outgoing, events.NewMessage(outgoing=True))
        if self.sink and self.sink.enabled:
            ok = await asyncio.to_thread(self.sink.ensure)
            log.info("Таблица %s: %s", self.sink.workbook, "готова" if ok else "НЕДОСТУПНА")
        elif self.cfg.sheet_url:
            log.warning("Таблица включена, но SHEET_WORKBOOK пуст — записи отключены")
        if self.db_sink and self.db_sink.enabled:
            ok_db = await asyncio.to_thread(self.db_sink.ensure)
            log.info("Supabase %s: %s", self.db_sink.table, "готова" if ok_db else "НЕДОСТУПНА")

    # -------------------------------------------------------------- таблица

    def _sheet_log(self, channel: str, sender: str, text: str, reply: str = "",
                   username: str = "", chat_id: int | str = "",
                   chat_title: str = "", chat_username: str = "",
                   sender_id: int | str | None = None, direction: str = "",
                   media_url: str = "", media_type: str = "") -> None:
        if not self.sink or not self.sink.enabled:
            return
        values = self.sink.build_row(
            channel, sender, text, reply, username, chat_id,
            chat_title=chat_title, chat_username=chat_username,
            sender_id=sender_id, direction=direction,
            media_url=media_url, media_type=media_type,
        )
        log.debug("Строка для таблицы: %s", values)
        asyncio.create_task(self._sheet_append(values))

    async def _sheet_append(self, values: List) -> None:
        try:
            ok = await asyncio.to_thread(self.sink.append, values)  # type: ignore[union-attr]
            if not ok:
                log.warning("Строка не записана в таблицу")
        except Exception as exc:  # noqa: BLE001
            log.error("Ошибка записи в таблицу: %s", exc)

    # --------------------------------------------------------- база Supabase

    def _db_log(self, channel: str, sender: str, text: str, reply: str = "",
                username: str = "", chat_id: int | str = "", media_url: str = "",
                media_type: str = "", chat_title: str = "", chat_username: str = "",
                sender_id: int | str | None = None, direction: str = "") -> None:
        """Реально-временная запись сообщения в Supabase (неблокирующе)."""
        if not self.db_sink or not self.db_sink.enabled:
            return
        asyncio.create_task(
            self._db_append(
                channel, sender, text, reply, username, chat_id, media_url, media_type,
                chat_title=chat_title, chat_username=chat_username,
                sender_id=sender_id, direction=direction,
            )
        )

    async def _db_append(self, channel: str, sender: str, text: str, reply: str,
                         username: str, chat_id: int | str, media_url: str,
                         media_type: str, chat_title: str = "",
                         chat_username: str = "", sender_id: int | str | None = None,
                         direction: str = "") -> None:
        try:
            ok = await asyncio.to_thread(
                self.db_sink.append_message,
                channel, sender, text, reply, username, chat_id, media_url, media_type,
                chat_title=chat_title, chat_username=chat_username,
                sender_id=sender_id, direction=direction,
            )
            if not ok:
                log.warning("Сообщение не записано в Supabase")
        except Exception as exc:  # noqa: BLE001
            log.error("Ошибка записи в Supabase: %s", exc)

    _MIME_EXT = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
        "image/webp": ".webp", "image/bmp": ".bmp", "video/mp4": ".mp4",
        "video/quicktime": ".mov", "audio/ogg": ".ogg", "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a", "audio/wav": ".wav", "application/pdf": ".pdf",
        "application/zip": ".zip", "application/json": ".json",
        "text/plain": ".txt", "text/csv": ".csv", "text/html": ".html",
        "text/markdown": ".md",
    }

    def _db_media(self, attachment: Optional["attachments.Attachment"], caption: str = "",
                  sender: str = "", chat_id: int | str = "") -> None:
        """Сохраняет медиа (картинку или файл) в Supabase Storage и пишет строку с URL."""
        if attachment is None or not self.db_sink or not self.db_sink.enabled:
            return
        if not attachment.image and not attachment.file:
            return
        task = asyncio.create_task(self._db_media_task(attachment, caption, sender, chat_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _upload_attachment(self, attachment: "attachments.Attachment") -> str:
        """Загружает вложение в Supabase Storage, возвращает публичный URL."""
        if not self.db_sink or not self.db_sink.enabled:
            return ""
        if attachment.image:
            mime = attachment.image_mime or "image/jpeg"
            ext = self._MIME_EXT.get(mime, ".bin")
            payload, file_mime = attachment.image, mime
            timeout = 60
        elif attachment.file:
            mime = attachment.file_mime or "application/octet-stream"
            ext = attachment.file_ext or self._MIME_EXT.get(mime, ".bin")
            payload, file_mime = attachment.file, mime
            timeout = 120
        else:
            return ""
        filename = f"tg_{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}"
        return await asyncio.wait_for(
            asyncio.to_thread(self.db_sink.upload_media, payload, filename, file_mime, "media_upload"),
            timeout=timeout,
        )

    async def _db_media_task(self, attachment: "attachments.Attachment", caption: str,
                             sender: str, chat_id: int | str) -> None:
        try:
            media_type = attachment.kind or "media"
            url = await self._upload_attachment(attachment)
            if url:
                await self._db_append("Медиа", sender, caption, "[медиа сохранено]", "", chat_id, url, media_type)
                log.info("Медиа %s сохранено в Supabase: %s", media_type, url)
            else:
                log.warning("Медиа %s не загружено в Supabase Storage", media_type)
        except Exception as exc:  # noqa: BLE001
            log.error("Медиа не сохранено в Supabase: %s", exc)

    # --------------------------------------------------------------- filters

    def _norm(self, value: str) -> str:
        return value.strip().lstrip("@").lower()

    def _peer_keys(self, peer_id: int, username: Optional[str]) -> set[str]:
        keys = {str(peer_id)}
        if username:
            keys.add(self._norm(username))
        return keys

    def _blocked(self, peer_id: int, username: Optional[str]) -> bool:
        blocked = {self._norm(x) for x in self.cfg.blacklist}
        blocked |= {self._norm(x) for x in self.store.get("blacklist", "").split(",") if x.strip()}
        return bool(self._peer_keys(peer_id, username) & blocked)

    def _whitelisted(self, peer_id: int, username: Optional[str]) -> bool:
        allowed = {self._norm(x) for x in self.cfg.whitelist}
        if not allowed:
            return True
        return bool(self._peer_keys(peer_id, username) & allowed)

    def _paused(self, peer_id: int) -> bool:
        return self.store.get("global_pause") == "1" or self.store.is_paused(peer_id)

    # ------------------------------------------------ группы и каналы (лог)

    @staticmethod
    def _chat_type(event) -> str:
        """Тип чата: Группа (включая супергруппы), Канал или Чат."""
        if getattr(event, "is_group", False):
            return "Группа"
        if getattr(event, "is_channel", False):
            return "Канал"
        return "Чат"

    def _chat_logging_enabled(self, event) -> bool:
        if getattr(event, "is_group", False):
            return self.cfg.log_groups
        if getattr(event, "is_channel", False):
            return self.cfg.log_channels
        return False

    def _log_chat_event(self, event, outgoing: bool) -> None:
        """Ставит в фон запись сообщения группы/канала во все хранилища."""
        if not self._chat_logging_enabled(event):
            return
        task = asyncio.create_task(self._log_chat_event_task(event, outgoing))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -------------------------------------------------------------- incoming

    async def _on_incoming(self, event: events.NewMessage.Event) -> None:
        log.debug("Событие: peer=%s incoming=%s text=%r", event.chat_id, event.is_private, (event.raw_text or "")[:40])
        try:
            await self._process_incoming(event)
        except Exception as exc:  # noqa: BLE001 — обработчик не должен молча падать
            log.exception("Ошибка обработки входящего: %s", exc)

    async def _process_incoming(self, event: events.NewMessage.Event) -> None:
        try:
            await self._process_incoming_inner(event)
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка обработки сообщения: %s", exc)

    async def _process_incoming_inner(self, event: events.NewMessage.Event) -> None:
        if not event.is_private:
            # Группы и каналы: сохраняем сообщение, но не отвечаем в них.
            self._log_chat_event(event, outgoing=False)
            return
        peer_id = event.chat_id
        text = (event.raw_text or "").strip()
        caption = text
        attachment: Optional[attachments.Attachment] = None
        has_media = bool(getattr(event, "photo", None) or getattr(event, "document", None))
        log.debug("Разбор: peer=%s photo=%s doc=%s text=%r", peer_id, bool(getattr(event, "photo", None)),
                  bool(getattr(event, "document", None)), text[:30])
        if has_media:
            try:
                text, attachment = await asyncio.wait_for(self._attachment_text(event), timeout=45)
            except asyncio.TimeoutError:
                log.error("Скачивание вложения заняло слишком долго (peer=%s)", peer_id)
            except Exception as exc:  # noqa: BLE001 — фото важнее, даже если скачать не вышло
                log.exception("Не удалось извлечь вложение: %s", exc)
        log.info("Разбор сообщения: peer=%s, вложение=%s, текст=%r",
                 peer_id, attachment.kind if attachment else "нет", text[:60])
        if attachment is not None:
            sender = await event.get_sender()
            who = getattr(sender, "first_name", "") or ""
            is_mine = peer_id == getattr(self.me, "id", None) or getattr(event, "out", False)
            if is_mine:
                log.info("Вложение от себя — сразу в таблицу")
                self._sheet_image(attachment, caption, who)
                self._db_media(attachment, caption, who, peer_id)
            else:
                try:
                    self.store.upsert_peer(peer_id, getattr(sender, "username", None), who)
                    self.store.set(f"peer_name_{peer_id}", who)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Не удалось сохранить собеседника: %s", exc)
                self._sheet_log_async("Картинка", who, caption, reply="[фото вставлено в таблицу]", chat_id=peer_id)
                self._sheet_image(attachment, caption, who)
                self._db_media(attachment, caption, who, peer_id)
            return

        sender = await event.get_sender()
        if not isinstance(sender, User) or sender.bot:
            return

        # свои сообщения (в т.ч. в «Избранном») приходим сюда же
        if self.me and (peer_id == self.me.id or sender.id == self.me.id):
            await self._handle_own_message(peer_id, text, None)
            return

        username = sender.username
        self.store.upsert_peer(peer_id, username, sender.first_name)

        if self._blocked(peer_id, username):
            log.info("Заблокирован: peer=%s @%s", peer_id, username)
            return
        if self.cfg.reply_scope == "whitelist" and not self._whitelisted(peer_id, username):
            log.info("Не в белом списке: peer=%s @%s", peer_id, username)
            return
        if self._paused(peer_id):
            log.info("Пауза: peer=%s", peer_id)
            return

        self.store.add_message(peer_id, "user", text)
        log.info("Входящее: peer=%s @%s: %s", peer_id, username, text[:120])
        self.store.set(f"peer_name_{peer_id}", f"{sender.first_name} {('@' + username) if username else ''}".strip())
        self._sheet_log("Личка", sender.first_name or "", text,
                        username=username or "", chat_id=peer_id)
        self._db_log("Личка", sender.first_name or "", text,
                     username=username or "", chat_id=peer_id)
        if attachment is not None:
            self._sheet_image(attachment, caption, sender.first_name or "")
            self._sheet_log_async("Картинка", sender.first_name or "", text,
                                  reply="[фото вставлено в таблицу]", username=username or "", chat_id=peer_id)
            self._db_media(attachment, caption, sender.first_name or "", peer_id)
            return  # на картинку не отвечаем текстом, она уже в таблице

        self._buffers.setdefault(peer_id, []).append(text)
        self._schedule(peer_id, self.cfg.debounce_seconds,
                       lambda: self._respond(peer_id))

    async def _log_chat_event_task(self, event: events.NewMessage.Event, outgoing: bool) -> None:
        """Сохраняет сообщение группы/канала со всеми доступными полями."""
        try:
            chat_id = event.chat_id
            if chat_id is None:
                return
            chat_type = self._chat_type(event)
            try:
                chat = await event.get_chat()
            except Exception as exc:  # noqa: BLE001 — без чата всё равно сохраняем текст
                log.debug("Не удалось получить чат %s: %s", chat_id, exc)
                chat = None
            chat_title = (getattr(chat, "title", "") or getattr(chat, "first_name", "") or "").strip()
            chat_username = (getattr(chat, "username", "") or "").strip()

            try:
                sender = await event.get_sender()
            except Exception as exc:  # noqa: BLE001 — у постов канала отправителя может не быть
                log.debug("Не удалось получить отправителя в %s: %s", chat_id, exc)
                sender = None
            sender_id = getattr(sender, "id", None) or getattr(event, "sender_id", None)
            sender_name = ""
            sender_username = ""
            if sender is not None:
                sender_name = (getattr(sender, "first_name", "") or getattr(sender, "title", "") or "").strip()
                sender_username = (getattr(sender, "username", "") or "").strip()
            if not sender_name:
                if chat_type == "Канал":
                    # у поста канала отправителя нет — пишем сам канал
                    sender_name = chat_title
                    sender_username = chat_username
                    sender_id = chat_id
                elif outgoing and self.me is not None:
                    sender_name = self.me.first_name or "владелец"
                    sender_username = self.me.username or ""
                    sender_id = self.me.id
                else:
                    sender_name = "Аноним"

            if getattr(sender, "bot", False) and not self.cfg.log_bots:
                return

            text = (event.raw_text or "").strip()
            attachment: Optional["attachments.Attachment"] = None
            if self.cfg.log_chat_media and self._has_attachment(event):
                try:
                    async with self._media_semaphore:
                        attachment = await asyncio.wait_for(attachments.extract(event), timeout=45)
                except asyncio.TimeoutError:
                    log.warning("Вложение из чата %s не скачано за 45 с", chat_id)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Не удалось извлечь вложение из чата %s: %s", chat_id, exc)

            media_url = ""
            media_type = ""
            if attachment is not None:
                media_type = attachment.kind or "media"
                if attachment.image or attachment.file:
                    try:
                        async with self._media_semaphore:
                            media_url = await self._upload_attachment(attachment)
                    except Exception as exc:  # noqa: BLE001
                        log.error("Медиа из чата %s не загружено: %s", chat_id, exc)
                if not text:
                    text = attachments.describe(attachment)
            elif not text:
                # Пустые служебные события (вход/выход, закрепление) не пишем.
                return

            direction = "out" if outgoing else "in"
            try:
                self.store.log_chat(
                    chat_id=chat_id, chat_type=chat_type, chat_title=chat_title,
                    chat_username=chat_username, sender_id=sender_id,
                    sender_name=sender_name, sender_username=sender_username,
                    direction=direction, text=text,
                    media_url=media_url, media_type=media_type,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Не удалось записать чат %s в SQLite: %s", chat_id, exc)

            if self.sink and self.sink.enabled:
                self._sheet_log_async(
                    chat_type, sender_name, text,
                    reply="[медиа]" if attachment is not None else "",
                    username=sender_username, chat_id=chat_id,
                    chat_title=chat_title, chat_username=chat_username,
                    sender_id=sender_id, direction=direction,
                    media_url=media_url, media_type=media_type,
                )
                if attachment is not None and attachment.image:
                    self._sheet_image(attachment, text, sender_name)

            self._db_log(
                chat_type, sender_name, text, username=sender_username,
                chat_id=chat_id, media_url=media_url, media_type=media_type,
                chat_title=chat_title, chat_username=chat_username,
                sender_id=sender_id, direction=direction,
            )
            log.debug("Сохранено %s-сообщение из %s «%s»: %s",
                      direction, chat_type, chat_title, text[:80])
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка логирования группы/канала: %s", exc)

    @staticmethod
    def _has_attachment(event: events.NewMessage.Event) -> bool:
        message = event.message
        if message is None:
            return False
        return bool(message.photo or message.document or message.voice or message.audio
                    or message.video or message.video_note or message.sticker or message.gif
                    or message.contact or message.geo or message.poll)

    def _schedule(self, key: int, delay: float, coro_factory) -> None:
        old = self._timers.get(key)
        if old and not old.done():
            old.cancel()
        self._timers[key] = asyncio.create_task(self._delayed(delay, key, coro_factory))

    async def _delayed(self, delay: float, key: int, coro_factory) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        await coro_factory()

    async def _respond(self, peer_id: int, image: Optional[str] = None) -> None:
        texts = self._buffers.pop(peer_id, [])
        if not texts:
            return

        lock = self._locks.setdefault(peer_id, asyncio.Lock())
        async with lock:
            if self._paused(peer_id):
                return
            if self._owner_recently_active(peer_id):
                log.info("Владелец сам ответил недавно — молчим (peer=%s)", peer_id)
                return
            if self.store.replies_last_hour(peer_id) >= self.cfg.max_replies_per_hour:
                log.warning("Лимит ответов в час (peer=%s)", peer_id)
                return
            last = self.store.last_reply_ts(peer_id)
            if last and time.time() - last < self.cfg.min_gap_seconds:
                log.info("Слишком часто (peer=%s) — пропуск", peer_id)
                return

            user_text = "\n".join(texts)
            try:
                answer = await self._generate(peer_id, user_text)
            except Exception as exc:  # noqa: BLE001
                log.error("Ошибка генерации (peer=%s): %s", peer_id, exc)
                return

            if self.cfg.disclose_ai and not self.store.is_disclosed(peer_id):
                answer += DISCLOSURE
                self.store.set_disclosed(peer_id)

            self.store.add_message(peer_id, "assistant", answer)
            await self._send(peer_id, answer)
            self._sheet_log("Личка", self.store.get(f"peer_name_{peer_id}", "") or "ассистент",
                            user_text, reply=answer, chat_id=peer_id)
            self._db_log("Личка", self.store.get(f"peer_name_{peer_id}", "") or "ассистент",
                         user_text, reply=answer, chat_id=peer_id)

    async def _generate(self, peer_id: int, user_text: str) -> str:
        typing = asyncio.create_task(self._keep_typing(peer_id))
        try:
            answer = await self.llm.reply(peer_id, user_text)
        finally:
            typing.cancel()
            await self._stop_typing(peer_id)
        delay = min(self.cfg.max_delay, max(self.cfg.min_delay, len(answer) / 60))
        await asyncio.sleep(delay + random.uniform(0, 0.8))
        return answer

    async def _attachment_text(self, event) -> tuple[str, Optional["attachments.Attachment"]]:
        """(текст для LLM, вложение с картинкой или None)."""
        log.info("Обрабатываю вложение: peer=%s", event.chat_id)
        attachment = await attachments.extract(event)
        if attachment is None:
            log.warning("Вложение не распознано: peer=%s", event.chat_id)
            return "", None
        log.info("Вложение: %s (%s), байт: %s", attachment.kind, attachment.description[:60],
                 len(attachment.image or b""))
        return attachments.describe(attachment), attachment

    def _sheet_image(self, attachment: Optional["attachments.Attachment"],
                     caption: str = "", sender: str = "") -> None:
        """Вставляет картинку в таблицу отдельной строкой-картинкой."""
        if attachment is None or not attachment.image or not self.sink or not self.sink.enabled:
            return
        task = asyncio.create_task(self._sheet_image_task(attachment, caption, sender))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _sheet_log_async(self, channel: str, sender: str, text: str, reply: str = "",
                         username: str = "", chat_id: int | str = "",
                         chat_title: str = "", chat_username: str = "",
                         sender_id: int | str | None = None, direction: str = "",
                         media_url: str = "", media_type: str = "") -> None:
        if not self.sink or not self.sink.enabled:
            return
        task = asyncio.create_task(self._sheet_append(self.sink.build_row(
            channel, sender, text, reply, username, chat_id,
            chat_title=chat_title, chat_username=chat_username,
            sender_id=sender_id, direction=direction,
            media_url=media_url, media_type=media_type,
        )))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _sheet_image_task(self, attachment: "attachments.Attachment", caption: str, sender: str) -> None:
        try:
            drawing_id = await asyncio.wait_for(
                asyncio.to_thread(
                    self.sink.append_image,  # type: ignore[union-attr]
                    attachment.image, attachment.image_mime, attachment.width, attachment.height,
                    name=f"{sender} {caption}".strip()[:80],
                ),
                timeout=60,
            )
            if drawing_id:
                log.info("Фото добавлено в таблицу (drawing %s)", drawing_id)
            else:
                log.warning("Фото не добавлено в таблицу (см. ошибки выше)")
        except Exception as exc:  # noqa: BLE001
            log.error("Фото не добавлено в таблицу: %s", exc)

    # -------------------------------------------------------------- outgoing

    async def _on_outgoing(self, event: events.NewMessage.Event) -> None:
        log.debug("Исходящее: peer=%s text=%r", event.chat_id, (event.raw_text or "")[:40])
        try:
            await self._process_outgoing(event)
        except Exception as exc:  # noqa: BLE001 — обработчик не должен молча падать
            log.exception("Ошибка обработки исходящего: %s", exc)

    async def _process_outgoing(self, event: events.NewMessage.Event) -> None:
        if not event.is_private:
            # Свои сообщения в группах/каналах: сохраняем, но не вмешиваемся.
            self._log_chat_event(event, outgoing=True)
            return
        text = (event.raw_text or "").strip()

        if self._is_our_send(event.chat_id, text):
            return  # наша собственная отправка уже отражена в БД

        if self.me and event.chat_id == self.me.id:
            await self._handle_own_message(event.chat_id, text)
            return

        if event.document or event.photo:
            if not text and self.me and event.chat_id == self.me.id:
                return
            if self.cfg.sheet_log_owner:
                self._sheet_log("Ваш ответ", self.me.first_name if self.me else "владелец",
                                f"[вложение] {text}".strip(), chat_id=event.chat_id)
            if self.cfg.supabase_log_owner:
                self._db_log("Ваш ответ", self.me.first_name if self.me else "владелец",
                             f"[вложение] {text}".strip(), chat_id=event.chat_id)
        if event.id in self._sent_ids:
            return

        # владелец написал контакту сам — не мешаем
        self.store.set(f"owner_active_{event.chat_id}", str(time.time()))
        timer = self._timers.pop(event.chat_id, None)
        if timer and not timer.done():
            timer.cancel()
        self._buffers.pop(event.chat_id, None)
        log.info("Владелец ответил сам: peer=%s", event.chat_id)
        if self.cfg.sheet_log_owner:
            self._sheet_log("Ваш ответ", self.me.first_name if self.me else "владелец", text,
                            chat_id=event.chat_id)
        if self.cfg.supabase_log_owner:
            self._db_log("Ваш ответ", self.me.first_name if self.me else "владелец", text,
                         chat_id=event.chat_id)

    async def _handle_own_message(self, peer_id: int, text: str, image: Optional[str] = None) -> None:
        """Сообщение, отправленное с этого аккаунта: команда или вопрос ассистенту."""
        if not text:
            return
        if text.startswith("/"):
            log.info("Команда владельца: %s", text[:60])
            await self._handle_command(text)
            return
        if not self.cfg.self_chat_mode:
            return
        self._buffers.setdefault(peer_id, []).append(text)
        self._schedule(peer_id, 0.5, lambda: self._respond_self(peer_id, image))

    def _is_our_send(self, peer_id: int, text: str) -> bool:
        now = time.time()
        for p, t, ts in list(self._recent_sends):
            if p == peer_id and t == text and now - ts < 10:
                return True
        return False

    def _owner_recently_active(self, peer_id: int) -> bool:
        raw = self.store.get(f"owner_active_{peer_id}")
        if not raw:
            return False
        try:
            minutes = int(self.store.get("takeover_minutes", str(self.cfg.owner_takeover_minutes)))
        except ValueError:
            minutes = self.cfg.owner_takeover_minutes
        return time.time() - float(raw) < minutes * 60

    async def _respond_self(self, peer_id: int, image: Optional[str] = None) -> None:
        texts = self._buffers.pop(peer_id, [])
        if not texts:
            return
        user_text = "\n".join(texts)
        self.store.add_message(peer_id, "user", user_text)
        self._sheet_log("Избранное", self.me.first_name if self.me else "владелец", user_text,
                        chat_id=peer_id)
        self._db_log("Избранное", self.me.first_name if self.me else "владелец", user_text,
                     chat_id=peer_id)
        try:
            answer = await self._generate(peer_id, user_text)
        except Exception as exc:  # noqa: BLE001
            log.error("Ошибка генерации (self): %s", exc)
            await self._send(peer_id, "Не смог сгенерировать ответ — проверьте ключ LLM и лог.")
            return
        self.store.add_message(peer_id, "assistant", answer)
        await self._send(peer_id, answer)
        self._sheet_log("Избранное", "ИИ-ассистент", "", reply=answer, chat_id=peer_id)
        self._db_log("Избранное", "ИИ-ассистент", "", reply=answer, chat_id=peer_id)

    # ------------------------------------------------------------ отправка

    async def _send(self, peer_id: int, text: str) -> None:
        from llm import split_text

        for part in split_text(text):
            for attempt in range(2):
                try:
                    msg = await self.client.send_message(peer_id, part, link_preview=False)
                    self._sent_ids.append(msg.id)
                    self._recent_sends.append((peer_id, part, time.time()))
                    if self.cfg.mark_read:
                        try:
                            await self.client.send_read_acknowledge(peer_id, max_id=msg.id, clear_mentions=True)
                        except Exception:  # noqa: BLE001
                            pass
                    break
                except FloodWaitError as exc:
                    log.warning("FloodWait %s c (peer=%s)", exc.seconds, peer_id)
                    await asyncio.sleep(exc.seconds + 1)
                except Exception as exc:  # noqa: BLE001
                    log.error("Не удалось отправить (peer=%s): %s", peer_id, exc)
                    break
            await asyncio.sleep(random.uniform(0.5, 1.2))

    async def _keep_typing(self, peer_id: int) -> None:
        while True:
            try:
                await self.client(SetTypingRequest(peer_id, SendMessageTypingAction()))
            except Exception:  # noqa: BLE001
                return
            await asyncio.sleep(4)

    async def _stop_typing(self, peer_id: int) -> None:
        try:
            await self.client(SetTypingRequest(peer_id, SendMessageTypingAction()))
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------- команды

    async def _handle_command(self, text: str) -> None:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.strip().lower()
        try:
            if cmd == "/help":
                reply = HELP_TEXT
            elif cmd == "/status":
                reply = self._status_text()
            elif cmd == "/pause":
                self.store.set("global_pause", "1")
                reply = "Пауза включена: ассистент никому не отвечает."
            elif cmd == "/resume":
                self.store.set("global_pause", "0")
                reply = "Пауза снята."
            elif cmd == "/reset":
                self.store.clear_all()
                reply = "История всех диалогов очищена."
            elif cmd == "/prompt":
                if not arg.strip():
                    reply = "Текущий промпт:\n" + self._current_prompt()
                elif arg.strip().lower() == "reset":
                    self.store.delete("prompt_override")
                    reply = "Промпт вернул к значению из .env."
                else:
                    self.store.set("prompt_override", arg.strip())
                    reply = "Промпт обновлён (действует до перезапуска)."
            elif cmd == "/block":
                if not arg.strip():
                    reply = "Укажите id или @username."
                else:
                    ids = [x for x in self.store.get("blacklist", "").split(",") if x.strip()]
                    ids.append(arg.strip().lstrip("@"))
                    self.store.set("blacklist", ",".join(ids))
                    reply = f"Заблокирован: {arg.strip()}"
            elif cmd == "/unblock":
                ids = [x for x in self.store.get("blacklist", "").split(",") if x.strip()]
                ids = [x for x in ids if self._norm(x) != self._norm(arg)]
                self.store.set("blacklist", ",".join(ids))
                reply = f"Разблокирован: {arg.strip()}" if arg.strip() else "Чёрный список пуст."
            elif cmd == "/takeover":
                if not arg.strip().isdigit():
                    reply = "Нужно число минут, например /takeover 30"
                else:
                    self.store.set("takeover_minutes", arg.strip())
                    reply = f"После ручного ответа молчим {arg.strip()} мин."
            else:
                return
        except Exception as exc:  # noqa: BLE001
            log.error("Ошибка команды %s: %s", cmd, exc)
            reply = f"Ошибка: {exc}"
        await self._send(self.me.id, reply)

    def _current_prompt(self) -> str:
        override = self.store.get("prompt_override")
        return (override or self.cfg.system_prompt)[:1500]

    def _status_text(self) -> str:
        stats = self.store.stats()
        last = "—"
        if stats["last_reply"]:
            last = time.strftime("%d.%m %H:%M", time.localtime(stats["last_reply"]))
        takeover = self.store.get("takeover_minutes", str(self.cfg.owner_takeover_minutes))
        return (
            f"Ассистент: {'на паузе' if self.store.get('global_pause') == '1' else 'работает'}\n"
            f"Аккаунт: {self.me.first_name} (@{self.me.username})\n"
            f"Модель: {self.cfg.llm_model}\n"
            f"Диалогов: {stats['peers']}, сообщений: {stats['messages']}\n"
            f"Групп/каналов: {stats.get('chats', 0)}, сообщений: {stats.get('chat_messages', 0)}\n"
            f"Последний ответ: {last}\n"
            f"Лимит: {self.cfg.max_replies_per_hour}/час, молчание после вас: {takeover} мин"
        )
