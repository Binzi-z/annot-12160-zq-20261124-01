# Approval decisions: per-step evidence

Overall agreement with the independent reference model: **PASS**

## duplicate_confirmation — PASS

Same operator confirms twice; only the second distinct operator issues a grant.

```
 1. APPROVE  setup                code=SHIFT_STARTED            status=ACTIVE     audit=AUD-acme-00000003
 2. APPROVE  ticket               code=TICKET_CREATED           status=OPEN       audit=AUD-acme-00000004
 3. APPROVE  alice-1              code=CONFIRMATION_RECORDED    status=OPEN       audit=AUD-acme-00000005
 4. DENY     alice-2              code=DUPLICATE_CONFIRMATION   status=OPEN       audit=AUD-acme-00000006
 5. APPROVE  bob-1                code=GRANT_ISSUED             status=GRANTED    audit=AUD-acme-00000008
 6. DENY     alice-3              code=DUPLICATE_CONFIRMATION   status=GRANTED    audit=AUD-acme-00000009
```

Final state: `{"active_shift": ["pay", 1, ["alice", "bob"]], "grants_by_status": {"ACTIVE": 1}, "services": ["pay"], "tickets_by_status": {"GRANTED": 1}}`

## rotation — PASS

Shift rotation reclaims an unconsumed approval and reopens the ticket.

```
 1. APPROVE  setup                code=SHIFT_STARTED            status=ACTIVE     audit=AUD-acme-00000003
 2. APPROVE  ticket               code=TICKET_CREATED           status=OPEN       audit=AUD-acme-00000004
 3. APPROVE  alice                code=CONFIRMATION_RECORDED    status=OPEN       audit=AUD-acme-00000005
 4. APPROVE  bob                  code=GRANT_ISSUED             status=GRANTED    audit=AUD-acme-00000007
 5. APPROVE  rotate               code=SHIFT_ROTATED            status=ACTIVE     audit=AUD-acme-00000008
 6. DENY     old-consume          code=GRANT_REVOKED            status=REVOKED    audit=AUD-acme-00000009
 7. APPROVE  new-alice            code=CONFIRMATION_RECORDED    status=OPEN       audit=AUD-acme-00000011
 8. APPROVE  new-bob              code=GRANT_ISSUED             status=GRANTED    audit=AUD-acme-00000013
 9. APPROVE  new-consume          code=GRANT_CONSUMED           status=CONSUMED   audit=AUD-acme-00000014
```

Final state: `{"active_shift": ["pay", 2, ["carol", "dave"]], "grants_by_status": {"CONSUMED": 1, "REVOKED": 1}, "services": ["pay"], "tickets_by_status": {"CONSUMED": 1}}`

## expiry — PASS

An approval older than its TTL is expired on consumption and cannot be used.

```
 1. APPROVE  setup                code=SHIFT_STARTED            status=ACTIVE     audit=AUD-acme-00000003
 2. APPROVE  ticket               code=TICKET_CREATED           status=OPEN       audit=AUD-acme-00000004
 3. APPROVE  alice                code=CONFIRMATION_RECORDED    status=OPEN       audit=AUD-acme-00000005
 4. APPROVE  bob                  code=GRANT_ISSUED             status=GRANTED    audit=AUD-acme-00000007
 5. APPROVE  advance              code=CLOCK_ADVANCED           status=OK         audit=None
 6. DENY     late-consume         code=GRANT_EXPIRED            status=EXPIRED    audit=AUD-acme-00000008
 7. APPROVE  reopen-confirm       code=CONFIRMATION_RECORDED    status=OPEN       audit=AUD-acme-00000010
```

Final state: `{"active_shift": ["pay", 1, ["alice", "bob"]], "grants_by_status": {"EXPIRED": 1}, "services": ["pay"], "tickets_by_status": {"OPEN": 1}}`

## consume_once_and_retry — PASS

A grant is consumed once; identical retries replay the original decision and audit number.

```
 1. APPROVE  setup                code=SHIFT_STARTED            status=ACTIVE     audit=AUD-acme-00000003
 2. APPROVE  ticket               code=TICKET_CREATED           status=OPEN       audit=AUD-acme-00000004
 3. APPROVE  alice                code=CONFIRMATION_RECORDED    status=OPEN       audit=AUD-acme-00000005
 4. APPROVE  bob                  code=GRANT_ISSUED             status=GRANTED    audit=AUD-acme-00000007
 5. APPROVE  consume              code=GRANT_CONSUMED           status=CONSUMED   audit=AUD-acme-00000008
 6. APPROVE  retry-same           code=GRANT_CONSUMED           status=CONSUMED   audit=AUD-acme-00000008
 7. DENY     consume-again        code=ALREADY_CONSUMED         status=CONSUMED   audit=AUD-acme-00000008
```

Final state: `{"active_shift": ["pay", 1, ["alice", "bob"]], "grants_by_status": {"CONSUMED": 1}, "services": ["pay"], "tickets_by_status": {"CONSUMED": 1}}`

## conflict_and_mismatch — PASS

Changed parameters under the same key conflict; changed request content is a separate mismatch.

```
 1. APPROVE  setup                code=SHIFT_STARTED            status=ACTIVE     audit=AUD-acme-00000003
 2. APPROVE  ticket               code=TICKET_CREATED           status=OPEN       audit=AUD-acme-00000004
 3. APPROVE  alice                code=CONFIRMATION_RECORDED    status=OPEN       audit=AUD-acme-00000005
 4. APPROVE  bob                  code=GRANT_ISSUED             status=GRANTED    audit=AUD-acme-00000007
 5. DENY     wrong-request        code=REQUEST_MISMATCH         status=ACTIVE     audit=AUD-acme-00000008
 6. DENY     stale-version        code=STALE_ASSIGNMENT         status=ACTIVE     audit=AUD-acme-00000009
 7. APPROVE  consume              code=GRANT_CONSUMED           status=CONSUMED   audit=AUD-acme-00000010
 8. DENY     same-key-new-actor   code=IDEMPOTENCY_CONFLICT     status=CONFLICT   audit=AUD-acme-00000011
```

Final state: `{"active_shift": ["pay", 1, ["alice", "bob"]], "grants_by_status": {"CONSUMED": 1}, "services": ["pay"], "tickets_by_status": {"CONSUMED": 1}}`
