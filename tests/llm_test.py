"""Офлайн-тест LLM-слоя на локальном фейковом OpenAI-совместимом сервере."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from llm import LLM  # noqa: E402
from store import Store  # noqa: E402

REQUESTS: list[dict] = []
fail_first = {"n": 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # тише
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        REQUESTS.append(body)

        if fail_first["n"] > 0:
            fail_first["n"] -= 1
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": {"message": "boom"}}')
            return

        answer = "Привет! **Всё** хорошо, чем могу помочь?"
        payload = {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 0,
            "model": body.get("model", "fake"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": answer}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_cfg(port: int) -> Config:
    return Config(
        api_id=1, api_hash="a" * 32, phone="", llm_api_key="sk-test",
        llm_base_url=f"http://127.0.0.1:{port}/v1", llm_model="fake-model",
        owner_name="Тест", system_prompt="Ты ассистент Теста.",
    )


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()

    failures = []

    def check(name, cond, detail=""):
        print(f"  {'ok  ' if cond else 'FAIL'} {name} {detail if not cond else ''}")
        if not cond:
            failures.append(name)

    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            llm = LLM(make_cfg(port), store)

            store.add_message(1, "user", "Привет")
            store.add_message(1, "assistant", "Здравствуй!")
            store.add_message(1, "user", "Как дела?")

            answer = await llm.reply(1, "Как дела?")
            check("ответ получен", bool(answer), answer)
            check("markdown вычищен", "**" not in answer, answer)
            req = REQUESTS[-1]
            check("system-промпт первым", req["messages"][0]["role"] == "system", req["messages"][0])
            check("история передана", len(req["messages"]) == 4, f"{len(req['messages'])} сообщений")
            check("нет пары дублей", [m["content"] for m in req["messages"]].count("Как дела?") == 1)
            check("роль assistant сохранена", req["messages"][2]["role"] == "assistant")

            store.set("prompt_override", "Новый промпт")
            await llm.reply(1, "ещё раз")
            check("override применяется", REQUESTS[-1]["messages"][0]["content"].startswith("Новый промпт"))

            fail_first["n"] = 2
            before = len(REQUESTS)
            retried = await asyncio.wait_for(llm.reply(1, "тест ретраев"), timeout=30)
            check("ретрай после 500", bool(retried) and len(REQUESTS) - before == 3,
                  f"попыток: {len(REQUESTS) - before}")
            store.close()

    asyncio.run(run())
    server.shutdown()
    print()
    if failures:
        print(f"ПРОВАЛЕНО: {failures}")
        return 1
    print("LLM-СЛОЙ РАБОТАЕТ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
