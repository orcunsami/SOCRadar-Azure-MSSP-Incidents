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

    # SOCRadar answers HTTP 200 with is_success:false when it refuses the change. The
    # "synced" audit row must follow the success branch only, and the refusal must leave
    # a row of its own, or the run is green and the failure leaves no trace.
    marker = "/Check_SOCRadar_Write_Succeeded/"
    synced = [path for path, _, action in walk(actions) if "IncidentSynced" in json.dumps(action.get("inputs", ""))]
    rejected = [path for path, _, action in walk(actions) if "SyncWriteRejected" in json.dumps(action.get("inputs", ""))]
    if not synced or any(marker not in path or f"{marker}else" in path for path in synced):
        failures.append(
            "sync: the IncidentSynced audit row is not confined to the success branch of "
            "Check_SOCRadar_Write_Succeeded, so it is written when SOCRadar refused the change"
        )
    if not rejected or any(f"{marker}else" not in path for path in rejected):
        failures.append(
            "sync: the else branch of Check_SOCRadar_Write_Succeeded does not write a "
            "SyncWriteRejected audit row, so a refused status change is silent"
        )


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


def stream_columns(template):
    """Declared columns per DCR stream, keyed by the parameter or variable that names it."""
    variables = template.get("variables", {})
    columns = {}
    for resource in template.get("resources", []):
        if resource.get("type") != "Microsoft.Insights/dataCollectionRules":
            continue
        for stream, body in resource["properties"].get("streamDeclarations", {}).items():
            match = re.fullmatch(r"\[variables\('([^']+)'\)\]", stream)
            key = match.group(1) if match else stream
            columns[key] = {column["name"] for column in body["columns"]}
    return columns


def check_audit_columns(failures):
    """A DCR drops a column it did not declare, silently (EXP-AZURE-0138).

    Every audit row the playbooks send must use only the columns of the audit stream.
    The Sync playbook has no DCR of its own, so it is read against the root template's.
    """
    # the audit stream is the declared one that carries an EventType column
    audit = next(
        (cols for cols in stream_columns(load(ROOT_TEMPLATE)).values() if "EventType" in cols), None
    )
    if not audit:
        failures.append("audit columns: no audit stream declaration found in azuredeploy.json")
        return
    for label, path in (("root", ROOT_TEMPLATE), ("import", IMPORT), ("sync", SYNC)):
        for workflow in workflows(load(path)):
            for _, name, action in walk(workflow["properties"]["definition"]["actions"]):
                inputs = action.get("inputs")
                if not isinstance(inputs, dict) or "AuditStreamName" not in str(inputs.get("uri", "")):
                    continue
                for row in inputs.get("body", []):
                    extra = sorted(set(row) - audit)
                    if extra:
                        failures.append(
                            f"{label}: {name} sends {extra} to the audit table, which the DCR "
                            "does not declare and would drop silently"
                        )


DOC_FILES = ["README.md", "PUBLISHING.md", "CUSTOMER-TEST-RUNBOOK-MSSP.md", os.path.join("production", "README.md")]
# The incident title is `[MSSP-<CompanyName>] #<id> ...` and the label is `MSSP-<CompanyName>`.
# The single-customer product's `[<CompanyName>]` / `Company:<name>` wording is not this product's.
STALE_TAGGING = re.compile(r"Company:\s?<|\[<CompanyName>\]")


def check_tagging_wording(failures):
    repo = os.path.dirname(ROOT)
    paths = [os.path.join(repo, name) for name in DOC_FILES] + [ROOT_TEMPLATE, IMPORT, SYNC]
    for path in paths:
        with open(path) as handle:
            for number, line in enumerate(handle, 1):
                if STALE_TAGGING.search(line):
                    failures.append(
                        f"{os.path.relpath(path, repo)}:{number}: describes the title or tag as "
                        "`[<CompanyName>]` / `Company:<name>`; the code writes `[MSSP-<CompanyName>]` "
                        "and `MSSP-<CompanyName>`"
                    )


def check_outputs(failures):
    """An output must be gated by the same condition as the resource it names."""
    template = load(ROOT_TEMPLATE)
    conditions = {}
    for resource in template["resources"]:
        condition = resource.get("condition")
        if condition:
            conditions[resource["type"]] = condition
    wanted = {
        "dceName": "Microsoft.Insights/dataCollectionEndpoints",
        "workbookName": "Microsoft.Insights/workbooks",
    }
    for output, resource_type in wanted.items():
        condition = conditions.get(resource_type, "")
        inner = condition[1:-1] if condition.startswith("[") else condition
        value = template["outputs"].get(output, {}).get("value", "")
        if not inner or inner not in value:
            failures.append(
                f"outputs: {output} is not gated by the condition of its resource ({resource_type}), "
                "so it names something that was not deployed"
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


def split_nested(node):
    """Separate a nested deployment's inner template from its parent's scope.

    A Microsoft.Resources/deployments resource with expressionEvaluationOptions
    scope 'inner' carries a whole template under properties.template, which
    declares its own parameters. Reading it as parent scope reports every inner
    parameter as undeclared. The value bindings next to it
    (properties.parameters) DO belong to the parent, so only the template itself
    is split out.

    Scope 'outer' is the opposite and must NOT be split: ARM evaluates that inner
    template's expressions in the parent, which is how deploy-workspace-roles
    reaches the parent's variables. Splitting it reported five false failures.
    """
    inner = []
    if isinstance(node, dict):
        stripped = {}
        for key, value in node.items():
            if (key == "properties" and isinstance(value, dict)
                    and isinstance(value.get("template"), dict)
                    and str(node.get("type", "")).lower() == "microsoft.resources/deployments"
                    and value.get("expressionEvaluationOptions", {}).get("scope") == "inner"):
                child = dict(value)
                inner.append(child.pop("template"))
                child_stripped, child_inner = split_nested(child)
                inner += child_inner
                stripped[key] = child_stripped
                continue
            child_stripped, child_inner = split_nested(value)
            stripped[key] = child_stripped
            inner += child_inner
        return stripped, inner
    if isinstance(node, list):
        out = []
        for value in node:
            child_stripped, child_inner = split_nested(value)
            out.append(child_stripped)
            inner += child_inner
        return out, inner
    return node, inner


def check_self_contained(failures):
    """Each template must resolve on its own.

    The standalone sync playbook once fed its workflow from variables that only the
    one-click template declared, so deploying that playbook by itself failed with
    "The template variable 'dceResourceId' is not found" - invisible to a JSON parse
    and to ARM-TTK, visible only on a real deployment.
    """
    for path in [ROOT_TEMPLATE, IMPORT, SYNC]:
        template = load(path)
        label = os.path.relpath(path, ROOT)
        outer, nested = split_nested(template)
        scopes = [(label, outer)]
        for index, inner in enumerate(nested):
            scopes.append(("%s (nested template %d)" % (label, index + 1), inner))
        for scope_label, scope in scopes:
            used_parameters, used_variables = arm_references(scope)
            declared_parameters = set(scope.get("parameters", {}))
            declared_variables = set(scope.get("variables", {}))
            for name in sorted(used_parameters - declared_parameters):
                failures.append(f"{scope_label}: uses parameters('{name}') at template scope but never declares it")
            for name in sorted(used_variables - declared_variables):
                failures.append(f"{scope_label}: uses variables('{name}') at template scope but never declares it")


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


GUARD = "precheck-workspace-exists"
EXTERNAL = "precheck-external-workspace"


def check_workspace_guard(failures):
    """EXP-AZURE-0198: a condition:false resource is a satisfied dependency in ARM.

    Depending on the Workspace resource does not stop a deployment whose
    WorkspaceName does not exist -- it creates the connection, both Logic Apps, the
    workbook and the endpoint and only then fails, leaving an enabled Logic App
    behind. The guard is a nested deployment that resolves the workspace before
    anything else runs, and every other resource waits on it.

    Both branches are asserted, because the guard is SKIPPED on the greenfield path
    that every E2E uses: a wrong api-version or field name inside it would leave the
    tests green and fail only at a customer.
    """
    template = load(ROOT_TEMPLATE)
    resources = template["resources"]
    by_name = {r.get("name"): r for r in resources}

    # The default is false: the template does not update a workspace that already exists, so a
    # commitment tier can never be touched and a missing name fails before anything is created.
    # Creating a workspace is opt-in.
    toggle = template.get("parameters", {}).get("DeployNewWorkspace", {})
    if toggle.get("defaultValue") is not False:
        failures.append("azuredeploy.json: DeployNewWorkspace must default to false")

    for name, condition in ((GUARD, "[not(variables('deployWorkspace'))]"),
                            (EXTERNAL, "[variables('isExternalWorkspace')]")):
        resource = by_name.get(name)
        if resource is None:
            failures.append(
                f"azuredeploy.json: {name} is gone - a wrong WorkspaceName half-installs again"
            )
            continue
        if resource.get("type") != "Microsoft.Resources/deployments":
            failures.append(f"azuredeploy.json: {name} is not a nested deployment")
        if resource.get("condition") != condition:
            failures.append(
                f"azuredeploy.json: {name} condition is {resource.get('condition')!r}, "
                f"expected {condition!r}"
            )
        properties = resource.get("properties", {})
        scope = properties.get("expressionEvaluationOptions", {}).get("scope")
        if scope != "inner":
            # With outer scope the reference() is evaluated in the parent template,
            # where a missing resource is a template error rather than the runtime
            # failure this guard depends on.
            failures.append(
                f"azuredeploy.json: {name} evaluates expressions at {scope!r} scope, expected 'inner'"
            )
        outputs = properties.get("template", {}).get("outputs", {})
        if not outputs:
            failures.append(f"azuredeploy.json: {name} has no output, so it asserts nothing")
        for key, output in outputs.items():
            value = str(output.get("value", ""))
            if "reference(" not in value:
                # resourceId() alone is string arithmetic: it succeeds for a workspace
                # that does not exist.
                failures.append(
                    f"azuredeploy.json: {name} output {key} does not call reference(), "
                    "so it cannot detect a missing resource"
                )

    if GUARD not in by_name:
        return
    guard_ref = f"[resourceId('Microsoft.Resources/deployments', '{GUARD}')]"
    external_ref = f"[resourceId('Microsoft.Resources/deployments', '{EXTERNAL}')]"
    for resource in resources:
        name = resource.get("name")
        if name in (GUARD, EXTERNAL):
            continue
        deps = [str(d) for d in (resource.get("dependsOn") or [])]
        if resource.get("type", "").lower() == "microsoft.operationalinsights/workspaces":
            # The guard exists because this one may not be created; depending on it
            # would be a cycle.
            if guard_ref in deps:
                failures.append("azuredeploy.json: the Workspace resource depends on its own guard")
            continue
        if guard_ref not in deps:
            failures.append(
                f"azuredeploy.json: {name} does not wait on {GUARD} - it would be created "
                "before the workspace is known to exist"
            )
        if external_ref not in deps:
            failures.append(
                f"azuredeploy.json: {name} does not wait on {EXTERNAL} - a cross-RG deployment "
                "onto a workspace without Microsoft Sentinel would create it and then fail"
            )

    # EXP-AZURE-0160 belongs in CI too, not only in the live canary script.
    for resource in resources:
        if resource.get("type", "").lower() == "microsoft.operationalinsights/workspaces":
            if resource.get("properties"):
                failures.append(
                    "azuredeploy.json: the Workspace resource declares properties again - "
                    "a redeploy can rewrite an existing workspace's pricing tier or retention"
                )
            if resource.get("condition") != "[variables('deployWorkspace')]":
                failures.append(
                    "azuredeploy.json: the Workspace condition is "
                    f"{resource.get('condition')!r}, expected \"[variables('deployWorkspace')]\""
                )


def check_deploy_links(failures):
    """Every raw.githubusercontent path in a tracked markdown file must exist here.

    production/README.md shipped a Deploy to Azure button pointing at
    master/azuredeploy.json while the template lives at
    master/production/azuredeploy.json. Measured 8 Sep 2026: that URL returned 404,
    so the button did nothing for anyone who clicked it in that README. The root
    README had the right path, which is why nobody noticed.

    Checked against the repository contents rather than over the network, so it
    works in CI and fails on the commit that breaks the path rather than after it
    is published.
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pattern = re.compile(
        r"raw\.githubusercontent\.com(?:%2F|/)[^/%]+(?:%2F|/)[^/%]+(?:%2F|/)master(?:%2F|/)([A-Za-z0-9._%/-]+)"
    )
    seen = 0
    for directory, _, names in os.walk(repo):
        if ".git" in directory.split(os.sep):
            continue
        for name in names:
            if not name.endswith(".md"):
                continue
            path = os.path.join(directory, name)
            with open(path) as handle:
                raw = handle.read()
            for match in pattern.finditer(raw):
                target = match.group(1).replace("%2F", "/").rstrip(")")
                seen += 1
                if not os.path.exists(os.path.join(repo, target)):
                    failures.append(
                        f"{os.path.relpath(path, repo)}: links to master/{target}, "
                        "which does not exist in this repository - the Deploy button 404s"
                    )
    if not seen:
        # A pattern that matches nothing would make this check silently vacuous.
        failures.append("check_deploy_links found no raw.githubusercontent link to check")


def check_external_workspace_features(failures):
    """Cross-RG must switch the custom-table features off, not reference them.

    The DCE and both DCRs are conditioned on not(isExternalWorkspace), so a
    cross-RG deployment does not create them -- while the Logic Apps used to pass
    reference(<dce>) unconditionally. Measured 8 Sep 2026: a cross-RG deployment
    with Microsoft Sentinel correctly onboarded and both prechecks Succeeded still
    died with ResourceNotFound on SOCRadar-MSSP-DCE. The README advertises cross-RG.

    Also asserts that the DCE is created for audit logging on its own. It is the
    ingestion endpoint for BOTH DCRs, but was conditioned on the alarms table
    alone, so EnableAuditLogging=true with EnableAlarmsTable=false referenced a
    DCE that no branch creates.
    """
    template = load(ROOT_TEMPLATE)

    endpoints = [r for r in template["resources"]
                 if r.get("type", "").endswith("dataCollectionEndpoints")]
    if len(endpoints) != 1:
        failures.append("azuredeploy.json: expected exactly one data collection endpoint, "
                        "found %d" % len(endpoints))
    for endpoint in endpoints:
        condition = str(endpoint.get("condition") or "")
        if "EnableAuditLogging" not in condition or "EnableAlarmsTable" not in condition:
            failures.append(
                "azuredeploy.json: the DCE condition is %r - it is the ingestion endpoint for "
                "both DCRs, so either feature must create it" % condition
            )
        if "isExternalWorkspace" not in condition:
            failures.append("azuredeploy.json: the DCE condition no longer excludes a cross-RG workspace")

    for resource in template["resources"]:
        if resource.get("type") != "Microsoft.Logic/workflows":
            continue
        name = str(resource.get("name"))
        for key, value in resource["properties"].get("parameters", {}).items():
            raw = value.get("value")
            if not isinstance(raw, str):
                continue
            if "reference(" in raw and "isExternalWorkspace" not in raw:
                failures.append(
                    "%s parameter %s calls reference() without excluding a cross-RG workspace: "
                    "the resource is not created in that mode" % (name, key)
                )
            if key in ("EnableAuditLogging", "EnableAlarmsTable") and "isExternalWorkspace" not in raw:
                failures.append(
                    "%s parameter %s is passed straight through: cross-RG has no custom tables, "
                    "so the playbook would try to write to a DCR that does not exist" % (name, key)
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
    check_audit_columns(failures)
    check_tagging_wording(failures)
    check_outputs(failures)
    check_bounded_waits(failures)
    check_workspace_guard(failures)
    check_deploy_links(failures)
    check_external_workspace_features(failures)
    if failures:
        for failure in failures:
            print(f"  {failure}")
        print(f"{len(failures)} problem(s)")
        return 1
    print("No drift: the MSSP templates hold every checked invariant.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
