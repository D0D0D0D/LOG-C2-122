"""Fixtures consumed by ``work/scripts/mrb_live_llm_check.py``."""

from __future__ import annotations

import pytest


class DedicatedDB:
    def __init__(self) -> None:
        self.records = []

    def read_case(self, case_ref):
        return {"declared_items": []}

    def append(self, record):
        self.records.append(record)


class DedicatedKB:
    def __init__(self) -> None:
        self.records = []

    def search(self, query, top_k=5):
        return [
            {
                "source_id": "history_1",
                "check_observation": "包装証明を照合してください。",
                "transport_mode": "air",
                "provenance": {"case_ref": "older_case", "recorded_at": "2026-01-01"},
            }
        ][:top_k]

    def append(self, record):
        self.records.append(record)


@pytest.fixture
def agent_config() -> dict:
    db, kb = DedicatedDB(), DedicatedKB()
    return {
        "max_retry": 3,
        "memory_enabled": False,
        "hitl": {"enabled": False},
        "retrieval": {"top_k": 3},
        "caller_customer_map": {"live-check": "customer_a"},
        "db_connections": {"customer_a": db},
        "kb_connections": {"customer_a": kb},
    }


@pytest.fixture
def case_payload() -> dict:
    return {
        "mode": "invoke",
        "customer_id": "customer_a",
        "case_ref": "case_1",
        "transport_mode": "air",
        "rule_edition_ref": "iata_approved",
        "declared_items": [],
        "evidence_sources": [
            {
                "source_id": "sds_1",
                "doc_type": "sds",
                "quote_span": "declared span",
                "provenance": {"case_ref": "case_1"},
            }
        ],
    }
