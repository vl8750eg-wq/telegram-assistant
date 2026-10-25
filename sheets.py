"""Отправка сообщений в таблицу SheetDesk (http://127.0.0.1:8787)."""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
import uuid
from typing import List, Optional, Sequence

import httpx

log = logging.getLogger(__name__)

HTTP_TIMEOUT = 10.0
MAX_ROWS = 20_000


def _column_letter(index: int) -> str:
    value = index + 1
    result = ""
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _cell(value) -> dict:
    if isinstance(value, bool):
        return {"v": value, "t": 3}
    if isinstance(value, (int, float)):
        return {"v": value, "t": 2}
    return {"v": "" if value is None else str(value), "t": 1}


class SheetsSink:
    """Дописывает строки в лист таблицы через API SheetDesk."""

    def __init__(self, base_url: str, workbook: str, sheet: Optional[str] = None,
                 columns: Sequence[str] | None = None, style: Optional[str] = None,
                 write_replies: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.workbook = (workbook or "").strip()
        self.sheet = (sheet or "").strip() or None
        self.columns = list(columns or [])
        self.style = style or None
        self.write_replies = write_replies
        self.sheet_id: Optional[str] = None
        self._write_lock = threading.Lock()
        self._ids = ",".join(str(i) for i in range(1, len(self.columns) + 1)) if self.columns else "hdr"
        # trust_env=False: переменные HTTPS_PROXY/NO_PROXY окружения ломают запросы к localhost
        self._client = httpx.Client(base_url=self.base_url, timeout=HTTP_TIMEOUT, trust_env=False)

    # ------------------------------------------------------------- helpers

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.workbook)

    def _url(self, suffix: str = "") -> str:
        from urllib.parse import quote
        return f"/api/workbooks/{quote(self.workbook, safe='')}{suffix}"

    def _get_record(self) -> Optional[dict]:
        response = self._client.get(self._url())
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json().get("workbook")

    def _pick_sheet(self, snapshot: dict) -> dict:
        if not isinstance(snapshot.get("sheets"), dict) or not isinstance(snapshot.get("sheetOrder"), list):
            snapshot["sheets"] = snapshot.get("sheets") if isinstance(snapshot.get("sheets"), dict) else {}
            snapshot["sheetOrder"] = snapshot.get("sheetOrder") if isinstance(snapshot.get("sheetOrder"), list) else []
        sheets = snapshot["sheets"]
        order = snapshot["sheetOrder"] if snapshot["sheetOrder"] else list(sheets.keys())
        ids = [sid for sid in order if sid in sheets]
        sheet_id = ids[0] if ids else "sheet-1"
        if self.sheet:
            needle = self.sheet.strip().lower()
            match = next((sid for sid in ids if sid == self.sheet), None) or next(
                (sid for sid in ids if str(sheets[sid].get("name", "")).strip().lower() == needle), None)
            sheet_id = match or sheet_id
        self.sheet_id = sheet_id
        sheet = sheets.get(sheet_id)
        if not isinstance(sheet, dict):
            sheet = {
                "id": sheet_id, "name": "Сообщения", "tabColor": "", "hidden": 0,
                "rowCount": 5000, "columnCount": max(10, len(self.columns) or 8), "zoomRatio": 1,
                "scrollTop": 0, "scrollLeft": 0, "defaultColumnWidth": 120, "defaultRowHeight": 24,
                "freeze": {"xSplit": 0, "ySplit": 1, "startColumn": -1, "startRow": 0},
                "mergeData": [], "cellData": {}, "rowData": {}, "columnData": {},
                "rowHeader": {"width": 46, "hidden": 0}, "columnHeader": {"height": 24, "hidden": 0},
                "showGridlines": 1, "rightToLeft": 0,
            }
            sheets[sheet_id] = sheet
            snapshot["sheetOrder"].append(sheet_id)
        sheet.setdefault("cellData", {})
        return sheet

    # ------------------------------------------------- изображения в листе

    def _drawings(self, snapshot: dict) -> dict:
        resources = snapshot.setdefault("resources", [])
        for resource in resources:
            if resource.get("name") == "SHEET_DRAWING_PLUGIN":
                try:
                    data = json.loads(resource.get("data") or "{}")
                except json.JSONDecodeError:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                resource["_parsed"] = data
                return data
        data: dict = {}
        resources.append({"name": "SHEET_DRAWING_PLUGIN", "data": "{}", "_parsed": data})
        return data

    @staticmethod
    def _flush_drawings(snapshot: dict) -> None:
        for resource in snapshot.get("resources", []):
            if "_parsed" in resource:
                resource["data"] = json.dumps(resource.pop("_parsed"), ensure_ascii=False)

    def _next_row(self, sheet: dict, snapshot: dict) -> int:
        """Первая свободная строка: учитывает и текст, и картинки, привязанные к строкам."""
        row = self._last_row(sheet)
        drawings = self._drawings(snapshot).get(self.sheet_id or "", {})
        for drawing in (drawings.get("data") or {}).values():
            anchor = drawing.get("sheetTransform") or {}
            end_row = (anchor.get("to") or {}).get("row")
            if isinstance(end_row, int):
                row = max(row, end_row + 1)
        return row

    def append_image(self, image: bytes, mime: str, width: int, height: int,
                     row: Optional[int] = None, column: int = 0,
                     max_width: int = 260, max_height: int = 200, name: str = "") -> Optional[str]:
        """Вставляет картинку поверх листа (drawing), возвращает drawingId или None."""
        if not self.enabled:
            return None
        with self._write_lock:
            return self._append_image_locked(image, mime, width, height, row, column, max_width, max_height, name)

    def _append_image_locked(self, image: bytes, mime: str, width: int, height: int,
                             row: Optional[int], column: int, max_width: int, max_height: int,
                             name: str) -> Optional[str]:
        for attempt in range(1, 3):
            try:
                record = self._get_record()
                if record is None:
                    self.ensure()
                    record = self._get_record()
                if record is None:
                    return None
                snapshot = record.get("snapshot") or self._new_snapshot()
                sheet = self._pick_sheet(snapshot)
                target_row = self._next_row(sheet, snapshot) if row is None else row

                scale = 1.0
                if width and height:
                    scale = min(max_width / width, max_height / height, 1.0)
                    w, h = max(20, int(width * scale)), max(20, int(height * scale))
                else:
                    w, h = max_width, max_height

                left = 46 + column * 100 + 4
                top = 24 + target_row * 24 + 4
                drawing_id = "tg_" + uuid.uuid4().hex[:12]
                source = f"data:{mime};base64,{base64.b64encode(image).decode()}"
                drawing = {
                    "unitId": self.workbook,
                    "subUnitId": self.sheet_id or "sheet-1",
                    "drawingId": drawing_id,
                    "drawingType": 0,
                    "imageSourceType": "BASE64",
                    "source": source,
                    "transform": {"left": left, "top": top, "width": w, "height": h, "angle": 0},
                    "sheetTransform": {
                        "from": {"row": target_row, "column": column, "rowOffset": 4, "columnOffset": 4},
                        "to": {"row": target_row + 1, "column": column + 3, "rowOffset": 0, "columnOffset": 0},
                    },
                    "name": name or "Telegram",
                }
                unit = self._drawings(snapshot).setdefault(self.sheet_id or "sheet-1", {"data": {}, "order": []})
                unit.setdefault("data", {})[drawing_id] = drawing
                unit.setdefault("order", []).append(drawing_id)
                self._flush_drawings(snapshot)
                if row is None:
                    sheet.setdefault("rowData", {}).setdefault(str(target_row), {})["h"] = max(h + 8, 24)
                response = self._client.put(self._url(), json={"snapshot": snapshot})
                response.raise_for_status()
                log.info("Картинка вставлена в лист: строка %s", target_row + 1)
                return drawing_id
            except Exception as exc:  # noqa: BLE001
                log.warning("Картинка не вставлена (попытка %s/2): %s", attempt, exc)
                time.sleep(1.0)
        return None

    @staticmethod
    def _last_row(sheet: dict) -> int:
        rows = [int(r) for r, cells in sheet.get("cellData", {}).items() if cells]
        return max(rows) + 1 if rows else 0

    def _write_cells(self, cell_data: dict, row: int, values: Sequence) -> None:
        cells = {}
        for index, value in enumerate(values):
            if value is None or value == "":
                continue
            cell = _cell(value)
            if self.style:
                cell["s"] = self.style
            cells[index] = cell
        if cells:
            cell_data[str(row)] = cells

    # -------------------------------------------------------------- public

    def _new_snapshot(self) -> dict:
        return {
            "id": self.workbook,
            "sheetOrder": ["sheet-1"],
            "name": "Telegram",
            "appVersion": "1.2.0",
            "locale": "ruRU",
            "styles": {},
            "sheets": {},
            "resources": [],
            "calculationSettings": {"iterativeCalculation": False, "maxIterations": 100, "maxChange": 0.001},
        }

    def _conflict(self) -> bool:
        try:
            other = self._client.get("/api/workbooks").json().get("workbooks") or []
        except Exception:  # noqa: BLE001
            return False
        mine = next((w for w in other if w.get("id") == self.workbook), None)
        if not mine:
            return False
        duplicates = [w for w in other if w.get("id") != self.workbook
                      and str(w.get("name", "")).strip() == str(mine.get("name", "")).strip()]
        return bool(duplicates)

    def ensure(self) -> bool:
        """Создаёт таблицу/лист/шапку, если их нет. True — найдена или создана."""
        if not self.enabled:
            return False
        try:
            record = self._get_record()
            if record is None:
                if self._conflict():
                    log.error("Таблица «%s»: указано имя, а в списке есть несколько таблиц с таким именем. "
                              "Укажите в SHEET_WORKBOOK id (uuid) нужной таблицы.", self.workbook)
                    return False
                snapshot = self._new_snapshot()
                sheet = self._pick_sheet(snapshot)
                if self.columns:
                    self._write_cells(sheet["cellData"], 0, self.columns)
                else:
                    self._write_cells(sheet["cellData"], 0, ["Канал", "Дата", "От", "Сообщение", "Ответ"])
                response = self._client.post("/api/workbooks", json={"id": self.workbook, "name": self.workbook, "snapshot": snapshot})
                response.raise_for_status()
                log.info("Таблица создана: %s", self.workbook)
                return True

            snapshot = record.get("snapshot") or self._new_snapshot()
            sheet = self._pick_sheet(snapshot)
            changed = False
            if not sheet["cellData"].get("0"):
                if self.columns:
                    self._write_cells(sheet["cellData"], 0, self.columns)
                else:
                    self._write_cells(sheet["cellData"], 0, ["Канал", "Дата", "От", "Сообщение", "Ответ"])
                changed = True
            if changed:
                self._client.put(self._url(), json={"snapshot": snapshot}).raise_for_status()
                log.info("Шапка добавлена в лист таблицы %s", self.workbook)
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("Таблица недоступна (%s): %s", self.base_url, exc)
            return False

    def append(self, values: Sequence) -> bool:
        if not self.enabled:
            return False
        with self._write_lock:
            return self._append_locked(values)

    def _append_locked(self, values: Sequence) -> bool:
        for attempt in range(1, 3):
            try:
                record = self._get_record()
                if record is None:
                    self.ensure()
                    record = self._get_record()
                if record is None:
                    return False
                snapshot = record.get("snapshot") or {}
                sheet = self._pick_sheet(snapshot)
                row = self._next_row(sheet, snapshot)
                if row >= MAX_ROWS:
                    log.error("Таблица переполнена (%s строк)", row)
                    return False
                self._write_cells(sheet["cellData"], row, values)
                self._flush_drawings(snapshot)
                response = self._client.put(self._url(), json={"snapshot": snapshot})
                response.raise_for_status()
                log.info("В таблицу добавлена строка %s", row + 1)
                return True
            except Exception as exc:  # noqa: BLE001
                log.warning("Запись в таблицу не удалась (попытка %s/2): %s", attempt, exc)
                time.sleep(1.0)
        return False

    def build_row(self, channel: str, sender: str, text: str, reply: str = "",
                  username: str = "", chat_id: int | str = "") -> List:
        """Строка в порядке колонок таблицы (или базовый набор по умолчанию)."""
        stamp = time.strftime("%d.%m.%Y %H:%M:%S")
        if self.columns:
            mapping = {
                "канал": channel, "channel": channel,
                "дата": stamp, "date": stamp, "время": stamp,
                "от": sender, "from": sender, "name": sender, "sender": sender, "имя": sender,
                "username": f"@{username}" if username else "",
                "id": chat_id, "chat_id": chat_id, "chatid": chat_id,
                "сообщение": text, "message": text, "текст": text, "text": text,
                "ответ": reply, "reply": reply, "ai": reply,
            }
            return [mapping.get(str(col).strip().lower(), "") for col in self.columns]
        return [channel, stamp, f"{sender} {('@' + username) if username else ''}".strip(), text, reply]

    def append_message(self, channel: str, sender: str, text: str, reply: str = "",
                       username: str = "", chat_id: int | str = "") -> bool:
        return self.append(self.build_row(channel, sender, text, reply, username, chat_id))

    def close(self) -> None:
        self._client.close()
