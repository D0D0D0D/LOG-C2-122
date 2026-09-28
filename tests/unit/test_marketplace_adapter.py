"""Unit contracts for the AGENTIC STAR Marketplace entry adapter."""

from __future__ import annotations

import json
import sys
from types import ModuleType
from typing import Any

import pytest

import cli
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import MissingSecret, NullProvider
from framework.secrets.context import bound_secrets
from src.graph.graph import DangerousGoodsDocConsistencyAgent
from src.nodes.deterministic_check_node import DeterministicCheckNode


def _set_required_environment(monkeypatch, suffix: str = "one") -> dict[str, str]:
    values = {
        "AZURE_OPENAI_API_KEY": f"{suffix}-key",
        "AZURE_OPENAI_ENDPOINT": f"https://{suffix}.azure.example",
        "AZURE_OPENAI_DEPLOYMENT": f"{suffix}-deployment",
        cli._CALLER_ID_ENV: f"marketplace-caller-{suffix}",
        cli._CUSTOMER_ID_ENV: f"marketplace-customer-{suffix}",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


@pytest.fixture(autouse=True)
def required_environment(monkeypatch):
    _set_required_environment(monkeypatch)


def _packet(*, mode: str = "invoke", customer_id: str = "attacker-customer") -> str:
    return json.dumps(
        {
            "mode": mode,
            "customer_id": customer_id,
            "case_ref": "case_1",
            "transport_mode": "air",
            "rule_edition_ref": "iata_approved",
            "declared_items": [],
            "evidence_sources": [
                {
                    "source_id": "sds_1",
                    "doc_type": "sds",
                    "quote_span": "SDS section 14",
                    "provenance": {"case_ref": "case_1"},
                }
            ],
        }
    )


def _contains(value: Any, needle: str) -> bool:
    if isinstance(value, dict):
        return any(_contains(key, needle) or _contains(item, needle) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains(item, needle) for item in value)
    return needle in value if isinstance(value, str) else False


def test_invoke_discards_runner_context_uses_deployment_scope_and_suppresses_hitl(monkeypatch):
    values = _set_required_environment(monkeypatch)
    sentinel = "UNSCANNED-CONVERSATION-122"
    captured: dict[str, Any] = {}

    def parent_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        del self
        captured.update(user_input=user_input, session_id=session_id, ctx=ctx, input_context=input_context)
        return {"status": "success"}

    monkeypatch.setattr(DangerousGoodsDocConsistencyAgent, "invoke", parent_invoke)
    original_ctx = InvocationContext(
        caller_id="runner-claim",
        caller_trust_level=TrustLevel.INTERNAL,
        hitl_allowed=True,
    )

    result = cli.MarketplaceGraph().invoke(
        _packet(),
        session_id="market-session",
        ctx=original_ctx,
        input_context={
            "conversation_history": [{"role": "user", "content": sentinel}],
            "customer_id": "attacker-customer",
            "future_runner_key": sentinel,
        },
    )

    assert result == {"status": "success"}
    assert captured["input_context"] == {"customer_id": values[cli._CUSTOMER_ID_ENV]}
    assert not _contains(captured["input_context"], sentinel)
    assert json.loads(captured["user_input"])["customer_id"] == values[cli._CUSTOMER_ID_ENV]
    assert captured["ctx"] is not original_ctx
    assert captured["ctx"].caller_id == values[cli._CALLER_ID_ENV]
    assert captured["ctx"].caller_trust_level is TrustLevel.VERIFIED_EXTERNAL
    assert captured["ctx"].hitl_allowed is False


def test_runner_conversation_history_never_enters_real_graph_state(monkeypatch):
    values = _set_required_environment(monkeypatch)
    sentinel = "UNSCANNED-CONVERSATION-STATE-122"
    agent = cli.MarketplaceGraph()
    agent.compile()
    original_invoke = agent._compiled.invoke
    captured: dict[str, Any] = {}

    def capture_state(initial_state, config=None):
        captured["initial"] = initial_state
        final_state = original_invoke(initial_state, config=config)
        captured["final"] = final_state
        return final_state

    agent._compiled.invoke = capture_state
    unsafe_case_packet = json.loads(_packet())
    unsafe_case_packet["case_ref"] = "case with spaces"
    result = agent.invoke(
        json.dumps(unsafe_case_packet),
        ctx=InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        input_context={"conversation_history": [{"role": "user", "content": sentinel}]},
    )

    assert captured["initial"]["input_context"] == {"customer_id": values[cli._CUSTOMER_ID_ENV]}
    assert not _contains(captured["initial"], sentinel)
    assert not _contains(captured["final"], sentinel)
    assert not _contains(result, sentinel)
    assert result["status"] == "error"


@pytest.mark.parametrize("mode", ["ingest", "feedback", "write", "unknown"])
def test_marketplace_does_not_expose_non_invoke_modes(monkeypatch, mode):
    values = _set_required_environment(monkeypatch)

    adapted = json.loads(cli._marketplace_user_input(_packet(mode=mode), values[cli._CUSTOMER_ID_ENV]))

    assert adapted == {"mode": cli._MARKETPLACE_REJECTION_MODE}


def test_marketplace_connections_reject_writes():
    with pytest.raises(RuntimeError, match="writes are not available"):
        cli._MarketplaceReadOnlyDB().append({})
    with pytest.raises(RuntimeError, match="writes are not available"):
        cli._MarketplaceReadOnlyKB().append({})


@pytest.mark.parametrize("key", cli._REQUIRED_ENV_KEYS)
def test_missing_required_environment_fails_during_startup(monkeypatch, key):
    agent = cli.MarketplaceGraph()
    monkeypatch.delenv(key)

    with pytest.raises(MissingSecret, match=key):
        agent.provision_secrets(NullProvider())


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(monkeypatch, endpoint):
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    agent = cli.MarketplaceGraph()
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        agent.provision_secrets(NullProvider())


@pytest.mark.parametrize("key", cli._REQUIRED_ENV_KEYS)
@pytest.mark.parametrize("invalid", ["", " ", "bad value", "bad\tvalue", "bad\nvalue"])
def test_empty_or_whitespace_required_environment_fails_during_startup(monkeypatch, key, invalid):
    agent = cli.MarketplaceGraph()
    monkeypatch.setenv(key, invalid)

    with pytest.raises(ValueError, match=key):
        agent.provision_secrets(NullProvider())


def test_bad_scope_fails_before_compile(monkeypatch):
    monkeypatch.setenv(cli._CUSTOMER_ID_ENV, "bad customer")

    with pytest.raises(ValueError, match=cli._CUSTOMER_ID_ENV):
        cli.MarketplaceGraph()


def test_allowlisted_provider_hides_unrelated_environment(monkeypatch):
    monkeypatch.setenv("UNRELATED_PLATFORM_SECRET", "must-not-be-visible")

    assert cli._AllowlistedEnvironmentProvider().get("UNRELATED_PLATFORM_SECRET") is None


def test_azure_llm_resolves_fresh_credentials_and_client_for_every_call(monkeypatch):
    configs: list[dict[str, str]] = []

    class FakeAzureOpenAIClient:
        def __init__(self, config):
            configs.append(dict(config))

        def complete(self, messages):
            return {"content": "ok", "messages": messages}

        def stream(self, messages):
            del messages
            yield "ok"

        def bind_tools(self, tools):
            del tools
            return self

    azure_module = ModuleType("shared.services.llm.azure_openai_client")
    azure_module.AzureOpenAIClient = FakeAzureOpenAIClient
    monkeypatch.setitem(sys.modules, azure_module.__name__, azure_module)

    provider = cli._AllowlistedEnvironmentProvider()
    llm = cli._AzureMarketplaceLLM()
    assert vars(llm) == {}
    with bound_secrets(provider):
        first = _set_required_environment(monkeypatch, "first")
        llm.complete([{"role": "user", "content": "first"}])
        second = _set_required_environment(monkeypatch, "second")
        llm.complete([{"role": "user", "content": "second"}])

    assert vars(llm) == {}
    assert configs == [
        {
            "api_key": first["AZURE_OPENAI_API_KEY"],
            "azure_endpoint": first["AZURE_OPENAI_ENDPOINT"],
            "azure_deployment": first["AZURE_OPENAI_DEPLOYMENT"],
        },
        {
            "api_key": second["AZURE_OPENAI_API_KEY"],
            "azure_endpoint": second["AZURE_OPENAI_ENDPOINT"],
            "azure_deployment": second["AZURE_OPENAI_DEPLOYMENT"],
        },
    ]


def test_compile_wires_azure_without_replacing_deterministic_completeness_node():
    agent = cli.MarketplaceGraph()
    agent.compile()

    inner = agent._nodes["main"].get_subgraph()
    assert isinstance(agent.config["llm"], cli._AzureMarketplaceLLM)
    assert isinstance(inner._nodes["deterministic_check"], DeterministicCheckNode)
    assert inner._nodes["rule_consistency_llm"]._config["llm"] is agent.config["llm"]


def test_llm_cannot_clear_deterministic_missing_document_review(monkeypatch):
    prompts: list[dict[str, Any]] = []

    class ContractAzureClient:
        def __init__(self, config):
            assert config["azure_deployment"] == "one-deployment"

        def complete(self, messages):
            prompt = json.loads(messages[0]["content"].split("\n")[-1])
            prompts.append(prompt)
            return {
                "content": json.dumps(
                    {
                        "declared_items": [],
                        "evidence_sources": [
                            {
                                "source_id": "sds_1",
                                "doc_type": "sds",
                                "quote_span": "SDS section 14",
                                "provenance": {"case_ref": "case_1"},
                            }
                        ],
                        "inconsistency_candidates": [],
                        "inquiry_points": [],
                        "verification_questions": [],
                    }
                )
            }

    azure_module = ModuleType("shared.services.llm.azure_openai_client")
    azure_module.AzureOpenAIClient = ContractAzureClient
    monkeypatch.setitem(sys.modules, azure_module.__name__, azure_module)

    agent = cli.MarketplaceGraph()
    agent.compile()
    agent.provision_secrets(NullProvider())
    original_invoke = agent._compiled.invoke
    captured: dict[str, Any] = {}

    def capture_state(initial_state, config=None):
        result = original_invoke(initial_state, config=config)
        captured.update(result)
        return result

    agent._compiled.invoke = capture_state
    with bound_secrets(agent._secrets_provider):
        result = agent.invoke(_packet())

    assert result["status"] == "success"
    assert prompts[0]["case_data"]["docs_present"] == {
        "required_documents": ["packing_certificate", "sds"],
        "present_documents": ["sds"],
        "missing_documents": ["packing_certificate"],
    }
    assert captured["requires_human_review"] is True


def test_main_passes_repository_config_to_marketplace_runner(monkeypatch):
    called: dict[str, Any] = {}

    def fake_runner(agent_cls, **kwargs):
        called["agent_cls"] = agent_cls
        called.update(kwargs)

    monkeypatch.setattr(cli, "run_agent_marketplace", fake_runner)
    cli.main()

    assert called["agent_cls"] is cli.MarketplaceGraph
    assert called["agent_name"] == "LOG-C2-122"
    assert called["namespace"] == "log"
    assert called["config"]["max_retry"] == 3
