import asyncio
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import DB_PATH, load_config  # noqa: E402
from handlers import Assistant  # noqa: E402
from llm import LLM  # noqa: E402
from sheets import SheetsSink  # noqa: E402
from store import Store  # noqa: E402


class FakeClient:
    async def __call__(self, request):
        print("typing request ok")

    async def send_message(self, peer, text, **kw):
        print("send_message:", text[:60])

        class M:
            id = 1

        return M()


async def main():
    cfg = load_config()
    store = Store(DB_PATH)
    llm = LLM(cfg, store)
    sink = SheetsSink(cfg.sheet_url, cfg.sheet_workbook, cfg.sheet_name, cfg.sheet_columns, cfg.sheet_style)

    class Me:
        id = 8444434959
        first_name = "Jedi"
        username = "jedio7"

    a = Assistant(FakeClient(), cfg, store, llm, sink)
    a.me = Me()
    try:
        await a._handle_own_message(8444434959, "привет, ассистент (диагностика)")
        await asyncio.sleep(0.1)
        print("buffers:", a._buffers)
        tasks = [t for t in a._timers.values()]
        print("timers:", len(tasks))
        await asyncio.gather(*tasks, return_exceptions=False)
        await asyncio.sleep(8)
        print("ОК: обработка завершилась без исключений")
    except Exception:
        traceback.print_exc()
    finally:
        sink.close()
        store.close()


asyncio.run(main())
