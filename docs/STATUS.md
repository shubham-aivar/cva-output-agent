# Status: what is verified, what is not  (version 0.12.1)

## 0.12.1: the deck is filled from AWS Transform's own outputs; the database sheets are read
* Why the two decks differed (seen on a real 231-server MPA run): A01 filled the scope slides from the inventory, which had no OS, environment, CPU or storage columns, so it printed 0 Windows, 0 Linux, 0 zombies.  AWS's business case deck had 188 Windows, 38 Linux, 15 SQL, 89.41 TiB.  The cost totals were always the same data (A01's slides show infrastructure only, $598K; AWS's business case adds support, $643,857).
* Now AWS Transform comes FIRST, the inventory second: scope counts, SQL servers and block storage from the business case deck (`transform/business_case.py`, read by exact labels only); per-server OS, average CPU (zombie counts and the lowest-CPU table, with AWS's own cost for the saving) and applications from the analysis workbook; right-sized % = servers AWS gave an instance / servers in scope.  A figure no output gives stays blank, never 0.  Speaker notes say which source filled the scope.
* The analysis workbook's database sheets are read (layout verified on the real file): `RDS Annual Costs`, `RDS Server Mappings`, `RDS Overview`, `Object-S3`.  AWS's compute category = EC2 per-server rows + [(RDS compute + licence + backup + SA + storage) x (1 - MAP credit rate) - storage]; storage = EBS + S3 + RDS storage; the credit rate is AWS's own (overview).  On the real file every reconciliation row ties within $0.50, so the "does not add up" warning disappears for this kind of run.  Volumes named "<server>-storage" (MPA layout) are now matched to their servers (207 of 208 were orphaned).
* Slide 9: current on-prem SQL CPUs = the figure AWS states ("across 84 on-prem CPUs"; it is not a sum of provisioned vCPUs, which gives 74); right-sized = the vCPUs AWS licenses on its recommended RDS instances, by edition.  Row 3 (Optimize CPU), slide 10, Datacenters and Windows versions stay blank: no AWS output has them.  The environment doughnut needs an Environment column: the real workbook's is empty, so it comes from the inventory.
* Not verified: a run on your real MPA inventory end to end (this was checked with the real workbook and business case, a sample inventory and the totals from your screenshots).

## 0.12.0: Ingram's own OLA deck, a job check that clears stale approvals, a conversation view, tidier AWS tab
* Deck: every export is now made from Ingram's template (`templates/Ingram_OLA_Template.pptx`, 25 slides) filled in place by `cva/outputs/ingram_ola.py`: same slides, order and layouts; only customer figures change; a section with no data stays and is BLANK (no sample number survives: tested).  `template="placeholder"` still gives the old generic design.  Slide 5's two doughnuts (PowerPoint-2016 "chartEx" with a static picture fallback that other viewers and the PDF would show) are replaced by native doughnuts; slide 7's static total labels (fixed text boxes) are replaced by totals in the bar labels.
* Choices to confirm with Ingram: the 3-year and 1-year columns are labelled "NURI - License Inc." (AWS Transform prices them licence-included, so "BYOL SQL" would be false); "On Demand BYOL" is blank unless a licensing input gives A01's own estimate; SQL core tables (slides 9-10), Datacenters and Total Applications are blank (no such data in the inputs); release status is in the speaker notes; slide 12 gets a table of the lowest-CPU servers under its "Servers" heading.
* Job check: `Service.transform_sync` (read-only look at the job) fetches and imports the results itself when the outputs exist and clears the paused question, so an approval screen no longer outlives the job (seen live: "outputs ready" in the log while "Show me exactly what it will run" was still on screen).  The AWS tab calls it when it opens with a paused question.
* Conversation: the AWS tab shows the exchange (You / A01 / AWS Transform) built from the audit trail.
* AWS tab: Scope grid no longer squeezes its labels; cross-check rows with no data are hidden; when AWS priced services its per-server sheets do not itemise (RDS SQL Server, S3) the reconciliation says so (amber chip) instead of "does not add up".
* NOT verified live: the job check and conversation against the real service; the deck on a real Cloudamize MPA run; the database sheets of the analysis workbook are still not read (their layout was not available).  Rendered and validated here: slides 4-13 with and without AWS data (PowerPoint validator passes).

## 0.11.3: the per-server section was a wall of text, and the mismatch had no visible cause (231-server Cloudamize MPA run on the hosted demo)
* Seen: AWS Transform's per-server rows covered 209 of 224 in-scope servers; compute differed by about -$166k (On-Demand) and storage by -$12k, network matched; the page printed one line per orphan volume and a raw "DOES NOT ADD UP" table.
* Likely cause (NOT proven; the workbook was not available here): the 15 in-scope servers without a row are database servers (names such as TestRDS21, pwsql05, pwrdsh01) that AWS models on sheets with another layout, which A01 skipped without saying so.
* Now: every sheet that A01 does not read is named with its row count and column headers (the Read Me and Glossary sheets excluded); orphan volumes are ONE line (count, servers, annual cost); the page shows a plain notice (headline figures are AWS Transform's and unaffected; the per-server table understates), a tidy table with a result chip and a signed dollar difference, the list of servers without a row folded away, and the workbook's sheets in a folded section.
* `scripts/inspect_results.py` prints only the SHAPE of a result (sheet names, row counts, column headers; the cost split by assessment), so the missing layout can be sent without customer rows.
* Not done, on purpose: A01 does not yet read the database sheets or split AWS's compute total into EC2 and database parts; that needs the real sheet layout (see above). Tests: 2 new, 1 updated; 294 pass; ruff clean.

## 0.11.2: the service asks its own question before it lists a configuration (seen live on the hosted demo)
* Seen: A01 asked the service to list its configuration; it answered "I cannot provide the complete configuration list yet ... your input on the data quality gaps first" (19 SQL Server databases with unrecognised environment and role; options: download an enrichment file, describe the classification, proceed as-is).  A01 judged that a MISMATCH (all 8 settings "missing") and offered "Approve and run anyway", whose text says "exactly the configuration you just listed": false, because nothing was listed.  An earlier reply ("your data is being processed in the background") was judged the same way.
* Now: a reply in which NONE of the expected settings appears is "not a listing" (`NeedsHuman.question`), not a mismatch.  The screen says so, shows the service's words, offers "Proceed with the current inventory" (sends `messages.proceed_as_is`: proceed, then present the complete configuration and run nothing until confirmed), Check again, and the suggestion box for any other answer.  After that answer A01 fetches the REAL listing, checks it and asks for approval again: the run starts only on that second, ordinary approval.  A real mismatch (some settings listed, some wrong) is unchanged.
* Tests: 4 new (two fail without the change, with the exact message from the screenshot); full suite (292) and ruff clean.
* Not verified live: what the service does after "proceed as-is" (it said it would continue through its checkpoints and present the configuration); if it asks another question, the same screen appears again.

## 0.11.1: review of the hosted-demo build
* Fixed: the sign-in lockout counted the FIRST `X-Forwarded-For` entry, which a visitor can forge to dodge the lock; CloudFront appends the real address, so the LAST entry is used (regression test added; it failed before the fix).
* Fixed: `deploy.sh` sent SSM commands in a shorthand that breaks on commas (now JSON). `requirements.txt` comment corrected: the AWS Transform server 0.2.3 needs mcp 2.x (checked in its wheel), hence the separate venv.
* Checked against the real wheel: console script `awslabs.aws-transform-mcp-server` exists, Python >= 3.10. Its SigV4 signing name is `elasticgumbyfrontendservice`; the wheel does not name the IAM actions, so `transform:*` for the instance role stays UNVERIFIED (pre-flight shows it; fallback `EXTRA_POLICY_ARN`).

## 0.11.0: hosted demo: CloudFront in front of one EC2 instance, username and password sign-in (no MFA)
* **Sign-in** (`cva/ui/auth.py`, on only when `CVA_AUTH_FILE` is set): one user, PBKDF2-SHA256 password hash, signed HttpOnly session cookie (8 h, Secure when hosted), failed attempts slowed
  and locked per client address (5 in 5 min -> 5 min), POSTs must be JSON (CSRF), a secret header (`X-Origin-Verify`) that only CloudFront sends, `/health` open, sign-out button. `--host 0.0.0.0` REFUSES to start without sign-in.
  The signed-in name pre-fills the reviewer field. Local use (127.0.0.1, no file) is unchanged.
* **Slow actions** (export, fetch results, pre-flight, artifacts): after 8 s the answer becomes 200 + one space every 10 s + the JSON, so CloudFront's 60 s origin read timeout never cuts them (errors arrive as `{"error": ...}`, which the page already shows).
* **Pre-flight** no longer requires AWS_PROFILE (the instance role is used); `--runs` also read from `CVA_RUNS_DIR`, `--host` from `CVA_HOST`.
* **`deploy/`**: `cloudformation.yaml` (VPC, one public subnet, security group open only to CloudFront's prefix list, instance role, Ubuntu 24.04 t3.medium, CloudFront), `bootstrap.sh` (runs on the instance: LibreOffice, two Python
  environments because A01 needs mcp 1.x and the AWS Transform server 0.2.3 needs mcp 2.x, systemd service), `deploy.sh` (deploy | update | status | logs | destroy).
* Tests: 288 pass (0.11.0) (10 new in `tests/test_auth.py`); ruff, cfn-lint and shellcheck clean.
* **Not verified (no access to the account from here):** the stack was never deployed; whether `transform:*` is enough for the instance role to use AWS Transform (the pre-flight shows AccessDenied if not: redeploy with
  `EXTRA_POLICY_ARN`); jobs created by an instance role may not show in your own AWS Transform console login; the page was not driven in a real browser (none available here).
* SOW 13 note: this runs in Aivar's account with a single shared login: demo data only, no Ingram data.

## 0.10.6: A01 must not speak over a question the service asked the person
* Seen live (AWS Transform chat, run-0003): the person replied; the service answered and ASKED "anything else you'd like me to adjust, or are you ready to proceed?"; A01 immediately posted its
  own read-back request ("Before I confirm anything, list the exact scenario configuration ...") before the person had answered.  After EVERY reply the person sent, A01 took the next turn itself.
* The turn-taking rule now: after A01 relays a person's message and shows the service's answer, it sends NOTHING until the person acts.  The panel shows the service's own words with
  "A01 has sent nothing more to it", a reply box, and "Show me exactly what it will run": only that click makes A01 request the configuration listing (one message), check it, and put the
  approval to the person.  There is no Approve button until the person has seen the listing.  The click is audited (`transform_listing_requested`).
* Second case closed: if the job waits and the service's latest message is a QUESTION that A01 does not recognise as the scenario decision, A01 used to send a "status" nudge over it; now it
  stops at once, shows the question and sends nothing.  (Both cases were reproduced by tests that failed first.)
* What A01 still sends on its own, and only these: the start-up message with the settings; a read-only "status" when the service itself says "send me a message to see an update"; the verification
  read-back at the FIRST scenario decision (and again if the service re-asks the scenario after the go-ahead), before A01 asks the person; and the go-ahead after the person approves.

## 0.10.5: it worked at 17:24:42 and stalled from ~17:25: not the path, not a crashed server
* The log (v0.10.2): `job AWAITING_HUMAN_INPUT ... Generated Outputs/: 1 file(s), 1 folder(s)` at 17:24:42, then `get_job_status` timed out at 17:26:32, then every listing and the messages.  The SAME path
  listing answered 20 s earlier and the status and messages calls (no path at all) failed too, so it is NOT the path.  The server is not "dead" either: read from its source, each call runs in a
  worker thread, builds a FRESH botocore session and resolves the profile's credentials there (the "Loading cached SSO token" line on every call); one stalled network step has a 60 s timeout with
  3 retries (over 4 minutes per call) while A01 gives up at 45 to 120 s.  Onset ~45 minutes after the last `aws sso login` (if that was ~16:40) fits an SSO login entering its refresh window.  UNPROVEN.
* Now: A01 checks the AWS login with the AWS CLI (`aws sts get-caller-identity`, a short separate process with a timeout; nothing secret is read) when a wait starts, every 5 minutes while waiting,
  before starting a server and before a fresh one: it refreshes an SSO login near its end into the shared cache, and an EXPIRED login stops the run at once with the exact command
  (`aws sso login --profile ...`) instead of every call hanging.  The pre-flight shows "Your AWS login works" with the ROLE and account (so a non-admin role is seen at once).
* Fixed on the way: "Check again" after the go-ahead went back to the decision step instead of just waiting for the outputs (the page now passes the stage); a connection or login stop raised while the
  service was asked for its configuration was disguised as a listing problem.  The diagnostics file now carries the server's own log tail and the login check.
* Option not built (it would make A01 hold short-lived credentials in memory): export the profile's temporary credentials with the AWS CLI into the server's environment, so no call needs the SSO
  service at all; A01 would renew them and restart the server before they expire.

## 0.10.4: the files existed (17:29) but every call timed out for 7+ minutes: a stuck server session was never replaced
* Seen live (twice): ListResources (files), the messages and get_job_status ALL timed out, run after run, while the web application showed aggregated_output.json, the zip and
  error_file.csv already created.  Not a listing problem: the whole server session was unresponsive and A01 kept asking it.
* Tested with the real MCP library (stub servers): (1) A01's own timeout handling is NOT the cause: after a client-side timeout the next call answers at once; (2) if the SERVER blocks its own event
  loop (a synchronous slow call inside it), EVERY call times out until it recovers: exactly the log; (3) closing a stuck server takes about 2 s and kills the process; a fresh one answers immediately.
* Now: after `restart_after_timeouts` (2) calls in a row time out, A01 closes the stuck session, starts a fresh server (same environment and retries as before), loads the job's instructions
  again and carries on; at most `max_restarts` (6) times in a run, then a person is told with the reason and the "Fetch the results now" way out.  A single slow call restarts nothing.
  The same self-healing applies to the artifacts panel and "Fetch the results now".  The page's log says when it happens.
* The frequent output check no longer lists the connector store (`transform-output/<job>/`: empty in every real run); the whole-job crawl (every 4th check, the panel, Fetch) still does.
* Not verified: WHY the server session stuck (the server's own log, ~/.aws-transform-mcp/server.log, around that time would show).  A01 now recovers and records each restart.

## 0.10.3: NOT_CONFIGURED "every single time"
* Seen: the panel showed "Loading cached SSO token" three times (A01's three automatic retries, 40 s apart): the SSO login IS found each time, and the failure comes after it.  Read from the
  server's source: at start-up it loads the credentials, then calls ListWorkspaces in every region with a 5-second limit, and says NOT_CONFIGURED only if NO region answers.  The decisive lines
  ("Region discovery failed for <region>: <error>"; an empty error is a timeout) go to the server's OWN log, `~/.aws-transform-mcp/server.log`.  A01 starts the server with quiet logging
  and only read that quiet stderr, so it showed the wrong lines.
* **A01 started the server with a stripped environment.**  The MCP library gives a server only HOME, PATH, USER, SHELL, TERM and LOGNAME plus what the caller adds; A01 added AWS_PROFILE,
  AWS_REGION and AWS_DEFAULT_REGION.  Proxy settings (HTTPS_PROXY, NO_PROXY), a company certificate bundle (SSL_CERT_FILE, AWS_CA_BUNDLE) and every other variable of the person's terminal were
  dropped, while `aws sts get-caller-identity` (and Claude Code, which passes the full environment) had them.  A server that cannot reach AWS Transform in 5 seconds logs the empty "Region
  discovery failed" and answers NOT_CONFIGURED.
* Now: the server gets the person's whole environment (minus the variables of A01's own Python environment), at all four places A01 starts it; A01 reads the server's own log (the latest start-up
  only) and says in plain words what is most likely wrong: no usable AWS login, every region timed out (network, VPN, proxy), a permission, a certificate problem, or several regions needing a choice.
  The same text is in the pre-flight and on the "not connected" screen.
* Not verified: which of these is the cause on the affected machine; the screen now says.

## 0.10.2: run-0003: A01 waited for outputs before the service had asked its question
* Seen live: the web application showed "Awaiting user input" and the question "Ready to proceed with this scenario?" (3:35 PM) with NO reply from A01, while A01's log said
  "waiting for the assessment outputs ... job UNKNOWN" (started 15:34:25, before the question existed).  Three faults chained:
  1. **A01 could not read the job's state** (my v0.9 bug).  The real job record keeps it in `statusDetails.status` (+ `failureReason`); it has no top-level `status`.  The server's own
     `_pollingGuidance.jobStatus` reads that missing field, so it is ALWAYS "UNKNOWN" (and isTerminal always false).  A01 trusted it.  Now it reads `statusDetails.status`, never trusts the
     guidance, never reports the string UNKNOWN, and decides terminal states itself.
  2. **A01 took an early message for the decision.**  The service's own status report said "you can proceed with defaults ... how would you like to proceed?" while the job was still
     EXECUTING; A01 matched the words, sent its go-ahead (or asked for approval) too early, and the service, not waiting yet, ignored it.  Now a message counts as the decision only when the job
     is AWAITING_HUMAN_INPUT (if the state cannot be read, the old message-only rule applies).
  3. **No recovery.**  Now, if the job still waits 45 s after the go-ahead and its latest message is a scenario question again, A01 goes back to the decision step (finds the question, has the
     service list its configuration, checks it) and either asks the person again (approval mode: a NEW request, never a silent wait) or sends the go-ahead again (automatic mode); at most 3 times,
     then it stops with the reason.  A "send me a message" notice after the go-ahead gets the read-only status request.
* The scripted service now has the REAL job record (including the server's always-UNKNOWN guidance), a status report that sounds like a decision while the job prepares, a go-ahead that is
  ignored, and a job that fails AFTER the go-ahead; the failure is reproduced by tests that failed before these fixes.
* A failed job now says why in AWS's own words (`failureReason`).  The "Ask me to approve" checkbox no longer resets on a page refresh.
* Not verified live: the exact text of the service's early report in your run (A01 now does not depend on it: it depends on the job state).

## 0.10.1: NOT_CONFIGURED again, and a screen that sent people to Claude Code
* The "not connected" panel told people to sign in through Claude Code. That is the LAST resort, not the usual fix: the AWS Transform MCP server signs in with the AWS credentials of the terminal that
  started A01 (or with a saved sign-in). Read from its source: at start-up it checks those credentials against AWS Transform with a 5-second limit per region; if that check fails by chance (a slow
  network; the empty "Region discovery failed for us-east-1:" log line) the WHOLE session answers NOT_CONFIGURED although the login is fine.
* Now: a session that starts not connected is closed and started again (3 times; 2 for the pre-flight); the screen puts AWS credentials first (`aws sts get-caller-identity` must show the admin role;
  `aws sso login --profile ...`; export AWS_PROFILE and AWS_REGION; restart A01 from that terminal) and a Claude Code sign-in last; and it shows the lines the MCP server itself logged at start-up.
* Contains 0.10.0: per-server results from the analysis workbook (see below).

## 0.10.0: AWS Transform's per-server results (the analysis workbook, layout now seen and verified)
* Read from a REAL workbook (RVTools sample, October 2026, 55 servers): `analysis_<timestamp>.xlsx`, inside the `<Scenario>_<timestamp>.zip`. One sheet per pricing model (`Shared Tenancy - On-Demand`,
  `- 1yr NU`, `- 3yr NU`), ONE ROW PER IN-SCOPE SERVER, the same 28 columns on each: Server Id (`server-<hex>`), DNS name, on-prem cores and RAM, the utilization AWS used, `EC2 Instance Recommended`, its
  cores and RAM, region and the annual cost as license + EC2 + network. The recommended instance can differ between pricing models (7 of 55 servers did), so it is kept PER MODEL.
  `Block-EBS-Cost-Optimized` has one row per volume; `Assessment Issues` and `Excluded Resources` give the warnings and the exclusions. Sheets are found by their column headers, not by position.
* **Reconciled against AWS's own totals**: per-server license + EC2 = the compute total, network = the network total, volumes = the storage total (all within $0.50 on the real data). A mismatch is
  a TRW-RECON warning; an unreadable workbook is a TRW-ANALYSIS warning and never loses the totals.
* **Where it shows**: Workloads (AWS instance, vCPU/RAM, direction, confidence, annual cost On-Demand and 3-year; a row opens every pricing model, the utilization AWS used and AWS's warnings); the AWS tab
  (servers read, matched, the reconciliation); the Overview rightsizing column; the ledger (`sizing.*`, rule AT-SIZING; A01's own estimate, if run, moves to `a01.sizing.*`); the deck's rightsizing slide;
  the supporting workbook (AWS columns); the narrative.
* **The deck now exports without A01's offline estimate** when the per-server results are imported. Direction uses A01's own rule (upsize only when the workload needs more than it has, by AWS's utilization;
  downsize when the instance has under 70% of the cores or RAM allocated today). Confidence is low where AWS itself says it used default utilization. AWS's support charge is not allocated to servers.
* Matching to the inventory is by the first label of the DNS name (web-p01.corp.example.local -> WEB-P01). Servers AWS excluded (powered off) or that cannot be matched are listed, never guessed.
* Also fixed: the server detail panel printed the word "null" when a server had no sizing notes.
* Not verified: other tenancies or pricing models (Mixed/Dedicated sheets, all-upfront) are ignored and named in the notes; Cloudamize MPA input has not been seen.

## 0.9.0: the agent loop after the go-ahead: approvals, slow calls, and a run that must never just "fail"
* Seen live (v0.8.1): after the go-ahead the job went AWAITING_HUMAN_INPUT (a pending approval in the web app), calls got slow, the messages listing hit my 45 s timeout, that exception was
  not caught and the run was reported as failed, although AWS Transform delivered the reports at 12:03 AM.
* Read from the server's source: get_job_status(detailed=true) (A01 already used it; the default would post a status question into the job's chat) returns the job status (CREATED ...
  EXECUTING, AWAITING_HUMAN_INPUT, COMPLETED, FAILED, STOPPED) AND the human tasks waiting for a person (title, description, severity STANDARD or CRITICAL, category TOOL_APPROVAL or REGULAR,
  status). A01 ignored the tasks.
* Now each check reads that snapshot: results present -> done; job FAILED or STOPPED -> stop and say so; a BLOCKING task waiting on two checks in a row -> stop at once and show it (title, description,
  tags); AWAITING with no open task -> after 45 s show the service's last message and a reply box; COMPLETED or "reports ready" but no numbers file for 90 s -> say what was seen.
* **Approvals from A01**: for a non-CRITICAL tool approval you click Approve or Reject; A01 re-fetches the task, then relays it with complete_task (only call site: the driver's answer_task; no content is invented);
  a CRITICAL task or one that needs a form is left to the web application with a button to say you did it there. Every decision is audited with your name. A01 never answers a task by itself.
* Nothing in a check ends the run: a status or messages call that is slow or fails is counted and logged ("the job status did not answer: still waiting"); the status call has 90 s, the messages listing 120 s,
  a file listing 45 s; the snapshot's messages are reused instead of listing them again; if the listing is refused on 3 checks (or times out on 6) the run stops with the reason.
  Any other unexpected exception becomes a recoverable pause with "Fetch the results now" instead of "failed".
* Not verified live: the cause of the slowdown at 23:59 (A01 now tolerates it and shows it), and the approval relay against your tenant (first live use).

## 0.8.1: one hung listing, 4 minutes of silence, then a bare "list_failed"
* Seen live (v0.8.0): the listing now worked (root 2 folders, Generated Outputs 1 file + 1 folder, 5 files), then NOTHING for ~4 minutes and a bare `list_failed`. The MCP client waited up to
  240 s for one call, which matches the gap exactly: one listing call hung until it timed out, and A01 swallowed the error and showed no reason.
* Fixes: a listing is cut off after `list_timeout_seconds` (45 s) and one crawl stops after `listing_budget_seconds` (75 s); a failed or timed-out listing is named in the page's log
  ("listing of 'X' failed: TimeoutError ...") and counted; THREE failed checks in a row stop the run with the reason instead of waiting 40 minutes; a sign-in that expires mid-run
  (INVALID_SESSION, an expired token) is raised as a connection problem instead of being swallowed; "Stop waiting" now works in the middle of a listing.
* Removed my own inventions, which were pointless and a likely cause of the hang: the server matches a pathPrefix EXACTLY, so A01 no longer tries "relative" or "full root" forms, no longer
  searches by name, and no longer passes maxResults.
* Waiting for outputs lists only `Generated Outputs/` (without the ingestion folder) and the connector output: 3 to 4 calls per check instead of a crawl of the whole job (every 4th check
  lists the whole job in case the layout differs). The panel still lists everything.
* The deliverables zip is named after the SCENARIO ("Cost_Optimized_Assessment_<ts>.zip", not only "Initial_Assessment_<ts>.zip"): any zip outside the input and ingestion folders counts, and
  the importer unpacks any zip. Of several files with the same name, the newest is downloaded.
* Not verified against your tenant: why one call hung. The causes A01 could contribute to are removed and bounded; the rest would show in the log with the call's name.

## 0.8.0: A01 could not read the service's file listing (the real cause of "0 files")
* Cause, found by reading the AWS Transform MCP server's own source (v0.2.3) and its API model, not by guessing: a listing is `{artifacts: [{artifactId, fileMetadata: {path},
  sizeInBytes, artifactType}], assets: [{assetKey}], folders: ["<prefix>/"], connectorId}`. A file's NAME is `fileMetadata.path`, and some files arrive as ASSETS. A01 only accepted a name
  beside the id, so it discarded every file the service listed, and then showed "0 files" without the raw answer.
* The listing does NOT descend into folders: each folder string is re-listed VERBATIM as `pathPrefix`; some stages omit `folders` at the root, so the canonical folders
  (`AWSTransform/Workspaces/{ws}/Jobs/{job}/Generated Outputs/`, `.../User Uploads/`, `transform-output/{job}/`) are tried directly. The scenario folder is named after the scenario
  ("Initial Assessment", "Cost Optimized Assessment"...): none is assumed.
* Downloads follow the server's rules: `get_resource` needs workspaceId, jobId, artifactId (assets: also the listing's connectorId and the assetKey); a savePath ending in "/" is a directory and
  fileName is appended; writes are confined to AWS_TRANSFORM_MCP_WRITE_DIR.
* The wait gives the warnings file and the zip up to 60 s after the numbers file appears. The newest job (set on every create/resume) is used. A refused listing shows the service's own error
  and an empty one shows the raw answer. artifacts are not paginated by A01 (the server does it). The searchTerm fallback was removed (the server ignores it for artifacts).
* Tests use a scripted service with the REAL response shape, exact-prefix semantics, directory-style downloads, assets and differently named scenario folders, and run start, approve and the
  automatic fetch and import through the page's API in fresh sessions.
* **Not verified against your real tenant:** that exact run. The schema is verified from the service model and the server's source.

## 0.7.1: "NOT_CONFIGURED: Not connected to AWS Transform"
* Seen live: a new run stopped at create_workspace with the service's answer `NOT_CONFIGURED: Not connected to AWS Transform`. The MCP server's sign-in (SSO token or session cookie, saved in
  `~/.aws-transform-mcp/config.json` and auto-loaded) was missing or had expired. Earlier runs worked while it was valid. A01 never signs in for you (it never handles the cookie or token).
* Before: A01 ignored the answer and reported "Could not read the workspace id", and the pre-flight did not recognise NOT_CONFIGURED (it looked for the words "not configured").
* Now: any answer carrying NOT_CONFIGURED stops the run at once, before anything is created, with a plain panel (the reason once, the steps, and "Check the connection again" showing the result in place);
  the pre-flight fails "Connected to AWS Transform" and "A read-only listing works" with the same cause.
* To fix it: sign in again once with an assistant that has the AWS Transform MCP server (for example Claude Code: "Configure AWS Transform with SSO", or with cookie auth), then run
  "Check the connection first" until every line says pass.

## 0.7.0: AWS Transform artifacts: fetch the results by itself, and browse and download everything
* **Outputs tab, "AWS Transform artifacts"**: lists every file the job produced (name, folder, size), read live. Tick files and **Download selected** (a zip that keeps the folders),
  **Download all (one zip)**, or the per-file **Download**. Files already fetched are marked "in A01". The list and the ticks survive the page's automatic refreshes.
  If nothing can be listed it shows what each listing returned.
* **Fetch the results now and fill the other tabs** (AWS Transform tab and Outputs tab): fetches aggregated_output.json, error_file.csv and the Initial_Assessment zip straight from the job and
  imports them: the same step the run does by itself, on demand, no manual download.
* Discovery is wider: every page of a listing is read (nextToken); each folder is opened as written, relative to the job and as a full path; the files named in the service's messages are added;
  and if the numbers file is still missing the listing is searched by file name (searchTerm).
* The job is remembered the moment it exists (and is recovered from the saved trace for runs made by an older version), so the panel works while a run is paused or stuck.
* Downloads are looked up by artifact id, never by path; names are sanitised so nothing can be written outside the run.
* **Not verified against the real service:** the listing shapes (the whole point of this release is that the panel and the log now show them). Manual import still works.

## 0.6.2: 13 minutes of "0 new file(s)" after the reports were ready
* Seen live: the web application showed the reports at 7:44 PM; A01 reported "0 new file(s) so far" until at least 19:52:40. A01's listing found NOTHING, not even the zip.
  The cause is still unknown (the real shape of the listing has not been seen), so this version makes it explain itself:
  - the wait log now says what EACH listing returned ("'(root)': 0 file(s), 2 folder(s) | 'Generated Outputs/': refused: ...");
  - more key spellings for file and folder entries are read (artifactID/artifactName/assetId/filename..., isFolder/isDirectory flags), and a file-like value is used as the name when no name key exists;
  - the service's own "Reports ready ..." message counts: if it was said and no numbers file is visible for 90 s, A01 STOPS and shows what it saw, with the fallback;
  - the trace keeps each call's arguments (the prefixes tried; never message text), and Download diagnostics now includes `listing_samples.jsonl`: the first and last raw answer of every distinct listing.
* Not verified live: the automatic discovery itself. The manual import (Choose File: aggregated_output.json and error_file.csv) works today.

## 0.6.1: reports ready in AWS Transform, but A01 waits
* Seen live: the web application said "Reports ready for scenario 'Initial Assessment'" (the deliverables list shows only the zip and error_file.csv with their artifact ids) while A01 had found no
  files. The numbers file, aggregated_output.json, sits in a NESTED folder (Initial Assessment / assessment outputs) that was never opened against the real service.
* A01 now opens every folder as the service wrote it, relative to the job, AND as a full path from the job's root, up to 60 folders deep, and also takes the files (with ids) the service names in
  its own messages.
* If the reports are visible but aggregated_output.json is not for 90 s, A01 STOPS and says so, listing what it can see and the exact fallback, instead of waiting 40 minutes.
* **Import several files at once** (AWS Transform tab: Choose File / Import file(s)): aggregated_output.json (required, the numbers), error_file.csv (the warnings), the zip.
* Not verified live: whether the nested folder can now be opened automatically. The fallback is the manual import above.

## 0.6.0: readiness: a pre-flight, a diagnostics file, and a whole-journey test
* **Check the connection first** (AWS Transform tab) / `python -m cva transform-preflight`: about 10 seconds, changes nothing in AWS: profile and region set, server command installed, the MCP server
  starts, the tools A01 needs exist, connected to AWS Transform, a read-only listing works. Run it before an assessment.
* **Download diagnostics**: one zip with the tail of the raw conversation with the service, the server's log, this run's AWS Transform events, the paused state, versions and the transform settings.
  It contains the job's messages: share it only with people who may see them.
* A test now reproduces EVERY behaviour seen live at once (answers wrapped as JSON strings; newest-first listings with no timestamps or readable senders; a notice that waits for a nudge; the job's
  instructions needed afresh in every session; folders-only artifact listings accepting only relative prefixes; the suggestion loop; approval relayed from A01) and drives start, suggestion and
  approval through the page's API, each in a NEW session. No message was refused or lost.
* A01 also recognises its own standard messages across sessions even if the sender cannot be read.
* **Still not verified against the real service** (first live use of each): the go-ahead delivery after approval, output discovery and download, the job-state and sender/timestamp field names, and
  how the service answers free-text suggestions. Each is covered by a fallback or a diagnostic (manual import of the zip; Download diagnostics).

## 0.5.1: a suggestion that never reached the AWS chat
* Seen live: after an approval request, a suggestion sent from A01 never appeared in the AWS Transform chat and no answer came (0 new messages for 160 s). Cause: the AWS Transform
  server refuses job tools with INSTRUCTIONS_REQUIRED until `load_instructions` was called for that job IN THE CURRENT SESSION. A01 called it only when it STARTED a job; every approval or
  suggestion runs in a NEW session and skipped it, so the service refused the message and A01 did not treat the refusal as a failure. (The approval button would have failed the same way.)
* Now: a resumed run loads the instructions first; any INSTRUCTIONS_REQUIRED answer triggers load-and-retry once; a message the service refuses (`success: false` or an error) stops the run with the
  service's own answer instead of waiting; the relay-a-message path loads them too.
* Normal: the service builds its "Initial Assessment" plan step while it waits for you; it does not run until the go-ahead.
* **Not verified against the real service:** this explanation is inferred from the tool list ("Other tools return INSTRUCTIONS_REQUIRED if you skip it for a job") and the symptoms; the raw answer
  is in `transform_trace.jsonl` (search it for INSTRUCTIONS_REQUIRED).

## 0.5.0: the loop: approve, or suggest, until AWS Transform runs
* Every pause now offers both choices in A01: **Approve and run**, or a **suggestion box** ("Use 40% CPU and 70% memory", or any question). A01 sends your text to AWS Transform
  UNCHANGED, waits for its answer, asks it to list its configuration again, checks that, and shows the result as the **next approval request** with the service's answer, its
  configuration (including your new figures) and the exact go-ahead A01 will send. Round and round until you approve; then A01 waits for the outputs, imports them and shows the results.
* After a suggestion the go-ahead says "the values we agreed", not "defaults". A suggestion that changes a requested setting (pricing, region, tenancy...) is flagged as a difference
  from the original request, and you decide. A reply to a question asked AFTER the go-ahead is relayed and A01 goes on waiting for the outputs (it does not ask again).
* Every relayed suggestion and every approval is in the audit log with your name.
* **Not verified against the real service:** how it answers free-text suggestions (its replies vary run to run). Approvals of AWS-side TASKS (a "tool approval" card) are never relayed: A01 does not
  call complete_task; approve those in the AWS Transform web application.

## 0.4.0: approve in A01; A01 relays it; nobody types in the AWS Transform chat
* Cause of the last stall: A01's panel had a button "I confirmed it in the web application" which SENDS NOTHING (it assumes you typed the go-ahead in the AWS chat). Clicking it
  looked like approving, and the job kept waiting. There was no plain Approve button.
* Now the AWS Transform tab shows an **approval request**: the configuration the service itself listed, the check against what was requested (matches / differs), and the exact
  message A01 will send. **Approve and run** makes A01 send the go-ahead to AWS Transform, wait for the outputs, import them and show the results on the same page. It is on by default
  ("Ask me to approve before AWS Transform runs"); untick it, or use `--auto-approve`, for fully automatic runs when the configuration matches. A mismatch always asks.
* When something else needs a person, a **Reply to AWS Transform** box relays your text unchanged (audited), so the AWS chat is never needed. The old option moved to **Advanced**.
* The approval applies to the first scenario of a run (the optional enrichment pass proceeds on its own once the configuration matches).
* CLI: `transform-run RUN --resume WS:JOB --approve` approves; `--auto-approve` skips the question.

## 0.3.9: a confirmation that did not register
* Seen live: a person approved in the AWS Transform web application and clicked "I confirmed it in the web application"; the job still said "Awaiting user input" (the service
  wants the go-ahead typed in its chat) and A01 waited silently for outputs. A01 now reads the job state while waiting for outputs and, if it still says awaiting input
  `awaiting_after_confirm_seconds` (180) after the scenario was confirmed, stops and says so, with the exact thing to do. The wait log also shows the job state.

## 0.3.8: finding the outputs when the listing returns only folders
* Seen live: the service answered the read-back (6:11 PM) and A01 recognised yet another wording of the decision point ("Awaiting Your Input ... How would you like to
  proceed?"). After you confirm, A01 deliberately sends nothing and only lists the job's files every 20 s.
* A listing of a job's artifacts returned FOLDERS ("Generated Outputs/", "User Uploads/") with no files and no ids (a manual run). The earlier code ignored such entries and
  could wait forever. Folders are now followed however they are listed (bare paths, typed entries, with or without ids), opening each with `pathPrefix` as given and
  relative to the job's folder, several levels deep.
* **Not verified against the real service:** the exact listing shape. If A01 keeps waiting after the web application shows the outputs, download the Initial_Assessment zip there and
  use AWS Transform tab > Import file.

## 0.3.7: the real cause of the stall: the service's WORDING (seen in the web application)
* The service is an AI assistant and words its messages differently from run to run. A01 looked for the phrase "Ready to proceed?" (used in the first manual
  run); this time the service said "awaiting your confirmation to proceed ... What would you like to do?" and offered "Proceed with defaults". A01 never recognised
  the decision point, and the job waited for it from 5:36 PM.
* A01 no longer depends on exact phrases. It recognises that the service waits for a person from the job state, from the service answering its status nudge
  with something that is not a notice, or from broad waiting phrases; then it **asks the service to list its configuration back** and the configuration check
  judges that reply (regex tokens with readable labels). It answers with the service's own suggested "Proceed with defaults" (the enriched pass says so
  instead). Its own messages are never mistaken for the service's.
* `python -m cva transform-run RUN --resume WORKSPACE:JOB --confirmed-externally` lets A01 take over a job you confirmed in the web application.
* **Not verified against the real service:** the job-state field name, the sender field, the artifact listing, and the format of the read-back reply.

## 0.3.6: seen live: a nudge can be answered with only a status report
* In a live run the four status nudges were spent early and the service answered with "Current Status: Primary Assessment \u2014 Still ..." (it was still
  processing, not waiting). The old logic counted nudges once for the whole wait, so a later "I have an update for you" notice would have gone unanswered.
  Nudges are now counted **per message from the service** (a new message earns a new nudge; at most `max_pokes` per message and `max_total_pokes` overall).
* Only messages that are NEW (by message id) count as the answer, so an old proposal in the listing is never mistaken for a reply.
* The listing's order (newest first, as seen live, or oldest first) is handled with or without timestamps, and re-learned from each listing.
* **Not verified:** the real message records' field names for sender and timestamp (the raw answer is in `transform_trace.jsonl`).

## 0.3.5: the service's answers arrive as JSON inside a string (seen in a live run)
* The live run showed A01 reading the whole reply, `{"success": true, "data": {"messages": [...]}}`, as ONE message, with the job state "unknown". Answers are
  now decoded on arrival (and again in the driver), so each message, id and state is read individually. A message whose text merely looks like JSON is left alone.
* Consequence of the old behaviour: the configuration check could have run on all messages glued together, so an earlier message that echoed every setting
  could make a changed final proposal look right. It now checks only the latest proposal (regression test added).
* The AWS library inside the server logs two lines for every AWS call ("No endpoints ruleset found for service elasticgumbyfrontendservice", "Loading cached SSO
  token"). They are harmless and are now written to `out/transform_job/mcp_server.log`, not your terminal.
* **Not verified:** the sender/role field names in the real message records (the raw answer is in `transform_trace.jsonl`). If the sender is unreadable, every
  message is treated as the service's; that is safe (A01 never sends a confirmation-looking phrase before the guard passes) but I want to confirm it from a trace.

## 0.3.4: the run that stopped at create_workspace
* The driver discarded the service's answer when it could not find a workspace id, and it reused the fixed name `a01-run-0001` for every attempt (a fresh
  runs folder restarts at run-0001, and the earlier run's workspace of that name still exists in AWS). **The exact cause of that stop is not confirmed**
  (the raw answer was not seen); a name clash is the likely one.
* Now: every attempt gets a unique workspace and job name; if an answer carries no readable id the driver looks the workspace or job up by name; and if
  that fails it stops and shows **the service's own answer** on screen, so the cause is visible immediately.

## 0.3.3
* The AWS Transform tab no longer says an offline estimate exists when none was run.

## What changed in 0.3.2: nothing is pre-loaded
* **`python -m cva ui` now offers your own files only.** The upload dialog lists no bundled samples, the AWS Transform tab has no "sample assessment"
  button, and the page opens empty ("No runs yet") and never reopens an earlier run by itself. Everything on every tab is computed from files you upload.
* This is enforced by the server, not just hidden: a request that names a bundled sample is refused unless the UI was started with
  `python -m cva ui --demo-samples`, which is for demos and tests only and labels every sample "DEMO".
* Fixed a stray "null" printed in the upload dialog.

## What changed in 0.3.1: the cause of the first live stall (seen in the AWS Transform web application)
* **AWS Transform does not post its scenario proposal by itself.** After processing the inventory it posts "I have an update for you. Please send a
  message (e.g., 'status') to see the details." and waits for ANY message. A01 only listed messages, so it waited, silently, from 15:54 (five minutes
  into the job) onwards. The driver now sends a read-only `status` message when the service asks for one, or when the job is awaiting a person with no
  recognisable proposal (up to `max_pokes` times, `poke_seconds` apart), then reads the reply. The configuration check still runs on the proposal that
  comes back; the nudge is never a confirmation. This also explains why a status check from Claude Code worked.
* The same screenshots verify that the real service accepted our upload, echoed our settings back correctly (including "Sustainability: Disabled" and
  License Included for SQL Server) and ingested the 57 servers and 101 storage resources in about five minutes.
* **Not yet verified:** the nudge against the real service (it is tested against a scripted service that reproduces the behaviour seen).

## What changed in 0.3.0
* **A01 no longer calculates by default.** Creating a run does intake and cleaning only: estate counts, data grade, licensing coverage and gaps,
  end-of-support findings, exceptions. Sizing, pricing and the scenario bars appear only after an AWS Transform assessment is imported or someone chooses
  "Run A01's offline estimate" (fallback and cross-check, placeholder prices). The Overview opens with "No cost figures yet".
* **Export is blocked until figures exist.** Because the deck builders were deliberately not changed, the rightsizing slide still needs A01's per-server
  estimate, so export requires the offline estimate to have been run. When AWS Transform's per-server results are imported, this requirement can go.
* **The release gate** now has a figures-source check that cannot pass without figures.
* **SQL coverage** reads "not detected" (with the number of database VMs needing evidence) instead of a false 100%.
* **One warning per database VM**, not two. **"Upsize"** now means the workload itself needs more, not that the cheapest catalog instance is larger.
* **The AWS Transform wait is never silent**: the log shows the job state, how many agent messages were read and the latest one; message text is fetched
  with `get_resource` when a listing returns only ids; a job awaiting a person with an unrecognised message stops after 5 minutes and shows that message;
  a failed job stops at once; **"Stop waiting"** ends A01's wait (the AWS job is untouched: A01 cannot stop or delete it); **"I confirmed it in the web
  application: continue"** resumes without A01 reading or sending the confirmation.

## Concurrency fixes found while testing (0.3.0)
* The audit log used a lock per object while the service creates a new object per call, so two threads could fork the hash chain: now one lock per file.
* The run's state file was rewritten in place, so a reader could see half a file: now an atomic replace.
* The status endpoint read the job's state after building its view, so it could show "needs a person" without the pending panel: now the job is read first.

## Verified
* **278 automated tests pass** (run repeatedly: the threaded ones 15 of 15) (clean virtual environment with `mcp` 1.30); lint is clean.
* **Your real AWS Transform result is imported and drives the figures.** `aggregated_output.json` and `error_file.csv` from your job parse into
  the three pricing models (On-Demand $122,339; 1-year RI $89,360; 3-year RI $66,969, recommended), the category split, the on-premises baseline,
  the 55-in-scope / 2-excluded servers, and the 251 warnings (aggregated into 3 exceptions). The deck, workbook and UI show them, labelled
  "AWS Transform"; A01's own estimate ($237,767 On-Demand on the same file) is kept as a labelled cross-check. Aggregate AWS-vs-A01 compute: -54%.
* **The enrichment filler works on your real template** (`server_enrichment.csv`): 57 of 57 servers matched by host name, only the intended
  columns change, powered-off servers are untouched, the template's 25% / 60% defaults are blanked where we hold no measurement.
* **The MCP client works against a real MCP server process** (a fake server speaking the real protocol): round trips, and the allow-list blocks
  `complete_task` and every destructive tool even though the server offers them.
* **The driver works against a scripted service** that reproduces the sequence seen in your manual run (ingest, proposal, confirm, outputs,
  enrichment, re-run, second proposal, second outputs): it sends full settings, refuses a mismatching proposal, resumes a job, times out
  cleanly, never calls `complete_task`, and writes a raw-response trace.
* From YOUR manual runs (evidence, not our code): SigV4 access without a browser login; `migration-assessment-agent-v2` is enabled; the `.xlsx`
  upload is accepted; settings and confirmations work through chat; outputs (JSON, CSV, zip with XLSX, PPTX and PDF) download; the enrichment
  template merges.

* **The UI now covers the AWS Transform journey** and was driven in a real browser against the scripted service: a new **AWS Transform** tab
  (settings that will be sent, run button with live progress, a "waiting for a person" panel showing the service's proposal and the plain-language
  reason, "Check again" / "Confirm anyway", imported models, scope, AWS-vs-A01-vs-Cloudamize cross-check, assumptions, warnings, files, manual import);
  honest "A01 estimate" labels on Workloads and Licensing; a release-gate check and Review-tab action for the figures source; AWS Transform controls
  removed from Outputs. The override is recorded against the reviewer's name in the audit log. No browser errors; no `innerHTML` anywhere.

## NOT verified
* **Why the first live run never recognised the proposal.** I have not seen its trace. The likely causes are handled (text fetched via `get_resource`, stall
  detection, visible diagnostics) but not confirmed against the real service.
* **The driver against the REAL service.** Response shapes (messages, artifacts, status) are read defensively but are undocumented. Run
  `python -m cva transform-run RUN` once and send me `out/transform_job/transform_trace.jsonl`; any wrong guess is then a small fix.
* **Whether enrichment changes the result.** The re-run you started was never completed, so WEB-P01's before/after is unknown. Whether the
  service accepts blank values in the enrichment file is also untested (`policy.blank_unknown` in `config/mappings/aws_transform_enrichment.toml`).
* **Cloudamize into AWS Transform.** No MPA export has been seen. Our OLA export and "Download All Reports" are not listed AWS Transform inputs;
  the driver refuses them unless `--allow-unlisted-format` is given.
* **Per-server AWS results.** The analysis XLSX layout is unseen, so per-server recommendations and per-server parity are not imported.
  The rightsizing slide is labelled as A01's estimate.
* **BYOL / SQL licensing from AWS Transform** (needs database data; untested what-if).
* **The UI's run button against the real service** (it uses the same untested driver; the browser tests use the scripted stand-in).
* A dedicated least-privilege IAM role (only your admin role was tested), the real Bedrock model, run time per assessment, and hosting.

## Known limits
* The service ignored the RVTools utilisation readings and used 25% CPU / 60% memory. Cloudamize measurements must go in through enrichment.
* AWS Transform's on-premises baseline uses default discounts (hardware 40%, software 25%, storage 50%): not real customer costs.
* A01's own estimate runs about 2x AWS's on the same file; prices remain a placeholder catalog. Lifecycle dates remain unverified.
* The local UI has no sign-in and must not be exposed.

## Next
1. Run `transform-run` once; send the trace file. 2. Send `analysis_*.xlsx` and a real Cloudamize MPA export.
3. Finish the enrichment comparison (WEB-P01) to confirm the loop. 4. Create a limited IAM role for A01.
