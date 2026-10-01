#!/usr/bin/env python3
"""Check each Analytic Rule and Hunting Query against the schema and metadata Azure enforces.

Three defects shipped in one rule and each needed a different depth to find:
a column that does not exist (only running the query shows it), a severity
literal in the wrong case (only the ingested data shows it), and a technique
with no matching tactic (only a real ARM PUT shows it). This gate reproduces
all three offline so the next one is caught before it is published.
"""
import glob
import json
import os
import re
import sys

import yaml

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "production")

# Columns Log Analytics adds to every custom table.
BUILTIN_COLUMNS = {
    "TimeGenerated", "Type", "TenantId", "_ResourceId", "_ItemId",
    "_BilledSize", "_IsBillable", "_SubscriptionId", "SourceSystem",
    "MG", "Computer", "RawData",
}

# KQL surface used by these rules. An identifier here is never a column.
KQL_WORDS = {
    "let", "where", "extend", "project", "summarize", "by", "and", "or", "not",
    "in", "has", "contains", "startswith", "endswith", "join", "on", "union",
    "take", "limit", "top", "sort", "order", "asc", "desc", "distinct",
    "count", "countif", "dcount", "sum", "avg", "min", "max", "make_set",
    "make_list", "ago", "now", "toscalar", "tostring", "toint", "tolong",
    "todouble", "todatetime", "todynamic", "round", "strcat", "extract",
    "extract_all", "split", "substring", "isnotempty", "isempty", "isnull",
    "isnotnull", "coalesce", "iff", "iif", "case", "bin", "parse_json",
    "array_length", "materialize", "datetime", "timespan", "true", "false",
    "d", "h", "m", "s", "print", "mv_expand", "evaluate", "narrow",
    "kind", "inner", "innerunique", "leftouter", "rightouter", "fullouter",
    "leftsemi", "rightsemi", "leftanti", "rightanti", "anti", "hint",
    "isfuzzy", "render", "timechart", "piechart", "columnchart", "percentile",
    "format_datetime", "datetime_diff", "between", "arg_max", "countif",
}

# Techniques used by this repo, mapped to their ATT&CK tactics. Azure rejects a
# rule whose technique has no matching tactic, with an error that names neither.
TECHNIQUE_TACTICS = {
    "T1078": {"DefenseEvasion", "Persistence", "PrivilegeEscalation", "InitialAccess"},
    "T1485": {"Impact"},
    "T1526": {"Discovery"},
    "T1567": {"Exfiltration"},
    "T1589": {"Reconnaissance"},
    "T1486": {"Impact"},
}

# SOCRadar sends risk levels uppercase; the import playbook passes them through
# unchanged (see its Map_Severity action). KQL string comparison is case
# sensitive, so a rule written in title case matches nothing and stays silent.
UPPERCASE_VALUE_COLUMNS = {"Severity", "Status"}


def custom_table_schemas():
    """Column names per custom table, read from the DCR stream declarations."""
    schemas = {}
    for path in [os.path.join(ROOT, "azuredeploy.json")] + sorted(
        glob.glob(os.path.join(ROOT, "Playbooks", "*", "azuredeploy.json"))
    ):
        with open(path) as handle:
            template = json.load(handle)
        variables = template.get("variables", {})
        for resource in template.get("resources", []):
            if resource.get("type") != "Microsoft.Insights/dataCollectionRules":
                continue
            declarations = resource["properties"].get("streamDeclarations", {})
            for stream, body in declarations.items():
                name = stream
                match = re.fullmatch(r"\[variables\('([^']+)'\)\]", stream)
                if match:
                    name = variables.get(match.group(1), stream)
                table = name.split("Custom-", 1)[-1]
                columns = {column["name"] for column in body["columns"]}
                schemas.setdefault(table, set()).update(columns)
    return schemas


def emitted_event_types():
    """Every EventType the playbooks write to the audit table, read from the templates."""
    found = set()

    def visit(node):
        if isinstance(node, dict):
            value = node.get("EventType")
            if isinstance(value, str):
                three_arg = re.findall(r"\)\s*,\s*'(\w+)'\s*,\s*'(\w+)'\s*\)", value)
                if three_arg:
                    found.update(name for pair in three_arg for name in pair)
                else:
                    found.add(value)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    for path in [os.path.join(ROOT, "azuredeploy.json")] + sorted(
        glob.glob(os.path.join(ROOT, "Playbooks", "*", "azuredeploy.json"))
    ):
        with open(path) as handle:
            visit(json.load(handle))
    return found


EVENT_LITERAL = re.compile(
    r'\bEventType\b\s*(?:(?:==|=~|!=|has|contains)\s*"([^"]*)"|in~?\s*\(([^)]*)\))'
)

# SecurityIncident is append-only: every change adds a row. The newest row per incident
# must be taken BEFORE any filter on Status or Labels, or a filter keeps the stale row
# (an incident closed, then labelled Synced, still matches "closed and not Synced").
INCIDENT_DEDUP = re.compile(
    r"SecurityIncident\s*(?:\|\s*where\s+TimeGenerated[^|]*)*"
    r"\|\s*summarize\s+arg_max\(TimeGenerated,\s*\*\)\s+by\s+IncidentNumber"
)


def strip_strings(query):
    """Blank out string literals so their contents are not read as identifiers."""
    query = re.sub(r'@?"(?:[^"\\]|\\.)*"', '""', query)
    return re.sub(r"@?'(?:[^'\\]|\\.)*'", "''", query)


def defined_in_query(query):
    """Names the query itself introduces, which need not exist in the table."""
    names = set(re.findall(r"\b(?:let|extend|project|summarize)\s+([A-Za-z_]\w*)\s*=", query))
    names |= set(re.findall(r",\s*([A-Za-z_]\w*)\s*=", query))
    names |= set(re.findall(r"\bby\s+([A-Za-z_]\w*)", query))
    return names


def string_literals_for(query, column):
    """Literals a query compares against the given column."""
    pattern = re.compile(
        rf"\b{column}\b\s*(==|=~|!=|in~?\s*\(|has|contains)\s*([^|\n]*)"
    )
    found = []
    for operator, tail in pattern.findall(query):
        case_insensitive = "~" in operator
        for literal in re.findall(r'"([^"]*)"', tail):
            found.append((literal, case_insensitive))
    return found


def query_problems(query, schemas, events):
    problems = []
    clean = strip_strings(query)

    if re.search(r"\bSecurityIncident\b", clean) and not INCIDENT_DEDUP.search(query):
        problems.append(
            "SecurityIncident is read without 'summarize arg_max(TimeGenerated, *) by IncidentNumber' "
            "ahead of any other filter; the table is append-only, so a stale row can match"
        )
    if "[SOCRadar]" in query:
        problems.append(
            'matches the title "[SOCRadar]", which this product never writes; '
            'its incident titles start with "[MSSP-<CompanyName>]"'
        )
    for single, many in EVENT_LITERAL.findall(query):
        for literal in ([single] if single else re.findall(r'"([^"]*)"', many)):
            if literal and literal not in events:
                problems.append(
                    f"EventType is compared to \"{literal}\", which no playbook writes "
                    f"(they write {sorted(events)})"
                )

    tables = [table for table in schemas if re.search(rf"\b{re.escape(table)}\b", clean)]
    if tables:
        known = set(BUILTIN_COLUMNS) | defined_in_query(clean) | set(tables) | KQL_WORDS
        for table in tables:
            known |= schemas[table]
        for identifier in sorted(set(re.findall(r"\b[A-Za-z_]\w*\b", clean))):
            if identifier in known or identifier.isdigit():
                continue
            problems.append(
                f"query uses '{identifier}', which is not a column of {'/'.join(tables)} "
                "and is not defined by the query"
            )
        for column in UPPERCASE_VALUE_COLUMNS & set().union(*(schemas[t] for t in tables)):
            for literal, case_insensitive in string_literals_for(query, column):
                if case_insensitive or not literal or literal.isdigit():
                    continue
                if literal != literal.upper():
                    problems.append(
                        f"'{column}' is compared to \"{literal}\" but SOCRadar sends "
                        f"\"{literal.upper()}\"; the rule would match nothing"
                    )
    return problems


def check_rule(path, schemas, events):
    with open(path) as handle:
        rule = yaml.safe_load(handle)
    problems = query_problems(rule.get("query", ""), schemas, events)

    tactics = set(rule.get("tactics", []))
    for technique in rule.get("relevantTechniques", []):
        expected = TECHNIQUE_TACTICS.get(technique)
        if expected is None:
            problems.append(
                f"{technique} has no tactic mapping here; add it to TECHNIQUE_TACTICS "
                "so this gate can check it"
            )
        elif not (expected & tactics):
            problems.append(
                f"{technique} needs one of {sorted(expected)} in tactics, which lists "
                f"{sorted(tactics)}; Azure rejects the rule on deployment"
            )
    return problems


KQL_FILE = os.path.join(ROOT, "socradar-kql-queries.kql")
BUILTIN_TABLES = ("SecurityIncident", "AzureDiagnostics")


def check_kql_file(schemas, events):
    """Each recipe in the .kql file, read the same way as a rule.

    Recipes are separated by blank lines. One that reads only the custom tables is checked
    in full. One that also reads a built-in table is checked for the SecurityIncident dedup
    and event names only: this gate has no schema for built-in columns.
    """
    with open(KQL_FILE) as handle:
        text = handle.read()
    problems = []
    checked = skipped = 0
    for block in re.split(r"\n\s*\n", text):
        query = "\n".join(line for line in block.splitlines() if not line.strip().startswith("//")).strip()
        if not query:
            continue
        label = next((l.strip("/ ").strip() for l in block.splitlines() if l.strip().startswith("//") and l.strip("/ -=")), query.splitlines()[0])
        mixed = any(re.search(rf"\b{name}\b", query) for name in BUILTIN_TABLES)
        if mixed:
            skipped += 1
            found = [p for p in query_problems(query, {}, events)]
        else:
            checked += 1
            found = query_problems(query, schemas, events)
        problems.extend(f"{label}: {problem}" for problem in found)
    return problems, checked, skipped


def main():
    schemas = custom_table_schemas()
    events = emitted_event_types()
    if not schemas:
        print("no DCR stream declarations found - the gate cannot check anything")
        return 1
    paths = sorted(glob.glob(os.path.join(ROOT, "Analytic Rules", "*.yaml")))
    paths += sorted(glob.glob(os.path.join(ROOT, "Hunting Queries", "*.yaml")))
    if not paths:
        print("no analytic rules or hunting queries found")
        return 1
    failed = False
    for path in paths:
        name = os.path.basename(path)
        problems = check_rule(path, schemas, events)
        if problems:
            failed = True
            print(f"{name}:")
            for problem in problems:
                print(f"  {problem}")
        else:
            print(f"{name}: ok")
    kql_problems, kql_checked, kql_skipped = check_kql_file(schemas, events)
    if kql_problems:
        failed = True
        print("socradar-kql-queries.kql:")
        for problem in kql_problems:
            print(f"  {problem}")
    else:
        print(f"socradar-kql-queries.kql: ok ({kql_checked} recipe(s) column-checked, "
              f"{kql_skipped} read a built-in table and were checked for dedup and event names only)")
    if failed:
        return 1
    print(f"{len(paths)} rule(s) and hunting query/queries checked against {len(schemas)} custom table schema(s).")
    print("Built-in tables such as SecurityIncident are not column-checked here.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
