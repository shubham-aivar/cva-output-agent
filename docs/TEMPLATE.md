# Using the approved Ingram template (SOW D06)

Until Ingram supplies the approved CVA/OLA PowerPoint, the deck uses a clearly labelled PLACEHOLDER design.
When the real template arrives, no code changes: build it in PowerPoint with the tokens and shape names below and run

    python -m cva export RUN --template path/to/ingram_template.pptx

## Text tokens (in any text box or table cell)
`{{status}}` release status, `{{run}}`, `{{date}}`, `{{customer}}`, `{{workbook}}`, `{{grade}}`,
`{{commentary:<slide>}}` where slide is one of executive_summary, cost_comparison, rightsizing, licensing, lifecycle, data_quality,
and `{{F:<key>}}` for any recorded figure (money is formatted as dollars):

- `{{F:estate.workloads}}`  Workloads in scope (count)
- `{{F:estate.windows}}`  Windows workloads (count)
- `{{F:estate.linux}}`  Linux workloads (count)
- `{{F:estate.sql_instances}}`  SQL Server instances (count)
- `{{F:estate.vcpu}}`  Current vCPU (where reported) (vCPU)
- `{{F:estate.ram_gb}}`  Current RAM (where reported) (GB)
- `{{F:estate.storage_tb}}`  Used storage (TB)
- `{{F:estate.data_grade}}`  Data richness grade (grade)
- `{{F:sizing.sized}}`  Workloads sized (count)
- `{{F:sizing.vcpu_rec}}`  Recommended vCPU (vCPU)
- `{{F:sizing.vcpu_cur}}`  Current vCPU of sized workloads (vCPU)
- `{{F:sizing.mem_rec}}`  Recommended memory (GB)
- `{{F:sizing.mem_cur}}`  Current memory of sized workloads (GB)
- `{{F:sizing.downsize}}`  Workloads: downsize (count)
- `{{F:sizing.same}}`  Workloads: same (count)
- `{{F:sizing.upsize}}`  Workloads: upsize (count)
- `{{F:sizing.conf_high}}`  Sizing confidence high (count)
- `{{F:sizing.conf_medium}}`  Sizing confidence medium (count)
- `{{F:sizing.conf_low}}`  Sizing confidence low (count)
- `{{F:sizing.unsized}}`  Workloads that could not be sized (count)
- `{{F:scenario.on_demand.total}}`  On-Demand: annual total (USD/yr)
- `{{F:scenario.on_demand.infra}}`  On-Demand: infrastructure (USD/yr)
- `{{F:scenario.on_demand.windows}}`  On-Demand: Windows licenses (USD/yr)
- `{{F:scenario.on_demand.sql}}`  On-Demand: SQL Server licenses (USD/yr)
- `{{F:scenario.on_demand.storage}}`  On-Demand: storage (USD/yr)
- `{{F:scenario.ri_1yr.total}}`  1-year commitment: annual total (USD/yr)
- `{{F:scenario.ri_1yr.infra}}`  1-year commitment: infrastructure (USD/yr)
- `{{F:scenario.ri_1yr.windows}}`  1-year commitment: Windows licenses (USD/yr)
- `{{F:scenario.ri_1yr.sql}}`  1-year commitment: SQL Server licenses (USD/yr)
- `{{F:scenario.ri_1yr.storage}}`  1-year commitment: storage (USD/yr)
- `{{F:scenario.ri_3yr.total}}`  3-year commitment: annual total (USD/yr)
- `{{F:scenario.ri_3yr.infra}}`  3-year commitment: infrastructure (USD/yr)
- `{{F:scenario.ri_3yr.windows}}`  3-year commitment: Windows licenses (USD/yr)
- `{{F:scenario.ri_3yr.sql}}`  3-year commitment: SQL Server licenses (USD/yr)
- `{{F:scenario.ri_3yr.storage}}`  3-year commitment: storage (USD/yr)
- `{{F:scenario.byol.total}}`  BYOL: annual total (USD/yr)
- `{{F:scenario.byol.infra}}`  BYOL: infrastructure (USD/yr)
- `{{F:scenario.byol.windows}}`  BYOL: Windows licenses (USD/yr)
- `{{F:scenario.byol.sql}}`  BYOL: SQL Server licenses (USD/yr)
- `{{F:scenario.byol.storage}}`  BYOL: storage (USD/yr)
- `{{F:scenario.byol.coverage}}`  BYOL: licensing coverage (%)
- `{{F:scenario.ri3_saving_pct}}`  3-year commitment saving vs On-Demand (%)
- `{{F:licensing.bearing}}`  License-bearing items (count)
- `{{F:licensing.gaps}}`  Items without a licensing finding (count)
- `{{F:licensing.coverage}}`  Licensing input coverage (all products) (%)
- `{{F:licensing.coverage_sql}}`  SQL Server licensing coverage (%)
- `{{F:licensing.coverage_windows}}`  Windows Server licensing coverage (%)
- `{{F:lifecycle.eos}}`  Past end of support (OS or SQL) (count)
- `{{F:lifecycle.approaching}}`  Approaching end of support (count)
- `{{F:lifecycle.at_risk}}`  Workloads at lifecycle risk (count)
- `{{F:quality.total}}`  Data-quality exceptions (count)
- `{{F:quality.error}}`  Exceptions (error) (count)
- `{{F:quality.warn}}`  Exceptions (warn) (count)
- `{{F:quality.info}}`  Exceptions (info) (count)
- `{{F:parity.compared}}`  Workloads compared with a reference (count)
- `{{F:parity.outliers}}`  Parity outliers (count)

A token the agent does not know is left in place and reported in the export notes; it is never silently dropped.

## Named shapes (set the shape's name in PowerPoint's Selection Pane)
A shape named one of these is replaced, at the same position and size, by a native chart or table:
`CHART_cost` (stacked scenario cost), `CHART_estate_os`, `TABLE_scenarios`, `TABLE_lifecycle`.
Add more by extending `build_named_visual` in `cva/outputs/pptx_deck.py`.

## Rules that stay in force
The status token must appear on every slide you want stamped DRAFT, so put `{{status}}` in the master or the footer.
The template is frozen at the end of Pilot Week 2 (SOW risk R-4); changes after that go through change control.
