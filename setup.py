"""Мастер настройки: собирает .env по ответам в консоли."""

from __future__ import annotations

import sys
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent / ".env"

SECTIONS = [
    ("Telegram (my.telegram.org → API development tools)", ["TG_API_ID", "TG_API_HASH", "TG_PHONE"]),
    ("LLM", ["LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"]),
    ("Личность", ["OWNER_NAME"]),
    ("Кому отвечаем", ["REPLY_SCOPE", "WHITELIST", "BLACKLIST"]),
    ("Поведение", ["DISCLOSE_AI", "SELF_CHAT_MODE", "MAX_REPLIES_PER_HOUR", "MIN_GAP_SECONDS", "OWNER_TAKEOVER_MINUTES"]),
]

HINTS = {
    "TG_API_ID": "api_id (число)",
    "TG_API_HASH": "api_hash (32 символа)",
    "TG_PHONE": "номер телефона, +79991234567 (можно оставить пустым)",
    "LLM_API_KEY": "ключ DeepSeek/OpenAI",
    "LLM_BASE_URL": "адрес API",
    "LLM_MODEL": "модель",
    "OWNER_NAME": "ваше имя для ассистента",
    "REPLY_SCOPE": "all (всем) или whitelist (только из списка)",
    "WHITELIST": "через запятую: @user,123456",
    "BLACKLIST": "через запятую: @spam,123456",
    "DISCLOSE_AI": "true — предупреждать, что отвечает ИИ",
    "SELF_CHAT_MODE": "true — отвечать в «Избранном»",
    "MAX_REPLIES_PER_HOUR": "лимит ответов в час",
    "MIN_GAP_SECONDS": "минимальный интервал между ответами одному человеку",
    "OWNER_TAKEOVER_MINUTES": "сколько молчать после вашего ответа вручную",
}


def parse_env(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        data[key.strip()] = value.strip()
    return data


def render(values: dict[str, str]) -> str:
    lines = [
        "# Файл создан setup.py. Секреты не коммитить в git.",
        "",
    ]
    for title, keys in SECTIONS:
        lines.append(f"# --- {title} ---")
        for key in keys:
            lines.append(f"{key}={values.get(key, '')}")
        lines.append("")
    lines += [
        "# --- Тонкая настройка (обычно менять не нужно) ---",
        "DEBOUNCE_SECONDS=2.5",
        "MIN_DELAY=1.5",
        "MAX_DELAY=5.0",
        "HISTORY_LIMIT=20",
        "MARK_READ=true",
        "LLM_TIMEOUT=60",
        "LOG_LEVEL=INFO",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    values = parse_env(ENV_PATH)
    print("Настройка ИИ-ассистента. Enter — оставить текущее значение.\n")
    for title, keys in SECTIONS:
        print(f"— {title}")
        for key in keys:
            current = values.get(key, "")
            shown = "***" if key == "LLM_API_KEY" and current else current
            hint = HINTS.get(key, key)
            answer = input(f"  {key} ({hint}) [{shown}]: ").strip()
            if answer:
                values[key] = answer
        print()

    ENV_PATH.write_text(render(values), encoding="utf-8")
    print(f"Сохранено: {ENV_PATH}")
    print("\nДальше: python login.py  (вход), затем python main.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
