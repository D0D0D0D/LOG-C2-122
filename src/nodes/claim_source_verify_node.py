"""Stage 4 current-case claim-to-source contract verification."""

from __future__ import annotations

from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.evidence_gate import ProvenanceContract, validate_provenance


class ClaimSourceVerifyNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    _contract = ProvenanceContract(
        gate_kind="dg_consistency_brief", cited_ids_key="cited_source_ids", retrieved_records_key="retrieved_records",
        evidence_map_key="evidence_map", record_id_keys=("source_id",),
    )

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def execute(self, state: dict) -> dict:
        emit_trace_event("dg_claim_source_verified", {"case_ref": state.get("case_ref", "")}, state)
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        brief = state.get("consistency_brief")
        records = state.get("retrieved_records", [])
        if not isinstance(brief, dict) or not isinstance(records, list):
            return {"status": AgentStatus.ERROR.value, "error_log": ["claim_source_verify: missing brief or source records"]}
        case_ref = str(state.get("case_ref", ""))
        source_ids = {record.get("source_id") for record in records if isinstance(record, dict) and record.get("case_ref") == case_ref and record.get("quote_span")}
        declared_source_ids = {
            source.get("source_id") for source in brief.get("evidence_sources", [])
            if isinstance(source, dict) and isinstance(source.get("provenance"), dict)
            and source["provenance"].get("case_ref") == case_ref and source.get("quote_span")
        }
        evidence_map: list[dict] = []
        cited: list[str] = []
        try:
            for section, text_key in (("inconsistency_candidates", "claim"), ("inquiry_points", "question_ref")):
                entries = brief.get(section, [])
                if not isinstance(entries, list):
                    raise SecurityViolationError(f"{section} must be a list")
                for index, entry in enumerate(entries, 1):
                    if not isinstance(entry, dict):
                        raise SecurityViolationError("brief claim is not an object")
                    source_id = entry.get("evidence_source_id")
                    if not isinstance(source_id, str) or source_id not in source_ids or source_id not in declared_source_ids:
                        raise SecurityViolationError("claim must cite a current-case source with quote span")
                    cited.append(source_id)
                    evidence_map.append({"claim_id": f"{section}_{index}", "claim_text": str(entry.get(text_key, "")), "source_ids": [source_id]})
            material = {
                "status": AgentStatus.SUCCESS.value, "gate_kind": "dg_consistency_brief", "cited_source_ids": sorted(set(cited)),
                "retrieved_records": records, "evidence_map": evidence_map,
            }
            validate_provenance(material, self._contract)
        except SecurityViolationError as error:
            return {"status": AgentStatus.ERROR.value, "error_log": [f"claim_source_verify: {error}"]}
        confidence_threshold = float(self._config.get("confidence_threshold", 0.75))
        low_confidence = any(
            isinstance(entry, dict) and isinstance(entry.get("confidence"), (int, float)) and not isinstance(entry.get("confidence"), bool) and entry["confidence"] < confidence_threshold
            for entry in brief.get("inconsistency_candidates", []) if isinstance(brief.get("inconsistency_candidates", []), list)
        )
        return {
            "evidence_map": evidence_map, "cited_source_ids": sorted(set(cited)),
            "requires_human_review": bool(state.get("requires_human_review")) or low_confidence, "status": AgentStatus.SUCCESS.value,
        }
