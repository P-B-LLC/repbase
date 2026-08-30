"""Check that responses match the contract that describes them.

Two bugs in one afternoon came from the schema and the server disagreeing
about a type, and neither was visible from either side alone:

- ``SavedMealResult.meal`` was documented as a string and returned the
  integer primary key, so the generated Swift client refused to decode its
  own contract and saving a meal from a post failed outright.
- ``Gym.created_by`` and ``Exercise.created_by`` were documented as
  non-nullable integers and answer ``null`` for anything the app shipped
  with, so gym search and exercise lookup could not decode at all.

Both are the same shape of mistake: drf-spectacular describes what it can
infer from the serializer, which is not always what the view returns. The
only way to catch it is to compare a real response against the real schema,
which is what this does.

Used from two places. ``core.tests`` runs it over the test client on data
built to be awkward on purpose, which is the version CI runs. ``Scripts/
validate-contract.py`` runs it against a live server, where the data has
shapes no fixture thought to produce.
"""

from __future__ import annotations

from typing import Any, Iterable

from jsonschema import Draft202012Validator


def to_json_schema(node: Any) -> Any:
    """Rewrite OpenAPI 3.0 ``nullable`` as something JSON Schema understands.

    ``nullable: true`` is an OpenAPI 3.0 keyword, not a JSON Schema one, so a
    validator ignores it and reports every null field as a type error. The
    first run of this check produced twenty such false positives and hid the
    five real findings among them.
    """
    if isinstance(node, dict):
        out = {k: to_json_schema(v) for k, v in node.items() if k != "nullable"}
        if node.get("nullable"):
            if "type" in out:
                declared = out["type"]
                out["type"] = (
                    [declared, "null"]
                    if isinstance(declared, str)
                    else list(declared) + ["null"]
                )
            elif "allOf" in out or "$ref" in out:
                # A nullable reference cannot take a "null" type beside it, so
                # it becomes a choice between the reference and null.
                referenced = {k: v for k, v in out.items() if k in ("allOf", "$ref")}
                for key in ("allOf", "$ref"):
                    out.pop(key, None)
                out["anyOf"] = [referenced, {"type": "null"}]
        return out
    if isinstance(node, list):
        return [to_json_schema(v) for v in node]
    return node


def collection_paths(spec: dict) -> list[str]:
    """Every GET path that needs no path parameter to reach.

    Detail routes are left out rather than guessed at: inventing an id
    produces a 404, and a 404 body says nothing about whether the 200 body
    would have matched.
    """
    return sorted(
        path
        for path, operations in spec.get("paths", {}).items()
        if "get" in operations and "{" not in path
    )


def response_schema(spec: dict, path: str) -> dict | None:
    """The JSON schema a GET on `path` promises for a 200, if it declares one."""
    try:
        declared = spec["paths"][path]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
    except (KeyError, TypeError):
        return None
    full = dict(declared)
    full["components"] = {"schemas": spec.get("components", {}).get("schemas", {})}
    return full


def violations(schema: dict, body: Any, limit: int = 4) -> list[str]:
    """Where `body` disagrees with `schema`, most structural first.

    Capped per endpoint: one wrong field in a list of forty rows is forty
    identical complaints, and the second one adds nothing.
    """
    found = sorted(
        Draft202012Validator(schema).iter_errors(body),
        key=lambda error: list(error.path),
    )
    seen: set[str] = set()
    lines: list[str] = []
    for error in found:
        where = "/".join(str(part) for part in error.path) or "(root)"
        # Collapse the row index, so results/0/x and results/7/x read as one.
        signature = "/".join(
            part for part in where.split("/") if not part.isdigit()
        ) + error.message
        if signature in seen:
            continue
        seen.add(signature)
        lines.append(f"{where}: {error.message}")
        if len(lines) >= limit:
            break
    return lines


def check(spec: dict, fetch, paths: Iterable[str] | None = None) -> list[str]:
    """Validate every collection response `fetch` can return.

    `fetch(path)` returns the decoded body, or None to skip the path -- the
    two callers reach the server differently, and neither should have to know
    how the other does it.
    """
    spec = to_json_schema(spec)
    problems: list[str] = []
    for path in paths if paths is not None else collection_paths(spec):
        schema = response_schema(spec, path)
        if schema is None:
            continue
        body = fetch(path)
        if body is None:
            continue
        for line in violations(schema, body):
            problems.append(f"{path} -> {line}")
    return problems
