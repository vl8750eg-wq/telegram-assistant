"""Конфигурация ассистента: читает .env и проверяет значения."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ENV_PATH = BASE_DIR / ".env"
SESSION_PATH = DATA_DIR / "assistant.session"
DB_PATH = DATA_DIR / "assistant.db"
LOG_PATH = DATA_DIR / "assistant.log"

DEFAULT_SYSTEM_PROMPT = (
    "Ты — личный ассистент {owner} в Telegram. Ты отвечаешь на сообщения от его имени, "
    "как живой человек: коротко, по делу, в его обычном стиле — дружелюбно и без канцелярита.\n"
    "Правила:\n"
    "- Отвечай по-русски, если собеседник пишет по-русски; иначе на его языке.\n"
    "- 1–3 коротких абзаца, без списков и markdown, как в обычной переписке.\n"
    "- Не выдумывай факты о {owner}, которых не знаешь. Если не уверен — скажи, что уточнишь позже.\n"
    "- Не обещай встреч, денег и договорённостей от имени {owner} без его подтверждения.\n"
    "- Если вопрос требует самого {owner} (деньги, работа, личное) — мягко предложи написать позже."
)


class ConfigError(Exception):
    """Некорректная или неполная конфигурация."""


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or "").strip() or default


def _bool(name: str, default: bool) -> bool:
    raw = _env(name, "").lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "да"}


def _int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должно быть целым числом, получено: {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = _env(name, "").replace(",", ".")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должно быть числом, получено: {raw!r}") from exc


def _list(name: str) -> List[str]:
    raw = _env(name)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class Config:
    api_id: int
    api_hash: str
    phone: str

    llm_api_key: str
    llm_base_url: str
    llm_model: str

    owner_name: str
    system_prompt: str

    reply_scope: str = "all"          # all | whitelist
    whitelist: List[str] = field(default_factory=list)
    blacklist: List[str] = field(default_factory=list)

    disclose_ai: bool = True
    mark_read: bool = True

    debounce_seconds: float = 2.5
    min_delay: float = 1.5
    max_delay: float = 5.0
    history_limit: int = 20
    max_replies_per_hour: int = 40
    min_gap_seconds: int = 20
    owner_takeover_minutes: int = 30
    self_chat_mode: bool = True
    request_timeout: int = 60
    log_level: str = "INFO"

    # --- таблица SheetDesk ---
    sheet_url: str = ""
    sheet_workbook: str = ""
    sheet_name: str = ""
    sheet_columns: List[str] = field(default_factory=list)
    sheet_style: str = ""
    sheet_write_replies: bool = True
    sheet_log_owner: bool = True

    # --- логирование групп и каналов ---
    log_groups: bool = True
    log_channels: bool = True
    log_bots: bool = True

    # --- вложения ---
    save_media: bool = False
    vision_enabled: bool = True
    vision_model: str = ""
    vision_base_url: str = ""
    vision_api_key: str = ""

    # --- база данных Supabase (supabase.com) ---
    supabase_url: str = ""
    supabase_key: str = ""
    supabase_table: str = "messages"
    supabase_bucket: str = "messages"
    supabase_write_replies: bool = True
    supabase_log_owner: bool = True
    supabase_journal: bool = True
    supabase_journal_table: str = "journal"

    def validate(self, require_llm: bool = True) -> None:
        if not self.api_id:
            raise ConfigError("Не задан TG_API_ID. Возьмите его на my.telegram.org → API development tools.")
        if len(self.api_hash) != 32:
            raise ConfigError("TG_API_HASH должен быть 32 символа. Проверьте значение из my.telegram.org.")
        if require_llm and not self.llm_api_key:
            raise ConfigError("Не задан LLM_API_KEY. Впишите ключ DeepSeek/OpenAI в .env или запустите setup.py.")
        if self.reply_scope not in {"all", "whitelist"}:
            raise ConfigError("REPLY_SCOPE может быть только 'all' или 'whitelist'.")
        if self.history_limit < 2:
            raise ConfigError("HISTORY_LIMIT должен быть >= 2.")
        if self.debounce_seconds < 0 or self.min_delay < 0 or self.max_delay < self.min_delay:
            raise ConfigError("Проверьте DEBOUNCE_SECONDS, MIN_DELAY, MAX_DELAY: задержки не могут быть отрицательными.")

    @property
    def vision_ready(self) -> bool:
        """Есть ли отдельная модель с поддержкой изображений."""
        return self.vision_enabled and bool(self.vision_model)


def load_config(require_llm: bool = True) -> Config:
    load_dotenv(ENV_PATH, override=False)

    owner = _env("OWNER_NAME", "владельца аккаунта")
    prompt = _env("SYSTEM_PROMPT") or DEFAULT_SYSTEM_PROMPT
    prompt = prompt.replace("{owner}", owner)

    cfg = Config(
        api_id=_int("TG_API_ID", 0),
        api_hash=_env("TG_API_HASH"),
        phone=_env("TG_PHONE"),
        llm_api_key=_env("LLM_API_KEY"),
        llm_base_url=_env("LLM_BASE_URL", "https://api.deepseek.com"),
        llm_model=_env("LLM_MODEL", "deepseek-chat"),
        owner_name=owner,
        system_prompt=prompt,
        reply_scope=_env("REPLY_SCOPE", "all").lower(),
        whitelist=_list("WHITELIST"),
        blacklist=_list("BLACKLIST"),
        disclose_ai=_bool("DISCLOSE_AI", True),
        mark_read=_bool("MARK_READ", True),
        debounce_seconds=_float("DEBOUNCE_SECONDS", 2.5),
        min_delay=_float("MIN_DELAY", 1.5),
        max_delay=_float("MAX_DELAY", 5.0),
        history_limit=_int("HISTORY_LIMIT", 20),
        max_replies_per_hour=_int("MAX_REPLIES_PER_HOUR", 40),
        min_gap_seconds=_int("MIN_GAP_SECONDS", 20),
        owner_takeover_minutes=_int("OWNER_TAKEOVER_MINUTES", 30),
        self_chat_mode=_bool("SELF_CHAT_MODE", True),
        request_timeout=_int("LLM_TIMEOUT", 60),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        sheet_url=_env("SHEET_URL"),
        sheet_workbook=_env("SHEET_WORKBOOK"),
        sheet_name=_env("SHEET_NAME"),
        sheet_columns=_list("SHEET_COLUMNS"),
        sheet_style=_env("SHEET_STYLE"),
        sheet_write_replies=_bool("SHEET_WRITE_REPLIES", True),
        sheet_log_owner=_bool("SHEET_LOG_OWNER", True),
        log_groups=_bool("LOG_GROUPS", True),
        log_channels=_bool("LOG_CHANNELS", True),
        log_bots=_bool("LOG_BOTS", True),
        save_media=_bool("SAVE_MEDIA", False),
        vision_enabled=_bool("VISION_ENABLED", True),
        vision_model=_env("VISION_MODEL"),
        vision_base_url=_env("VISION_BASE_URL"),
        vision_api_key=_env("VISION_API_KEY"),
        supabase_url=_env("SUPABASE_URL"),
        supabase_key=_env("SUPABASE_KEY"),
        supabase_table=_env("SUPABASE_TABLE", "messages"),
        supabase_bucket=_env("SUPABASE_BUCKET", "messages"),
        supabase_write_replies=_bool("SUPABASE_WRITE_REPLIES", True),
        supabase_log_owner=_bool("SUPABASE_LOG_OWNER", True),
        supabase_journal=_bool("SUPABASE_JOURNAL", True),
        supabase_journal_table=_env("SUPABASE_JOURNAL_TABLE", "journal"),
    )
    cfg.validate(require_llm=require_llm)
    return cfg
