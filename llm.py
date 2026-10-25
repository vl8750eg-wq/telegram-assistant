"""LLM-слой: запрос к OpenAI-совместимому API (по умолчанию DeepSeek) + чистка текста."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Optional

from openai import AsyncOpenAI

from config import Config
from store import Store

log = logging.getLogger(__name__)

MAX_REPLY_TOKENS = 400
TELEGRAM_LIMIT = 3800


class LLMError(Exception):
    pass


def clean_text(text: str) -> str:
    """Убирает markdown-разметку, чтобы сообщение выглядело как обычный текст."""
    text = (text or "").strip()
    text = re.sub(r"^\s*(ассистент|assistant|бот)\s*:\s*", "", text, flags=re.IGNORECASE)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"(?m)^#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s*[-*+]\s+", "— ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_text(text: str, limit: int = TELEGRAM_LIMIT) -> List[str]:
    """Режет длинный текст на части по абзацам/предложениям, не разрывая слова."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []

    parts: List[str] = []
    rest = text
    while rest:
        if len(rest) <= limit:
            parts.append(rest.strip())
            break
        window = rest[:limit]
        cut = max(window.rfind("\n\n"), window.rfind(". "), window.rfind("! "), window.rfind("? "))
        if cut < limit * 0.5:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut].strip())
        rest = rest[cut:].lstrip()
    return [p for p in parts if p]


class LLM:
    def __init__(self, cfg: Config, store: Store) -> None:
        self.cfg = cfg
        self.store = store
        self._client = AsyncOpenAI(
            api_key=cfg.llm_api_key,
            base_url=cfg.llm_base_url,
            timeout=cfg.request_timeout,
            max_retries=0,
        )

    async def reply(self, peer_id: int, user_text: str, extra_system: str = "",
                    image_data_url: Optional[str] = None) -> str:
        try:
            return await self._call(peer_id, user_text, extra_system, image_data_url)
        except LLMError as exc:
            if not image_data_url or not self.cfg.vision_ready:
                raise
            log.warning("Основная модель не приняла картинку (%s) — перехожу на модель с изображениями", exc)
            return await self._describe_image(peer_id, user_text, image_data_url, extra_system)

    async def _call(self, peer_id: int, user_text: str, extra_system: str,
                    image_data_url: Optional[str], model: Optional[str] = None,
                    client=None) -> str:
        prompt = self.store.get("prompt_override") or self.cfg.system_prompt
        messages = [{"role": "system", "content": prompt + extra_system}]

        history = self.store.history(peer_id, self.cfg.history_limit * 2)
        if history and history[-1] == ("user", user_text):
            history = history[:-1]  # текущее сообщение добавим ниже, без дубля
        for role, text in history:
            messages.append({"role": "user" if role in {"user", "owner"} else "assistant", "content": text})

        if image_data_url:
            current = {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                ],
            }
        else:
            current = {"role": "user", "content": user_text}
        messages.append(current)

        client = client or self._client
        model = model or self.cfg.llm_model
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.7,
                    max_tokens=MAX_REPLY_TOKENS,
                )
                content = (response.choices[0].message.content or "").strip()
                if not content:
                    raise LLMError("Пустой ответ от модели")
                answer = clean_text(content)
                log.info("LLM ok: peer=%s model=%s attempt=%s len=%s", peer_id, model, attempt, len(answer))
                return answer
            except Exception as exc:  # noqa: BLE001 — нужен общий перехват для ретраев
                last_error = exc
                status = getattr(exc, "status_code", None)
                log.warning("LLM error (attempt %s/3): %s %s", attempt, type(exc).__name__, exc)
                if status in (401, 403, 404):
                    raise LLMError(f"Ошибка авторизации/модели: {exc}") from exc
                if status == 400 or "image" in str(exc).lower():
                    raise LLMError(f"Модель отклонила запрос: {exc}") from exc
                if attempt < 3:
                    await asyncio.sleep(attempt * 2)

        raise LLMError(f"Не удалось получить ответ: {last_error}")

    async def _describe_image(self, peer_id: int, user_text: str, image_data_url: str,
                              extra_system: str = "") -> str:
        """Описание картинки отдельной моделью + ответ основной моделью по описанию."""
        vision_client = AsyncOpenAI(
            api_key=self.cfg.vision_api_key or self.cfg.llm_api_key,
            base_url=self.cfg.vision_base_url or self.cfg.llm_base_url,
            timeout=self.cfg.request_timeout,
            max_retries=0,
        )
        description = await self._call(
            peer_id,
            user_text,
            "\n\nОпиши подробно, что на изображении: люди, предметы, текст, интерфейс, настроение, "
            "важные детали. Без выдумок.",
            image_data_url,
            model=self.cfg.vision_model,
            client=vision_client,
        )
        log.info("Изображение описано моделью %s (%s симв.)", self.cfg.vision_model, len(description))
        enhanced = f"{user_text}\n\n[Описание присланного изображения]\n{description}"
        return await self._call(peer_id, enhanced, extra_system, None)
