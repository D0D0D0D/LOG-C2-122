# Test Specification — LOG-C2-122

## Test Strategy

Unit tests cover the flat state contract, S-1 through S-4 framework boundaries, three-mode routing, and the closed `consistency_brief` schema. Integration tests use injected DB/KB/LLM fakes only; no test opens a network connection.

## Framework Compliance Tests

| TC-ID | Test | Expected Result | Result |
|---|---|---|---|
| TC-01 | State contract | Flat TypedDict with DG fields; no credentials | PASS |
| TC-02 | S-3 closed schema | Five reject cases are refused | PASS |
| TC-03 | Credential absence | State AST scan has no credential fields | PASS |
| TC-04 | InvocationContext | Context is framework `configurable`-only, absent from State | PASS |
| TC-05 | S-4 lifecycle | Domain nodes do not emit framework lifecycle events | PASS |
| TC-06 | S-2 final gate | Direct `_security_gate_input` override raises TypeError | PASS |
| TC-07 | S-3 final gate | Direct `_security_gate_output` override raises TypeError | PASS |
| TC-08 | S-1 trust level | Anonymous caller is denied before `PreProcessNode.execute`。加えて PB-6 の常設フィクスチャで **`execute()` が呼ばれなかったこと**を証明する（`test_pb_invoke_order.py` の `test_s1_denial_refuses_execution_before_execute` — `_PrivilegedTrustGateFixture` を不足 trust で `node(state)` 呼びし、`status=error` / `error_log[0]` の `S-1 trust gate denied` / `events == ["s1_denied"]` / `execute` 未呼び出し を assert。正本 docs/03 の TC-08 要求） | PASS |

## Dangerous-Goods Coverage

| ID | Scenario | Expected Result | Result |
|---|---|---|---|
| DG-01 | Invoke with current-case SDS/packing sources | Closed five-field consistency brief | PASS |
| DG-02 | Caller/customer mismatch | Fails before DB/KB access | PASS |
| DG-03 | Classification request | LLM not called; human review question only | PASS |
| DG-04 | Ingest mode | Dedicated DB/KB append and audit record | PASS |
| DG-05 | Feedback mode | Explicitly approved feedback only is accumulated | PASS |
| DG-06 | `shipment_release` key | Rejected recursively | PASS |
| DG-07 | Alias `releaseDecision` | Rejected after NFKC/casefold normalisation | PASS |
| DG-08 | Missing claim source | Rejected | PASS |
| DG-09 | Source from another case | Rejected | PASS |
| DG-10 | Nested prohibited key (`Shipment Release`) | Rejected recursively | PASS |
| DG-11 | Undeclared extra top-level key | Rejected by closed schema (allowlist) | PASS |
| DG-12 | Tampered `severity` (not `candidate`) | Rejected | PASS |

## Common Parts Coverage

| Part | TESTING.md Item | LOG-C2-122 Concrete Check | Result |
|---|---|---|---|
| cat2-graph-skeleton | G-1 / G-3 | Three-mode `ModeSpec` merge and real BaseGraph execution preserve only mode-scoped fields | PASS |
| cat2-graph-skeleton | G-4 | Subgraph failure projection does not expose inner error text; safe failure codes only | PASS |
| evidence-gate | V-1 | Current-case `evidence_map` with cited source IDs passes; fake/unsafe/misattributed IDs block | PASS |
| evidence-gate | V-3 / V-3-bis | Arabic numeric/comparative claims are flagged; lexical quantity expressions remain intentionally undetected | PASS |
| evidence-gate | V-4 | Typed finding projection rejects undeclared keys, wrong types, invalid enum values, and dict values in `list[str]` | PASS |
| output-envelope | E-1 / E-2 | Closed five-field invoke envelope rejects missing/extra/prohibited DG output fields using production config | PASS |
| payload-store | P-1 / P-2 / P-3 / P-4 / P-5 | Scoped, single-use payload refs reject wrong customer/session, expiry, replay, and unscoped reads | PASS |

## Proof of Boundary

| PB-ID | Boundary | Expected Result | Result |
|---|---|---|---|
| PB-1 | Audit emission | Each node executes a domain trace event | PASS |
| PB-2 | State serialisation | JSON/msgpack-safe primitives only | PASS |
| PB-3 | Service boundary | DB/KB are injected fake seams; no network | PASS |
| PB-4 | Import isolation | No Level 0 SDK import | PASS |
| PB-5 | Checkpoint safety | No credential-bearing State fields | PASS |
| PB-6 | Invoke order | S-1 → S-2 → execute → S-3 order for every node | PASS |
| PB-7 | HITL interrupt | Auto-waived — `hitl.enabled: false` | SKIP |

## Execution Summary

- Command: `pytest tests/ -q` → **193 passed, 2 skipped**
- Command: `pytest tests/proof_of_boundary/ -q` → **10 passed, 2 skipped**
- Part regression: `pytest tests/parts/ -q` → **143 passed**

## 実 LLM 1 回転検証

モックだけで固めた実装は実 LLM を繋いだ瞬間に動かないことがあるため、注入シームへ実 LLM クライアントを刺して 1 回 invoke し、**中身のある出力が出ること**を確認している（`gitproject-dev-flow.md` §14-1-bis）。

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-18 |
| 使用 LLM | ローカル CLI（Azure OpenAI `gpt-5.5`）を `BaseLLM` 実装として注入 |
| 注入シーム | `config[llm]` |
| 実 LLM 呼び出し | 1 回 |
| 出力 | ['evidence_sources', 'inconsistency_candidates', 'inquiry_points', 'verification_questions', 'mode', 'gate_kind'] が非空。縮退理由なし |

本番の LLM は `AnthropicClient` 等で、これは検証用シーム。テンプレの `src/` は変更していない（一時テストを生成→実行→削除する方式）。CI には入れていない（ローカル CLI と鍵が要るため）。
