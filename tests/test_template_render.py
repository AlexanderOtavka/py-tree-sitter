from unittest import TestCase

import tree_sitter_hcl
import tree_sitter_javascript
import tree_sitter_json
import tree_sitter_python

from tree_sitter import Language, Parser
from tree_sitter.template._errors import TemplateSyntaxError
from tree_sitter.template._holes import AnyChildren, Capture, anything, capture
from tree_sitter.template._render import SENTINEL_PREFIX, render


def parses_cleanly(language, text):
    """True when ``text`` parses with no ERROR/MISSING node and no has_error."""
    tree = Parser(language).parse(text.encode("utf-8"))
    root = tree.root_node
    if root.has_error:
        return False
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            return False
        stack.extend(node.children)
    return True


class TestTemplateRender(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hcl = Language(tree_sitter_hcl.language())
        cls.json = Language(tree_sitter_json.language())
        cls.python = Language(tree_sitter_python.language())
        cls.javascript = Language(tree_sitter_javascript.language())

    def assert_clean(self, language, result):
        self.assertTrue(
            parses_cleanly(language, result.text),
            f"rendered text does not parse cleanly:\n{result.text}",
        )

    def slot_text(self, result, index):
        slot = result.slots[index]
        return result.text.encode("utf-8")[slot.start : slot.end].decode("utf-8")

    # -- padding search ---------------------------------------------------

    def test_hcl_block_body_needs_attribute_padding(self):
        result = render(
            t"""
            resource "aws_s3_bucket" "b" {{
              {capture("attr")}
            }}
            """,
            self.hcl,
        )
        self.assert_clean(self.hcl, result)
        self.assertEqual(1, len(result.slots))
        slot = result.slots[0]
        self.assertEqual("", slot.pad_prefix)
        self.assertEqual(" = 0", slot.pad_suffix)
        self.assertEqual("TSQH0 = 0", self.slot_text(result, 0))
        self.assertIn("TSQH0 = 0", result.text)

    def test_hcl_block_label_uses_bare_sentinel(self):
        result = render(
            t"""
            resource "aws_s3_bucket" {capture("name")} {{
              acl = "private"
            }}
            """,
            self.hcl,
        )
        self.assert_clean(self.hcl, result)
        slot = result.slots[0]
        self.assertEqual("", slot.pad_prefix)
        self.assertEqual("", slot.pad_suffix)
        self.assertEqual("TSQH0", self.slot_text(result, 0))

    def test_json_object_member_is_quoted_and_valued(self):
        result = render(t"""{{"a": 1, {capture("member")}}}""", self.json)
        self.assert_clean(self.json, result)
        slot = result.slots[0]
        self.assertEqual('"', slot.pad_prefix)
        self.assertEqual('": 0', slot.pad_suffix)
        self.assertEqual('"TSQH0": 0', self.slot_text(result, 0))
        self.assertEqual('{"a": 1, "TSQH0": 0}', result.text)

    def test_json_value_position_is_quoted(self):
        result = render(t"""{{"a": {capture("value")}}}""", self.json)
        self.assert_clean(self.json, result)
        self.assertEqual('"TSQH0"', self.slot_text(result, 0))

    def test_python_statement_block_uses_bare_sentinel(self):
        result = render(
            t"""
            def f():
                {capture("body")}
            """,
            self.python,
        )
        self.assert_clean(self.python, result)
        self.assertEqual("def f():\n    TSQH0", result.text)
        self.assertEqual("TSQH0", self.slot_text(result, 0))
        self.assertEqual("", result.slots[0].pad_prefix)
        self.assertEqual("", result.slots[0].pad_suffix)

    def test_javascript_statement_block(self):
        result = render(
            t"""
            function f() {{
              {anything()}
            }}
            """,
            self.javascript,
        )
        self.assert_clean(self.javascript, result)
        self.assertEqual("TSQH0", self.slot_text(result, 0))

    # -- coercion ---------------------------------------------------------

    def test_str_interpolation_splices_without_a_slot(self):
        kind = "aws_s3_bucket"
        result = render(
            t"""
            resource "{kind}" {capture("name")} {{
              acl = "private"
            }}
            """,
            self.hcl,
        )
        self.assert_clean(self.hcl, result)
        self.assertIn('resource "aws_s3_bucket"', result.text)
        self.assertNotIn(f"{SENTINEL_PREFIX}1", result.text)
        # One interpolation was a str, so only the capture produced a slot.
        self.assertEqual(1, len(result.slots))
        self.assertEqual(0, result.slots[0].index)
        self.assertEqual("TSQH0", result.slots[0].sentinel)
        self.assertIsInstance(result.slots[0].hole, Capture)

    def test_ellipsis_coerces_to_any_children(self):
        result = render(
            t"""
            resource "aws_s3_bucket" "b" {{
              acl = "private"
              {...}
            }}
            """,
            self.hcl,
        )
        self.assert_clean(self.hcl, result)
        self.assertEqual(1, len(result.slots))
        self.assertIsInstance(result.slots[0].hole, AnyChildren)
        self.assertTrue(result.slots[0].hole.is_variadic)
        # An AnyChildren hole still gets a sentinel and a span.
        self.assertEqual("TSQH0 = 0", self.slot_text(result, 0))

    def test_unsupported_interpolation_raises_type_error(self):
        value = 42
        with self.assertRaises(TypeError) as ctx:
            render(t"""x = {value}""", self.python)
        self.assertIn("value", str(ctx.exception))
        self.assertIn("int", str(ctx.exception))

    def test_hole_instances_pass_through_unchanged(self):
        hole = capture("name", "identifier", pattern="^f")
        result = render(t"""x = {hole}""", self.python)
        self.assertIs(hole, result.slots[0].hole)

    # -- dedent -----------------------------------------------------------

    def test_dedent_strips_common_indent_and_blank_edges(self):
        result = render(
            t"""
                resource "aws_s3_bucket" "b" {{
                  acl = "private"
                  tags = {{}}
                }}
            """,
            self.hcl,
        )
        self.assert_clean(self.hcl, result)
        self.assertEqual(
            'resource "aws_s3_bucket" "b" {\n  acl = "private"\n  tags = {}\n}',
            result.text,
        )

    def test_dedent_is_computed_across_holes(self):
        result = render(
            t"""
                def f():
                    {capture("first")}
                    {capture("second")}
            """,
            self.python,
        )
        self.assert_clean(self.python, result)
        self.assertEqual("def f():\n    TSQH0\n    TSQH1", result.text)

    # -- spans ------------------------------------------------------------

    def test_spans_are_byte_offsets_with_non_ascii_text(self):
        result = render(
            t"""
            resource "aws_s3_bucket" "b" {{
              description = "caffè ☕"
              {capture("attr")}
            }}
            """,
            self.hcl,
        )
        self.assert_clean(self.hcl, result)
        slot = result.slots[0]
        source = result.text.encode("utf-8")
        self.assertEqual(b"TSQH0 = 0", source[slot.start : slot.end])
        # The span is a byte span, not a character span.
        self.assertNotEqual(result.text[slot.start : slot.end], "TSQH0 = 0")

    def test_multiple_holes_get_distinct_sentinels_and_spans(self):
        result = render(
            t"""
            resource "aws_s3_bucket" {capture("name")} {{
              acl = {capture("acl")}
              {capture("extra")}
              {...}
            }}
            """,
            self.hcl,
        )
        self.assert_clean(self.hcl, result)
        self.assertEqual(4, len(result.slots))
        self.assertEqual(
            ["TSQH0", "TSQH1", "TSQH2", "TSQH3"],
            [slot.sentinel for slot in result.slots],
        )
        self.assertEqual([0, 1, 2, 3], [slot.index for slot in result.slots])
        source = result.text.encode("utf-8")
        for slot in result.slots:
            rendered = source[slot.start : slot.end].decode("utf-8")
            self.assertEqual(
                slot.pad_prefix + slot.sentinel + slot.pad_suffix,
                rendered,
                f"span of {slot.sentinel} does not cover its rendered text",
            )
            self.assertIn(slot.sentinel, rendered)
        # Spans are disjoint and ordered.
        for left, right in zip(result.slots, result.slots[1:]):
            self.assertLess(left.end, right.start)

    def test_expression_source_is_recorded(self):
        result = render(t"""x = {capture("rhs")}""", self.python)
        self.assertEqual('capture("rhs")', result.slots[0].expression)

    # -- misc -------------------------------------------------------------

    def test_template_without_holes_renders_text_only(self):
        result = render(
            t"""
            resource "aws_s3_bucket" "b" {{
              acl = "private"
            }}
            """,
            self.hcl,
        )
        self.assert_clean(self.hcl, result)
        self.assertEqual([], result.slots)
        self.assertEqual('resource "aws_s3_bucket" "b" {\n  acl = "private"\n}', result.text)

    def test_prebuilt_parser_is_accepted(self):
        parser = Parser(self.hcl)
        result = render(
            t"""
            resource "aws_s3_bucket" "b" {{
              {capture("attr")}
            }}
            """,
            self.hcl,
            parser=parser,
        )
        self.assert_clean(self.hcl, result)
        self.assertEqual("TSQH0 = 0", self.slot_text(result, 0))

    def test_unparseable_template_raises_template_syntax_error(self):
        with self.assertRaises(TemplateSyntaxError) as ctx:
            render(t"""def f(:::: {capture("x")}""", self.python)
        message = str(ctx.exception)
        self.assertIn("Rendered text was:", message)
        self.assertIn("TSQH0", message)
        self.assertIn("row", message)
