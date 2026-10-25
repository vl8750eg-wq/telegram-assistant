"""Симуляция входящего фото через обработчик (реальное сообщение из «Избранного»)."""

from __future__ import annotations

import asyncio
import logging
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from telethon import TelegramClient  # noqa: E402

from config import DB_PATH, SESSION_PATH, load_config  # noqa: E402
from handlers import Assistant  # noqa: E402
from llm import LLM  # noqa: E402
from sheets import SheetsSink  # noqa: E402
from store import Store  # noqa: E402

MESSAGE_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 616


class Ev:
    def __init__(self, msg):
        self.message = msg

    async def download_media(self, file=None):
        return await self.message.download_media(file=file)

    @property
    def chat_id(self):
        return 8444434959

    @property
    def is_private(self):
        return True

    @property
    def raw_text(self):
        return ""

    @property
    def photo(self):
        return self.message.photo

    @property
    def document(self):
        return self.message.document

    async def get_sender(self):
        class S:
            id = 8444434959
            username = "jedio7"
            first_name = "Jedi"
            bot = False

        return S()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    store = Store(DB_PATH)
    llm = LLM(cfg, store)
    sink = SheetsSink(cfg.sheet_url, cfg.sheet_workbook, cfg.sheet_name, cfg.sheet_columns, cfg.sheet_style)
    client = TelegramClient(str(SESSION_PATH), cfg.api_id, cfg.api_hash)
    await client.connect()
    me = await client.get_me()
    assistant = Assistant(client, cfg, store, llm, sink)
    assistant.me = me
    msg = await client.get_messages(me.id, ids=MESSAGE_ID)
    print("фото:", bool(msg.photo), "| id:", msg.id, flush=True)
    print("photo attr:", bool(getattr(msg, 'photo', None)), "| document:", bool(getattr(msg, 'document', None)), flush=True)
    try:
        await assistant._process_incoming(Ev(msg))
    except BaseException as exc:  # noqa: BLE001
        print("ИСКЛЮЧЕНИЕ:", type(exc).__name__, exc, flush=True)
        traceback.print_exc()
    await asyncio.sleep(15)
    print("задачи в работе:", len(assistant._tasks), flush=True)
    sink.close()
    store.close()
    await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except BaseException:  # noqa: BLE001
        traceback.print_exc()
