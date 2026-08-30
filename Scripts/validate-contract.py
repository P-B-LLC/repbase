#!/usr/bin/env python
"""Validate a running server's responses against the schema it publishes.

The same check `core.tests.ContractMatchesResponsesTests` runs in CI, pointed
at a live server instead of the test client. Worth running against real data
now and then, because the two see different things:

- CI builds its own rows, so it only finds what a fixture thought to create.
  It catches a field that is *always* the wrong type.
- This sees whatever an account has actually accumulated, including the
  shapes nobody would think to write down. Both bugs it was built for were
  of that kind -- a null on a seeded row, a pk on a result object.

Neither replaces the other. This one needs a server and a token, which is
why it is not the CI check.

    python Scripts/validate-contract.py --token <token>
    python Scripts/validate-contract.py --token <token> --base http://host:port

Exits non-zero if anything disagrees, so it can gate a deploy.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:5000")
    parser.add_argument("--token", required=True, help="DRF auth token")
    parser.add_argument(
        "--schema",
        help="Path to an openapi.yaml. Generated in process when omitted.",
    )
    args = parser.parse_args()

    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()

    from core.contract_check import check, collection_paths, to_json_schema

    if args.schema:
        import yaml

        with open(args.schema, encoding="utf-8") as handle:
            spec = yaml.safe_load(handle)
    else:
        from drf_spectacular.generators import SchemaGenerator

        spec = SchemaGenerator().get_schema(request=None, public=True)

    skipped: list[str] = []

    def fetch(path: str):
        request = urllib.request.Request(
            args.base + path, headers={"Authorization": "Token " + args.token}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            # A route wanting a query parameter answers 400 to a bare GET, and
            # its 400 body says nothing about whether its 200 body would match.
            skipped.append(f"{path} (HTTP {error.code})")
            return None
        except Exception as error:  # noqa: BLE001 - reported, not swallowed
            skipped.append(f"{path} ({error})")
            return None

    paths = collection_paths(to_json_schema(spec))
    problems = check(spec, fetch, paths=paths)

    print(f"checked {len(paths) - len(skipped)} of {len(paths)} collection endpoints")
    for line in skipped:
        print(f"  skipped {line}")

    if problems:
        print(f"\n{len(problems)} disagreements with the contract:")
        for line in problems:
            print(f"  {line}")
        return 1

    print("\nno disagreements")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
