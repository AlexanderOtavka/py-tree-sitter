"""Exceptions raised by the template-query pipeline.

All of these carry the intermediate artifact (the rendered template text or the
generated query text) in their message, because anyone debugging a template
needs to see the form that actually failed.
"""

from __future__ import annotations

__all__ = ["TemplateCompileError", "TemplateError", "TemplateSyntaxError"]


class TemplateError(Exception):
    """Base class for every error raised while turning a template into a query."""


class TemplateSyntaxError(TemplateError):
    """The rendered template does not parse in the target grammar.

    Raised by step 2 of the pipeline when the parse tree of the rendered
    template contains ``ERROR`` or ``MISSING`` nodes, i.e. no padding candidate
    made the template valid source in the target language.
    """


class TemplateCompileError(TemplateError):
    """The parse tree could not be turned into a valid query pattern.

    Raised by step 3 of the pipeline — for example when a hole's byte span does
    not correspond to any node in the parse tree.
    """
