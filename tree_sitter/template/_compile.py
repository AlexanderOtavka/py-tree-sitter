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
from ._sexp import AnonNode, NamedNode, Pattern, Predicate, Sexp, quote

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


#: Separator literals tried when widening a quantified capture. A template only
#: ever shows one element, so the separator cannot be read off it -- it has to be
#: guessed and then confirmed against the grammar.
_SEPARATOR_CANDIDATES = (",", ";", "|", "&&", "||", "+")


_UNSET = object()


def _find_kind(node: Node, kind: str) -> Node | None:
    """The first node of type *kind* at or below *node*."""
    if node.type == kind:
        return node
    for child in node.children:
        found = _find_kind(child, kind)
        if found is not None:
            return found
    return None


def _separator_text(children: list[Node]) -> str | None:
    """The repeated anonymous separator joining *children*, if visible.

    A separator is an anonymous child sitting strictly between two others; the
    first and last children are delimiters, not separators. Returns ``None`` when
    the sequence shows no separator, which is the common case for a template
    (it lists one element, so there is nothing to separate).
    """
    if len(children) < 3:
        return None
    inner = children[1:-1]
    texts = {child.text.decode("utf-8", "replace") for child in inner if not child.is_named}
    if len(texts) != 1:
        return None
    return texts.pop()


def _has_untiled_text(node: Node, children: list[Node]) -> bool:
    """True when *children* leave meaningful text of *node* unaccounted for.

    Any byte of the node not claimed by a child is literal text the pattern would
    otherwise leave unconstrained. Whitespace between children does not count:
    it is layout, and the matched source is free to lay itself out differently,
    so pinning it would reject equivalent code.
    """
    text = node.text
    base = node.start_byte
    gaps: list[bytes] = []
    cursor = base
    for child in children:
        if child.start_byte > cursor:
            gaps.append(text[cursor - base : child.start_byte - base])
        cursor = max(cursor, child.end_byte)
    if cursor < node.end_byte:
        gaps.append(text[cursor - base :])
    return any(gap.strip() for gap in gaps)


def _non_extra_children(node: Node) -> list[Node]:
    return [c for c in node.children if not c.is_extra]


def orphaned_separators(parent: Node, children: list[Node], holes: set[int]) -> set[int]:
    """Indices of separator children left dangling by removed ``AnyChildren`` holes.

    A hole's subtree is dropped from the pattern, but the punctuation that
    attached it to its neighbours is a sibling in its own right and would survive
    -- turning the "extra children are allowed here" hole into a demand for them.
    ``f(a, {...})`` would compile to ``(argument_list "(" (_) @a "," ")")``, whose
    trailing comma only matches a call that really has a second argument.

    A *separator* is derived from the grammar rather than from a per-language list
    of punctuation, so ``;`` or ``|`` work as well as ``,``. It must be:

    * **anonymous** -- named children are meaningful content, never glue;
    * **strictly interior** -- the first and last children of a sequence are
      structural delimiters (``"("``/``")"`` of an ``argument_list``,
      ``"{"``/``"}"`` of a JSON ``object``) and deleting them would be wrong;
    * **field-free, between field-free neighbours** -- a separator joins members
      of a homogeneous list, which grammars leave unnamed. Punctuation that
      structures a *heterogeneous* rule is either a field itself (the ``"+"`` of
      Python's ``binary_operator`` is its ``operator`` field) or sits between two
      fields (the ``":"`` of an ``if_statement`` separates ``condition`` from
      ``consequence``). Both must survive: dropping the colon leaves a pattern
      the grammar can never match.

    Each hole orphans at most *one* separator, so a hole in the middle
    (``f(a, {...}, b)``) still leaves the single comma that joins its surviving
    neighbours. The preceding separator is dropped by preference, with the
    following one as the fallback -- which is what makes a hole at the start of a
    sequence give up the separator after it instead. A hole that is a sequence's
    only real child (``f({...})``) sits between two delimiters and so orphans
    nothing, and a separator-less grammar (an HCL ``body``) never loses anything.
    """
    if not holes:
        return set()
    last = len(children) - 1
    fields = {id(c): parent.field_name_for_child(i) for i, c in enumerate(parent.children)}

    def field_free(index: int) -> bool:
        return fields.get(id(children[index])) is None

    def is_separator(index: int) -> bool:
        if not (0 < index < last) or index in holes:
            return False
        if children[index].is_named or not field_free(index):
            return False
        return field_free(index - 1) and field_free(index + 1)

    dropped: set[int] = set()
    for hole in sorted(holes):
        for candidate in (hole - 1, hole + 1):
            if candidate not in dropped and is_separator(candidate):
                dropped.add(candidate)
                break
    return dropped


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
        self._extras_ok: dict[tuple, Any] = {}
        self._separators: dict[str, Any] = {}
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
            cached = self._fit_extras(node)
            self._extras_ok[shape] = cached
        node.extras, node.no_extras_at = cached

    def _fit_extras(self, node: NamedNode) -> tuple[tuple[str, ...], frozenset[int]]:
        """Work out the largest extras set and gap positions *node* accepts.

        Both dimensions have to be narrowed independently, because a single
        rejection used to cost the node its comment tolerance entirely:

        * kinds -- Rust names three comment kinds but ``(block (doc_comment)*)``
          is impossible, and one bad member poisons the whole alternation.
        * positions -- JS accepts a comment between an argument list's arguments
          but not before its ``(``.
        """
        kinds = tuple(k for k in self.extras if self._accepts_kind(node.kind, k))
        if not kinds:
            return (), frozenset()

        # An extras run immediately before a trailing *anonymous* delimiter can
        # absorb real siblings on its way to it, which would let `def f(a)` match
        # `def f(a, b)`. Named trailing children do not have that problem -- a
        # comment before HCL's `block_end` sits exactly there and must be
        # tolerated -- so the exclusion is keyed on the delimiter being anonymous.
        base: set[int] = set()
        if node.children and isinstance(node.children[-1], AnonNode):
            base.add(len(node.children) - 1)
        # Nor may a run sit before an untyped `(_)` wildcard: the wildcard would
        # happily bind to a comment, so the run slides along and an extra real
        # child slips in behind it (`def f(a)` matching `def f(a, b)`).
        for i, child in enumerate(node.children):
            if isinstance(child, NamedNode) and child.kind is None and not child.children:
                base.add(i)

        frozen_base = frozenset(base)
        node.extras = kinds
        node.no_extras_at = frozen_base
        if self._compiles(node):
            return kinds, frozen_base

        # Narrow to the positions that do compile. Probing each gap on its own
        # tells us which single positions are legal; the union of those is then
        # verified once, since legality can interact.
        blocked = set(base) | {
            i
            for i in range(len(node.children))
            if i not in base and not self._accepts_gap_at(node, kinds, i)
        }
        node.no_extras_at = frozenset(blocked)
        if self._compiles(node):
            return kinds, frozenset(blocked)

        return (), frozenset()

    def _accepts_kind(self, parent: str | None, kind: str) -> bool:
        from tree_sitter import Query

        if parent is None:
            return True
        try:
            Query(self.language, f"({parent} ({kind})*)")
        except Exception:
            return False
        return True

    def _accepts_gap_at(self, node: NamedNode, kinds: tuple[str, ...], index: int) -> bool:
        node.extras = kinds
        node.no_extras_at = frozenset(i for i in range(len(node.children)) if i != index)
        return self._compiles(node)

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

    def _identifiable(self, children: list[Sexp]) -> bool:
        """True when every child can be recognised without relying on position.

        Removing a separator costs the sequence the exactness that separator was
        accidentally providing, so the anchors have to stay -- but anchors also
        pin *where* each child sits. That is right for a child the pattern can
        only find by position and wrong for one it can find by identity.

        Identity here means a *type*: an anonymous literal or a named kind can be
        looked for wherever it sits. JSON's surviving ``(pair ...)`` is such a
        child, which is why fully un-anchoring the object is safe and is what lets
        ``"version"`` be found as the first, last, *or* a middle member.

        A bare ``(_)`` wildcard has no type to search by, so only its position
        distinguishes it. In ``f({capture('a')}, {...})`` the ``(_) @a`` would bind
        to *any* argument and ``requests.get(url, timeout=5)`` would yield a
        spurious second match with ``@a = "timeout=5"``. There the anchors must
        stay, with only the vacated gap open, holding ``@a`` to the first argument.
        """
        return not any(
            isinstance(child, NamedNode) and child.kind is None and not child.children
            for child in children
        )

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
    def _span_separators(self, children: list[Sexp], raw: list[Node]) -> None:
        """Let a ``*``/``+`` quantified child absorb the list's separator.

        ``(_)*`` matches only the named siblings, so in a comma-separated list it
        stops at the first ``,`` and ``f({capture('a', quantifier='*')})`` quietly
        returns nothing for ``f(a, b)``. Widening it to ``[(_) @a ","]*`` -- with
        the capture inside, on the node branch, so the separators are matched but
        not captured -- makes it span the whole list.

        Only applies where a separator actually exists; sequences without one
        (Python statement blocks, HCL block bodies) already work.
        """
        quantified = [
            child
            for child in children
            if isinstance(child, NamedNode)
            and child.quantifier in ("*", "+")
            and not child.children
        ]
        if not quantified:
            return

        separator = _separator_text(raw) or self._guess_separator(raw)
        if separator is None:
            return
        for child in quantified:
            child.alternatives = (separator,)

    def _guess_separator(self, raw: list[Node]) -> str | None:
        """Discover the separator this node's kind uses, by experiment.

        A template lists a single element, so its own text never shows the
        separator, and compiling is not a discriminator either -- tree-sitter
        accepts any anonymous literal inside an alternation. So try each
        candidate for real: splice a second copy of the element into the
        template's text with the candidate between them, reparse, and keep the
        candidate that both parses cleanly and yields the expected two elements.
        """
        if self.language is None or len(raw) < 2:
            return None
        parent = raw[0].parent
        if parent is None:
            return None
        cached = self._separators.get(parent.type, _UNSET)
        if cached is not _UNSET:
            return cached

        element = next((child for child in raw if child.is_named), None)
        found = None
        if element is not None:
            found = self._probe_separator(parent, element)
        self._separators[parent.type] = found
        return found

    def _probe_separator(self, parent: Node, element: Node) -> str | None:
        from tree_sitter import Parser

        parser = Parser(self.language)
        # Splice into the *whole* template, not just this node's text: a node's
        # text taken alone often reparses as something else entirely (Python's
        # `(a, b)` is a tuple, not an argument list).
        text = self.source
        cut = element.end_byte
        piece = text[element.start_byte : element.end_byte]
        before = len(_non_extra_children(parent))

        for candidate in _SEPARATOR_CANDIDATES:
            probe = text[:cut] + candidate.encode() + piece + text[cut:]
            tree = parser.parse(probe)
            if tree.root_node.has_error:
                continue
            grown = _find_kind(tree.root_node, parent.type)
            if grown is not None and len(_non_extra_children(grown)) == before + 2:
                return candidate
        return None

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

        raw = _non_extra_children(node)
        holes = {
            i
            for i, child in enumerate(raw)
            if isinstance(getattr(self.targets.get(child.id), "hole", None), AnyChildren)
        }
        # An AnyChildren hole is emitted as nothing, so the separator that joined
        # it to its neighbours has to go with it -- otherwise the "extra children
        # allowed here" hole silently demands them. See orphaned_separators.
        skip = holes | orphaned_separators(node, raw, holes)

        children: list[Sexp] = []
        open_gaps: set[int] = set()
        for i, child in enumerate(raw):
            if i in skip:
                # Record the gap the removed run vacated, so the sequence can
                # stay anchored everywhere *except* there.
                open_gaps.add(len(children))
                continue
            compiled = self.compile_node(child)
            if compiled is not None:
                children.append(compiled)

        self._span_separators(children, raw)

        anchored = not holes
        if holes and children and not self._identifiable(children):
            # A surviving child that the query can only find by position needs
            # its anchors, so keep them and open just the gaps the holes vacated.
            # That holds `@a` to the first argument in `f({capture('a')}, {...})`
            # instead of letting it float onto any argument.
            #
            # Only worth doing when a gap really opened: a separator-less sequence
            # such as an HCL `body` records none, and keeping its anchors would
            # make `...` mean nothing at all.
            anchored = bool(open_gaps)

        sexp = NamedNode(kind=node.type, children=children, anchored=anchored)
        if anchored:
            if holes:
                sexp.open_gaps = frozenset(open_gaps)
            # Only anchored sequences reject comments, so only they need extras.
            self.allow_extras(sexp)
        else:
            self.allow_skip_siblings(sexp)

        children_of = _non_extra_children(node)
        if not children_of:
            # Rule 3: a named leaf's type does not constrain its text.
            self._pin_text(sexp, node)
        elif not self._covers_a_hole(node) and _has_untiled_text(node, children_of):
            # The children do not account for all of this node's text, so the
            # uncovered literal part is constrained by nothing: Python parses
            # `"a\nb"`'s content as a `string_content` holding one
            # `escape_sequence`, leaving the `a` and `b` unpinned, and the
            # pattern would match `"Xa\nbY"`. That text is not a node, so
            # anchors cannot help -- pin the whole node's text instead.
            #
            # Only safe when nothing below is a hole: pinning the full text of a
            # subtree containing a capture would contradict the capture.
            self._pin_text(sexp, node)
        return sexp

    def _covers_a_hole(self, node: Node) -> bool:
        """True when any hole's span falls inside *node*.

        Used to keep text pinning off subtrees containing a capture: the captured
        text is by definition not known from the template.
        """
        return any(
            node.start_byte <= slot.start and slot.end <= node.end_byte
            for slot in self.targets.values()
        )

    def _pin_text(self, sexp: NamedNode, node: Node) -> None:
        name = self._private_name()
        sexp.captures.append(name)
        self.predicates.append(Predicate("eq?", [f"@{name}", quote(self._text(node))]))


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
