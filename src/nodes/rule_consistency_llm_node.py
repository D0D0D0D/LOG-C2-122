"""Stage 3 evidence-bounded DG consistency review with deterministic fallback."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.dg_consistency_contract import (
    DG_BRIEF_FIELDS,
    DG_CLAIM_REQUIRED_KEYS_BY_SECTION,
    DG_CLAIM_SECTIONS,
    DG_CLAIM_TEXT_KEY_BY_SECTION,
    DG_EVIDENCE_SOURCE_KEYS,
    response_contract,
)


_DECISION_REQUEST = re.compile(r"classification|classify|分類|psn|\bun\b", re.IGNORECASE)
_FIELDS = frozenset(DG_BRIEF_FIELDS)


class RuleConsistencyLLMNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def execute(self, state: dict) -> dict:
        emit_trace_event(
            "dg_rule_consistency_reviewed", {"source_count": len(state.get("retrieved_records", []))}, state
        )
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        brief = self._fallback(state)
        requested = _decision_request_search(state.get("review_request", ""))
        if requested:
            brief["verification_questions"] = ["PSN、UN番号、Class の一次決定は有資格者に確認してください。"]
            return {"consistency_brief": brief, "requires_human_review": True, "status": AgentStatus.SUCCESS.value}
        llm = self._config.get("llm")
        degradation = ""
        if llm is None:
            degradation = "consistency_llm_not_configured"
        elif hasattr(llm, "complete"):
            try:
                response = llm.complete([{"role": "user", "content": build_consistency_prompt(state)}])
                content = response.get("content") if isinstance(response, dict) else None
                candidate = json.loads(content) if isinstance(content, str) else None
                validated = _validate_llm_candidate(candidate, state)
                if validated is not None:
                    brief = validated
                else:
                    degradation = "consistency_llm_schema_violation"
            except json.JSONDecodeError:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_code": "llm_output_parse_failed",
                    "error_type": "JSONDecodeError",
                    "error_log": ["rule_consistency_llm: llm_output_parse_failed"],
                }
            except Exception as exc:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_code": "llm_call_failed",
                    "error_type": type(exc).__name__,
                    "error_log": ["rule_consistency_llm: llm_call_failed"],
                }
        else:
            degradation = "consistency_llm_not_configured"
        result: dict[str, Any] = {
            "consistency_brief": brief,
            "requires_human_review": bool(state.get("requires_human_review")),
            "status": AgentStatus.SUCCESS.value,
        }
        if degradation:
            result["degradation_reason"] = degradation
        return result

    @staticmethod
    def _fallback(state: dict) -> dict[str, Any]:
        sources = [
            {key: record[key] for key in ("source_id", "doc_type", "quote_span") if key in record}
            | {"provenance": {"case_ref": record.get("case_ref", "")}}
            for record in state.get("retrieved_records", [])
            if isinstance(record, dict)
        ]
        candidates = []
        questions = []
        if state.get("field_diff") and sources:
            candidates.append(
                {
                    "claim": "既申告情報と裏付け書類の項目差異を確認してください。",
                    "evidence_source_id": sources[0]["source_id"],
                    "rule_ref": str(state.get("rule_edition_ref", "")),
                    "severity": "candidate",
                    "confidence": 0.5,
                }
            )
        if state.get("docs_present", {}).get("missing_documents"):
            questions.append("不足している裏付け書類を有資格者が確認してください。")
        return {
            "declared_items": list(state.get("declared_items", [])),
            "evidence_sources": sources,
            "inconsistency_candidates": candidates,
            "inquiry_points": [],
            "verification_questions": questions,
        }


def build_consistency_prompt(state: dict[str, Any]) -> str:
    payload = {
        "response_contract": _response_contract(),
        "case_data": _case_prompt_data(state),
        "constraints": [
            "Return only one JSON object. Do not wrap it in markdown.",
            "Use exactly the five top-level fields in response_contract.required; do not add extra top-level fields.",
            "Do not make a final PSN, UN number, Class, packing-group, or shipment approval decision.",
            "Use only evidence_source_id values that appear in case_data.retrieved_records.",
        ],
    }
    return (
        "You are reviewing dangerous-goods document consistency for a qualified human reviewer.\n"
        "Build a non-conclusive consistency_brief from the provided case data and closed response contract.\n"
        f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
    )


def _response_contract() -> dict[str, Any]:
    return response_contract()


def _case_prompt_data(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_ref": str(state.get("case_ref", "")),
        "declared_items": state.get("declared_items", []),
        "retrieved_records": _prompt_records(state.get("retrieved_records", [])),
        "field_diff": state.get("field_diff", []),
        "docs_present": state.get("docs_present", {}),
        "rule_edition_ref": str(state.get("rule_edition_ref", "")),
    }


def _prompt_records(records: object) -> list[dict[str, str]]:
    safe: list[dict[str, str]] = []
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        source_id = record.get("source_id")
        doc_type = record.get("doc_type")
        quote_span = record.get("quote_span")
        if isinstance(source_id, str) and isinstance(doc_type, str) and isinstance(quote_span, str):
            safe.append({"source_id": source_id, "doc_type": doc_type, "quote_span": quote_span})
    return safe


def _validate_llm_candidate(candidate: object, state: dict[str, Any]) -> dict[str, Any] | None:
    contract = _response_contract()
    expected_fields = set(contract.get("required", []))
    if not isinstance(candidate, dict) or set(candidate) != expected_fields or expected_fields != _FIELDS:
        return None
    if not isinstance(candidate.get("declared_items"), list) or not _strings(candidate.get("verification_questions")):
        return None
    source_ids = _valid_output_sources(candidate.get("evidence_sources"), state)
    if source_ids is None:
        return None
    for section in DG_CLAIM_SECTIONS:
        entries = candidate.get(section)
        if not isinstance(entries, list):
            return None
        required_keys = set(DG_CLAIM_REQUIRED_KEYS_BY_SECTION[section])
        text_key = DG_CLAIM_TEXT_KEY_BY_SECTION[section]
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != required_keys:
                return None
            if not isinstance(entry.get(text_key), str) or not entry[text_key]:
                return None
            if not isinstance(entry.get("evidence_source_id"), str) or entry["evidence_source_id"] not in source_ids:
                return None
            if not isinstance(entry.get("rule_ref"), str) or not entry["rule_ref"]:
                return None
            if entry.get("severity") != "candidate":
                return None
            confidence = entry.get("confidence")
            if not isinstance(confidence, float) or not 0.0 <= confidence <= 1.0:
                return None
    return candidate


def _valid_output_sources(value: object, state: dict[str, Any]) -> set[str] | None:
    if not isinstance(value, list):
        return None
    known = {record["source_id"] for record in _prompt_records(state.get("retrieved_records", []))}
    case_ref = str(state.get("case_ref", ""))
    source_ids: set[str] = set()
    required_keys = set(DG_EVIDENCE_SOURCE_KEYS)
    for source in value:
        if not isinstance(source, dict) or set(source) != required_keys:
            return None
        source_id = source.get("source_id")
        if not isinstance(source_id, str) or source_id not in known:
            return None
        if (
            not isinstance(source.get("doc_type"), str)
            or not isinstance(source.get("quote_span"), str)
            or not source["quote_span"]
        ):
            return None
        provenance = source.get("provenance")
        if not isinstance(provenance, dict) or provenance.get("case_ref") != case_ref:
            return None
        source_ids.add(source_id)
    return source_ids


def _strings(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _decision_request_search(value: object) -> bool:
    return bool(_DECISION_REQUEST.search(unicodedata.normalize("NFKC", str(value)).casefold()))
