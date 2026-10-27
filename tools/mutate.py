#!/usr/bin/env python3
"""Break each invariant on purpose and require the gate to notice.

A gate that has never failed is not evidence. Every mutation below is a defect
that actually shipped, or the direct inversion of one: the guard removed, its
scope flipped, its reference() reduced to string arithmetic, a resource no longer
waiting on it, the workspace properties that overwrite a customer's pricing tier,
and the Deploy button path that returned 404.

Each mutation is applied to a copy of the tree, the gate is run, the file is
restored, and a mutation the gate does not catch is reported as blind.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = ["python3", "tools/check_template_drift.py"]
RULE_GATE = ["python3", "tools/check_analytic_rules.py"]

TEMPLATE = "production/azuredeploy.json"
IMPORT_PLAYBOOK = "production/Playbooks/SOCRadar-MSSP-Alarm-Import/azuredeploy.json"
SYNC_PLAYBOOK = "production/Playbooks/SOCRadar-MSSP-Alarm-Sync/azuredeploy.json"
README = "production/README.md"
UNSYNCED_RULE = "production/Analytic Rules/SOCRadarUnsyncedClosedIncident.yaml"
KQL_FILE = "production/socradar-kql-queries.kql"

# (label, file, find, replace)
MUTATIONS = [
    ("workspace toggle defaults to true again",
     TEMPLATE, '"DeployNewWorkspace": {\n      "type": "bool",\n      "defaultValue": false,',
     '"DeployNewWorkspace": {\n      "type": "bool",\n      "defaultValue": true,'),

    ("the guard is gone",
     TEMPLATE, '"name": "precheck-workspace-exists",', '"name": "precheck-workspace-exists-renamed",'),

    ("the guard evaluates at outer scope",
     TEMPLATE,
     '"expressionEvaluationOptions": {\n          "scope": "inner"\n        },\n        "parameters": {\n          "WorkspaceResourceId": {\n            "value": "[variables(\'workspaceResourceId\')]"\n          }\n        },\n        "template": {\n          "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",\n          "contentVersion": "1.0.0.0",\n          "parameters": {\n            "WorkspaceResourceId": {\n              "type": "string"\n            }\n          },\n          "resources": [],\n          "outputs": {\n            "workspaceId": {',
     '"expressionEvaluationOptions": {\n          "scope": "outer"\n        },\n        "parameters": {\n          "WorkspaceResourceId": {\n            "value": "[variables(\'workspaceResourceId\')]"\n          }\n        },\n        "template": {\n          "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",\n          "contentVersion": "1.0.0.0",\n          "parameters": {\n            "WorkspaceResourceId": {\n              "type": "string"\n            }\n          },\n          "resources": [],\n          "outputs": {\n            "workspaceId": {'),

    ("the guard's output no longer calls reference()",
     TEMPLATE,
     '"value": "[reference(parameters(\'WorkspaceResourceId\'), \'2023-09-01\', \'Full\').properties.customerId]"',
     '"value": "[parameters(\'WorkspaceResourceId\')]"'),

    ("the cross-RG assert is gone",
     TEMPLATE, '"name": "precheck-external-workspace",', '"name": "precheck-external-workspace-renamed",'),

    ("the workspace declares a pricing tier again",
     TEMPLATE,
     '"name": "[parameters(\'WorkspaceName\')]",\n      "location": "[parameters(\'WorkspaceLocation\')]",\n      "properties": {}',
     '"name": "[parameters(\'WorkspaceName\')]",\n      "location": "[parameters(\'WorkspaceLocation\')]",\n      "properties": {\n        "sku": {\n          "name": "PerGB2018"\n        }\n      }'),

    ("the workspace is created unconditionally",
     TEMPLATE,
     '"condition": "[variables(\'deployWorkspace\')]",\n      "type": "Microsoft.OperationalInsights/workspaces"',
     '"condition": "[true()]",\n      "type": "Microsoft.OperationalInsights/workspaces"'),

    ("the Deploy button points at the pre-move path",
     README,
     "%2Fmaster%2Fproduction%2Fazuredeploy.json",
     "%2Fmaster%2Fazuredeploy.json"),

    # These touch no workflow definition, so the drift digest stays equal and only the
    # new check can be the one that fails.
    ("the README names the single-customer tag again",
     README,
     "+ `MSSP-<CompanyName>` (tag and label) |",
     "+ `Company:<CompanyName>` |"),

    ("the dceName output ignores the DCE's own condition",
     TEMPLATE,
     "\"value\": \"[if(and(or(parameters('EnableAuditLogging'), parameters('EnableAlarmsTable')), not(variables('isExternalWorkspace'))), variables('DceName'), 'N/A')]\"",
     "\"value\": \"[if(parameters('EnableAlarmsTable'), variables('DceName'), 'N/A')]\""),
]

# (label, file, find, replace, text the failing output must contain), run against check_analytic_rules.py
RULE_MUTATIONS = [
    ("the unsynced rule filters before it dedups SecurityIncident",
     UNSYNCED_RULE,
     "    | summarize arg_max(TimeGenerated, *) by IncidentNumber\n", "", "SecurityIncident is read without"),

    ("the KQL file reads a column the audit table does not have",
     KQL_FILE,
     "| where EventType in (\"ImportError\", \"CompanyImportFailed\", \"SyncWriteRejected\")\n| summarize FailureCount",
     "| where OperationStatus == \"Failed\"\n| summarize FailureCount",
     "'OperationStatus', which is not a column"),

    ("the KQL file compares an event name no playbook writes",
     KQL_FILE,
     "Synced = countif(EventType == \"IncidentSynced\")",
     "Synced = countif(EventType == \"AlarmSynced\")",
     "which no playbook writes"),

    ("the KQL file matches the single-customer incident title",
     KQL_FILE,
     "| where Title startswith \"[MSSP-\"\n| where Status == \"Closed\"",
     "| where Title contains \"[SOCRadar]\"\n| where Status == \"Closed\"",
     "never writes"),
]


def drop_guard_dependency(text):
    """The single most important assertion: one resource stops waiting on the guard.

    Text replacement cannot express this -- the same dependsOn entry appears on
    every resource -- so it is done on the parsed template.
    """
    d = json.loads(text)
    ref = "[resourceId('Microsoft.Resources/deployments', 'precheck-workspace-exists')]"
    for resource in d["resources"]:
        if resource.get("name") == "[variables('SyncPlaybookName')]":
            resource["dependsOn"] = [x for x in resource["dependsOn"] if x != ref]
            return json.dumps(d, indent=2, ensure_ascii=False) + "\n"
    raise SystemExit("mutation anchor gone: no resource named [variables('SyncPlaybookName')]")


def drop_external_dependency(text):
    d = json.loads(text)
    ref = "[resourceId('Microsoft.Resources/deployments', 'precheck-external-workspace')]"
    for resource in d["resources"]:
        if resource.get("name") == "[variables('ImportPlaybookName')]":
            resource["dependsOn"] = [x for x in resource["dependsOn"] if x != ref]
            return json.dumps(d, indent=2, ensure_ascii=False) + "\n"
    raise SystemExit("mutation anchor gone: no resource named [variables('ImportPlaybookName')]")


def workspace_waits_on_its_own_guard(text):
    """A cycle: the guard exists because the workspace may not be created."""
    d = json.loads(text)
    ref = "[resourceId('Microsoft.Resources/deployments', 'precheck-workspace-exists')]"
    for resource in d["resources"]:
        if resource.get("type", "").lower() == "microsoft.operationalinsights/workspaces":
            resource["dependsOn"] = [ref]
            return json.dumps(d, indent=2, ensure_ascii=False) + "\n"
    raise SystemExit("mutation anchor gone: no Workspace resource")


def guard_asserts_nothing(text):
    """Keep the guard, remove its output. It then runs and proves nothing."""
    d = json.loads(text)
    for resource in d["resources"]:
        if resource.get("name") == "precheck-workspace-exists":
            resource["properties"]["template"]["outputs"] = {}
            return json.dumps(d, indent=2, ensure_ascii=False) + "\n"
    raise SystemExit("mutation anchor gone: no precheck-workspace-exists")


def unguard_dce_reference(text):
    """Back to the shape that killed every cross-RG deployment."""
    d = json.loads(text)
    for resource in d["resources"]:
        if resource.get("type") != "Microsoft.Logic/workflows":
            continue
        params = resource["properties"].get("parameters", {})
        if "DceEndpoint" in params:
            params["DceEndpoint"]["value"] = \
                "[reference(variables('dceResourceId'), '2024-03-11').logsIngestion.endpoint]"
            return json.dumps(d, indent=2, ensure_ascii=False) + "\n"
    raise SystemExit("mutation anchor gone: no DceEndpoint workflow parameter")


def unguard_feature_flag(text):
    """Cross-RG passes EnableAlarmsTable through, so the playbook writes to a DCR
    that was never created."""
    d = json.loads(text)
    for resource in d["resources"]:
        if resource.get("type") != "Microsoft.Logic/workflows":
            continue
        params = resource["properties"].get("parameters", {})
        if "EnableAlarmsTable" in params:
            params["EnableAlarmsTable"]["value"] = "[parameters('EnableAlarmsTable')]"
            return json.dumps(d, indent=2, ensure_ascii=False) + "\n"
    raise SystemExit("mutation anchor gone: no EnableAlarmsTable workflow parameter")


def dce_only_for_alarms(text):
    """Audit logging on its own then references a DCE no branch creates."""
    d = json.loads(text)
    for resource in d["resources"]:
        if resource.get("type", "").endswith("dataCollectionEndpoints"):
            resource["condition"] = \
                "[and(parameters('EnableAlarmsTable'), not(variables('isExternalWorkspace')))]"
            return json.dumps(d, indent=2, ensure_ascii=False) + "\n"
    raise SystemExit("mutation anchor gone: no data collection endpoint")


def _sync_workflows(path_text):
    """Every workflow in a template that holds the Sync playbook's For_Each_Incident."""
    d = json.loads(path_text)
    return d, [
        r for r in d["resources"]
        if r.get("type") == "Microsoft.Logic/workflows"
        and "For_Each_Incident" in r["properties"]["definition"]["actions"]
    ]


def _dump(d):
    return json.dumps(d, indent=2, ensure_ascii=False) + "\n"


def audit_before_the_write_check(text):
    """The IncidentSynced row goes back next to the write check, so it runs on any outcome."""
    d, found = _sync_workflows(text)
    for r in found:
        chk = r["properties"]["definition"]["actions"]["For_Each_Incident"]["actions"]["Check_If_Should_Sync"]["actions"]
        guard = chk["Check_SOCRadar_Write_Succeeded"]
        audit = guard["actions"].pop("Check_Sync_Audit")
        audit["runAfter"] = {"Check_SOCRadar_Write_Succeeded": ["Succeeded"]}
        chk["Check_Sync_Audit"] = audit
    if not found:
        raise SystemExit("mutation anchor gone: no Sync workflow")
    return _dump(d)


def silent_refusal(text):
    """A refused write leaves no row at all."""
    d, found = _sync_workflows(text)
    for r in found:
        chk = r["properties"]["definition"]["actions"]["For_Each_Incident"]["actions"]["Check_If_Should_Sync"]["actions"]
        chk["Check_SOCRadar_Write_Succeeded"]["else"] = {"actions": {}}
    if not found:
        raise SystemExit("mutation anchor gone: no Sync workflow")
    return _dump(d)


def audit_row_with_undeclared_column(text):
    """Adds back the column the audit DCR never declared. Applied to all three templates,
    so the embedded and standalone definitions stay equal and drift cannot be what fails."""
    d = json.loads(text)
    touched = 0

    def visit(node):
        nonlocal touched
        if isinstance(node, dict):
            if "EventType" in node and "company_name" in node:
                node["CompanyId"] = "1"
                touched += 1
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(d)
    if not touched:
        raise SystemExit("mutation anchor gone: no audit row")
    return _dump(d)


# (label, [files], mutate, text the failing output must contain). The expectation is what
# keeps a mutation honest: the drift check runs first and would catch an edit made to one
# copy only, which proves nothing about the new check.
MULTI_MUTATIONS = [
    ("the synced audit row is written before the write check",
     [TEMPLATE, SYNC_PLAYBOOK], audit_before_the_write_check, "IncidentSynced audit row is not confined"),
    ("a refused write leaves no audit row",
     [TEMPLATE, SYNC_PLAYBOOK], silent_refusal, "SyncWriteRejected audit row"),
    ("an audit row sends a column the DCR drops",
     [TEMPLATE, IMPORT_PLAYBOOK, SYNC_PLAYBOOK], audit_row_with_undeclared_column, "does not declare and would drop"),
]


JSON_MUTATIONS = [
    ("the Sync Logic App no longer waits on the guard", TEMPLATE, drop_guard_dependency),
    ("the Import Logic App no longer waits on the cross-RG assert", TEMPLATE, drop_external_dependency),
    ("the workspace waits on its own guard", TEMPLATE, workspace_waits_on_its_own_guard),
    ("the guard keeps running but outputs nothing", TEMPLATE, guard_asserts_nothing),
    ("cross-RG references a DCE it does not create", TEMPLATE, unguard_dce_reference),
    ("cross-RG leaves the alarms table feature on", TEMPLATE, unguard_feature_flag),
    ("the DCE is created for the alarms table only", TEMPLATE, dce_only_for_alarms),
]


def run_gate(cwd, gate=None):
    result = subprocess.run(gate or GATE, cwd=cwd, capture_output=True, text=True)
    return result.returncode, (result.stdout + result.stderr).strip()


def main():
    work = tempfile.mkdtemp(prefix="mssp-mutate-")
    tree = os.path.join(work, "repo")
    shutil.copytree(REPO, tree, ignore=shutil.ignore_patterns(".git"))
    try:
        code, out = run_gate(tree)
        if code != 0:
            print("the gate is already failing on an unmutated tree, so nothing below proves anything:")
            print(out)
            return 1
        code, out = run_gate(tree, RULE_GATE)
        if code != 0:
            print("the rule gate is already failing on an unmutated tree:")
            print(out)
            return 1
        print("baseline: both gates pass on the unmutated tree")

        blind = 0
        skipped = 0
        for label, rel, find, replace in MUTATIONS:
            path = os.path.join(tree, rel)
            original = open(path, encoding="utf-8").read()
            if original.count(find) != 1:
                print("  SKIP  %-52s anchor appears %d times" % (label, original.count(find)))
                skipped += 1
                continue
            open(path, "w", encoding="utf-8").write(original.replace(find, replace))
            code, out = run_gate(tree)
            open(path, "w", encoding="utf-8").write(original)
            if code == 0:
                print("  BLIND %-52s the gate still passed" % label)
                blind += 1
            else:
                first = out.splitlines()[0].strip() if out else "(no output)"
                print("  caught %-51s %s" % (label, first[:100]))

        for label, rel, mutate in JSON_MUTATIONS:
            path = os.path.join(tree, rel)
            original = open(path, encoding="utf-8").read()
            open(path, "w", encoding="utf-8").write(mutate(original))
            code, out = run_gate(tree)
            open(path, "w", encoding="utf-8").write(original)
            if code == 0:
                print("  BLIND %-52s the gate still passed" % label)
                blind += 1
            else:
                first = out.splitlines()[0].strip() if out else "(no output)"
                print("  caught %-51s %s" % (label, first[:100]))

        for label, rel, find, replace, expect in RULE_MUTATIONS:
            path = os.path.join(tree, rel)
            original = open(path, encoding="utf-8").read()
            if original.count(find) != 1:
                print("  SKIP  %-52s anchor appears %d times" % (label, original.count(find)))
                skipped += 1
                continue
            open(path, "w", encoding="utf-8").write(original.replace(find, replace))
            code, out = run_gate(tree, RULE_GATE)
            open(path, "w", encoding="utf-8").write(original)
            if code == 0:
                print("  BLIND %-52s the gate still passed" % label)
                blind += 1
            elif expect not in out:
                print("  BLIND %-52s failed, but not on the check it targets" % label)
                blind += 1
            else:
                print("  caught %-51s %s" % (label, expect[:100]))

        for label, rels, mutate, expect in MULTI_MUTATIONS:
            originals = {}
            for rel in rels:
                path = os.path.join(tree, rel)
                originals[path] = open(path, encoding="utf-8").read()
            for path, original in originals.items():
                open(path, "w", encoding="utf-8").write(mutate(original))
            code, out = run_gate(tree)
            for path, original in originals.items():
                open(path, "w", encoding="utf-8").write(original)
            if code == 0:
                print("  BLIND %-52s the gate still passed" % label)
                blind += 1
            elif expect not in out:
                print("  BLIND %-52s failed, but not on the check it targets" % label)
                blind += 1
            else:
                print("  caught %-51s %s" % (label, expect[:100]))

        print()
        if skipped:
            # A mutation that never ran is not a passing mutation.
            print("%d mutation(s) never ran - their anchor no longer matches the file" % skipped)
        if blind or skipped:
            return 1
        print("all %d mutation(s) caught, blind 0" % (len(MUTATIONS) + len(JSON_MUTATIONS) + len(RULE_MUTATIONS) + len(MULTI_MUTATIONS)))
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
