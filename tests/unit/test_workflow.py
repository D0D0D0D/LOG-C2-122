"""TC-01..TC-08 and DG-specific workflow/S-3 acceptance coverage."""

from __future__ import annotations

import json

import pytest
from framework.errors import SecurityViolationError, SubgraphError
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from langgraph.checkpoint.memory import InMemorySaver

from src.graph.graph import DangerousGoodsDocConsistencyAgent
from src.nodes.main_node import DangerousGoodsWorkflowGraphNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State


class FakeDB:
    def __init__(self) -> None:
        self.writes: list[dict] = []

    def read_case(self, case_ref: str) -> dict:
        return {
            "declared_items": [
                {"item_ref": "item_1", "un": "UN1203", "class": "3", "packing_group": "II", "quantity": "1 L"}
            ]
        }

    def append(self, record: dict) -> None:
        self.writes.append(record)


class FakeKB:
    def __init__(self) -> None:
        self.writes: list[dict] = []
        self.calls = 0

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        self.calls += 1
        return [
            {
                "source_id": "return_1",
                "check_observation": "数量と包装証明を照合してください。",
                "transport_mode": "air",
                "provenance": {"case_ref": "old_case", "recorded_at": "2026-01-01"},
            }
        ]

    def append(self, record: dict) -> None:
        self.writes.append(record)


def _config(db: FakeDB | None = None, kb: FakeKB | None = None, *, llm=None) -> dict:
    return {
        "max_retry": 3,
        "memory_enabled": True,
        "hitl": {"enabled": False},
        "retrieval": {"top_k": 5},
        "llm": llm,
        "caller_customer_map": {"dg_operator": "customer_a"},
        "db_connections": {"customer_a": db or FakeDB()},
        "kb_connections": {"customer_a": kb or FakeKB()},
        "required_documents_by_transport": {"air": ["sds", "packing_certificate"]},
        "class_packing_rules": {"3": ["I", "II", "III"]},
    }


def _packet(**overrides) -> dict:
    packet = {
        "mode": "invoke",
        "customer_id": "customer_a",
        "case_ref": "case_1",
        "transport_mode": "air",
        "rule_edition_ref": "iata_dgr_2026",
        "declared_items": [
            {
                "item_ref": "item_1",
                "psn": "Gasoline",
                "un": "UN1203",
                "class": "3",
                "packing_group": "II",
                "quantity": "1 L",
            }
        ],
        "evidence_sources": [
            {
                "source_id": "sds_1",
                "doc_type": "sds",
                "quote_span": "UN1203 / Class 3",
                "provenance": {"case_ref": "case_1"},
            },
            {
                "source_id": "pack_1",
                "doc_type": "packing_certificate",
                "quote_span": "Packing group II",
                "provenance": {"case_ref": "case_1"},
            },
        ],
    }
    packet.update(overrides)
    return packet


def _graph(config=None) -> DangerousGoodsDocConsistencyAgent:
    graph = DangerousGoodsDocConsistencyAgent(config or _config())
    graph.compile(checkpointer=InMemorySaver())
    return graph


def _ctx() -> InvocationContext:
    return InvocationContext(
        caller_id="dg_operator", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, thread_id="dg-workflow"
    )


def test_tc01_state_is_flat_and_has_dg_contract_fields():
    annotations = State.__annotations__
    assert {"mode", "case_ref", "validated_input", "consistency_brief", "evidence_map", "requires_human_review"} <= set(
        annotations
    )
    assert not any("secret" in key or "token" in key or "credential" in key for key in annotations)


def test_invoke_returns_closed_brief_from_current_case_sources_only():
    result = _graph().invoke(json.dumps(_packet()), ctx=_ctx())
    assert result["status"] == "success"
    brief = result["output"]
    assert {
        "declared_items",
        "evidence_sources",
        "inconsistency_candidates",
        "inquiry_points",
        "verification_questions",
        "mode",
        "gate_kind",
        "degradation_reason",
    } == set(brief)
    assert brief["mode"] == "invoke" and brief["gate_kind"] == "dg_consistency_brief"
    assert brief["degradation_reason"] == "consistency_llm_not_configured"
    assert {source["source_id"] for source in brief["evidence_sources"]} == {"sds_1", "pack_1"}


def test_customer_mismatch_fails_closed_before_dedicated_kb_read():
    kb = FakeKB()
    result = _graph(_config(kb=kb)).invoke(json.dumps(_packet(customer_id="other")), ctx=_ctx())
    assert result["status"] == "error"
    assert kb.calls == 0


def test_tc08_s1_denies_preprocess_before_execute(monkeypatch):
    node = PreProcessNode(config=_config())
    called = False

    def unexpected(_state):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(node, "execute", unexpected)
    result = node({"caller_trust_level": TrustLevel.ANONYMOUS.value})
    assert result["status"] == "error"
    assert called is False


def test_classification_request_does_not_call_llm_and_requires_human_review():
    class FailingLLM:
        def complete(self, messages):
            raise AssertionError("classification request must not reach the LLM")

    result = _graph(_config(llm=FailingLLM())).invoke(
        json.dumps(_packet(review_request="Please classify this UN item")), ctx=_ctx()
    )
    assert result["status"] == "success"
    assert "一次決定" in result["output"]["verification_questions"][0]


def test_llm_prompt_contains_case_data_schema_and_uses_llm_claim_sections():
    llm_claim = {
        "claim": "LLM found a declared quantity mismatch.",
        "evidence_source_id": "sds_1",
        "rule_ref": "iata_dgr_2026",
        "severity": "candidate",
        "confidence": 0.82,
    }
    llm_inquiry = {
        "question_ref": "confirm_packing_certificate",
        "evidence_source_id": "pack_1",
        "rule_ref": "iata_dgr_2026",
        "severity": "candidate",
        "confidence": 0.76,
    }

    class CapturingLLM:
        def __init__(self) -> None:
            self.messages: list[dict] = []

        def complete(self, messages):
            self.messages = messages
            return {
                "content": json.dumps(
                    {
                        "declared_items": [
                            {
                                "item_ref": "item_1",
                                "psn": "Gasoline",
                                "un": "UN1203",
                                "class": "3",
                                "packing_group": "II",
                                "quantity": "2 L",
                            }
                        ],
                        "evidence_sources": [
                            {
                                "source_id": "sds_1",
                                "doc_type": "sds",
                                "quote_span": "UN1203 / Class 3",
                                "provenance": {"case_ref": "case_1"},
                            },
                            {
                                "source_id": "pack_1",
                                "doc_type": "packing_certificate",
                                "quote_span": "Packing group II",
                                "provenance": {"case_ref": "case_1"},
                            },
                        ],
                        "inconsistency_candidates": [llm_claim],
                        "inquiry_points": [llm_inquiry],
                        "verification_questions": ["Ask a qualified reviewer to confirm the quantity mismatch."],
                    }
                )
            }

    llm = CapturingLLM()
    result = _graph(_config(llm=llm)).invoke(
        json.dumps(
            _packet(
                declared_items=[
                    {
                        "item_ref": "item_1",
                        "psn": "Gasoline",
                        "un": "UN1203",
                        "class": "3",
                        "packing_group": "II",
                        "quantity": "2 L",
                    }
                ],
            )
        ),
        ctx=_ctx(),
    )

    prompt = llm.messages[0]["content"]
    for expected in ("Gasoline", "2 L", "sds_1", "pack_1", "field_diff", "docs_present", "iata_dgr_2026"):
        assert expected in prompt
    for field in (
        "declared_items",
        "evidence_sources",
        "inconsistency_candidates",
        "inquiry_points",
        "verification_questions",
    ):
        assert field in prompt

    assert result["status"] == "success"
    assert "degradation_reason" not in result["output"]
    assert result["output"]["inconsistency_candidates"] == [llm_claim]
    assert result["output"]["inquiry_points"] == [llm_inquiry]


def test_llm_schema_violation_uses_fallback_with_degradation_reason():
    class InvalidSchemaLLM:
        def complete(self, messages):
            return {"content": json.dumps({"declared_items": []})}

    result = _graph(_config(llm=InvalidSchemaLLM())).invoke(json.dumps(_packet()), ctx=_ctx())

    assert result["status"] == "success"
    assert result["output"]["degradation_reason"] == "consistency_llm_schema_violation"
    assert result["output"]["declared_items"][0]["psn"] == "Gasoline"
    assert result["output"]["inconsistency_candidates"] == []


def test_main_node_handles_subgraph_errors_without_raw_inner_log():
    marker = "RAW_PROVIDER_EXCEPTION_MARKER"
    node = DangerousGoodsWorkflowGraphNode(config=_config())
    error = SubgraphError(agent_name="inner", error_log=[f"llm_call_failed: {marker}"], trace_id="trace-1")

    delta = node.on_subgraph_error({}, error)

    assert DangerousGoodsWorkflowGraphNode.error_strategy == "handle"
    assert "error_code" in State.__annotations__
    assert delta["error_code"] == "llm_call_failed"
    assert delta["error_type"] == "SubgraphError"
    assert marker not in json.dumps(delta, ensure_ascii=False)


def test_graph_invoke_surfaces_safe_error_code_without_provider_text():
    marker = "PROVIDER-SECRET-XYZ"

    class ExplodingLLM:
        def complete(self, messages):
            raise RuntimeError(marker)

    result = _graph(_config(llm=ExplodingLLM())).invoke(json.dumps(_packet()), ctx=_ctx())
    print("keys:", sorted(result))

    assert result["status"] == "error"
    assert result["error_code"] == "llm_call_failed"
    assert result["error_type"] == "SubgraphError"
    assert result["error_log"] == list(dict.fromkeys(result["error_log"]))
    assert marker not in json.dumps(result, ensure_ascii=False, default=str)


def test_ingest_and_feedback_write_only_to_dedicated_connections():
    db, kb = FakeDB(), FakeKB()
    graph = _graph(_config(db, kb))
    ingest = {
        "mode": "ingest",
        "customer_id": "customer_a",
        "case_ref": "case_1",
        "ingest_records": [{"source_id": "ret_1", "check_observation": "数量を確認", "transport_mode": "air"}],
    }
    feedback = {
        "mode": "feedback",
        "customer_id": "customer_a",
        "case_ref": "case_1",
        "feedback": {"approved": True, "action": "approve"},
    }
    assert graph.invoke(json.dumps(ingest), ctx=_ctx())["output"]["ingest_summary"]["written_count"] == 1
    assert (
        graph.invoke(
            json.dumps(feedback),
            ctx=InvocationContext(
                caller_id="dg_operator", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, thread_id="dg-feedback"
            ),
        )["output"]["feedback_summary"]["recorded"]
        is True
    )
    assert db.writes and kb.writes


def _s3_result(brief: dict, case_ref: str = "case_1") -> dict:
    records = [
        {"source_id": source["source_id"], "case_ref": case_ref, "quote_span": source.get("quote_span", "span")}
        for source in brief["evidence_sources"]
    ]
    claims = brief["inconsistency_candidates"] + brief["inquiry_points"]
    return {
        "status": "success",
        "gate_kind": "dg_consistency_brief",
        "cited_source_ids": [
            claim["evidence_source_id"] for claim in claims if isinstance(claim.get("evidence_source_id"), str)
        ],
        "retrieved_records": records,
        "evidence_map": [
            {"claim_id": "c", "source_ids": [claim["evidence_source_id"]]}
            for claim in claims
            if isinstance(claim.get("evidence_source_id"), str)
        ],
        "formatted_output": {**brief, "mode": "invoke", "gate_kind": "dg_consistency_brief"},
        "_output_envelope_hook_state": {"case_ref": case_ref},
    }


def _brief() -> dict:
    return {
        "declared_items": [],
        "evidence_sources": [
            {"source_id": "sds_1", "doc_type": "sds", "quote_span": "UN1203", "provenance": {"case_ref": "case_1"}}
        ],
        "inconsistency_candidates": [
            {
                "claim": "数量の差異を確認してください。",
                "evidence_source_id": "sds_1",
                "rule_ref": "iata",
                "severity": "candidate",
                "confidence": 0.5,
            }
        ],
        "inquiry_points": [],
        "verification_questions": [],
    }


def test_s3_accepts_a_valid_closed_brief():
    result = _s3_result(_brief())
    assert PostProcessNode()._extra_security_gate_output(result)["formatted_output"]["mode"] == "invoke"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda brief: brief.update({"shipment_release": "yes"}),  # top-level prohibited key
        lambda brief: brief.update({"releaseDecision": "yes"}),  # alias key (camelCase)
        lambda brief: brief["inconsistency_candidates"][0].update(
            {"Shipment Release": "yes"}
        ),  # nested prohibited key (spaced) -> recursive exclusion
        lambda brief: brief.update(
            {"extra_field": "x"}
        ),  # undeclared key -> closed-schema rejection (allowlist, not denylist)
        lambda brief: brief["inconsistency_candidates"][0].pop("evidence_source_id"),  # claim without evidence
        lambda brief: brief["inconsistency_candidates"][0].update(
            {"evidence_source_id": {"raw": "sds_1"}}
        ),  # non-string source id -> no TypeError
        lambda brief: brief["evidence_sources"][0]["provenance"].update(
            {"case_ref": "other_case"}
        ),  # out-of-case source
    ],
)
def test_tc02_s3_rejects_all_design_rejection_cases(mutator):
    brief = _brief()
    mutator(brief)
    with pytest.raises(SecurityViolationError):
        PostProcessNode()._extra_security_gate_output(_s3_result(brief))
