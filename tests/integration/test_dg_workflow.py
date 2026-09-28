"""Integration coverage for the compiled Cat2 DG three-mode workflow."""

from __future__ import annotations

import json

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from langgraph.checkpoint.memory import InMemorySaver

from src.graph.graph import DangerousGoodsDocConsistencyAgent


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
        ]

    def append(self, record):
        self.records.append(record)


def test_compiled_three_modes_use_only_injected_dedicated_connections():
    db, kb = DedicatedDB(), DedicatedKB()
    graph = DangerousGoodsDocConsistencyAgent(
        {
            "max_retry": 3,
            "memory_enabled": True,
            "hitl": {"enabled": False},
            "retrieval": {"top_k": 3},
            "llm": None,
            "caller_customer_map": {"operator": "customer_a"},
            "db_connections": {"customer_a": db},
            "kb_connections": {"customer_a": kb},
        }
    )
    graph.compile(checkpointer=InMemorySaver())
    ctx = InvocationContext(
        caller_id="operator", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, thread_id="integration-invoke"
    )
    invoke = {
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
    result = graph.invoke(json.dumps(invoke), ctx=ctx)
    assert result["status"] == "success"
    assert set(result["output"]) == {
        "declared_items",
        "evidence_sources",
        "inconsistency_candidates",
        "inquiry_points",
        "verification_questions",
        "mode",
        "gate_kind",
        "degradation_reason",
    }
    assert result["output"]["degradation_reason"] == "consistency_llm_not_configured"
    assert "history_1" not in {source["source_id"] for source in result["output"]["evidence_sources"]}

    ingest = {
        "mode": "ingest",
        "customer_id": "customer_a",
        "case_ref": "case_1",
        "ingest_records": [{"source_id": "return_1", "check_observation": "数量を照合", "transport_mode": "air"}],
    }
    feedback = {
        "mode": "feedback",
        "customer_id": "customer_a",
        "case_ref": "case_1",
        "feedback": {"approved": True, "action": "approve"},
    }
    assert (
        graph.invoke(
            json.dumps(ingest),
            ctx=InvocationContext(
                caller_id="operator", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, thread_id="integration-ingest"
            ),
        )["status"]
        == "success"
    )
    assert (
        graph.invoke(
            json.dumps(feedback),
            ctx=InvocationContext(
                caller_id="operator", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, thread_id="integration-feedback"
            ),
        )["status"]
        == "success"
    )
    assert db.records and kb.records
