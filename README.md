# A01 CVA Output Processing Agent (Project Elevate)

Turns Cloudamize discovery data into the OLA / CVA package: cost across four pricing scenarios, rightsizing,
licensing, end-of-life findings, a data-quality exceptions list, and a workbook that traces every slide figure to
workload-level data.  A person reviews and releases; nothing leaves as final before the review gate is clear.

**Read `docs/STATUS.md` first.**  A01 and AWS Transform work as one entity: AWS Transform calculates, A01 prepares the inputs, runs and
checks the job, imports the result and does the Ingram deck, licensing, traceability and review.  Without an imported assessment the
figures are A01's own OFFLINE estimate (placeholder prices) and say so.  The real-model path and the live AWS Transform driver have not
been run against real services; with no model configured the agent runs as an offline stub.

## Quick start
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m cva demo --full          # narrated end-to-end run on SYNTHETIC data (needs LibreOffice for the PDF copy)
python -m cva ui                   # the demo screen: http://127.0.0.1:8765
python -m pytest                   # 313 tests
```
`python scripts/make_samples.py` regenerates the synthetic estates, licensing inputs and interview notes.

## Using your own data
```bash
python -m cva run export.xlsx --licensing licensing.xlsx --notes notes_folder --collection-days 14
python -m cva agent RUN-ID                     # the agent decides which tasks the run needs and proposes
python -m cva proposals RUN-ID
python -m cva decide RUN-ID P0001 accept --by "Your Name"
python -m cva ack RUN-ID LI-GAP --by "Your Name" --note "why this is acceptable"
python -m cva confirm RUN-ID --by "Your Name"; python -m cva finalise RUN-ID --by "Your Name"
python -m cva export RUN-ID [--template ingram_template.pptx]
```
Accepted file types: Cloudamize OLA Accelerator export, Cloudamize "Download All Reports", Cloudamize MPA export
(layout unverified), RVTools (xlsx or zip of CSVs).  Everything is recognised automatically.

## AWS Transform
```bash
python -m cva import-results RUN-ID path/to/Initial_Assessment_xxx.zip      # a finished assessment (or its folder / aggregated_output.json)
python -m cva transform-run RUN-ID [--enrich]                              # A01 runs the job itself (AWS credentials + uvx needed)
```
See `docs/INTEGRATION.md` for what the driver does and never does.

## The real model
```bash
pip install boto3
export CVA_AWS_PROFILE=your-profile CVA_AWS_REGION=us-east-1      # CVA_MODEL_ID overrides config/settings.toml
python -m cva agent RUN-ID --model bedrock
```
Read the transcripts (UI: Agent > What the agent looked at) before trusting the result.

## Layout
`cva/ingest` readers and licensing intake, `cva/engines` calculation rules, `cva/agent` tools and loop,
`cva/review` run store, audit chain, gate and KPIs, `cva/outputs` deck, workbook, PDF, `cva/transform` AWS Transform
adapters, `cva/ui` local console, `config/` every lever (mappings, rules, prices, scenarios, prompts), `samples/`,
`tests/`, `docs/` (DESIGN, INTEGRATION, TEMPLATE, STATUS).

Data handling: runs are folders under `./runs` (inputs, decisions, audit log, outputs).  Nothing is sent anywhere
unless you choose the Bedrock model, which sends tool results (workload names, figures, note excerpts) to that model.
`python -m cva purge RUN-ID --confirm` deletes a run.

## Starting the UI
```bash
python -m cva ui                 # your own files only: nothing is pre-loaded; upload to begin
python -m cva ui --demo-samples  # demos and tests only: also offers the bundled synthetic and public sample files, each labelled DEMO
```
Runs are stored in `./runs`. To start with a completely clean slate, delete that folder or point at another one: `python -m cva --runs ~/cva-runs ui`.

## Hosted demo (CloudFront + one EC2 instance)
See `deploy/README.md`. `./deploy/deploy.sh` builds, deploys and prints the URL and the password.
