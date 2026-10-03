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
CREATE TABLE IF NOT EXISTS counters (
    counter TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_versions (
    version_id TEXT PRIMARY KEY,
    ordinal INTEGER NOT NULL UNIQUE,
    rules_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS investors (
    investor_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('private','state_owned','mixed','foreign')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS investor_relations (
    relation_id TEXT PRIMARY KEY,
    from_investor TEXT NOT NULL REFERENCES investors(investor_id),
    to_investor TEXT NOT NULL REFERENCES investors(investor_id),
    relation_kind TEXT NOT NULL CHECK(relation_kind IN
        ('parent','subsidiary','actual_control','affiliate','consortium')),
    effective_at TEXT NOT NULL,
    revoked_at TEXT,
    created_by TEXT NOT NULL,
    CHECK(from_investor <> to_investor)
);
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL CHECK(current_version >= 1),
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','awarded','closed')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_versions (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    name TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    rule_version_id TEXT NOT NULL REFERENCES rule_versions(version_id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, version_no)
);
CREATE TABLE IF NOT EXISTS pledged_assets (
    asset_id TEXT PRIMARY KEY,
    investor_id TEXT NOT NULL REFERENCES investors(investor_id),
    name TEXT NOT NULL,
    assessed_value REAL NOT NULL CHECK(assessed_value > 0),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS asset_engagements (
    engagement_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES pledged_assets(asset_id),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    application_id TEXT NOT NULL,
    task_id TEXT,
    amount REAL NOT NULL CHECK(amount >= 0),
    engaged_at TEXT NOT NULL,
    released_at TEXT
);
CREATE INDEX IF NOT EXISTS asset_engagements_application ON asset_engagements(application_id);
CREATE UNIQUE INDEX IF NOT EXISTS asset_active_engagement
    ON asset_engagements(asset_id) WHERE released_at IS NULL;
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    investor_id TEXT NOT NULL REFERENCES investors(investor_id),
    evidence_kind TEXT NOT NULL,
    document_hash TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_verifications (
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    seq INTEGER NOT NULL CHECK(seq >= 1),
    status TEXT NOT NULL CHECK(status IN ('verified','revoked')),
    actor_id TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL,
    PRIMARY KEY(evidence_id, seq)
);
CREATE TABLE IF NOT EXISTS applications (
    application_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    project_version INTEGER NOT NULL,
    rule_version_id TEXT NOT NULL REFERENCES rule_versions(version_id),
    lead_investor_id TEXT NOT NULL REFERENCES investors(investor_id),
    plan_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    app_seq INTEGER NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('submitted','selected','rejected','withdrawn')),
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_selected_application
    ON applications(project_id) WHERE status='selected';
CREATE TABLE IF NOT EXISTS application_members (
    application_id TEXT NOT NULL REFERENCES applications(application_id),
    investor_id TEXT NOT NULL REFERENCES investors(investor_id),
    member_role TEXT NOT NULL CHECK(member_role IN ('lead','member')),
    share REAL NOT NULL CHECK(share >= 0 AND share <= 1),
    entry_phase INTEGER NOT NULL CHECK(entry_phase >= 1),
    is_private INTEGER NOT NULL CHECK(is_private IN (0,1)),
    PRIMARY KEY(application_id, investor_id)
);
CREATE TABLE IF NOT EXISTS application_evidence (
    application_id TEXT NOT NULL REFERENCES applications(application_id),
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    investor_id TEXT NOT NULL,
    document_hash TEXT NOT NULL,
    PRIMARY KEY(application_id, evidence_id)
);
CREATE TABLE IF NOT EXISTS application_events (
    application_id TEXT NOT NULL REFERENCES applications(application_id),
    seq INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('submitted','selected','rejected','withdrawn')),
    actor_id TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL,
    PRIMARY KEY(application_id, seq)
);
CREATE TABLE IF NOT EXISTS guarantees (
    guarantee_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    application_id TEXT NOT NULL REFERENCES applications(application_id),
    investor_id TEXT NOT NULL REFERENCES investors(investor_id),
    guarantee_kind TEXT NOT NULL CHECK(guarantee_kind IN ('bid','performance')),
    amount REAL NOT NULL CHECK(amount > 0),
    status TEXT NOT NULL CHECK(status IN ('held','partial','released','claimed')),
    posted_by TEXT NOT NULL,
    posted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS guarantee_events (
    guarantee_id TEXT NOT NULL REFERENCES guarantees(guarantee_id),
    seq INTEGER NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('post','release','claim')),
    amount REAL NOT NULL CHECK(amount >= 0),
    actor_id TEXT NOT NULL,
    at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(guarantee_id, seq)
);
CREATE TABLE IF NOT EXISTS review_tasks (
    task_id TEXT PRIMARY KEY,
    task_seq INTEGER NOT NULL UNIQUE,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    application_id TEXT REFERENCES applications(application_id),
    agreement_id TEXT REFERENCES agreements(agreement_id),
    kind TEXT NOT NULL CHECK(kind IN
        ('admission','member_add','member_remove','phased_entry','control_change','default_exit')),
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    required_roles_json TEXT NOT NULL,
    submitter TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_signatures (
    task_id TEXT NOT NULL REFERENCES review_tasks(task_id),
    role TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
    note TEXT NOT NULL DEFAULT '',
    signed_at TEXT NOT NULL,
    PRIMARY KEY(task_id, role)
);
CREATE TABLE IF NOT EXISTS agreements (
    agreement_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    application_id TEXT NOT NULL UNIQUE REFERENCES applications(application_id),
    rule_version_id TEXT NOT NULL REFERENCES rule_versions(version_id),
    lead_investor_id TEXT NOT NULL,
    controller_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL CHECK(status IN ('active','terminated')),
    signed_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_agreement_per_project
    ON agreements(project_id) WHERE status='active';
CREATE TABLE IF NOT EXISTS agreement_plans (
    agreement_id TEXT NOT NULL REFERENCES agreements(agreement_id),
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    plan_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by_task TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(agreement_id, version_no)
);
CREATE TABLE IF NOT EXISTS membership_events (
    event_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL REFERENCES agreements(agreement_id),
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN
        ('admitted','member_added','member_removed','phase_entered','control_changed','exited')),
    investor_id TEXT,
    payload_json TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    at TEXT NOT NULL,
    UNIQUE(agreement_id, seq)
);
CREATE TABLE IF NOT EXISTS agreement_events (
    agreement_id TEXT NOT NULL REFERENCES agreements(agreement_id),
    seq INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','terminated')),
    actor_id TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL,
    PRIMARY KEY(agreement_id, seq)
);
CREATE TABLE IF NOT EXISTS obligations (
    obligation_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL REFERENCES agreements(agreement_id),
    investor_id TEXT NOT NULL REFERENCES investors(investor_id),
    phase INTEGER NOT NULL CHECK(phase >= 1),
    title TEXT NOT NULL,
    amount REAL NOT NULL CHECK(amount >= 0),
    due_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','fulfilled','settled')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS obligations_agreement ON obligations(agreement_id, investor_id);
CREATE TABLE IF NOT EXISTS obligation_events (
    obligation_id TEXT NOT NULL REFERENCES obligations(obligation_id),
    seq INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','fulfilled','settled')),
    actor_id TEXT NOT NULL,
    at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(obligation_id, seq)
);
CREATE TABLE IF NOT EXISTS exit_settlements (
    settlement_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL REFERENCES agreements(agreement_id),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    reason TEXT NOT NULL,
    outstanding_amount REAL NOT NULL,
    claimed_amount REAL NOT NULL,
    released_amount REAL NOT NULL,
    detail_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
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
