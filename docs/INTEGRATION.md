# Integration contracts: what changes when real material arrives

A01 is built so that each unknown lives in ONE place. This page lists each unknown, where it lives, and the
change needed when it becomes known.

| Unknown today | Where it lives | When it is known, change |
|---|---|---|
| Real MPA export column names | `config/mappings/cloudamize_mpa.toml` | edit aliases (or accept the agent's mapping proposal). No code. |
| Cloudamize layout drift (new column names) | `config/mappings/cloudamize_*.toml` | same; the intake task proposes mappings, a person accepts |
| Licensing analyst sheet layout (OI-4) | `config/mappings/licensing_analyst.toml` | edit aliases |
| A02 output format (Release 2) | `cva/ingest/licensing_in.py` `parse_findings` | map A02's fields into `LicensingFinding`; the engines do not change |
| AWS Transform results export layout | `config/mappings/aws_transform.toml` | edit aliases after the first real export |
| Real AWS Transform response shapes (messages, artifacts) | `cva/transform/shape.py`, `cva/transform/driver.py` | run `transform-run` once; the trace file `out/transform_job/transform_trace.jsonl` shows what to adjust |
| AWS Transform per-server results (`analysis_*.xlsx`) | `cva/transform/results.py` | add a parser once the file's layout is seen |
| Real prices | `config/rules/pricing_catalog.toml` | replace with an AWS Price List snapshot of the same shape; set `placeholder = false` |
| Lifecycle dates | `config/rules/lifecycle.toml` | verify each date at the vendor page, set `verified = true` |
| Ingram's four scenario definitions | `config/scenarios.toml` | edit; figures and slides follow |
| The approved deck template | `docs/TEMPLATE.md` | build the template with tokens; pass `--template` |

## Licensing findings contract, version `licensing-findings/0` (Aivar-proposed)
Defined from the columns of the AWS OLA template (Active SQL SA?, License SQL By, AWS SQL Licensing Model,
Include/Exclude from license count) and Microsoft's published BYOL rules. It is a PROPOSAL until Ingram or A02
replaces it. File: JSON.

```json
{
  "schema": "licensing-findings/0",
  "source": "A02",
  "generated": "2026-10-19",
  "findings": [
    {"workload": "PROD-SQL01", "product": "sql_server", "instance": "MSSQLSERVER",
     "model": "byol", "tenancy": "shared", "licensed_by": "cores", "active_sa": true,
     "include_in_count": true, "cores": 8, "basis": "Active SA through 2027-06-30", "confidence": "high",
     "exceptions": []},
    {"workload": "FILE-01", "product": "windows_server", "model": "byol", "tenancy": "dedicated",
     "basis": "Perpetual license purchased 2018", "confidence": "medium"}
  ]
}
```
* `model`: `byol` | `li` | `unknown`. `tenancy`: `shared` | `dedicated` | `any`. `licensed_by`: `cores` | `server_cal`.
* Coherence rules enforced by A01 (not decided by it): SQL Server BYOL on shared tenancy needs `active_sa: true`;
  Windows Server BYOL needs `tenancy: dedicated` (no License Mobility). An incoherent finding is reported as an
  error exception and priced as license-included.
* A02 stays responsible for eligibility. A01 only checks coherence, lists gaps and applies findings to pricing.
* With no input, the BYOL scenario is `not computable` (default). Setting `pricing.byol_without_input =
  "illustrative_upper_bound"` produces a labelled upper bound instead; it is never evidence.

## Cloudamize
* `OLA Accelerator Export` and `Download All Reports (Detailed)` are verified against Cloudamize's public samples.
* Cloudamize sizes CPU by hardware benchmark. For `Download All Reports`, the `Predicted CPU Utilization` on the
  recommended instance is used as measured CPU demand (not Cloudamize's instance choice). For other sources,
  `sizing.cpu_perf_factor` is the lever.
* Cloudamize applies its own "License Mobility through SA" assumption to SQL servers. A01 raises it as an exception
  (`ALL-SA-ASSUMED`): it is not evidence.

## AWS Transform (A01 and AWS Transform as one entity)
**Who does what:** AWS Transform calculates (sizing, EC2/EBS cost, RI pricing, on-premises baseline, business case). A01 prepares
the inputs, steers the job, imports the result, adds what AWS Transform lacks (licensing evidence, end-of-support, Cloudamize-specific
checks, interview-note evidence), merges both warning lists, and does the Ingram deck, traceability, review gate and audit.

**Run it:** `python -m cva transform-run RUN [--enrich]` (needs `pip install -r requirements.txt`, AWS credentials in the environment
(`AWS_PROFILE`, `AWS_REGION`) and `uvx`; override the server command with `CVA_TRANSFORM_MCP_CMD`). Or import a finished assessment:
`python -m cva import-results RUN <folder | Initial_Assessment_*.zip | aggregated_output.json>`.

**What the driver does (and never does):**
* Calls only: `get_status`, `create_workspace`, `create_job`, `load_instructions`, `upload_artifact`, `send_message`, `list_resources`,
  `get_resource`, `get_job_status`. `complete_task` (its default action is APPROVE), `configure`, every delete, collaborator, connector
  and `control_job` tool are blocked in `cva/transform/mcp_client.py`.
* Sends the FULL settings every time (a re-run does not keep earlier settings) and compares the service's proposed scenario with
  `config/transform.toml [expected]` before confirming. A mismatch stops the run for a person (`--accept-proposal` is the person's override).
* Refuses Cloudamize files that AWS Transform does not list as inputs (documented: RVTools, MPA, Migration Evaluator) unless told otherwise.
* Optional enrichment pass: fetches the service's own `server_enrichment.csv`, fills only columns we hold real measurements for
  (blanking the template's 25% / 60% defaults), uploads it, applies it, re-runs, checks and confirms again.

**Known service behaviours the design handles:** a re-run drops settings and adds others (sustainability, network cost); the service
once said a configuration "matches exactly" when it did not; defaults in the template hide data-quality warnings; the `id` column
(`server-<hex>`) is the key, not the VM name; BYOL for SQL needs database data, which an RVTools file does not have.
