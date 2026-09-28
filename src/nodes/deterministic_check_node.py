"""Stage 1 deterministic DG document checks on the dedicated DB connection."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.service import read_case, resolve_customer_db


class DeterministicCheckNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def execute(self, state: dict) -> dict:
        emit_trace_event("dg_deterministic_check", {"case_ref": state.get("case_ref", "")}, state)
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        packet = _packet(state)
        customer_id = str(packet.get("customer_id", ""))
        db = resolve_customer_db(self._config, customer_id)
        if db is None:
            return {"status": AgentStatus.ERROR.value, "error_log": ["deterministic_check: no dedicated customer DB connection"]}
        case_ref = packet.get("case_ref")
        if not isinstance(case_ref, str) or not case_ref:
            return {"status": AgentStatus.ERROR.value, "error_log": ["deterministic_check: case_ref is required"]}
        try:
            case_data = read_case(db, case_ref) or {}
        except Exception:
            return {"status": AgentStatus.ERROR.value, "error_log": ["deterministic_check: customer DB read failed"]}
        sources = packet.get("evidence_sources", [])
        present = sorted({str(source.get("doc_type", "")) for source in sources if isinstance(source, dict)})
        required_by_mode = self._config.get("required_documents_by_transport", {})
        required = required_by_mode.get(packet.get("transport_mode"), ["sds", "packing_certificate"])
        required = sorted(str(item) for item in required if isinstance(item, str))
        missing = [item for item in required if item not in present]
        diffs = _field_differences(packet.get("declared_items", []), case_data, self._config.get("class_packing_rules", {}))
        records = [
            {
                "source_id": source["source_id"], "case_ref": case_ref, "customer_id": customer_id,
                "doc_type": source["doc_type"], "quote_span": source["quote_span"],
            }
            for source in sources if isinstance(source, dict)
        ]
        return {
            "mode": "invoke", "case_ref": case_ref, "transport_mode": packet.get("transport_mode", ""),
            "rule_edition_ref": packet.get("rule_edition_ref", ""), "declared_items": list(packet.get("declared_items", [])),
            "review_request": packet.get("review_request", ""),
            "docs_present": {"required_documents": required, "present_documents": present, "missing_documents": missing},
            "field_diff": diffs, "retrieved_records": records,
            "requires_human_review": bool(missing or diffs), "status": AgentStatus.SUCCESS.value,
        }


def _packet(state: dict) -> dict[str, Any]:
    try:
        envelope = json.loads(state.get("user_input", "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return envelope.get("packet", {}) if isinstance(envelope, dict) and isinstance(envelope.get("packet"), dict) else {}


def _field_differences(items: object, case_data: dict[str, Any], rules: object) -> list[dict]:
    differences: list[dict] = []
    expected = case_data.get("declared_items") if isinstance(case_data.get("declared_items"), list) else []
    expected_by_ref = {str(item.get("item_ref")): item for item in expected if isinstance(item, dict)}
    allowed_pairs = rules if isinstance(rules, dict) else {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("item_ref", ""))
        baseline = expected_by_ref.get(ref)
        if baseline:
            for field in ("un", "class", "packing_group", "quantity"):
                if str(baseline.get(field, "")) != str(item.get(field, "")):
                    differences.append({"item_ref": ref, "field": field, "kind": "declared_value_difference"})
        allowed = allowed_pairs.get(str(item.get("class", "")))
        if isinstance(allowed, list) and item.get("packing_group") not in allowed:
            differences.append({"item_ref": ref, "field": "packing_group", "kind": "configured_combination_check"})
    return differences
