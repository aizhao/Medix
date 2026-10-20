"""Evidence-first medical QA with claim-level citation gating."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from loguru import logger

from core import LLMClient
from research.deep_research_workflow import DeepResearchWorkflow
from research.evidence_synthesizer import ResearchReport


_CITATION = re.compile(r"\[((?:kb|web)-[A-Za-z0-9_-]+)\]")
_HIGH_RISK = re.compile(
    r"(剂量|用量|停药|换药|处方|禁忌|过量|孕妇|怀孕|妊娠|哺乳|儿童|婴儿|"
    r"肝功能|肾功能|诊断|确诊|治疗方案|手术|急救|drug dose|dosage|pregnan)",
    re.IGNORECASE,
)


@dataclass
class EvidenceGateDecision:
    status: str
    high_risk: bool
    supported_findings: List[str] = field(default_factory=list)
    supported_recommendations: List[str] = field(default_factory=list)
    rejected_findings: List[Dict[str, Any]] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    external_source_count: int = 0
    authoritative_source_count: int = 0
    verification_mode: str = "llm_entailment"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _extract_json(text: str) -> Dict[str, Any]:
    cleaned = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL | re.IGNORECASE)
    if fenced:
        cleaned = fenced.group(1).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("verification response did not contain JSON")
    value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("verification response was not an object")
    return value


class EvidenceGate:
    """Allow only claims that map to real retrieved evidence and pass support checks."""

    def __init__(self, llm_client: LLMClient):
        self.llm_client = llm_client

    async def evaluate(self, question: str, report: ResearchReport) -> EvidenceGateDecision:
        sources = {
            str(source.get("id")): source
            for source in report.sources
            if source.get("citation_allowed", True)
            and source.get("type") != "generated"
            and str(source.get("id", "")).startswith(("kb-", "web-"))
        }
        authoritative_ids = {
            source_id for source_id, source in sources.items()
            if float(source.get("authority") or 0.0) >= 0.75
        }
        high_risk = bool(_HIGH_RISK.search(question))
        decision = EvidenceGateDecision(
            status="refused",
            high_risk=high_risk,
            external_source_count=len(sources),
            authoritative_source_count=len(authoritative_ids),
        )
        if not sources:
            decision.reasons.append("没有可引用的外部检索证据")
            return decision

        verifiable: List[Dict[str, Any]] = []
        for index, finding in enumerate(report.key_findings):
            cited = list(dict.fromkeys(_CITATION.findall(finding)))
            invalid = [source_id for source_id in cited if source_id not in sources]
            if not cited or invalid:
                decision.rejected_findings.append({
                    "finding": finding,
                    "reason": "缺少有效引用" if not cited else f"包含无效引用：{', '.join(invalid)}",
                })
                continue
            verifiable.append({"index": index, "kind": "finding", "text": finding, "citation_ids": cited})

        recommendation_offset = len(report.key_findings)
        for position, recommendation in enumerate(report.recommendations):
            cited = list(dict.fromkeys(_CITATION.findall(recommendation)))
            invalid = [source_id for source_id in cited if source_id not in sources]
            if not cited or invalid:
                decision.rejected_findings.append({
                    "kind": "recommendation",
                    "finding": recommendation,
                    "reason": "建议缺少有效引用" if not cited else f"建议包含无效引用：{', '.join(invalid)}",
                })
                continue
            verifiable.append({
                "index": recommendation_offset + position,
                "kind": "recommendation",
                "text": recommendation,
                "citation_ids": cited,
            })

        if not verifiable:
            decision.reasons.append("生成结果中没有带有效来源 ID 的关键结论")
            return decision

        evidence_blocks = []
        for source_id, source in sources.items():
            evidence_blocks.append(
                f"[{source_id}] title={source.get('title', '')}\n{str(source.get('excerpt', ''))[:1200]}"
            )
        claims = [
            {"index": item["index"], "claim": item["text"], "citation_ids": item["citation_ids"]}
            for item in verifiable
        ]
        prompt = f"""你是严格的医学证据核验器。逐条判断 claim 是否被它引用的原文片段直接支持。
不能因为常识正确就判定支持；原文未表达、仅部分相关、推断过度、剂量或人群不一致都必须判为 false。
忽略 claim 末尾的引用标记，只核验实质内容。
只输出 JSON：{{"verdicts":[{{"index":0,"supported":true,"reason":"原文直接支持"}}]}}

问题：{question}
Claims：{json.dumps(claims, ensure_ascii=False)}
Evidence：
{chr(10).join(evidence_blocks)}
"""
        verdicts: Dict[int, Dict[str, Any]] = {}
        try:
            response = await self.llm_client.chat(
                [{"role": "user", "content": prompt}], temperature=0.0, max_tokens=1000
            )
            data = _extract_json(response)
            for item in data.get("verdicts", []):
                if not isinstance(item, dict):
                    continue
                try:
                    verdicts[int(item.get("index"))] = item
                except (TypeError, ValueError):
                    continue
        except Exception as exc:
            logger.warning(f"Evidence entailment verification degraded: {exc}")
            decision.verification_mode = "verification_failed"
            decision.reasons.append("证据支持性核验失败，未放行未经核验的医学结论")

        for item in verifiable:
            verdict = verdicts.get(item["index"], {})
            if verdict.get("supported") is True:
                if item["kind"] == "recommendation":
                    decision.supported_recommendations.append(item["text"])
                else:
                    decision.supported_findings.append(item["text"])
            else:
                decision.rejected_findings.append({
                    "kind": item["kind"],
                    "finding": item["text"],
                    "reason": str(verdict.get("reason") or "未通过证据支持性核验"),
                })

        if not decision.supported_findings:
            decision.reasons.append("没有关键结论通过证据支持性核验")
            return decision

        supported_citations = set()
        for finding in decision.supported_findings:
            supported_citations.update(_CITATION.findall(finding))
        if high_risk and not (supported_citations & authoritative_ids):
            decision.supported_findings = []
            decision.status = "refused"
            decision.reasons.append("高风险医学问题缺少权威来源支持")
            return decision

        degraded = bool(decision.rejected_findings or report.evidence_trace.get("degraded_stages"))
        decision.status = "degraded" if degraded else "passed"
        if degraded:
            decision.reasons.append("部分候选结论或流程阶段未通过，已仅保留核验通过的结论")
        return decision


class EvidenceFirstMedicalWorkflow:
    """Backward-compatible MedRGAG evidence stage used by the unified graph."""

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        research_workflow: Optional[DeepResearchWorkflow] = None,
    ):
        self.llm_client = llm_client or LLMClient()
        self.research_workflow = research_workflow or DeepResearchWorkflow(llm_client=self.llm_client)
        self.gate = EvidenceGate(self.llm_client)

    async def run(
        self,
        question: str,
        context: Optional[Dict[str, Any]] = None,
        agent_findings: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        # Agent findings are carried into the evidence trace and final synthesis
        # context; retrieved sources remain the only citation-authorized inputs.
        try:
            report = await self.research_workflow.run(
                question,
                context=context,
                agent_findings=agent_findings or [],
            )
        except TypeError:
            # Keep lightweight test doubles and legacy research workflows
            # compatible while the unified stage is being adopted.
            report = await self.research_workflow.run(question)
        decision = await self.gate.evaluate(question, report)
        trace = dict(report.evidence_trace or {})
        trace["evidence_gate"] = decision.to_dict()
        trace["pipeline"] = "medrgag_evidence_stage"
        trace["agent_findings_count"] = len(agent_findings or [])

        if decision.status == "refused":
            answer = (
                "## 暂无法提供可靠建议\n\n"
                "目前可用的医学资料不足以支持明确结论，因此不作未经证实的推断。\n\n"
                "如症状持续或加重，或涉及用药、孕产妇、儿童及诊断治疗决策，请咨询医生或药师。"
            )
        else:
            findings = "\n".join(f"- {item}" for item in decision.supported_findings)
            answer = (
                f"## 相关建议\n\n{findings}"
            )

        warnings = []
        if decision.status != "passed":
            warnings.extend(decision.reasons or ["证据不足，回答已安全降级。"])
        citations = [
            source for source in report.sources
            if source.get("citation_allowed", True) and source.get("type") != "generated"
        ]
        return {
            "answer": answer,
            "mode": "evidence_first",
            "swarm_enabled": False,
            "agents_involved": ["medrgag_pipeline", "evidence_gate"],
            "suggestions": decision.supported_recommendations[:5],
            "citations": citations,
            "evidence_trace": trace,
            "warnings": warnings,
            "disclaimer": "以上内容基于当前可检索证据，仅供健康信息参考，不能替代医生诊断和治疗。",
        }


__all__ = ["EvidenceFirstMedicalWorkflow", "EvidenceGate", "EvidenceGateDecision"]
