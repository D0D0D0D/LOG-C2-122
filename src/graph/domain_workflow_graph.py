"""Inner three-mode dangerous-goods document-consistency workflow."""

from __future__ import annotations

import json

from langgraph.graph import END, START

from framework.errors import ConfigError
from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus

from src.nodes.accumulate_feedback_node import AccumulateFeedbackNode
from src.nodes.accumulate_ingest_node import AccumulateIngestNode
from src.nodes.claim_source_verify_node import ClaimSourceVerifyNode
from src.nodes.deterministic_check_node import DeterministicCheckNode
from src.nodes.kb_recall_node import KBRecallNode
from src.nodes.rule_consistency_llm_node import RuleConsistencyLLMNode
from src.schemas.state import State


class DangerousGoodsWorkflowGraph(BaseGraph):
    @property
    def name(self) -> str:
        return "dg_doc_consistency_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        if "retrieval" not in self.config or "llm" not in self.config:
            raise ConfigError("DangerousGoodsWorkflowGraph requires retrieval and llm config keys")
        retrieval = self.config["retrieval"]
        if not isinstance(retrieval, dict) or not isinstance(retrieval.get("top_k"), int) or isinstance(retrieval["top_k"], bool) or retrieval["top_k"] < 1:
            raise ConfigError("DangerousGoodsWorkflowGraph retrieval.top_k must be a positive integer")

    def register_nodes(self) -> None:
        self._nodes = {
            "deterministic_check": DeterministicCheckNode(config=self.config), "kb_recall": KBRecallNode(config=self.config),
            "rule_consistency_llm": RuleConsistencyLLMNode(config=self.config), "claim_source_verify": ClaimSourceVerifyNode(config=self.config),
            "accumulate_ingest": AccumulateIngestNode(config=self.config), "accumulate_feedback": AccumulateFeedbackNode(config=self.config),
        }

    def add_edges(self) -> None:
        self._sg.add_conditional_edges(START, self.route)
        self._sg.add_edge("deterministic_check", "kb_recall")
        self._sg.add_edge("kb_recall", "rule_consistency_llm")
        self._sg.add_edge("rule_consistency_llm", "claim_source_verify")
        self._sg.add_edge("claim_source_verify", END)
        self._sg.add_edge("accumulate_ingest", END)
        self._sg.add_edge("accumulate_feedback", END)

    def route(self, state: State) -> str:
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return {"invoke": "deterministic_check", "ingest": "accumulate_ingest", "feedback": "accumulate_feedback"}.get(_mode(state), END)

    def get_output(self, state: State) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            output = {"status": AgentStatus.ERROR.value, "error_log": state.get("error_log", [])}
            if state.get("error_code"):
                output["error_code"] = state["error_code"]
            if state.get("error_type"):
                output["error_type"] = state["error_type"]
            return output
        return {
            "mode": _mode(state), "status": state.get("status", AgentStatus.SUCCESS.value),
            "consistency_brief": state.get("consistency_brief"), "evidence_map": state.get("evidence_map", []),
            "cited_source_ids": state.get("cited_source_ids", []), "retrieved_records": state.get("retrieved_records", []),
            "requires_human_review": state.get("requires_human_review", False), "ingest_summary": state.get("ingest_summary"),
            "feedback_summary": state.get("feedback_summary"), "degradation_reason": state.get("degradation_reason", ""),
        }


def _mode(state: State) -> str:
    if state.get("mode"):
        return state["mode"]
    try:
        envelope = json.loads(state.get("user_input", "{}"))
    except (TypeError, json.JSONDecodeError):
        return ""
    return envelope.get("mode", "") if isinstance(envelope, dict) else ""
