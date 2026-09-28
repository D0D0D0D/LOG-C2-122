"""Stage 2 retrieval-safe recall from the dedicated customer KB."""

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.service import normalize_query, project_recall_record, resolve_customer_kb


class KBRecallNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def execute(self, state: dict) -> dict:
        emit_trace_event("dg_recall_completed", {"transport_mode": state.get("transport_mode", "")}, state)
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        records = state.get("retrieved_records", [])
        customer_id = records[0].get("customer_id", "") if isinstance(records, list) and records and isinstance(records[0], dict) else ""
        kb = resolve_customer_kb(self._config, str(customer_id))
        if kb is None:
            return {"status": AgentStatus.ERROR.value, "error_log": ["kb_recall: no dedicated customer KB connection"]}
        try:
            retrieval = self._config.get("retrieval", {})
            top_k = int(retrieval.get("top_k", 5)) if isinstance(retrieval, dict) else 5
            hits = kb.search(normalize_query(f"dangerous goods document consistency {state.get('transport_mode', '')}"), top_k=top_k)
        except Exception:
            return {"status": AgentStatus.ERROR.value, "error_log": ["kb_recall: customer KB search failed"]}
        safe = [project_recall_record(hit) for hit in hits if isinstance(hits, list)]
        recall_refs = [item for item in safe if item is not None]
        return {
            "recall_refs": recall_refs,
            "requires_human_review": bool(state.get("requires_human_review")),
            "status": AgentStatus.SUCCESS.value,
        }
