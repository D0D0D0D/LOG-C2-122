"""Outer fixed Cat2 backbone for LOG-C2-122."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import yaml
from framework.graph.agent_base_graph import AgentBaseGraph

from src.nodes.main_node import DangerousGoodsWorkflowGraphNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State


def load_runtime_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or Path(__file__).resolve().parents[2] / "config" / "config.yaml"
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    return loaded if isinstance(loaded, dict) else {}


class DangerousGoodsDocConsistencyAgent(AgentBaseGraph):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config=config if config is not None else load_runtime_config())

    @property
    def name(self) -> str:
        return "log_c2_122"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()
        self._nodes["pre_process"] = PreProcessNode(config=self.config)
        self._nodes["main"] = DangerousGoodsWorkflowGraphNode(config=self.config)
        self._nodes["post_process"] = PostProcessNode()

    def get_output(self, state: dict[str, Any]) -> dict[str, Any]:
        output = cast(dict[str, Any], super().get_output(state))
        if state.get("error_code"):
            output["error_code"] = state["error_code"]
        if state.get("error_type"):
            output["error_type"] = state["error_type"]
        if state.get("error_log"):
            output["error_log"] = list(dict.fromkeys(state.get("error_log", [])))
        return output

    def invoke(self, user_input: str, session_id: str = "", ctx=None, input_context: dict | None = None) -> dict:
        """Reject raw or unknown packet fields before they can be checkpointed."""
        if not _is_checkpoint_safe_packet(user_input):
            return {"output": None, "status": "error", "error_log": ["invoke: raw or malformed DG packet was rejected"]}
        return super().invoke(user_input, session_id=session_id, ctx=ctx, input_context=input_context)


def _is_checkpoint_safe_packet(user_input: str) -> bool:
    try:
        packet = json.loads(user_input)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(packet, dict) or packet.get("mode") not in {"invoke", "ingest", "feedback"}:
        return False
    allowed = {
        "mode", "customer_id", "case_ref", "transport_mode", "rule_edition_ref", "declared_items", "evidence_sources",
        "review_request", "ingest_records", "feedback", "payload_ref",
    }
    if set(packet) - allowed or not isinstance(packet.get("customer_id"), str) or not isinstance(packet.get("case_ref"), str):
        return False
    if "payload_ref" in packet and not isinstance(packet["payload_ref"], str):
        return False
    return True


Graph = DangerousGoodsDocConsistencyAgent
