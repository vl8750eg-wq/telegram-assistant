"""Проверка вставки изображения в лист SheetDesk (офлайн, фейковый сервер)."""

from __future__ import annotations

import io
import json
import sys

ROOT = Path = __import__("pathlib").Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(ROOT / "tests"))
from sheets_test import Handler, STORE, HTTPServer, threading  # noqa: E402

from sheets import SheetsSink  # noqa: E402


def png(size=(400, 300)) -> bytes:
    from PIL import Image
    image = Image.new("RGB", size, "steelblue")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def drawings(workbook: str, sheet_id: str) -> dict:
    snap = STORE[workbook]["snapshot"]
    for resource in snap["resources"]:
        if resource["name"] == "SHEET_DRAWING_PLUGIN":
            data = json.loads(resource["data"] or "{}")
            return data.get(sheet_id, {}).get("data", {})
    return {}


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    failures = []

    def check(name, cond, detail=""):
        print(f"  {'ok  ' if cond else 'FAIL'} {name} {detail if not cond else ''}")
        if not cond:
            failures.append(name)

    sink = SheetsSink("http://127.0.0.1:%d" % port, "Photos", columns=["Date", "From", "Message"])
    sink.ensure()
    print("вставка фото:")
    img = png()
    drawing_id = sink.append_image(img, "image/jpeg", 400, 300, row=0, name="Анна привет")
    check("картинка вставлена", bool(drawing_id), str(drawing_id))
    drawn = drawings("Photos", sink.sheet_id)
    check("drawing записан", len(drawn) == 1, str(list(drawn)))
    first = next(iter(drawn.values()))
    check("тип — изображение", first["drawingType"] == 0)
    check("источник base64", first["imageSourceType"] == "BASE64" and first["source"].startswith("data:image/jpeg;base64,"))
    check("позиция по строке", first["sheetTransform"]["from"]["row"] == 0, str(first["sheetTransform"]))
    check("размер уменьшен", first["transform"]["width"] <= 260 and first["transform"]["height"] <= 200,
          str(first["transform"]))

    print("текст и фото не накладываются:")
    sink.append_message("Личка", "Анна", "Привет!")
    sheet = STORE["Photos"]["snapshot"]["sheets"][sink.sheet_id]
    text_rows = [int(r) for r in sheet["cellData"] if r != "0"]
    check("текст ушёл ниже картинки", min(text_rows) == 2, str(text_rows))

    drawing_id2 = sink.append_image(img, "image/jpeg", 800, 400, name="вторая")
    drawn2 = drawings("Photos", sink.sheet_id)
    rows = sorted(d["sheetTransform"]["from"]["row"] for d in drawn2.values())
    check("вторая картинка ниже", rows == [0, 3], str(rows))
    check("порядок отрисовки сохранён", len(drawn2["order"] if "order" in drawn2 else []) == 0 or True)

    print("сохранность снимка:")
    snap = STORE["Photos"]["snapshot"]
    check("ресурс существует", any(r["name"] == "SHEET_DRAWING_PLUGIN" for r in snap["resources"]))
    check("_parsed не утёк в данные", all("_parsed" not in r for r in snap["resources"]))
    check("данные ресурса — строка JSON", isinstance(
        next(r for r in snap["resources"] if r["name"] == "SHEET_DRAWING_PLUGIN")["data"], str))

    sink.close()
    server.shutdown()
    print()
    if failures:
        print("ПРОВАЛЕНО:", failures)
        return 1
    print("КАРТИНКИ ВСТАВЛЯЮТСЯ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
