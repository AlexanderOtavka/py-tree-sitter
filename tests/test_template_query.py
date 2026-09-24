"""Integration tests for the public ``tree_sitter.template`` API.

These exercise ``query()`` / ``TemplateQuery`` / ``Match`` end to end against real
grammars. Renderer and compiler internals are covered by ``test_template_render``
and ``test_template_compile``; nothing here reaches below the public surface.
"""

import unittest
from unittest import TestCase

import tree_sitter_hcl
import tree_sitter_javascript
import tree_sitter_json
import tree_sitter_python

from tree_sitter import Language, Node, Parser
from tree_sitter.template import (
    TemplateCompileError,
    TemplateError,
    TemplateSyntaxError,
    anything,
    capture,
    query,
)

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

JSON_SOURCE = """
{
  "name": "widget",
  "version": "2.1.0",
  "dependencies": {"left-pad": "^1.0.0"}
}
"""

JS_SOURCE = """
app.get("/users", listUsers);
app.post("/users", createUser);
app.get("/health", ping);
"""


class TemplateQueryTestBase(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hcl = Language(tree_sitter_hcl.language())
        cls.python = Language(tree_sitter_python.language())
        cls.json = Language(tree_sitter_json.language())
        cls.javascript = Language(tree_sitter_javascript.language())

    def texts(self, matches, name):
        """Capture texts for ``name``, one entry per match."""
        return [match.text(name) for match in matches]


class TestCoreBehaviour(TemplateQueryTestBase):
    def test_hcl_finds_every_bucket_and_captures_inner_text(self):
        # The hole sits *inside* the quotes, so it binds to the string's contents
        # rather than to the quoted literal: "logs" -> logs.
        q = query(
            self.hcl,
            t"""
            resource "aws_s3_bucket" "{capture("name")}" {{
              {...}
            }}
            """,
        )
        matches = q.matches(TERRAFORM)
        self.assertEqual(["logs", "public_assets", "backups"], self.texts(matches, "name"))

    def test_hole_outside_quotes_captures_the_quoted_literal(self):
        q = query(
            self.hcl,
            t"""
            resource "aws_s3_bucket" {capture("name")} {{
              {...}
            }}
            """,
        )
        matches = q.matches(TERRAFORM)
        self.assertEqual(
            ['"logs"', '"public_assets"', '"backups"'],
            self.texts(matches, "name"),
        )

    def test_exact_by_default_extra_attributes_are_rejected(self):
        # The single most important guarantee: a one-attribute template means
        # "a block whose only attribute is this one".
        exact = query(
            self.hcl,
            t"""
            resource "aws_s3_bucket" "{capture("name")}" {{
              acl = "private"
            }}
            """,
        )
        # "logs" and "backups" are private but each carries a second attribute.
        self.assertEqual([], exact.matches(TERRAFORM))

    def test_ellipsis_relaxes_exactness(self):
        relaxed = query(
            self.hcl,
            t"""
            resource "aws_s3_bucket" "{capture("name")}" {{
              acl = "private"
              {...}
            }}
            """,
        )
        self.assertEqual(["logs", "backups"], self.texts(relaxed.matches(TERRAFORM), "name"))

    def test_exact_template_matches_the_sole_attribute_block(self):
        q = query(
            self.hcl,
            t"""
            resource "aws_s3_bucket" "{capture("name")}" {{
              acl = "public-read"
            }}
            """,
        )
        self.assertEqual(["public_assets"], self.texts(q.matches(TERRAFORM), "name"))

    def test_literal_attribute_value_is_pinned_with_eq(self):
        # acl = "private" must not match acl = "public-read".
        q = query(
            self.hcl,
            t"""
            resource "aws_s3_bucket" "{capture("name")}" {{
              acl = "private"
              {...}
            }}
            """,
        )
        names = self.texts(q.matches(TERRAFORM), "name")
        self.assertEqual(["logs", "backups"], names)
        self.assertNotIn("public_assets", names)

    def test_literal_block_type_is_pinned_with_eq(self):
        q = query(
            self.hcl,
            t"""
            resource "aws_iam_role" "{capture("name")}" {{
              {...}
            }}
            """,
        )
        self.assertEqual(["runner"], self.texts(q.matches(TERRAFORM), "name"))

    def test_anonymous_nodes_are_significant(self):
        # x + y and x - y differ *only* in an anonymous node, so a "+" template
        # must not match subtraction.
        source = "a = x + y\nb = x - y\nc = p + q\n"
        q = query(self.python, t"{capture('l')} + {capture('r')}")
        matches = q.matches(source)
        self.assertEqual([("x", "y"), ("p", "q")], [(m.text("l"), m.text("r")) for m in matches])

    def test_argument_count_is_exact(self):
        # Inter-child anchors in the argument_list make the arity exact:
        # requests.get(url) matches, requests.get(url, timeout=5) does not.
        q = query(self.python, t"requests.get({capture('a')})")
        matches = q.matches(PYTHON_SOURCE)
        self.assertEqual(1, len(matches))
        self.assertEqual("url", matches[0].text("a"))
        # ...and it is the one-argument call inside broken(), not fetch().
        self.assertEqual(10, matches[0]["a"].start_point.row)

    def test_ellipsis_admits_the_extra_argument(self):
        # `{...}` relaxes only the *count* of siblings, not the position of the
        # holes the user wrote. Dropping the orphaned "," would leave `(_) @a`
        # free to float onto any argument, so the anchors are kept and only the
        # gap the hole vacated is opened:
        #     (argument_list . "(" . (_) @a ")" .)
        # @a therefore stays pinned to the first argument -- exactly one match per
        # call site -- while the trailing gap admits the extra `timeout=5`.
        q = query(self.python, t"requests.get({capture('a')}, {...})")
        matches = q.matches(PYTHON_SOURCE)
        # One match per call site, each capturing the first argument.
        self.assertEqual(["url", "url"], [m.text("a") for m in matches])
        self.assertEqual([2, 10], sorted(m["a"].start_point.row for m in matches))


class TestHoles(TemplateQueryTestBase):
    def test_bare_capture(self):
        q = query(self.python, t"json.dump({capture('data')}, {capture('fh')})")
        matches = q.matches(PYTHON_SOURCE)
        self.assertEqual(1, len(matches))
        self.assertEqual("data", matches[0].text("data"))
        self.assertEqual("fh", matches[0].text("fh"))

    def test_capture_with_kind_constrains_the_node_type(self):
        q = query(
            self.hcl,
            t"""
            resource "aws_s3_bucket" {capture("name", "string_lit")} {{
              {...}
            }}
            """,
        )
        self.assertEqual(
            ['"logs"', '"public_assets"', '"backups"'],
            self.texts(q.matches(TERRAFORM), "name"),
        )

    def test_capture_with_wrong_kind_matches_nothing(self):
        q = query(
            self.hcl,
            t"""
            resource "aws_s3_bucket" {capture("name", "identifier")} {{
              {...}
            }}
            """,
        )
        self.assertEqual([], q.matches(TERRAFORM))

    def test_capture_with_pattern(self):
        # The hole goes inside the quotes so the captured text is the string
        # contents; otherwise a ^-anchored regex would be tested against the
        # leading double quote and never match.
        q = query(
            self.hcl,
            t"""
            resource "{capture("type", pattern="^aws_s3")}" "{capture("name")}" {{
              {...}
            }}
            """,
        )
        matches = q.matches(TERRAFORM)
        self.assertEqual(
            [
                ("aws_s3_bucket", "logs"),
                ("aws_s3_bucket", "public_assets"),
                ("aws_s3_bucket", "backups"),
            ],
            [(m.text("type"), m.text("name")) for m in matches],
        )

    def test_capture_with_pattern_selecting_the_other_resource_type(self):
        q = query(
            self.hcl,
            t"""
            resource "{capture("type", pattern="^aws_iam")}" "{capture("name")}" {{
              {...}
            }}
            """,
        )
        matches = q.matches(TERRAFORM)
        self.assertEqual(
            [("aws_iam_role", "runner")], [(m.text("type"), m.text("name")) for m in matches]
        )

    def test_anchored_pattern_against_quoted_literal_matches_nothing(self):
        # Documents the quoting subtlety from the other direction: with the hole
        # outside the quotes the captured text starts with '"', so ^aws_s3 fails.
        q = query(
            self.hcl,
            t"""
            resource {capture("type", pattern="^aws_s3")} "{capture("name")}" {{
              {...}
            }}
            """,
        )
        self.assertEqual([], q.matches(TERRAFORM))

    def test_capture_with_one_of(self):
        q = query(
            self.hcl,
            t"""
            resource "aws_s3_bucket" "{capture("name", one_of=["logs", "backups"])}" {{
              {...}
            }}
            """,
        )
        self.assertEqual(["logs", "backups"], self.texts(q.matches(TERRAFORM), "name"))

    def test_one_of_with_no_matching_alternative(self):
        q = query(
            self.hcl,
            t"""
            resource "aws_s3_bucket" "{capture("name", one_of=["nope"])}" {{
              {...}
            }}
            """,
        )
        self.assertEqual([], q.matches(TERRAFORM))

    def test_anything_matches_one_node_without_capturing(self):
        q = query(self.python, t"def {capture('fname')}({anything()}):\n    {...}")
        self.assertEqual(["fname"], q.capture_names)
        self.assertNotIn("anything", q.capture_names)
        matches = q.matches(PYTHON_SOURCE)
        # Exactly the single-parameter defs; save(path, data) has two.
        self.assertEqual(["fetch", "broken"], self.texts(matches, "fname"))
        for match in matches:
            self.assertEqual(["fname"], list(match.captures))

    def test_anything_with_kind(self):
        q = query(self.python, t"def {capture('fname')}({anything('identifier')}):\n    {...}")
        self.assertEqual(["fetch", "broken"], self.texts(q.matches(PYTHON_SOURCE), "fname"))

    def test_ellipsis_at_multiple_positions_including_nested(self):
        # A nested `{...}` relaxes the `if` body without disturbing @cond. The
        # trailing hole in the body sits after the `":"` that separates the
        # if_statement's `condition` from its `consequence`, but that colon is
        # structural punctuation between two *field* children, not a list
        # separator, so it survives and keeps @cond pinned to the condition.
        # Dropping it would both free @cond to bind `return 1` and leave a
        # pattern the grammar can never match.
        q = query(
            self.python,
            t"def {capture('fn')}({...}):\n    if {capture('cond')}:\n        {...}\n    {...}",
        )
        source = "def a(x):\n    if x:\n        return 1\n    return 2\ndef b(y):\n    return y\n"
        matches = q.matches(source)
        self.assertEqual([("a", "x")], [(m.text("fn"), m.text("cond")) for m in matches])

    def test_ellipsis_in_an_argument_position(self):
        q = query(self.python, t"class {capture('c')}({...}):\n    {...}")
        source = "class A(B, C):\n    x = 1\n    def m(self):\n        pass\nclass D:\n    pass\n"
        # class D has no argument list at all, so it does not match.
        self.assertEqual(["A"], self.texts(q.matches(source), "c"))

    def test_str_interpolation_splices_literal_text(self):
        resource_type = "aws_iam_role"
        q = query(
            self.hcl,
            t"""
            resource "{resource_type}" "{capture("name")}" {{
              {...}
            }}
            """,
        )
        self.assertEqual(["runner"], self.texts(q.matches(TERRAFORM), "name"))
        # The spliced text is a literal, not a capture.
        self.assertEqual(["name"], q.capture_names)

    def test_str_interpolation_is_parameterisable(self):
        found = {}
        for resource_type in ("aws_s3_bucket", "aws_iam_role"):
            q = query(
                self.hcl,
                t"""
                resource "{resource_type}" "{capture("name")}" {{
                  {...}
                }}
                """,
            )
            found[resource_type] = self.texts(q.matches(TERRAFORM), "name")
        self.assertEqual(
            {
                "aws_s3_bucket": ["logs", "public_assets", "backups"],
                "aws_iam_role": ["runner"],
            },
            found,
        )


class TestMatchApi(TemplateQueryTestBase):
    def setUp(self):
        self.query = query(
            self.hcl,
            t"""
            resource "aws_s3_bucket" "{capture("name")}" {{
              {...}
            }}
            """,
        )
        self.matches = self.query.matches(TERRAFORM)
        self.match = self.matches[0]

    def test_getitem_returns_a_node(self):
        node = self.match["name"]
        self.assertIsInstance(node, Node)
        self.assertEqual(b"logs", node.text)

    def test_text_returns_source_text(self):
        self.assertEqual("logs", self.match.text("name"))

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.match.get("nope"))

    def test_get_missing_returns_default(self):
        sentinel = object()
        self.assertIs(sentinel, self.match.get("nope", sentinel))

    def test_getitem_missing_raises_key_error(self):
        with self.assertRaises(KeyError):
            self.match["missing"]

    def test_all_returns_a_list(self):
        self.assertEqual([b"logs"], [n.text for n in self.match.all("name")])
        self.assertEqual([], self.match.all("missing"))

    def test_contains(self):
        self.assertIn("name", self.match)
        self.assertNotIn("missing", self.match)

    def test_iteration_yields_capture_names(self):
        self.assertEqual(["name"], list(self.match))

    def test_pattern_index_is_present(self):
        self.assertEqual(0, self.match.pattern_index)
        for match in self.matches:
            self.assertEqual(0, match.pattern_index)

    def test_sexp_is_non_empty(self):
        self.assertTrue(self.query.sexp.strip())
        self.assertIn("@name", self.query.sexp)

    def test_capture_names_lists_only_public_names(self):
        self.assertEqual(["name"], self.query.capture_names)
        # The generated query really does use private captures internally...
        self.assertIn("@_h0", self.query.sexp)

    def test_private_captures_never_appear_in_matches(self):
        for match in self.matches:
            for name in match.captures:
                self.assertFalse(
                    name.startswith("_h"),
                    f"private capture {name!r} leaked into Match.captures",
                )
            self.assertEqual(["name"], list(match.captures))

    def test_matches_accepts_str_bytes_and_tree(self):
        from_str = self.texts(self.query.matches(TERRAFORM), "name")
        from_bytes = self.texts(self.query.matches(TERRAFORM.encode()), "name")
        tree = Parser(self.hcl).parse(TERRAFORM.encode())
        from_tree = self.texts(self.query.matches(tree), "name")
        self.assertEqual(["logs", "public_assets", "backups"], from_str)
        self.assertEqual(from_str, from_bytes)
        self.assertEqual(from_str, from_tree)

    def test_first_returns_a_match(self):
        first = self.query.first(TERRAFORM)
        self.assertIsNotNone(first)
        self.assertEqual("logs", first.text("name"))

    def test_first_returns_none_when_nothing_matches(self):
        self.assertIsNone(self.query.first('resource "aws_iam_role" "r" { acl = "private" }'))

    def test_captures_merges_by_name(self):
        merged = self.query.captures(TERRAFORM)
        self.assertEqual(["name"], list(merged))
        # All three buckets end up under the one name. Order is asserted as a set
        # because captures() ordering is unstable (see TestKnownBugs).
        self.assertEqual(
            {"logs", "public_assets", "backups"},
            {node.text.decode() for node in merged["name"]},
        )
        self.assertEqual(3, len(merged["name"]))

    def test_captures_accepts_bytes_and_tree_too(self):
        tree = Parser(self.hcl).parse(TERRAFORM.encode())
        expected = {"logs", "public_assets", "backups"}
        for source in (TERRAFORM, TERRAFORM.encode(), tree):
            merged = self.query.captures(source)
            # Compared as a set: see TestKnownBugs for the ordering instability.
            self.assertEqual(expected, {node.text.decode() for node in merged["name"]})

    def test_captures_hides_private_names(self):
        for name in self.query.captures(TERRAFORM):
            self.assertFalse(name.startswith("_h"))

    def test_language_round_trips(self):
        self.assertIs(self.hcl, self.query.language)

    def test_repr_mentions_capture_names(self):
        self.assertIn("name", repr(self.query))
        self.assertIn("logs", repr(self.match))


class TestMultiLanguage(TemplateQueryTestBase):
    def test_python(self):
        q = query(self.python, t"def {capture('fn')}({...}):\n    {...}")
        self.assertEqual(["fetch", "save", "broken"], self.texts(q.matches(PYTHON_SOURCE), "fn"))

    def test_python_with_statement(self):
        q = query(
            self.python,
            t'with open({capture("path")}, "w") as {capture("handle")}:\n    {...}',
        )
        matches = q.matches(PYTHON_SOURCE)
        self.assertEqual([("path", "fh")], [(m.text("path"), m.text("handle")) for m in matches])

    def test_json_value_of_a_known_key(self):
        q = query(self.json, t'{{"version": {capture("version")}, {...}}}')
        matches = q.matches(JSON_SOURCE)
        self.assertEqual(['"2.1.0"'], self.texts(matches, "version"))

    def test_json_inner_string_text(self):
        q = query(self.json, t'{{"name": "{capture("name")}", {...}}}')
        self.assertEqual(["widget"], self.texts(q.matches(JSON_SOURCE), "name"))

    def test_json_nested_object_pair(self):
        q = query(self.json, t'{{"{capture("dep")}": "{capture("range")}"}}')
        matches = q.matches(JSON_SOURCE)
        self.assertEqual(
            [("left-pad", "^1.0.0")], [(m.text("dep"), m.text("range")) for m in matches]
        )

    def test_javascript_route_registrations(self):
        q = query(self.javascript, t"app.get({capture('route')}, {capture('handler')});")
        matches = q.matches(JS_SOURCE)
        self.assertEqual(
            [('"/users"', "listUsers"), ('"/health"', "ping")],
            [(m.text("route"), m.text("handler")) for m in matches],
        )
        # app.post is a different anonymous/identifier text, so it is excluded.
        self.assertNotIn("createUser", self.texts(matches, "handler"))

    def test_javascript_inner_string_and_braces(self):
        source = "function add(a, b) { return a + b; }\nfunction sub(a, b) { return a - b; }\n"
        q = query(
            self.javascript,
            t"function {capture('fn')}({...}) {{ return {capture('l')} + {capture('r')}; }}",
        )
        matches = q.matches(source)
        self.assertEqual(
            [("add", "a", "b")],
            [(m.text("fn"), m.text("l"), m.text("r")) for m in matches],
        )

    def test_hcl(self):
        # Literal resource type, hole for the name: selects the one bucket that
        # carries a `versioning` attribute.
        q = query(
            self.hcl,
            t"""
            resource "aws_s3_bucket" "{capture("name")}" {{
              versioning = true
              {...}
            }}
            """,
        )
        self.assertEqual(["backups"], self.texts(q.matches(TERRAFORM), "name"))

    def test_hcl_two_captured_labels(self):
        q = query(
            self.hcl,
            t"""
            resource "{capture("type")}" "{capture("name")}" {{
              {...}
            }}
            """,
        )
        matches = q.matches(TERRAFORM)
        self.assertEqual(
            [
                ("aws_s3_bucket", "logs"),
                ("aws_s3_bucket", "public_assets"),
                ("aws_s3_bucket", "backups"),
                ("aws_iam_role", "runner"),
            ],
            [(m.text("type"), m.text("name")) for m in matches],
        )


class TestErrors(TemplateQueryTestBase):
    def test_unparseable_template_raises_template_syntax_error(self):
        with self.assertRaises(TemplateSyntaxError) as ctx:
            query(self.python, t"def (((:")
        message = str(ctx.exception)
        self.assertIn("does not parse", message)
        # The message must show what we actually tried to parse.
        self.assertIn("def (((:", message)

    def test_unparseable_hcl_template_raises_template_syntax_error(self):
        with self.assertRaises(TemplateSyntaxError):
            query(self.hcl, t'resource "aws_s3_bucket" {{{{ ]]] ')

    def test_template_syntax_error_is_a_template_error(self):
        with self.assertRaises(TemplateError):
            query(self.python, t"def (((:")

    def test_int_interpolation_raises_type_error(self):
        with self.assertRaises(TypeError) as ctx:
            query(self.hcl, t'resource "aws_s3_bucket" "{123}" {{ {...} }}')
        self.assertIn("int", str(ctx.exception))

    def test_other_unsupported_interpolations_raise_type_error(self):
        for value in (1.5, None, [1, 2], {"a": 1}):
            with self.subTest(value=value), self.assertRaises(TypeError):
                query(self.hcl, t'resource "aws_s3_bucket" "{value}" {{ {...} }}')

    def test_impossible_kind_raises_template_compile_error(self):
        # A kind that the grammar can never produce at this position is rejected
        # by tree-sitter itself; the wrapper surfaces it as a compile error with
        # the generated query attached.
        with self.assertRaises(TemplateCompileError) as ctx:
            query(self.python, t"def {capture('fn', 'integer')}({...}):\n    {...}")
        self.assertIn("generated query", str(ctx.exception))


class TestKnownBugs(TemplateQueryTestBase):
    """Templates that should match but do not. See the per-test comments."""

    def test_trailing_ellipsis_should_not_require_a_following_sibling(self):
        # A trailing `{...}` must not leave its separator behind. The rendered
        # template `{"version": TSQH0, "TSQH1": 0}` drops the sentinel pair for
        # the AnyChildren hole, and the "," that joined it to the previous member
        # is dropped with it, so the generated query is
        #     (object "{" (pair ... (_) @v) "}")
        # with no comma demanding a member *after* the captured pair. Both
        # objects below contain "version" and so both match, wherever it sits.
        q = query(self.json, t'{{"version": {capture("v")}, {...}}}')
        version_first = '{"version": "2.1.0", "name": "widget"}'
        version_last = '{"name": "widget", "version": "2.1.0"}'
        self.assertEqual(['"2.1.0"'], self.texts(q.matches(version_first), "v"))
        self.assertEqual(['"2.1.0"'], self.texts(q.matches(version_last), "v"))

    def test_leading_ellipsis_should_not_require_a_preceding_sibling(self):
        # The mirror image of the above: a leading `{...}` has no separator before
        # it, so the one that *followed* it is the orphan and is dropped instead,
        # giving (object "{" (pair ...) "}"). The pinned key may therefore be the
        # first member of the object as well as a later one.
        q = query(self.json, t'{{{...}, "version": {capture("v")}}}')
        version_first = '{"version": "2.1.0", "name": "widget"}'
        version_last = '{"name": "widget", "version": "2.1.0"}'
        self.assertEqual(['"2.1.0"'], self.texts(q.matches(version_last), "v"))
        self.assertEqual(['"2.1.0"'], self.texts(q.matches(version_first), "v"))

    def test_quoted_hole_should_not_gain_extra_quotes_from_padding_search(self):
        # A hole the user already wrote inside quotes must keep the bare sentinel.
        # Quote padding on top renders `""TSQH0""`, which still parses in HCL (an
        # empty string plus an identifier) and so would be silently accepted by
        # the padding search -- but it compiles to a pattern expecting children
        # that do not exist, and matches nothing.
        q = query(
            self.hcl,
            t"""
            resource "{capture("type")}" "{capture("name")}" {{
              versioning = true
              {...}
            }}
            """,
        )
        self.assertNotIn('""TSQH', q.rendered_template)
        matches = q.matches(TERRAFORM)
        self.assertEqual(
            [("aws_s3_bucket", "backups")],
            [(m.text("type"), m.text("name")) for m in matches],
        )

    def test_captures_ordering_should_be_deterministic(self):
        # QueryCursor.captures() does not guarantee an order: repeating the same
        # call on live pre-parsed trees used to yield both
        # ('logs', 'backups', 'public_assets') and
        # ('logs', 'public_assets', 'backups'). TemplateQuery.captures() sorts by
        # byte range so callers get source order every time.
        q = query(
            self.hcl,
            t"""
            resource "aws_s3_bucket" "{capture("name")}" {{
              {...}
            }}
            """,
        )
        orders = set()
        trees = []  # keep the trees alive; the reordering needs live trees
        for _ in range(200):
            tree = Parser(self.hcl).parse(TERRAFORM.encode())
            trees.append(tree)
            orders.add(tuple(n.text.decode() for n in q.captures(tree)["name"]))
        self.assertEqual({("logs", "public_assets", "backups")}, orders)


if __name__ == "__main__":
    unittest.main()
