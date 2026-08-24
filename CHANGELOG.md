# Changelog

All notable changes to the SOCRadar MSSP Incidents Integration for Microsoft Sentinel
are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.1.0] - 2026-08-21

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

- **MSSP-scoped API key required.** The single `SocradarApiKey` must cover every
  Company ID listed in `CompanyIds`. SOCRadar's account team configures this on
  their side. Documented in README and `CUSTOMER-TEST-RUNBOOK.md`.
- **Logic App based.** No Entra ID App Registration, no FIC, no Graph
  permissions. The Logic Apps authenticate to Microsoft Sentinel via Azure Managed
  Identity.
- **Customer-facing version**: 1.0.0. **Content Hub Solution version**: 3.0.0
  (Microsoft V3 builder minimum - a separate namespace from the customer
  version).

[1.0.0]: https://github.com/orcunsami/SOCRadar-Azure-MSSP-Incidents/releases/tag/v1.0.0
