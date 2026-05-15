# SOCRadar MSSP Incidents Integration — Customer Acceptance Test Runbook

**Audience:** MSSP / holding-company IT/SOC engineer running the integration for the first time, where one Microsoft Sentinel workspace is monitoring **multiple SOCRadar company IDs**.
**Goal:** Confirm the integration imports SOCRadar alarms from multiple companies as Microsoft Sentinel incidents tagged per company, syncs closed Microsoft Sentinel incidents back to the matching SOCRadar account, and lands per-company audit rows in Log Analytics — end to end — using 3 throwaway test companies × 3 dummy alarms each.
**Duration:** ~45 minutes (10 min SOCRadar-side seeding, 15 min deploy, 15 min verification, 5 min cleanup).

---

## 0. What's exchanged before the test

| Item | Provided by | Sent to |
|------|-------------|---------|
| 3 test SOCRadar Company IDs (e.g. 132, 200, 300) | SOCRadar account team | Customer |
| Human-readable names for each company (e.g. Acme, Beta, Gamma) | Customer or SOCRadar | Deploy parameter |
| **MSSP-scoped `SocradarApiKey`** — one key that grants `/api/company/{id}/incidents/v4` access for ALL listed companies | SOCRadar account team | Deploy parameter |
| Microsoft Sentinel workspace name + resource group | Customer | Deploy parameter |

**MSSP-scoped API key**: this is a single SOCRadar API key configured by the account team to authorize multiple `CompanyId` paths. The customer does **not** rotate keys per company. If the customer has separate per-company keys instead, contact SOCRadar account team before this test — the integration assumes one key.

---

## 1. SOCRadar side — seed 9 dummy alarms across 3 companies (10 min)

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

— should return 3 records. Repeat for `200` and `300`. The **same** key must work for all three.

Confirm to customer: "9 alarms seeded across companies 132, 200, 300. Ready for deploy."

---

## 2. Customer side — deploy via Content Hub or Deploy to Azure (15 min)

### Option A — Content Hub install (preferred for Marketplace path)

1. Microsoft Sentinel → **Content Hub**.
2. Search for **SOCRadar MSSP** → **Install**.
3. From the Solution's **Manage** view, click **Configure** on each of the 2 playbook templates:

   **SOCRadar-MSSP-Alarm-Import:**
   - **Playbook Name**: leave default `SOCRadar-MSSP-Alarm-Import`
   - **Company Ids**: `132,200,300`
   - **Company Names**: `Acme,Beta,Gamma`
   - **Socradar Api Key**: the MSSP-scoped API key
   - **Workspace Name**: your Microsoft Sentinel workspace name
   - **Workspace Resource Group**: (optional) leave blank if same RG; set if workspace lives in another RG
   - **Workspace Location**: e.g. `westeurope` — must match your workspace's region

   **SOCRadar-MSSP-Alarm-Sync:**
   - **Playbook Name**: leave default `SOCRadar-MSSP-Alarm-Sync`
   - **Company Ids**: `132,200,300` *(must match Import)*
   - **Company Names**: `Acme,Beta,Gamma` *(must match Import)*
   - **Socradar Api Key**: same MSSP-scoped key
   - **Workspace Name**: same workspace

4. **Review + Create** on each playbook. Wait for `Succeeded` (~3–5 minutes each).

### Option B — Standalone Deploy to Azure button

1. Open the GitHub repo README: `https://github.com/<socradar-org>/SOCRadar-Azure-MSSP-Incidents`.
2. Click **Deploy to Azure** on the **combined** template.
3. Same parameters as Content Hub (above), but supplied once for both Import + Sync.
4. Review + Create. Wait for `Succeeded`.

### Both options

The Logic Apps begin polling **3 minutes after deployment** to give role assignments time to propagate. If `CompanyIds` and `CompanyNames` have different counts (e.g. 3 ids, 2 names), the first run **fails fast** with `InvalidInput: CompanyIds count (3) must equal CompanyNames count (2)`.

---

## 3. Customer side — verify in Log Analytics (15 min)

Wait ~5 minutes after the trigger fires (so allow ~8 min total post-deploy), then open **Log Analytics workspace → Logs**.

### A. Per-record results — 9 alarms across 3 companies?

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
| project TimeGenerated, company_name, Severity, Message, total_alarms, new_alarms
```

**Expected:**
- 3 rows per polling cycle (one per company)
- `Severity = "INFO"` for every row
- `total_alarms = 3` and `new_alarms = 3` on the first run
- Subsequent cycles: `new_alarms = 0` (idempotent — composite `(company_id, alarm_id)` dedup key)

### C. Microsoft Sentinel Incidents — tagged per company

Microsoft Sentinel → **Incidents**. Filter by `tag = "Company:Acme"`:

**Expected:**
- 3 incidents whose **title starts with `[Acme] `**
- Each carries the tag `Company:Acme` and the standard SOCRadar tags
- Severity matches the seeded SOCRadar severity (High/Medium/Low mix)

Repeat for `Company:Beta` and `Company:Gamma`. Total 9 incidents, 3 per company.

### D. Workbook — Company Breakdown tile

Microsoft Sentinel → **Workbooks** → **My workbooks** → **SOCRadar MSSP — Dashboard**.

Open the workbook. The **CompanyName** dropdown should list Acme, Beta, Gamma. The **Company Breakdown** tile should show a 3-segment pie chart, 3 alarms each. Switching the dropdown to a single company should filter every tile.

### E. Bidirectional sync — close one Microsoft Sentinel incident → SOCRadar reflects

1. In Microsoft Sentinel Incidents, open one of the `[Acme]` incidents.
2. **Change status** → **Closed** → **Classification: FalsePositive** → **Apply**.
3. Wait ~5 minutes for the Sync Logic App to pick it up.
4. Verify via SOCRadar API:
   ```
   GET https://platform.socradar.com/api/company/132/incidents/v4
   Header: API-Key: <mssp-api-key>
   ```
   — that alarm should now report `status = "FALSE_POSITIVE"` (mapped from `FalsePositive`).

Mapping reference:
| Microsoft Sentinel Classification | SOCRadar status |
|------------------------|-----------------|
| TruePositive | Resolved |
| FalsePositive | False Positive |
| BenignPositive | Mitigated |

---

## 4. Cleanup (5 min)

**Customer side:**
1. Microsoft Sentinel → **Content Hub** → SOCRadar MSSP → **Uninstall** (or delete the resource group).
2. (If standalone deployed) Delete the resource group.

**SOCRadar side:**
1. Mark the 9 dummy alarms `RESOLVED` (or delete) on test environment across companies 132, 200, 300 so they don't bleed into the next test.

---

## 5. Pass / Fail criteria

The test is **PASS** when:
- [ ] 9 rows in `SOCRadar_MSSP_Alarms_CL`, 3 per company
- [ ] 3 rows per cycle in `SOCRadar_MSSP_AuditLog_CL` with `Severity="INFO"` and `total_alarms=3`
- [ ] 9 Microsoft Sentinel incidents, titles prefixed `[Acme]/[Beta]/[Gamma]`, tagged `Company:<name>`
- [ ] Workbook Company Breakdown tile renders 3 segments
- [ ] One Microsoft Sentinel incident closed as FalsePositive flips the SOCRadar alarm to `False Positive` within 5 minutes

If any expectation fails, capture the relevant LAW row and the Logic App **Runs history** (which company iteration ran when), then open a ticket with SOCRadar referencing the deployment's resource group + UTC timestamp.

---

## Common issues seen during first-run test

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Logic App run terminates with `InvalidInput: CompanyIds count (X) must equal CompanyNames count (Y)` | CSV lengths differ | Re-deploy with matching CSVs |
| `403 Forbidden` for one company in audit `Severity="ERROR"` rows, others succeed | API key not scoped for that company on SOCRadar side | SOCRadar account team adds the company to the MSSP key |
| Two companies' incidents collide / dedup fails | One company seeded the same `alarmId` as another | Use unique alarmIds across companies — `(company_id, alarm_id)` is the composite key but `alarmId` collisions in seed data hint at a SOCRadar-side fixture bug |
| Microsoft Sentinel incidents created but no `Company:<name>` tag | Microsoft Sentinel API rejected the label (e.g. special chars in name) | Use plain alphanumeric `CompanyNames` — the integration sanitises to `[A-Za-z0-9 _-]` and truncates at 90 chars |
| Sync didn't reflect a closed incident in SOCRadar | Incident closed but Sync Logic App disabled, or `Classification` left as Undetermined | Check Logic App `SOCRadar-MSSP-Alarm-Sync` is Enabled + has run since the close; set Classification before closing |
| Workbook Company Breakdown empty | Custom-table first-write latency | Wait 10 min after first ingest; LAW custom tables can take time on the first row |
