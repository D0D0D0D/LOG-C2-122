"""Shared DG consistency brief response contract."""

from __future__ import annotations

from typing import Any


DG_BRIEF_FIELDS = (
    "declared_items",
    "evidence_sources",
    "inconsistency_candidates",
    "inquiry_points",
    "verification_questions",
)
DG_EVIDENCE_SOURCE_KEYS = ("source_id", "doc_type", "quote_span", "provenance")
DG_CLAIM_TEXT_KEY_BY_SECTION = {
    "inconsistency_candidates": "claim",
    "inquiry_points": "question_ref",
}
DG_CLAIM_REQUIRED_KEYS_BY_SECTION = {
    "inconsistency_candidates": ("claim", "evidence_source_id", "rule_ref", "severity", "confidence"),
    "inquiry_points": ("question_ref", "evidence_source_id", "rule_ref", "severity", "confidence"),
}
DG_CLAIM_SECTIONS = tuple(DG_CLAIM_REQUIRED_KEYS_BY_SECTION)


def response_contract() -> dict[str, Any]:
    """Return the closed schema used both for prompting and LLM response checks."""

    return {
        "type": "object",
        "additional_properties": False,
        "required": list(DG_BRIEF_FIELDS),
        "properties": {
            "declared_items": {
                "type": "array",
                "description": "Copy or summarize the provided declared_items; do not determine PSN, UN number, Class, or shipment approval.",
            },
            "evidence_sources": {
                "type": "array",
                "item_required_keys": list(DG_EVIDENCE_SOURCE_KEYS),
                "description": "Use only retrieved_records from this case. Include source_id, doc_type, quote_span, and provenance.case_ref.",
            },
            "inconsistency_candidates": {
                "type": "array",
                "item_required_keys": list(DG_CLAIM_REQUIRED_KEYS_BY_SECTION["inconsistency_candidates"]),
                "constraints": {
                    "evidence_source_id": "string source_id that exists in retrieved_records",
                    "severity": "candidate",
                    "confidence": "JSON float from 0.0 to 1.0",
                },
            },
            "inquiry_points": {
                "type": "array",
                "item_required_keys": list(DG_CLAIM_REQUIRED_KEYS_BY_SECTION["inquiry_points"]),
                "constraints": {
                    "evidence_source_id": "string source_id that exists in retrieved_records",
                    "severity": "candidate",
                    "confidence": "JSON float from 0.0 to 1.0",
                },
            },
            "verification_questions": {
                "type": "array",
                "items": "string questions for a qualified human reviewer",
            },
        },
    }
