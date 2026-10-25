"""Синхронная проверка: что сейчас возвращает getDifference для "Избранного"."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telethon import TelegramClient  # noqa: E402
from telethon.tl.functions.updates import GetStateRequest, GetDifferenceRequest  # noqa: E402

from config import load_config, SESSION_PATH  # noqa: E402


async def main() -> None:
    cfg = load_config(require_llm=False)
    client = TelegramClient(str(SESSION_PATH), cfg.api_id, cfg.api_hash)
    await client.connect()
    me = await client.get_me()
    for attempt in (1, 2):
        state = await client(GetStateRequest())
        diff = await client(GetDifferenceRequest(pts=state.pts - 30, date=state.date, qts=state.qts))
        print(f"--- попытка {attempt} ---", flush=True)
        print("state pts:", state.pts, "| diff type:", type(diff).__name__, flush=True)
        for upd in getattr(diff, "updates", []):
            name = type(upd).__name__
            if name in {"UpdateNewMessage", "UpdateShortMessage", "UpdateNewChannelMessage"}:
                msg = getattr(upd, "message", None)
                text = getattr(msg, "message", None) or getattr(upd, "message", None)
                media = type(getattr(msg, "media", None)).__name__ if msg is not None else ""
                print(f"  {name}: id={getattr(msg, 'id', getattr(upd, 'id', ''))} media={media} text={str(text)[:30]!r}", flush=True)
            else:
                print(f"  {name}", flush=True)
        await asyncio.sleep(2)
    print("мой id:", me.id, flush=True)
    await client.disconnect()


asyncio.run(main())
