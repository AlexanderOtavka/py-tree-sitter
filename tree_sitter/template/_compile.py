"""Step 3 of the pipeline: parse tree + hole slots -> query :class:`Pattern`.

The template has already been rendered to plain source text with sentinel
identifiers standing in for the user's interpolations (step 1) and parsed with
the *target* grammar (step 2). This module walks that parse tree and emits the
equivalent tree-sitter query pattern.

Four rules, each validated experimentally against real grammars:

1. **Emit every child, anonymous ones included.** Anonymous nodes are
   semantically load-bearing: ``x + y`` and ``x - y`` differ *only* by an
   anonymous ``"+"``/``"-"``, so filtering to named children would silently
   widen the pattern.

2. **Anchor child sequences by default.** ``.`` anchors bracket and separate the
   children so the pattern means "a node with *exactly* these children". Anchors
   do **not** skip named punctuation -- HCL's ``block_start``/``block_end`` are
   *named* nodes -- which is precisely why rule 1 must emit everything. Anchoring
   is switched off for one node only: a node with an :class:`AnyChildren` hole
   among its direct children.

3. **Pin named leaf text with ``#eq?``.** A named node with no children matches
   any text of that type, so it gets a private capture (``@_h0``, ``@_h1``, ...)
   plus an ``(#eq? @_h0 "text")`` predicate. Anonymous nodes never need this --
   their text is already in the pattern literally. The rule is deliberately
   uniform ("every named zero-child node gets an ``#eq?``") rather than trying to
   detect leaves whose text is implied by their type: a redundant ``#eq?`` (e.g.
   on HCL's ``quoted_template_start``, whose text is always ``"``) is harmless,
   while a *missing* one silently widens the pattern. The ``language`` argument
   is accepted for API stability and for future type-implies-text detection; the
   uniform rule does not need it.

4. **Holes replace subtrees.** A slot's byte span selects the outermost node
   fully contained in that span, and that node's whole subtree is replaced by the
   hole's pattern. A slot matching no node is a :class:`TemplateCompileError`.

Nodes with ``is_extra`` set (comments) are skipped throughout, so a commented
template does not demand comments in the matched source.

Predicate placement
-------------------
``_sexp.Pattern.render`` writes predicates *after* the root's closing paren,
which tree-sitter parses as a second, separate pattern -- the predicate then
constrains nothing. :func:`render_pattern` therefore emits the group-wrapped
form ``((root ...) (#eq? ...))``, which was verified to produce exactly one
pattern and to filter correctly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ._errors import TemplateCompileError
from ._holes import Alternatives, AnyChildren, Capture, HoleSlot, Wildcard
from ._sexp import AnonNode, NamedNode, Pattern, Predicate, Sexp, extras_pattern, quote

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tree_sitter import Language, Node, Tree

__all__ = ["CompileResult", "compile_tree", "find_pattern_root", "render_pattern"]


@dataclass
class CompileResult:
    """The compiled query for one template.

    Attributes
    ----------
    pattern : Pattern
        The S-expression tree, predicates collected on ``pattern.predicates``.
    text : str
        Query text ready to hand to :class:`tree_sitter.Query`.
    capture_names : list[str]
        Public capture names, in first-appearance order.
    private_names : list[str]
        Internal ``_h*`` capture names used for ``#eq?`` pinning and for
        unnamed :class:`Alternatives`. Hidden from user-visible results.
    """

    pattern: Pattern
    text: str
    capture_names: list[str] = field(default_factory=list)
    private_names: list[str] = field(default_factory=list)


def render_pattern(pattern: Pattern) -> str:
    """Render *pattern* to query text.

    Kept as a named helper because it is the single place the compiler turns an
    S-expression tree into text; the group-wrapping that predicates require now
    lives in :meth:`Pattern.render` itself.
    """
    return pattern.render()


#: Node kinds treated as skippable extras even when a grammar names them
#: something other than plain ``comment``.
_EXTRA_KIND_HINTS = ("comment",)


def extra_kinds(language: Any) -> tuple[str, ...]:
    """Return the node kinds that may appear anywhere in a child sequence.

    Anchored patterns otherwise reject them, so a template would stop matching
    source code merely because a comment was added to it. Kinds are discovered
    from the grammar's node-kind table rather than hardcoded per language, so
    grammars that split comments into several kinds (Rust's ``line_comment`` and
    ``block_comment``) are handled too.

    Doc-comment *markers* are deliberately excluded: they are components of a
    doc comment rather than free-floating extras, and including them would widen
    patterns for no benefit.
    """
    if language is None:
        return ()
    try:
        count = language.node_kind_count
    except AttributeError:  # pragma: no cover - defensive
        return ()

    found: list[str] = []
    for kind_id in range(count):
        kind = language.node_kind_for_id(kind_id)
        if not kind or kind in found:
            continue
        if not language.node_kind_is_named(kind_id):
            continue
        if "marker" in kind:
            continue
        if any(hint in kind for hint in _EXTRA_KIND_HINTS):
            found.append(kind)
    return tuple(found)


def _shape_of(child: Sexp) -> tuple:
    """A cache key describing a child's shape, ignoring captures and text."""
    if isinstance(child, NamedNode):
        return ("n", child.kind, child.quantifier, len(child.children))
    if isinstance(child, AnonNode):
        return ("a", child.text)
    return ("?",)


def _non_extra_children(node: Node) -> list[Node]:
    return [c for c in node.children if not c.is_extra]


def _slot_node(root: Node, start: int, end: int) -> Node | None:
    """Return the outermost non-extra node fully contained in ``[start, end)``.

    Breadth-first from *root*, descending only into children that overlap the
    span, so the first fully-contained node found is the shallowest -- i.e. the
    outermost. This is what makes a padded hole (``TSQH1 = 0``) select the whole
    ``attribute`` node rather than just the sentinel ``identifier``.
    """
    queue = [root]
    while queue:
        node = queue.pop(0)
        if not node.is_extra and start <= node.start_byte and node.end_byte <= end:
            return node
        for child in node.children:
            if child.end_byte > start and child.start_byte < end:
                queue.append(child)
    return None


def _slot_map(root: Node, slots: list[HoleSlot]) -> dict[int, HoleSlot]:
    """Map ``node.id`` -> slot for every hole, raising if a slot matches nothing."""
    mapping: dict[int, HoleSlot] = {}
    for slot in slots:
        node = _slot_node(root, slot.start, slot.end)
        if node is None:
            raise TemplateCompileError(
                f"hole {slot.index} (sentinel {slot.sentinel!r}) spans bytes "
                f"{slot.start}..{slot.end}, which matches no node in the parsed "
                f"template"
            )
        mapping[node.id] = slot
    return mapping


def find_pattern_root(root: Node, slots: list[HoleSlot] | None = None) -> Node:
    """Pick the node to use as the top of the generated pattern.

    A template parse is normally wrapped in one or more container nodes
    (``config_file`` -> ``body`` -> ``block`` in HCL, ``module`` in Python,
    ``document`` in JSON) that carry no information worth matching on. Descend
    while the current node has exactly one non-extra child and is not itself the
    target of a hole; stop at the first node with 2+ non-extra children, at a
    leaf, or at a hole target.
    """
    targets = _slot_map(root, list(slots or []))
    node = root
    while True:
        if node.id in targets:
            return node
        children = _non_extra_children(node)
        if len(children) != 1:
            return node
        node = children[0]


class _Compiler:
    def __init__(self, source: bytes, targets: dict[int, HoleSlot], language: Any):
        self.source = source
        self.targets = targets
        self.language = language
        self.extras = extra_kinds(language)
        self._extras_ok: dict[tuple, bool] = {}
        self.predicates: list[Predicate] = []
        self.capture_names: list[str] = []
        self.private_names: list[str] = []
        self._counter = 0

    def allow_extras(self, node: NamedNode) -> None:
        """Permit extras inside *node* if the grammar accepts them there.

        Comments cannot appear just anywhere: HCL allows one between a block's
        children but not between the quotes and contents of a ``string_lit``, and
        emitting one where the grammar forbids it makes tree-sitter reject the
        entire pattern as an "Impossible pattern". Whether a given position
        admits a comment depends on the node's *actual children*, not just its
        kind, so ask the grammar directly: set the extras, try to compile that
        one subpattern, and roll back if it is rejected.

        The result is cached per (kind, child-shape) because a template
        repeats shapes often and each probe costs a query compilation.
        """
        if not self.extras or self.language is None:
            return
        shape = ("extras", node.kind, tuple(_shape_of(c) for c in node.children), node.anchored)
        cached = self._extras_ok.get(shape)
        if cached is None:
            node.extras = self.extras
            cached = self._compiles(node)
            self._extras_ok[shape] = cached
        if cached:
            node.extras = self.extras
        else:
            node.extras = ()

    def allow_skip_siblings(self, node: NamedNode) -> None:
        """Let an un-anchored *node* skip unlisted earlier siblings.

        Without a leading ``(_)*`` tree-sitter lines the listed children up
        against the node's *first* children, so a ``...`` body would only match
        when the attribute the user wrote happens to come first. As with extras,
        some nodes reject ``(_)*`` outright ("Impossible pattern"), so probe the
        grammar and roll back when it does not take.
        """
        if self.language is None:
            return
        shape = ("skip", node.kind, tuple(_shape_of(c) for c in node.children))
        cached = self._extras_ok.get(shape)
        if cached is None:
            node.skip_siblings = True
            cached = self._compiles(node)
            self._extras_ok[shape] = cached
        node.skip_siblings = cached

    def _compiles(self, node: NamedNode) -> bool:
        from tree_sitter import Query

        try:
            Query(self.language, node.render())
        except Exception:
            return False
        return True

    # -- names ---------------------------------------------------------------
    def _private_name(self) -> str:
        name = f"_h{self._counter}"
        self._counter += 1
        self.private_names.append(name)
        return name

    def _public_name(self, name: str) -> str:
        if name not in self.capture_names:
            self.capture_names.append(name)
        return name

    def _text(self, node: Node) -> str:
        return self.source[node.start_byte : node.end_byte].decode("utf-8")

    # -- holes ---------------------------------------------------------------
    def _hole_sexp(self, slot: HoleSlot) -> Sexp | None:
        """Build the pattern for one hole, or ``None`` to emit nothing."""
        hole = slot.hole
        if isinstance(hole, AnyChildren):
            return None
        if isinstance(hole, Capture):
            name = self._public_name(hole.name)
            if hole.pattern is not None:
                self.predicates.append(Predicate("match?", [f"@{name}", quote(hole.pattern)]))
            if hole.one_of is not None:
                self.predicates.append(
                    Predicate("any-of?", [f"@{name}", *(quote(t) for t in hole.one_of)])
                )
            return NamedNode(kind=hole.kind, captures=[name], quantifier=hole.quantifier)
        if isinstance(hole, Wildcard):
            return NamedNode(kind=hole.kind)
        if isinstance(hole, Alternatives):
            name = self._public_name(hole.name) if hole.name is not None else self._private_name()
            self.predicates.append(
                Predicate("any-of?", [f"@{name}", *(quote(t) for t in hole.texts)])
            )
            return NamedNode(kind=None, captures=[name])
        raise TemplateCompileError(f"unsupported hole type: {type(hole).__name__!r}")

    # -- tree walk -----------------------------------------------------------
    def compile_node(self, node: Node) -> Sexp | None:
        slot = self.targets.get(node.id)
        if slot is not None:
            return self._hole_sexp(slot)

        if not node.is_named:
            return AnonNode(text=self._text(node))

        children: list[Sexp] = []
        anchored = True
        for child in node.children:
            if child.is_extra:
                continue
            child_slot = self.targets.get(child.id)
            if child_slot is not None and isinstance(child_slot.hole, AnyChildren):
                anchored = False
                continue
            compiled = self.compile_node(child)
            if compiled is not None:
                children.append(compiled)

        sexp = NamedNode(kind=node.type, children=children, anchored=anchored)
        if anchored:
            # Only anchored sequences reject comments, so only they need extras.
            self.allow_extras(sexp)
        else:
            self.allow_skip_siblings(sexp)

        if not _non_extra_children(node):
            # Rule 3: a named leaf's type does not constrain its text.
            name = self._private_name()
            sexp.captures.append(name)
            self.predicates.append(Predicate("eq?", [f"@{name}", quote(self._text(node))]))
        return sexp


def compile_tree(
    tree: Tree,
    slots: list[HoleSlot],
    *,
    source: bytes,
    language: Language | None = None,
) -> CompileResult:
    """Compile a parsed template tree plus its hole slots into a query pattern.

    Parameters
    ----------
    tree :
        Parse of the *rendered* template text, produced with the target grammar.
    slots :
        One :class:`HoleSlot` per interpolation, with byte spans covering the
        sentinel *including* any padding the renderer added.
    source :
        The rendered template text as bytes -- the exact bytes ``tree`` was
        parsed from.
    language :
        The target language. Accepted for API stability; the uniform ``#eq?``
        rule (see module docstring) does not need it.

    Raises
    ------
    TemplateCompileError
        If a slot's byte span matches no node in the tree, or a hole type is not
        recognized.
    """
    slots = list(slots)
    root = find_pattern_root(tree.root_node, slots)
    targets = _slot_map(tree.root_node, slots)

    compiler = _Compiler(source=source, targets=targets, language=language)
    compiled = compiler.compile_node(root)
    if compiled is None:
        raise TemplateCompileError(
            "the template compiled to an empty pattern; a variadic hole cannot "
            "stand alone as the whole template"
        )

    pattern = Pattern(root=compiled, predicates=compiler.predicates)
    return CompileResult(
        pattern=pattern,
        text=render_pattern(pattern),
        capture_names=compiler.capture_names,
        private_names=compiler.private_names,
    )
