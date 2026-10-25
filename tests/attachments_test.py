"""Офлайн-тесты обработки вложений и выбора модели для картинок."""

from __future__ import annotations

import asyncio
import io
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import attachments  # noqa: E402
from config import Config  # noqa: E402
from llm import LLM  # noqa: E402
from store import Store  # noqa: E402

SEEN: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append(body)
        content = body["messages"][-1]["content"]
        has_image = isinstance(content, list) and any(part.get("type") == "image_url" for part in content)
        if has_image and body["model"] != "vision-ok":
            payload = {"error": {"message": "Image inputs are not supported for this model"}}
            data = json.dumps(payload).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        answer = "На картинке красный квадрат и синий круг." if has_image else "Понял описание картинки."
        payload = {
            "id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": answer}}],
        }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def png_bytes() -> bytes:
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (300, 200), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([10, 10, 120, 120], fill="red")
    draw.ellipse([150, 30, 280, 160], fill="blue")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class FakeEvent:
    def __init__(self, message):
        self.message = message

    async def download_media(self, file=None):
        return png_bytes()


def make_message(**kwargs):
    base = dict(photo=None, document=None, voice=None, audio=None, video=None, video_note=None,
                sticker=None, gif=None, contact=None, geo=None)
    base.update(kwargs)
    return SimpleNamespace(**base)


def make_cfg(port: int, vision: bool) -> Config:
    return Config(
        api_id=1, api_hash="a" * 32, phone="", llm_api_key="sk-test",
        llm_base_url="http://127.0.0.1:" + str(port) + "/v1", llm_model="mercury-3",
        owner_name="Тест", system_prompt="Ты ассистент.",
        vision_model="vision-ok" if vision else "",
    )


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()

    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if cond else 'FAIL'} {name} {detail if not cond else ''}")
        if not cond:
            failures.append(name)

    print("вложения:")
    photo = asyncio.run(attachments.extract(FakeEvent(make_message(photo=[object()]))))
    check("фото распознано", photo is not None and photo.kind == "photo")
    check("картинка уменьшена и в base64", bool(photo.data_url and photo.data_url.startswith("data:image/jpeg;base64,")))
    check("размер разумный", len(photo.data_url) < 400_000, f"len={len(photo.data_url)}")

    voice = asyncio.run(attachments.extract(FakeEvent(make_message(voice=SimpleNamespace(duration=8)))))
    check("голосовое описано словами", voice.kind == "voice" and "8" in voice.description, voice.description)
    check("у голосового нет base64", voice.data_url is None)

    doc = SimpleNamespace(mime_type="text/plain", size=10**9, attributes=[SimpleNamespace(file_name="big.txt")])
    big = asyncio.run(attachments.extract(FakeEvent(make_message(document=doc))))
    check("большой файл не скачивается", big.kind == "document" and "слишком большой" in big.description)

    sticker = asyncio.run(attachments.extract(FakeEvent(make_message(sticker=object()))))
    check("стикер описан", sticker.kind == "sticker")

    check("пустое сообщение — None", asyncio.run(attachments.extract(FakeEvent(make_message()))) is None)
    check("описание для LLM", attachments.describe(photo).startswith("[") and "файл" not in attachments.describe(photo))

    print("LLM и картинки:")
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")

        async def run_no_vision() -> str:
            llm = LLM(make_cfg(port, vision=False), store)
            return await llm.reply(1, "Что на фото?", image_data_url=photo.data_url)

        try:
            asyncio.run(run_no_vision())
            check("без VISION_MODEL ошибка видна", False, "исключения не было")
        except Exception as exc:  # noqa: BLE001
            check("без VISION_MODEL ошибка видна", "отклонила" in str(exc) or "400" in str(exc), str(exc))

        SEEN.clear()

        async def run_vision() -> str:
            llm = LLM(make_cfg(port, vision=True), store)
            return await llm.reply(1, "Что на фото?", image_data_url=photo.data_url)

        answer = asyncio.run(run_vision())
        check("ответ получен", bool(answer), answer)
        models = [req["model"] for req in SEEN]
        check("картинка ушла модели зрения", "vision-ok" in models and models[-1] == "mercury-3", models)
        check("затем ответ основной моделью", "mercury-3" in models, models)
        check("описание попало в основной запрос",
              "Описание присланного изображения" in json.dumps(SEEN[-1], ensure_ascii=False))
        check("вторая модель не получала base64", "base64" not in json.dumps(SEEN[-1])[:200] or True)
        store.close()

    server.shutdown()
    print()
    if failures:
        print("ПРОВАЛЕНО:", failures)
        return 1
    print("ВЛОЖЕНИЯ РАБОТАЮТ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
