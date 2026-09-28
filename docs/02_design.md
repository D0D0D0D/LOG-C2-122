# Template Design Specification — LOG-C2-122 Logistics Dangerous-Goods Document Consistency Agent

> 本設計は、設計着手条件 (a)〜(e) を schema / API / テストに落とすことを目的とする。

## Position in AgentCore Architecture

- **Agent Class**: `DangerousGoodsDocConsistencyAgent`（outer graph・`AgentBaseGraph` 継承）
- **L1 Base**: **AgentBaseGraph**（L1 直継承）。`AutonomousBaseGraph` ではない（自律 think→act ループを持たない、固定パイプラインの Cat 2）。
  - 本テンプレートは旧 Level 2 base 継承モデルを使用しない（v0.16 で「Level 2 base → L1 直接継承」へ移行済み）。危険品書類整合という多段ワークフローは、outer の `AgentBaseGraph` 固定 5 ノード backbone の `main` スロットに `GraphNode`（inner domain workflow graph）を差し込んで表現する。
- **Three-Layer Separation**:
  - State: flat TypedDict composition（Pydantic 不可 — msgpack 非互換）。案件参照 ID・モード・レビュー状態など primitives / JSON-serializable のみ保持。
  - Node: L1 inheritance（Template Method: `execute(self, state: dict) -> dict` のみ override）。
  - Graph: composition（outer は `register_nodes()` で `main` に GraphNode を差し込む。inner は独自トポロジ）。

## Architecture Overview

本テンプレートは **DocGenerationAgent パターン**（整合レビューブリーフという構造化ドキュメント成果物を生成する）を Cat 2 として実装する。outer graph は固定 5 ノード backbone、ドメインの多段ロジックは `main` の `GraphNode`（inner `DangerousGoodsWorkflowGraph`）に隠蔽する。

### 3 モード

エントリポイント `src/api/server.py` は 3 モードを受ける。モードは `state["mode"]` に載せ、inner graph が分岐する。

| モード | 目的 | 主な作用 |
|--------|------|---------|
| `ingest` | 顧客の DG 出荷パターン・過去差戻し理由・照会履歴を構造化して DB / KB へ蓄積 | DB 書込（監査イベント付き）。分類確定/出荷可否の結論ラベルは検索軸・model payload から除外。 |
| `invoke` | 新規 DG 案件について、既申告危険品情報 × 裏付け書類を IATA DGR / IMDG / ADR に照らして整合照合し、矛盾候補・要照会論点を抽出 | 読み取り主体。整合レビューブリーフの draft + 根拠マップを返す。 |
| `feedback` | 人間が確定・編集した論点を、明示承認 + 監査イベント付きで DB / KB へ反映 | DB / KB 書込（承認済みのみ）。 |

### Node Configuration（outer graph — 固定 backbone）

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| initialize | 案件 context 初期化（framework 既定） | user_input, mode | validated context | InitializeNode (default) |
| pre_process | 呼出元認証→認可顧客の解決（未解決は fail-closed）・provision 済み顧客専用接続の確定・S-2 入力最小化・案件参照 ID 化 | user_input, mode | validated_input, case_ref | `PreProcessNode`（FunctionNode） |
| main | inner `DangerousGoodsWorkflowGraph` を GraphNode で実行（Stage1〜4） | validated_input, case_ref, mode | consistency_brief, evidence_map, requires_human_review | `DangerousGoodsWorkflowGraphNode`（GraphNode） |
| post_process | 根拠リンク実在性/対応検証・情報再走査・分類確定/出荷可否フィールドの構造遮断（S-3 拡張） | consistency_brief, evidence_map | formatted_output, status | `PostProcessNode`（FunctionNode） |
| finalize | 出力整形（framework 既定） | formatted_output | final output | FinalizeNode (default) |

### Node Configuration（inner `DangerousGoodsWorkflowGraph` — 独自トポロジ）

inner graph は `BaseGraph` を直接継承し、独自ノード名で 4 ステージを構成する（`src/graph/domain_workflow_graph.py`）。ingest/invoke/feedback で通るステージが異なる（`route()` で分岐）。

| Node | 対応 Stage | Responsibility |
|------|-----------|----------------|
| deterministic_check | Stage 1 | 認可顧客の専用接続内の案件データを読み、必須書類の有無、数値項目の突合、包装等級×クラスの組み合わせ表を決定論的に検証（config 表）。整合の意味的結論は出さない。→ `docs_present` / `field_diff` |
| kb_recall | Stage 2 | 認可顧客の**専用 KB 接続**から過去差戻し理由・照会観点を、retrieval-safe projection（source-linked・非結論的・provenance 付き）だけ検索。結論は継承しない。 |
| rule_consistency_llm | Stage 3 | LLM が既申告情報を IATA DGR / IMDG / ADR に当てはめ、裏付けとの整合の矛盾候補・要照会論点を抽出。事実と推論を分離。 |
| claim_source_verify | Stage 4 | 全論点に根拠 link を要求。claim→source の対応・顧客専用接続の境界・禁止フィールド・分類確定/出荷可否要求を検査。 |

> `ingest` / `feedback` モードは Stage 3（LLM 照合）を通らず、**認可顧客の専用接続内**への構造化蓄積のみを行う（`route()` で `deterministic_check` → 蓄積ノードへ分岐）。inner の蓄積ノード `accumulate_ingest`（ingest）/ `accumulate_feedback`（feedback）も pre_process が解決した専用 DB / KB 接続に書き込む（default 接続への書込は fail-closed）。

### inner `DangerousGoodsWorkflowGraph` の BaseGraph ABC 実装（7 メソッド）

inner graph は `BaseGraph` を直接継承するため、以下 7 つの抽象メソッドをすべて実装する（`domain_workflow_graph_sample.py` 準拠）。`register_nodes()` は `super()` を呼ばない（`BaseGraph.register_nodes()` は abstract）。`initialize` / `finalize` は inner に登録しない（outer backbone の責務）。

| メソッド | 実装内容 |
|---------|---------|
| `name` | `"dg_doc_consistency_workflow"` |
| `state_schema` | `State`（outer と共有の flat TypedDict） |
| `_validate_config` | `retrieval` / `llm` 必須キーの存在確認（不足時 `ConfigError`） |
| `register_nodes` | `deterministic_check` / `kb_recall` / `rule_consistency_llm` / `claim_source_verify` / `accumulate_ingest` / `accumulate_feedback` を登録 |
| `add_edges` | mode 別トポロジを `add_conditional_edges(START, self.route)` で分岐。invoke は 4 ステージ直列、ingest/feedback は蓄積ノードのみ。全ノードが START から到達可能。 |
| `route` | `state["mode"]` と `state["status"]` で分岐（invoke → `deterministic_check`、ingest → `accumulate_ingest`、feedback → `accumulate_feedback`、error → END） |
| `get_output` | `consistency_brief` / `evidence_map` / `requires_human_review` / `status` を返す。outer `merge_output()` と対で設計。 |

### Node 別 `required_trust_level`（S-1）

`FunctionNode` サブクラスは class body で `required_trust_level` を明示宣言する（manifest レベルの trust では代替不可）。

| Node | required_trust_level | 根拠 |
|------|---------------------|------|
| pre_process | `VERIFIED_EXTERNAL` | 外部（輸出担当）からの入力を受け、呼出元認証→認可顧客→専用接続を解決する標準業務操作 |
| deterministic_check | `VERIFIED_EXTERNAL` | 案件データを読む |
| kb_recall | `VERIFIED_EXTERNAL` | 認可顧客の専用 KB 接続を読む |
| rule_consistency_llm | `VERIFIED_EXTERNAL` | LLM 照合 |
| claim_source_verify | `VERIFIED_EXTERNAL` | 根拠検証 |
| accumulate_ingest / accumulate_feedback | `VERIFIED_EXTERNAL` | DB / KB への構造化書込（監査イベント付き） |
| post_process | `VERIFIED_EXTERNAL` | 出力整形・S-3 拡張 |

> TC-08（S-1 境界）は各 privileged node を `node(state)`（`__call__` 経由）で呼び、trust 不足時に `execute()` が呼ばれないことを証明する（MR-B）。

### Data Flow

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                          ↓ (retry, max 3)
                                        pre_process

  inside main (DangerousGoodsWorkflowGraphNode → inner graph):
    invoke:   START → deterministic_check → kb_recall → rule_consistency_llm → claim_source_verify → END
    ingest:   START → deterministic_check → (structured accumulate) → END
    feedback: START → (approved accumulate + audit) → END
```

### State Definition

State は flat TypedDict（`AgentState` を継承）。生の機微情報・書類全文は保存せず、案件参照 ID・モード・レビュー状態に最小化する。

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| mode | str | `ingest` / `invoke` / `feedback` | Yes |
| case_ref | str | DG 出荷案件の参照 ID（生データではなく ID） | Yes（invoke/feedback） |
| validated_input | str | pre_process が検証・最小化した後の JSON 文字列（GraphNode `extract_input()` が inner `user_input` として渡す）。生 SDS 全文・機微値は含めず、参照 ID・許可 business キーの allowlist 射影のみ。 | Yes |
| transport_mode | str | `air`（IATA DGR）/ `sea`（IMDG）/ `road`（ADR）— 適用規則コーパスの選択キー | invoke |
| rule_edition_ref | str | 顧客承認済み規則コーパスの版・発効日を指す参照 ID（AG は規則全文を解釈しない） | invoke |
| declared_items | list[dict] | 既申告危険品情報（PSN / UN / class / packing_group / quantity の参照化表現） | invoke |
| docs_present | dict | Stage 1: 必須書類の有無 | — |
| field_diff | list[dict] | Stage 1: 数値項目突合の差分 | — |
| recall_refs | list[dict] | Stage 2: 過去差戻し照合観点（source ID + provenance のみ） | — |
| consistency_brief | dict | Stage 3: 整合レビューブリーフ draft（下記 5 フィールド固定） | invoke |
| evidence_map | list[dict] | claim→source の対応マップ | invoke |
| requires_human_review | bool | 根拠不足・不整合時の fail-closed フラグ | — |
| formatted_output | dict | post_process 整形済み出力 | — |

**出力契約（consistency_brief の固定フィールド）:**
`declared_items` / `evidence_sources` / `inconsistency_candidates` / `inquiry_points` / `verification_questions` の 5 フィールドを別フィールドに固定する。
**`classification_final`・`shipment_release` に相当するフィールドは出力スキーマに持たない**（S-3 で構造遮断）。

**State Constraints (mandatory):**
- Flat TypedDict only（primitives + JSON-serializable types）。書類全文・生 SDS は state に載せない（参照 ID 化）。
- No JWT, API keys, credentials in State（checkpoint DB leakage）。
- InvocationContext via `config["configurable"]` only（not in State）。secrets は `ctx.secrets.require()`。
- No Pydantic models, dataclass, arbitrary Python objects（msgpack incompatible）。

## Framework Utilization

### Shared Components Used
- [x] InvocationContext（correlation_id, session_id, permissions, credential handle）— `InvocationContext.from_state(state)` 経由のみ。直接構築しない。
- [x] ConnectionPolicy（retry/timeout strategy）
- [x] SecurityViolationError
- [x] **S-2: `_extra_security_gate_input()`** — `PreProcessNode`（FunctionNode）に実装。認可顧客が解決できない（provision 済み接続が無い）時は fail-closed（default KB なし）、入力最小化、機微情報を state / log に残さない。business キーは allowlist 方式で検証。入力に `customer_id` 等が含まれる場合は**認可済み顧客との照合のみ**に使い、検索の絞り込みキーには使わない。
- [x] **S-3: `_extra_security_gate_output()`** — `PostProcessNode`（FunctionNode）に実装。閉じた 5 フィールド schema（allowlist）検査、`classification_final` / `shipment_release` 相当キー（別名含む）の**再帰的構造遮断**、`severity == "candidate"` 固定、claim→source 対応検証、全論点の根拠リンク必須。不足時は `requires_human_review` または fail-closed。非結論性は**構造**で担保し、自由文の禁止フレーズ denylist には依存しない。
- [x] **S-4: `emit_trace_event()`** — 各ノードの `execute()` 内で最低 1 回、ドメインイベントを emit（副作用の有無を問わず）。`node_start` / `node_complete` / `node_error` は framework が自動 emit するため重複させない。

> **S-2/S-3 gate behaviour by node type（ADR-017）:**
> - `PreProcessNode` / `PostProcessNode` は `FunctionNode` サブクラス → framework `@final` gate が自動実行。ドメイン拡張は `_extra_security_gate_input()` / `_extra_security_gate_output()` のみ。`_security_gate_*` を直接 override しない（class 定義時 `TypeError`）。
> - `main` の `DangerousGoodsWorkflowGraphNode` は `GraphNode` サブクラス → S-2/S-3 は deliberate no-op（gating は inner graph の各ノードと outer pre/post で担う）。inner graph の各 `FunctionNode` が自身の gate を持つ。

> **S-3 構造遮断 + claim→source の二本立て:** post_process の `_extra_security_gate_output()` は、
> (1) **構造遮断（allowlist）** — 閉じた 5 フィールド schema・`classification_final` / `shipment_release` 相当キー（別名・再帰）の拒否・`severity=="candidate"` 固定、
> (2) **claim→source 対応検証** — 全 inconsistency_candidate / inquiry_point が現在案件の実在する evidence_source に紐づくことを検証、
> の両方を担う。非結論性は構造（schema + severity 固定）で担保し、自由文の禁止フレーズ denylist には依存しない（すり抜け可能で false assurance になるため）。

### Composition Pattern

- **Pattern**: **GraphNode（subgraph）** — outer `AgentBaseGraph` の `main` スロットに `DangerousGoodsWorkflowGraphNode`（`GraphNode` サブクラス）を差し込み、inner `DangerousGoodsWorkflowGraph`（`BaseGraph`）を実行する。
- **Composition target**: `src/graph/domain_workflow_graph.py`（inner graph の唯一の受理パス）。
- **GraphNode 必須メソッド**:
  - `get_subgraph()` — inner `DangerousGoodsWorkflowGraph` を返す。
  - `extract_input()` — `state["validated_input"]`（pre_process 設定済）を inner `user_input` へ。
  - `merge_output()` — inner の `get_output()` が返す `consistency_brief` / `evidence_map` / `requires_human_review` / `status` を outer state へ**明示マッピング**（raw pass-through しない）。
- **Error propagation strategy**: **propagate**（inner エラーは `SubgraphError` として再送出。fail fast）。`error_strategy = "propagate"` を明示宣言。
- **propagate_hitl**: `False`（本テンプレートは HITL を outer に伝播させない。壁打ち HITL は inner の feedback ループで扱う）。

## HITL 方針

本テンプレートは `hitl.enabled` を **false** とする（MR-A 時点の設計方針）。人間の関与は `/feedback` モード（非同期に人間が確定した論点を明示承認付きで蓄積する）で実現し、backbone の同期 `interrupt()` は使わない。よって PB-7 は auto-waived（non-HITL）。
※ Design レビューで同期 HITL（低信頼時に `interrupt()` で人間確認）が必要と判断された場合は、`hitl.enabled: true` + `memory_enabled: true` + `hitl_allowed` ガード付き D6 パターンへ切り替える（この場合 PB-7 が必須化）。

## AGENTIC STAR Marketplace entry adapter

Repository-root `cli.py` defines `MarketplaceGraph`, a subclass of
`DangerousGoodsDocConsistencyAgent`. The Marketplace runner still supplies one plain string; for
this AG that string is the JSON serialization of the existing `invoke` packet. The adapter never
turns conversation history into domain input. It deletes the runner `input_context` and rebuilds
`{"customer_id": <deployment customer>}` from deployment-owned environment data.

The deployment boundary consists of `STG_DEFAULT_CALLER_ID` and
`STG_DEFAULT_CUSTOMER_ID`. The adapter uses the former for `ctx.caller_id`, the latter for both the
packet `customer_id` and rebuilt context, and installs the exact one-to-one
`caller_customer_map`. Missing, empty, or whitespace-containing values fail during graph
construction, before compilation. `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, and
`AZURE_OPENAI_DEPLOYMENT` are validated by `provision_secrets()` after compilation and before the
runner starts serving. The allowlisted provider exposes no other process environment values.

Only `invoke` is available. `ingest`, `feedback`, and unknown modes are converted to a packet that
the existing input gate rejects; the Marketplace DB/KB fallbacks are read-only and raise if a
write method is reached. The adapter also forces `VERIFIED_EXTERNAL`, sets `hitl_allowed=False`,
disables graph HITL/checkpoint memory, and treats an unexpected `AWAITING_HUMAN` result as a
runtime failure.

Azure is injected only through the existing `config["llm"]` seam. Connection values are read from
the bound secret provider and a new client is constructed for every LLM operation; neither the
client nor its connection values are retained on the adapter LLM object. The optional
`AzureOpenAIClient` import occurs inside that operation. AgentCore 1.0.1 compatibility fallbacks
cover `load_agent_config`, Marketplace bootstrap, environment provider, and Azure client support.

### State dependency inventory

The adapter design follows the complete state flow rather than only searching
`input_context.get(...)`:

| Component | State read | State produced / effect |
|---|---|---|
| outer `PreProcessNode` | `user_input`, `caller_id`, `status` | validates caller/customer and packet shape; produces `mode`, `case_ref`, `validated_input`, `payload_ref` |
| outer `DangerousGoodsWorkflowGraphNode` | `status`, then `mode` and `validated_input` | constructs the inner envelope and merges the mode-scoped inner result |
| inner graph router | `status`, `mode` (or `user_input` fallback) | selects `deterministic_check`; Marketplace does not expose write routes |
| `DeterministicCheckNode` | `status`, packet in `user_input` | deterministically produces `docs_present`, `field_diff`, current-case `retrieved_records`, and `requires_human_review` |
| `KBRecallNode` | `status`, `retrieved_records`, `transport_mode`, `requires_human_review` | adds retrieval-safe `recall_refs` without changing the deterministic review flag |
| `RuleConsistencyLLMNode` | `status`, `review_request`, `retrieved_records`, `field_diff`, `docs_present`, `declared_items`, `rule_edition_ref`, `case_ref`, `requires_human_review` | produces a schema-validated, non-conclusive brief and preserves the deterministic review flag |
| `ClaimSourceVerifyNode` | `status`, `consistency_brief`, `retrieved_records`, `case_ref`, `requires_human_review` | enforces current-case citations and can only add a low-confidence review requirement |
| `AccumulateIngestNode` / `AccumulateFeedbackNode` | `status` and packet in `user_input` | write-mode summaries; unreachable from Marketplace |
| outer `PostProcessNode` | `status`, `mode`, mode result fields, `case_ref`, citation/evidence fields | applies the closed five-field/prohibited-key/provenance gate and emits the envelope |

Document presence is therefore not decided by Azure. The deterministic node compares source
`doc_type` values with `required_documents_by_transport`; the LLM receives that result only to
prepare a review brief. Its output cannot clear `requires_human_review`, and all claim/source and
non-conclusive output gates remain unchanged by the adapter.

## Import Isolation Confirmation
- [x] Template does not import agenticstar-platform SDK（Level 0）
- [x] Import targets: `framework/` and `shared/` only（no agents/base/ required）

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph** | 固定パイプラインの Cat 2。自律 think→act ループを持たない。多段ロジックは main の GraphNode で表現。 |
| Composition pattern | Standalone FunctionNode | GraphNode (subgraph) | **GraphNode** | Stage1〜4 の多段ドメインワークフローを inner graph に隠蔽し、outer は固定 backbone を維持する。 |
| 品目分類の扱い | AG が PSN/UN/class を一次決定 | 既申告情報 × 裏付けの整合照合に限定 | **整合照合に限定** | 高責任規制（分類の一次決定）への踏み込みを避け、有資格者に残す（decision.md §2, §11 R5）。 |
| 分類/出荷可否フィールド | 出力 schema に含める | schema / API / S-3 から構造排除 | **構造排除** | 誤用（有資格者判断の軽視）を防ぐ。S-3 の拒否条件として構造化する。 |

## データ層設計（blocker (a) / (d)）

**顧客分離モデル（重要・姉妹テンプレート Customs Shipment Document Completeness Validator に準拠）:** DB / KB は**マルチテナント共有ストアではなく、顧客専用（物理分離）に provision された接続**である。分離は **KB / DB 接続レベル**で担保する — 呼出元を認証（caller_id）→ 認可された顧客を解決 → その顧客に **provision 済みの専用接続**を使う。入力に含まれる `customer_id` 等は**認可済み顧客との照合のみ**に使い、行を絞り込む検索キーには使わない。**default KB は持たない**（認可解決不能なら fail-closed）。したがって scoped-retrieval / payload-store の `scope_keys`（＝1 顧客 KB 内の混ぜてはいけない区画用）は**導入しない**（DG ドメインに顧客内区画は無い）。

**二層分離の原則:** 監査用の**原記録（DB）**と、model 可視の **retrieval-safe projection（KB）** を別レイヤに置く。KB には結論ラベル・生 SDS 全文をベクトル化しない — source-linked な非結論的照合観点 + provenance のみを射影する。

### DB schema（顧客専用接続内・原記録・monotonic append）

> 各テーブルは**認可済み顧客専用の接続内**に存在する（行レベルの tenant カラムで共有ストアを分割するのではない）。

| テーブル | 主キー | 主なカラム | 禁止カラム |
|---------|--------|-----------|-----------|
| `dg_shipment_cases` | `case_ref` | `transport_mode`, `declared_items_ref`（参照 ID）, `submitted_at`, `status` | 生 SDS 全文・分類確定結論 |
| `transport_rule_refs` | `rule_edition_ref` | `transport_mode`, `edition`, `effective_date`, `source_uri`（顧客承認済 1 版） | — |
| `return_history` | `return_id` | `case_ref`, `return_reason_ref`, `resolved_by_inquiry`, `recorded_at` | 出荷可否結論ラベル |
| `feedback_audit_events` | `event_id` | `case_ref`, `actor`, `action`（approve/edit/reject）, `payload_ref`, `at` | 生機微値 |

### KB projection（retrieval-safe・model 可視）

`return_history` / 承認済み feedback から**許可フィールドだけ**を射影する allowlist 方式。

| projection フィールド | 型 | 説明 |
|----------------------|-----|------|
| `source_id` | str | 原記録への provenance 参照（実在性検証対象） |
| `check_observation` | str | 過去差戻しの照合観点（非結論・要約済み・承認済みのみ） |
| `transport_mode` | str | 検索スコープ絞り込み（顧客専用接続内での輸送手段別絞り込み） |
| `provenance` | dict | `{case_ref, recorded_at}` |

> **禁止射影:** `classification_final` / `shipment_release` / 出荷可否結論 / 生 SDS span は KB に射影しない（prompt に渡さない）。

> **recall refs は claim の根拠にならない:** Stage 2 の `recall_refs`（過去案件由来・別 `case_ref`）は LLM への**照合観点のヒント**にすぎず、`evidence_sources` にはならない。矛盾候補・要照会論点の根拠は**現在案件（当該 `case_ref`）の裏付け書類 source のみ**。Stage 4 の claim→source 契約は現在案件 source を要求し、recall 由来の別 case_ref を claim の evidence として受け付けない。

### 顧客データ統治（blocker (d)）

- **最小 export セット:** `dg_shipment_cases`（申告項目参照）+ `return_history`（差戻し理由・照会観点）。生 SDS 全文の export は不要（照合観点のみ）。
- **保持・削除:** 顧客専用接続単位で顧客のデータ統治に従う。削除要求時は当該顧客の DB 原記録 + KB projection の両方を接続内で削除。
- **監査:** すべての ingest / feedback 書込は `feedback_audit_events` に actor + action + provenance を残す。
- 詳細な IAM（caller_id → 認可顧客 → provision 済み接続）・export 方式は `docs/07_operation_guide.md`（MR-B）で顧客ごとに具体化。

## 規則参照インターフェース（blocker (b)）

LLM（Stage 3）は規則全文を解釈せず、顧客承認済みの **1 版**（`transport_mode` に対応する DGR/IMDG/ADR）を参照 ID で受ける。照合ロジックの入力契約:

| 入力 | 由来 | 用途 |
|------|------|------|
| `declared_items` | 既申告危険品情報（PSN/UN/class/packing_group/quantity の参照化表現） | 照合対象 |
| `evidence_refs` | SDS・包装証明の source ID + 抜粋 span | 裏付け |
| `rule_edition_ref` + `transport_mode` | `transport_rule_refs` の承認済 1 版 | 適用規則コーパス |
| `recall_refs` | Stage 2 KB projection | 過去照合観点 |

**出力（非結論）:** 矛盾候補・要照会論点のみ。**分類確定要求（PSN/UN/class の一次決定）が入力に含まれても LLM は応答せず、`requires_human_review` を立てる**（S-3 で拒否）。

## S-3 出力安全契約（blocker (c)・testable schema）

`consistency_brief` は**閉じた** schema とする（`additionalProperties: false`）。5 フィールド固定:

```
consistency_brief:
  additionalProperties: false
  required: [declared_items, evidence_sources, inconsistency_candidates, inquiry_points, verification_questions]
  properties:
    declared_items:            # list[dict]  {item_ref: str, psn: str, un: str, class: str, packing_group: str, quantity: str}
    evidence_sources:          # list[dict]  {source_id: str, doc_type: str, quote_span: str, provenance: dict}
    inconsistency_candidates:  # list[dict]  {claim: str, evidence_source_id: str, rule_ref: str, severity: "candidate", confidence: float}
    inquiry_points:            # list[dict]  {question_ref: str, evidence_source_id: str, rule_ref: str}
    verification_questions:    # list[str]   人間（有資格者）への確認質問。断定を含まない
  # classification_final / shipment_release / release_decision 等の結論キーは schema に存在しない
```

> 各 `inconsistency_candidate` の `severity` は `"candidate"` 固定（断定 severity を持たせない）。`confidence` は 0.0–1.0 の float で、閾値未満は Stage 4 で `requires_human_review` に振る。全 item の `evidence_source_id` は `evidence_sources[].source_id` に実在参照すること（claim→source 契約）。

**非結論性の担保は「構造」で行う（denylist に依存しない）:** 出荷可否・分類確定を出させない担保は、
禁止語 denylist（断定文言の遮断リスト）ではなく **allowlist（閉じた schema）+ 構造制約**で行う。
自由文の断定表現を denylist で網羅しようとすると言い換えですり抜け、守っているように見えて守っていない
false assurance を生むため、本テンプレートは**自由文の結論文言 denylist を持たない**（allowlist が担保、
禁止フレーズ blacklist は担保にしない）。断定は以下の構造で作れないようにする。

**S-3 `_extra_security_gate_output()` の拒否条件（受入テスト化・MR-B）:**
1. **閉じた schema（allowlist）:** 出力 dict は closed 5 フィールドのみ（`additionalProperties: false`）。未宣言キーは名称を問わず reject。
2. **禁止キーの再帰排除:** `classification_final` / `shipment_release` / `release_decision` / それらの別名（alias・全半角・大小・区切り差）キーが**再帰的に**存在したら reject（NFKC + casefold + collapse で正規化）。
3. **severity 固定:** `inconsistency_candidate` は `severity == "candidate"` 固定・`confidence` は 0.0–1.0 の float。断定 severity（confirmed 等）は reject＝そもそも「候補」の粒度しか作れない。
4. **claim→source 契約:** 各 `inconsistency_candidate` / `inquiry_point` は `evidence_source_id` を持ち、それが `evidence_sources[].source_id` に実在参照すること。参照先 source は (i) 実在し、(ii) **認可顧客の専用接続内の当該 `case_ref` スコープ**（現在案件）に属し、(iii) 引用 span / 参照位置（`quote_span`）を持つこと。1 つでも欠けたら `requires_human_review=True` または fail-closed。

**受入テスト（MR-B で実装済み）:** accept/reject の対テストを用意する — (accept) 全論点が有効 source を持つ正常ブリーフ、(reject) `shipment_release` キー混入 / 別名混入 / **ネスト禁止キー** / **未宣言キー混入** / severity 改竄 / 根拠なし論点 / 認可顧客の接続外 source 参照。
※ 自由文 claim が出荷可否を言い換えで示唆するケースは、denylist で個別に潰さず「AG は candidate 粒度の非結論しか出さない」という**構造**（severity 固定 + closed schema）で扱う。文言網羅は追わない。

## 隣接テンプレートとの差異（具体比較）

| 観点 | Customs Shipment Document Completeness Validator（通関） | SDSRevisionEvidenceAgent（SDS 改訂） | 本テンプレート LOG-C2-122（DG 申告） |
|------|--------------------------|------------------------------|-------------------------------|
| 参照規則体系 | 税関 / HS 分類 | GHS / REACH | IATA DGR / IMDG / ADR |
| 入力ソース | SKU / 品目 → 必要証憑 | SDS 改訂主張 × 製品安全 | 既申告 DG 情報 × SDS/包装証明 |
| KB projection | 通関証憑ルール | SDS 改訂履歴観点 | DG 差戻し照合観点 |
| 出力物 | 証憑充足ブリーフ | SDS 整合ブリーフ | DG 整合レビューブリーフ（分類/出荷可否を持たない） |

規則体系・入力・KB・出力が異なり、config 複製では吸収されない固有 schema である。

## Design-Entry Blocker（decision.md §12）の解決状況（サマリ）

| # | blocker | 解決の所在 |
|---|---------|-----------|
| (a) | DB schema + KB retrieval-safe projection の分離 | 上記「データ層設計」DB schema / KB projection 表 |
| (b) | 品目分類を外し既申告情報×裏付け整合に限定する IF・照合ロジック | 上記「規則参照インターフェース」入力契約 + 非結論出力 |
| (c) | S-3 安全契約を schema / 受入テストへ | 上記「S-3 出力安全契約」閉じた schema + 4 拒否条件 + accept/reject テスト |
| (d) | 顧客 export 最小要件・統治契約 | 上記「顧客データ統治」最小 export / 保持削除 / 監査 |
| (e) | 隣接テンプレートとの差異明文化 | 上記「隣接テンプレートとの差異」比較表 |
