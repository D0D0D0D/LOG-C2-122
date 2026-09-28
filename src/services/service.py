"""Injected, customer-dedicated service helpers for DG consistency checks.

Clients are provisioned by the deployment and injected as per-customer
connections.  This module deliberately has no fallback shared connection.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any


_SAFE_ID = re.compile(r"^[0-9A-Za-z_-]+$")
_PROHIBITED_PROJECTION_KEYS = frozenset({"classification_final", "shipment_release", "release_decision"})


def normalize_query(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def resolve_customer_for_caller(config: dict[str, Any], caller_id: str) -> str | None:
    mapping = config.get("caller_customer_map", {})
    customer = mapping.get(caller_id) if caller_id and isinstance(mapping, dict) else None
    return customer if isinstance(customer, str) and customer.strip() else None


def _resolve_connection(config: dict[str, Any], key: str, customer_id: str) -> Any | None:
    connections = config.get(key, {})
    if not customer_id or not isinstance(connections, dict):
        return None
    return connections.get(customer_id)


def resolve_customer_db(config: dict[str, Any], customer_id: str) -> Any | None:
    """Return a provisioned dedicated database connection, never a default."""
    return _resolve_connection(config, "db_connections", customer_id)


def resolve_customer_kb(config: dict[str, Any], customer_id: str) -> Any | None:
    """Return a provisioned dedicated KB connection, never a default."""
    return _resolve_connection(config, "kb_connections", customer_id)


def project_recall_record(record: Any) -> dict[str, Any] | None:
    """Project a KB hit to the non-conclusive DG recall contract."""
    if not isinstance(record, dict):
        return None
    source_id = record.get("source_id") or record.get("id")
    observation = record.get("check_observation")
    transport = record.get("transport_mode")
    provenance = record.get("provenance")
    if not all(isinstance(value, str) and value for value in (source_id, observation, transport)):
        return None
    if not _SAFE_ID.fullmatch(source_id) or len(observation) > 512 or not isinstance(provenance, dict):
        return None
    if any(key in record for key in _PROHIBITED_PROJECTION_KEYS):
        return None
    case_ref = provenance.get("case_ref")
    recorded_at = provenance.get("recorded_at")
    if not isinstance(case_ref, str) or not isinstance(recorded_at, str):
        return None
    return {
        "source_id": source_id,
        "check_observation": observation,
        "transport_mode": transport,
        "provenance": {"case_ref": case_ref, "recorded_at": recorded_at},
    }


def read_case(db: Any, case_ref: str) -> dict[str, Any] | None:
    """Read by case ID on an already-dedicated connection (never tenant filter)."""
    for name in ("read_case", "get_case", "fetch_case"):
        method = getattr(db, name, None)
        if callable(method):
            value = method(case_ref)
            return value if isinstance(value, dict) else None
    return None


def append_record(connection: Any, record: dict[str, Any]) -> None:
    """Write through the injected connection without constructing clients here."""
    for name in ("append", "write", "insert", "upsert"):
        method = getattr(connection, name, None)
        if callable(method):
            method(record)
            return
    raise RuntimeError("provisioned connection does not support writes")


def wire_services(config: dict[str, Any]) -> dict[str, Any]:
    """Keep deployment-wired service seams explicit; do not create default clients."""
    wired = dict(config)
    wired.setdefault("db_connections", {})
    wired.setdefault("kb_connections", {})
    wired.setdefault("caller_customer_map", {})
    return wired
