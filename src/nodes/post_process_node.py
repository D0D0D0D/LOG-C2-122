"""Outer output envelope and S-3 DG professional-boundary gate."""

from __future__ import annotations

import unicodedata
from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.evidence_gate import ProvenanceContract, validate_provenance
from src.output_envelope import OutputEnvelopeNode
from src.schemas.dg_consistency_contract import (
    DG_BRIEF_FIELDS,
    DG_CLAIM_REQUIRED_KEYS_BY_SECTION,
    DG_CLAIM_SECTIONS,
    DG_CLAIM_TEXT_KEY_BY_SECTION,
    DG_EVIDENCE_SOURCE_KEYS,
)


class PostProcessNode(OutputEnvelopeNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    allowed_modes: ClassVar[tuple[str, ...]] = ("invoke", "ingest", "feedback")
    gate_kind_by_mode: ClassVar[dict[str, str]] = {
        "invoke": "dg_consistency_brief",
        "ingest": "ingest",
        "feedback": "feedback",
    }
    required_fields_by_mode: ClassVar[dict[str, tuple[str, ...]]] = {
        "invoke": ("mode", "gate_kind", *DG_BRIEF_FIELDS),
        "ingest": ("mode", "gate_kind", "ingest_summary"),
        "feedback": ("mode", "gate_kind", "feedback_summary"),
    }
    _contract = ProvenanceContract(
        gate_kind="dg_consistency_brief",
        cited_ids_key="cited_source_ids",
        retrieved_records_key="retrieved_records",
        evidence_map_key="evidence_map",
        record_id_keys=("source_id",),
    )
    _brief_keys = frozenset(DG_BRIEF_FIELDS)
    _brief_meta_keys = frozenset({"mode", "gate_kind", "degradation_reason"})
    _prohibited_keys = frozenset(
        {
            "classificationfinal",
            "classificationdecision",
            "dangerousgoodsclassification",
            "dgclassification",
            "shipmentrelease",
            "releasedecision",
            "shippingdecision",
            "shipmentdecision",
            "出荷可否",
            "分類確定",
            "危険品分類",
        }
    )

    def execute(self, state: dict) -> dict:
        emit_trace_event("dg_output_formatted", {"mode": state.get("mode", "")}, state)
        result = super().execute(state)
        if result.get("status") == AgentStatus.ERROR.value:
            return result
        result.update(
            {
                "gate_kind": self.gate_kind_by_mode.get(str(state.get("mode", "")), ""),
                "cited_source_ids": state.get("cited_source_ids", []),
                "retrieved_records": state.get("retrieved_records", []),
                "evidence_map": state.get("evidence_map", []),
            }
        )
        return result

    def format_invoke(self, state: dict) -> dict[str, Any]:
        brief = state.get("consistency_brief")
        return dict(brief) if isinstance(brief, dict) else {}

    def format_ingest(self, state: dict) -> dict[str, Any]:
        return {"ingest_summary": state.get("ingest_summary", {})}

    def format_feedback(self, state: dict) -> dict[str, Any]:
        return {"feedback_summary": state.get("feedback_summary", {})}

    def _extra_security_gate_output(self, result: dict) -> dict:
        if result.get("status") == AgentStatus.ERROR.value:
            return super()._extra_security_gate_output(result)
        if result.get("formatted_output", {}).get("mode") == "invoke":
            validate_provenance(result, self._contract)
            self._assert_closed_brief(result["formatted_output"], result.get("_output_envelope_hook_state", {}))
        return super()._extra_security_gate_output(result)

    def _sanitize_client_echoes(self, formatted: dict[str, Any]) -> dict[str, Any]:
        if formatted.get("mode") == "invoke":
            self._assert_closed_brief(formatted, {})
        return super()._sanitize_client_echoes(formatted)

    def _assert_closed_brief(self, formatted: dict[str, Any], state: object) -> None:
        if not isinstance(formatted, dict):
            raise SecurityViolationError("S-3 DG: output is not an object")
        public_keys = set(formatted) - self._brief_meta_keys
        if public_keys != self._brief_keys:
            raise SecurityViolationError("S-3 DG: consistency_brief must use the closed five-field schema")
        self._reject_prohibited_keys(formatted)
        sources = formatted.get("evidence_sources")
        if not isinstance(sources, list):
            raise SecurityViolationError("S-3 DG: evidence_sources must be a list")
        current_case = state.get("case_ref", "") if isinstance(state, dict) else ""
        source_ids: set[str] = set()
        for source in sources:
            if not isinstance(source, dict) or set(source) != set(DG_EVIDENCE_SOURCE_KEYS):
                raise SecurityViolationError("S-3 DG: source schema is invalid")
            if (
                not isinstance(source.get("source_id"), str)
                or not isinstance(source.get("quote_span"), str)
                or not source["quote_span"]
            ):
                raise SecurityViolationError("S-3 DG: source ID and quote span are required")
            provenance = source.get("provenance")
            if not isinstance(provenance, dict) or (current_case and provenance.get("case_ref") != current_case):
                raise SecurityViolationError("S-3 DG: source is not from the current case")
            source_ids.add(source["source_id"])
        # Non-conclusive output is enforced structurally: the closed five-field
        # schema + prohibited-key exclusion + severity=="candidate" fixed value +
        # claim->source linkage. There is deliberately NO free-text conclusion
        # denylist — a denylist of banned phrases is bypassable by paraphrase and
        # gives false assurance (policy: allowlist is the guarantee, not a
        # phrase blacklist).
        for section in DG_CLAIM_SECTIONS:
            entries = formatted.get(section)
            if not isinstance(entries, list):
                raise SecurityViolationError("S-3 DG: claim section must be a list")
            required_keys = set(DG_CLAIM_REQUIRED_KEYS_BY_SECTION[section])
            text_key = DG_CLAIM_TEXT_KEY_BY_SECTION[section]
            for entry in entries:
                if not isinstance(entry, dict) or set(entry) != required_keys:
                    raise SecurityViolationError("S-3 DG: claim schema is invalid")
                evidence_source_id = entry.get("evidence_source_id")
                if not isinstance(evidence_source_id, str) or evidence_source_id not in source_ids:
                    raise SecurityViolationError("S-3 DG: every claim requires an existing evidence_source_id")
                if not isinstance(entry.get(text_key), str) or not entry[text_key]:
                    raise SecurityViolationError("S-3 DG: claim text reference is required")
                if not isinstance(entry.get("rule_ref"), str) or not entry["rule_ref"]:
                    raise SecurityViolationError("S-3 DG: rule_ref is required")
                if (
                    entry.get("severity") != "candidate"
                    or not isinstance(entry.get("confidence"), float)
                    or not 0.0 <= entry["confidence"] <= 1.0
                ):
                    raise SecurityViolationError("S-3 DG: candidate severity/confidence is invalid")
        if not isinstance(formatted.get("declared_items"), list) or not isinstance(
            formatted.get("verification_questions"), list
        ):
            raise SecurityViolationError("S-3 DG: fixed list fields are required")

    def _reject_prohibited_keys(self, value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                normal = unicodedata.normalize("NFKC", str(key)).casefold().replace("-", "_").replace(" ", "_")
                collapsed = normal.replace("_", "")
                if normal in self._prohibited_keys or collapsed in self._prohibited_keys:
                    raise SecurityViolationError(
                        "S-3 DG: classification or shipment-release key is structurally prohibited"
                    )
                self._reject_prohibited_keys(nested)
        elif isinstance(value, list):
            for nested in value:
                self._reject_prohibited_keys(nested)
