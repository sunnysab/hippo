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
        parsed, event = topics.parse_answer(answer('research', 'ai', 'research', 'ai'), self.vocabulary)
        self.assertIsNone(event)
        self.assertEqual(['research', 'ai'], topics.slugs_of(parsed))
        self.assertEqual('研究与论文', parsed[0]['name'])

    def test_the_cap_is_two_so_a_third_label_cannot_drift(self):
        self.assertEqual(2, topics.MAX_TOPICS)

    def test_an_empty_answer_is_a_legitimate_result_not_a_failure(self):
        self.assertEqual(([], None), topics.parse_answer('{"topics":[]}', self.vocabulary))

    def test_fenced_json_is_accepted(self):
        parsed, _ = topics.parse_answer('```json\n' + answer('ai') + '\n```', self.vocabulary)
        self.assertEqual(['ai'], topics.slugs_of(parsed))

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
        self.assertEqual('人工智能', topics.parse_answer(answer('ai'), self.vocabulary)[0][0]['name'])


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
            parsed, event = await topics.classify(
                {'model': 'test'}, title='T', digest='D', vocabulary=topics.default_vocabulary()
            )
        self.assertEqual(1, call.await_count)
        self.assertEqual(['ai'], topics.slugs_of(parsed))
        self.assertIsNone(event)


if __name__ == '__main__':
    unittest.main()

class EventDecisionTest(unittest.TestCase):
    def setUp(self):
        self.vocabulary = topics.default_vocabulary()

    def _answer(self, event):
        return json.dumps({'topics': ['ai'], 'event': event})

    def test_no_event_section_still_yields_topics(self):
        parsed, event = topics.parse_answer(answer('ai'), self.vocabulary)
        self.assertEqual(['ai'], topics.slugs_of(parsed))
        self.assertIsNone(event)

    def test_none_is_reduced_to_no_event(self):
        for value in ({'action': 'none'}, {'action': 'none', 'id': 5}, {'action': 'none', 'title': 'x'}):
            with self.subTest(value=value):
                parsed, event = topics.parse_answer(self._answer(value), self.vocabulary, candidate_ids={1})
                self.assertEqual(['ai'], topics.slugs_of(parsed))
                self.assertIsNone(event)

    def test_joining_a_supplied_candidate_is_accepted(self):
        _, event = topics.parse_answer(
            self._answer({'action': 'join', 'id': 7}), self.vocabulary, candidate_ids={7, 8}
        )
        self.assertEqual({'action': 'join', 'id': 7}, event)

    def test_joining_an_unsupplied_candidate_rejects_the_whole_answer(self):
        # Attaching an article to an unrelated story is worse than leaving it loose.
        with self.assertRaises(ApiError) as ctx:
            topics.parse_answer(self._answer({'action': 'join', 'id': 99}), self.vocabulary, candidate_ids={7})
        self.assertEqual('topic.invalid_result', ctx.exception.code)

    def test_joining_without_an_id_is_rejected(self):
        with self.assertRaises(ApiError) as ctx:
            topics.parse_answer(self._answer({'action': 'join'}), self.vocabulary, candidate_ids={7})
        self.assertEqual('topic.invalid_result', ctx.exception.code)

    def test_a_new_event_needs_a_non_blank_title(self):
        _, event = topics.parse_answer(self._answer({'action': 'new', 'title': ' 某事发生 '}), self.vocabulary)
        self.assertEqual({'action': 'new', 'title': '某事发生'}, event)

        for value in ({'action': 'new'}, {'action': 'new', 'title': '   '}):
            with self.subTest(value=value), self.assertRaises(ApiError) as ctx:
                topics.parse_answer(self._answer(value), self.vocabulary)
            self.assertEqual('topic.invalid_result', ctx.exception.code)

    def test_an_unknown_action_or_an_extra_key_is_rejected(self):
        for value in ({'action': 'merge'}, {'action': 'none', 'confidence': 0.9}):
            with self.subTest(value=value), self.assertRaises(ApiError) as ctx:
                topics.parse_answer(self._answer(value), self.vocabulary)
            self.assertEqual('topic.invalid_result', ctx.exception.code)


class EventCandidatePromptTest(unittest.TestCase):
    def test_candidates_are_sent_with_the_vocabulary_in_one_call(self):
        candidates = [{'id': 3, 'title': 'Existing story', 'sources': 4}]
        payload = json.loads(
            topics.build_messages('T', 'D', topics.default_vocabulary(), candidates)[1]['content']
        )
        self.assertEqual([{'id': 3, 'title': 'Existing story'}], payload['events'])
        # One call: the vocabulary and the candidates arrive together.
        self.assertEqual(['slug', 'name'], list(payload['vocabulary'][0]))

    def test_the_candidate_list_is_capped_so_the_prompt_stays_flat(self):
        candidates = [{'id': index, 'title': f'S{index}'} for index in range(topics.MAX_EVENT_CANDIDATES + 20)]
        payload = json.loads(topics.build_messages('T', 'D', topics.default_vocabulary(), candidates)[1]['content'])
        self.assertEqual(topics.MAX_EVENT_CANDIDATES, len(payload['events']))

    def test_no_candidates_still_sends_an_empty_list(self):
        payload = json.loads(topics.build_messages('T', 'D', topics.default_vocabulary())[1]['content'])
        self.assertEqual([], payload['events'])

    def test_the_prompt_carries_the_event_rules(self):
        system = topics.build_messages('T', 'D', topics.default_vocabulary())[0]['content']
        self.assertIn('"none"', system)
        self.assertIn('prefer "none" over inventing one', system)
