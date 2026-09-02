"""Task run receipts -- one small JSON file per scheduled task, written atomically.

WHY
---
Task Scheduler's LastTaskResult only says whether the process exited 0. It cannot say
whether MT5 initialised, how long the run took, or which exception ended it. The intraday
mean-reversion task exited 0 for three weeks while mt5.initialize() failed on every fire,
so nothing was red. A receipt makes every run of a task inspectable from one file.

Usage::

    with run_receipt("MT5-IntradayMR") as r:
        r.mt5_init = bool(mt5.initialize())
        ...
        r.record_error(exc, "run_symbol:GOLD")   # handled, but worth seeing

File: ``DATA_CACHE/task_runs/<TaskName>.json``. Another lane's health check reads it, so
the schema is fixed -- do not add, rename or drop keys::

    {"task": str, "started_utc": iso, "finished_utc": iso|null, "ok": bool,
     "exit_code": int|null, "mt5_init": bool|null,
     "errors": [{"type": str, "where": str}], "duration_s": float|null}

Semantics:
  * written once on entry (finished_utc / duration_s / exit_code null, ok false) so a run
    that hangs or is killed leaves a receipt saying "started, never finished";
  * rewritten on exit. An exception escaping the with-block is recorded and RE-RAISED;
    SystemExit is recorded with its code and re-raised. ``exit_code`` follows Python's
    process semantics: 0 for a clean finish or SystemExit(0)/SystemExit(None), the code
    for SystemExit(int), 1 for any other escaping exception;
  * ``ok`` is true only for a clean run: exit_code 0 AND nothing recorded through
    record_error(). A handled per-symbol exception therefore still turns the receipt red;
    that is deliberate -- a live path that fails quietly every fire is what this exists for;
  * the receipt never raises into the task. I/O failures are printed to stderr.

Stdlib only.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import DATA_CACHE, write_json_atomic  # noqa: E402

TASK_RUNS_DIR: Path = DATA_CACHE / "task_runs"


class TaskReceipt:
    """Context manager that records one run of a scheduled task."""

    def __init__(self, task: str, directory: Path | None = None) -> None:
        self.task = str(task)
        self.directory = Path(directory) if directory is not None else TASK_RUNS_DIR
        self.mt5_init: bool | None = None
        self.errors: list[dict[str, str]] = []
        self._started: datetime | None = None
        self._t0 = 0.0

    @property
    def path(self) -> Path:
        return self.directory / f"{self.task}.json"

    def record_error(self, exc: BaseException, where: str) -> None:
        """Record a handled exception: class name plus a short label saying where."""
        self.errors.append({"type": type(exc).__name__, "where": str(where)[:120]})

    def __enter__(self) -> "TaskReceipt":
        self._started = datetime.now(tz=timezone.utc)
        self._t0 = time.monotonic()
        self._write(finished=None, exit_code=None)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is None:
            exit_code = 0
        elif isinstance(exc, SystemExit):
            code = exc.code
            if code is None:
                exit_code = 0
            elif isinstance(code, int):
                exit_code = code
            else:
                exit_code = 1  # Python prints a non-int code and exits 1
        else:
            exit_code = 1
            self.record_error(exc, "unhandled")
        self._write(finished=datetime.now(tz=timezone.utc), exit_code=exit_code)
        return False  # never swallow: the task's own exit semantics are preserved

    def _write(self, *, finished: datetime | None, exit_code: int | None) -> None:
        duration = round(time.monotonic() - self._t0, 3) if finished is not None else None
        payload = {
            "task": self.task,
            "started_utc": self._started.isoformat() if self._started else None,
            "finished_utc": finished.isoformat() if finished else None,
            "ok": exit_code == 0 and not self.errors,
            "exit_code": exit_code,
            "mt5_init": None if self.mt5_init is None else bool(self.mt5_init),
            "errors": list(self.errors),
            "duration_s": duration,
        }
        try:
            write_json_atomic(self.path, payload)
        except Exception as io_exc:  # the receipt must never take the task down
            print(f"task_receipt: could not write {self.path}: "
                  f"{type(io_exc).__name__}: {io_exc}", file=sys.stderr)


def run_receipt(task: str, directory: Path | None = None) -> TaskReceipt:
    """``with run_receipt('MT5-Something') as r:`` -- see the module docstring."""
    return TaskReceipt(task, directory)
