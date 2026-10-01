# Changelog

All notable changes to the SOCRadar MSSP Incidents Integration for Microsoft Sentinel
are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

The only tag in this repository is `v1.0.0` (annotated, 15 May 2026, commit
`4c3bde3`) and it has no GitHub release. The 1.1.0 heading below was written on
21 Aug 2026 but was never tagged, and two commits landed after it without a
changelog entry -- they are recorded here.

### Fixed

- **A refused status change no longer logs as synced.** SOCRadar answers HTTP 200 with
  `is_success: false` when it refuses a change. The `IncidentSynced` audit row was written
  regardless; it now follows the success branch only, and the refusal writes a
  `SyncWriteRejected` row carrying SOCRadar's message.
- **Audit rows no longer send `CompanyId`.** The audit table has no such column, so the
  data collection rule dropped it silently. A drift check now compares every audit row to
  the declared columns.
- **The unsynced-incident rule dedups `SecurityIncident`.** The table is append-only, so a
  closed incident that was later labelled `Synced` still matched on its older row.
- **Documentation names the real title and label** (`[MSSP-<CompanyName>]`,
  `MSSP-<CompanyName>`), the deploying principal's real roles, and the workspace default.
  `socradar-kql-queries.kql` is rewritten for the MSSP tables.
- **A wrong workspace name no longer half-installs.** The deployment resolves the
  workspace in a `precheck-workspace-exists` nested deployment that every other
  resource waits on. ARM counts a resource whose `condition` is false as a
  satisfied dependency, so depending on the Workspace resource stopped nothing:
  with `DeployNewWorkspace` at its default the Microsoft Sentinel API connection,
  the Data Collection Endpoint, the workbook and both Logic Apps were created and
  only then did the deployment fail with `ResourceNotFound`. The Import Logic App
  is enabled and billable in that state. Measured on a fresh resource group: `0`
  resources afterwards. (EXP-AZURE-0198)
- **Cross-RG onto a workspace without Microsoft Sentinel is rejected.** This
  template creates the Microsoft Sentinel solution and its onboarding state only
  in the deployment resource group, so a cross-RG deployment used to go green and
  import nothing. A `precheck-external-workspace` step now reads the external
  workspace's onboarding state first.
- **Cross-RG deployment never completed.** Pointing the template at a workspace
  in another resource group failed every time, and had done so since cross-RG
  support was written. The custom-table resources are conditioned out in that
  mode but the parameters feeding the Logic Apps still called `reference()` on
  them, and ARM evaluates a nested deployment's parameters whether the resource
  behind them exists or not. The `EnableAlarmsTable` and `EnableAuditLogging`
  flags handed to the Logic Apps are now forced to `false()` when the workspace
  is external (three call sites), and each remaining `reference()` is wrapped in
  its own resource's condition with an `'N/A'` fallback. The workbook was
  already conditioned out in that mode. Custom tables and the workbook are therefore not
  available cross-RG; incident import and sync are.
- **Audit logging without the alarms table could not deploy.** The Data
  Collection Endpoint was conditioned on `EnableAlarmsTable` alone, so
  `EnableAlarmsTable=false EnableAuditLogging=true` produced an audit Data
  Collection Rule pointing at an endpoint that was never created. The condition
  now covers either table. Measured on that combination: `Succeeded`, one
  endpoint, one rule.
- **The Deploy to Azure button in `production/README.md` was a 404.** It pointed
  at `master/azuredeploy.json`; the template lives at
  `master/production/azuredeploy.json`. The root README had the right path, which
  is why it went unnoticed.
- **CI has been failing since 27 Aug 2026.** The commit that added the workspace
  safety test also added em-dashes to it, and this repository's own ASCII gate
  rejects them. Replaced with `--`. A gate nobody reads is not a gate.
- **The workspace safety test aimed at a closed subscription.** Its default was a
  hardcoded subscription id belonging to an account that has since been closed,
  so the default silently targeted whatever that id points at today. It now uses
  `TEST_SUBSCRIPTION` or the logged-in context and refuses to run without one.
- **A redeploy cannot rewrite an existing workspace.** `DeployNewWorkspace`
  (default `true` now, `false` in those commits) and a Workspace resource that declares no properties, from the
  27 Aug commits. (EXP-AZURE-0160)

### Added

- `production/scripts/test_deploy_paths.sh` -- seven live deployment paths, 19
  assertions, including the guard's success branch, which the greenfield path
  skips: a wrong api-version inside the guard would otherwise leave every test
  green and fail only at a customer. The cross-RG path onboards its own
  workspace, so it does not inherit one an earlier path installed into.
- `tools/mutate.py` -- breaks each invariant on a copy of the tree and requires
  the drift gate to catch it. Wired into CI. 14 mutations, 0 blind. A mutation
  whose anchor no longer matches counts as a failure, not a skip.
- Static gates in `tools/check_template_drift.py`: both prechecks exist with the
  right condition and `scope: inner`, their outputs call `reference()` rather
  than `resourceId()` string arithmetic, every resource waits on them, the
  Workspace declares no properties, and every `raw.githubusercontent` link in a
  markdown file resolves to a path that exists in the repository.

### Changed

- `check_self_contained` no longer reads an inner-scope nested template's
  parameters as the parent's. It reported five false failures on
  `deploy-workspace-roles` when the prechecks were added -- scope `outer` is
  evaluated in the parent and must not be split, scope `inner` must.

## Drafted as 1.1.0, never tagged - 2026-08-21

Correctness release. Ports the alarm-import fixes made in the single-customer
integration (`SOCRadar-Azure-Incidents`) in August 2026, and closes the gap
between what the documentation described and what the templates actually did.

### Fixed

- **Per-page alarm processing.** `For_Each_Alarm` now runs *inside*
  `Company_Pagination_Loop` instead of after it. Previously every page of every
  company was accumulated into a single `all_alarms` variable before any alarm
  was imported, which could exceed the Logic App action payload limit on large
  alarms and silently truncate an import.
- **Loud failure instead of silent success.** New `pages_read` counter, a
  per-page `Check_Page_Succeeded` gate on the API's `is_success` flag, a
  per-company `Verify_Company_Complete` check and a run-level
  `Verify_Import_Complete` gate. A run that did not read every page of every
  company now terminates `Failed` with `ImportIncomplete` instead of reporting
  `Succeeded`.
- **Synced tag is gated on the SOCRadar write.** `Add_Synced_Tag` moved inside
  `Check_SOCRadar_Write_Succeeded`, which reads `is_success` from the
  status-change response. Previously the tag was applied even when the write to
  SOCRadar had failed or timed out, so the failure was invisible and the
  `Unsynced Closed Incident` rule could not see it either.
- **Critical alarm rule now fires.** `SOCRadar_MSSP_Alarms_CL` stores the
  severity exactly as SOCRadar returns it, in upper case. The rule filtered on
  `("High", "Critical")` and therefore never matched. Corrected to
  `("HIGH", "CRITICAL")`, and the alarm deep link corrected from `/alarms/` to
  `/alarm/`.
- **Company name is sanitised.** `Build_Company_Array` strips backslash, double
  quote, square brackets, curly brackets and `#` from `CompanyNames` values.
  These characters previously broke the incident title, the tag JSON, or the
  Sync playbook's title parsing.
- **Input validation is reachable.** `Validate_Company_Arrays` now runs *before*
  `Build_Company_Array`. Previously a `CompanyIds` list longer than
  `CompanyNames` crashed `Build_Company_Array` on an out-of-range index before
  the friendly `InvalidInput` message could be produced. The same validation was
  missing from the Sync playbook entirely and has been added.
- **Bounded retries and loops.** Every `PT1H` loop timeout and retry
  `maximumInterval` replaced with `PT30M` / `PT10M` loop timeouts and `PT1M`
  retry intervals, so a failing API call cannot stall a run for an hour.
- **Unmatched companies are no longer skipped silently.** A closed incident
  whose parsed company name is not in `CompanyNames` now writes a
  `CompanySkippedUnmatched` audit row.
- **Analytic rule tactic corrected.** `T1589` belongs to Reconnaissance, which
  was not in the rule's `tactics` list.

- **The standalone sync playbook can be deployed on its own.** It fed its workflow
  from `variables('dceResourceId')`, `variables('auditDcrResourceId')` and
  `variables('AuditStreamName')`, which only the one-click template declares, so
  deploying `Playbooks/SOCRadar-MSSP-Alarm-Sync/azuredeploy.json` by itself failed
  with `The template variable 'dceResourceId' is not found`. Those values are now
  template parameters (`EnableAuditLogging`, `AuditDcrEndpoint`,
  `AuditDcrImmutableId`, `AuditStreamName`, `AuditDcrResourceId`), all optional and
  audit-off by default.
- **The sync playbook can actually write its audit rows.** Nothing granted its
  managed identity Monitoring Metrics Publisher on the audit Data Collection Rule,
  so every `IncidentSynced` write came back `403 OperationFailed` and failed the run.
  The one-click template now grants it, and the standalone template grants it too
  when `AuditDcrResourceId` is supplied. Note that a fresh grant takes several
  minutes to reach the ingestion endpoint.
- **Severity is no longer silently downgraded on SOCRadar.** Microsoft Sentinel has
  no Critical severity, so a CRITICAL alarm imports as High. The sync playbook then
  wrote that High straight back, downgrading the alarm on the customer's own
  platform the moment an analyst closed the incident. The write-back is now behind a
  new `SyncSeverity` parameter, **off by default**, and the mapping emits the
  upper-case values SOCRadar stores.
- **Lookback tracks the real gap.** It was a fixed `PollingIntervalMinutes * 2`,
  which loses alarms whenever a run takes longer than that - routine once a tenant
  list is long enough. It now measures the time since the newest imported incident,
  with a 15 minute margin, clamped between `max(PollingIntervalMinutes * 6, 60)` and
  7 days. The existing-incident query gained
  `$orderby=properties/createdTimeUtc desc`, without which `first()` returned an
  arbitrary incident and the gap was computed from the wrong one.
- **Per-company failures are visible again.** `Verify_Company_Complete` sat inside
  `Scope_Try_Company`, and because it always succeeds it made the scope succeed too,
  which silently disabled `Scope_Catch_Company` and its `CompanyImportFailed` audit
  row. It is now a sibling of both scopes.

### Added

- **5 hunting queries** under `production/Hunting Queries/`, all keyed on
  `company_name`: Company Breakdown, Critical Alarms, Alarm Overview, Alarm
  Trends, Audit Analysis.
- **Per-company analytics.** All 3 analytic rules now carry `company_name`.
  `Alarm Volume Spike` computes its baseline per company, so one noisy tenant no
  longer masks or triggers another.
- **Workbook company tiles.** `Alarms by Company` and
  `Alarms by Company and Severity`, and the dashboard is now named
  `SOCRadar MSSP Dashboard`.
- **Template checks** under `.github/workflows/` and `tools/`, mirroring the
  single-customer repository, so this class of defect is caught before release.

### Changed

- Playbook `contentVersion` values raised to the 4-part form and
  `metadata.support` / `metadata.author` filled in, which Microsoft's Content Hub
  playbook validator requires.
- All shipped YAML is ASCII only.
- Documentation corrected: incident titles are `[MSSP-<CompanyName>] `, labels
  are `MSSP-<CompanyName>`, the dedup key is `(CompanyName, alarm_id)`, and the
  audit table has no `total_alarms` / `new_alarms` columns.

---

## [1.0.0] - 2026-05-15

Initial public release. MSSP / holding-company variant of
[`SOCRadar-Azure-Incidents`](https://github.com/orcunsami/SOCRadar-Azure-Incidents)
that monitors N SOCRadar Company IDs from a single Microsoft Sentinel deployment.

### Added

- **`CompanyIds` + `CompanyNames` CSV parameters** - replaces single-customer
  `CompanyId`. The Logic App's `Build_Company_Array` action pairs the two CSVs
  by index into `[{CompanyId, CompanyName}]` and `For_Each_Company`
  (concurrency 1) iterates them.
- **Per-company incident tagging** - incident title prepended with
  `[MSSP-<CompanyName>] `; label `MSSP-<CompanyName>` added. The dedup key is
  `(CompanyName, alarm_id)`, parsed back out of the incident title.
- **Per-company error isolation** - `Scope_Try_Company` + `Scope_Catch_Company`
  around each company iteration. One company's failure does not block others.
- **Length-mismatch validation** - `Validate_Company_Arrays` action terminates
  the run with a descriptive error if `CompanyIds` and `CompanyNames` CSVs are
  different lengths.
- **MSSP custom LAW tables** - `SOCRadar_MSSP_Alarms_CL` and
  `SOCRadar_MSSP_AuditLog_CL`. Both carry a `company_name` column so KQL can
  pivot per-customer without joining to a configuration table.
- **MSSP-prefixed resource names** - `SOCRadar-MSSP-DCE`,
  `SOCRadar-MSSP-Alarms-DCR`, `SOCRadar-MSSP-Audit-DCR`,
  `SOCRadar-MSSP-Alarm-Import`, `SOCRadar-MSSP-Alarm-Sync`. Can co-exist with
  the single-customer integration in the same Resource Group.
- **Embedded Option A infrastructure** - DCE, custom tables, DCRs, and role
  assignments live inside the Import playbook template. Content Hub Solution
  install paths through `Manage` view show only the two playbook templates,
  not separate Infrastructure templates.
- **Multi-tenant Recurrence trigger guard** - `operationOptions: SingleInstance`
  on the timer trigger prevents overlapping runs when iterating large CSVs.
- **3 MSSP Analytic Rules** - `SOCRadar MSSP - Alarm Volume Spike`,
  `SOCRadar MSSP - High or Critical Severity Alarm`,
  `SOCRadar MSSP - Unsynced Closed Incident`.
- **1 Workbook** - `SOCRadar Integration Dashboard`, deployed when
  `EnableWorkbook` and `EnableAlarmsTable` are both true.
- **KQL recipe file** - `socradar-kql-queries.kql`, a reference collection of
  queries for the two custom tables.

### Notes

- **MSSP-scoped API key required.** The single `SOCRadarAPIKey` must cover every
  Company ID listed in `CompanyIds`. SOCRadar's account team configures this on
  their side. Documented in README and `CUSTOMER-TEST-RUNBOOK-MSSP.md`.
- **Logic App based.** No Entra ID App Registration, no FIC, no Graph
  permissions. The Logic Apps authenticate to Microsoft Sentinel via Azure Managed
  Identity.
- **Customer-facing version**: 1.0.0. **Content Hub Solution version**: 3.0.0
  (Microsoft V3 builder minimum - a separate namespace from the customer
  version).

[1.0.0]: https://github.com/orcunsami/SOCRadar-Azure-MSSP-Incidents/releases/tag/v1.0.0
