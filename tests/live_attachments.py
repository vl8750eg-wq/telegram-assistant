"""Живая диагностика вложений: слушает «Избранное» и печатает, что пришло."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from telethon import TelegramClient, events  # noqa: E402

import attachments  # noqa: E402
from config import SESSION_PATH, load_config  # noqa: E402


async def main() -> int:
    cfg = load_config(require_llm=False)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)

    client = TelegramClient(str(SESSION_PATH), cfg.api_id, cfg.api_hash)
    await client.connect()
    me = await client.get_me()
    print("вход:", me.first_name, me.id, flush=True)

    @client.on(events.NewMessage)
    async def handler(event: events.NewMessage.Event) -> None:
        print("СОБЫТИЕ:", event.chat_id, "out=", event.out, "photo=", bool(event.photo),
              "doc=", bool(event.document), "text=", repr((event.raw_text or "")[:40]), flush=True)
        attachment = await attachments.extract(event)
        if attachment:
            print("  вложение:", attachment.kind, f"{attachment.width}x{attachment.height}",
                  "bytes=", len(attachment.image or b""), flush=True)

    print("Жду 60 секунд — напишите в «Избранное» (текст или фото).", flush=True)
    await asyncio.sleep(60)
    await client.disconnect()
    print("готово", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
