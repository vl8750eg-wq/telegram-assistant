"""Прямой вызов Path A: _handle_own_message + задача вставки картинки."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import attachments  # noqa: E402
from config import DB_PATH, load_config  # noqa: E402
from handlers import Assistant  # noqa: E402
from llm import LLM  # noqa: E402
from sheets import SheetsSink  # noqa: E402
from store import Store  # noqa: E402

PHOTO = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "tmp" / "test-photo.jpg"


class FakeClient:
    async def __call__(self, request):
        return None

    async def send_message(self, peer, text, **kw):
        class M:
            id = 1

        return M()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    store = Store(DB_PATH)
    llm = LLM(cfg, store)
    sink = SheetsSink(cfg.sheet_url, cfg.sheet_workbook, cfg.sheet_name, cfg.sheet_columns, cfg.sheet_style)
    assistant = Assistant(FakeClient(), cfg, store, llm, sink)

    class Me:
        id = 8444434959
        first_name = "Jedi"
        username = "jedio7"

    assistant.me = Me()
    raw = PHOTO.read_bytes()
    data, mime, width, height = attachments._shrink_image(raw)
    att = attachments.Attachment(kind="photo", description="фото", image=data, image_mime=mime,
                                 width=width, height=height)
    assistant._sheet_image(att, "проверка", "Jedi")
    assistant._sheet_log_async("Картинка", "Jedi", "проверка", reply="[фото вставлено в таблицу]",
                               chat_id=8444434959)
    print("задач:", len(assistant._tasks), flush=True)
    for _ in range(120):
        if not assistant._tasks:
            break
        await asyncio.sleep(0.5)
    print("задачи: завершены", flush=True)
    await asyncio.sleep(1)
    sink.close()
    store.close()


if __name__ == "__main__":
    asyncio.run(main())
