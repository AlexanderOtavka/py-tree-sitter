from unittest import TestCase

import tree_sitter_hcl
import tree_sitter_python

from tree_sitter import Language, Parser, Query, QueryCursor
from tree_sitter.template._compile import (
    CompileResult,
    compile_tree,
    find_pattern_root,
)
from tree_sitter.template._errors import TemplateCompileError
from tree_sitter.template._holes import (
    Alternatives,
    AnyChildren,
    Capture,
    HoleSlot,
    Wildcard,
)
from tree_sitter.template._sexp import render


def slot(index, hole, text, sentinel="TSQH0", pad=""):
    """Build a HoleSlot by locating ``sentinel + pad`` inside rendered ``text``."""
    span = sentinel + pad
    start = text.index(span)
    return HoleSlot(
        index=index,
        hole=hole,
        sentinel=sentinel,
        start=start,
        end=start + len(span),
    )


class TemplateCompileTestBase(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hcl = Language(tree_sitter_hcl.language())
        cls.python = Language(tree_sitter_python.language())

    def compile(self, language, text, slots):
        """Render + compile ``text`` (a rendered template) into a CompileResult."""
        source = text.encode("utf-8")
        tree = Parser(language).parse(source)
        self.assertFalse(
            tree.root_node.has_error,
            f"test fixture does not parse cleanly:\n{text}",
        )
        return compile_tree(tree, slots, source=source, language=language)

    def run_query(self, language, result, source):
        """Build a real Query from ``result`` and run it over ``source``."""
        query = Query(language, result.text)
        tree = Parser(language).parse(source)
        matches = QueryCursor(query).matches(tree.root_node)
        return query, matches

    def captured(self, captures, name):
        return [n.text.decode("utf-8") for n in captures.get(name, [])]


HCL_TEMPLATE = 'resource "aws_s3_bucket" TSQH0 {\n  acl = "private"\n}\n'

HCL_TEMPLATE_ELLIPSIS = 'resource "aws_s3_bucket" TSQH0 {\n  acl = "private"\n  TSQH1 = 0\n}\n'

HCL_TWO_RESOURCES = b"""resource "aws_s3_bucket" mybucket {
  acl = "private"
}

resource "aws_instance" myinstance {
  acl = "private"
}
"""

HCL_EXTRA_ATTRIBUTE = b"""resource "aws_s3_bucket" mybucket {
  acl = "private"
  versioning = true
}
"""


class TestPatternRoot(TemplateCompileTestBase):
    def test_descends_to_block_for_hcl(self):
        """config_file -> body -> block: descend past single-child wrappers."""
        text = HCL_TEMPLATE
        slots = [slot(0, Capture("name"), text)]
        tree = Parser(self.hcl).parse(text.encode())
        self.assertEqual(tree.root_node.type, "config_file")
        root = find_pattern_root(tree.root_node, slots)
        self.assertEqual(root.type, "block")

    def test_stops_at_multi_child_node(self):
        """A body with two blocks has 2+ children, so descent stops there."""
        text = "a {\n  x = 1\n}\nb {\n  y = 2\n}\n"
        tree = Parser(self.hcl).parse(text.encode())
        root = find_pattern_root(tree.root_node, [])
        self.assertEqual(root.type, "body")

    def test_stops_at_hole_target(self):
        """Descent must not walk past a node that is itself a hole target."""
        text = "TSQH0\n"
        slots = [slot(0, Capture("stmt"), text)]
        tree = Parser(self.python).parse(text.encode())
        root = find_pattern_root(tree.root_node, slots)
        # module -> expression_statement -> identifier. The slot span resolves to
        # the outermost node inside it (expression_statement), so descent stops
        # there instead of running on to the identifier leaf.
        self.assertEqual(root.type, "expression_statement")
        # The hole then replaces that whole node, so the pattern is just (_) @stmt.
        result = compile_tree(tree, slots, source=text.encode(), language=self.python)
        self.assertEqual(result.text, "(_) @stmt")
        self.assertEqual(result.capture_names, ["stmt"])
        self.assertEqual(result.private_names, [])

    def test_stops_at_leaf(self):
        text = "x\n"
        tree = Parser(self.python).parse(text.encode())
        root = find_pattern_root(tree.root_node, [])
        self.assertEqual(root.type, "identifier")


class TestHclResourceEndToEnd(TemplateCompileTestBase):
    def test_compiles_and_matches_exactly_one_resource(self):
        text = HCL_TEMPLATE
        slots = [slot(0, Capture("name"), text)]
        result = self.compile(self.hcl, text, slots)

        self.assertIsInstance(result, CompileResult)
        self.assertEqual(result.capture_names, ["name"])
        self.assertTrue(result.private_names)
        self.assertTrue(all(n.startswith("_h") for n in result.private_names))

        query, matches = self.run_query(self.hcl, result, HCL_TWO_RESOURCES)
        self.assertEqual(query.pattern_count, 1)
        self.assertEqual(len(matches), 1)
        _, captures = matches[0]
        self.assertEqual(self.captured(captures, "name"), ["mybucket"])

    def test_text_equals_pattern_root_plus_predicates(self):
        """``text`` must be a faithful rendering of ``pattern``."""
        text = HCL_TEMPLATE
        result = self.compile(self.hcl, text, [slot(0, Capture("name"), text)])
        self.assertIn(result.pattern.root.render(1).strip(), result.text)
        for predicate in result.pattern.predicates:
            self.assertIn(predicate.render(), result.text)

    def test_capture_hole_in_label_position_selects_label_node(self):
        """An unpadded label hole replaces just the label, not the block."""
        text = HCL_TEMPLATE
        result = self.compile(self.hcl, text, [slot(0, Capture("name"), text)])
        # (_) @name appears as a direct child of the block, alongside the
        # block_start/body/block_end siblings.
        self.assertIn("(_) @name", result.text)
        self.assertIn("(block_start", result.text)
        self.assertIn("(block_end", result.text)

    def test_capture_hole_with_kind(self):
        text = HCL_TEMPLATE
        result = self.compile(self.hcl, text, [slot(0, Capture("name", kind="identifier"), text)])
        self.assertIn("(identifier) @name", result.text)
        _, matches = self.run_query(self.hcl, result, HCL_TWO_RESOURCES)
        self.assertEqual(len(matches), 1)


class TestAnchoring(TemplateCompileTestBase):
    def test_strict_template_rejects_extra_attribute(self):
        """Anchors make a one-attribute template refuse a two-attribute block."""
        text = HCL_TEMPLATE
        result = self.compile(self.hcl, text, [slot(0, Capture("name"), text)])
        self.assertIn(".", result.text)

        _, matches = self.run_query(self.hcl, result, HCL_EXTRA_ATTRIBUTE)
        self.assertEqual(len(matches), 0)

    def test_ellipsis_allows_extra_attribute(self):
        """AnyChildren un-anchors the body, so extra attributes are permitted."""
        text = HCL_TEMPLATE_ELLIPSIS
        slots = [
            slot(0, Capture("name"), text),
            slot(1, AnyChildren(), text, sentinel="TSQH1", pad=" = 0"),
        ]
        result = self.compile(self.hcl, text, slots)

        _, matches = self.run_query(self.hcl, result, HCL_EXTRA_ATTRIBUTE)
        self.assertEqual(len(matches), 1)
        _, captures = matches[0]
        self.assertEqual(self.captured(captures, "name"), ["mybucket"])

    def test_ellipsis_still_requires_the_listed_attribute(self):
        """Relaxing the body must not turn the template into "match anything"."""
        text = HCL_TEMPLATE_ELLIPSIS
        slots = [
            slot(0, Capture("name"), text),
            slot(1, AnyChildren(), text, sentinel="TSQH1", pad=" = 0"),
        ]
        result = self.compile(self.hcl, text, slots)
        _, matches = self.run_query(
            self.hcl, result, b'resource "aws_s3_bucket" b {\n  other = 1\n}\n'
        )
        self.assertEqual(len(matches), 0)

    def test_ellipsis_parent_is_unanchored(self):
        """The body holding the ellipsis loses its anchors; the block keeps them."""
        text = HCL_TEMPLATE_ELLIPSIS
        slots = [
            slot(0, Capture("name"), text),
            slot(1, AnyChildren(), text, sentinel="TSQH1", pad=" = 0"),
        ]
        result = self.compile(self.hcl, text, slots)
        block = result.pattern.root
        self.assertEqual(block.kind, "block")
        self.assertTrue(block.anchored)
        bodies = [c for c in block.children if getattr(c, "kind", None) == "body"]
        self.assertEqual(len(bodies), 1)
        self.assertFalse(bodies[0].anchored)

    def test_padded_ellipsis_slot_selects_whole_attribute(self):
        """The padded span ``TSQH1 = 0`` resolves to the attribute node."""
        text = HCL_TEMPLATE_ELLIPSIS
        slots = [
            slot(0, Capture("name"), text),
            slot(1, AnyChildren(), text, sentinel="TSQH1", pad=" = 0"),
        ]
        result = self.compile(self.hcl, text, slots)
        # The sentinel identifier and its padding are gone entirely.
        self.assertNotIn("TSQH1", result.text)
        self.assertNotIn('"0"', result.text)


class TestEqPinning(TemplateCompileTestBase):
    HCL_KEY_TEMPLATE = 'block TSQH0 {\n  key = "some_value"\n}\n'

    def test_pins_leaf_text(self):
        text = self.HCL_KEY_TEMPLATE
        result = self.compile(self.hcl, text, [slot(0, Capture("label"), text)])
        self.assertIn("(#eq? @_h", result.text)
        self.assertIn('"some_value"', result.text)

        _, matches = self.run_query(self.hcl, result, b'block a {\n  key = "some_value"\n}\n')
        self.assertEqual(len(matches), 1)

    def test_rejects_different_leaf_text(self):
        text = self.HCL_KEY_TEMPLATE
        result = self.compile(self.hcl, text, [slot(0, Capture("label"), text)])
        _, matches = self.run_query(self.hcl, result, b'block a {\n  key = "other"\n}\n')
        self.assertEqual(len(matches), 0)

    def test_rejects_different_key_name(self):
        text = self.HCL_KEY_TEMPLATE
        result = self.compile(self.hcl, text, [slot(0, Capture("label"), text)])
        _, matches = self.run_query(self.hcl, result, b'block a {\n  other = "some_value"\n}\n')
        self.assertEqual(len(matches), 0)

    def test_predicates_are_a_single_pattern(self):
        """Predicates must bind to the root pattern, not become a 2nd pattern."""
        text = self.HCL_KEY_TEMPLATE
        result = self.compile(self.hcl, text, [slot(0, Capture("label"), text)])
        query = Query(self.hcl, result.text)
        self.assertEqual(query.pattern_count, 1)

    def test_private_names_are_unique_and_ordered(self):
        text = self.HCL_KEY_TEMPLATE
        result = self.compile(self.hcl, text, [slot(0, Capture("label"), text)])
        self.assertEqual(len(result.private_names), len(set(result.private_names)))
        self.assertEqual(
            result.private_names,
            [f"_h{i}" for i in range(len(result.private_names))],
        )


class TestAnonymousNodes(TemplateCompileTestBase):
    def test_plus_does_not_match_minus(self):
        """Anonymous operators are load-bearing: x + y must not match x - y."""
        text = "x + y\n"
        result = self.compile(self.python, text, [])
        self.assertIn('"+"', result.text)

        _, plus = self.run_query(self.python, result, b"x + y\n")
        self.assertEqual(len(plus), 1)

        _, minus = self.run_query(self.python, result, b"x - y\n")
        self.assertEqual(len(minus), 0)

    def test_operands_are_pinned(self):
        text = "x + y\n"
        result = self.compile(self.python, text, [])
        _, matches = self.run_query(self.python, result, b"a + b\n")
        self.assertEqual(len(matches), 0)


class TestConstrainedCaptures(TemplateCompileTestBase):
    TEMPLATE = 'resource TSQH0 {\n  acl = "private"\n}\n'

    SOURCE = b"""resource aws_s3_bucket {
  acl = "private"
}

resource gcp_thing {
  acl = "private"
}
"""

    def test_regex_pattern(self):
        text = self.TEMPLATE
        result = self.compile(self.hcl, text, [slot(0, Capture("name", pattern="^aws_"), text)])
        self.assertIn('(#match? @name "^aws_")', result.text)

        query, matches = self.run_query(self.hcl, result, self.SOURCE)
        self.assertEqual(query.pattern_count, 1)
        self.assertEqual(len(matches), 1)
        _, captures = matches[0]
        self.assertEqual(self.captured(captures, "name"), ["aws_s3_bucket"])

    def test_one_of(self):
        text = self.TEMPLATE
        result = self.compile(
            self.hcl,
            text,
            [slot(0, Capture("name", one_of=("gcp_thing", "azure_thing")), text)],
        )
        self.assertIn('(#any-of? @name "gcp_thing" "azure_thing")', result.text)

        query, matches = self.run_query(self.hcl, result, self.SOURCE)
        self.assertEqual(query.pattern_count, 1)
        self.assertEqual(len(matches), 1)
        _, captures = matches[0]
        self.assertEqual(self.captured(captures, "name"), ["gcp_thing"])

    def test_wildcard_hole_captures_nothing(self):
        text = self.TEMPLATE
        result = self.compile(self.hcl, text, [slot(0, Wildcard(), text)])
        self.assertEqual(result.capture_names, [])
        self.assertIn("(_)", result.text)

        _, matches = self.run_query(self.hcl, result, self.SOURCE)
        self.assertEqual(len(matches), 2)

    def test_wildcard_hole_with_kind(self):
        text = self.TEMPLATE
        result = self.compile(self.hcl, text, [slot(0, Wildcard("identifier"), text)])
        self.assertEqual(result.capture_names, [])
        _, matches = self.run_query(self.hcl, result, self.SOURCE)
        self.assertEqual(len(matches), 2)

    def test_alternatives_named(self):
        text = self.TEMPLATE
        result = self.compile(
            self.hcl,
            text,
            [slot(0, Alternatives(texts=("aws_s3_bucket",), name="name"), text)],
        )
        self.assertEqual(result.capture_names, ["name"])
        self.assertIn('(#any-of? @name "aws_s3_bucket")', result.text)

        _, matches = self.run_query(self.hcl, result, self.SOURCE)
        self.assertEqual(len(matches), 1)
        _, captures = matches[0]
        self.assertEqual(self.captured(captures, "name"), ["aws_s3_bucket"])

    def test_alternatives_unnamed_uses_private_capture(self):
        text = self.TEMPLATE
        result = self.compile(self.hcl, text, [slot(0, Alternatives(texts=("gcp_thing",)), text)])
        self.assertEqual(result.capture_names, [])
        self.assertTrue(any("any-of?" == p.name for p in result.pattern.predicates))

        _, matches = self.run_query(self.hcl, result, self.SOURCE)
        self.assertEqual(len(matches), 1)


class TestExtras(TemplateCompileTestBase):
    def test_comment_in_template_is_skipped(self):
        """A commented template must still match uncommented source.

        The comment's *text* must not leak into the pattern. ``(comment)*`` runs
        do appear, but those come from the extras tolerance below, not from the
        template's own comment.
        """
        text = 'resource "aws_s3_bucket" TSQH0 {\n  # why\n  acl = "private"\n}\n'
        result = self.compile(self.hcl, text, [slot(0, Capture("name"), text)])
        self.assertNotIn("why", result.text)

        _, matches = self.run_query(self.hcl, result, HCL_TWO_RESOURCES)
        self.assertEqual(len(matches), 1)

    def test_comment_in_source_does_not_break_match(self):
        """Adding a comment to the matched source must not lose the match."""
        text = 'resource "aws_s3_bucket" TSQH0 {\n  acl = "private"\n}\n'
        result = self.compile(self.hcl, text, [slot(0, Capture("name"), text)])

        commented = (
            b'resource "aws_s3_bucket" "logs" { # trailing\n'
            b"  # leading\n"
            b'  acl = "private"\n'
            b"  # after\n"
            b"}\n"
        )
        _, matches = self.run_query(self.hcl, result, commented)
        self.assertEqual(len(matches), 1)

    def test_extras_tolerance_does_not_admit_extra_children(self):
        """Tolerating comments must not tolerate additional real children."""
        text = 'resource "aws_s3_bucket" TSQH0 {\n  acl = "private"\n}\n'
        result = self.compile(self.hcl, text, [slot(0, Capture("name"), text)])

        two_attrs = (
            b'resource "aws_s3_bucket" "logs" {\n  acl = "private"\n  versioning = true\n}\n'
        )
        _, matches = self.run_query(self.hcl, result, two_attrs)
        self.assertEqual(matches, [])


class TestErrors(TemplateCompileTestBase):
    def test_slot_matching_no_node_raises(self):
        """A span past the end of the source cannot resolve to a node."""
        text = HCL_TEMPLATE
        source = text.encode()
        tree = Parser(self.hcl).parse(source)
        bad = HoleSlot(
            index=0,
            hole=Capture("name"),
            sentinel="TSQH0",
            start=len(source) + 10,
            end=len(source) + 20,
        )
        with self.assertRaises(TemplateCompileError) as ctx:
            compile_tree(tree, [bad], source=source, language=self.hcl)
        self.assertIn("TSQH0", str(ctx.exception))

    def test_empty_span_between_nodes_raises(self):
        """A zero-width span inside whitespace encloses no node."""
        text = HCL_TEMPLATE
        source = text.encode()
        tree = Parser(self.hcl).parse(source)
        gap = text.index("  acl")
        bad = HoleSlot(index=0, hole=Capture("name"), sentinel="TSQH9", start=gap, end=gap)
        with self.assertRaises(TemplateCompileError):
            compile_tree(tree, [bad], source=source, language=self.hcl)

    def test_unknown_hole_type_raises(self):
        class WeirdHole:
            is_variadic = False

        text = HCL_TEMPLATE
        with self.assertRaises(TemplateCompileError):
            self.compile(self.hcl, text, [slot(0, WeirdHole(), text)])


class TestRenderConsistency(TemplateCompileTestBase):
    def test_render_matches_text_when_no_predicates(self):
        """With no predicates, ``text`` is exactly ``_sexp.render(pattern)``."""
        text = "TSQH0\n"
        result = self.compile(self.python, text, [slot(0, Capture("stmt"), text)])
        self.assertEqual(result.pattern.predicates, [])
        self.assertEqual(result.text, render(result.pattern))

    def test_predicates_are_grouped_into_a_single_pattern(self):
        """Predicates must render *inside* the pattern's parentheses.

        A root followed by a bare ``(#eq? ...)`` is two patterns as far as
        tree-sitter is concerned, and the predicate constrains neither. The
        grouping lives in ``Pattern.render``, so ``CompileResult.text`` and
        ``_sexp.render`` agree.
        """
        text = HCL_TEMPLATE
        result = self.compile(self.hcl, text, [slot(0, Capture("name"), text)])
        self.assertTrue(result.pattern.predicates)
        self.assertEqual(result.text, render(result.pattern))
        self.assertEqual(Query(self.hcl, result.text).pattern_count, 1)

        # The ungrouped form is what we must not emit.
        ungrouped = (
            result.pattern.root.render()
            + "\n"
            + "\n".join(p.render() for p in result.pattern.predicates)
        )
        self.assertGreater(Query(self.hcl, ungrouped).pattern_count, 1)

    def test_no_predicates_case_is_single_pattern(self):
        template = "a {\n  TSQH0 = 0\n}\n"
        result = self.compile(self.hcl, template, [slot(0, AnyChildren(), template, pad=" = 0")])
        query = Query(self.hcl, result.text)
        self.assertEqual(query.pattern_count, 1)

    def test_variadic_hole_covering_whole_template_raises(self):
        """An ellipsis that swallows the pattern root leaves nothing to match."""
        template = "a {\n  TSQH0 = 0\n}\n"
        source = template.encode()
        tree = Parser(self.hcl).parse(source)
        whole = HoleSlot(
            index=0,
            hole=AnyChildren(),
            sentinel="TSQH0",
            start=0,
            end=len(source),
        )
        with self.assertRaises(TemplateCompileError):
            compile_tree(tree, [whole], source=source, language=self.hcl)
