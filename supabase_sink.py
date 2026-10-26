"""Реально-временная запись сообщений в проект Supabase (supabase.com).

Использует PostgREST REST API (``/rest/v1/<table>``) для вставки строк и
Supabase Storage (``/storage/v1/object/...``) для сохранения медиа (картинок).
Работает полностью неблокирующе и независимо от таблицы SheetDesk: сбой
Supabase не останавливает переписку.

Требуемая структура таблицы — см. ``bootstrap.sql``. По умолчанию таблица
``messages`` в публичной схеме.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Dict, Optional

import httpx

log = logging.getLogger(__name__)

HTTP_TIMEOUT = 10.0


class SupabaseSink:
    """Дописывает строки в таблицу Supabase и грузит медиа в Storage."""

    def __init__(self, url: str, key: str, table: str = "messages",
                 bucket: str = "messages", write_replies: bool = True,
                 log_owner: bool = True, journal: bool = True,
                 journal_table: str = "journal") -> None:
        self.url = url.rstrip("/")
        self.key = key.strip()
        self.table = (table or "messages").strip()
        self.bucket = (bucket or "messages").strip()
        self.write_replies = write_replies
        self.log_owner = log_owner
        self.journal_enabled = journal
        self.journal_table = (journal_table or "journal").strip()
        self._write_lock = threading.Lock()
        # Какие необязательные колонки реально есть в таблице (после миграции).
        self._has: Dict[str, bool] = {}
        # trust_env=False: переменные HTTPS_PROXY/NO_PROXY окружения не должны ломать запросы
        self._client = httpx.Client(timeout=HTTP_TIMEOUT, trust_env=False)

    # ------------------------------------------------------------- helpers

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.key)

    def _headers(self, with_prefer: bool = True) -> Dict[str, str]:
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        if with_prefer:
            headers["Prefer"] = "return=representation"
        return headers

    def _storage_url(self, path: str) -> str:
        return f"{self.url}/storage/v1/object/{path}"

    # --------------------------------------------------------------- public

    def ensure(self) -> bool:
        """Проверяет, существует ли таблица. True — таблица доступна.

        Таблица создаётся один раз вручную через SQL-редактор Supabase
        (см. bootstrap.sql) — PostgREST не умеет создавать таблицы.
        """
        if not self.enabled:
            return False
        try:
            response = self._client.get(
                f"{self.url}/rest/v1/{self.table}?select=*&limit=1",
                headers=self._headers(with_prefer=False),
            )
            if response.status_code in (200, 206):
                log.info("Таблица Supabase %s доступна", self.table)
                self._detect_columns()
                return True
            log.error("Таблица Supabase %s недоступна (HTTP %s). Создайте её по SQL из bootstrap.sql: %s",
                      self.table, response.status_code, response.text[:200])
            return False
        except Exception as exc:  # noqa: BLE001
            log.error("Supabase недоступен (%s): %s", self.url, exc)
            return False

    def _detect_columns(self) -> None:
        """Проверяет наличие необязательных колонок (миграция из bootstrap.sql)."""
        for column in ("chat_title", "chat_username", "sender_id", "direction"):
            try:
                response = self._client.get(
                    f"{self.url}/rest/v1/{self.table}?select={column}&limit=0",
                    headers=self._headers(with_prefer=False),
                )
                self._has[column] = response.status_code in (200, 206)
            except Exception as exc:  # noqa: BLE001
                self._has[column] = False
                log.debug("Не удалось проверить колонку %s: %s", column, exc)
        missing = [name for name, present in self._has.items() if not present]
        if missing:
            log.warning(
                "В таблице %s нет колонок %s — данные чата сохранятся в channel. "
                "Выполните миграцию из bootstrap.sql.",
                self.table, ", ".join(missing),
            )

    def append(self, values: Dict) -> bool:
        """Вставляет одну строку (dict) в таблицу. Результат пишется в журнал."""
        if not self.enabled:
            return False
        ok = False
        detail = ""
        with self._write_lock:
            for attempt in range(1, 3):
                try:
                    response = self._client.post(
                        f"{self.url}/rest/v1/{self.table}",
                        headers=self._headers(),
                        json=[values],
                    )
                    if response.status_code < 300:
                        log.info("Сообщение записано в Supabase (%s)", self.table)
                        ok = True
                        detail = f"{self.table} insert ok"
                        break
                    detail = f"{self.table} insert HTTP {response.status_code}: {response.text[:200]}"
                    log.warning("%s", detail)
                except Exception as exc:  # noqa: BLE001
                    detail = f"{self.table} insert error (попытка {attempt}/2): {exc}"
                    log.warning("%s", detail)
                    time.sleep(1.0)
        self._journal("message_insert", "ok" if ok else "fail", detail,
                      channel=values.get("channel", ""), chat_id=values.get("chat_id", ""))
        return ok

    def upload_media(self, data: bytes, filename: str,
                     content_type: str = "application/octet-stream",
                     kind: str = "") -> str:
        """Загружает файл в Storage-ковш. Возвращает публичный URL или пустую строку."""
        if not self.enabled:
            return ""
        path = f"{self.bucket}/{filename}"
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": content_type or "application/octet-stream",
        }
        url = ""
        detail = ""
        for attempt in range(1, 3):
            try:
                response = self._client.put(self._storage_url(path), headers=headers, content=data)
                if response.status_code < 300:
                    ref = self.url.split("//", 1)[-1].split(".", 1)[0]
                    url = f"https://{ref}.supabase.co/storage/v1/object/public/{path}"
                    detail = f"upload ok: {filename}"
                    break
                detail = f"upload HTTP {response.status_code}: {response.text[:200]}"
                log.warning("Supabase upload HTTP %s: %s", response.status_code, response.text[:200])
            except Exception as exc:  # noqa: BLE001
                detail = f"upload error (попытка {attempt}/2): {exc}"
                log.warning("Ошибка загрузки в Storage (попытка %s/2): %s", attempt, exc)
                time.sleep(1.0)
        self._journal(kind or "media_upload", "ok" if url else "fail", detail)
        return url

    def _insert(self, table: str, values: Dict) -> bool:
        """Вставляет строку без журналирования (используется самим журналом)."""
        if not self.enabled:
            return False
        try:
            response = self._client.post(
                f"{self.url}/rest/v1/{table}",
                headers=self._headers(),
                json=[values],
            )
            return response.status_code < 300
        except Exception:  # noqa: BLE001
            return False

    def journal(self, event: str, status: str, detail: str,
                channel: str = "", chat_id: str = "") -> None:
        """Публичная запись события в журнал (для внешних вызовов)."""
        self._journal(event, status, detail, channel=channel, chat_id=chat_id)

    def _journal(self, event: str, status: str, detail: str,
                 channel: str = "", chat_id: str = "") -> None:
        """Пишет строку в таблицу журнала (неблокирующе, сбой только в лог)."""
        if not self.journal_enabled:
            return
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        row = {
            "created_at": stamp,
            "event": (event or "")[:100],
            "status": (status or "")[:40],
            "detail": (detail or "")[:2000],
            "channel": (channel or "")[:200],
            "chat_id": str(chat_id or ""),
        }
        try:
            with self._write_lock:
                self._insert(self.journal_table, row)
        except Exception as exc:  # noqa: BLE001
            log.debug("Журнал Supabase недоступен: %s", exc)

    def build_row(self, channel: str, sender: str, text: str, reply: str = "",
                  username: str = "", chat_id: int | str = "",
                  media_url: str = "", media_type: str = "",
                  chat_title: str = "", chat_username: str = "",
                  sender_id: int | str | None = None, direction: str = "") -> Dict:
        """Строка-словарь для вставки: ключи = колонки таблицы.

        Необязательные поля (chat_title/chat_username/sender_id/direction)
        добавляются только если колонки есть в таблице. Иначе важные данные
        не теряются: название и username чата дописываются в channel.
        """
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        label = channel
        if chat_title and not self._has.get("chat_title"):
            label = f"{label} · {chat_title}"
        if chat_username and not self._has.get("chat_username"):
            label = f"{label} (@{chat_username})"
        if direction == "out" and not self._has.get("direction"):
            label = f"{label} · исходящее"

        row: Dict = {
            "created_at": stamp,
            "channel": label,
            "sender": sender,
            "username": f"@{username}" if username else "",
            "chat_id": str(chat_id),
            "message": text,
            "reply": reply,
            "media_url": media_url,
            "media_type": media_type,
        }
        if self._has.get("chat_title"):
            row["chat_title"] = chat_title
        if self._has.get("chat_username"):
            row["chat_username"] = chat_username
        if self._has.get("sender_id"):
            row["sender_id"] = str(sender_id or "")
        if self._has.get("direction"):
            row["direction"] = direction
        return row

    def append_message(self, channel: str, sender: str, text: str, reply: str = "",
                       username: str = "", chat_id: int | str = "",
                       media_url: str = "", media_type: str = "",
                       chat_title: str = "", chat_username: str = "",
                       sender_id: int | str | None = None, direction: str = "") -> bool:
        return self.append(self.build_row(
            channel, sender, text, reply, username, chat_id, media_url, media_type,
            chat_title=chat_title, chat_username=chat_username,
            sender_id=sender_id, direction=direction,
        ))

    def close(self) -> None:
        self._client.close()
