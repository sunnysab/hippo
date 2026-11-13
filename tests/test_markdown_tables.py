"""Table row boundaries survive the Markdown-to-block conversion."""

import pytest

from hippo.downloader import parse_markdown_blocks


@pytest.mark.parametrize('header', ['| Asset | Purpose |', 'Asset | Purpose'])
def test_tables_keep_rows_separate_from_surrounding_prose(header):
    table = f'{header}\n| :--- | ---: |\n| CodeGraph | Calls and symbols |\n| RAG | Documents |'
    _, _, blocks, markdown = parse_markdown_blocks(f'# Title\n\nBefore\n{table}\nAfter')
    assert blocks == [
        {'type': 'paragraph', 'text': 'Before'},
        {'type': 'paragraph', 'text': table},
        {'type': 'paragraph', 'text': 'After'},
    ]
    assert table in markdown


def test_fenced_table_examples_stay_code_and_prose_is_still_joined():
    _, _, blocks, _ = parse_markdown_blocks('```\n| A | B |\n| --- | --- |\n```\n\nOne\nparagraph')
    assert blocks[0]['type'] == 'code'
    assert blocks[1] == {'type': 'paragraph', 'text': 'One paragraph'}
