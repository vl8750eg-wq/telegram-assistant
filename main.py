"""Точка входа: запускает ассистента."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler

from telethon import TelegramClient
from telethon.sessions import StringSession

from config import DB_PATH, LOG_PATH, SESSION_PATH, ConfigError, load_config
from handlers import Assistant
from llm import LLM
from sheets import SheetsSink
from store import Store
from supabase_sink import SupabaseSink

log = logging.getLogger("assistant")


def setup_logging(level: str) -> None:
    level_value = getattr(logging, level.upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%d.%m %H:%M:%S")

    root = logging.getLogger()
    root.setLevel(level_value)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def start_health_server() -> None:
    """HTTP-порт для облачных платформ (Render): любой GET -> 200 OK.

    Порт задаётся переменной PORT; локально переменной нет — сервер не стартует.
    """
    port = int((os.getenv("PORT") or "0").strip() or 0)
    if not port:
        return
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="health").start()
    log.info("Health-сервер: порт %s", port)


async def amain() -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}")
        return 2

    setup_logging(cfg.log_level)
    # порт открываем до подключения к Telegram: облако ждёт именно HTTP-порт
    start_health_server()
    store = Store(DB_PATH)
    llm = LLM(cfg, store)
    sink = None
    if cfg.sheet_url:
        sink = SheetsSink(
            base_url=cfg.sheet_url,
            workbook=cfg.sheet_workbook,
            sheet=cfg.sheet_name,
            columns=cfg.sheet_columns,
            style=cfg.sheet_style,
            write_replies=cfg.sheet_write_replies,
        )
    db_sink = None
    if cfg.supabase_url:
        db_sink = SupabaseSink(
            url=cfg.supabase_url,
            key=cfg.supabase_key,
            table=cfg.supabase_table,
            bucket=cfg.supabase_bucket,
            write_replies=cfg.supabase_write_replies,
            log_owner=cfg.supabase_log_owner,
            journal=cfg.supabase_journal,
            journal_table=cfg.supabase_journal_table,
        )
    session_str = (os.getenv("TG_SESSION_STRING") or "").strip()
    client = TelegramClient(
        StringSession(session_str) if session_str else str(SESSION_PATH),
        cfg.api_id,
        cfg.api_hash,
        flood_sleep_threshold=120,
        device_model="AI Assistant",
        # не завершаться при долгих простоях сети: ждать соединения бесконечно
        connection_retries=None,
        retry_delay=10,
    )

    await client.connect()
    if not await client.is_user_authorized():
        print("Сессия не авторизована. Запустите: python login.py")
        await client.disconnect()
        store.close()
        return 2

    assistant = Assistant(client, cfg, store, llm, sink=sink, db_sink=db_sink)
    await assistant.start()

    print("\nИИ-ассистент запущен. Ctrl+C — остановить.")
    if sink and sink.enabled:
        print(f"Новые сообщения -> таблица {cfg.sheet_url} / «{sink.workbook}»")
    if db_sink and db_sink.enabled:
        print(f"Новые сообщения -> Supabase {cfg.supabase_url} / «{db_sink.table}»")
    print(f"Лог: {LOG_PATH}\n")
    try:
        await client.run_until_disconnected()
    except KeyboardInterrupt:
        pass
    finally:
        await client.disconnect()
        if sink:
            sink.close()
        if db_sink:
            db_sink.close()
        store.close()
    log.info("Остановлено")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(amain()))
    except KeyboardInterrupt:
        sys.exit(0)
