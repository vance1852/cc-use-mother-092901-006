"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facilities (
    facility_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    facility_type TEXT NOT NULL,
    depends_on_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS corridors (
    corridor_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    spare_capacity REAL NOT NULL CHECK(spare_capacity >= 0),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crews (
    crew_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    qualifications_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS materials (
    material_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    arrived_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS funds (
    fund_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    amount REAL NOT NULL CHECK(amount >= 0),
    valid_until TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS renewal_windows (
    window_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL,
    fund_id TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    active_revision INTEGER NOT NULL DEFAULT 0,
    effective_proposal_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    window_id TEXT NOT NULL REFERENCES renewal_windows(window_id),
    revision INTEGER,
    base_revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    ttl_expires_at TEXT NOT NULL,
    budget REAL NOT NULL,
    conflicts_json TEXT NOT NULL,
    tradeoffs_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(window_id, revision)
);
CREATE TABLE IF NOT EXISTS phase_plans (
    phase_plan_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
    window_id TEXT NOT NULL,
    phase_code TEXT NOT NULL,
    title TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    planned_start TEXT NOT NULL,
    planned_end TEXT NOT NULL,
    closure_scope TEXT NOT NULL,
    diverted_volume REAL NOT NULL,
    corridor_id TEXT,
    crew_id TEXT NOT NULL,
    qualification TEXT NOT NULL,
    material_ids_json TEXT NOT NULL,
    cost REAL NOT NULL,
    kind TEXT NOT NULL,
    UNIQUE(proposal_id, phase_code)
);
CREATE TABLE IF NOT EXISTS signatures (
    signature_id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
    window_id TEXT NOT NULL,
    party TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    UNIQUE(proposal_id, party)
);
CREATE TABLE IF NOT EXISTS leases (
    lease_id TEXT PRIMARY KEY,
    window_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    phase_code TEXT,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    demand REAL NOT NULL,
    leased_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    status TEXT NOT NULL,
    released_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS phase_executions (
    window_id TEXT NOT NULL,
    phase_code TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    planned_start TEXT NOT NULL,
    planned_end TEXT NOT NULL,
    closure_scope TEXT NOT NULL,
    diverted_volume REAL NOT NULL,
    corridor_id TEXT,
    crew_id TEXT NOT NULL,
    qualification TEXT NOT NULL,
    material_ids_json TEXT NOT NULL,
    cost REAL NOT NULL,
    kind TEXT NOT NULL,
    actual_start TEXT,
    actual_end TEXT,
    PRIMARY KEY(window_id, phase_code)
);
CREATE TABLE IF NOT EXISTS outage_records (
    outage_id TEXT PRIMARY KEY,
    window_id TEXT NOT NULL,
    phase_code TEXT NOT NULL,
    facility_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    diverted_volume REAL NOT NULL,
    occurred_at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY,
    window_id TEXT NOT NULL,
    fund_id TEXT NOT NULL,
    phase_code TEXT,
    amount REAL NOT NULL CHECK(amount > 0),
    milestone TEXT NOT NULL,
    paid_by TEXT NOT NULL,
    paid_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS window_journal (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    event_at TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
