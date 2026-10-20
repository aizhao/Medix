import asyncio
import unittest

from research.evidence_synthesizer import ResearchReport
from research.query_router import classify_query
from research.trusted_qa import EvidenceFirstMedicalWorkflow, EvidenceGate
from swarm.swarm_coordinator import SwarmCoordinator


class VerificationLLM:
    def __init__(self, supported=True):
        self.supported = supported

    async def chat(self, messages, **kwargs):
        value = "true" if self.supported else "false"
        return '{"verdicts":[{"index":0,"supported":' + value + ',"reason":"核验结果"}]}'


def source(source_id="kb-guide", authority=0.95, source_type="clinical_guideline"):
    return {
        "id": source_id,
        "title": "高血压指南",
        "type": source_type,
        "authority": authority,
        "excerpt": "高血压管理需要结合血压水平和总体心血管风险。",
        "citation_allowed": True,
    }


class FakeResearchWorkflow:
    def __init__(self, report):
        self.report = report

    async def run(self, question):
        return self.report


class FakeMemory:
    enabled = False

    def __init__(self):
        self.saved = []

    def get_recent_messages(self, **kwargs):
        return []

    def search_similar_sessions(self, **kwargs):
        return []

    def add_session_summary(self, **kwargs):
        self.saved.append(kwargs)


class FakeEvidenceWorkflow:
    def __init__(self):
        self.calls = []

    async def run(self, question, context=None):
        self.calls.append((question, context))
        return {
            "answer": "证据回答 [kb-guide]",
            "mode": "evidence_first",
            "swarm_enabled": False,
            "citations": [source()],
            "evidence_trace": {"pipeline": "evidence_first_medrgag"},
        }


class FakeLeadAgent:
    async def assess_and_decompose(self, question, context=None):
        return {
            "subtasks": [{
                "description": "回答医学问题",
                "assigned_agent": "consultation_agent",
            }]
        }

    async def synthesize_single_result(self, question, agent_result, evidence):
        return f"基于证据：{evidence.get('answer', '')}"


class FakeWorker:
    agent_id = "consultation_agent"

    async def process(self, payload):
        return {"answer": "Agent 初步回答"}


class TrustedQATests(unittest.TestCase):
    def test_deterministic_router_forces_medical_questions(self):
        self.assertEqual(classify_query("高血压应该如何治疗？").route, "medical_evidence")
        self.assertEqual(classify_query("你好").route, "direct")
        self.assertEqual(classify_query("胸痛并且呼吸困难").route, "urgent")

    def test_evidence_gate_passes_supported_claim(self):
        report = ResearchReport(
            query="高血压如何管理",
            key_findings=["管理应结合血压与总体风险 [kb-guide]"],
            sources=[source()],
        )
        decision = asyncio.run(EvidenceGate(VerificationLLM()).evaluate(report.query, report))
        self.assertEqual(decision.status, "passed")
        self.assertEqual(len(decision.supported_findings), 1)

    def test_high_risk_claim_without_authoritative_source_is_refused(self):
        report = ResearchReport(
            query="儿童用药剂量是多少",
            key_findings=["需要按体重调整 [web-blog]"],
            sources=[source("web-blog", authority=0.4, source_type="web")],
        )
        decision = asyncio.run(EvidenceGate(VerificationLLM()).evaluate(report.query, report))
        self.assertEqual(decision.status, "refused")
        self.assertFalse(decision.supported_findings)
        self.assertIn("高风险医学问题缺少权威来源支持", decision.reasons)

    def test_generated_or_unknown_citation_is_rejected(self):
        report = ResearchReport(
            query="高血压",
            key_findings=["模型认为应该这样处理 [gen-demo]"],
            sources=[source()],
        )
        decision = asyncio.run(EvidenceGate(VerificationLLM()).evaluate(report.query, report))
        self.assertEqual(decision.status, "refused")
        self.assertEqual(decision.verification_mode, "llm_entailment")

    def test_workflow_outputs_only_gated_findings(self):
        report = ResearchReport(
            query="高血压如何管理",
            key_findings=["管理应结合血压与总体风险 [kb-guide]", "没有引用的断言"],
            sources=[source()],
            evidence_trace={"selected_generated": 1, "generated_contexts_are_citations": False},
            recommendations=["没有引用的建议"],
        )
        workflow = EvidenceFirstMedicalWorkflow(
            llm_client=VerificationLLM(),
            research_workflow=FakeResearchWorkflow(report),
        )
        result = asyncio.run(workflow.run(report.query))
        self.assertEqual(result["mode"], "evidence_first")
        self.assertIn("[kb-guide]", result["answer"])
        self.assertNotIn("没有引用的断言", result["answer"])
        self.assertEqual(result["suggestions"], [])
        self.assertEqual(result["evidence_trace"]["evidence_gate"]["status"], "degraded")

    def test_coordinator_runs_agents_before_evidence_workflow(self):
        coordinator = object.__new__(SwarmCoordinator)
        coordinator.enable_swarm = True
        coordinator.short_term_memory = FakeMemory()
        coordinator.long_term_memory = FakeMemory()
        coordinator._evidence_workflow = FakeEvidenceWorkflow()
        coordinator.lead_agent = FakeLeadAgent()
        coordinator.consultation_agent = FakeWorker()
        coordinator.diagnostic_agent = FakeWorker()
        coordinator.research_agent = FakeWorker()
        result = asyncio.run(coordinator.process("什么是高血压？", session_id="route-test"))
        self.assertEqual(result["mode"], "medrgag_guided_single")
        self.assertEqual(len(coordinator._evidence_workflow.calls), 1)
        self.assertEqual(result["evidence_trace"]["route"]["route"], "medical_evidence")
        self.assertIn("consultation_agent", result["agents_involved"])


if __name__ == "__main__":
    unittest.main()
