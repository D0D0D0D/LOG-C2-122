"""PB-1/PB-3/PB-5 and static TC-04/TC-05/TC-11 boundary checks."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from langgraph.checkpoint.memory import InMemorySaver

from src.payload_store import PayloadStore
from src.graph.graph import DangerousGoodsDocConsistencyAgent
from src.schemas.state import State


class _DB:
    def read_case(self, case_ref):
        return {}

    def append(self, record):
        pass


class _KB:
    def search(self, query, top_k=5):
        return []

    def append(self, record):
        pass


def _config():
    return {"max_retry": 3, "memory_enabled": True, "hitl": {"enabled": False}, "retrieval": {"top_k": 2}, "llm": None, "caller_customer_map": {"caller": "customer"}, "db_connections": {"customer": _DB()}, "kb_connections": {"customer": _KB()}}


def test_pb3_injected_service_seams_have_no_default_customer_connection():
    graph = DangerousGoodsDocConsistencyAgent({**_config(), "db_connections": {}, "kb_connections": {}})
    graph.compile(checkpointer=InMemorySaver())
    packet = {"mode": "invoke", "customer_id": "customer", "case_ref": "case", "transport_mode": "air", "rule_edition_ref": "iata", "declared_items": [], "evidence_sources": []}
    result = graph.invoke(json.dumps(packet), ctx=InvocationContext(caller_id="caller", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, thread_id="no-default"))
    assert result["status"] == "error"


def test_pb5_payload_body_never_enters_outer_or_inner_checkpoint():
    sentinel = "raw-sds-body-must-not-checkpoint"
    store = PayloadStore(scope_keys=("customer_id",), ttl_seconds=300)
    config = {**_config(), "payload_store": store}
    graph = DangerousGoodsDocConsistencyAgent(config)
    outer = InMemorySaver()
    graph.compile(checkpointer=outer)
    ref = store.put({"full_sds": sentinel}, scope={"customer_id": "customer"}, session_id="payload-safe")
    packet = {"mode": "invoke", "customer_id": "customer", "case_ref": "case", "transport_mode": "air", "rule_edition_ref": "iata", "declared_items": [], "evidence_sources": [{"source_id": "sds", "doc_type": "sds", "quote_span": "safe span", "provenance": {"case_ref": "case"}}], "payload_ref": ref}
    assert graph.invoke(json.dumps(packet), ctx=InvocationContext(caller_id="caller", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, thread_id="payload-safe"))["status"] == "success"
    inner = graph._nodes["main"]._checkpointer
    for saver in (outer, inner):
        persisted = json.dumps([{"checkpoint": item.checkpoint, "writes": item.pending_writes} for item in saver.list(None)], default=str)
        assert sentinel not in persisted


def test_tc04_state_has_no_invocation_context_and_tc05_tc11_nodes_emit_domain_events_only():
    state_source = Path("src/schemas/state.py").read_text(encoding="utf-8")
    assert "InvocationContext" not in state_source
    for path in Path("src/nodes").glob("*_node.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in (item for item in ast.walk(tree) if isinstance(item, ast.FunctionDef) and item.name == "execute"):
            segment = ast.get_source_segment(source, node) or ""
            assert "emit_trace_event(" in segment, f"{path}:{node.lineno} has no domain trace event"
            assert not any(name in segment for name in ('emit_trace_event("node_start"', 'emit_trace_event("node_complete"', 'emit_trace_event("node_error"'))
    assert "InvocationContext" not in State.__annotations__.values()
