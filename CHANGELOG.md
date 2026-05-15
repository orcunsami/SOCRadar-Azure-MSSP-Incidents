# Changelog

All notable changes to the SOCRadar MSSP Incidents Integration for Microsoft Sentinel
are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.0.0] — 2026-05-15

Initial public release. MSSP / holding-company variant of
[`SOCRadar-Azure-Incidents`](https://github.com/orcunsami/SOCRadar-Azure-Incidents)
that monitors N SOCRadar Company IDs from a single Microsoft Sentinel deployment.

### Added

- **`CompanyIds` + `CompanyNames` CSV parameters** — replaces single-customer
  `CompanyId`. The Logic App's `Build_Company_Array` action `zip()`s the two
  CSVs into `[{CompanyId, CompanyName}]` and `For_Each_Company` (concurrency 1)
  iterates them.
- **Per-company incident tagging** — incident title prepended with
  `[<CompanyName>] `; label `Company:<CompanyName>` added. Dedup key is
  composite `(company_id, alarm_id)`.
- **Per-company error isolation** — `Scope_Try_Company` + `Scope_Catch_Company`
  around each company iteration. One company's failure does not block others.
- **Length-mismatch validation** — `Validate_Company_Arrays` action terminates
  the run with a descriptive error if `CompanyIds` and `CompanyNames` CSVs are
  different lengths.
- **MSSP custom LAW tables** — `SOCRadar_MSSP_Alarms_CL` and
  `SOCRadar_MSSP_AuditLog_CL`. Both carry a `company_name` column so KQL can
  pivot per-customer without joining to a configuration table.
- **MSSP-prefixed resource names** — `SOCRadar-MSSP-DCE`,
  `SOCRadar-MSSP-Alarms-DCR`, `SOCRadar-MSSP-Audit-DCR`,
  `SOCRadar-MSSP-Alarm-Import`, `SOCRadar-MSSP-Alarm-Sync`. Can co-exist with
  the single-customer integration in the same Resource Group.
- **Embedded Option A infrastructure** — DCE, custom tables, DCRs, and role
  assignments live inside the Import playbook template. Content Hub Solution
  install paths through `Manage` view show only the two playbook templates,
  not separate Infrastructure templates.
- **Multi-tenant Recurrence trigger guard** — `operationOptions: SingleInstance`
  on the timer trigger prevents overlapping runs when iterating large CSVs.
- **3 MSSP Analytic Rules** — `SOCRadar MSSP — Alarm Volume Spike`,
  `Critical Alarm Detection`, `Unsynced Closed Incident`. All group by
  `company_name`.
- **5 MSSP Hunting Queries** — Company Breakdown, Critical Alarms, Alarm
  Overview, Alarm Trends, Audit Analysis.
- **1 MSSP Workbook** — `SOCRadar-MSSP-Dashboard` with Company Breakdown tile,
  CompanyName filter dropdown, and per-source detail panes.

### Notes

- **MSSP-scoped API key required.** The single `SocradarApiKey` must cover every
  Company ID listed in `CompanyIds`. SOCRadar's account team configures this on
  their side. Documented in README and `CUSTOMER-TEST-RUNBOOK.md`.
- **Logic App based.** No Entra ID App Registration, no FIC, no Graph
  permissions. The Logic Apps authenticate to Microsoft Sentinel via Azure Managed
  Identity.
- **Customer-facing version**: 1.0.0. **Content Hub Solution version**: 3.0.0
  (Microsoft V3 builder minimum — a separate namespace from the customer
  version).

[1.0.0]: https://github.com/orcunsami/SOCRadar-Azure-MSSP-Incidents/releases/tag/v1.0.0
