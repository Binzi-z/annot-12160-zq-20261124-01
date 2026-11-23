# Offline On-Call Privileged Approval Tool

A self-contained Python 3.12 + SQLite tool for two-operator privileged approvals.
No network, no third-party packages: everything runs against local synthetic
data and per-tenant SQLite files.

## What it does

- **Two distinct on-call operators** must confirm the same ticket. A single
  operator's repeated confirmation is rejected as `DUPLICATE_CONFIRMATION`.
- The resulting **approval is bound** to tenant, service, the canonical request
  content hash, and the independent **assignment version** (the shift).
- **Shift rotation** (`rotate`) reclaims any approval from the previous version
  that has not yet been consumed (`REVOKED`) and reopens the ticket.
- An approval can be **consumed exactly once**. A second consumption is denied
  (`ALREADY_CONSUMED`) and points at the original decision's audit number.
- Caller **retries** with the same idempotency key and parameters return the
  original business status and audit number. Changed parameters under the same
  key are a separate `IDEMPOTENCY_CONFLICT`.
- **Crash-after-commit** is replayable; **crash-before-commit** leaves nothing.
- **Restart** never resurrects an expired approval; wall-clock rollback cannot
  extend one.
- **One tenant's cold-start recovery cannot freeze another tenant** (separate
  database per tenant).

## Quick start

```bash
# Create deterministic local synthetic data (two tenants, four tickets).
python -m approval.cli --data-dir data seed

# First operator confirms.
python -m approval.cli --data-dir data --now 2026-01-01T00:01:00Z \
  confirm --tenant acme --ticket ACME-1001 --operator alice --idem-key c1

# Second distinct operator confirms -> a grant is issued.
python -m approval.cli --data-dir data --now 2026-01-01T00:01:00Z \
  confirm --tenant acme --ticket ACME-1001 --operator bob --idem-key c2 --json

# Consume the approval exactly once (request content is bound).
python -m approval.cli --data-dir data --now 2026-01-01T00:02:00Z \
  consume --tenant acme --service acme-payments \
  --request '{"action":"restart","host":"pay-01","reason":"memory leak"}' \
  --actor runbook --grant G-acme-000001 --idem-key x1 --json

# A duplicate delivery of the same command replays the original decision.
python -m approval.cli --data-dir data --now 2026-01-01T00:02:00Z \
  consume --tenant acme --service acme-payments \
  --request '{"action":"restart","host":"pay-01","reason":"memory leak"}' \
  --actor runbook --grant G-acme-000001 --idem-key x1 --json   # replayed: true

# Inspect state and audit trail.
python -m approval.cli --data-dir data status --tenant acme --ticket ACME-1001
python -m approval.cli --data-dir data audit  --tenant acme
```

`--now` injects the clock, so runs are reproducible without sleeping. Omit it to
use the system clock. Commands exit `0` on approval and `3` on denial, so shell
scripts can branch on the result.

### Commands

| Command   | Purpose |
|-----------|---------|
| `seed`    | Create local synthetic tenants/services/shifts/tickets. |
| `confirm` | Record one operator's confirmation (two distinct ones issue a grant). |
| `rotate`  | Register a shift change; reclaim unconsumed approvals of the prior version. |
| `consume` | Consume an approval exactly once, bound to the request content. |
| `recover` | Cold-start reconciliation for **one** tenant. |
| `status`  | Show tenant / ticket / grant / active-shift state. |
| `audit`   | Print the audit log, including denied attempts. |
| `tenants` | List known tenants. |

## Evidence

`docs/EVIDENCE.md` is generated from live execution and lists every scenario's
per-step approve/deny decision, its business code, resulting status, and audit
number, alongside agreement with the independent reference model. It is
rewritten each time the test suite runs, so it cannot drift from the code. To
regenerate manually:

```bash
python -m approval.report --markdown   # prints the document
```

## Independent reference model & differential checks

`approval/reference.py` is an independent in-memory state machine derived from
the specification (not from the service code). `approval/scenarios.py` runs the
same step scripts against both and compares each decision and the final state.
Covered scenarios: duplicate confirmation, shift rotation, TTL expiry, consume
exactly once with replay, and idempotency conflict vs. request mismatch.

## Concurrency, crash, and isolation proofs

Run the full suite (standard library `unittest`, no pytest required):

```bash
python -m unittest discover -s tests -v
```

Highlights in `tests/`:

- `test_multiprocess.py` — two real OS processes racing to consume the same
  grant (exactly one wins); rotation vs. consumption in **both** commit orders;
  a process that dies after COMMIT (retry replays) and one that dies before
  COMMIT (nothing persists).
- `test_recovery.py` — a blocked cold-start recovery for tenant A does not
  freeze tenant B (measured, with a hard deadline); expired grants stay expired
  across restart; wall-clock rollback cannot extend a grant.
- `test_invariants.py` — direct-SQL attempts to resurrect/consume inconsistently
  are rejected by database triggers.
- `test_evidence.py` / `test_differential.py` — every decision is pinned and
  cross-checked against the reference model.

## Layout

```
approval/
  cli.py         command-line interface
  service.py     business logic + persistence ordering
  schema.sql     schema, constraints, and safety triggers
  store.py       per-tenant SQLite connections and transactions
  reference.py   independent reference state machine
  scenarios.py   differential scenario runner
  report.py      evidence report generator
  worker.py      subprocess entry point for the multi-process proofs
  fixtures.py    local synthetic data
docs/
  DESIGN.md      contract, ordering rationale, safety arguments
  EVIDENCE.md    generated per-decision approve/deny evidence
tests/           unittest suite (unit, differential, multi-process, recovery)
```

## Requirements

Python 3.12, standard library only. SQLite is bundled with CPython. The tool
uses WAL journaling and `synchronous=FULL` for durable commits.
