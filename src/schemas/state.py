"""JSON-serialisable state for the dangerous-goods consistency workflow."""

from __future__ import annotations

from typing import NotRequired

from framework.schemas.agent_state import AgentState


class State(AgentState, total=False):
    """Flat template state; bodies, connections, and credentials stay out of it."""

    mode: str
    case_ref: str
    validated_input: str
    transport_mode: NotRequired[str]
    rule_edition_ref: NotRequired[str]
    review_request: NotRequired[str]
    declared_items: NotRequired[list[dict]]
    docs_present: NotRequired[dict]
    field_diff: NotRequired[list[dict]]
    recall_refs: NotRequired[list[dict]]
    consistency_brief: NotRequired[dict]
    evidence_map: NotRequired[list[dict]]
    cited_source_ids: NotRequired[list[str]]
    retrieved_records: NotRequired[list[dict]]
    requires_human_review: NotRequired[bool]
    ingest_summary: NotRequired[dict]
    feedback_summary: NotRequired[dict]
    payload_ref: NotRequired[str]
    error_code: NotRequired[str]  # type: ignore[valid-type]
    error_type: NotRequired[str]  # type: ignore[valid-type]
    degradation_reason: NotRequired[str]  # type: ignore[valid-type]
