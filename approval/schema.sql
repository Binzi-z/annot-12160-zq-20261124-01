-- Offline on-call privileged approval tool.
--
-- Persistence rules:
--   * Every business decision is one SQLite write transaction.
--   * The idempotency record, audit record, and state transition are written in
--     that same transaction.  A crash after COMMIT therefore leaves both the
--     decision and its replay record durable; a crash before COMMIT leaves
--     neither.
--   * Terminal grant states (CONSUMED/REVOKED/EXPIRED) are never left in an
--     intermediate state.  Recovery may only move ACTIVE -> terminal.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

INSERT OR IGNORE INTO counters(name, value) VALUES ('audit', 0);
INSERT OR IGNORE INTO counters(name, value) VALUES ('grant', 0);

CREATE TABLE IF NOT EXISTS tenants (
    tenant_id TEXT PRIMARY KEY,
    name      TEXT NOT NULL
);

-- Effective time per tenant only ever moves forward.  A wall-clock rollback
-- therefore cannot extend the life of an authorization after a restart.
CREATE TABLE IF NOT EXISTS tenant_clock (
    tenant_id    TEXT PRIMARY KEY REFERENCES tenants(tenant_id),
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS services (
    service_id          TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
    name                TEXT NOT NULL,
    approval_ttl_seconds INTEGER NOT NULL DEFAULT 1800
);

-- A shift is also the assignment version for a tenant/service.  Rotating a
-- shift creates a new version and reclaims approvals from older versions.
CREATE TABLE IF NOT EXISTS shifts (
    shift_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id      TEXT NOT NULL REFERENCES tenants(tenant_id),
    service_id     TEXT NOT NULL REFERENCES services(service_id),
    shift_version  INTEGER NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CLOSED')),
    started_at     TEXT NOT NULL,
    ended_at       TEXT,
    UNIQUE (tenant_id, service_id, shift_version)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_shift
    ON shifts(tenant_id, service_id)
    WHERE status = 'ACTIVE';

CREATE TABLE IF NOT EXISTS shift_operators (
    shift_id    INTEGER NOT NULL REFERENCES shifts(shift_id),
    operator_id TEXT NOT NULL,
    PRIMARY KEY (shift_id, operator_id)
);

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id        TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL REFERENCES tenants(tenant_id),
    service_id       TEXT NOT NULL REFERENCES services(service_id),
    request_json     TEXT NOT NULL,
    request_hash     TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('OPEN', 'GRANTED', 'CONSUMED')),
    round_no         INTEGER NOT NULL DEFAULT 1,
    created_audit_no TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS confirmations (
    confirmation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id       TEXT NOT NULL REFERENCES tickets(ticket_id),
    shift_id        INTEGER NOT NULL REFERENCES shifts(shift_id),
    shift_version   INTEGER NOT NULL,
    round_no        INTEGER NOT NULL,
    operator_id     TEXT NOT NULL,
    confirmed_at    TEXT NOT NULL,
    audit_no        TEXT NOT NULL,
    UNIQUE (ticket_id, shift_id, operator_id, round_no)
);

CREATE TABLE IF NOT EXISTS grants (
    grant_id           TEXT PRIMARY KEY,
    tenant_id          TEXT NOT NULL REFERENCES tenants(tenant_id),
    service_id         TEXT NOT NULL REFERENCES services(service_id),
    ticket_id          TEXT NOT NULL REFERENCES tickets(ticket_id),
    request_hash       TEXT NOT NULL,
    assignment_version INTEGER NOT NULL,
    shift_id           INTEGER NOT NULL REFERENCES shifts(shift_id),
    round_no           INTEGER NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CONSUMED', 'REVOKED', 'EXPIRED')),
    issued_at          TEXT NOT NULL,
    issued_audit_no    TEXT NOT NULL,
    expires_at         TEXT NOT NULL,
    terminal_at        TEXT,
    terminal_reason    TEXT,
    terminal_audit_no  TEXT,
    UNIQUE (ticket_id, assignment_version, round_no)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_grant_per_ticket
    ON grants(ticket_id)
    WHERE status = 'ACTIVE';

CREATE INDEX IF NOT EXISTS grants_by_state ON grants(tenant_id, service_id, status);
CREATE INDEX IF NOT EXISTS confirmations_by_round
    ON confirmations(ticket_id, shift_id, round_no);

CREATE TABLE IF NOT EXISTS consumptions (
    grant_id     TEXT PRIMARY KEY REFERENCES grants(grant_id),
    tenant_id    TEXT NOT NULL,
    service_id   TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    actor        TEXT NOT NULL,
    consumed_at  TEXT NOT NULL,
    audit_no     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency (
    operation    TEXT NOT NULL,
    idem_key     TEXT NOT NULL,
    params_hash  TEXT NOT NULL,
    tenant_id    TEXT NOT NULL,
    result_json  TEXT NOT NULL,
    audit_no     TEXT,
    replay_count INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (operation, idem_key)
);

-- Enforce the core safety properties even for code paths that bypass the
-- service layer.

CREATE TRIGGER IF NOT EXISTS grant_no_resurrect
BEFORE UPDATE OF status ON grants
WHEN OLD.status IN ('CONSUMED', 'REVOKED', 'EXPIRED')
     AND NEW.status <> OLD.status
BEGIN
    SELECT RAISE(ABORT, 'terminal grant cannot change status');
END;

CREATE TRIGGER IF NOT EXISTS consumption_requires_active_grant
BEFORE INSERT ON consumptions
WHEN (SELECT status FROM grants WHERE grant_id = NEW.grant_id) <> 'ACTIVE'
BEGIN
    SELECT RAISE(ABORT, 'consumption requires an ACTIVE grant');
END;

CREATE TRIGGER IF NOT EXISTS consumption_binding_mismatch
BEFORE INSERT ON consumptions
WHEN NEW.request_hash <> (SELECT request_hash FROM grants WHERE grant_id = NEW.grant_id)
     OR NEW.tenant_id <> (SELECT tenant_id FROM grants WHERE grant_id = NEW.grant_id)
     OR NEW.service_id <> (SELECT service_id FROM grants WHERE grant_id = NEW.grant_id)
BEGIN
    SELECT RAISE(ABORT, 'consumption binding mismatch');
END;

CREATE TRIGGER IF NOT EXISTS ticket_consumed_immutable
BEFORE UPDATE OF status ON tickets
WHEN OLD.status = 'CONSUMED' AND NEW.status <> OLD.status
BEGIN
    SELECT RAISE(ABORT, 'consumed ticket is immutable');
END;

CREATE TABLE IF NOT EXISTS audit (
    seq                INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_no           TEXT NOT NULL UNIQUE,
    tenant_id          TEXT,
    service_id         TEXT,
    subject_type       TEXT NOT NULL,
    subject_id         TEXT NOT NULL,
    action             TEXT NOT NULL,
    decision           TEXT NOT NULL CHECK (decision IN ('APPROVED', 'DENIED', 'NEUTRAL')),
    code               TEXT NOT NULL,
    actor              TEXT,
    shift_id           INTEGER,
    assignment_version INTEGER,
    detail_json        TEXT NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS audit_by_tenant ON audit(tenant_id, seq);
CREATE INDEX IF NOT EXISTS audit_by_subject ON audit(subject_type, subject_id);

-- Per-tenant recovery lease.  Recovery never takes a lease that spans tenants,
-- so an unhealthy tenant cannot freeze the others.
CREATE TABLE IF NOT EXISTS recovery_leases (
    tenant_id   TEXT PRIMARY KEY REFERENCES tenants(tenant_id),
    owner       TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT NOT NULL REFERENCES tenants(tenant_id),
    owner       TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    scanned     INTEGER NOT NULL DEFAULT 0,
    expired     INTEGER NOT NULL DEFAULT 0,
    revoked     INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'RUNNING'
);

CREATE INDEX IF NOT EXISTS recovery_runs_by_tenant ON recovery_runs(tenant_id, run_id);
