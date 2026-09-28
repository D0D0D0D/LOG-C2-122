"""Authenticated HTTP adapter for the three dangerous-goods workflow modes."""

from __future__ import annotations

import json
import os
import secrets
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from src.payload_store import PayloadStore, PayloadStoreError
from shared.secrets import factory as secrets_factory

from src.graph.graph import DangerousGoodsDocConsistencyAgent, load_runtime_config
from src.services.service import resolve_customer_for_caller, wire_services


app = FastAPI(title="Logistics Dangerous-Goods Document Consistency Agent")
runtime_config = wire_services(load_runtime_config())
payload_store = PayloadStore(scope_keys=("customer_id",), ttl_seconds=300)
runtime_config["payload_store"] = payload_store


class _StgSmokeConnection:
    """In-memory stand-in used ONLY by the Stage 5 smoke provision (never shipped data)."""

    def __init__(self) -> None:
        self._records: list[dict] = []

    def read_case(self, case_ref: str) -> dict:
        return {"case_ref": case_ref}

    def append(self, record: dict) -> None:
        self._records.append(record)


def _is_provisional_stg_smoke_job() -> bool:
    """True only inside the ephemeral `deploy-stg` provisional smoke job.

    `INVOKE_AUTH_TOKEN` is NOT a staging discriminator — it is the ordinary
    standalone caller-auth token, so a real standalone deployment sets it too.
    Binding the fallback to it alone would let leaked/broadly-scoped
    `STG_DEFAULT_*` variables manufacture an authorized connection-level identity
    in production. Require instead the provenance that only that CI job has:
    a GitLab CI runner (`CI=true`), the job named exactly `deploy-stg`, and the
    job-scoped `STG_MOCK_MODE=true` that marks the in-job ephemeral deploy.
    """
    return (
        os.environ.get("CI") == "true"
        and os.environ.get("CI_JOB_NAME") == "deploy-stg"
        and os.environ.get("STG_MOCK_MODE") == "true"
    )


def _provision_stg_default(config: dict) -> None:
    """Stage 5 smoke only: bind one placeholder caller/customer + in-memory connections.

    Guards (all required):
      * the process is the provisional `deploy-stg` smoke job (see above) — this is
        the staging discriminator, not the bearer token;
      * `INVOKE_AUTH_TOKEN` is set, so the smoke request is still token-authenticated;
      * both `STG_DEFAULT_*` values are present (values are NOT cleaned — a padded or
        blank value simply does not provision);
      * every connection map is an *empty dict*. A map that is absent, wrongly typed,
        or partially populated is a deployment error and must keep failing closed
        rather than be rescued by the placeholder.

    Outside that job this is a no-op, so the shipped fail-closed posture — empty maps,
    no default connection — is unchanged for every real deployment.
    """
    if not _is_provisional_stg_smoke_job():
        return
    if not os.environ.get("INVOKE_AUTH_TOKEN"):
        return
    caller = os.environ.get("STG_DEFAULT_CALLER_ID", "")
    customer = os.environ.get("STG_DEFAULT_CUSTOMER_ID", "")
    if not caller or not customer:
        return
    # Exactly-empty dicts only: `{}` means "nothing provisioned"; anything else
    # (None, "", [], a populated map) is left untouched.
    for key in ("caller_customer_map", "db_connections", "kb_connections"):
        if config.get(key) != {} or not isinstance(config.get(key), dict):
            return
    config["caller_customer_map"] = {caller: customer}
    config["db_connections"] = {customer: _StgSmokeConnection()}
    config["kb_connections"] = {customer: _StgSmokeConnection()}


_provision_stg_default(runtime_config)
agent = DangerousGoodsDocConsistencyAgent(config=runtime_config)
# Synchronous HITL is disabled; do not attach an unused checkpoint store.
agent.compile()
agent.provision_secrets(secrets_factory(namespace="LOG", agent_name="log_c2_122"))


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    thread_id: str = ""


def _context(request: Request, *, session_id: str = "", thread_id: str = "") -> InvocationContext:
    caller_id = getattr(request.state, "caller_id", "")
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    if not isinstance(trust, TrustLevel):
        trust = TrustLevel(trust)
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL
        caller_id = caller_id or "standalone_bearer"
    return InvocationContext(session_id=session_id or str(uuid4()), thread_id=thread_id or str(uuid4()), caller_id=caller_id, caller_trust_level=trust, hitl_allowed=False)


def _safe_rejection_packet() -> str:
    return json.dumps({"mode": "invoke", "customer_id": "", "case_ref": "", "transport_mode": "air", "rule_edition_ref": "", "declared_items": [], "evidence_sources": []}, sort_keys=True)


def _prepare_invoke_input(raw_input: str, ctx: InvocationContext) -> str:
    try:
        packet = json.loads(raw_input)
    except (TypeError, json.JSONDecodeError):
        return _safe_rejection_packet()
    if not isinstance(packet, dict) or not isinstance(packet.get("customer_id"), str) or not isinstance(packet.get("case_ref"), str):
        return _safe_rejection_packet()
    customer = resolve_customer_for_caller(runtime_config, ctx.caller_id)
    if not customer or packet["customer_id"] != customer:
        raise HTTPException(status_code=403, detail="Caller is not authorised for this customer.")
    try:
        payload_ref = payload_store.put(packet, scope={"customer_id": customer}, session_id=ctx.thread_id)
    except PayloadStoreError:
        raise HTTPException(status_code=403, detail="Caller is not authorised for this customer.") from None
    safe = {key: value for key, value in packet.items() if key in {"mode", "customer_id", "case_ref", "transport_mode", "rule_edition_ref", "declared_items", "evidence_sources", "review_request", "ingest_records", "feedback"}}
    safe["payload_ref"] = payload_ref
    return json.dumps(safe, ensure_ascii=False, default=str, sort_keys=True)


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request):
    ctx = _context(request, session_id=req.session_id, thread_id=req.thread_id)
    with bound_secrets(agent._secrets_provider):
        return agent.invoke(_prepare_invoke_input(req.input, ctx), ctx=ctx)


@app.post("/feedback")
async def feedback(req: InvokeRequest, request: Request):
    """Feedback uses the same authenticated mode boundary; no synchronous HITL resume exists."""
    ctx = _context(request, session_id=req.session_id, thread_id=req.thread_id)
    try:
        packet = json.loads(req.input)
    except (TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=422, detail="Feedback is invalid.") from None
    if not isinstance(packet, dict) or packet.get("mode") != "feedback":
        raise HTTPException(status_code=422, detail="Feedback mode is required.")
    with bound_secrets(agent._secrets_provider):
        return agent.invoke(_prepare_invoke_input(req.input, ctx), ctx=ctx)


@app.get("/health")
def health():
    return {"status": "ok", "agent": "log_c2_122"}
