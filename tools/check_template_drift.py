#!/usr/bin/env python3
"""Guard the invariants that a released MSSP template must hold.

Every check here exists because the opposite once shipped. v1.0.0 accumulated
every page of every company into one variable before importing anything, counted
a page as read whether or not the API said it succeeded, tagged an incident
"Synced" even when the write to SOCRadar had failed, and validated the CompanyIds
/ CompanyNames lengths only after the action that crashes on a mismatch. None of
that is visible in a JSON syntax check, so it is asserted here.

Run:  python3 tools/check_template_drift.py
"""
import glob
import hashlib
import json
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "production")
ROOT_TEMPLATE = os.path.join(ROOT, "azuredeploy.json")
IMPORT = os.path.join(ROOT, "Playbooks", "SOCRadar-MSSP-Alarm-Import", "azuredeploy.json")
SYNC = os.path.join(ROOT, "Playbooks", "SOCRadar-MSSP-Alarm-Sync", "azuredeploy.json")

# CompanyName lands inside an incident title, inside json(concat(...)) tag and
# label payloads, and is parsed back out of the title by the Sync playbook. Each
# of these characters breaks one of those three, so Build_Company_Array strips
# them at the single point where the value enters the workflow.
SANITISED_CHARS = ["\\", '"', "[", "]", "{", "}", "#"]

MUTATING_TYPES = {
    "SetVariable", "IncrementVariable", "DecrementVariable",
    "AppendToArrayVariable", "AppendToStringVariable",
}


def load(path):
    with open(path) as handle:
        return json.load(handle)


def workflows(template):
    return [
        resource
        for resource in template.get("resources", [])
        if (resource.get("type") or "").lower().endswith("microsoft.logic/workflows")
    ]


def definition(path):
    found = workflows(load(path))
    if len(found) != 1:
        raise SystemExit(f"{path}: expected exactly one workflow, found {len(found)}")
    return found[0]["properties"]["definition"]


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def walk(actions, path=""):
    """Yield (path, name, action) for every action, at any nesting depth."""
    for name, action in (actions or {}).items():
        if not isinstance(action, dict):
            continue
        here = f"{path}/{name}"
        yield here, name, action
        yield from walk(action.get("actions"), here)
        branch = action.get("else")
        if isinstance(branch, dict):
            yield from walk(branch.get("actions"), f"{here}/else")
        for case_name, case in (action.get("cases") or {}).items():
            yield from walk(case.get("actions"), f"{here}/case:{case_name}")


def names_at(actions):
    return set((actions or {}).keys())


def dangling_run_after(actions, path=""):
    """runAfter may only name a sibling. Anything else never runs."""
    problems = []
    siblings = names_at(actions)
    for name, action in (actions or {}).items():
        if not isinstance(action, dict):
            continue
        for dependency in (action.get("runAfter") or {}):
            if dependency not in siblings:
                problems.append(f"{path}/{name} runs after '{dependency}', which is not a sibling")
        problems += dangling_run_after(action.get("actions"), f"{path}/{name}")
        branch = action.get("else")
        if isinstance(branch, dict):
            problems += dangling_run_after(branch.get("actions"), f"{path}/{name}/else")
    return problems


def mutation_under_parallel_foreach(actions, path="", parallel=False):
    """Logic Apps rejects a variable write inside a Foreach unless concurrency is 1."""
    problems = []
    for name, action in (actions or {}).items():
        if not isinstance(action, dict):
            continue
        here = f"{path}/{name}"
        inside = parallel
        if action.get("type") == "Foreach":
            repetitions = (
                ((action.get("runtimeConfiguration") or {}).get("concurrency") or {}).get("repetitions")
            )
            inside = parallel or repetitions is None or repetitions > 1
        if inside and action.get("type") in MUTATING_TYPES:
            problems.append(f"{here} writes a variable inside a Foreach that is not concurrency 1")
        problems += mutation_under_parallel_foreach(action.get("actions"), here, inside)
        branch = action.get("else")
        if isinstance(branch, dict):
            problems += mutation_under_parallel_foreach(branch.get("actions"), f"{here}/else", inside)
    return problems


def runs_before(actions, earlier, later):
    """True when `later` transitively depends on `earlier`."""
    edges = {
        name: set((action.get("runAfter") or {}).keys())
        for name, action in actions.items()
        if isinstance(action, dict)
    }
    if earlier not in edges or later not in edges:
        return False
    seen, stack = set(), [later]
    while stack:
        current = stack.pop()
        for dependency in edges.get(current, ()):
            if dependency == earlier:
                return True
            if dependency not in seen:
                seen.add(dependency)
                stack.append(dependency)
    return False


def check_import(failures):
    actions = definition(IMPORT)["actions"]
    flat = {name: action for _, name, action in walk(actions)}
    raw = json.dumps(definition(IMPORT))

    for gone in ("Initialize_All_Alarms", "Append_Alarms", "Update_All_Alarms"):
        if gone in flat:
            failures.append(
                f"import: '{gone}' is back. Alarms must be imported page by page, not accumulated"
            )
    if "all_alarms" in raw:
        failures.append("import: the all_alarms variable is back")

    for needed in (
        "Check_Page_Succeeded", "Increment_Pages_Read", "Verify_Company_Complete",
        "Verify_Import_Complete", "Fail_Incomplete_Import",
    ):
        if needed not in flat:
            failures.append(f"import: '{needed}' is missing, so an incomplete import can report success")

    # Verify_Company_Complete must sit outside Scope_Try_Company. Inside, it always
    # succeeds and therefore makes the scope succeed, which silently disables
    # Scope_Catch_Company and with it the CompanyImportFailed audit trail.
    company = (actions.get("For_Each_Company") or {}).get("actions", {})
    inside_try = (company.get("Scope_Try_Company") or {}).get("actions", {})
    if "Verify_Company_Complete" in inside_try:
        failures.append(
            "import: Verify_Company_Complete is inside Scope_Try_Company, so the scope can "
            "never fail and Scope_Catch_Company never writes its audit row"
        )
    if "Verify_Company_Complete" not in company:
        failures.append("import: Verify_Company_Complete is not a direct child of For_Each_Company")

    loop = flat.get("Company_Pagination_Loop")
    if loop is None:
        failures.append("import: Company_Pagination_Loop is missing")
    elif "For_Each_Alarm" not in (loop.get("actions") or {}):
        failures.append("import: For_Each_Alarm is not inside Company_Pagination_Loop")

    # The lookback must track the real gap since the newest imported incident. A fixed
    # multiple of the polling interval leaves a permanent blind spot whenever a run
    # takes longer than that interval, and a large MSSP tenant list makes that routine.
    query = flat.get("Query_Existing_MSSP_Incidents") or {}
    uri = (query.get("inputs") or {}).get("uri", "")
    if "$orderby=properties/createdTimeUtc%20desc" not in uri:
        failures.append(
            "import: the existing-incident query has no createdTimeUtc ordering, so first() "
            "returns an arbitrary incident and the lookback is computed from the wrong one"
        )
    lookback = (flat.get("Determine_Lookback") or {}).get("inputs", "")
    if "ticks(utcNow())" not in str(lookback):
        failures.append(
            "import: Determine_Lookback no longer measures the gap since the newest incident; "
            "a fixed polling-interval multiple silently drops alarms when a run overruns it"
        )

    if "include_total_records" not in raw:
        failures.append(
            "import: the alarm request drops include_total_records; without it the API "
            "returns a flat array and total_pages never arrives"
        )


def check_sync(failures):
    actions = definition(SYNC)["actions"]
    flat = {name: action for _, name, action in walk(actions)}

    guard = flat.get("Check_SOCRadar_Write_Succeeded")
    if guard is None:
        failures.append("sync: Check_SOCRadar_Write_Succeeded is missing; the Synced tag is ungated")
    elif "Add_Synced_Tag" not in (guard.get("actions") or {}):
        failures.append("sync: Add_Synced_Tag is not inside Check_SOCRadar_Write_Succeeded")


def check_severity_writeback(failures):
    """Writing the Sentinel severity back to SOCRadar must be opt-in.

    Microsoft Sentinel has no Critical severity, so a CRITICAL alarm imports as High.
    Pushing that back downgraded the alarm on the customer's platform the moment an
    analyst closed the incident - observed live on alarm 103062317.
    """
    actions = definition(SYNC)["actions"]
    flat = {name: action for _, name, action in walk(actions)}
    gate = flat.get("Check_Severity_Sync_Enabled")
    if gate is None:
        failures.append(
            "sync: Update_SOCRadar_Severity is not behind Check_Severity_Sync_Enabled, so closing "
            "an incident silently rewrites the alarm severity on SOCRadar"
        )
        return
    if "Update_SOCRadar_Severity" not in (gate.get("actions") or {}):
        failures.append("sync: Update_SOCRadar_Severity is outside Check_Severity_Sync_Enabled")
    mapped = str((flat.get("Map_Severity") or {}).get("inputs", ""))
    if "'HIGH'" not in mapped:
        failures.append(
            "sync: Map_Severity no longer emits the upper-case severities SOCRadar stores"
        )


def check_both(failures):
    for label, path in (("import", IMPORT), ("sync", SYNC)):
        actions = definition(path)["actions"]
        if "Validate_Company_Arrays" not in actions:
            failures.append(f"{label}: Validate_Company_Arrays is missing")
        elif not runs_before(actions, "Validate_Company_Arrays", "Build_Company_Array"):
            failures.append(
                f"{label}: Build_Company_Array does not wait for Validate_Company_Arrays, so a "
                "CompanyIds/CompanyNames length mismatch crashes before the friendly error"
            )
        select = (actions.get("Build_Company_Array") or {}).get("inputs", {}).get("select", {})
        expression = select.get("CompanyName", "")
        for character in SANITISED_CHARS:
            if f"'{character}', ''" not in expression:
                failures.append(
                    f"{label}: Build_Company_Array does not strip {character!r} from CompanyName"
                )
        failures.extend(f"{label}: {problem}" for problem in dangling_run_after(actions))
        failures.extend(f"{label}: {problem}" for problem in mutation_under_parallel_foreach(actions))


def arm_references(node, inside_definition=False):
    """Collect parameters()/variables() names used at ARM scope.

    Anything under a Logic App's properties.definition is workflow scope, where
    parameters() resolves against the workflow's own parameters, not the template's.
    """
    used_parameters, used_variables = set(), set()
    if isinstance(node, dict):
        for key, value in node.items():
            arm = inside_definition or key == "definition"
            child_parameters, child_variables = arm_references(value, arm)
            used_parameters |= child_parameters
            used_variables |= child_variables
    elif isinstance(node, list):
        for value in node:
            child_parameters, child_variables = arm_references(value, inside_definition)
            used_parameters |= child_parameters
            used_variables |= child_variables
    elif isinstance(node, str) and not inside_definition:
        used_parameters |= set(re.findall(r"parameters\('([^']+)'\)", node))
        used_variables |= set(re.findall(r"variables\('([^']+)'\)", node))
    return used_parameters, used_variables


def check_self_contained(failures):
    """Each template must resolve on its own.

    The standalone sync playbook once fed its workflow from variables that only the
    one-click template declared, so deploying that playbook by itself failed with
    "The template variable 'dceResourceId' is not found" - invisible to a JSON parse
    and to ARM-TTK, visible only on a real deployment.
    """
    for path in [ROOT_TEMPLATE, IMPORT, SYNC]:
        template = load(path)
        used_parameters, used_variables = arm_references(template)
        declared_parameters = set(template.get("parameters", {}))
        declared_variables = set(template.get("variables", {}))
        label = os.path.relpath(path, ROOT)
        for name in sorted(used_parameters - declared_parameters):
            failures.append(f"{label}: uses parameters('{name}') at template scope but never declares it")
        for name in sorted(used_variables - declared_variables):
            failures.append(f"{label}: uses variables('{name}') at template scope but never declares it")


def check_audit_permissions(failures):
    """A playbook that writes to a DCR needs Monitoring Metrics Publisher on it.

    The sync playbook logs IncidentSynced rows, but nothing granted its managed
    identity that role, so every audit write came back 403 and failed the run.
    """
    PUBLISHER = "MonitoringMetricsPublisher"
    for label, path, writes_audit in (
        ("azuredeploy.json", ROOT_TEMPLATE, True),
        ("SOCRadar-MSSP-Alarm-Sync", SYNC, True),
    ):
        template = load(path)
        raw = json.dumps(template)
        if "AuditDcrImmutableId" not in raw:
            continue
        grants = [
            resource
            for resource in template.get("resources", [])
            if resource.get("type") == "Microsoft.Authorization/roleAssignments"
            and PUBLISHER in json.dumps(resource)
        ]
        wants_sync = "SyncPlaybookName" if path is ROOT_TEMPLATE else "PlaybookName"
        if not any(wants_sync in json.dumps(grant) for grant in grants):
            failures.append(
                f"{label}: the sync playbook writes audit rows but no role assignment grants its "
                "managed identity Monitoring Metrics Publisher on the audit DCR; every write 403s"
            )


def check_bounded_waits(failures):
    for path in [ROOT_TEMPLATE, IMPORT, SYNC]:
        with open(path) as handle:
            raw = handle.read()
        if '"PT1H"' in raw:
            failures.append(
                f"{os.path.relpath(path, ROOT)}: an hour-long loop timeout or retry interval is back"
            )


def check_no_drift(failures):
    """The one-click template embeds both playbooks; the copies must not diverge."""
    embedded = {}
    for resource in workflows(load(ROOT_TEMPLATE)):
        embedded[resource["name"]] = digest(resource["properties"]["definition"])
    for label, path in (("Import", IMPORT), ("Sync", SYNC)):
        standalone = digest(definition(path))
        if standalone not in embedded.values():
            failures.append(
                f"{label}: the standalone playbook does not match any workflow embedded in "
                "azuredeploy.json - one of the two was edited alone"
            )


def main():
    failures = []
    check_no_drift(failures)
    check_self_contained(failures)
    check_audit_permissions(failures)
    check_import(failures)
    check_sync(failures)
    check_severity_writeback(failures)
    check_both(failures)
    check_bounded_waits(failures)
    if failures:
        for failure in failures:
            print(f"  {failure}")
        print(f"{len(failures)} problem(s)")
        return 1
    print("No drift: the MSSP templates hold every checked invariant.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
