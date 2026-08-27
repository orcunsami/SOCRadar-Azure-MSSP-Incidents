#!/usr/bin/env bash
# Regression test for EXP-AZURE-0160: a redeploy must never touch an existing
# workspace's pricing tier, retention or daily cap. Extracts the CURRENT
# Workspace resource straight out of azuredeploy.json (never a hand-copied
# snapshot, so it can't drift from the real template), deploys it with the
# unsafe parameter value against a workspace pre-set to a non-default
# retention, and fails if anything about that workspace changed.
#
# Run this before pushing any change that touches the Workspace resource.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE="$REPO_ROOT/azuredeploy.json"

SUBSCRIPTION="${TEST_SUBSCRIPTION:-b622bfd9-6b2b-45b3-b7d2-7b5d3114b96b}"
LOCATION="${TEST_LOCATION:-westeurope}"
RG="rg-workspace-safety-canary-mssp"
WS="workspace-safety-canary-mssp"
KEEP="${KEEP_CANARY:-false}"

WORKDIR=$(mktemp -d)
WRAPPER="$WORKDIR/wrapper.json"
trap 'rm -rf "$WORKDIR"' EXIT

echo "[1/6] Extracting current Workspace resource + deployWorkspace/isExternalWorkspace variables from $TEMPLATE ..."
python3 - "$TEMPLATE" "$WRAPPER" <<'PY'
import json, sys
template_path, out_path = sys.argv[1], sys.argv[2]
d = json.load(open(template_path))
ws = next(r for r in d["resources"] if r.get("type") == "Microsoft.OperationalInsights/workspaces")
wrapper = {
    "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
    "contentVersion": "1.0.0.0",
    "parameters": {
        "WorkspaceName": {"type": "string"},
        "WorkspaceLocation": {"type": "string", "defaultValue": "[resourceGroup().location]"},
        "WorkspaceResourceGroup": {"type": "string", "defaultValue": "[resourceGroup().name]"},
        "DeployNewWorkspace": {"type": "bool", "defaultValue": True},
    },
    "variables": {
        "isExternalWorkspace": d["variables"]["isExternalWorkspace"],
        "deployWorkspace": d["variables"]["deployWorkspace"],
    },
    "resources": [ws],
}
json.dump(wrapper, open(out_path, "w"))
print(f"  extracted condition: {ws.get('condition')}")
print(f"  extracted properties: {ws.get('properties')}")
PY

echo "[2/6] Static check: properties must be empty (this is the actual safety guarantee — an"
echo "      empty block can't overwrite anything regardless of the SKU/retention the real"
echo "      customer workspace already has, and regardless of whether the condition above is"
echo "      ever wrong). A live mutation test alone would miss this: it can only prove drift"
echo "      on whatever value the canary happens to start with."
STATIC_OK=$(python3 -c "
import json
d = json.load(open('$WRAPPER'))
props = d['resources'][0].get('properties', {})
print('true' if props == {} else 'false')
")
if [[ "$STATIC_OK" != "true" ]]; then
    echo "FAIL: Workspace resource's properties block is not empty — it can overwrite an existing workspace's settings."
    exit 1
fi
echo "  OK: properties is {}"

echo "[3/6] Ensuring canary RG + pre-configured workspace (retention=90, non-default) ..."
az group create -n "$RG" -l "$LOCATION" --subscription "$SUBSCRIPTION" -o none
if ! az monitor log-analytics workspace show -g "$RG" -n "$WS" --subscription "$SUBSCRIPTION" &>/dev/null; then
    az monitor log-analytics workspace create -g "$RG" -n "$WS" -l "$LOCATION" \
        --subscription "$SUBSCRIPTION" --retention-time 90 -o none
fi

BEFORE=$(az monitor log-analytics workspace show -g "$RG" -n "$WS" --subscription "$SUBSCRIPTION" \
    --query "{sku:sku.name, retention:retentionInDays, lastSkuUpdate:sku.lastSkuUpdate}" -o json)
echo "  before: $BEFORE"

echo "[4/6] Deploying with DeployNewWorkspace=true — the real customer mistake (pointing at an existing, same-RG workspace without flipping the toggle) ..."
az deployment group create --subscription "$SUBSCRIPTION" -g "$RG" \
    --template-file "$WRAPPER" \
    --parameters WorkspaceName="$WS" DeployNewWorkspace=true \
    --query "properties.provisioningState" -o tsv

AFTER=$(az monitor log-analytics workspace show -g "$RG" -n "$WS" --subscription "$SUBSCRIPTION" \
    --query "{sku:sku.name, retention:retentionInDays, lastSkuUpdate:sku.lastSkuUpdate}" -o json)
echo "  after:  $AFTER"

echo "[5/6] Comparing ..."
RESULT=0
if [[ "$BEFORE" != "$AFTER" ]]; then
    echo "FAIL: the existing workspace's settings changed. This is the EXP-AZURE-0160 bug."
    RESULT=1
else
    echo "PASS: existing workspace untouched."
fi

echo "[6/6] Cleanup ..."
if [[ "$KEEP" == "true" ]]; then
    echo "  KEEP_CANARY=true — leaving $RG in place for a faster rerun."
else
    az group delete -n "$RG" --subscription "$SUBSCRIPTION" --yes --no-wait
    echo "  delete started for $RG"
fi

exit $RESULT
