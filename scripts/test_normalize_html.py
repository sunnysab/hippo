#!/usr/bin/env python3
"""Quick test for normalize_html markdown output."""

from __future__ import annotations

import argparse
from pathlib import Path

from hippo.normalize_html import normalize_html


def main() -> int:
    parser = argparse.ArgumentParser(description='Test normalize_html markdown output.')
    parser.add_argument('--input', default='~/test.html', help='HTML file path')
    parser.add_argument('--output', default='/tmp/normalize_test.md', help='Markdown output path')
    parser.add_argument('--preview-lines', type=int, default=80, help='Preview line count')
    args = parser.parse_args()

    input_path = Path(args.input).expanduser()
    output_path = Path(args.output).expanduser()

    raw_html = input_path.read_text(encoding='utf-8', errors='ignore')
    markdown = normalize_html(raw_html, fmt='markdown')
    output_path.write_text(markdown, encoding='utf-8')

    print(f'Wrote markdown to {output_path}')
    print('--- preview ---')
    for line in markdown.splitlines()[: args.preview_lines]:
        print(line)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
