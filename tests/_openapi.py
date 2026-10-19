"""The app's generated OpenAPI document, built once and handed out read-only.

The contract files read the same document through ``_document()`` / ``_resolve()``.
Building it means assembling the whole app, which costs a few hundred
milliseconds, so it is built once here. Every call returns a copy: a test that
mutated the shared spec would otherwise leak into the next one.
"""

from __future__ import annotations

import copy
import tempfile
from typing import Any
from unittest.mock import patch

from hippo import server

_document: dict[str, Any] | None = None


def document() -> dict[str, Any]:
    """A fresh copy of the document the real app generates."""
    global _document
    if _document is None:
        with (
            tempfile.TemporaryDirectory() as static_dir,
            patch('hippo.server.configure_logging'),
            patch('hippo.server.init_telemetry', return_value=None),
            patch('hippo.server.instrument_app'),
        ):
            _document = server.create_app(static_dir=static_dir, enable_inprocess_sync=False).openapi()
    return copy.deepcopy(_document)


def resolve(spec: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """Follow a local ``$ref`` to the schema it names."""
    while '$ref' in node:
        _, _, name = node['$ref'].rpartition('/')
        node = spec['components']['schemas'][name]
    return node
