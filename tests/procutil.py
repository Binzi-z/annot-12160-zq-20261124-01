"""Shared helpers for subprocess-based proofs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent


def spawn_worker(*args: str, env: dict[str, str] | None = None) -> subprocess.Popen[str]:
    command = [sys.executable, "-m", "approval.worker", *args]
    full_env = os.environ.copy()
    full_env["PYTHONPATH"] = str(REPO_ROOT)
    if env:
        full_env.update(env)
    return subprocess.Popen(
        command,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=full_env,
    )


def wait_for_file(path: str | Path, timeout: float = 20.0) -> None:
    target = Path(path)
    deadline = time.monotonic() + timeout
    while not target.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for {target}")
        time.sleep(0.01)


def read_result(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def finish(proc: subprocess.Popen[str], timeout: float = 60.0) -> tuple[int, str, str]:
    out, err = proc.communicate(timeout=timeout)
    return proc.returncode, out, err
