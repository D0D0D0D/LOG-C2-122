"""AGENTIC STAR Marketplace entry adapter for LOG-C2-122."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets

try:
    from framework.utils.config_loader import load_agent_config
except ImportError:  # AgentCore 1.0.0 / 1.0.1 compatibility
    from framework.utils.config_loader import load_config

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return load_config(str(config_path)) if config_path.exists() else {}


try:
    from shared.bootstrap import run_agent_marketplace
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.bootstrap":
        raise
    _BOOTSTRAP_IMPORT_ERROR: ModuleNotFoundError | None = exc

    def run_agent_marketplace(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError(
            "The installed AgentCore wheel does not provide Marketplace bootstrap support"
        ) from _BOOTSTRAP_IMPORT_ERROR
else:
    _BOOTSTRAP_IMPORT_ERROR = None


try:
    from shared.secrets.env_provider import EnvProvider
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.secrets.env_provider":
        raise

    class EnvProvider(SecretProvider):  # type: ignore[no-redef]
        """Minimal process-environment provider for pre-1.0.2 wheels."""

        def __init__(self, namespace: str = "", agent_name: str = "") -> None:
            self._namespace = namespace
            self._agent_name = agent_name

        def get(self, key: str, default: str | None = None) -> str | None:
            return os.environ.get(key, default)


from shared.services.llm.base_llm import BaseLLM

from src.graph.graph import DangerousGoodsDocConsistencyAgent

_NAMESPACE = "log"
_AGENT_NAME = "LOG-C2-122"
_PROVIDER_AGENT_NAME = "log_c2_122"

_CALLER_ID_ENV = "STG_DEFAULT_CALLER_ID"
_CUSTOMER_ID_ENV = "STG_DEFAULT_CUSTOMER_ID"
_AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)
_SCOPE_ENV_KEYS = (_CALLER_ID_ENV, _CUSTOMER_ID_ENV)
_REQUIRED_ENV_KEYS = (*_AZURE_OPENAI_SECRET_KEYS, *_SCOPE_ENV_KEYS)
_ALLOWED_ENV_KEYS = frozenset(_REQUIRED_ENV_KEYS)
_MARKETPLACE_REJECTION_MODE = "marketplace_mode_not_available"


class _AllowlistedEnvironmentProvider(EnvProvider):  # type: ignore[misc]
    """Expose only this single-agent container's declared environment values."""

    def __init__(self) -> None:
        super().__init__(namespace=_NAMESPACE, agent_name=_PROVIDER_AGENT_NAME)

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _ALLOWED_ENV_KEYS:
            return default
        value = super().get(key)
        return value if value is not None else default


def _require_clean(provider: SecretProvider, key: str) -> str:
    """Resolve a required deployment value without trimming or repairing it."""

    value = provider.require(key)
    if not value or any(character.isspace() for character in value):
        raise ValueError(f"Marketplace environment variable {key!r} must be non-empty and contain no whitespace")
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = _require_clean(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


def _deployment_scope(provider: SecretProvider) -> dict[str, str]:
    """Resolve the caller/customer binding solely from deployment configuration."""

    return {
        "caller_id": _require_clean(provider, _CALLER_ID_ENV),
        "customer_id": _require_clean(provider, _CUSTOMER_ID_ENV),
    }


class _MarketplaceReadOnlyDB:
    """Credential-free baseline reader; Marketplace never exposes write modes."""

    def read_case(self, case_ref: str) -> dict[str, Any]:
        return {"case_ref": case_ref, "declared_items": []}

    def append(self, record: dict[str, Any]) -> None:
        del record
        raise RuntimeError("Marketplace customer DB writes are not available")


class _MarketplaceReadOnlyKB:
    """Credential-free empty recall source; Marketplace never exposes writes."""

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        del query, top_k
        return []

    def append(self, record: dict[str, Any]) -> None:
        del record
        raise RuntimeError("Marketplace customer KB writes are not available")


class _AzureOpenAIClientCompat(BaseLLM):
    """Small Azure chat-completion client for AgentCore before 1.0.2."""

    def __init__(self, config: dict[str, str]) -> None:
        self._config = dict(config)

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        endpoint = self._config["azure_endpoint"].rstrip("/")
        deployment = quote(self._config["azure_deployment"], safe="")
        url = f"{endpoint}/openai/deployments/{deployment}/chat/completions" "?api-version=2024-10-21"
        request = Request(
            url,
            data=json.dumps({"messages": messages}).encode("utf-8"),
            headers={
                "api-key": self._config["api_key"],
                "content-type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=30) as response:  # noqa: S310 - deployment-owned endpoint
            payload = json.loads(response.read().decode("utf-8"))
        choices = payload.get("choices", []) if isinstance(payload, dict) else []
        message = choices[0].get("message", {}) if choices else {}
        content = message.get("content", "") if isinstance(message, dict) else ""
        model = payload.get("model", self._config["azure_deployment"])
        return {
            "content": content,
            "tool_calls": message.get("tool_calls", []) if isinstance(message, dict) else [],
            "model": model,
            "usage": payload.get("usage", {}),
        }

    def stream(self, messages: list[Any]) -> Iterator[str]:
        content = self.complete(messages).get("content", "")
        if content:
            yield str(content)

    def bind_tools(self, tools: list[Any]) -> "_AzureOpenAIClientCompat":
        if tools:
            raise NotImplementedError("Azure compatibility client does not support tool binding")
        return self


class _AzureMarketplaceLLM(BaseLLM):
    """Resolve Azure values and construct a fresh client for every operation."""

    @staticmethod
    def _new_client() -> BaseLLM:
        provider = current_secrets()
        values = {key: _require_clean(provider, key) for key in _AZURE_OPENAI_SECRET_KEYS}

        # Azure is the sole invocation-time import. Framework, graph, security,
        # and state imports remain eager for Marketplace's worker-thread path.
        try:
            from shared.services.llm.azure_openai_client import AzureOpenAIClient
        except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
            if exc.name != "shared.services.llm.azure_openai_client":
                raise
            AzureOpenAIClient = _AzureOpenAIClientCompat  # type: ignore[misc,assignment]

        return cast(
            BaseLLM,
            AzureOpenAIClient(
                {
                    "api_key": values["AZURE_OPENAI_API_KEY"],
                    "azure_endpoint": values["AZURE_OPENAI_ENDPOINT"],
                    "azure_deployment": values["AZURE_OPENAI_DEPLOYMENT"],
                }
            ),
        )

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._new_client().complete(messages))

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield from self._new_client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> BaseLLM:
        return self._new_client().bind_tools(tools)


def _marketplace_user_input(user_input: str, customer_id: str) -> str:
    """Apply deployment customer scope and expose only the read-only invoke mode."""

    if not isinstance(user_input, str):
        raise TypeError("Marketplace input must be one plain string")
    try:
        packet = json.loads(user_input)
    except json.JSONDecodeError:
        return user_input
    if not isinstance(packet, dict):
        return user_input
    if packet.get("mode", "invoke") != "invoke":
        return json.dumps({"mode": _MARKETPLACE_REJECTION_MODE})
    packet["mode"] = "invoke"
    packet["customer_id"] = customer_id
    return json.dumps(packet, ensure_ascii=False, sort_keys=True)


class MarketplaceGraph(DangerousGoodsDocConsistencyAgent):
    """Marketplace-only scope, read posture, HITL, secret, and Azure wiring."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        # The runner compiles before provision_secrets(). Resolve credential-free
        # scope and services now, while Azure values remain per-call secrets.
        scope = _deployment_scope(_AllowlistedEnvironmentProvider())
        customer_id = scope["customer_id"]
        marketplace_config = dict(config if config is not None else load_agent_config(Path(__file__).resolve().parent))
        configured_dbs = marketplace_config.get("db_connections")
        configured_kbs = marketplace_config.get("kb_connections")
        db = configured_dbs.get(customer_id) if isinstance(configured_dbs, dict) else None
        kb = configured_kbs.get(customer_id) if isinstance(configured_kbs, dict) else None
        marketplace_config.update(
            {
                "llm": _AzureMarketplaceLLM(),
                "caller_customer_map": {scope["caller_id"]: customer_id},
                "db_connections": {customer_id: db or _MarketplaceReadOnlyDB()},
                "kb_connections": {customer_id: kb or _MarketplaceReadOnlyKB()},
                "memory_enabled": False,
                "hitl": {"enabled": False},
            }
        )
        super().__init__(config=marketplace_config)

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Runner conversation history is untrusted free text outside S-2.
        # Rebuild context exclusively from adapter-owned deployment scope.
        del input_context
        scope = _deployment_scope(_AllowlistedEnvironmentProvider())
        base_ctx = ctx if ctx is not None else InvocationContext(session_id=session_id or "")
        marketplace_ctx = replace(
            base_ctx,
            caller_id=scope["caller_id"],
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
            hitl_allowed=False,
        )
        result = cast(
            dict[str, Any],
            super().invoke(
                _marketplace_user_input(user_input, scope["customer_id"]),
                session_id=session_id,
                ctx=marketplace_ctx,
                input_context={"customer_id": scope["customer_id"]},
            ),
        )
        if result.get("status") == AgentStatus.AWAITING_HUMAN.value:
            raise RuntimeError("AWAITING_HUMAN is not supported by AGENTIC STAR Marketplace")
        return result

    def provision_secrets(self, provider: SecretProvider) -> None:
        # Ignore runner providers that may expose dotenv or other agents' keys.
        # This method is called after compile and makes missing values a startup failure.
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        for key in _REQUIRED_ENV_KEYS:
            _require_clean(environment_provider, key)
        _deployment_scope(environment_provider)
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)


def main() -> None:
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=load_agent_config(Path(__file__).resolve().parent),
    )


if __name__ == "__main__":
    main()
