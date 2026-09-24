"""Demo: build tree-sitter queries from template strings in the target language.

Run with Python 3.14 or newer (template strings are a 3.14 feature)::

    python examples/template_queries.py

Requires the ``tests`` extra plus ``tree-sitter-hcl``::

    pip install tree-sitter-hcl tree-sitter-python tree-sitter-json
"""

import tree_sitter_hcl
import tree_sitter_json
import tree_sitter_python

from tree_sitter import Language
from tree_sitter.template import anything, capture, query

HCL = Language(tree_sitter_hcl.language())
PY = Language(tree_sitter_python.language())
JSON = Language(tree_sitter_json.language())


def banner(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


TERRAFORM = """
resource "aws_s3_bucket" "logs" {
  acl    = "private"
  region = "us-west-2"
}

resource "aws_s3_bucket" "public_assets" {
  acl = "public-read"
}

resource "aws_s3_bucket" "backups" {
  acl        = "private"
  versioning = true
}

resource "aws_iam_role" "runner" {
  acl = "private"
}
"""


def demo_hcl_basic():
    banner("1. Find every aws_s3_bucket, whatever else it contains")

    q = query(
        HCL,
        t"""
        resource "aws_s3_bucket" "{capture("name")}" {{
          {...}
        }}
        """,
    )
    print("generated query:\n")
    print(q.sexp)
    print("\nmatches:")
    for match in q.matches(TERRAFORM):
        print(f"  - {match.text('name')}")


def demo_hcl_constrained():
    banner("2. Only private buckets (an attribute pins the value)")

    q = query(
        HCL,
        t"""
        resource "aws_s3_bucket" "{capture("name")}" {{
          acl = "private"
          {...}
        }}
        """,
    )
    for match in q.matches(TERRAFORM):
        print(f"  - {match.text('name')}")


def demo_hcl_exact():
    banner("3. Exact match: no '...', so extra attributes are rejected")

    q = query(
        HCL,
        t"""
        resource "aws_s3_bucket" "{capture("name")}" {{
          acl = "public-read"
        }}
        """,
    )
    print("  (only the bucket whose *sole* attribute is acl=public-read)")
    for match in q.matches(TERRAFORM):
        print(f"  - {match.text('name')}")


def demo_hcl_regex():
    banner("4. Constrain a capture with a regex")

    # Putting the hole *inside* the quotes captures the string's contents
    # (aws_s3_bucket) rather than the quoted literal ("aws_s3_bucket"), which is
    # what makes an anchored pattern like ^aws_s3 behave as you'd expect.
    q = query(
        HCL,
        t"""
        resource "{capture("type", pattern="^aws_s3")}" "{capture("name")}" {{
          {...}
        }}
        """,
    )
    for match in q.matches(TERRAFORM):
        print(f"  - {match.text('type')}.{match.text('name')}")


PYTHON_SOURCE = """
def fetch(url):
    response = requests.get(url, timeout=5)
    return response.json()

def save(path, data):
    with open(path, "w") as fh:
        json.dump(data, fh)

def broken(url):
    response = requests.get(url)
    return response
"""


def demo_python():
    banner("5. A different language, same mechanism: find requests.get calls")

    q = query(PY, t"requests.get({capture('args')})")
    print("generated query:\n")
    print(q.sexp)
    print("\nmatches:")
    for match in q.matches(PYTHON_SOURCE):
        print(f"  - requests.get({match.text('args')})")


def demo_python_wildcard():
    banner("6. anything() matches one node without capturing it")

    q = query(PY, t"def {capture('fname')}({anything()}):\n    {...}")
    for match in q.matches(PYTHON_SOURCE):
        print(f"  - def {match.text('fname')}(...)")


JSON_SOURCE = """
{
  "name": "widget",
  "version": "2.1.0",
  "dependencies": {"left-pad": "^1.0.0"}
}
"""


def demo_json():
    banner("7. JSON: capture the value of a known key")

    q = query(JSON, t'{{"version": {capture("version")}, {...}}}')
    print("generated query:\n")
    print(q.sexp)
    print("\nmatches:")
    for match in q.matches(JSON_SOURCE):
        print(f"  - version = {match.text('version')}")


def main():
    demo_hcl_basic()
    demo_hcl_constrained()
    demo_hcl_exact()
    demo_hcl_regex()
    demo_python()
    demo_python_wildcard()
    demo_json()
    print()


if __name__ == "__main__":
    main()
