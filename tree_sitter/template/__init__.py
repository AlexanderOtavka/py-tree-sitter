"""Build tree-sitter queries from template strings written in the target language.

Instead of writing a query as an S-expression, write an *example* of the code you
want to match as a PEP 750 template string, and mark the interesting parts with
interpolations:

.. code-block:: python

    from tree_sitter import Language
    from tree_sitter.template import query, capture
    import tree_sitter_hcl

    HCL = Language(tree_sitter_hcl.language())

    q = query(HCL, t'''
        resource "aws_s3_bucket" {capture("name")} {{
          acl = "private"
          {...}
        }}
    ''')

    for match in q.matches(terraform_source):
        print(match.text("name"))

The template is parsed with the very same grammar the query will run against, so
there is no per-language translation logic: whatever the grammar can parse, you
can write a query in.

Interpolations
--------------

=============================  ==============================================
``capture("name")``            capture the node at this position as ``@name``
``capture("n", "string_lit")`` capture, requiring a node type
``capture("n", pattern=r"^a")`` capture, constrained by a regular expression
``capture("n", one_of=[...])`` capture, constrained to literal alternatives
``anything()``                 match one node of any type, without capturing
``...``                        allow additional unmatched siblings here
``"some string"``              splice literal source text into the template
=============================  ==============================================

By default a template matches *exactly*: a block with one attribute will not
match a block with two. Use ``...`` wherever extra children should be tolerated.

Note
----
Template strings require Python 3.14 or newer.
"""

from ._edits import Edits, OverlappingEditError
from ._errors import TemplateCompileError, TemplateError, TemplateSyntaxError
from ._holes import (
    Alternatives,
    AnyChildren,
    Capture,
    Hole,
    Wildcard,
    anything,
    capture,
)
from ._query import Match, TemplateQuery, query

__all__ = [
    # Entry point
    "query",
    "TemplateQuery",
    "Match",
    # Hole constructors
    "capture",
    "anything",
    # Hole types
    "Hole",
    "Capture",
    "Wildcard",
    "AnyChildren",
    "Alternatives",
    # Source rewriting
    "Edits",
    # Errors
    "TemplateError",
    "TemplateSyntaxError",
    "TemplateCompileError",
    "OverlappingEditError",
]
