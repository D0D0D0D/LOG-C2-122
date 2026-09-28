"""Outer S-2 caller/customer binding and minimal DG packet projection."""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.service import resolve_customer_db, resolve_customer_for_caller, resolve_customer_kb


_SAFE_ID = re.compile(r"^[0-9A-Za-z_-]+$")
_MODES = frozenset({"invoke", "ingest", "feedback"})
_ITEM_KEYS = frozenset({"item_ref", "psn", "un", "class", "packing_group", "quantity"})
_SOURCE_KEYS = frozenset({"source_id", "doc_type", "quote_span", "provenance"})
_PACKET_KEYS = frozenset({
    "mode", "customer_id", "case_ref", "transport_mode", "rule_edition_ref", "declared_items",
    "evidence_sources", "review_request", "ingest_records", "feedback", "payload_ref",
})


class PreProcessNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def _extra_security_gate_input(self, state: dict) -> dict:
        packet = _parse_packet(state.get("user_input"))
        caller_customer = resolve_customer_for_caller(self._config, str(state.get("caller_id", "")))
        valid = (
            isinstance(packet, dict)
            and set(packet).issubset(_PACKET_KEYS)
            and packet.get("mode") in _MODES
            and isinstance(packet.get("customer_id"), str)
            and packet.get("customer_id") == caller_customer
            and isinstance(packet.get("case_ref"), str)
            and bool(_SAFE_ID.fullmatch(packet.get("case_ref", "")))
            and resolve_customer_db(self._config, caller_customer or "") is not None
            and resolve_customer_kb(self._config, caller_customer or "") is not None
            and _packet_shape_is_safe(packet)
        )
        if valid:
            return state
        return {
            **state,
            "status": AgentStatus.ERROR.value,
            "error_log": ["pre_process: packet, customer binding, or dedicated connection was rejected"],
        }

    def execute(self, state: dict) -> dict:
        emit_trace_event("dg_packet_validated", {"caller_bound": bool(state.get("caller_id"))}, state)
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        packet = _parse_packet(state.get("user_input"))
        assert isinstance(packet, dict)  # established by S-2
        safe = _safe_packet(packet)
        return {
            "mode": safe["mode"],
            "case_ref": safe["case_ref"],
            "validated_input": json.dumps(safe, ensure_ascii=False, default=str, sort_keys=True),
            "payload_ref": safe.get("payload_ref", ""),
            "status": AgentStatus.SUCCESS.value,
        }


def _parse_packet(raw: object) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else None
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _packet_shape_is_safe(packet: dict[str, Any]) -> bool:
    mode = packet.get("mode")
    if mode == "invoke":
        return (
            packet.get("transport_mode") in {"air", "sea", "road"}
            and isinstance(packet.get("rule_edition_ref"), str)
            and bool(packet["rule_edition_ref"])
            and isinstance(packet.get("declared_items"), list)
            and isinstance(packet.get("evidence_sources"), list)
            and all(isinstance(item, dict) and set(item).issubset(_ITEM_KEYS) for item in packet["declared_items"])
            and all(_safe_source(source) for source in packet["evidence_sources"])
        )
    if mode == "ingest":
        return isinstance(packet.get("ingest_records", []), list) and all(isinstance(item, dict) for item in packet.get("ingest_records", []))
    feedback = packet.get("feedback")
    return isinstance(feedback, dict) and feedback.get("approved") is True and feedback.get("action") in {"approve", "edit", "reject"}


def _safe_source(source: object) -> bool:
    if not isinstance(source, dict) or not set(source).issubset(_SOURCE_KEYS):
        return False
    provenance = source.get("provenance")
    return (
        isinstance(source.get("source_id"), str)
        and bool(_SAFE_ID.fullmatch(source["source_id"]))
        and isinstance(source.get("doc_type"), str)
        and isinstance(source.get("quote_span"), str)
        and 0 < len(source["quote_span"]) <= 512
        and isinstance(provenance, dict)
    )


def _safe_packet(packet: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {key: packet[key] for key in ("mode", "customer_id", "case_ref", "payload_ref") if key in packet}
    if packet["mode"] == "invoke":
        safe.update({
            "transport_mode": packet["transport_mode"],
            "rule_edition_ref": packet["rule_edition_ref"],
            "declared_items": [{key: str(item.get(key, "")) for key in _ITEM_KEYS} for item in packet["declared_items"]],
            "evidence_sources": [
                {"source_id": source["source_id"], "doc_type": source["doc_type"], "quote_span": source["quote_span"], "provenance": dict(source["provenance"])}
                for source in packet["evidence_sources"]
            ],
            "review_request": str(packet.get("review_request", ""))[:256],
        })
    elif packet["mode"] == "ingest":
        safe["ingest_records"] = [dict(item) for item in packet.get("ingest_records", [])]
    else:
        safe["feedback"] = {"approved": True, "action": packet["feedback"]["action"]}
    return safe
