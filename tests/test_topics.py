"""The closed vocabulary is the feature: an unknown slug rejects the answer."""

import json
import unittest
from unittest.mock import AsyncMock, patch

from hippo import topics
from hippo.exceptions import ApiError


def answer(*slugs):
    return json.dumps({'topics': list(slugs)})


class VocabularyTest(unittest.TestCase):
    def test_the_default_vocabulary_is_valid_and_unique(self):
        vocabulary = topics.validate_vocabulary(topics.default_vocabulary())
        slugs = [topic['slug'] for topic in vocabulary['topics']]
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertEqual(1, vocabulary['version'])

    def test_slugs_stay_ascii_identifiers(self):
        for topic in topics.DEFAULT_VOCABULARY:
            self.assertRegex(topic['slug'], r'^[a-z][a-z0-9-]*$')

    def test_a_duplicated_slug_is_rejected_rather_than_rendered_twice(self):
        raw = topics.default_vocabulary()
        raw['topics'].append(dict(raw['topics'][0]))
        with self.assertRaises(ApiError) as ctx:
            topics.validate_vocabulary(raw)
        self.assertEqual('topic.invalid_vocabulary', ctx.exception.code)

    def test_malformed_vocabularies_are_rejected(self):
        invalid = [
            {},
            {'version': 1, 'topics': []},
            {'version': 0, 'topics': [{'slug': 'ai', 'name': 'x'}]},
            {'version': 1, 'topics': [{'slug': 'AI', 'name': 'x'}]},
            {'version': 1, 'topics': [{'slug': 'ai'}]},
            {'version': 1, 'topics': [{'slug': 'ai', 'name': 'x', 'extra': 1}]},
            {'version': '1', 'topics': [{'slug': 'ai', 'name': 'x'}]},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ApiError) as ctx:
                topics.validate_vocabulary(value)
            self.assertEqual('topic.invalid_vocabulary', ctx.exception.code)


class FingerprintTest(unittest.TestCase):
    def test_title_digest_and_prompt_version_all_invalidate_the_cache(self):
        original = topics.fingerprint('Title', 'Digest')
        self.assertNotEqual(original, topics.fingerprint('Other', 'Digest'))
        self.assertNotEqual(original, topics.fingerprint('Title', 'Other'))
        with patch.object(topics, 'PROMPT_VERSION', topics.PROMPT_VERSION + 1):
            self.assertNotEqual(original, topics.fingerprint('Title', 'Digest'))

    def test_a_missing_digest_hashes_like_an_empty_one(self):
        self.assertEqual(topics.fingerprint('T', None), topics.fingerprint('T', ''))


class ParseAnswerTest(unittest.TestCase):
    def setUp(self):
        self.vocabulary = topics.default_vocabulary()

    def test_duplicates_collapse_and_leading_order_is_kept(self):
        parsed = topics.parse_answer(answer('research', 'ai', 'research'), self.vocabulary)
        self.assertEqual(['research', 'ai'], topics.slugs_of(parsed))
        self.assertEqual('研究与论文', parsed[0]['name'])

    def test_an_empty_answer_is_a_legitimate_result_not_a_failure(self):
        self.assertEqual([], topics.parse_answer('{"topics":[]}', self.vocabulary))

    def test_fenced_json_is_accepted(self):
        self.assertEqual(['ai'], topics.slugs_of(topics.parse_answer('```json\n' + answer('ai') + '\n```', self.vocabulary)))

    def test_a_slug_outside_the_vocabulary_rejects_the_whole_answer(self):
        with self.assertRaises(ApiError) as ctx:
            topics.parse_answer(answer('ai', 'invented'), self.vocabulary)
        self.assertEqual('topic.invalid_result', ctx.exception.code)

    def test_more_than_the_cap_is_rejected(self):
        too_many = [topic['slug'] for topic in self.vocabulary['topics'][: topics.MAX_TOPICS + 1]]
        with self.assertRaises(ApiError) as ctx:
            topics.parse_answer(answer(*too_many), self.vocabulary)
        self.assertEqual('topic.invalid_result', ctx.exception.code)

    def test_unparsable_answers_never_pass_through(self):
        invalid = ['', 'partial', '{}', '[]', answer(1), '{"topics":"ai"}', '{"topics":["ai"],"extra":1}']
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ApiError) as ctx:
                topics.parse_answer(value, self.vocabulary)
            self.assertEqual('topic.invalid_result', ctx.exception.code)

    def test_the_name_always_comes_from_the_vocabulary_not_the_model(self):
        self.assertEqual('人工智能', topics.parse_answer(answer('ai'), self.vocabulary)[0]['name'])


class BuildMessagesTest(unittest.IsolatedAsyncioTestCase):
    def test_the_prompt_carries_the_whole_vocabulary_and_the_article(self):
        messages = topics.build_messages('Title', 'Digest', topics.default_vocabulary())
        payload = json.loads(messages[1]['content'])
        self.assertEqual('Title', payload['title'])
        self.assertEqual('Digest', payload['digest'])
        self.assertEqual(len(topics.DEFAULT_VOCABULARY), len(payload['vocabulary']))
        # Untrusted text is data to classify, never an instruction.
        self.assertIn('not instructions', messages[0]['content'])

    def test_a_missing_digest_is_sent_as_an_empty_string(self):
        messages = topics.build_messages('Title', None, topics.default_vocabulary())
        self.assertEqual('', json.loads(messages[1]['content'])['digest'])

    async def test_classify_runs_one_call_and_validates_the_answer(self):
        with patch('hippo.llm.client.complete', AsyncMock(return_value=answer('ai'))) as call:
            parsed = await topics.classify({'model': 'test'}, title='T', digest='D', vocabulary=topics.default_vocabulary())
        self.assertEqual(1, call.await_count)
        self.assertEqual(['ai'], topics.slugs_of(parsed))


if __name__ == '__main__':
    unittest.main()
