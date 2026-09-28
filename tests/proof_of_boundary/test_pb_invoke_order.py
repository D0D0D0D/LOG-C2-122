"""PB-6: every concrete node retains the framework's S-1/S-2/execute/S-3 order."""

from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
from typing import ClassVar

from framework.nodes.base_node import BaseNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel


_CALLER_ID = "operator"
_CUSTOMER_ID = "customer_a"
_CASE_REF = "case_1"


def _node_classes() -> list[type]:
    from framework.nodes.base_node import BaseNode
    import src.nodes as package

    found = []
    for _, modname, _ in pkgutil.walk_packages(package.__path__, prefix="src.nodes."):
        module = importlib.import_module(modname)
        found.extend(
            value
            for value in vars(module).values()
            if isinstance(value, type)
            and issubclass(value, BaseNode)
            and value is not BaseNode
            and value.__module__ == modname
            and not inspect.isabstract(value)
        )
    return found


def _seeded_config() -> dict:
    """Use the same dedicated DB/KB fakes and caller binding as integration invoke."""
    from tests.conftest import DedicatedDB, DedicatedKB

    db, kb = DedicatedDB(), DedicatedKB()
    return {
        "max_retry": 3,
        "memory_enabled": True,
        "hitl": {"enabled": False},
        "retrieval": {"top_k": 3},
        "llm": None,
        "caller_customer_map": {_CALLER_ID: _CUSTOMER_ID},
        "db_connections": {_CUSTOMER_ID: db},
        "kb_connections": {_CUSTOMER_ID: kb},
    }


def _invoke_packet() -> dict:
    """Successful invoke packet from tests/integration/test_dg_workflow.py."""
    return {
        "mode": "invoke",
        "customer_id": _CUSTOMER_ID,
        "case_ref": _CASE_REF,
        "transport_mode": "air",
        "rule_edition_ref": "iata_approved",
        "declared_items": [],
        "evidence_sources": [
            {
                "source_id": "sds_1",
                "doc_type": "sds",
                "quote_span": "declared span",
                "provenance": {"case_ref": _CASE_REF},
            }
        ],
    }


def _ingest_packet() -> dict:
    """Successful ingest packet from tests/integration/test_dg_workflow.py."""
    return {
        "mode": "ingest",
        "customer_id": _CUSTOMER_ID,
        "case_ref": _CASE_REF,
        "ingest_records": [
            {"source_id": "return_1", "check_observation": "数量を照合", "transport_mode": "air"}
        ],
    }


def _feedback_packet() -> dict:
    """Successful feedback packet from tests/integration/test_dg_workflow.py."""
    return {
        "mode": "feedback",
        "customer_id": _CUSTOMER_ID,
        "case_ref": _CASE_REF,
        "feedback": {"approved": True, "action": "approve"},
    }


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def _base_state(node_cls: type) -> dict:
    return {
        "caller_trust_level": node_cls.required_trust_level.value,
        "caller_id": _CALLER_ID,
        "correlation_id": "pb6-invoke-order-test",
        "session_id": "pb6-session",
        "thread_id": "pb6-thread",
        "trace_id": "pb6-trace",
        "hitl_allowed": False,
        "input_context": {"permissions": []},
        "status": AgentStatus.SUCCESS.value,
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }


def _outer_pre_process_state(node_cls: type) -> dict:
    state = _base_state(node_cls)
    state["user_input"] = _json(_invoke_packet())
    return state


def _inner_envelope(packet: dict) -> dict:
    return {"mode": packet["mode"], "packet": packet}


def _inner_start_state(node_cls: type, packet: dict) -> dict:
    state = _base_state(node_cls)
    state.update(
        {
            "mode": packet["mode"],
            "case_ref": packet["case_ref"],
            "user_input": _json(_inner_envelope(packet)),
        }
    )
    return state


def _instantiate(node_cls: type):
    config = _seeded_config()
    try:
        return node_cls(config=config)
    except TypeError:
        return node_cls()


def _merge_success(node, state: dict) -> dict:
    result = node(dict(state))
    assert result.get("status") == AgentStatus.SUCCESS.value, (
        f"{node.__class__.__name__} setup invoke failed: {result}"
    )
    return {**state, **result}


def _state_after_outer_pre_process(node_cls: type) -> dict:
    from src.nodes.pre_process_node import PreProcessNode

    state = _outer_pre_process_state(PreProcessNode)
    return {**_base_state(node_cls), **state, **_merge_success(_instantiate(PreProcessNode), state)}


def _state_after_inner_deterministic(node_cls: type) -> dict:
    from src.nodes.deterministic_check_node import DeterministicCheckNode

    state = _inner_start_state(DeterministicCheckNode, _invoke_packet())
    return {**_base_state(node_cls), **_merge_success(_instantiate(DeterministicCheckNode), state)}


def _state_after_inner_recall(node_cls: type) -> dict:
    from src.nodes.kb_recall_node import KBRecallNode

    state = _state_after_inner_deterministic(KBRecallNode)
    return {**_base_state(node_cls), **state, **_merge_success(_instantiate(KBRecallNode), state)}


def _state_after_inner_llm(node_cls: type) -> dict:
    from src.nodes.rule_consistency_llm_node import RuleConsistencyLLMNode

    state = _state_after_inner_recall(RuleConsistencyLLMNode)
    return {**_base_state(node_cls), **state, **_merge_success(_instantiate(RuleConsistencyLLMNode), state)}


def _state_after_outer_main(node_cls: type) -> dict:
    from src.nodes.main_node import DangerousGoodsWorkflowGraphNode

    state = _state_after_outer_pre_process(DangerousGoodsWorkflowGraphNode)
    return {**_base_state(node_cls), **state, **_merge_success(_instantiate(DangerousGoodsWorkflowGraphNode), state)}


def _valid_invoke_state(node_cls: type) -> dict:
    """Build the predecessor-shaped state needed to run the real node body."""
    from src.nodes.accumulate_feedback_node import AccumulateFeedbackNode
    from src.nodes.accumulate_ingest_node import AccumulateIngestNode
    from src.nodes.claim_source_verify_node import ClaimSourceVerifyNode
    from src.nodes.deterministic_check_node import DeterministicCheckNode
    from src.nodes.kb_recall_node import KBRecallNode
    from src.nodes.main_node import DangerousGoodsWorkflowGraphNode
    from src.nodes.post_process_node import PostProcessNode
    from src.nodes.pre_process_node import PreProcessNode
    from src.nodes.rule_consistency_llm_node import RuleConsistencyLLMNode

    if node_cls is PreProcessNode:
        return _outer_pre_process_state(node_cls)
    if node_cls is DangerousGoodsWorkflowGraphNode:
        return _state_after_outer_pre_process(node_cls)
    if node_cls is PostProcessNode:
        return _state_after_outer_main(node_cls)
    if node_cls is DeterministicCheckNode:
        return _inner_start_state(node_cls, _invoke_packet())
    if node_cls is KBRecallNode:
        return _state_after_inner_deterministic(node_cls)
    if node_cls is RuleConsistencyLLMNode:
        return _state_after_inner_recall(node_cls)
    if node_cls is ClaimSourceVerifyNode:
        return _state_after_inner_llm(node_cls)
    if node_cls is AccumulateIngestNode:
        return _inner_start_state(node_cls, _ingest_packet())
    if node_cls is AccumulateFeedbackNode:
        return _inner_start_state(node_cls, _feedback_packet())
    raise AssertionError(f"PB-6 has no valid predecessor-shaped state for {node_cls.__name__}")


def _outer_order(order: list[str]) -> list[str]:
    """Hide lifecycle events emitted by a subgraph inside GraphNode.execute()."""
    if "execute" not in order:
        return list(order)
    start = order.index("execute")
    try:
        end = order.index("security_gate_output", start)
    except ValueError:
        return list(order)
    return order[: start + 1] + order[end:]


# ── PB-6 S-1 negative boundary（2026-08-17 正本追従で移植）──────────────
# 正本 tests/proof_of_boundary/test_pb_invoke_order.py より。テンプレのノード構成に
# 依存しない常設フィクスチャで「不足 trust では execute() に到達しない」ことを証明する
# （TC-08 の要求。既存の全ノード走査テストは触っていない）。


class _PrivilegedTrustGateFixture(BaseNode):
    """Always-present privileged node used to prove the S-1 negative boundary."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _security_gate_input(self, state):
        return state

    def execute(self, state):
        return {"status": "success"}

    def _security_gate_output(self, result):
        return result


def _trust_predecessor(required: TrustLevel) -> TrustLevel:
    """Return a lower valid trust level; fail loudly if the framework adds one."""
    predecessors = {
        TrustLevel.VERIFIED_EXTERNAL: TrustLevel.ANONYMOUS,
        TrustLevel.INTERNAL: TrustLevel.VERIFIED_EXTERNAL,
    }
    try:
        return predecessors[required]
    except KeyError as exc:
        raise AssertionError(f"no lower trust level defined for {required!r}") from exc


def test_pb6_framework_calls_security_gates_around_every_node(monkeypatch):
    import framework.nodes.base_node as base

    failures = []
    for node_cls in _node_classes():
        instance = _instantiate(node_cls)
        state = _valid_invoke_state(node_cls)

        order = []
        monkeypatch.setattr(
            base,
            "emit_trace_event",
            lambda event, _payload, _state, _o=order: _o.append(f"event:{event}"),
        )
        for name, label in (
            ("_security_gate_input", "security_gate_input"),
            ("execute", "execute"),
            ("_security_gate_output", "security_gate_output"),
        ):
            original = getattr(node_cls, name)

            def wrapped(self, arg, _original=original, _label=label, _order=order):
                _order.append(_label)
                return _original(self, arg)

            monkeypatch.setattr(node_cls, name, wrapped)

        result = instance(state)
        order = _outer_order(order)
        expected = [
            "event:node_start",
            "security_gate_input",
            "execute",
            "security_gate_output",
            "event:node_complete",
        ]
        if result.get("status") != AgentStatus.SUCCESS.value or order != expected:
            failures.append(
                f"{node_cls.__name__}: invoke order violation.\n"
                f"expected: {expected}\n"
                f"actual:   {order}\n"
                f"result:   {result}"
            )
    assert not failures, "\n".join(failures)


def test_s1_denial_refuses_execution_before_execute(monkeypatch):
    """TC-08: an always-present privileged node proves the negative S-1 path."""
    import framework.nodes.base_node as base_node_module

    events: list[str] = []
    execute_calls: list[object] = []
    monkeypatch.setattr(
        base_node_module,
        "emit_trace_event",
        lambda event_type, _payload, _state: events.append(event_type),
    )
    original_execute = _PrivilegedTrustGateFixture.execute

    def spy_execute(self, state):
        execute_calls.append(state)
        return original_execute(self, state)

    monkeypatch.setattr(_PrivilegedTrustGateFixture, "execute", spy_execute)
    result = _PrivilegedTrustGateFixture()(
        {
            "caller_trust_level": _trust_predecessor(
                _PrivilegedTrustGateFixture.required_trust_level
            ).value,
            "correlation_id": "tc08-s1-denial",
        }
    )

    assert result["status"] == "error"
    assert "S-1 trust gate denied" in result["error_log"][0]
    assert events == ["s1_denied"]
    assert not execute_calls
