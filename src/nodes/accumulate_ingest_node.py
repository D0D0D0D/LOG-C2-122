"""Dedicated-connection ingestion of non-conclusive DG recall projections."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.service import append_record, resolve_customer_db, resolve_customer_kb


class AccumulateIngestNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def execute(self, state: dict) -> dict:
        emit_trace_event("dg_ingest_accumulated", {"has_input": bool(state.get("user_input"))}, state)
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        packet = _packet(state)
        customer_id, case_ref = packet.get("customer_id"), packet.get("case_ref")
        db, kb = resolve_customer_db(self._config, str(customer_id)), resolve_customer_kb(self._config, str(customer_id))
        if db is None or kb is None or not isinstance(case_ref, str):
            return {"status": AgentStatus.ERROR.value, "error_log": ["accumulate_ingest: dedicated connections are required"]}
        records = packet.get("ingest_records", [])
        if not isinstance(records, list):
            return {"status": AgentStatus.ERROR.value, "error_log": ["accumulate_ingest: ingest_records is invalid"]}
        now = datetime.now(timezone.utc).isoformat()
        try:
            for index, record in enumerate(records, 1):
                if not isinstance(record, dict):
                    raise ValueError("record is invalid")
                observation = str(record.get("check_observation", ""))[:512]
                source_id = str(record.get("source_id", f"ingest_{index}"))
                projection = {"source_id": source_id, "check_observation": observation, "transport_mode": str(record.get("transport_mode", "")), "provenance": {"case_ref": case_ref, "recorded_at": now}}
                append_record(db, {"table": "return_history", "case_ref": case_ref, "return_reason_ref": source_id, "recorded_at": now})
                append_record(kb, projection)
            append_record(db, {"table": "feedback_audit_events", "case_ref": case_ref, "actor": "ingest", "action": "ingest", "payload_ref": packet.get("payload_ref", ""), "at": now})
        except Exception:
            return {"status": AgentStatus.ERROR.value, "error_log": ["accumulate_ingest: dedicated write failed"]}
        return {"mode": "ingest", "ingest_summary": {"case_ref": case_ref, "written_count": len(records)}, "status": AgentStatus.SUCCESS.value}


def _packet(state: dict) -> dict[str, Any]:
    try:
        envelope = json.loads(state.get("user_input", "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return envelope.get("packet", {}) if isinstance(envelope, dict) and isinstance(envelope.get("packet"), dict) else {}
