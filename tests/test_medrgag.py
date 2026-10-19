import asyncio
import unittest

from research.medrgag import (
    EvidenceDocument,
    MedRGAGPipeline,
    _extract_relevant_passages,
    evidence_from_kb,
    rrf_fuse,
    select_documents_kads,
    source_balanced_candidates,
)


class FakeLLM:
    async def chat(self, messages, **kwargs):
        prompt = messages[-1]["content"]
        if "摘要器" in prompt:
            return '{"useful_information":"与问题相关的治疗证据"}'
        if "知识缺口分析器" in prompt:
            return '{"missing_knowledge":[{"point":"安全性与禁忌","reason":"摘要未覆盖"},{"point":"适用人群","reason":"摘要未覆盖"},{"point":"疗效比较","reason":"摘要未覆盖"},{"point":"第四个缺口","reason":"不应保留"}]}'
        if "背景文档生成器" in prompt:
            return "生成的背景上下文，不是外部引用。"
        if "KADS 医学证据选择器" in prompt:
            return '{"knowledge_requirements":["治疗有效性","安全性"],"requirement_mapping":{"治疗有效性":[0],"安全性":[1]},"irrelevant_indices":[],"selected_indices":[0,1,2,3,4],"selection_reason":"覆盖有效性和安全性"}'
        raise AssertionError("unexpected prompt")


class MedRGAGTests(unittest.TestCase):
    def test_rrf_and_source_balance(self):
        a = EvidenceDocument("a", "糖尿病治疗", source_kind="kb", retrieval_score=0.8)
        b = EvidenceDocument("b", "生活方式", source_kind="web", retrieval_score=0.7)
        self.assertEqual(rrf_fuse([[a, b], [a]])[0].id, "a")
        balanced = source_balanced_candidates([a, b])
        self.assertEqual({item.source_kind for item in balanced}, {"kb", "web"})

    def test_original_kgcc_flow_preserves_provenance(self):
        retrieved = [
            EvidenceDocument("kb-1", "治疗证据", title="指南", source_kind="clinical_guideline", authority=0.95),
            EvidenceDocument("web-1", "疾病背景", title="权威网页", source_kind="authoritative_web", authority=0.9),
        ]
        result = asyncio.run(MedRGAGPipeline(FakeLLM(), top_k=5).run("如何治疗？", retrieved))
        self.assertEqual(len(result.missing_knowledge), 3)
        self.assertEqual(len(result.generated_documents), 5)
        self.assertTrue(all(not doc.is_external_citation for doc in result.generated_documents))
        self.assertEqual(result.trace.excluded_generated_from_citations, result.trace.selected_generated)
        self.assertTrue(all(not doc.is_generated for doc in result.external_citations))
        self.assertEqual(result.trace.kads_requirements, ["治疗有效性", "安全性"])
        self.assertEqual(result.trace.kads_mapping["治疗有效性"], [result.trace.reranked_ids[0]])
        self.assertEqual(result.trace.kads_selection_reason, "覆盖有效性和安全性")
        self.assertFalse(result.trace.kads_used_fallback)
        self.assertEqual(result.trace.summaries[0]["useful"], True)

    def test_kads_returns_nonempty_and_diverse_selection(self):
        docs = [
            EvidenceDocument("1", "高血压 药物治疗", source_kind="kb", rerank_score=0.9),
            EvidenceDocument("2", "高血压 生活方式", source_kind="web", rerank_score=0.8),
        ]
        selected = select_documents_kads("高血压治疗", docs, top_k=2)
        self.assertEqual(len(selected), 2)
        self.assertEqual({doc.source_kind for doc in selected}, {"kb", "web"})

    def test_extract_fallback_and_authority_metadata(self):
        extracted = _extract_relevant_passages(
            "高血压诊断标准",
            "无关前言。\n高血压诊断标准为诊室血压达到相应阈值。\n其他内容。",
        )
        self.assertIn("高血压诊断标准", extracted)
        low_quality = evidence_from_kb({
            "id": 1,
            "content": "指南正文",
            "score": 0.9,
            "metadata": {"type": "clinical_guideline", "authority_level": "low"},
        })
        self.assertEqual(low_quality.authority, 0.45)


if __name__ == "__main__":
    unittest.main()
