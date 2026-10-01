#!/usr/bin/env bash
# Regression test for EXP-AZURE-0198, ported to the MSSP edition.
#
# A wrong WorkspaceName with the parameters left at their defaults used to
# half-install: ARM counts a condition:false resource as a satisfied dependency,
# so depending on the Workspace resource stopped nothing. The Microsoft Sentinel
# API connection, the Data Collection Endpoint, the workbook and BOTH Logic Apps
# were created and only then did the deployment fail with ResourceNotFound. The
# Import Logic App is enabled and billable in that state.
#
# It shipped that way because every earlier test ran DeployNewWorkspace=true --
# a fresh workspace is what a greenfield test needs -- so the path a customer
# actually clicks, the defaults, was never run.
#
# Seven paths, each asserting a different thing:
#   A  missing workspace, DeployNewWorkspace=false -> fails, resource group stays EMPTY
#   B  same RG, defaults (one click)          -> succeeds, guard SKIPPED
#   C1 existing workspace, same RG           -> guard SUCCEEDS and resolves customerId
#   C2 cross-RG onto a separately onboarded workspace -> assert SUCCEEDS, no endpoint
#   D  redeploy against the workspace B made  -> the guard does not block it
#   E  cross-RG onto a workspace with no Microsoft Sentinel -> rejected, nothing created
#   F  audit logging with the alarms table off -> succeeds, endpoint still created
#
# C matters most: the guard is skipped on the greenfield path, so a wrong
# api-version or field name inside it would leave every test green and fail only
# at a customer. That is how reference(...).name once shipped in Incidents.
#
# No SOCRadar credentials are needed -- the deployment only writes the key into
# the Logic App definition. Placeholders are passed so no real secret enters a
# test run.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE="$ROOT/azuredeploy.json"

# No hardcoded subscription default: an id baked into a sibling script once aimed a
# test at an account that had been closed. TEST_SUBSCRIPTION names the target, it must
# equal the logged-in context, and it is printed, so a run can never be about a
# subscription nobody looked at.
#
# Not passed per command either. `az --subscription <id> group create` is rejected
# by this CLI -- the flag only parses AFTER the subcommand -- and a shell variable
# holding "az --subscription <id>" behaves differently in bash and zsh. Both were
# tried here and both produced a run where every assertion read <unreadable>.
SUBSCRIPTION="$(az account show --query id -o tsv 2>/dev/null || true)"
if [ -z "$SUBSCRIPTION" ]; then
    echo "ERROR: not logged in. Run 'az login'." >&2
    exit 2
fi
# The target must be named, not inherited: a context that drifted to a stale account
# would otherwise run seven deployments there without anyone having chosen it.
if [ -z "${TEST_SUBSCRIPTION:-}" ]; then
    echo "ERROR: set TEST_SUBSCRIPTION to the subscription this run may deploy into." >&2
    exit 2
fi
if [ "$TEST_SUBSCRIPTION" != "$SUBSCRIPTION" ]; then
    echo "ERROR: TEST_SUBSCRIPTION is $TEST_SUBSCRIPTION but the active context is" >&2
    echo "       $SUBSCRIPTION. Run: az account set --subscription $TEST_SUBSCRIPTION" >&2
    exit 2
fi
LOCATION="${TEST_LOCATION:-westeurope}"
KEEP="${KEEP_RESOURCES:-false}"

SFX=$(python3 -c "import uuid;print(uuid.uuid4().hex[:4])")
RG_APP="rg-mssp-paths-$SFX"
RG_WS="rg-mssp-paths-ws-$SFX"
RG_BARE="rg-mssp-paths-bare-$SFX"
RG_E="rg-mssp-paths-e-$SFX"
RG_X="rg-mssp-paths-x-$SFX"
RG_ONB="rg-mssp-paths-onb-$SFX"
RG_F="rg-mssp-paths-f-$SFX"
WS_ONB="ws-mssp-onb-$SFX"
WS="ws-mssp-paths-$SFX"
WS_BARE="ws-mssp-bare-$SFX"
MISSING="ws-does-not-exist-$SFX"

fails=0
row() {  # row <name> <PASS|FAIL> <detail>
    printf '%-42s %-5s %s\n' "$1" "$2" "$3"
    [ "$2" = PASS ] || fails=$((fails + 1))
}

cleanup() {
    if [ "$KEEP" = true ]; then
        echo "KEEP_RESOURCES=true, leaving $RG_APP $RG_WS $RG_BARE $RG_E $RG_X $RG_ONB $RG_F in place"
        return
    fi
    for rg in "$RG_APP" "$RG_WS" "$RG_BARE" "$RG_E" "$RG_X" "$RG_ONB" "$RG_F"; do
        az group delete -n "$rg" --yes --no-wait -o none 2>/dev/null
    done
    echo "cleanup: delete queued, NOT verified (az group exists -n <rg>), for $RG_APP $RG_WS $RG_BARE $RG_E $RG_X $RG_ONB $RG_F"
}
trap cleanup EXIT

start_time() { python3 -c "import datetime;print((datetime.datetime.now(datetime.UTC)+datetime.timedelta(minutes=180)).strftime('%Y-%m-%dT%H:%M:%SZ'))"; }

deploy() {  # deploy <rg> <name> <extra params...>
    local rg="$1" name="$2"; shift 2
    az deployment group create -g "$rg" -n "$name" --template-file "$TEMPLATE" \
        --parameters CompanyIds="1" CompanyNames="TestCompany" \
                     SOCRadarAPIKey="placeholder-not-a-real-key" \
                     WorkspaceLocation="$LOCATION" _triggerStartTime="$(start_time)" "$@" \
        -o none 2>/dev/null
}

echo "[0/5] Target subscription: $SUBSCRIPTION"
echo "[1/5] Creating $RG_APP and $RG_WS ..."
az group create -n "$RG_APP" -l "$LOCATION" -o none
az group create -n "$RG_WS"  -l "$LOCATION" -o none

# --- A: the customer's typo, defaults left alone --------------------------------
echo "[2/5] Path A: missing workspace with DeployNewWorkspace=false ..."
deploy "$RG_APP" path-a WorkspaceName="$MISSING" WorkspaceResourceGroup="$RG_APP" DeployNewWorkspace=false
state=$(az deployment group show -g "$RG_APP" -n path-a --query properties.provisioningState -o tsv 2>/dev/null)
left=$(az resource list -g "$RG_APP" --query "length(@)" -o tsv 2>/dev/null)
failed_steps=$(az deployment operation group list -g "$RG_APP" -n path-a \
    --query "[?properties.provisioningState=='Failed'].properties.targetResource.resourceName" -o tsv 2>/dev/null \
    | grep -c 'precheck-workspace-exists')

[ "$state" = Failed ] \
    && row "A deployment fails" PASS "$state" \
    || row "A deployment fails" FAIL "expected Failed, got '${state:-<unreadable>}'"
# The whole point: nothing may exist afterwards. This assertion used to be 5.
[ "${left:-x}" = 0 ] \
    && row "A leaves nothing behind" PASS "0 resources" \
    || row "A leaves nothing behind" FAIL "expected 0 resources, found '${left:-<unreadable>}' - the guard did not hold"
[ "${failed_steps:-0}" -ge 1 ] \
    && row "A blames the precheck" PASS "precheck-workspace-exists" \
    || row "A blames the precheck" FAIL "the precheck was not the failing step"

# --- B: greenfield, the only mode earlier tests used ----------------------------
echo "[3/5] Path B: new workspace name, parameters at their defaults ..."
deploy "$RG_APP" path-b WorkspaceName="$WS" WorkspaceResourceGroup="$RG_APP"
state=$(az deployment group show -g "$RG_APP" -n path-b --query properties.provisioningState -o tsv 2>/dev/null)
[ "$state" = Succeeded ] \
    && row "B greenfield succeeds" PASS "$state" \
    || row "B greenfield succeeds" FAIL "expected Succeeded, got '${state:-<unreadable>}'"
guard_ran=$(az deployment operation group list -g "$RG_APP" -n path-b \
    --query "[?properties.targetResource.resourceName=='precheck-workspace-exists'] | length(@)" -o tsv 2>/dev/null)
[ "${guard_ran:-x}" = 0 ] \
    && row "B skips the precheck" PASS "condition false" \
    || row "B skips the precheck" FAIL "the precheck ran against a workspace being created"
# Both Logic Apps must exist and be enabled, or B is green and the product is absent.
apps=$(az resource list -g "$RG_APP" --resource-type "Microsoft.Logic/workflows" --query "length(@)" -o tsv 2>/dev/null)
[ "${apps:-0}" = 2 ] \
    && row "B deploys both Logic Apps" PASS "2 workflows" \
    || row "B deploys both Logic Apps" FAIL "expected 2, found '${apps:-<unreadable>}'"

# --- C: the guard's success branch, which B never exercises ---------------------
# C1 is same-RG: the deployment RG is the workspace's own RG, so the cross-RG
# assert is skipped and the template onboards Microsoft Sentinel itself.
echo "[4/5] Path C1: existing workspace, same resource group, guard active ..."
az monitor log-analytics workspace create -g "$RG_WS" -n "$WS" -l "$LOCATION" --retention-time 30 -o none 2>/dev/null
deploy "$RG_WS" path-c1 WorkspaceName="$WS" WorkspaceResourceGroup="$RG_WS" DeployNewWorkspace=false
guard_state=$(az deployment operation group list -g "$RG_WS" -n path-c1 \
    --query "[?properties.targetResource.resourceName=='precheck-workspace-exists'].properties.provisioningState" -o tsv 2>/dev/null)
[ "$guard_state" = Succeeded ] \
    && row "C1 precheck succeeds" PASS "$guard_state" \
    || row "C1 precheck succeeds" FAIL "expected Succeeded, got '${guard_state:-<unreadable>}' - reference() may name a field or api-version that does not exist"
# Reading the output proves reference() resolved a real value rather than an empty string.
ws_id=$(az deployment group show -g "$RG_WS" -n precheck-workspace-exists \
    --query "properties.outputs.workspaceId.value" -o tsv 2>/dev/null)
printf '%s' "${ws_id:-}" | grep -Eq '^[0-9a-f]{8}-' \
    && row "C1 precheck resolves customerId" PASS "looks like a guid" \
    || row "C1 precheck resolves customerId" FAIL "output was '${ws_id:-<empty>}'"

# C2 is the real cross-RG success, and it needs a workspace NOTHING has installed
# into. Pointing it at C1's workspace tested something else entirely: the role
# assignments are named guid(workspaceResourceId, playbookName, roleId), with no
# identity in the name, so a second install from another resource group computes the
# same names for new identities and Azure refuses to repoint them --
# RoleAssignmentUpdateNotPermitted, [[EXP-AZURE-0199]], reproduced live here on
# 7 Sep 2026. That is a real product limitation, not a cross-RG bug, and it is
# waiting on a UAMI decision. So C2 onboards its own workspace.
echo "[4/5] Path C2: cross-RG onto a separately onboarded workspace ..."
az group create -n "$RG_ONB" -l "$LOCATION" -o none
az monitor log-analytics workspace create -g "$RG_ONB" -n "$WS_ONB" -l "$LOCATION" --retention-time 30 -o none 2>/dev/null
az rest --method PUT -o none \
    --url "https://management.azure.com/subscriptions/$SUBSCRIPTION/resourceGroups/$RG_ONB/providers/Microsoft.OperationalInsights/workspaces/$WS_ONB/providers/Microsoft.SecurityInsights/onboardingStates/default?api-version=2023-02-01" \
    --body '{"properties":{}}' 2>/dev/null
# Read it back. An onboarding PUT that silently did nothing would make C2 look like
# a cross-RG failure when it is really a fixture that was never set up.
onboarded=$(az rest --method GET \
    --url "https://management.azure.com/subscriptions/$SUBSCRIPTION/resourceGroups/$RG_ONB/providers/Microsoft.OperationalInsights/workspaces/$WS_ONB/providers/Microsoft.SecurityInsights/onboardingStates/default?api-version=2023-02-01" \
    --query "name" -o tsv 2>/dev/null)
[ "$onboarded" = default ] \
    && row "C2 fixture: workspace onboarded" PASS "onboardingStates/default" \
    || row "C2 fixture: workspace onboarded" FAIL "the PUT did not take, C2 below tests nothing"

az group create -n "$RG_X" -l "$LOCATION" -o none
deploy "$RG_X" path-c2 WorkspaceName="$WS_ONB" WorkspaceResourceGroup="$RG_ONB" DeployNewWorkspace=false
state=$(az deployment group show -g "$RG_X" -n path-c2 --query properties.provisioningState -o tsv 2>/dev/null)
[ "$state" = Succeeded ] \
    && row "C2 cross-RG succeeds" PASS "$state" \
    || row "C2 cross-RG succeeds" FAIL "expected Succeeded, got '${state:-<unreadable>}'"
ext_state=$(az deployment operation group list -g "$RG_X" -n path-c2 \
    --query "[?properties.targetResource.resourceName=='precheck-external-workspace'].properties.provisioningState" -o tsv 2>/dev/null)
[ "$ext_state" = Succeeded ] \
    && row "C2 cross-RG assert succeeds" PASS "$ext_state" \
    || row "C2 cross-RG assert succeeds" FAIL "expected Succeeded, got '${ext_state:-<unreadable>}'"
sentinel_id=$(az deployment group show -g "$RG_X" -n precheck-external-workspace \
    --query "properties.outputs.sentinelOnboarded.value" -o tsv 2>/dev/null)
printf '%s' "${sentinel_id:-}" | grep -q "onboardingStates/default" \
    && row "C2 assert reads the onboarding state" PASS "resolved the resource id" \
    || row "C2 assert reads the onboarding state" FAIL "output was '${sentinel_id:-<empty>}'"
# Cross-RG has no custom tables, so nothing may reference the endpoint that mode
# does not create. This is the assertion that used to read ResourceNotFound.
tables=$(az resource list -g "$RG_X" --resource-type "Microsoft.Insights/dataCollectionEndpoints" --query "length(@)" -o tsv 2>/dev/null)
[ "${tables:-x}" = 0 ] \
    && row "C2 creates no endpoint" PASS "0 endpoints, features off" \
    || row "C2 creates no endpoint" FAIL "expected 0, found '${tables:-<unreadable>}'"

# --- D: the guard must not block a second, correct deployment -------------------
echo "[5/5] Path D: redeploying B's resource group with the guard active ..."
deploy "$RG_APP" path-d WorkspaceName="$WS" WorkspaceResourceGroup="$RG_APP" DeployNewWorkspace=false
state=$(az deployment group show -g "$RG_APP" -n path-d --query properties.provisioningState -o tsv 2>/dev/null)
[ "$state" = Succeeded ] \
    && row "D redeploy succeeds" PASS "$state" \
    || row "D redeploy succeeds" FAIL "expected Succeeded, got '${state:-<unreadable>}' - the guard blocks a workspace that exists"

# --- E: cross-RG onto a bare workspace looks green and is dead ------------------
echo "[5/5] Path E: cross-RG onto a workspace with no Microsoft Sentinel ..."
az group create -n "$RG_BARE" -l "$LOCATION" -o none
az monitor log-analytics workspace create -g "$RG_BARE" -n "$WS_BARE" -l "$LOCATION" --retention-time 30 -o none 2>/dev/null
az group create -n "$RG_E" -l "$LOCATION" -o none
deploy "$RG_E" path-e WorkspaceName="$WS_BARE" WorkspaceResourceGroup="$RG_BARE" DeployNewWorkspace=false
state=$(az deployment group show -g "$RG_E" -n path-e --query properties.provisioningState -o tsv 2>/dev/null)
left=$(az resource list -g "$RG_E" --query "length(@)" -o tsv 2>/dev/null)
[ "$state" = Failed ] \
    && row "E rejects un-onboarded workspace" PASS "$state" \
    || row "E rejects un-onboarded workspace" FAIL "expected Failed, got '${state:-<unreadable>}' - a green deploy here means a dead integration"
[ "${left:-x}" = 0 ] \
    && row "E leaves nothing behind" PASS "0 resources" \
    || row "E leaves nothing behind" FAIL "expected 0, found '${left:-<unreadable>}'"

# --- F: audit logging without the alarms table ---------------------------------
# The DCE is the ingestion endpoint for BOTH DCRs but was created only when
# EnableAlarmsTable was true, so this combination -- documented and supported --
# referenced an endpoint no branch creates.
echo "[5/5] Path F: EnableAuditLogging=true with EnableAlarmsTable=false ..."
az group create -n "$RG_F" -l "$LOCATION" -o none
deploy "$RG_F" path-f WorkspaceName="ws-mssp-audit-$SFX" WorkspaceResourceGroup="$RG_F" \
    DeployNewWorkspace=true EnableAlarmsTable=false EnableAuditLogging=true
state=$(az deployment group show -g "$RG_F" -n path-f --query properties.provisioningState -o tsv 2>/dev/null)
[ "$state" = Succeeded ] \
    && row "F audit-only succeeds" PASS "$state" \
    || row "F audit-only succeeds" FAIL "expected Succeeded, got '${state:-<unreadable>}'"
# The endpoint must exist even though the alarms table is off: the audit DCR needs it.
dce=$(az resource list -g "$RG_F" --resource-type "Microsoft.Insights/dataCollectionEndpoints" --query "length(@)" -o tsv 2>/dev/null)
[ "${dce:-x}" = 1 ] \
    && row "F creates the endpoint anyway" PASS "1 endpoint" \
    || row "F creates the endpoint anyway" FAIL "expected 1, found '${dce:-<unreadable>}' - the audit DCR has nothing to ingest through"
alarms=$(az resource list -g "$RG_F" --resource-type "Microsoft.Insights/dataCollectionRules" --query "length(@)" -o tsv 2>/dev/null)
[ "${alarms:-x}" = 1 ] \
    && row "F creates only the audit DCR" PASS "1 rule" \
    || row "F creates only the audit DCR" FAIL "expected 1, found '${alarms:-<unreadable>}'"

echo
if [ "$fails" -eq 0 ]; then
    echo "All seven deployment paths behaved as asserted."
else
    echo "$fails assertion(s) failed."
fi
exit $((fails == 0 ? 0 : 1))
