"""The user-facing template query API.

This module stitches the pipeline together: render the template to parseable
text, parse it with the *target* grammar, compile the resulting tree into a
tree-sitter query pattern, and run that pattern.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from tree_sitter import Language, Node, Parser, Query, QueryCursor, Tree

from ._compile import compile_tree
from ._errors import TemplateCompileError, TemplateError, TemplateSyntaxError
from ._render import render as render_template

if TYPE_CHECKING:
    from string.templatelib import Template

__all__ = ["Match", "TemplateQuery", "query"]


class Match:
    """One match of a template query.

    Captures are reachable by name. A name that matched a single node yields that
    node directly; use :meth:`all` when a capture may match several nodes (as
    with a ``*``/``+`` quantifier).
    """

    __slots__ = ("_captures", "_index")

    def __init__(self, index: int, captures: dict[str, list[Node]]):
        self._index = index
        self._captures = captures

    @property
    def pattern_index(self) -> int:
        """Index of the pattern that produced this match."""
        return self._index

    @property
    def captures(self) -> dict[str, list[Node]]:
        """All captures, as a mapping of name to list of nodes."""
        return self._captures

    def __getitem__(self, name: str) -> Node:
        """Return the single node captured as ``name``.

        Raises
        ------
        KeyError
            If nothing was captured under ``name``.
        """
        nodes = self._captures.get(name)
        if not nodes:
            raise KeyError(name)
        return nodes[0]

    def __contains__(self, name: str) -> bool:
        return bool(self._captures.get(name))

    def __iter__(self) -> Iterator[str]:
        return iter(self._captures)

    def get(self, name: str, default: Any = None) -> Node | Any:
        """Return the node captured as ``name``, or ``default``."""
        nodes = self._captures.get(name)
        return nodes[0] if nodes else default

    def all(self, name: str) -> list[Node]:
        """Return every node captured as ``name``."""
        return list(self._captures.get(name, ()))

    def text(self, name: str, encoding: str = "utf-8") -> str:
        """Return the source text of the node captured as ``name``."""
        return self[name].text.decode(encoding)

    def __repr__(self) -> str:
        preview = {
            name: [n.text.decode("utf-8", "replace") for n in nodes]
            for name, nodes in self._captures.items()
        }
        return f"<Match {preview}>"


class TemplateQuery:
    """A tree-sitter query built from a source-language template.

    Create one with :func:`query`. Inspect :attr:`sexp` to see the generated
    tree-sitter query, which is the fastest way to understand what a template
    actually matches.
    """

    __slots__ = ("_compiled", "_language", "_parser", "_query", "_rendered")

    def __init__(self, language: Language, template: Template):
        self._language = language
        self._parser = Parser(language)

        self._rendered = render_template(template, language, parser=self._parser)
        tree = self._parser.parse(self._rendered.text.encode())
        self._compiled = compile_tree(
            tree,
            self._rendered.slots,
            source=self._rendered.text.encode(),
            language=language,
        )
        try:
            self._query = Query(language, self._compiled.text)
        except Exception as exc:  # pragma: no cover - depends on grammar
            raise TemplateCompileError(
                f"generated query was rejected by tree-sitter: {exc}\n"
                f"--- generated query ---\n{self._compiled.text}\n"
                f"--- rendered template ---\n{self._rendered.text}"
            ) from exc

    @property
    def language(self) -> Language:
        """The language this query runs against."""
        return self._language

    @property
    def sexp(self) -> str:
        """The generated tree-sitter query text."""
        return self._compiled.text

    @property
    def rendered_template(self) -> str:
        """The template text that was parsed, with sentinels substituted in."""
        return self._rendered.text

    @property
    def capture_names(self) -> list[str]:
        """Public capture names, in order of first appearance."""
        return list(self._compiled.capture_names)

    @property
    def query(self) -> Query:
        """The underlying :class:`tree_sitter.Query`."""
        return self._query

    def _parse(self, source: str | bytes | Tree) -> Node:
        if isinstance(source, Tree):
            return source.root_node
        if isinstance(source, str):
            source = source.encode()
        return self._parser.parse(source).root_node

    def _public(self, captures: dict[str, list[Node]]) -> dict[str, list[Node]]:
        private = set(self._compiled.private_names)
        return {name: nodes for name, nodes in captures.items() if name not in private}

    def matches(self, source: str | bytes | Tree) -> list[Match]:
        """Run the query and return every match.

        Parameters
        ----------
        source : str | bytes | tree_sitter.Tree
            Source text in the query's language, or an already-parsed tree.
        """
        root = self._parse(source)
        cursor = QueryCursor(self._query)
        return [Match(index, self._public(captures)) for index, captures in cursor.matches(root)]

    def captures(self, source: str | bytes | Tree) -> dict[str, list[Node]]:
        """Run the query and return all captures merged by name."""
        root = self._parse(source)
        cursor = QueryCursor(self._query)
        return self._public(cursor.captures(root))

    def first(self, source: str | bytes | Tree) -> Match | None:
        """Return the first match, or ``None`` if the query does not match."""
        matches = self.matches(source)
        return matches[0] if matches else None

    def __repr__(self) -> str:
        return f"<TemplateQuery captures={self.capture_names}>"


def query(language: Language, template: Template) -> TemplateQuery:
    """Build a tree-sitter query from a template written in ``language``.

    Parameters
    ----------
    language : tree_sitter.Language
        The grammar the template is written in, and the grammar the resulting
        query runs against.
    template : string.templatelib.Template
        A PEP 750 template string (``t"..."``) containing example source code.
        Interpolate :func:`~tree_sitter.template.capture` to capture a node,
        :func:`~tree_sitter.template.anything` for an uncaptured wildcard, and
        ``...`` to allow additional unmatched siblings at that position.

    Returns
    -------
    TemplateQuery

    Raises
    ------
    TemplateSyntaxError
        If the template cannot be parsed by ``language``.
    TemplateCompileError
        If no valid query can be built from the parsed template.

    Examples
    --------
    >>> q = query(HCL, t'resource "aws_s3_bucket" {capture("name")} {{ {...} }}')
    >>> [m.text("name") for m in q.matches(source)]
    ['my_bucket']
    """
    return TemplateQuery(language, template)


# Re-exported so callers can catch these from this module too.
_ = (TemplateError, TemplateSyntaxError)
