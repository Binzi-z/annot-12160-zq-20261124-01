# Design

This document states the contract, the persistence model, and why the safety
properties hold. Code references are to `approval/service.py` unless noted.

## 1. Domain

A **ticket** describes one privileged request bound to
`(tenant, service, request content)`. To authorize it, two distinct on-call
operators, both on the currently active shift for that service, must each
confirm it. Two distinct confirmations in the same assignment version issue one
**grant** (the approval). A grant is bound to:

- tenant,
- service,
- the canonical **request hash**,
- the **assignment version** (the shift that issued it),
- an **expiry** derived from the service TTL.

The grant can be **consumed exactly once**. Consumption is the only action that
turns a request into an executed privileged operation. Everything else
(rotation, expiry, recovery) can only move an `ACTIVE` grant to a terminal
state `CONSUMED` / `REVOKED` / `EXPIRED`.

## 2. Assignment versions and shift rotation

Each `(tenant, service)` has at most one `ACTIVE` shift (enforced by a partial
unique index). A shift's `shift_version` **is** the assignment version.
Rotating a shift:

1. closes the old shift,
2. opens the new shift at `version + 1`,
3. reclaims every `ACTIVE` grant whose `assignment_version < new version` by
   moving it to `REVOKED` and reopening its ticket (`round_no += 1`).

Steps 1–3 are a single transaction. A stale approval therefore cannot survive a
shift change, and a ticket reopened by that change can be confirmed again in the
new version.

## 3. Persistence ordering (rotation vs. consumption)

Rotation and consumption both write the **same tenant database**. SQLite's
`BEGIN IMMEDIATE` takes the database's single write lock up front, so the two
transactions are totally ordered. Whichever **commits first** defines the
durable order:

- **Consume commits first.** It transitions the grant `ACTIVE -> CONSUMED` and
  writes the `consumptions` row. A later rotation looks for `ACTIVE` grants and
  finds none for that ticket; it does not, and cannot, un-consume it. (The
  `grant_no_resurrect` trigger also blocks any attempt.)
- **Rotate commits first.** It transitions the grant `ACTIVE -> REVOKED`. A
  consumption that was waiting then observes `REVOKED` and is denied with
  `GRANT_REVOKED`, reporting the rotation's revocation audit number.

There is no window in which a grant is both consumed and revoked, and no window
in which both transactions believe they won. `tests/test_multiprocess.py`
drives both orderings with two real processes, using a pre-commit gate to pin
which side commits first.

## 4. Idempotency, replay, and conflict

Every mutating operation accepts an optional `idem_key`. Within the operation's
own transaction we write an `idempotency` row holding the **canonical parameter
hash** and the **serialized result** (including its audit number). On retry:

- Same key, same parameters → return the **stored result verbatim**, flagged
  `replayed`. The original business status and audit number are reproduced; no
  new decision is made.
- Same key, **different** parameters → `IDEMPOTENCY_CONFLICT` (a distinct
  `CONFLICT` status), with a fresh denying audit row that references the
  original. This is separate from business mismatches.
- Different key, changed request content → a normal business denial
  (`REQUEST_MISMATCH`), not a key conflict.

`tests/test_invariants.py` pins that the conflict path and the request-mismatch
path stay distinct.

## 5. Crash semantics (commit, then die)

The state change, the audit row, and the idempotency row are written in **one**
transaction and commit together.

- **Crash after COMMIT, before the reply.** The decision is durable; the caller
  saw nothing. A retry with the same key replays the committed result and audit
  number (`tests/test_multiprocess.py::CrashAfterCommitTest`). A process that
  instead retries with a *new* key gets `ALREADY_CONSUMED` pointing at the
  original consumption — never a second consumption.
- **Crash before COMMIT.** SQLite rolls back the whole transaction. No audit,
  idempotency, consumption, or state row survives; the grant is still `ACTIVE`
  and the same key can be used again cleanly
  (`tests/test_multiprocess.py::CrashBeforeCommitTest`).

So a restart never observes a half-written decision: it sees either all of a
decision or none of it.

## 6. Expiry and restart

Time is injected through a `Clock` (`approval/clock.py`). The service never
calls `datetime.now`. TTL is evaluated against the tenant's **effective now**,
which is `max(wall_clock, tenant_clock.last_seen_at)` and is persisted on every
write. Consequences:

- A grant that expired while the service was down is expired on cold-start
  `recover`, never restored to `ACTIVE`.
- A wall-clock rollback cannot extend a grant, because the tenant high-water
  mark only moves forward (`tests/test_recovery.py`).
- Recovery only moves `ACTIVE -> terminal`; it never consumes and never revives.

## 7. Tenant isolation

Each tenant is a **separate SQLite database file**
(`data/tenants/<tenant>.db`). All of a tenant's writes take that file's write
lock and nothing else. Cold-start `recover(<tenant>)` opens only that tenant's
file, so its transaction cannot acquire, wait on, or block another tenant's
database. `tests/test_recovery.py::TenantIsolationTest` holds tenant A's
recovery transaction open behind a gate in a second process and successfully
consumes tenant B's grant while A is blocked — then confirms the order of
magnitude, not just eventual completion.

## 8. Defense in depth

The service enforces the rules, and the schema enforces the most dangerous ones
independently (`approval/schema.sql`):

- `grant_no_resurrect` — a terminal grant cannot change status.
- `consumption_requires_active_grant` — no consumption row unless the grant is
  `ACTIVE`.
- `consumption_binding_mismatch` — a consumption row must match the grant's
  tenant, service, and request hash.
- `ticket_consumed_immutable` — a consumed ticket cannot reopen.

`tests/test_invariants.py` writes directly through `sqlite3` (bypassing the
service) and confirms each trigger rejects the unsafe write.

## 9. Independent reference model

`approval/reference.py` is a from-scratch, in-memory state machine that
re-derives the decision rules from this document rather than sharing the
service's code. `approval/scenarios.py` runs identical step scripts against the
real SQLite service and the reference model and compares every step's
`(ok, code, status)` plus the final state. `tests/test_evidence.py` additionally
pins every step to an explicit expected approve/deny table, so agreement on a
wrong answer would still be caught.

Scenarios covered: duplicate confirmation, shift rotation, TTL expiry, consume
exactly once with replay, and idempotency-conflict vs. request-mismatch.

## 10. Known boundaries

- SQLite provides serialisation **per database**. Cross-tenant operations are
  independent by design; there is no attempt to make them atomic together.
- Idempotency rows are retained indefinitely (no TTL). An operational
  compaction policy is out of scope for this offline tool.
- Wall-clock skew larger than a TTL is handled by the forward-only tenant
  high-water mark, not by NTP; a process that has never written for a tenant
  starts from the injected clock.
