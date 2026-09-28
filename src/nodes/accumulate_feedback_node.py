"""Explicitly approved feedback accumulation on dedicated customer connections."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.service import append_record, resolve_customer_db, resolve_customer_kb


class AccumulateFeedbackNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def execute(self, state: dict) -> dict:
        emit_trace_event("dg_feedback_accumulated", {"approved": True}, state)
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        packet = _packet(state)
        feedback = packet.get("feedback", {})
        customer_id, case_ref = packet.get("customer_id"), packet.get("case_ref")
        db, kb = resolve_customer_db(self._config, str(customer_id)), resolve_customer_kb(self._config, str(customer_id))
        if feedback.get("approved") is not True or db is None or kb is None or not isinstance(case_ref, str):
            return {"status": AgentStatus.ERROR.value, "error_log": ["accumulate_feedback: explicit approval and dedicated connections are required"]}
        now = datetime.now(timezone.utc).isoformat()
        try:
            append_record(db, {"table": "feedback_audit_events", "case_ref": case_ref, "actor": "human", "action": feedback.get("action"), "payload_ref": packet.get("payload_ref", ""), "at": now})
            append_record(kb, {"source_id": f"feedback_{case_ref}", "check_observation": "承認済みの照会論点を次回照合観点として記録。", "transport_mode": "", "provenance": {"case_ref": case_ref, "recorded_at": now}})
        except Exception:
            return {"status": AgentStatus.ERROR.value, "error_log": ["accumulate_feedback: dedicated write failed"]}
        return {"mode": "feedback", "feedback_summary": {"case_ref": case_ref, "recorded": True, "action": feedback.get("action")}, "status": AgentStatus.SUCCESS.value}


def _packet(state: dict) -> dict[str, Any]:
    try:
        envelope = json.loads(state.get("user_input", "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return envelope.get("packet", {}) if isinstance(envelope, dict) and isinstance(envelope.get("packet"), dict) else {}
