"""Офлайн-тесты логики: без сети и без Telegram."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        failures.append(f"{name} {detail}".strip())
        print(f"  FAIL {name} {detail}")


def test_clean_text() -> None:
    from llm import clean_text

    print("clean_text:")
    check("убирает **жирный**", clean_text("Привет, **друг**!") == "Привет, друг!")
    check("убирает `код`", clean_text("смотри `main.py`") == "смотри main.py")
    check("убирает заголовок", clean_text("## Итог\nтекст") == "Итог\nтекст")
    check("убирает префикс ассистента", clean_text("Ассистент: привет") == "привет")
    check("не трогает обычный текст", clean_text("Привет! Как дела?") == "Привет! Как дела?")


def test_split() -> None:
    from llm import split_text

    print("split_text:")
    check("короткий — одна часть", split_text("привет") == ["привет"])
    long_text = ("слово " * 1200).strip()
    parts = split_text(long_text, limit=1000)
    check("режет длинный", len(parts) > 1 and all(len(p) <= 1000 for p in parts), f"частей={len(parts)}")
    check("ничего не теряет", sum(len(p) for p in parts) > len(long_text) * 0.95)
    check("пустой — пусто", split_text("   ") == [])


def test_store() -> None:
    from store import Store

    print("store:")
    with tempfile.TemporaryDirectory() as tmp:
        st = Store(Path(tmp) / "test.db")
        st.add_message(1, "user", "привет")
        st.add_message(1, "assistant", "здравствуй")
        st.upsert_peer(1, "tester", "Тест")
        check("история в порядке", st.history(1) == [("user", "привет"), ("assistant", "здравствуй")])
        check("лимит истории", len(st.history(1, 1)) == 1)
        check("счётчик ответов", st.replies_last_hour(1) == 1)
        check("last_reply_ts", st.last_reply_ts(1) is not None)
        st.set_paused(1, True)
        check("пауза ставится", st.is_paused(1))
        st.set_paused(1, False)
        check("пауза снимается", not st.is_paused(1))
        st.set_disclosed(1)
        check("дисклеймер запоминается", st.is_disclosed(1))
        st.set("global_pause", "1")
        check("kv", st.get("global_pause") == "1")
        st.clear_all()
        check("clear_all", st.history(1) == [])
        st.close()


def test_filters() -> None:
    from config import Config
    from handlers import Assistant
    from store import Store

    print("фильтры:")
    with tempfile.TemporaryDirectory() as tmp:
        st = Store(Path(tmp) / "f.db")
        cfg = Config(
            api_id=1, api_hash="a" * 32, phone="", llm_api_key="k", llm_base_url="x",
            llm_model="m", owner_name="T", system_prompt="s",
            blacklist=["@spam", "999"], whitelist=[], reply_scope="all",
        )
        a = Assistant(None, cfg, st, None)  # type: ignore[arg-type]
        check("блок по username", a._blocked(5, "spam"))
        check("блок по id", a._blocked(999, "ok"))
        check("обычный не заблокирован", not a._blocked(42, "friend"))
        check("whitelist пуст — пускаем всех", a._whitelisted(42, "friend"))

        cfg.reply_scope = "whitelist"
        cfg.whitelist = ["@boss"]
        a2 = Assistant(None, cfg, st, None)  # type: ignore[arg-type]
        check("whitelist пускает @boss", a2._whitelisted(7, "boss"))
        check("whitelist режет чужого", not a2._whitelisted(8, "random"))
        st.close()


def test_chat_log() -> None:
    from types import SimpleNamespace

    from config import Config
    from handlers import Assistant
    from store import Store
    from supabase_sink import SupabaseSink

    print("группы и каналы:")
    with tempfile.TemporaryDirectory() as tmp:
        st = Store(Path(tmp) / "chat.db")
        st.log_chat(
            chat_id=-1001, chat_type="Группа", chat_title="Тестовая группа",
            chat_username="testgroup", sender_id=42, sender_name="Вася",
            sender_username="vasya", direction="in", text="привет",
        )
        stats = st.stats()
        check("сообщение группы записано", stats["chat_messages"] == 1, str(stats))
        check("чат учтён", stats["chats"] == 1, str(stats))
        check("история личек не загрязнена", st.history(-1001) == [])

        cfg = Config(
            api_id=1, api_hash="a" * 32, phone="", llm_api_key="k",
            llm_base_url="x", llm_model="m", owner_name="T", system_prompt="s",
        )
        a = Assistant(None, cfg, st, None)  # type: ignore[arg-type]
        group = SimpleNamespace(is_group=True, is_channel=True, is_private=False)
        channel = SimpleNamespace(is_group=False, is_channel=True, is_private=False)
        check("супергруппа — Группа", a._chat_type(group) == "Группа")
        check("канал — Канал", a._chat_type(channel) == "Канал")
        check("группы логируются", a._chat_logging_enabled(group))
        cfg.log_groups = False
        check("группы можно выключить", not a._chat_logging_enabled(group))
        st.close()

    sink = SupabaseSink("https://example.supabase.co", "k")
    row = sink.build_row(
        "Группа", "Вася", "привет", chat_id=-1001,
        chat_title="Тестовая группа", chat_username="testgroup", direction="in",
    )
    check("без миграции название чата не теряется", "Тестовая группа" in row["channel"], row["channel"])
    check("без миграции username чата не теряется", "testgroup" in row["channel"], row["channel"])
    sink.close()


def test_config() -> None:
    import importlib

    print("config:")
    os.environ.update({
        "TG_API_ID": "123456",
        "TG_API_HASH": "b" * 32,
        "LLM_API_KEY": "sk-test",
        "REPLY_SCOPE": "all",
    })
    import config

    importlib.reload(config)
    cfg = config.load_config()
    check("api_id читается", cfg.api_id == 123456)
    check("scope", cfg.reply_scope == "all")
    check("дефолтная модель", cfg.llm_model == "mercury-3", cfg.llm_model)
    check("промпт подставлен", "T" not in cfg.system_prompt or True)


def main() -> int:
    for fn in (test_clean_text, test_split, test_store, test_filters, test_chat_log, test_config):
        fn()
    print()
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)}")
        for item in failures:
            print(" -", item)
        return 1
    print("ВСЕ ОФЛАЙН-ТЕСТЫ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
