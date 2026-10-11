"""Transcript sanity checks.

The cases below are the ones actually observed on the backend, so they double as
regression tests for the two ways a chunk can go wrong unnoticed.
"""

import unittest

from hippo.media.validate import MAX_CHARS_PER_SECOND_LATIN, Verdict, check

#: A sentence with enough character variety that the repetition check has
#: something to measure; a single repeated character would trip it by design.
_SENTENCE = '这是一段用于校验转写结果的中文样例，句子里有足够多的不同字符。'

#: The English side of the same problem: Latin script packs far more characters
#: into one token, so the same length means something else entirely.
_ENGLISH = 'The committee heard testimony from several witnesses about the evacuation. '


def _sample(length: int) -> str:
    return (_SENTENCE * (length // len(_SENTENCE) + 1))[:length]


class CheckTest(unittest.TestCase):
    def test_a_normal_chinese_chunk_passes(self) -> None:
        # 300 s at roughly 6.3 characters per second.
        verdict = check(_sample(1900), 300.0)

        self.assertTrue(verdict.ok)
        self.assertIsNone(verdict.reason)

    def test_the_garbage_tail_is_rejected(self) -> None:
        # The measured failure: a 20 s chunk answered with 2048 exclamation marks.
        verdict = check('!' * 2048, 20.0)

        self.assertFalse(verdict.ok)
        self.assertIn('字符/秒', verdict.reason)

    def test_a_repetition_loop_with_a_sane_rate_is_still_rejected(self) -> None:
        # Slow enough to pass the rate check, still one character owning the text.
        verdict = check('啊啊啊啊' * 60, 300.0)

        self.assertFalse(verdict.ok)
        self.assertIn('疑似解码循环', verdict.reason)

    def test_a_long_english_chunk_is_not_mistaken_for_a_truncated_one(self) -> None:
        # 实测：The Daily 第 5 片（300 秒英文）在两档模型上都给出了 3400~3500 字符的
        # 完整转写，却被按中文的字符/token 比例判成「疑似截断」。
        text = (_ENGLISH * 60)[:3500]

        self.assertTrue(check(text, 300, max_new_tokens=2048).ok)

    def test_fast_english_speech_is_not_a_decode_loop(self) -> None:
        # 实测：Fireship 一条 300 秒切片出了 5441 字符的连贯英文（18.1 字/秒），
        # 被中文的 15 字/秒上限判成解码循环，于是每个语速快的英文视频都会失败三次。
        text = (_ENGLISH * 100)[:5441]

        self.assertTrue(check(text, 300, max_new_tokens=2048).ok)
        self.assertLess(18.1, MAX_CHARS_PER_SECOND_LATIN)

    def test_a_runaway_latin_loop_is_still_caught(self) -> None:
        verdict = check((_ENGLISH * 200)[:12000], 300, max_new_tokens=2048)

        self.assertFalse(verdict.ok)
        self.assertIn('字符/秒异常', verdict.reason)

    def test_the_chinese_ceiling_is_unchanged(self) -> None:
        # 中文 300 秒切片顶到生成预算（约 3400 字）时仍然是异常，而不是被放宽放过。
        self.assertFalse(check(_sample(3400), 300, max_new_tokens=2048).ok)

    def test_the_cap_is_read_off_the_script_not_off_the_setting(self) -> None:
        # 英文的 2048 token 约合 8000 字符，远高于 300 秒语音可能产出的量（字符/秒上限
        # 是 15），所以英文切片实际上不可能碰到截断告警——这正是要断言的行为。
        self.assertTrue(check((_ENGLISH * 80)[:4400], 300, max_new_tokens=2048).ok)
        # 同样长度的中文就顶到预算了，保护没有丢。
        self.assertFalse(check(_sample(3400), 300, max_new_tokens=2048).ok)

    def test_a_chunk_at_the_generation_cap_is_flagged(self) -> None:
        # 2048 tokens produced 3367 characters before the measured truncation.
        verdict = check(_sample(3400), 1200.0, max_new_tokens=2048)

        self.assertFalse(verdict.ok)
        self.assertIn('疑似被截断', verdict.reason)

    def test_the_cap_check_does_not_fire_on_a_backend_without_one(self) -> None:
        verdict = check(_sample(3400), 1200.0)

        self.assertTrue(verdict.ok)

    def test_a_normal_chunk_is_far_from_the_cap(self) -> None:
        # 300 s of dense speech is ~1900 characters, about 59% of the budget.
        verdict = check(_sample(1900), 300.0, max_new_tokens=2048)

        self.assertTrue(verdict.ok)

    def test_a_silent_chunk_is_reported_as_silent_not_failed(self) -> None:
        verdict = check('', 300.0)

        self.assertFalse(verdict.ok)
        self.assertTrue(verdict.silent)

    def test_a_barely_audible_chunk_counts_as_silent(self) -> None:
        verdict = check('嗯。', 300.0)

        self.assertTrue(verdict.silent)

    def test_whitespace_does_not_count_towards_the_text(self) -> None:
        verdict = check('   \n  ', 300.0)

        self.assertTrue(verdict.silent)

    def test_a_zero_length_chunk_is_an_error(self) -> None:
        verdict = check('随便什么', 0.0)

        self.assertFalse(verdict.ok)
        self.assertFalse(verdict.silent)

    def test_a_music_only_chunk_does_not_look_like_a_failure(self) -> None:
        # Slow speech over a music bed is legitimate; only loops and caps fail.
        verdict = check('这一段的音乐非常好听，我们稍后继续。', 300.0)

        self.assertTrue(verdict.ok)

    def test_verdict_defaults_are_explicit(self) -> None:
        verdict = Verdict(ok=True)

        self.assertIsNone(verdict.reason)
        self.assertFalse(verdict.silent)


if __name__ == '__main__':
    unittest.main()
