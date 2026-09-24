"""Tests for ``tree_sitter.template.Edits``.

The three traps the module exists to close are each covered by a test that
asserts a concrete output string, plus -- for trap 1 and trap 2 -- a companion
test that reproduces the naive hand-rolled approach and shows it produces
something *different* and wrong. Without that contrast a passing test would not
prove much: the fix and the bug agree on single-edit inputs.

``TestEditsDecoupling`` is the load-bearing design test: it drives ``Edits`` with
a plain hand-written ``tree_sitter.Query``, never touching the template machinery.
"""

import unittest
from unittest import TestCase

import tree_sitter_hcl
import tree_sitter_json
import tree_sitter_python

from tree_sitter import Language, Node, Parser, Query, QueryCursor
from tree_sitter.template import Edits, OverlappingEditError, capture, query

HCL = Language(tree_sitter_hcl.language())
JSON = Language(tree_sitter_json.language())
PYTHON = Language(tree_sitter_python.language())


def captured(language: Language, source: str, sexp: str, name: str) -> list[Node]:
    """Run a plain S-expression query and return ``@name`` nodes in source order."""
    tree = Parser(language).parse(source.encode())
    cursor = QueryCursor(Query(language, sexp))
    nodes = cursor.captures(tree.root_node).get(name, [])
    return sorted(nodes, key=lambda node: node.byte_range)


def naive_forward(source: str, nodes: list[Node], text: str) -> str:
    """The buggy loop the module exists to prevent, for contrast in assertions."""
    out = source
    for node in nodes:
        out = out[: node.start_byte] + text + out[node.end_byte :]
    return out


class TestTrapOneForwardIteration(TestCase):
    """Trap 1: editing forwards shifts every later offset."""

    SOURCE = "def alpha(): pass\ndef beta(): pass\ndef gamma(): pass\n"
    SEXP = "(function_definition name: (identifier) @n)"

    def names(self):
        return captured(PYTHON, self.SOURCE, self.SEXP, "n")

    def test_replacements_queued_in_source_order_are_correct(self):
        nodes = self.names()
        self.assertEqual(["alpha", "beta", "gamma"], [n.text.decode() for n in nodes])

        edits = Edits(self.SOURCE)
        for node in nodes:
            edits.replace(node, "renamed")

        self.assertEqual(
            "def renamed(): pass\ndef renamed(): pass\ndef renamed(): pass\n",
            edits.apply(),
        )

    def test_naive_forward_loop_corrupts_and_edits_does_not(self):
        """The naive loop drifts by the accumulated length delta; Edits must not."""
        nodes = self.names()
        correct = "def renamed(): pass\ndef renamed(): pass\ndef renamed(): pass\n"

        broken = naive_forward(self.SOURCE, nodes, "renamed")
        self.assertNotEqual(correct, broken)
        # Pin the measured corruption so this test fails loudly if the premise changes.
        self.assertEqual(
            "def renamed(): pass\nderenamedta(): passrenamedgamma(): pass\n",
            broken,
        )
        self.assertEqual(correct, Edits(self.SOURCE).replace_all(nodes, "renamed").apply())

    def test_queue_order_does_not_affect_result(self):
        nodes = self.names()
        forward = Edits(self.SOURCE).replace_all(nodes, "renamed").apply()
        backward = Edits(self.SOURCE).replace_all(list(reversed(nodes)), "renamed").apply()
        shuffled = (
            Edits(self.SOURCE)
            .replace(nodes[1], "renamed")
            .replace(nodes[2], "renamed")
            .replace(nodes[0], "renamed")
            .apply()
        )
        self.assertEqual(forward, backward)
        self.assertEqual(forward, shuffled)

    def test_replacements_of_differing_lengths(self):
        """Growth and shrinkage in the same batch is where drift shows up worst."""
        nodes = self.names()
        result = (
            Edits(self.SOURCE)
            .replace(nodes[0], "a_very_much_longer_name")
            .replace(nodes[1], "b")
            .replace(nodes[2], "c")
            .apply()
        )
        self.assertEqual(
            "def a_very_much_longer_name(): pass\ndef b(): pass\ndef c(): pass\n",
            result,
        )


class TestTrapTwoByteOffsets(TestCase):
    """Trap 2: node offsets are UTF-8 byte offsets, not character indices."""

    # café = 5 bytes / 4 chars; CJK chars are 3 bytes each; the emoji is 4 bytes.
    SOURCE = 'a = "café"\nb = "漢字"\nc = "\U0001f680"\nd = "plain"\n'

    def strings(self):
        return captured(HCL, self.SOURCE, "(string_lit) @s", "s")

    def test_source_really_is_multibyte(self):
        """Guard the premise: if this stops holding, the test below proves nothing."""
        self.assertLess(len(self.SOURCE), len(self.SOURCE.encode()))
        # café +1, 漢字 +4, and the emoji is one code point in 4 bytes: +3.
        self.assertEqual(8, len(self.SOURCE.encode()) - len(self.SOURCE))

    def test_replacement_after_non_ascii_lands_correctly(self):
        last = self.strings()[-1]
        self.assertEqual(b'"plain"', last.text)
        result = Edits(self.SOURCE).replace(last, '"REPLACED"')
        self.assertEqual(
            'a = "café"\nb = "漢字"\nc = "\U0001f680"\nd = "REPLACED"\n',
            result.apply(),
        )

    def test_character_indexing_lands_mid_token(self):
        """The contrast case: str slicing with byte offsets corrupts this source.

        Measured: replacing the emoji string by character index eats the ``d = "pl``
        that follows it and leaves a dangling ``ain"``.
        """
        emoji = self.strings()[2]
        self.assertEqual('"\U0001f680"', emoji.text.decode())
        wrong = self.SOURCE[: emoji.start_byte] + '"R"' + self.SOURCE[emoji.end_byte :]
        self.assertEqual('a = "café"\nb = "漢字"\nc = "\U0001f680"\nd"R"ain"\n', wrong)
        # The node it meant to edit is still there, untouched, and a different
        # line got mangled instead -- silently.
        self.assertIn('c = "\U0001f680"', wrong)

    def test_edits_replaces_the_emoji_node_correctly(self):
        emoji = self.strings()[2]
        result = Edits(self.SOURCE).replace(emoji, '"R"').apply()
        self.assertEqual('a = "café"\nb = "漢字"\nc = "R"\nd = "plain"\n', result)

    def test_character_indexing_would_have_been_wrong_for_the_last_node(self):
        """Past the last multibyte char the byte offsets run off the end entirely."""
        last = self.strings()[-1]
        wrong = self.SOURCE[: last.start_byte] + '"REPLACED"' + self.SOURCE[last.end_byte :]
        self.assertEqual(
            'a = "café"\nb = "漢字"\nc = "\U0001f680"\nd = "plain"\n"REPLACED"',
            wrong,
        )
        self.assertNotIn('d = "REPLACED"', wrong)

    def test_every_non_ascii_string_replaced_at_once(self):
        nodes = self.strings()
        result = Edits(self.SOURCE).replace_all(nodes, '"x"').apply()
        self.assertEqual('a = "x"\nb = "x"\nc = "x"\nd = "x"\n', result)

    def test_replacement_text_may_itself_be_non_ascii(self):
        nodes = self.strings()
        result = Edits(self.SOURCE).replace(nodes[-1], '"naïve \U0001f600"').apply()
        self.assertEqual(
            'a = "café"\nb = "漢字"\nc = "\U0001f680"\nd = "naïve \U0001f600"\n',
            result,
        )

    def test_insert_before_non_ascii_node(self):
        nodes = self.strings()
        result = Edits(self.SOURCE).insert_before(nodes[0], "/*c*/").apply()
        self.assertEqual(
            'a = /*c*/"café"\nb = "漢字"\nc = "\U0001f680"\nd = "plain"\n',
            result,
        )


class TestTrapThreeOverlaps(TestCase):
    """Trap 3: nested or duplicated edits must be refused, not silently applied."""

    TERRAFORM = 'resource "aws_s3_bucket" "logs" {\n  acl = "private"\n}\n'

    def test_nested_node_and_inner_token_overlap(self):
        """A captured string_lit and the quote token that opens it conflict."""
        tree = Parser(HCL).parse(self.TERRAFORM.encode())
        cursor = QueryCursor(Query(HCL, "(string_lit) @s (quoted_template_start) @q"))
        caps = cursor.captures(tree.root_node)
        outer = next(n for n in caps["s"] if n.text == b'"private"')
        inner = next(n for n in caps["q"] if n.start_byte == outer.start_byte)

        self.assertEqual((42, 51), outer.byte_range)
        self.assertEqual((42, 43), inner.byte_range)

        edits = Edits(self.TERRAFORM).replace(outer, '"public"').replace(inner, "'")
        with self.assertRaises(OverlappingEditError) as ctx:
            edits.apply()

        message = str(ctx.exception)
        self.assertIn("[42, 51)", message)
        self.assertIn("[42, 43)", message)
        self.assertIn('"public"', message)
        self.assertIn("'", message)

    def test_overlapping_edit_error_is_a_value_error(self):
        self.assertTrue(issubclass(OverlappingEditError, ValueError))

    def test_two_replacements_of_the_same_node_conflict(self):
        node = captured(HCL, self.TERRAFORM, "(string_lit) @s", "s")[0]
        edits = Edits(self.TERRAFORM).replace(node, '"a"').replace(node, '"b"')
        with self.assertRaises(OverlappingEditError) as ctx:
            edits.apply()
        self.assertIn(f"[{node.start_byte}, {node.end_byte})", str(ctx.exception))

    def test_deleting_and_replacing_the_same_node_conflicts(self):
        node = captured(HCL, self.TERRAFORM, "(string_lit) @s", "s")[0]
        with self.assertRaises(OverlappingEditError):
            Edits(self.TERRAFORM).delete(node).replace(node, '"x"').apply()

    def test_partial_overlap_of_unrelated_ranges_conflicts(self):
        """Nesting is the common case, but a straddling overlap must also raise."""
        nodes = captured(JSON, '{"a":1,"b":2}', "(pair) @p", "p")
        outer = captured(JSON, '{"a":1,"b":2}', "(object) @o", "o")[0]
        with self.assertRaises(OverlappingEditError):
            Edits('{"a":1,"b":2}').replace(outer, "{}").replace(nodes[0], '"z":9').apply()

    def test_failed_apply_leaves_the_object_usable(self):
        nodes = captured(HCL, self.TERRAFORM, "(string_lit) @s", "s")
        edits = Edits(self.TERRAFORM).replace(nodes[0], '"a"').replace(nodes[0], '"b"')
        with self.assertRaises(OverlappingEditError):
            edits.apply()
        self.assertEqual(2, len(edits))

    def test_overlap_detected_across_an_intervening_insertion(self):
        """A wide replacement must still be compared with what it encloses."""
        source = '{"a":1,"b":2}'
        obj = captured(JSON, source, "(object) @o", "o")[0]
        pair = captured(JSON, source, "(pair) @p", "p")[1]
        number = captured(JSON, source, "(number) @n", "n")[1]
        edits = Edits(source).replace(obj, "{}").insert_before(pair, "X").replace(number, "9")
        with self.assertRaises(OverlappingEditError):
            edits.apply()


class TestAdjacentSpans(TestCase):
    """Touching ranges (``end == start``) are legitimate neighbours, not overlaps."""

    SOURCE = '{"a":1,"b":2}'

    def test_json_pair_and_following_comma_touch(self):
        """Verify the premise with real nodes before relying on it."""
        pair = captured(JSON, self.SOURCE, "(pair) @p", "p")[0]
        comma = captured(JSON, self.SOURCE, '"," @c', "c")[0]
        self.assertEqual((1, 6), pair.byte_range)
        self.assertEqual((6, 7), comma.byte_range)
        self.assertEqual(pair.end_byte, comma.start_byte)

    def test_touching_replacements_do_not_raise(self):
        pair = captured(JSON, self.SOURCE, "(pair) @p", "p")[0]
        comma = captured(JSON, self.SOURCE, '"," @c', "c")[0]
        result = Edits(self.SOURCE).replace(pair, '"z":9').replace(comma, " , ").apply()
        self.assertEqual('{"z":9 , "b":2}', result)

    def test_touching_in_reverse_queue_order_also_fine(self):
        pair = captured(JSON, self.SOURCE, "(pair) @p", "p")[0]
        comma = captured(JSON, self.SOURCE, '"," @c', "c")[0]
        result = Edits(self.SOURCE).replace(comma, " , ").replace(pair, '"z":9').apply()
        self.assertEqual('{"z":9 , "b":2}', result)

    def test_sibling_tokens_across_a_whole_object(self):
        """Replace every token of a pair back to back; nothing here overlaps."""
        key = captured(JSON, self.SOURCE, "(pair (string) @k)", "k")[0]
        colon = captured(JSON, self.SOURCE, '":" @c', "c")[0]
        value = captured(JSON, self.SOURCE, "(number) @n", "n")[0]
        self.assertEqual(key.end_byte, colon.start_byte)
        self.assertEqual(colon.end_byte, value.start_byte)
        result = (
            Edits(self.SOURCE).replace(key, '"K"').replace(colon, ": ").replace(value, "42").apply()
        )
        self.assertEqual('{"K": 42,"b":2}', result)


class TestInsertAndDelete(TestCase):
    SOURCE = "def alpha(): pass\ndef beta(): pass\n"
    SEXP = "(function_definition name: (identifier) @n)"

    def names(self):
        return captured(PYTHON, self.SOURCE, self.SEXP, "n")

    def test_insert_before(self):
        result = Edits(self.SOURCE).insert_before(self.names()[0], "my_").apply()
        self.assertEqual("def my_alpha(): pass\ndef beta(): pass\n", result)

    def test_insert_after(self):
        result = Edits(self.SOURCE).insert_after(self.names()[0], "_v2").apply()
        self.assertEqual("def alpha_v2(): pass\ndef beta(): pass\n", result)

    def test_insert_before_and_after_the_same_node(self):
        node = self.names()[1]
        result = Edits(self.SOURCE).insert_before(node, "<").insert_after(node, ">").apply()
        self.assertEqual("def alpha(): pass\ndef <beta>(): pass\n", result)

    def test_delete(self):
        result = Edits(self.SOURCE).delete(self.names()[0]).apply()
        self.assertEqual("def (): pass\ndef beta(): pass\n", result)

    def test_delete_equals_replace_with_empty_string(self):
        node = self.names()[1]
        self.assertEqual(
            Edits(self.SOURCE).replace(node, "").apply(),
            Edits(self.SOURCE).delete(node).apply(),
        )

    def test_delete_several_nodes(self):
        edits = Edits(self.SOURCE)
        for node in self.names():
            edits.delete(node)
        self.assertEqual("def (): pass\ndef (): pass\n", edits.apply())

    def test_insert_at_the_very_start_of_the_source(self):
        root_first = captured(PYTHON, self.SOURCE, "(function_definition) @f", "f")[0]
        self.assertEqual(0, root_first.start_byte)
        result = Edits(self.SOURCE).insert_before(root_first, "# header\n").apply()
        self.assertEqual("# header\ndef alpha(): pass\ndef beta(): pass\n", result)

    def test_insert_after_the_last_node(self):
        """The last ``function_definition`` stops before the trailing newline."""
        last = captured(PYTHON, self.SOURCE, "(function_definition) @f", "f")[-1]
        self.assertEqual(len(self.SOURCE.encode()) - 1, last.end_byte)
        edits = Edits(self.SOURCE).insert_after(last, "\n# footer")
        self.assertEqual("def alpha(): pass\ndef beta(): pass\n# footer\n", edits.apply())

    def test_insert_at_the_absolute_end_offset(self):
        """The module tree spans the whole source, including the trailing newline."""
        tree = Parser(PYTHON).parse(self.SOURCE.encode())
        root = tree.root_node
        self.assertEqual(len(self.SOURCE.encode()), root.end_byte)
        self.assertEqual(
            "def alpha(): pass\ndef beta(): pass\n# eof",
            Edits(self.SOURCE).insert_after(root, "# eof").apply(),
        )

    def test_replace_the_entire_source(self):
        root = Parser(PYTHON).parse(self.SOURCE.encode()).root_node
        self.assertEqual("pass\n", Edits(self.SOURCE).replace(root, "pass\n").apply())

    def test_delete_the_first_node_and_insert_at_the_last(self):
        nodes = self.names()
        result = Edits(self.SOURCE).delete(nodes[0]).insert_after(nodes[1], "_x").apply()
        self.assertEqual("def (): pass\ndef beta_x(): pass\n", result)


class TestInsertionConflictRules(TestCase):
    """The documented judgement calls about zero-width insertions.

    * Same point, two insertions: allowed, applied in queue order.
    * Strictly inside a replaced range: refused -- the anchor text is destroyed.
    * At a boundary of a replaced range: allowed, and ordered so that
      ``insert_before`` lands outside the replacement text.
    """

    SOURCE = "def alpha(): pass\ndef beta(): pass\n"
    SEXP = "(function_definition name: (identifier) @n)"

    def names(self):
        return captured(PYTHON, self.SOURCE, self.SEXP, "n")

    def test_two_insertions_at_the_same_point_are_allowed_in_queue_order(self):
        node = self.names()[0]
        result = Edits(self.SOURCE).insert_before(node, "A").insert_before(node, "B").apply()
        self.assertEqual("def ABalpha(): pass\ndef beta(): pass\n", result)

    def test_same_point_insertion_order_is_the_reverse_when_queued_the_other_way(self):
        node = self.names()[0]
        result = Edits(self.SOURCE).insert_before(node, "B").insert_before(node, "A").apply()
        self.assertEqual("def BAalpha(): pass\ndef beta(): pass\n", result)

    def test_three_insertions_at_the_same_point_stay_in_queue_order(self):
        node = self.names()[0]
        edits = Edits(self.SOURCE)
        for text in ("1", "2", "3"):
            edits.insert_before(node, text)
        self.assertEqual("def 123alpha(): pass\ndef beta(): pass\n", edits.apply())

    def test_insert_after_one_node_and_before_the_next_are_independent(self):
        nodes = self.names()
        result = Edits(self.SOURCE).insert_after(nodes[0], "!").insert_before(nodes[1], "?").apply()
        self.assertEqual("def alpha!(): pass\ndef ?beta(): pass\n", result)

    def test_insertion_strictly_inside_a_replaced_range_raises(self):
        """The replacement destroys the text the insertion anchors to."""
        func = captured(PYTHON, self.SOURCE, "(function_definition) @f", "f")[0]
        inner = self.names()[0]
        self.assertLess(func.start_byte, inner.start_byte)
        self.assertLess(inner.start_byte, func.end_byte)
        edits = Edits(self.SOURCE).replace(func, "def x(): pass").insert_before(inner, "Z")
        with self.assertRaises(OverlappingEditError) as ctx:
            edits.apply()
        self.assertIn("insert at", str(ctx.exception))

    def test_insert_before_at_the_start_boundary_of_a_replacement_is_outside_it(self):
        node = self.names()[0]
        result = Edits(self.SOURCE).replace(node, "renamed").insert_before(node, "# ").apply()
        self.assertEqual("def # renamed(): pass\ndef beta(): pass\n", result)

    def test_insert_after_at_the_end_boundary_of_a_replacement_follows_it(self):
        node = self.names()[0]
        result = Edits(self.SOURCE).replace(node, "renamed").insert_after(node, "_x").apply()
        self.assertEqual("def renamed_x(): pass\ndef beta(): pass\n", result)

    def test_boundary_insertions_are_order_independent_relative_to_the_replacement(self):
        node = self.names()[0]
        a = Edits(self.SOURCE).replace(node, "R").insert_before(node, "<").apply()
        b = Edits(self.SOURCE).insert_before(node, "<").replace(node, "R").apply()
        self.assertEqual(a, b)
        self.assertEqual("def <R(): pass\ndef beta(): pass\n", a)

    def test_wrap_a_replacement_on_both_boundaries(self):
        node = self.names()[1]
        result = (
            Edits(self.SOURCE)
            .insert_before(node, "(")
            .replace(node, "GAMMA")
            .insert_after(node, ")")
            .apply()
        )
        self.assertEqual("def alpha(): pass\ndef (GAMMA)(): pass\n", result)

    def test_insertion_at_a_deleted_nodes_boundary_survives(self):
        """Deletion is replacement with ""; boundary insertions still anchor."""
        node = self.names()[0]
        result = Edits(self.SOURCE).delete(node).insert_before(node, "gone").apply()
        self.assertEqual("def gone(): pass\ndef beta(): pass\n", result)


class TestApplySemantics(TestCase):
    SOURCE = "def alpha(): pass\ndef beta(): pass\n"
    SEXP = "(function_definition name: (identifier) @n)"

    def names(self):
        return captured(PYTHON, self.SOURCE, self.SEXP, "n")

    def test_empty_edits_returns_source_unchanged(self):
        self.assertEqual(self.SOURCE, Edits(self.SOURCE).apply())

    def test_empty_edits_on_bytes_returns_bytes_unchanged(self):
        raw = self.SOURCE.encode()
        self.assertEqual(raw, Edits(raw).apply())

    def test_apply_is_idempotent_and_object_stays_usable(self):
        edits = Edits(self.SOURCE).replace(self.names()[0], "renamed")
        expected = "def renamed(): pass\ndef beta(): pass\n"
        self.assertEqual(expected, edits.apply())
        self.assertEqual(expected, edits.apply())
        self.assertEqual(1, len(edits))

        edits.replace(self.names()[1], "also")
        self.assertEqual("def renamed(): pass\ndef also(): pass\n", edits.apply())
        self.assertEqual(2, len(edits))

    def test_str_in_str_out(self):
        result = Edits(self.SOURCE).replace(self.names()[0], "x").apply()
        self.assertIsInstance(result, str)

    def test_bytes_in_bytes_out(self):
        result = Edits(self.SOURCE.encode()).replace(self.names()[0], "x").apply()
        self.assertIsInstance(result, bytes)
        self.assertEqual(b"def x(): pass\ndef beta(): pass\n", result)

    def test_bytes_and_str_produce_equivalent_output(self):
        nodes = self.names()
        as_str = Edits(self.SOURCE).replace_all(nodes, "z").apply()
        as_bytes = Edits(self.SOURCE.encode()).replace_all(nodes, "z").apply()
        self.assertEqual(as_str.encode(), as_bytes)

    def test_bytearray_source_is_accepted_and_returns_bytes(self):
        result = Edits(bytearray(self.SOURCE.encode())).replace(self.names()[0], "x").apply()
        self.assertIsInstance(result, bytes)
        self.assertEqual(b"def x(): pass\ndef beta(): pass\n", result)

    def test_len_and_bool(self):
        edits = Edits(self.SOURCE)
        self.assertEqual(0, len(edits))
        self.assertFalse(edits)
        edits.replace(self.names()[0], "x")
        self.assertEqual(1, len(edits))
        self.assertTrue(edits)

    def test_fluent_chaining_returns_the_same_object(self):
        edits = Edits(self.SOURCE)
        nodes = self.names()
        chained = (
            edits.replace(nodes[0], "a")
            .insert_before(nodes[1], "b")
            .insert_after(nodes[1], "c")
            .replace_all([], "unused")
        )
        self.assertIs(edits, chained)
        self.assertEqual("def a(): pass\ndef bbetac(): pass\n", chained.apply())

    def test_replace_all_with_an_iterator(self):
        result = Edits(self.SOURCE).replace_all(iter(self.names()), "q").apply()
        self.assertEqual("def q(): pass\ndef q(): pass\n", result)

    def test_replace_all_with_no_nodes_queues_nothing(self):
        edits = Edits(self.SOURCE).replace_all([], "x")
        self.assertEqual(0, len(edits))
        self.assertEqual(self.SOURCE, edits.apply())

    def test_nodes_from_a_shorter_source_are_rejected(self):
        node = captured(PYTHON, self.SOURCE, self.SEXP, "n")[-1]
        with self.assertRaises(ValueError) as ctx:
            Edits("def a(): pass\n").replace(node, "x")
        self.assertIn("different source", str(ctx.exception))

    def test_nodes_from_a_same_length_source_are_rejected(self):
        """A range check alone cannot catch this -- the offsets are all in bounds.

        Without comparing the node's own text to the source, this silently edits
        the wrong span, which is the exact failure mode this class exists to stop.
        """
        node = captured(PYTHON, "def foo(): pass\n", self.SEXP, "n")[0]
        other = "def zzz(): pass\n"  # same length, different text
        self.assertEqual(len(other), len("def foo(): pass\n"))
        with self.assertRaises(ValueError) as ctx:
            Edits(other).replace(node, "x")
        message = str(ctx.exception)
        self.assertIn("different source", message)
        self.assertIn("zzz", message)
        self.assertIn("foo", message)

    def test_insertions_also_validate_the_node(self):
        node = captured(PYTHON, "def foo(): pass\n", self.SEXP, "n")[0]
        for method in ("insert_before", "insert_after", "delete"):
            with self.subTest(method=method):
                edits = Edits("def zzz(): pass\n")
                args = () if method == "delete" else ("x",)
                with self.assertRaises(ValueError):
                    getattr(edits, method)(node, *args)

    def test_a_node_from_the_same_source_is_accepted(self):
        """The guard must not reject legitimate nodes."""
        node = captured(PYTHON, self.SOURCE, self.SEXP, "n")[0]
        self.assertEqual(1, len(Edits(self.SOURCE).replace(node, "x")))
        self.assertEqual(1, len(Edits(self.SOURCE.encode()).replace(node, "x")))

    def test_repr_mentions_the_queue_size(self):
        edits = Edits(self.SOURCE).replace(self.names()[0], "x")
        self.assertIn("1 queued", repr(edits))


class TestEditsDecoupling(TestCase):
    """``Edits`` must work with nodes from anywhere, not just template queries."""

    def test_plain_hand_written_query_renames_both_functions(self):
        source = "def fetch(url):\n    return url\n\ndef save(path):\n    return path\n"
        tree = Parser(PYTHON).parse(source.encode())
        cursor = QueryCursor(Query(PYTHON, "(function_definition name: (identifier) @n)"))
        nodes = sorted(cursor.captures(tree.root_node)["n"], key=lambda n: n.byte_range)
        self.assertEqual(["fetch", "save"], [n.text.decode() for n in nodes])

        edits = Edits(source)
        for node in nodes:
            edits.replace(node, f"do_{node.text.decode()}")

        self.assertEqual(
            "def do_fetch(url):\n    return url\n\ndef do_save(path):\n    return path\n",
            edits.apply(),
        )

    def test_manual_tree_walk_supplies_nodes(self):
        """No query at all -- just walking the tree."""
        source = '{"a":1,"b":2}'
        root = Parser(JSON).parse(source.encode()).root_node

        numbers = []
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type == "number":
                numbers.append(node)
            stack.extend(node.children)

        self.assertEqual(2, len(numbers))
        self.assertEqual('{"a":0,"b":0}', Edits(source).replace_all(numbers, "0").apply())

    def test_module_does_not_import_the_template_pipeline(self):
        """The decoupling is a design constraint, so assert it directly."""
        import ast
        import pathlib

        import tree_sitter.template._edits as edits_module

        path = pathlib.Path(edits_module.__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

        for forbidden in ("_compile", "_render", "_query", "_holes", "_sexp"):
            with self.subTest(module=forbidden):
                self.assertNotIn(forbidden, imported)
                self.assertNotIn(f"._{forbidden.lstrip('_')}", imported)


class TestTemplateIntegration(TestCase):
    """The template-query side: ``Match`` nodes and ``TemplateQuery.edit``."""

    TERRAFORM = (
        'resource "aws_s3_bucket" "logs" {\n'
        '  acl = "private"\n'
        "}\n"
        "\n"
        'resource "aws_s3_bucket" "assets" {\n'
        '  acl = "public-read"\n'
        "}\n"
        "\n"
        'resource "aws_iam_role" "runner" {\n'
        '  acl = "private"\n'
        "}\n"
    )

    def test_template_query_edit_returns_an_edits_for_the_source(self):
        q = query(HCL, t'resource "aws_s3_bucket" {capture("name")} {{ {...} }}')
        edits = q.edit(self.TERRAFORM)
        self.assertIsInstance(edits, Edits)
        self.assertEqual(0, len(edits))
        self.assertEqual(self.TERRAFORM, edits.apply())

    def test_rename_every_matching_resource_end_to_end(self):
        """The realistic case: rename all aws_s3_bucket resources, leave others alone."""
        q = query(HCL, t'resource "aws_s3_bucket" {capture("name")} {{ {...} }}')
        matches = q.matches(self.TERRAFORM)
        self.assertEqual(2, len(matches))

        edits = q.edit(self.TERRAFORM)
        for match in matches:
            node = match["name"]
            edits.replace(node, f'"legacy_{node.text.decode().strip(chr(34))}"')

        self.assertEqual(
            'resource "aws_s3_bucket" "legacy_logs" {\n'
            '  acl = "private"\n'
            "}\n"
            "\n"
            'resource "aws_s3_bucket" "legacy_assets" {\n'
            '  acl = "public-read"\n'
            "}\n"
            "\n"
            'resource "aws_iam_role" "runner" {\n'
            '  acl = "private"\n'
            "}\n",
            edits.apply(),
        )

    def test_rename_the_resource_type_itself(self):
        q = query(HCL, t"resource {capture('type')} {capture('name')} {{ {...} }}")
        edits = Edits(self.TERRAFORM)
        for match in q.matches(self.TERRAFORM):
            if match.text("type") == '"aws_s3_bucket"':
                edits.replace(match["type"], '"aws_s3_bucket_v2"')
        self.assertEqual(2, len(edits))
        result = edits.apply()
        self.assertEqual(2, result.count('"aws_s3_bucket_v2"'))
        self.assertIn('resource "aws_iam_role" "runner"', result)

    def test_replace_all_with_match_all_for_a_quantified_capture(self):
        """``match[name]`` gives only the first node; ``match.all`` gives them all."""
        source = "def f():\n    return g(1, 2, 3)\n"
        q = query(PYTHON, t"def f():\n    return g({capture('args', quantifier='+')})")
        match = q.first(source)
        self.assertIsNotNone(match)
        nodes = match.all("args")
        self.assertGreater(len(nodes), 1)

        # Indexing silently gives you only the first -- the trap replace_all closes.
        only_first = Edits(source).replace(match["args"], "0").apply()
        self.assertEqual(1, only_first.count("0"))

        all_of_them = Edits(source).replace_all(nodes, "0").apply()
        self.assertEqual(len(nodes), all_of_them.count("0"))
        self.assertNotEqual(only_first, all_of_them)

    def test_edits_is_exported_from_the_template_package(self):
        import tree_sitter.template as template_package

        self.assertIn("Edits", template_package.__all__)
        self.assertIn("OverlappingEditError", template_package.__all__)
        self.assertIs(Edits, template_package.Edits)
        self.assertIs(OverlappingEditError, template_package.OverlappingEditError)


if __name__ == "__main__":
    unittest.main()
