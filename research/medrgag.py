"""Safe, provenance-aware implementation of the MedRGAG pipeline.

The KGCC stages intentionally follow the paper:

1. summarize every retrieved document for the question;
2. identify important knowledge missing from those summaries;
3. generate one background document per missing knowledge point, then generate
   question-conditioned documents until ``top_k`` generated documents exist.

Generated documents may participate in KADS and answer reasoning, but they are
always labelled ``generated`` and are never presented as external citations.
The module is dependency-light so its ranking and safety invariants can be
tested without loading Milvus or a model.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlparse


NO_USEFUL_INFORMATION = "No useful information"


@dataclass
class EvidenceDocument:
    """A retrieved or generated context with explicit provenance."""

    id: str
    text: str
    title: str = ""
    source_kind: str = "knowledge_base"
    url_or_path: str = ""
    authority: float = 0.5
    retrieval_score: float = 0.0
    rerank_score: Optional[float] = None
    status: str = "retrieved"
    heading_path: str = ""
    generated_from: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_generated(self) -> bool:
        return self.source_kind == "generated"

    @property
    def is_external_citation(self) -> bool:
        return not self.is_generated and self.status not in {"rejected", "conflicting", "snippet_only"}

    def public_source(self) -> Dict[str, Any]:
        """Return source metadata without pretending generated text is a citation."""
        data: Dict[str, Any] = {
            "id": self.id,
            "type": self.source_kind,
            "title": self.title or self.id,
            "status": self.status,
            "retrieval_score": self.retrieval_score,
            "rerank_score": self.rerank_score,
            "authority": self.authority,
        }
        if self.url_or_path:
            data["url"] = self.url_or_path
        if self.heading_path:
            data["heading_path"] = self.heading_path
        data["excerpt"] = self.text[:600]
        # Preserve the join keys needed by the citation detail endpoint.
        for key in ("document_record_id", "chunk_id", "page", "filename", "source", "version", "content_fetched", "domain"):
            if key in self.metadata:
                data[key] = self.metadata[key]
        if self.is_generated:
            data["citation_allowed"] = False
            data["generated_from"] = self.generated_from
        else:
            data["citation_allowed"] = True
        return data


@dataclass
class DocumentSummary:
    document_id: str
    useful_information: str
    useful: bool
    method: str = "llm"


@dataclass
class MissingKnowledge:
    point: str
    reason: str = ""


@dataclass
class KADSDecision:
    """Auditable result of the paper's three KADS reasoning operations."""

    knowledge_requirements: List[str] = field(default_factory=list)
    requirement_mapping: Dict[str, List[str]] = field(default_factory=dict)
    irrelevant_ids: List[str] = field(default_factory=list)
    selected_ids: List[str] = field(default_factory=list)
    selection_reason: str = ""
    used_fallback: bool = False


@dataclass
class MedRGAGTrace:
    planned_queries: List[str] = field(default_factory=list)
    source_quota: int = 0
    source_status: Dict[str, str] = field(default_factory=dict)
    retrieved_count: int = 0
    retrieval_source_counts: Dict[str, int] = field(default_factory=dict)
    balanced_count: int = 0
    balanced_source_counts: Dict[str, int] = field(default_factory=dict)
    reranked_ids: List[str] = field(default_factory=list)
    summary_count: int = 0
    useful_summary_count: int = 0
    summaries: List[Dict[str, Any]] = field(default_factory=list)
    missing_knowledge: List[str] = field(default_factory=list)
    missing_knowledge_details: List[Dict[str, str]] = field(default_factory=list)
    generated_count: int = 0
    generated_documents: List[Dict[str, str]] = field(default_factory=list)
    kads_requirements: List[str] = field(default_factory=list)
    kads_mapping: Dict[str, List[str]] = field(default_factory=dict)
    kads_irrelevant_ids: List[str] = field(default_factory=list)
    kads_selection_reason: str = ""
    kads_used_fallback: bool = False
    selected_ids: List[str] = field(default_factory=list)
    selected_retrieved: int = 0
    selected_generated: int = 0
    excluded_generated_from_citations: int = 0
    degraded_stages: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MedRGAGResult:
    evidence: List[EvidenceDocument]
    external_citations: List[EvidenceDocument]
    summaries: List[DocumentSummary]
    missing_knowledge: List[MissingKnowledge]
    generated_documents: List[EvidenceDocument]
    trace: MedRGAGTrace


def _stable_id(prefix: str, *values: str) -> str:
    digest = hashlib.sha1("\n".join(values).encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _terms(text: str) -> set[str]:
    normalized = text.lower()
    latin = set(re.findall(r"[a-z0-9][a-z0-9._+-]*", normalized))
    cjk_runs = re.findall(r"[\u3400-\u9fff]+", normalized)
    cjk: set[str] = set()
    for run in cjk_runs:
        if len(run) == 1:
            cjk.add(run)
        else:
            cjk.update(run[i : i + 2] for i in range(len(run) - 1))
    return latin | cjk


def _overlap(left: str, right: str) -> float:
    a, b = _terms(left), _terms(right)
    if not a or not b:
        return 0.0
    return len(a & b) / math.sqrt(len(a) * len(b))


def _extract_relevant_passages(question: str, text: str, max_chars: int = 1000) -> str:
    """Conservative extractive fallback for false-negative LLM summaries."""
    segments = [
        item.strip()
        for item in re.split(r"(?<=[。！？!?；;])|\n+", text)
        if item.strip()
    ]
    scored = [(_overlap(question, item), index, item) for index, item in enumerate(segments)]
    useful = [(score, index, item) for score, index, item in scored if score >= 0.06]
    if not useful:
        return ""
    selected = sorted(sorted(useful, reverse=True)[:6], key=lambda item: item[1])
    output: List[str] = []
    length = 0
    for _, _, item in selected:
        if length + len(item) > max_chars and output:
            break
        output.append(item)
        length += len(item)
    return "\n".join(output)[:max_chars]


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _normalize_similarity(value: float) -> float:
    # COSINE may be [-1, 1]. Most stored scores are already [0, 1].
    return _clamp((value + 1.0) / 2.0) if value < 0 else _clamp(value)


_HIGH_AUTHORITY_DOMAINS = (
    "who.int",
    "cdc.gov",
    "nih.gov",
    "ncbi.nlm.nih.gov",
    "pubmed.ncbi.nlm.nih.gov",
    "fda.gov",
    "ema.europa.eu",
    "nhc.gov.cn",
)

_MEDICAL_REFERENCE_DOMAINS = (
    "mayoclinic.org",
    "medscape.com",
    "uptodate.com",
    "webmd.com",
)


def evidence_from_web(result: Any) -> EvidenceDocument:
    url = str(getattr(result, "url", "") or "")
    title = str(getattr(result, "title", "") or "")
    snippet = str(getattr(result, "snippet", "") or "")
    domain = urlparse(url).netloc.lower()
    if any(domain == d or domain.endswith(f".{d}") for d in _HIGH_AUTHORITY_DOMAINS):
        authority, source_kind = 0.9, "authoritative_web"
    elif any(domain == d or domain.endswith(f".{d}") for d in _MEDICAL_REFERENCE_DOMAINS):
        authority, source_kind = 0.68, "medical_reference"
    else:
        authority, source_kind = 0.4, "web"
    return EvidenceDocument(
        id=_stable_id("web", url, title, snippet),
        text=snippet,
        title=title,
        source_kind=source_kind,
        url_or_path=url,
        authority=authority,
        status="retrieved" if bool(getattr(result, "content_fetched", False)) else "snippet_only",
        metadata={
            "domain": domain,
            "content_fetched": bool(getattr(result, "content_fetched", False)),
        },
    )


def evidence_from_kb(doc: Dict[str, Any]) -> EvidenceDocument:
    metadata = dict(doc.get("metadata") or {})
    doc_type = str(metadata.get("type") or "knowledge_base")
    authority_map = {
        "clinical_guideline": 0.95,
        "drug_label": 0.95,
        "standard": 0.9,
        "textbook": 0.8,
        "document": 0.65,
        "lifestyle": 0.62,
        "knowledge_base": 0.65,
    }
    authority = authority_map.get(doc_type, 0.6)
    authority_level = str(metadata.get("authority_level") or "unknown").lower()
    if authority_level == "high":
        authority = max(authority, 0.85)
    elif authority_level == "medium":
        authority = min(authority, 0.72)
    elif authority_level == "low":
        authority = min(authority, 0.45)
    title = str(metadata.get("title") or metadata.get("filename") or metadata.get("disease") or "医学知识")
    identifier = str(doc.get("id") or _stable_id("kb", title, str(doc.get("content", ""))))
    return EvidenceDocument(
        id=f"kb-{identifier}",
        text=str(doc.get("content") or ""),
        title=title,
        source_kind=doc_type,
        url_or_path=str(metadata.get("url") or metadata.get("source") or ""),
        authority=authority,
        retrieval_score=float(doc.get("score") or 0.0),
        status="retrieved",
        heading_path=str(metadata.get("path") or ""),
        metadata=metadata,
    )


def rrf_fuse(ranked_lists: Sequence[Sequence[EvidenceDocument]], k: int = 60) -> List[EvidenceDocument]:
    """Fuse query/source rankings without assuming comparable raw scores."""
    by_id: Dict[str, EvidenceDocument] = {}
    fused: Dict[str, float] = {}
    for ranked in ranked_lists:
        seen: set[str] = set()
        for rank, doc in enumerate(ranked, start=1):
            if doc.id in seen:
                continue
            seen.add(doc.id)
            by_id.setdefault(doc.id, doc)
            fused[doc.id] = fused.get(doc.id, 0.0) + 1.0 / (k + rank)
    if not fused:
        return []
    maximum = max(fused.values())
    output = [replace(by_id[doc_id], retrieval_score=score / maximum) for doc_id, score in fused.items()]
    return sorted(output, key=lambda item: item.retrieval_score, reverse=True)


def source_balanced_candidates(
    documents: Sequence[EvidenceDocument], per_source: int = 5, max_total: int = 24
) -> List[EvidenceDocument]:
    """Take a fair candidate pool before global reranking."""
    groups: Dict[str, List[EvidenceDocument]] = {}
    for doc in documents:
        groups.setdefault(doc.source_kind, []).append(doc)
    for items in groups.values():
        items.sort(key=lambda d: d.retrieval_score, reverse=True)

    output: List[EvidenceDocument] = []
    for offset in range(per_source):
        for source in sorted(groups):
            items = groups[source]
            if offset < len(items):
                output.append(items[offset])
                if len(output) >= max_total:
                    return output
    return output


def rerank_documents(question: str, documents: Sequence[EvidenceDocument]) -> List[EvidenceDocument]:
    """Dependency-free first-stage reranker; replaceable with a cross-encoder."""
    reranked: List[EvidenceDocument] = []
    for doc in documents:
        lexical = _overlap(question, f"{doc.title}\n{doc.text}")
        dense = _normalize_similarity(doc.retrieval_score)
        score = 0.50 * dense + 0.30 * lexical + 0.20 * _clamp(doc.authority)
        reranked.append(replace(doc, rerank_score=score))
    return sorted(reranked, key=lambda d: d.rerank_score or 0.0, reverse=True)


def select_documents_kads(
    question: str, documents: Sequence[EvidenceDocument], top_k: int = 5
) -> List[EvidenceDocument]:
    """Coverage-aware KADS fallback used when the LLM selector is unavailable."""
    remaining = [d for d in documents if d.status != "rejected" and d.text.strip()]
    selected: List[EvidenceDocument] = []
    question_terms = _terms(question)
    covered: set[str] = set()

    while remaining and len(selected) < top_k:
        best: Optional[EvidenceDocument] = None
        best_value = -1.0
        for candidate in remaining:
            terms = _terms(f"{candidate.title}\n{candidate.text}")
            new_coverage = len((terms & question_terms) - covered) / max(1, len(question_terms))
            redundancy = max((_overlap(candidate.text, d.text) for d in selected), default=0.0)
            source_bonus = 0.06 if selected and all(d.source_kind != candidate.source_kind for d in selected) else 0.0
            provenance_bonus = 0.04 if not candidate.is_generated else 0.0
            base = candidate.rerank_score if candidate.rerank_score is not None else candidate.retrieval_score
            value = base + 0.25 * new_coverage + source_bonus + provenance_bonus - 0.18 * redundancy
            if value > best_value:
                best, best_value = candidate, value
        if best is None:
            break
        selected.append(best)
        covered.update(_terms(f"{best.title}\n{best.text}") & question_terms)
        remaining.remove(best)
    return selected


def _extract_json(text: str) -> Any:
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL | re.IGNORECASE)
    if fenced:
        cleaned = fenced.group(1).strip()
    for opening, closing in (("{", "}"), ("[", "]")):
        start, end = cleaned.find(opening), cleaned.rfind(closing)
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                pass
    raise ValueError("LLM response did not contain valid JSON")


class MedRGAGPipeline:
    """Paper-aligned KGCC plus provenance-aware KADS."""

    def __init__(self, llm_client: Any, top_k: int = 5, candidate_limit: int = 24):
        self.llm_client = llm_client
        self.top_k = top_k
        self.candidate_limit = candidate_limit

    async def run(
        self,
        question: str,
        retrieved: Sequence[EvidenceDocument],
        retrieval_source_counts: Optional[Dict[str, int]] = None,
        source_status: Optional[Dict[str, str]] = None,
    ) -> MedRGAGResult:
        trace = MedRGAGTrace(retrieved_count=len(retrieved))
        trace.retrieval_source_counts = retrieval_source_counts or _source_counts(retrieved)
        trace.source_status = dict(source_status or {})
        balanced = source_balanced_candidates(retrieved, max_total=self.candidate_limit)
        trace.balanced_count = len(balanced)
        trace.balanced_source_counts = _source_counts(balanced)
        reranked = rerank_documents(question, balanced)[: self.top_k]
        trace.reranked_ids = [doc.id for doc in reranked]

        summaries = await self._summarize_documents(question, reranked, trace)
        missing = await self._explore_missing_knowledge(question, summaries, trace)
        generated = await self._generate_background_documents(question, missing, trace)

        combined = rerank_documents(question, [*reranked, *generated])
        selected, decision = await self._select_documents(question, combined, trace)
        citations = [doc for doc in selected if doc.is_external_citation]

        trace.kads_requirements = decision.knowledge_requirements
        trace.kads_mapping = decision.requirement_mapping
        trace.kads_irrelevant_ids = decision.irrelevant_ids
        trace.kads_selection_reason = decision.selection_reason
        trace.kads_used_fallback = decision.used_fallback
        trace.selected_ids = [doc.id for doc in selected]
        trace.selected_retrieved = len(citations)
        trace.selected_generated = sum(doc.is_generated for doc in selected)
        trace.excluded_generated_from_citations = trace.selected_generated
        return MedRGAGResult(
            evidence=selected,
            external_citations=citations,
            summaries=summaries,
            missing_knowledge=missing,
            generated_documents=generated,
            trace=trace,
        )

    async def _summarize_documents(
        self, question: str, documents: Sequence[EvidenceDocument], trace: MedRGAGTrace
    ) -> List[DocumentSummary]:
        async def summarize(doc: EvidenceDocument) -> DocumentSummary:
            prompt = f"""你是医学证据摘要器。请只提取文档中对回答问题有用的信息，不得加入文档外知识，也不要直接回答问题。
如果文档没有有用信息，必须输出 {NO_USEFUL_INFORMATION}。
仅输出 JSON：{{"useful_information": "..."}}

示例1：问题“NF2患者常见哪种肿瘤？”，文档“NF2患者常发生双侧前庭神经鞘瘤、脑膜瘤和室管膜瘤。”
输出：{{"useful_information":"NF2与神经鞘瘤、脑膜瘤和室管膜瘤风险增加相关。"}}
示例2：问题“NF2患者常见哪种肿瘤？”，文档“糖尿病患者应控制血糖。”
输出：{{"useful_information":"{NO_USEFUL_INFORMATION}"}}

问题：{question}
文档 ID：{doc.id}
文档：{doc.text[:2400]}
"""
            response = await self.llm_client.chat(
                [{"role": "user", "content": prompt}], temperature=0.0, max_tokens=500
            )
            data = _extract_json(response)
            information = str(data.get("useful_information", "")).strip()
            useful = bool(information) and NO_USEFUL_INFORMATION.lower() not in information.lower()
            if not useful and (doc.rerank_score or 0.0) >= 0.45:
                extract = _extract_relevant_passages(question, doc.text)
                if extract:
                    trace.degraded_stages.append(f"summary_extract:{doc.id}")
                    return DocumentSummary(doc.id, extract, True, "extractive_fallback")
            return DocumentSummary(
                doc.id,
                information or NO_USEFUL_INFORMATION,
                useful,
                "llm" if useful else "llm_rejected",
            )

        if not documents:
            return []
        results = await asyncio.gather(*(summarize(doc) for doc in documents), return_exceptions=True)
        summaries: List[DocumentSummary] = []
        for doc, result in zip(documents, results):
            if isinstance(result, Exception):
                trace.degraded_stages.append(f"summary:{doc.id}")
                summaries.append(DocumentSummary(doc.id, NO_USEFUL_INFORMATION, False))
            else:
                summaries.append(result)
        trace.summary_count = len(summaries)
        trace.useful_summary_count = sum(summary.useful for summary in summaries)
        trace.summaries = [
            {
                "document_id": summary.document_id,
                "useful": summary.useful,
                "useful_information": summary.useful_information[:1000],
                "method": summary.method,
            }
            for summary in summaries
        ]
        return summaries

    async def _explore_missing_knowledge(
        self, question: str, summaries: Sequence[DocumentSummary], trace: MedRGAGTrace
    ) -> List[MissingKnowledge]:
        useful = [summary for summary in summaries if summary.useful]
        context = "\n".join(
            f"[{summary.document_id}] {summary.useful_information}" for summary in useful
        ) or NO_USEFUL_INFORMATION
        prompt = f"""你是医学知识缺口分析器。根据问题和检索摘要，识别回答问题仍缺少的最多 3 个关键且互不重复的知识点。
只输出 JSON：{{"missing_knowledge": [{{"point": "简短知识点", "reason": "为什么缺失"}}]}}
如果证据已充分，数组可以为空。不要回答原问题。

问题：{question}
检索摘要：
{context[:6000]}
"""
        try:
            response = await self.llm_client.chat(
                [{"role": "user", "content": prompt}], temperature=0.0, max_tokens=700
            )
            data = _extract_json(response)
            raw = data.get("missing_knowledge", [])
            missing = [
                MissingKnowledge(str(item.get("point", "")).strip(), str(item.get("reason", "")).strip())
                for item in raw
                if isinstance(item, dict) and str(item.get("point", "")).strip()
            ][:3]
        except Exception:
            trace.degraded_stages.append("explore")
            missing = []
        trace.missing_knowledge = [item.point for item in missing]
        trace.missing_knowledge_details = [asdict(item) for item in missing]
        return missing

    async def _generate_background_documents(
        self, question: str, missing: Sequence[MissingKnowledge], trace: MedRGAGTrace
    ) -> List[EvidenceDocument]:
        prompts: List[tuple[str, str]] = []
        for item in missing[: self.top_k]:
            prompts.append((item.point, self._generation_prompt(question, item.point)))
        while len(prompts) < self.top_k:
            label = "question-conditioned"
            prompts.append((label, self._generation_prompt(question, "")))

        async def generate(index: int, origin: str, prompt: str) -> EvidenceDocument:
            response = await self.llm_client.chat(
                [{"role": "user", "content": prompt}], temperature=0.7, max_tokens=600
            )
            text = response.strip()
            return EvidenceDocument(
                id=_stable_id("gen", question, origin, str(index), text),
                text=text,
                title=f"Generated background: {origin}",
                source_kind="generated",
                authority=0.0,
                retrieval_score=0.0,
                status="generated",
                generated_from=origin,
            )

        results = await asyncio.gather(
            *(generate(index, origin, prompt) for index, (origin, prompt) in enumerate(prompts)),
            return_exceptions=True,
        )
        documents: List[EvidenceDocument] = []
        for index, result in enumerate(results):
            if isinstance(result, Exception):
                trace.degraded_stages.append(f"generate:{index}")
            elif result.text:
                documents.append(result)
        trace.generated_count = len(documents)
        trace.generated_documents = [
            {"id": doc.id, "generated_from": doc.generated_from, "status": doc.status}
            for doc in documents
        ]
        return documents

    @staticmethod
    def _generation_prompt(question: str, knowledge_point: str) -> str:
        focus = (
            f"只围绕这个缺失知识点生成：{knowledge_point}。"
            if knowledge_point
            else "基于问题生成一个不同视角的相关医学背景。"
        )
        return f"""你是医学背景文档生成器。{focus}
要求：不猜测最终答案，不声称自己引用了文献，不伪造来源；内容简洁、独立且不超过 300 中文字。只返回背景正文。

问题：{question}
"""

    async def _select_documents(
        self, question: str, documents: Sequence[EvidenceDocument], trace: MedRGAGTrace
    ) -> tuple[List[EvidenceDocument], KADSDecision]:
        candidates = list(documents)
        rendered = "\n".join(
            f"[{index}] source={doc.source_kind}; citation_allowed={doc.is_external_citation}; "
            f"status={doc.status}; title={doc.title}; text={doc.text[:900]}"
            for index, doc in enumerate(candidates)
        )
        prompt = f"""你是 KADS 医学证据选择器。严格执行三个步骤：
1. Knowledge Requirement Identification：列出回答问题所需的关键知识。
2. Requirement-to-Passage Mapping：把每个候选文档映射到知识需求；无用文档放入 irrelevant_indices。
3. Balanced Evidence Selection：选择最多 {self.top_k} 条互补、低冗余且覆盖完整的文档。
检索文档可以作为事实来源；生成文档只可补充推理且可能不准确。
仅输出 JSON：
{{"knowledge_requirements":["需求"],"requirement_mapping":{{"需求":[0,1]}},"irrelevant_indices":[2],"selected_indices":[0,1],"selection_reason":"覆盖与去冗余理由"}}。

问题：{question}
候选文档：
{rendered[:12000]}
"""
        try:
            response = await self.llm_client.chat(
                [{"role": "user", "content": prompt}], temperature=0.0, max_tokens=400
            )
            data = _extract_json(response)
            indices = []
            for value in data.get("selected_indices", []):
                try:
                    index = int(value)
                except (TypeError, ValueError):
                    continue
                if 0 <= index < len(candidates) and index not in indices:
                    indices.append(index)
            selected = [candidates[index] for index in indices[: self.top_k]]
            if selected:
                requirements = [
                    str(item).strip() for item in data.get("knowledge_requirements", [])
                    if str(item).strip()
                ]
                mapping: Dict[str, List[str]] = {}
                raw_mapping = data.get("requirement_mapping", {})
                if isinstance(raw_mapping, dict):
                    for requirement, values in raw_mapping.items():
                        mapped_ids: List[str] = []
                        for value in values if isinstance(values, list) else []:
                            try:
                                mapped_index = int(value)
                            except (TypeError, ValueError):
                                continue
                            if 0 <= mapped_index < len(candidates):
                                mapped_ids.append(candidates[mapped_index].id)
                        mapping[str(requirement)] = mapped_ids
                irrelevant_ids: List[str] = []
                for value in data.get("irrelevant_indices", []):
                    try:
                        irrelevant_index = int(value)
                    except (TypeError, ValueError):
                        continue
                    if 0 <= irrelevant_index < len(candidates):
                        irrelevant_ids.append(candidates[irrelevant_index].id)
                decision = KADSDecision(
                    knowledge_requirements=requirements,
                    requirement_mapping=mapping,
                    irrelevant_ids=irrelevant_ids,
                    selected_ids=[doc.id for doc in selected],
                    selection_reason=str(data.get("selection_reason", "")).strip(),
                )
                return selected, decision
        except Exception:
            trace.degraded_stages.append("kads")
        selected = select_documents_kads(question, candidates, top_k=self.top_k)
        fallback_requirement = question.strip()[:200] or "回答用户医学问题"
        return selected, KADSDecision(
            knowledge_requirements=[fallback_requirement],
            requirement_mapping={fallback_requirement: [doc.id for doc in selected]},
            irrelevant_ids=[doc.id for doc in candidates if doc not in selected],
            selected_ids=[doc.id for doc in selected],
            selection_reason="LLM KADS 不可用，使用覆盖率、来源多样性和去冗余规则降级选择。",
            used_fallback=True,
        )


def _source_counts(documents: Sequence[EvidenceDocument]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for doc in documents:
        counts[doc.source_kind] = counts.get(doc.source_kind, 0) + 1
    return counts


__all__ = [
    "NO_USEFUL_INFORMATION",
    "DocumentSummary",
    "EvidenceDocument",
    "MedRGAGPipeline",
    "MedRGAGResult",
    "MedRGAGTrace",
    "MissingKnowledge",
    "KADSDecision",
    "evidence_from_kb",
    "evidence_from_web",
    "rerank_documents",
    "rrf_fuse",
    "select_documents_kads",
    "source_balanced_candidates",
]
