# SOCRadar MSSP Incidents Integration for Microsoft Sentinel

Bidirectional integration that imports SOCRadar alarms from **multiple Company IDs** into a single Microsoft Sentinel workspace and syncs closed incidents back to the matching SOCRadar account. Built for MSSPs, holding companies, and any organisation that monitors several legal entities under one Microsoft Sentinel deployment.

For **single-customer** deployments, use the [`SOCRadar-Azure-Incidents`](https://github.com/orcunsami/SOCRadar-Azure-Incidents) repository instead.

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2Forcunsami%2FSOCRadar-Azure-MSSP-Incidents%2Fmaster%2Fproduction%2Fazuredeploy.json)

## Quick start

1. Click **Deploy to Azure** above.
2. Fill the form:
   - `WorkspaceName` - your existing Microsoft Sentinel workspace
   - `CompanyIds` - comma-separated SOCRadar Company IDs (e.g. `132,200,300`)
   - `CompanyNames` - same length CSV with human-readable names (e.g. `Acme,Beta,Gamma`)
   - `SOCRadarAPIKey` - **MSSP-scoped** SOCRadar API key (one key for all Company IDs; SOCRadar's account team configures this)
3. Review + Create. Logic Apps begin polling 3 minutes after deployment.

## Capabilities

- **Multi-company alarm import** - one Logic App polls SOCRadar per company per cycle, configurable via `CompanyIds` + `CompanyNames` CSV parameters.
- **Bidirectional sync** - closed Microsoft Sentinel incidents flow back to the matching SOCRadar account (`TruePositive` -> Resolved, `FalsePositive` -> False Positive, `BenignPositive` -> Mitigated).
- **Per-company tagging** - incident title prefixed `[MSSP-<CompanyName>] `; label `MSSP-<CompanyName>` added; deduplication key `(CompanyName, alarm_id)`, parsed back out of the incident title.
- **Per-company error isolation** - one company's API failure logs to audit but does not block other companies.
- **Microsoft Sentinel-native analytics** - workbook with Alarms by Company tiles, 5 hunting queries and 3 analytic rules, all keyed on `company_name`.
- **DCR-based ingestion** - custom Log Analytics tables (`SOCRadar_MSSP_Alarms_CL` + `SOCRadar_MSSP_AuditLog_CL`) with `company_name` column, written via Data Collection Rule / Logs Ingestion API.
- **Managed Identity auth** - no App Registration, no Microsoft Graph permissions, no federated identity credentials.

## Required permissions

Deployment runs with a single Azure principal. The principal needs:

| Scope | Role | Purpose |
|-------|------|---------|
| Resource group | Contributor | Create Logic Apps, DCE, DCRs, custom tables, workbook |
| Workspace RG (if different) | Contributor | Create custom tables + DCRs in the workspace's RG |
| Workspace | Microsoft Sentinel Contributor | Onboard workspace to Microsoft Sentinel (one-time, if not already onboarded) |

The Logic Apps' Managed Identity gets **Microsoft Sentinel Responder** on the workspace (least-privilege default) and **Monitoring Metrics Publisher** on the DCRs. No tenant-level admin role required.

## Documentation

- [`production/README.md`](production/README.md) - full parameter reference, architecture diagrams, troubleshooting
- [`CHANGELOG.md`](CHANGELOG.md) - version history
- [`CUSTOMER-TEST-RUNBOOK-MSSP.md`](CUSTOMER-TEST-RUNBOOK-MSSP.md) - 45-minute acceptance test runbook (3 companies x 3 alarms)

## Support

- **Documentation:** [docs.socradar.io](https://docs.socradar.io)
- **Support email:** integration@socradar.io
