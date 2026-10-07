"""Backend for the savetokens desktop status-bar item: GET /api/plugins/savetokens/status.

Reads the local savetokens store only; returns a short line and a detail summary.
"""
import sys
from pathlib import Path

from fastapi import APIRouter

router = APIRouter()


def _ensure_import():
    try:
        import savetokens  # noqa: F401
    except ImportError:
        path_file = Path(__file__).resolve().parent.parent / "package_path.txt"
        if path_file.exists():
            sys.path.append(path_file.read_text().strip())


@router.get("/status")
def status():   # sync: FastAPI runs it in a worker thread, so SQLite never blocks the event loop
    try:
        _ensure_import()
        from savetokens import forecast, notify
        from savetokens.store import Store
        with Store() as s:
            return {"text": forecast.segment(s) or "savetokens", "detail": notify.daily(s)}
    except Exception as e:  # never break the dashboard
        return {"text": "savetokens", "detail": f"savetokens unavailable: {type(e).__name__}"}
