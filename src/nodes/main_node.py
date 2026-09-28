"""Cat2 main-slot adapter for the cached dangerous-goods inner workflow."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.cat2_graph_skeleton import DomainWorkflowGraphNode, ModeSpec


class DangerousGoodsWorkflowGraphNode(DomainWorkflowGraphNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    error_strategy: ClassVar[str] = "handle"
    propagate_hitl: ClassVar[bool] = False
    known_failure_codes: ClassVar[tuple[str, ...]] = ("llm_call_failed", "llm_output_parse_failed")
    mode_specs: ClassVar[dict[str, ModeSpec]] = {
        "invoke": ModeSpec("dg_consistency_brief", {
            "consistency_brief": "consistency_brief", "evidence_map": "evidence_map",
            "cited_source_ids": "cited_source_ids", "retrieved_records": "retrieved_records",
            "requires_human_review": "requires_human_review", "error_log": "error_log",
            "error_code": "error_code", "error_type": "error_type",
        }),
        "ingest": ModeSpec("ingest", {"ingest_summary": "ingest_summary", "error_log": "error_log", "error_code": "error_code", "error_type": "error_type"}),
        "feedback": ModeSpec("feedback", {"feedback_summary": "feedback_summary", "error_log": "error_log", "error_code": "error_code", "error_type": "error_type"}),
    }

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__(config=config, subgraph_factory=self._make_subgraph)

    @staticmethod
    def _make_subgraph(config: dict[str, Any]):
        from src.graph.domain_workflow_graph import DangerousGoodsWorkflowGraph
        return DangerousGoodsWorkflowGraph(config=config)

    def _parent_config(self) -> dict[str, Any]:
        parent = super()._parent_config()
        # The cached inner graph owns a separate checkpointer even though this
        # template never propagates synchronous HITL; this preserves Cat2
        # GraphNode execution semantics without enabling interrupt().
        parent["memory_enabled"] = True
        allowed = {"llm", "retrieval", "db_connections", "kb_connections", "caller_customer_map", "payload_store", "required_documents_by_transport", "class_packing_rules", "memory_enabled"}
        return {key: value for key, value in parent.items() if key in allowed}

    def execute(self, state: dict) -> dict:
        emit_trace_event("dg_workflow_invoked", {"mode": state.get("mode", "")}, state)
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        return super().execute(state)

    def extract_input(self, state: dict) -> str:
        raw = state.get("validated_input", "{}")
        try:
            packet = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError:
            packet = {}
        return json.dumps({"mode": state.get("mode", ""), "packet": packet}, ensure_ascii=False, default=str, sort_keys=True)


MainNode = DangerousGoodsWorkflowGraphNode
