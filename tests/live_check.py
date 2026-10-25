"""Живая проверка связи с Telegram по вашим api_id/api_hash."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import ApiIdInvalidError
from telethon.tl.functions.help import GetNearestDcRequest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import SESSION_PATH, ConfigError, load_config  # noqa: E402


async def main() -> int:
    try:
        cfg = load_config(require_llm=False)
    except ConfigError as exc:
        print(f"Конфигурация: {exc}")
        return 2

    print(f"api_id: {cfg.api_id}")
    print(f"api_hash: {cfg.api_hash[:6]}…{cfg.api_hash[-4:]} ({len(cfg.api_hash)} симв.)")
    print(f"сессия: {SESSION_PATH} (есть: {SESSION_PATH.exists()})")

    client = TelegramClient(str(SESSION_PATH), cfg.api_id, cfg.api_hash, device_model="AI Assistant")
    started = time.perf_counter()
    await client.connect()
    try:
        dc = await client(GetNearestDcRequest())
        rtt = (time.perf_counter() - started) * 1000
        country = getattr(dc, "country", None) or getattr(dc, "country_code", None) or "—"
        print(f"\nСвязь с Telegram: OK — DC {dc.this_dc} ({country}), {rtt:.0f} мс")
        print("api_id/api_hash: приняты сервером")

        authorized = await client.is_user_authorized()
        if authorized:
            me = await client.get_me()
            print(f"Авторизация: есть — {me.first_name} @{me.username} (id {me.id})")
        else:
            print("Авторизация: нет — запустите python login.py")
        return 0 if authorized else 1
    except ApiIdInvalidError:
        print("\napi_id/api_hash отклонены Telegram. Проверьте значения на my.telegram.org.")
        return 2
    finally:
        await client.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
