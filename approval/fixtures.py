"""Local synthetic data.

No network, no external services.  Everything here is deterministic so demos
and tests can assert exact audit numbers and business codes.
"""

from __future__ import annotations

from typing import Any

TENANTS = [
    {
        "tenant_id": "acme",
        "name": "Acme Production",
        "services": [
            {
                "service_id": "acme-payments",
                "name": "Payments API",
                "approval_ttl_seconds": 1800,
                "operators": ["alice", "bob"],
            },
            {
                "service_id": "acme-search",
                "name": "Search Cluster",
                "approval_ttl_seconds": 900,
                "operators": ["carol", "dave"],
            },
        ],
    },
    {
        "tenant_id": "globex",
        "name": "Globex Production",
        "services": [
            {
                "service_id": "globex-db",
                "name": "Primary Database",
                "approval_ttl_seconds": 600,
                "operators": ["erin", "frank"],
            }
        ],
    },
]

TICKETS: list[dict[str, Any]] = [
    {
        "tenant_id": "acme",
        "service_id": "acme-payments",
        "ticket_id": "ACME-1001",
        "request": {"action": "restart", "host": "pay-01", "reason": "memory leak"},
    },
    {
        "tenant_id": "acme",
        "service_id": "acme-payments",
        "ticket_id": "ACME-1002",
        "request": {"action": "scale", "host": "pay-02", "from": 2, "to": 4},
    },
    {
        "tenant_id": "acme",
        "service_id": "acme-search",
        "ticket_id": "ACME-2001",
        "request": {"action": "failover", "host": "search-03"},
    },
    {
        "tenant_id": "globex",
        "service_id": "globex-db",
        "ticket_id": "GLBX-7001",
        "request": {"action": "failover", "host": "db-primary"},
    },
]


def seed(service: Any, *, start_time: str) -> list[Any]:
    """Provision every tenant/service/shift and create all synthetic tickets."""

    results: list[Any] = []
    for tenant in TENANTS:
        results.append(
            service.provision_tenant(
                tenant["tenant_id"], tenant["name"], idem_key=f"seed-tenant-{tenant['tenant_id']}"
            )
        )
        for svc in tenant["services"]:
            results.append(
                service.add_service(
                    tenant["tenant_id"],
                    svc["service_id"],
                    svc["name"],
                    approval_ttl_seconds=svc["approval_ttl_seconds"],
                    idem_key=f"seed-service-{svc['service_id']}",
                )
            )
            results.append(
                service.start_shift(
                    tenant["tenant_id"],
                    svc["service_id"],
                    svc["operators"],
                    idem_key=f"seed-shift-{svc['service_id']}",
                )
            )
    for ticket in TICKETS:
        results.append(
            service.create_ticket(
                ticket["tenant_id"],
                ticket["service_id"],
                ticket["ticket_id"],
                ticket["request"],
                idem_key=f"seed-ticket-{ticket['ticket_id']}",
            )
        )
    return results
