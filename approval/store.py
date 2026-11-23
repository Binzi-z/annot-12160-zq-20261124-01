"""SQLite storage for exactly one tenant.

Each tenant has its own database file.  That is a structural guarantee that a
cold-start recovery (which takes a write transaction on its tenant database)
cannot freeze any other tenant.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

BUSY_TIMEOUT_MS = 30_000


class TenantStore:
    def __init__(self, root: str | Path, tenant_id: str) -> None:
        self.root = Path(root)
        self.tenant_id = tenant_id
        self.tenants_dir = self.root / "tenants"
        self.locks_dir = self.root / "locks"
        self.db_path = self.tenants_dir / f"{tenant_id}.db"
        self.lock_path = self.locks_dir / f"{tenant_id}.lock"

    # ------------------------------------------------------------------ paths
    def exists(self) -> bool:
        return self.db_path.exists()

    def registry_path(self) -> Path:
        return self.root / "tenants.json"

    # -------------------------------------------------------------- connections
    def connect(self) -> sqlite3.Connection:
        self.tenants_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        return conn

    def initialize(self) -> None:
        self.tenants_dir.mkdir(parents=True, exist_ok=True)
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            conn.execute("PRAGMA synchronous = FULL")
        finally:
            conn.close()

    # ------------------------------------------------------------- transactions
    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()


def list_tenants(root: str | Path) -> list[str]:
    tenants_dir = Path(root) / "tenants"
    if not tenants_dir.exists():
        return []
    return sorted(path.stem for path in tenants_dir.glob("*.db"))
