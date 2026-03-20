import json
import re
import unittest
from pathlib import Path

import quickjs


ROOT = Path(__file__).resolve().parent.parent
FACETS_JS = ROOT / 'static' / 'articles_facets.js'
ARTICLES_CSS = ROOT / 'static' / 'articles.css'


def extract_rule(css_text: str, selector: str) -> str:
    pattern = rf'{re.escape(selector)}\s*\{{([^}}]+)\}}'
    match = re.search(pattern, css_text, re.MULTILINE)
    if not match:
      raise AssertionError(f'Missing CSS rule for {selector}')
    return match.group(1)


class ArticleFrontendLogicTest(unittest.TestCase):
    def run_facet_helper(self, payload: dict) -> dict:
        context = quickjs.Context()
        context.eval('var window = {};')
        context.eval(FACETS_JS.read_text(encoding='utf-8'))
        context.eval(f'var result = window.HippoArticleFacets.buildArticleFacetVisibility({json.dumps(payload)});')
        return json.loads(context.eval('JSON.stringify(result)'))

    def test_collapsed_facets_keep_active_type_visible(self) -> None:
        result = self.run_facet_helper(
            {
                'items': [
                    {'value': '', 'label': 'All Types'},
                    {'value': '0', 'label': 'Regular Article'},
                    {'value': '5', 'label': 'Video Share'},
                    {'value': '6', 'label': 'Music Share'},
                    {'value': '7', 'label': 'Audio Share'},
                    {'value': '8', 'label': 'Picture Share'},
                    {'value': '10', 'label': 'Text Share'},
                ],
                'activeValue': '10',
                'collapsedLimit': 5,
                'expanded': False,
            }
        )

        self.assertTrue(result['isCollapsible'])
        self.assertEqual(['', '0', '5', '6', '10'], [item['value'] for item in result['visibleItems']])
        self.assertEqual(2, result['hiddenCount'])

    def test_avatar_and_name_rules_protect_reader_header_density(self) -> None:
        css = ARTICLES_CSS.read_text(encoding='utf-8')

        avatar_rule = extract_rule(css, '.article-preview-avatar')
        self.assertIn('width: 40px', avatar_rule)
        self.assertIn('height: 40px', avatar_rule)
        self.assertIn('flex: 0 0 40px', avatar_rule)

        name_rule = extract_rule(css, '.article-preview-name')
        self.assertIn('white-space: nowrap', name_rule)
        self.assertIn('overflow: hidden', name_rule)
        self.assertIn('text-overflow: ellipsis', name_rule)


if __name__ == '__main__':
    unittest.main()
