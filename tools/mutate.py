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

TEMPLATE = "production/azuredeploy.json"
README = "production/README.md"

# (label, file, find, replace)
MUTATIONS = [
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


JSON_MUTATIONS = [
    ("the Sync Logic App no longer waits on the guard", TEMPLATE, drop_guard_dependency),
    ("the Import Logic App no longer waits on the cross-RG assert", TEMPLATE, drop_external_dependency),
    ("the workspace waits on its own guard", TEMPLATE, workspace_waits_on_its_own_guard),
    ("the guard keeps running but outputs nothing", TEMPLATE, guard_asserts_nothing),
    ("cross-RG references a DCE it does not create", TEMPLATE, unguard_dce_reference),
    ("cross-RG leaves the alarms table feature on", TEMPLATE, unguard_feature_flag),
    ("the DCE is created for the alarms table only", TEMPLATE, dce_only_for_alarms),
]


def run_gate(cwd):
    result = subprocess.run(GATE, cwd=cwd, capture_output=True, text=True)
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
        print("baseline: gate passes on the unmutated tree")

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

        print()
        if skipped:
            # A mutation that never ran is not a passing mutation.
            print("%d mutation(s) never ran - their anchor no longer matches the file" % skipped)
        if blind or skipped:
            return 1
        print("all %d mutation(s) caught, blind 0" % (len(MUTATIONS) + len(JSON_MUTATIONS)))
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
