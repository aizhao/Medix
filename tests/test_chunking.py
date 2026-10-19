import unittest

from knowledge.chunking import ChunkPolicy, TokenCounter, split_table_text, split_text


class ChunkingSafetyTests(unittest.TestCase):
    def test_long_table_row_is_hard_split_below_model_limit(self):
        policy = ChunkPolicy(max_tokens=32, overlap_tokens=4, min_tokens=8)
        text = "字段一\n字段二\n" + ("高血压治疗建议" * 30)
        chunks = split_table_text(text, policy=policy, tokenizer=None)
        counter = TokenCounter(tokenizer=None)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(counter.count(chunk) <= policy.max_tokens for chunk in chunks))

    def test_token_count_disables_transformers_overlength_warning(self):
        class FakeTokenizer:
            def __init__(self):
                self.kwargs = None

            def encode(self, text, **kwargs):
                self.kwargs = kwargs
                return list(range(len(text)))

        tokenizer = FakeTokenizer()
        self.assertEqual(TokenCounter(tokenizer=tokenizer).count("12345"), 5)
        self.assertFalse(tokenizer.kwargs["verbose"])

    def test_overlap_never_pushes_next_unit_over_limit(self):
        policy = ChunkPolicy(
            max_tokens=64,
            overlap_tokens=16,
            min_tokens=8,
            topic_boundary_threshold=-1,
        )
        text = ("甲" * 15) + "。\n" + ("乙" * 60) + "。"
        chunks = split_text(text, policy=policy, tokenizer=None)
        counter = TokenCounter(tokenizer=None)
        self.assertTrue(all(counter.count(chunk) <= policy.max_tokens for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
