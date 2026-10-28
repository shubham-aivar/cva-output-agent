# Design

## Workflow or agent? (stated plainly)
The calculation is code and always will be: ingest, normalise, size, price, find, render. Given the same inputs it
returns the same figures, which is what SOW 5.2 (traceability to source data, rule and version) needs. That part is
a pipeline, not an agent.

The agent is the layer above it. A bounded tool loop where the model chooses the next tool, reads results, and
proposes. Its tools are read tools and propose-only tools; no tool can change a figure, an input or a decision
(a test fails if one is added). An orchestrator loop decides which specialist tasks a run needs.

| Task | What the model decides | What it can propose |
|---|---|---|
| intake | which unmapped header (if any) is a missing required field | a column mapping |
| licensing | which gaps matter most; whether interview notes contain evidence | questions for the analyst; an assumption backed by a VERBATIM note quote |
| parity | which outliers are material and whether the reference choice is reasonable | an instance override with reasons; a review flag |
| narrative | the wording of each slide's commentary | commentary, rejected unless every number is a recorded figure and no advice is given |
| orchestrator | which of the above this run needs | none itself |

The offline `StubLLM` follows a fixed readable plan per task so the demo and tests run without AWS. It is labelled
"stub" everywhere and is NOT an agent. `BedrockLLM` is the real model (Converse API with tool use); it has not been
exercised against a real account in this build.

## Layers
1. Canonical model (`cva/model.py`): estate, workload, licensing finding, figure ledger. Nothing downstream knows
   which tool produced the data.
2. Adapters (`cva/ingest`): Cloudamize OLA export, Cloudamize Download-All, Cloudamize MPA (layout unverified),
   RVTools; licensing (none, analyst sheet, A02 JSON); config-driven column mapping.
3. Engines (`cva/engines`): rightsizing, pricing (four scenarios), licensing resolution, lifecycle, data quality,
   parity, ledger. Rule ids and versions are in `rules.py`.
4. Agent (`cva/agent`): tools, loop, tasks, checker, interview notes.
5. Review (`cva/review`): run store, hash-chained audit log, release gate, KPIs.
6. Outputs (`cva/outputs`): deck (placeholder design or approved template), PDF copy, traceable workbook,
   exceptions CSV.
7. Service, CLI and local UI.

## Decisions taken (assumptions: change them in config, not code)
* "BYOL" scenario = on-demand purchase plus the customer's own licenses where the licensing input says they are eligible.
* No licensing input means BYOL is not computable, never guessed. Windows with no BYOL finding stays license-included.
* Sizing: CPU target 80%, memory 85%; cheapest fitting instance; burstable only when idle; every result states its basis.
* Data grade A to D travels with the figures; snapshot-only data (RVTools) is grade C and labelled directional.
* Findings from an AI proposal are always labelled; an accepted assumption is shown as "assumption, not evidence".
* Accepting any decision that changes figures marks pending commentary stale and expires confirmation.
* A finalised run is locked until reopened with a reason.

## SOW traceability (A01, section 6.2)
| SOW requirement | Where |
|---|---|
| Template-conformant OLA package, PPTX with PDF copy | `outputs/pptx_deck.py`, `outputs/pdf.py` |
| Cost comparison for BYOL, 1-yr RI, 3-yr RI, On-Demand | `engines/pricing.py`, `config/scenarios.toml` |
| Rightsizing per workload | `engines/rightsize.py` |
| Risk and lifecycle (end-of-life) findings | `engines/lifecycle.py`, `config/rules/lifecycle.toml` |
| Data-quality exceptions list | `engines/quality.py`; Exceptions sheet and CSV |
| Workbook tracing every slide figure to workload data | `outputs/xlsx_trace.py`; Ledger sheet; figure ids on every slide |
| Licensing input (analyst in Pilot, A02 later) | `ingest/licensing_in.py`, `docs/INTEGRATION.md` |
| AWS Transform integration, adapter, console fallback, parity | `transform/adapter.py`, `engines/parity.py` |
| Human-in-the-loop review gate before release | `review/gate.py`; DRAFT stamp on every output until finalised |
| Audit log of inputs, outputs, approvals, versions | `review/audit.py` |
| KPIs: turnaround, analyst hours, first-pass acceptance, OLA-to-MAP | `review/kpis.py` (OLA-to-MAP is recorded by a person; it is not measurable in the pilot window) |

## Combined entity: A01 + AWS Transform (v0.2)
One source of record per figure, so nothing is calculated twice on the slides:
| Figure | Source of record |
|---|---|
| Scenario totals, EC2/EBS/network/support split, on-premises baseline, servers in scope | **AWS Transform** (imported unchanged, rule `AT-IMPORT`, with job ids) |
| Licensing coherence, gaps, evidence from notes; end-of-support; Cloudamize-specific flags; data richness | **A01** |
| BYOL total | **A01 estimate**, labelled, until AWS Transform is given database data and a licensing finding |
| Per-server sizing slide | A01 estimate, labelled, until AWS Transform's per-server file layout is seen |
| Exceptions | one list: A01's plus AWS Transform's warnings (`TRW-*`) |
A01's own engine remains as the offline fallback and as the independent cross-check (ledger keys `a01.*`; aggregate parity in
`parity.totals.*`). The scenario view (`cva/outputs/scenario_view.py`) is the single place the deck, workbook, UI and agent read from.
