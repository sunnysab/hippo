#!/usr/bin/env python3
"""Debug markdown conversion for a local HTML file."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable

from bs4 import BeautifulSoup

try:
    from normalize_html import normalize_html
except Exception:
    from ..normalize_html import normalize_html  # type: ignore


def _max_dom_depth(node) -> int:
    if not hasattr(node, "contents"):
        return 0
    if not node.contents:
        return 1
    return 1 + max((_max_dom_depth(child) for child in node.contents if child), default=0)


def _count_tags(soup: BeautifulSoup) -> int:
    return sum(1 for _ in soup.find_all(True))


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: python scripts/debug_markdown.py /path/to/test.html", file=sys.stderr)
        return 2
    path = Path(argv[1]).expanduser().resolve()
    if not path.exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        return 2

    raw_html = path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(raw_html, "html.parser")
    max_depth = _max_dom_depth(soup)
    tag_count = _count_tags(soup)

    print(f"HTML tags: {tag_count}")
    print(f"Max DOM depth: {max_depth}")

    try:
        markdown = normalize_html(raw_html, fmt="markdown")
    except RecursionError as exc:
        print(f"RecursionError: {exc}")
        return 1
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1

    print(f"Markdown length: {len(markdown)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
