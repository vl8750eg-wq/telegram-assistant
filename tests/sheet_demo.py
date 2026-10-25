"""Ручная проверка записи в таблицу (не трогает сессию ассистента)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import DB_PATH, load_config  # noqa: E402
from handlers import Assistant  # noqa: E402
from llm import LLM  # noqa: E402
from sheets import SheetsSink  # noqa: E402
from store import Store  # noqa: E402


async def main() -> int:
    cfg = load_config()
    store = Store(DB_PATH)
    llm = LLM(cfg, store)
    sink = SheetsSink(cfg.sheet_url, cfg.sheet_workbook, cfg.sheet_name, cfg.sheet_columns, cfg.sheet_style)

    assistant = Assistant(client=None, cfg=cfg, store=store, llm=llm, sink=sink)  # type: ignore[arg-type]
    me_id = 8444434959
    user_text = "Демо: проверьте запись в таблицу"

    answer = await llm.reply(me_id, user_text)
    print("ответ LLM:", answer[:100])

    assistant._sheet_log("Избранное", "владелец", user_text, "", chat_id=me_id)
    assistant._sheet_log("Избранное", "ИИ-ассистент", "", reply=answer, chat_id=me_id)
    await asyncio.sleep(2)
    for task in asyncio.all_tasks():
        if task is not asyncio.current_task():
            await task

    sink.close()
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
