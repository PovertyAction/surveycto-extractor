"""Shared, pipeline-stage-agnostic support modules for the extractor.

Currently holds ``sentinels`` (the single source of truth for sentinel /
special-missing codes, consumed by both the variable-dictionary builder and the
Stata metadata generator). Kept separate from the stage packages
(``parsers``/``extractors``/``generators``/``transformers``) because it is
cross-cutting rather than part of any one pipeline stage.
"""
