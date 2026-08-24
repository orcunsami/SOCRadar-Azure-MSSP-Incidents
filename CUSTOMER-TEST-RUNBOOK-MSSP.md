# SOCRadar MSSP Incidents Integration - Customer Acceptance Test Runbook

**Audience:** MSSP / holding-company IT/SOC engineer running the integration for the first time, where one Microsoft Sentinel workspace is monitoring **multiple SOCRadar company IDs**.
**Goal:** Confirm the integration imports SOCRadar alarms from multiple companies as Microsoft Sentinel incidents tagged per company, syncs closed Microsoft Sentinel incidents back to the matching SOCRadar account, and lands per-company audit rows in Log Analytics - end to end - using 3 throwaway test companies x 3 dummy alarms each.
**Duration:** ~45 minutes (10 min SOCRadar-side seeding, 15 min deploy, 15 min verification, 5 min cleanup).

---

## 0. What's exchanged before the test

| Item | Provided by | Sent to |
|------|-------------|---------|
| 3 test SOCRadar Company IDs (e.g. 132, 200, 300) | SOCRadar account team | Customer |
| Human-readable names for each company (e.g. Acme, Beta, Gamma) | Customer or SOCRadar | Deploy parameter |
| **MSSP-scoped `SocradarApiKey`** - one key that grants `/api/company/{id}/incidents/v4` access for ALL listed companies | SOCRadar account team | Deploy parameter |
| Microsoft Sentinel workspace name + resource group | Customer | Deploy parameter |

**MSSP-scoped API key**: this is a single SOCRadar API key configured by the account team to authorize multiple `CompanyId` paths. The customer does **not** rotate keys per company. If the customer has separate per-company keys instead, contact SOCRadar account team before this test - the integration assumes one key.

---

## 1. SOCRadar side - seed 9 dummy alarms across 3 companies (10 min)

SOCRadar operations team creates 3 dummy alarms for each of 3 test company IDs:

| Company ID | Friendly name | Alarms |
|------------|---------------|--------|
| 132 | Acme | `[acme-test-1]`, `[acme-test-2]`, `[acme-test-3]` |
| 200 | Beta | `[beta-test-1]`, `[beta-test-2]`, `[beta-test-3]` |
| 300 | Gamma | `[gamma-test-1]`, `[gamma-test-2]`, `[gamma-test-3]` |

Each alarm gets a unique `alarmId` so sync semantics work later. Alarm `status` is `OPEN`, `mainType` and `subType` set to a recognisable SOCRadar category (e.g. `Brand Protection / Phishing Domain`), `severity` set to High/Medium/Low one each.

Verify the alarms appear in the SOCRadar API per company:

```
GET https://platform.socradar.com/api/company/132/incidents/v4?status=OPEN
Header: API-Key: <mssp-api-key>
```

- should return 3 records. Repeat for `200` and `300`. The **same** key must work for all three.

Confirm to customer: "9 alarms seeded across companies 132, 200, 300. Ready for deploy."

---

## 2. Customer side - deploy via Deploy to Azure button (15 min)

1. Open the GitHub repo README: `https://github.com/<socradar-org>/SOCRadar-Azure-MSSP-Incidents`.
2. Click **Deploy to Azure** on the combined ARM template.
3. Fill the form with these parameters:
   - **Resource Group**: a new or existing RG for the integration
   - **Region** / **Workspace Location**: e.g. `westeurope` - must match your Microsoft Sentinel workspace's region
   - **Workspace Name**: your existing Microsoft Sentinel workspace name
   - **Workspace Resource Group**: (optional) leave blank if same RG; set if workspace lives in another RG
   - **SOCRadar API Key**: the MSSP-scoped API key from the SOCRadar account team
   - **Company Ids**: `132,200,300`
   - **Company Names**: `Acme,Beta,Gamma`
   - **Enable Alarms Table**: [OK] true
   - **Enable Audit Logging**: [OK] true
4. **Review + Create**. Wait for `Succeeded` (~1 minute).

The Logic Apps begin polling **3 minutes after deployment** to give role assignments time to propagate. If `CompanyIds` and `CompanyNames` have different counts (e.g. 3 ids, 2 names), the first run **fails fast** with `InvalidInput: CompanyIds count (3) must equal CompanyNames count (2)`.

---

## 3. Customer side - verify in Log Analytics (15 min)

Wait ~5 minutes after the trigger fires (so allow ~8 min total post-deploy), then open **Log Analytics workspace -> Logs**.

### A. Per-record results - 9 alarms across 3 companies?

```kql
SOCRadar_MSSP_Alarms_CL
| where TimeGenerated > ago(30m)
| project TimeGenerated, company_name, AlarmId, Title, Severity, Status
| sort by company_name asc, AlarmId asc
```

**Expected:**
- 9 rows
- 3 rows per `company_name` (Acme, Beta, Gamma)
- `Status = "OPEN"` for all
- `Title` and `AlarmId` match the seeded alarms

### B. Per-company per-cycle audit

```kql
SOCRadar_MSSP_AuditLog_CL
| where TimeGenerated > ago(30m)
| sort by TimeGenerated desc
| project TimeGenerated, company_name, EventType, AlarmId, Severity, Message
```

**Expected:**
- One row per imported alarm, not one row per company. `EventType = "AlarmImported"`
- 9 rows on the first run (3 alarms x 3 companies)
- `Severity` carries the alarm severity from SOCRadar (HIGH / MEDIUM / LOW / CRITICAL), not a log level
- A company whose API call fails produces `EventType = "CompanyImportFailed"` instead, and the run ends `Failed`
- Subsequent cycles: no new rows (idempotent). The dedup key is `(CompanyName, alarm_id)`, parsed back out of the incident title

### C. Microsoft Sentinel Incidents - tagged per company

Microsoft Sentinel -> **Incidents**. Filter by `tag = "MSSP-Acme"`:

**Expected:**
- 3 incidents whose **title starts with `[MSSP-Acme] `**
- Each carries the tag `MSSP-Acme` and the standard SOCRadar tags
- Severity matches the seeded SOCRadar severity (High/Medium/Low mix)

Repeat for `MSSP-Beta` and `MSSP-Gamma`. Total 9 incidents, 3 per company.

### D. Workbook - company breakdown tiles

Microsoft Sentinel -> **Workbooks** -> **My workbooks** -> **SOCRadar MSSP Dashboard**.

Open the workbook. **Alarms by Company** should render a bar per company (3 alarms each) and **Alarms by Company and Severity** should list every company/severity pair. There is no CompanyName dropdown; use the workbook time range and the per-tile filters instead.

### E. Bidirectional sync - close one Microsoft Sentinel incident -> SOCRadar reflects

1. In Microsoft Sentinel Incidents, open one of the `[MSSP-Acme]` incidents.
2. **Change status** -> **Closed** -> **Classification: FalsePositive** -> **Apply**.
3. Wait ~5 minutes for the Sync Logic App to pick it up.
4. Verify via SOCRadar API:
   ```
   GET https://platform.socradar.com/api/company/132/incidents/v4
   Header: API-Key: <mssp-api-key>
   ```
   - that alarm should now report `status = "FALSE_POSITIVE"` (mapped from `FalsePositive`).

Mapping reference:
| Microsoft Sentinel Classification | SOCRadar status |
|------------------------|-----------------|
| TruePositive | Resolved |
| FalsePositive | False Positive |
| BenignPositive | Mitigated |

---

## 4. Cleanup (5 min)

**Customer side:**
1. Delete the deployment resource group (this removes all integration resources).
2. (If standalone deployed) Delete the resource group.

**SOCRadar side:**
1. Mark the 9 dummy alarms `RESOLVED` (or delete) on test environment across companies 132, 200, 300 so they don't bleed into the next test.

---

## 5. Pass / Fail criteria

The test is **PASS** when:
- [ ] 9 rows in `SOCRadar_MSSP_Alarms_CL`, 3 per company
- [ ] 9 rows in `SOCRadar_MSSP_AuditLog_CL` on the first cycle, `EventType="AlarmImported"`, one per alarm
- [ ] 9 Microsoft Sentinel incidents, titles prefixed `[MSSP-Acme]/[MSSP-Beta]/[MSSP-Gamma]`, tagged `MSSP-<name>`
- [ ] Workbook **Alarms by Company** tile renders one bar per company
- [ ] The Import run ends `Succeeded`; if any company failed it ends `Failed` with `ImportIncomplete`
- [ ] One Microsoft Sentinel incident closed as FalsePositive flips the SOCRadar alarm to `False Positive` within 5 minutes

If any expectation fails, capture the relevant LAW row and the Logic App **Runs history** (which company iteration ran when), then open a ticket with SOCRadar referencing the deployment's resource group + UTC timestamp.

---

## Common issues seen during first-run test

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Logic App run terminates with `InvalidInput: CompanyIds count (X) must equal CompanyNames count (Y)` | CSV lengths differ | Re-deploy with matching CSVs |
| Sync run fails with `403 OperationFailed` on `Log_Sync_Event` right after deployment | The Monitoring Metrics Publisher grant on the audit Data Collection Rule has not reached the ingestion endpoint yet | Wait and re-run. Azure Monitor caches data-plane authorization; a fresh grant took about 7 minutes to take effect in our own greenfield test, and Microsoft documents up to 30 minutes. The status write to SOCRadar already succeeded, so only the audit row is missing |
| One company fails, others succeed | API key not scoped for that company on SOCRadar side | Look for `EventType="CompanyImportFailed"` in the audit table. The other companies still import, but the run now ends `Failed` with `ImportIncomplete` so the failure is not silent. SOCRadar account team adds the company to the MSSP key |
| Two companies' incidents collide / dedup fails | Two companies share a `CompanyName`, or a name was changed after the first import | The dedup key is `(CompanyName, alarm_id)` parsed from the incident title, so `CompanyNames` values must be unique and stable. Renaming a company makes its existing incidents invisible to dedup and they will be re-created |
| Microsoft Sentinel incidents created but no `MSSP-<name>` tag | Microsoft Sentinel API rejected the label | `Build_Company_Array` strips the characters that would break the incident title or the tag JSON (backslash, double quote, square and curly brackets, `#`). It does not truncate, and Microsoft Sentinel rejects labels longer than 90 characters, so keep `CompanyNames` values short and plain |
| Sync didn't reflect a closed incident in SOCRadar | Incident closed but Sync Logic App disabled, or `Classification` left as Undetermined | Check Logic App `SOCRadar-MSSP-Alarm-Sync` is Enabled + has run since the close; set Classification before closing |
| Workbook Company Breakdown empty | Custom-table first-write latency | Wait 10 min after first ingest; LAW custom tables can take time on the first row |
