import multiprocessing
import sys
import traceback
from pathlib import Path


def _log_crash() -> None:
    try:
        base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
        (base / "downstream_agent_error.log").write_text(traceback.format_exc(), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


multiprocessing.freeze_support()  # нужен для дочерних процессов в .exe (извлечение таблиц)

try:
    from downstream_agent.app import main
    code = main()
except BaseException:  # noqa: BLE001
    _log_crash()
    code = 1
sys.exit(code)
