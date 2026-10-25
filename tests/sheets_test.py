"""Офлайн-тесты записи в таблицу SheetDesk через фейковый сервер."""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sheets import SheetsSink  # noqa: E402

STORE: dict[str, dict] = {}
HEADERS: list[dict] = []
FAIL_PUTS = {"n": 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _path_parts(self) -> list[str]:
        raw = self.path or "/"
        if raw.startswith("http://"):
            raw = "/" + raw.split("/", 3)[3]
        return [p for p in raw.split("?")[0].split("/") if p]

    def do_GET(self):
        parts = self._path_parts()
        if parts[:2] == ["api", "workbooks"] and len(parts) == 3:
            record = STORE.get(parts[2])
            if record is None:
                self._send(404, {"error": "not found"})
            else:
                self._send(200, {"workbook": record})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        body = self._body()
        parts = self._path_parts()
        if parts == ["api", "workbooks"]:
            record = {"id": body["id"], "name": body.get("name"), "snapshot": body.get("snapshot"),
                      "createdAt": "x", "updatedAt": "x"}
            STORE[body["id"]] = json.loads(json.dumps(record))  # копия, как настоящий сервер
            self._send(201, {"workbook": record})
            return
        self._send(404, {"error": "not found"})

    def do_PUT(self):
        body = self._body()
        parts = self._path_parts()
        if len(parts) == 3 and parts[1] == "workbooks":
            if FAIL_PUTS["n"] > 0:
                FAIL_PUTS["n"] -= 1
                self._send(500, {"error": "boom"})
                return
            if parts[2] not in STORE:
                self._send(404, {"error": "not found"})
                return
            STORE[parts[2]]["snapshot"] = json.loads(json.dumps(body["snapshot"]))
            self._send(200, {"workbook": json.loads(json.dumps(STORE[parts[2]]))})
            return
        self._send(404, {"error": "not found"})


def rows_of(workbook: str) -> list[list[str]]:
    sheets = STORE[workbook]["snapshot"].get("sheets") or {}
    if not sheets:
        return []
    sheet = next(iter(sheets.values()))
    result = []
    for row_key in sorted(sheet["cellData"], key=int):
        cells = sheet["cellData"][row_key]
        result.append([str(cells[str(i)]["v"]) if str(i) in cells else "" for i in range(6)])
    return result


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()

    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if cond else 'FAIL'} {name} {detail if not cond else ''}")
        if not cond:
            failures.append(name)

    url = "http://127.0.0.1:{}".format(port)
    columns = ["Date", "From", "Id", "Username", "Message", "Reply"]

    print("создание таблицы:")
    sink = SheetsSink(url, "TelegramChannel", columns=columns)
    check("таблица создана", sink.ensure())
    check("шапка записана", rows_of("TelegramChannel")[0][:3] == ["Date", "From", "Id"],
          rows_of("TelegramChannel")[0])

    print("добавление сообщений:")
    ok1 = sink.append_message("Личка", "Анна", "Привет! Чем занимаешься?", reply="Всё отлично, работаю ✌")
    ok2 = sink.append_message("Личка", "Пётр", "Есть минутка?", reply="Позже отвечу")
    rows = rows_of("TelegramChannel")
    check("обе строки записаны", ok1 and ok2 and len(rows) == 3, f"строк: {len(rows)}")
    check("порядок колонок соблюдён", rows[1][1] == "Анна" and rows[1][4].startswith("Привет"), rows[1])
    check("ответ в своей колонке", rows[1][5].startswith("Всё отлично"), rows[1])
    check("данные дописываются вниз", rows[2][1] == "Пётр", rows[2])
    check("дата заполнена", len(rows[1][0]) >= 10, rows[1][0])

    print("идемпотентность и сбои:")
    check("повторный ensure не дублирует шапку", sink.ensure() and len(rows_of("TelegramChannel")) == 3)
    FAIL_PUTS["n"] = 1
    ok3 = sink.append_message("Личка", "Мария", "тест ретрая", reply="ответ")
    check("ретрай после 500", ok3 and len(rows_of("TelegramChannel")) == 4)

    print("переживает перезапуск:")
    sink2 = SheetsSink(url, "TelegramChannel", columns=columns)
    check("новая таблица не создаётся", sink2.ensure() and len(rows_of("TelegramChannel")) == 4)
    check("дописывает к старым", sink2.append_message("Избранное", "владелец", "привет") and len(rows_of("TelegramChannel")) == 5)

    print("неизвестная колонка не ломает запись:")
    sink3 = SheetsSink(url, "Other", columns=["Message", "Неизвестно", "From"])
    sink3.ensure()
    sink3.append_message("Личка", "Олег", "текст")
    check("строка записана", len(rows_of("Other")) == 2)

    for s in (sink, sink2, sink3):
        s.close()
    server.shutdown()
    print()
    if failures:
        print(f"ПРОВАЛЕНО: {failures}")
        return 1
    print("ЗАПИСЬ В ТАБЛИЦУ РАБОТАЕТ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
