"""Stage 5 STG smoke provisioning must never weaken the shipped fail-closed posture.

`_provision_stg_default()` binds one placeholder caller/customer plus in-memory
connections so the Stage 5 smoke invoke can reach `status: success`. It must fire
ONLY inside the provisional `deploy-stg` CI job and be a no-op everywhere else.

The critical property: `INVOKE_AUTH_TOKEN` is the ordinary standalone caller-auth
token, NOT a staging discriminator. A production standalone deployment sets it too,
so the CI-job provenance (`CI` + `CI_JOB_NAME` + `STG_MOCK_MODE`) is what separates
staging from production.
"""

from __future__ import annotations

import pytest

from src.api.server import _provision_stg_default


# Provenance of the provisional deploy-stg smoke job.
CI_ENV = {"CI": "true", "CI_JOB_NAME": "deploy-stg", "STG_MOCK_MODE": "true"}
AUTH_ENV = {"INVOKE_AUTH_TOKEN": "mock-token-for-testing"}
STG_VARS = {"STG_DEFAULT_CALLER_ID": "standalone_bearer", "STG_DEFAULT_CUSTOMER_ID": "stg-signoff-customer"}
FULL_ENV = {**CI_ENV, **AUTH_ENV, **STG_VARS}

ALL_KEYS = tuple(FULL_ENV)


def _empty_config() -> dict:
    return {"caller_customer_map": {}, "db_connections": {}, "kb_connections": {}}


def _apply(monkeypatch, env: dict, config: dict) -> dict:
    for key in ALL_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    _provision_stg_default(config)
    return config


def _is_unprovisioned(config: dict) -> bool:
    return (
        config["caller_customer_map"] == {}
        and config["db_connections"] == {}
        and config["kb_connections"] == {}
    )


def test_provisions_inside_the_provisional_deploy_stg_job(monkeypatch):
    config = _apply(monkeypatch, FULL_ENV, _empty_config())
    assert config["caller_customer_map"] == {"standalone_bearer": "stg-signoff-customer"}
    assert set(config["db_connections"]) == {"stg-signoff-customer"}
    assert set(config["kb_connections"]) == {"stg-signoff-customer"}


@pytest.mark.parametrize(
    ("label", "env"),
    [
        # THE case that matters: a legitimate standalone production deployment that
        # sets the ordinary auth token and has the STG variables leaked/broadly scoped.
        ("production standalone, no CI provenance", {**AUTH_ENV, **STG_VARS}),
        ("CI but a different job", {**FULL_ENV, "CI_JOB_NAME": "run-tests"}),
        ("deploy-stg but not provisional mode", {**FULL_ENV, "STG_MOCK_MODE": "false"}),
        ("CI unset", {k: v for k, v in FULL_ENV.items() if k != "CI"}),
        ("no auth token", {**CI_ENV, **STG_VARS}),
        ("no STG variables", {**CI_ENV, **AUTH_ENV}),
        ("blank customer id", {**FULL_ENV, "STG_DEFAULT_CUSTOMER_ID": ""}),
        ("blank caller id", {**FULL_ENV, "STG_DEFAULT_CALLER_ID": ""}),
        ("nothing set at all", {}),
    ],
)
def test_no_provision_outside_the_provisional_smoke_job(monkeypatch, label, env):
    config = _apply(monkeypatch, env, _empty_config())
    assert _is_unprovisioned(config), label


@pytest.mark.parametrize(
    "config",
    [
        # Any already-populated map: a configured deployment is never overwritten.
        {"caller_customer_map": {"real-caller": "real-customer"}, "db_connections": {}, "kb_connections": {}},
        {"caller_customer_map": {}, "db_connections": {"real-customer": object()}, "kb_connections": {}},
        {"caller_customer_map": {}, "db_connections": {}, "kb_connections": {"real-customer": object()}},
        # Malformed/falsy configuration must fail closed, not be "rescued".
        {"caller_customer_map": None, "db_connections": {}, "kb_connections": {}},
        {"caller_customer_map": {}, "db_connections": None, "kb_connections": {}},
        {"caller_customer_map": {}, "db_connections": {}, "kb_connections": []},
        {"caller_customer_map": "", "db_connections": {}, "kb_connections": {}},
        {"db_connections": {}, "kb_connections": {}},  # key absent entirely
    ],
)
def test_never_overwrites_configured_or_malformed_maps(monkeypatch, config):
    before = {key: config.get(key) for key in ("caller_customer_map", "db_connections", "kb_connections")}
    _apply(monkeypatch, FULL_ENV, config)
    after = {key: config.get(key) for key in ("caller_customer_map", "db_connections", "kb_connections")}
    assert after == before


def test_production_request_stays_unauthorised_end_to_end(monkeypatch):
    """Endpoint-level: production posture + leaked STG vars must still refuse the caller."""
    import importlib
    import json
    import sys

    from fastapi.testclient import TestClient

    for key in ALL_KEYS:
        monkeypatch.delenv(key, raising=False)
    # Production standalone: ordinary auth token + STG variables present, but no CI provenance.
    for key, value in {**AUTH_ENV, **STG_VARS}.items():
        monkeypatch.setenv(key, value)
    for module in [name for name in list(sys.modules) if name.startswith("src.")]:
        del sys.modules[module]
    import src.api.server as server

    importlib.reload(server)

    assert server.runtime_config["caller_customer_map"] == {}
    packet = {
        "mode": "ingest",
        "customer_id": "stg-signoff-customer",
        "case_ref": "pwn_1",
        "ingest_records": [],
    }
    response = TestClient(server.app).post(
        "/invoke",
        json={"input": json.dumps(packet), "session_id": "s1"},
        headers={"Authorization": f"Bearer {AUTH_ENV['INVOKE_AUTH_TOKEN']}"},
    )
    assert response.status_code == 403
