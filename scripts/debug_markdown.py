#!/usr/bin/env python3
"""Debug markdown conversion for a local HTML file (no recursion)."""

from __future__ import annotations

import sys
from pathlib import Path

from bs4 import BeautifulSoup

from hippo.normalize_html import normalize_html

def max_dom_depth_iterative(root) -> int:
    max_depth = 0
    stack: list[tuple[object, int]] = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > max_depth:
            max_depth = depth
        contents = getattr(node, "contents", None)
        if not contents:
            continue
        for child in contents:
            stack.append((child, depth + 1))
    return max_depth


def count_tags(soup: BeautifulSoup) -> int:
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
    tag_count = count_tags(soup)
    max_depth = max_dom_depth_iterative(soup)

    print(f"HTML tags: {tag_count}")
    print(f"Max DOM depth: {max_depth}")

    try:
        markdown = normalize_html(raw_html, fmt="markdown")
    except RecursionError as exc:
        print(f"RecursionError in markdown conversion: {exc}")
        return 1
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1

    print(f"Markdown length: {len(markdown)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
