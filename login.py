"""Вход в Telegram под своим аккаунтом и сохранение сессии.

Режимы:
  python login.py                 — интерактивно (телефон → код → пароль 2FA)
  python login.py --phone +7999…  — отправить код
  python login.py --code 12345    — ввести код
  python login.py --password xxx  — пароль 2FA
  python login.py --check         — проверить, авторизован ли
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from telethon import TelegramClient, errors
from telethon.tl.functions.help import GetNearestDcRequest

from config import DATA_DIR, SESSION_PATH, ConfigError, load_config

STATE_PATH = DATA_DIR / "login_state.json"


def _hr(title: str) -> None:
    print(f"\n=== {title} ===")


async def interactive(client: TelegramClient) -> int:
    phone = input("Номер телефона в формате +79991234567: ").strip()
    if not phone:
        print("Номер не указан.")
        return 2
    sent = await client.send_code_request(phone)
    STATE_PATH.write_text(json.dumps({"phone": phone, "hash": sent.phone_code_hash}, ensure_ascii=False))
    print("Код отправлен в Telegram (чат «Telegram»).")
    code = input("Код из Telegram: ").strip()
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
    except errors.SessionPasswordNeededError:
        password = input("Пароль 2FA: ").strip()
        await client.sign_in(password=password)
    me = await client.get_me()
    print(f"\nГотово: вошли как {me.first_name} @{me.username} (id {me.id})")
    print(f"Сессия сохранена: {SESSION_PATH}")
    return 0


async def stepwise(client: TelegramClient, args: argparse.Namespace, phone: str) -> int:
    state = {}
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = {}

    if args.phone:
        sent = await client.send_code_request(phone)
        STATE_PATH.write_text(json.dumps({"phone": phone, "hash": sent.phone_code_hash}, ensure_ascii=False))
        print("Код отправлен. Следующий шаг: python login.py --code XXXXX")
        return 0

    if args.code:
        if not state.get("hash"):
            print("Нет сохранённого запроса кода. Сначала: python login.py --phone +7...")
            return 2
        try:
            await client.sign_in(phone=phone, code=args.code, phone_code_hash=state["hash"])
        except errors.SessionPasswordNeededError:
            print("Требуется пароль 2FA. Следующий шаг: python login.py --password ВАШ_ПАРОЛЬ")
            return 3
        except errors.PhoneCodeInvalidError:
            print("Неверный код. Попробуйте снова: python login.py --code XXXXX")
            return 4
        except errors.PhoneCodeExpiredError:
            print("Код истёк. Запросите новый: python login.py --phone " + phone)
            return 4

    if args.password:
        await client.sign_in(password=args.password)

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Готово: вошли как {me.first_name} @{me.username} (id {me.id})")
        try:
            STATE_PATH.unlink()
        except OSError:
            pass
        return 0
    print("Пока не авторизован. Запустите: python login.py")
    return 1


async def main() -> int:
    parser = argparse.ArgumentParser(description="Вход в Telegram для ИИ-ассистента")
    parser.add_argument("--phone")
    parser.add_argument("--code")
    parser.add_argument("--password")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    try:
        cfg = load_config(require_llm=False)
    except ConfigError as exc:
        print(f"Конфигурация: {exc}")
        return 2

    client = TelegramClient(
        str(SESSION_PATH), cfg.api_id, cfg.api_hash, flood_sleep_threshold=120, device_model="AI Assistant"
    )
    await client.connect()
    try:
        dc = await client(GetNearestDcRequest())
        print(f"Связь с Telegram: ok (DC {dc.this_dc}, api_id {cfg.api_id} принят)")

        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"OK: уже авторизован — {me.first_name} @{me.username} (id {me.id})")
            return 0
        if args.check:
            print("Сессия не авторизована. Запустите: python login.py")
            return 1

        phone = args.phone or cfg.phone
        if args.phone or args.code or args.password:
            if not phone:
                saved = json.loads(STATE_PATH.read_text(encoding="utf-8")).get("phone") if STATE_PATH.exists() else None
                phone = saved or ""
            if not phone:
                print("Не указан номер: добавьте --phone +7...")
                return 2
            _hr("Вход по шагам")
            return await stepwise(client, args, phone)

        _hr("Первый вход")
        return await interactive(client)
    except errors.ApiIdInvalidError:
        print("api_id/api_hash неверны — проверьте значения с my.telegram.org.")
        return 2
    except errors.PhoneNumberInvalidError:
        print("Неверный формат номера. Нужен вид +79991234567.")
        return 2
    finally:
        await client.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
