"""Диагностика: какие обновления реально приходят от Telegram."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from telethon import TelegramClient, events  # noqa: E402

from config import SESSION_PATH, load_config  # noqa: E402


async def main() -> int:
    cfg = load_config(require_llm=False)
    client = TelegramClient(str(SESSION_PATH), cfg.api_id, cfg.api_hash)
    me = None

    @client.on(events.NewMessage)
    async def on_msg(event: events.NewMessage.Event) -> None:
        sender = await event.get_sender()
        print(
            f"[event] id={event.id} chat_id={event.chat_id} incoming={event.is_private} "
            f"out={event.out} text={event.raw_text!r} sender={getattr(sender, 'id', None)}",
            flush=True,
        )

    @client.on(events.Raw)
    async def on_raw(update) -> None:
        name = type(update).__name__
        if name in {"UpdateShortMessage", "UpdateShortChatMessage"}:
            print(f"[raw] {name} text={getattr(update, 'message', None)!r}", flush=True)

    await client.connect()
    me = await client.get_me()
    print(f"вход: {me.first_name} id={me.id}", flush=True)
    print("последние 3 сообщения в «Избранном»:", flush=True)
    async for msg in client.iter_messages(me.id, limit=3):
        print(f"   {msg.id}: {msg.raw_text!r}", flush=True)

    print("\nЖду 90 секунд. Любое ваше сообщение в Telegram (в «Избранном» или в любом чате) будет напечатано.", flush=True)
    print("Отключение (Disconnected) или ошибки — тоже выводятся.\n", flush=True)

    @client.on(events.Raw)
    async def on_disconnect(*args) -> None:
        pass

    try:
        await asyncio.wait_for(asyncio.shield(asyncio.sleep(90)), timeout=90)
    except asyncio.TimeoutError:
        pass
    print("connected:", client.is_connected(), flush=True)
    await client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
