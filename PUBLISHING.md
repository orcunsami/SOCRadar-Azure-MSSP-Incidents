# Publishing Guide — SOCRadar MSSP Incidents Integration

This guide is for the SOCRadar engineer who will publish this integration to the SOCRadar GitHub organization (or any other namespace) so customers can deploy from the SOCRadar-branded repository.

The standalone bundle is currently namespaced to `orcunsami/SOCRadar-Azure-MSSP-Incidents` (placeholder). To republish under SOCRadar branding, follow the steps below.

## Prerequisites

- A GitHub organization (e.g. `SOCRadar-Inc`) with admin access
- A workstation with `bash`, `zip`, `jq` (only for the publisher — customers never need any of these)

This integration is **pure ARM JSON, Logic-App based**. There is no Bicep build step, no Function App package, no compiled assets. Customers deploy by clicking **Deploy to Azure** on the rendered ARM template URL.

## Step 1 — Create a new public repository

In the SOCRadar GitHub organization, create a new public repository:

```
SOCRadar-Inc/SOCRadar-Azure-MSSP-Incidents
```

(or any name you prefer — substitute everywhere below).

## Step 2 — Copy source files into the repository

Extract this delivery bundle, then copy the following files into the new repo root:

```
production/                              ← the standalone integration source
README.md                                ← customer-facing project overview
CHANGELOG.md
LICENSE
CUSTOMER-TEST-RUNBOOK-MSSP.md            ← forward to customers as the acceptance test
PUBLISHING.md (this file — optional, internal)
```

The `production/` directory contains:

```
production/
├── azuredeploy.json                     ← combined root template (both Logic Apps + embedded infra)
├── README.md                            ← in-repo deploy walkthrough
├── socradar-kql-queries.kql             ← KQL recipes including MSSP-specific Company Breakdown
├── Analytic Rules/                      ← 3 YAML files
└── Playbooks/
    ├── SOCRadar-MSSP-Alarm-Import/
    │   └── azuredeploy.json             ← Import playbook (DCE + tables + DCRs + Logic App embedded)
    └── SOCRadar-MSSP-Alarm-Sync/
        └── azuredeploy.json             ← Sync playbook
```

## Step 3 — Find-and-replace the GitHub namespace

The README references `orcunsami/SOCRadar-Azure-MSSP-Incidents`. Replace with your namespace.

```bash
cd path/to/socradar-azure-mssp-incidents
grep -rl "orcunsami/SOCRadar-Azure-MSSP-Incidents" . \
  | xargs sed -i '' 's|orcunsami/SOCRadar-Azure-MSSP-Incidents|SOCRadar-Inc/SOCRadar-Azure-MSSP-Incidents|g'

# Verify no stragglers
grep -rn "orcunsami" .
```

Should return zero hits after the replace.

There is **no embedded URL in the ARM templates** (no `WEBSITE_RUN_FROM_PACKAGE`, no release-asset reference). The only namespace surface is the Deploy-to-Azure button URL in the README files.

## Step 4 — Commit + push to your repository

```bash
git init
git add .
git commit -m "Initial release v1.0.0 — SOCRadar MSSP Incidents Integration for Microsoft Sentinel"
git branch -M master
git remote add origin git@github.com:SOCRadar-Inc/SOCRadar-Azure-MSSP-Incidents.git
git push -u origin master
```

## Step 5 — Tag the v1.0.0 release

This is optional but recommended for future versioning:

```bash
git tag v1.0.0 -m "Initial release"
git push origin v1.0.0
```

There is **no GitHub release asset to attach** — customers deploy the ARM template directly from the raw URL on `master` (or a tagged ref).

## Step 6 — Test the Deploy to Azure button

Click the **Deploy to Azure** button on your new repository's README. Verify the form opens with parameters:

- `CompanyIds` (CSV, required)
- `CompanyNames` (CSV, required, same length)
- `SocradarApiKey` (required, MSSP-scoped)
- `WorkspaceName` (required)
- `WorkspaceResourceGroup` (optional, defaults to deployment RG)
- `WorkspaceLocation` (optional)

You can cancel without deploying — the goal is to confirm the link resolves to the correct template.

## What is and is NOT in this bundle

**Included:**
- `production/` — full ARM template source (combined root + per-playbook standalone forms), Analytic Rules, KQL recipes, in-repo README.
- `README.md`, `CHANGELOG.md`, `LICENSE` — customer-facing project files.
- `CUSTOMER-TEST-RUNBOOK-MSSP.md` — 45-minute acceptance test runbook (3 companies × 3 dummy alarms; forward to customers as onboarding validation).
- `PUBLISHING.md` (this file).

**Features in this v1.0.0 release:**
- Multi-company alarm import — one Logic App polls SOCRadar per company per cycle, configurable via `CompanyIds` + `CompanyNames` CSV parameters.
- Bidirectional sync — closed Microsoft Sentinel incidents flow back to the matching SOCRadar account (`TruePositive` → Resolved, `FalsePositive` → False Positive, `BenignPositive` → Mitigated).
- Per-company incident tagging — title prefixed `[<CompanyName>]`, label `Company:<CompanyName>`.
- Per-company error isolation — one company's API failure logs to audit but does not block other companies.
- Custom Log Analytics tables `SOCRadar_MSSP_Alarms_CL` + `SOCRadar_MSSP_AuditLog_CL` with `company_name` column for KQL pivoting.
- 1 Microsoft Sentinel workbook (Company Breakdown + CompanyName filter), 5 hunting queries, 3 analytic rules — all group by company.
- DCR-based Logs Ingestion API (HTTP Data Collector deprecation handled).
- Azure Managed Identity auth — no App Registration, no FIC, no Graph permissions.

**NOT included (intentionally):**
- `_archive_old/` — pre-OptionA prototype kept inside the source folder for archaeology only. Excluded from the delivery zip via `zip -x "_archive_old/*"`.
- `dev/` — SOCRadar internal scripts (transform Python, sample-data generators, KQL validation test JSONs).
- Internal test artefacts, audit logs, session notes.

## Customer flow (after you publish)

Customers go to your repository and click **Deploy to Azure**. The Azure Portal opens with the ARM template:

| Customer profile | Deploy parameters |
|------------------|--------------------|
| MSSP monitoring N customer companies from one Microsoft Sentinel workspace | `CompanyIds=132,200,300`, `CompanyNames=Acme,Beta,Gamma`, MSSP-scoped `SocradarApiKey`, existing `WorkspaceName` |
| Holding company with multiple legal entities | Same — list each entity's SOCRadar Company ID + a human-readable name |
| Single-company customer | **Use the single-customer `SOCRadar` solution instead.** This MSSP edition is over-engineered for the single-company case. |

The MSSP-scoped API key is configured by the SOCRadar account team — one key authorises every `CompanyId` listed in the deploy parameter. See `CUSTOMER-TEST-RUNBOOK-MSSP.md` Step 0.

See `production/README.md` for full parameter reference.

## Updating to a new version

When you release a new version (e.g. v1.0.1):

1. Update `CHANGELOG.md` with the new version entry.
2. Update `metadata.version` in `production/azuredeploy.json` (root) and each `production/Playbooks/*/azuredeploy.json`. Bump the `hidden-SentinelTemplateVersion` tag on each Logic App resource if behavior changed materially.
3. Commit + push.
4. Tag the new release (`git tag v1.0.1 ...`).

Existing customer deployments will continue to run the old version (the customer's deployment captured the template at deploy time). They redeploy from the new tag to upgrade.

## Support

For questions about publishing or the integration itself, contact the original maintainer.
