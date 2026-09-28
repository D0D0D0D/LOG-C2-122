# LOG-C2-122 — Logistics Dangerous-Goods Document Consistency Agent

> **Category**: Cat 2 (domain workflow)
> **Industry**: LOG

## Overview

Logistics Dangerous-Goods Document Consistency Agent cross-checks what a dangerous-goods shipment
has already been declared as against what its supporting paperwork actually says, and returns the
discrepancies as an evidence-linked brief for a qualified person to work through.

For an air, sea or road consignment it compares the declared proper shipping name, UN number,
class, packing group and quantity against the safety data sheet and the packing certificate,
referencing the approved edition of IATA DGR, IMDG or ADR that the deployment has registered. What
comes back is a set of **candidate** inconsistencies, inquiry points and verification questions —
each tied to the specific source span it came from.

It suits an export officer checking a consignment before it ships, a logistics-quality reviewer
assembling the points to raise on a case, and a qualified dangerous-goods signatory who wants the
evidence and the open questions gathered in one place before they sign.

**It does not classify and it does not release.** Determining the proper shipping name, UN number
or class, and deciding whether a shipment may go, are not outputs — and are not reachable by
accident. Three structural properties hold that line:

- **The brief is a closed five-field schema.** A claim entry whose key set does not match exactly
  is rejected. There is nowhere to write a determination.
- **Severity is a fixed value.** Every claim carries `severity: "candidate"`. There is no scale
  the model can climb to express certainty.
- **Prohibited keys are excluded at projection.** `classification_final`, `shipment_release` and
  `release_decision` cannot be projected into the output at all.

Notably, the template has **no denylist of banned conclusion phrases**, and that is deliberate. A
phrase list is bypassable by paraphrase and gives false assurance that something is being
prevented. The allowlist — closed schema, fixed severity, mandatory claim-to-source linkage — is
the guarantee.

Every claim must name an `evidence_source_id` that exists among the sources supplied, must carry a
`rule_ref`, and must quote the span it rests on. A source whose provenance does not belong to the
current `case_ref` is rejected: evidence from a different case cannot be cited as if it applied to
this one. Past cases are recalled as review *perspectives*, never as evidence.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime;
- authentication middleware, or the standalone bearer token, in front of the entry adapter;
- **per-customer provisioned database and knowledge-base connections**, plus the caller-to-customer
  authorisation map.

That last one is not optional. The template ships with `db_connections`, `kb_connections` and
`caller_customer_map` all empty, and **there is deliberately no default connection**: a caller
whose customer cannot be resolved fails closed before any read or write. Isolation between
customers is enforced at the connection level — each customer has its own physically separate
store — rather than by filtering rows in a shared one. A `customer_id` present in the request is
used **only to cross-check against the authorised customer**, never as a search key; a mismatch is
a **403**.

Runtime parameters read from `config/config.yaml` (defaults in parentheses):

| Key | Meaning |
|---|---|
| `max_retry` (`3`) / `timeout_s` (`30`) | Backbone retry and timeout budget |
| `memory_enabled` (`false`) / `hitl.enabled` (`false`) | No checkpointer and no synchronous human interrupt — see *Release* |
| `retrieval.top_k` (`8`) | Past review perspectives retrieved from the customer's own knowledge base |
| `retrieval.score_threshold` (`0.75`) | Similarity floor, set conservatively for safety-critical output |
| `confidence_threshold` (`0.75`) | Floor below which a candidate is not surfaced |
| `llm.temperature` (`0.0`) / `llm.max_tokens` (`4000`) | Deterministic sampling for regulation-consistency reasoning |
| `required_documents_by_transport` | Documents Stage 1 checks per transport mode. Defaults: SDS and packing certificate for air, sea and road |
| `class_packing_rules` | Advisory class-to-packing-group consistency checks |
| `db_connections` / `kb_connections` / `caller_customer_map` | Injected at deployment. Empty by default — see above |

The Stage 1 checks are **advisory consistency checks**, not a regulatory determination.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run on AGENTIC STAR. Without the `agenticstar-agentcore`
distribution installed, `src/api/server.py` cannot be imported at all — the graph, the state
schema and the security gates all come from it, and there is no vendored substitute in this
repository.

With the framework installed but no platform around it, the standalone server starts and
`/health` answers `{"status": "ok", "agent": "log_c2_122"}`. It will not do useful work: with the
connection maps empty, every caller fails customer resolution and is refused. That is the shipped
posture and it is intentional. The one in-memory placeholder connection that exists in the code
is bound **only** inside the ephemeral staging smoke job, gated on CI provenance —
`CI=true`, the job named exactly `deploy-stg`, and a job-scoped `STG_MOCK_MODE=true` — not on the
bearer token, precisely so a leaked or broadly-scoped staging variable cannot manufacture an
authorised identity in production. The test suite runs without a platform connection.

## Input

| Route | Purpose |
|---|---|
| `POST /invoke` | Produce the consistency brief for one dangerous-goods case |
| `POST /feedback` | Record an approved review outcome so it informs later cases |

There is **no `/resume`** — this template has no synchronous human interrupt. Human involvement
happens asynchronously through `/feedback`, where an outcome is accumulated only once it has been
explicitly approved.

Both routes take `{"input": "<JSON string>", "session_id": "...", "thread_id": "..."}` and both go
through the same authentication and the same customer-authorisation boundary. When
`INVOKE_AUTH_TOKEN` is set, a caller with no verified middleware trust must present a matching
bearer token or receive **401**.

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{
    "input": "{\"mode\":\"invoke\",\"customer_id\":\"authorised_customer\",\"case_ref\":\"case_001\",\"transport_mode\":\"air\",\"rule_edition_ref\":\"iata_dgr_approved_edition\",\"declared_items\":[{\"item_ref\":\"item_1\",\"psn\":\"Declared item\",\"un\":\"UN0000\",\"class\":\"3\",\"packing_group\":\"II\",\"quantity\":\"1 L\"}],\"evidence_sources\":[{\"source_id\":\"sds_1\",\"doc_type\":\"sds\",\"quote_span\":\"relevant declared span\",\"provenance\":{\"case_ref\":\"case_001\"}}]}",
    "session_id": "demo-001"
  }'
```

Only a fixed set of keys is forwarded into the graph — `mode`, `customer_id`, `case_ref`,
`transport_mode`, `rule_edition_ref`, `declared_items`, `evidence_sources`, `review_request`,
`ingest_records`, `feedback`. Anything else is dropped rather than carried along. Malformed JSON
does not fail loudly at the boundary: it is replaced with a safe empty packet that the graph then
rejects on its own terms. `/feedback` additionally requires `mode: "feedback"` and returns **422**
otherwise.

The raw request packet is not passed through in full — it is placed in a customer-scoped payload
store and referenced by `payload_ref`, so the case body is not carried in the graph's state.

## Output

The brief is a closed five-field structure: `declared_items`, `evidence_sources`,
`inconsistency_candidates`, `inquiry_points`, `verification_questions`.

```json
{
  "declared_items": [],
  "evidence_sources": [],
  "inconsistency_candidates": [],
  "inquiry_points": [],
  "verification_questions": []
}
```

Every entry in a claim section carries exactly its required key set — no more, no fewer — with a
resolvable `evidence_source_id`, a `rule_ref`, the quoted text it rests on, and
`severity: "candidate"`. A violation of any of these is a rejection, not a warning: the output
gate raises rather than returning a softened brief.

`/feedback` returns a `feedback_summary` instead of a brief.

## Security and Limitations

The graph requires verified-external trust, so an anonymous caller never reaches a node body.
Beyond that, customer authorisation is resolved from the authenticated `caller_id` — never from a
value the caller supplies — and the resulting connection is that customer's own dedicated store.
There is no shared multi-tenant store to partition, and no default connection to fall back on.

Evidence is confined to the current case. A source whose provenance names a different `case_ref`
is rejected outright, so a document from a neighbouring shipment cannot be cited into this brief.

Known limits, stated rather than implied:

- The agent reports candidates. It does not determine the proper shipping name, UN number, class
  or packing group, and it does not decide whether a shipment may go. Those determinations belong
  to a qualified dangerous-goods person.
- Findings are relative to the rule edition the deployment registered. An out-of-date registered
  edition yields an out-of-date brief; the agent cannot detect that for you.
- The retrieval floor is set conservatively for safety-critical output, so a genuinely relevant
  past perspective phrased differently can fall below it and be missed. Recall is deliberately
  traded against precision in that direction.
- Past cases inform which questions to ask. They are never evidence for a claim about this case.

Output is machine-generated and assistive. It does not substitute for a qualified person's review
of the primary documents, and every candidate is intended to be confirmed or dismissed by that
person.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2), document-generation pattern
- Generation mode: `llm`, at `temperature: 0.0` for regulation-consistency reasoning
- HITL: **not enabled**. Human involvement is asynchronous, via `/feedback` (explicit-approval accumulation), rather than a synchronous interrupt. Enabling a synchronous low-confidence interrupt later means setting `hitl.enabled: true` together with `memory_enabled: true`
- Persistence: none — no checkpointer; case bodies live only in a customer-scoped payload store
- External API clients: deployment-injected; none bundled

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection, using fakes for the model and the stores. Running the
agent itself starts the standalone server:

```bash
uvicorn src.api.server:app --reload
```

Set `INVOKE_AUTH_TOKEN` before calling a route, and provision the customer connection maps — until
you do, every request is refused by design.

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the customer-isolation model, the
closed output contract and the security design, together with a comparison against the adjacent
customs-clearance and SDS-revision templates. `docs/03_test_spec.md` records the test
specification and the verification results.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

---
