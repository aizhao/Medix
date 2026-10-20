"""
深度研究工作流

编排多步骤研究流程：查询规划 → 搜索 → 检索 → 综合 → 验证
"""
from typing import List, Dict, Any, Optional
from loguru import logger
import asyncio
import os

from core import LLMClient
from research.web_search import WebSearchTool, SearchResult
from knowledge.milvus_kb import MedicalKnowledgeBase
from research.evidence_synthesizer import EvidenceSynthesizer, ResearchReport
from research.medrgag import (
    EvidenceDocument,
    MedRGAGPipeline,
    evidence_from_kb,
    evidence_from_web,
    rrf_fuse,
)

# 全局知识库实例（单例）
_kb_instance = None

DEFAULT_KB_SOURCE_TYPES = (
    "clinical_guideline",
    "drug_label",
    "textbook",
    "research",
    "standard",
    "document",
    "unclassified",
)

def get_knowledge_base():
    """获取知识库单例"""
    global _kb_instance
    if _kb_instance is None:
        _kb_instance = MedicalKnowledgeBase()
    return _kb_instance


class DeepResearchWorkflow:
    """
    深度研究工作流

    功能：
    - 多步骤研究流程编排
    - 查询规划和优化
    - 并行搜索和检索
    - 证据综合和质量控制
    """

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        use_web_search: bool = True,
        use_knowledge_base: bool = True,
        use_medrgag: bool = True,
        kb_source_types: Optional[List[str]] = None,
    ):
        """
        初始化工作流

        Args:
            llm_client: LLM 客户端
            use_web_search: 是否使用网络搜索
            use_knowledge_base: 是否使用 Milvus 知识库
        """
        self.llm_client = llm_client or LLMClient()
        self.use_web_search = use_web_search
        self.use_knowledge_base = use_knowledge_base
        self.use_medrgag = use_medrgag
        configured_sources = os.getenv("MEDIX_KB_SOURCE_TYPES", "")
        self.kb_source_types = kb_source_types or [
            item.strip() for item in configured_sources.split(",") if item.strip()
        ] or list(DEFAULT_KB_SOURCE_TYPES)
        self.per_source_quota = max(1, int(os.getenv("MEDIX_RETRIEVAL_PER_SOURCE", "3")))

        # 初始化组件
        web_timeout = max(2, int(os.getenv("MEDIX_WEB_SEARCH_TIMEOUT", "8")))
        self.web_search = WebSearchTool(timeout=web_timeout) if use_web_search else None
        # 使用 Milvus 知识库单例（和其他 Skills 共享，避免重复加载模型）
        self.knowledge_base = get_knowledge_base() if use_knowledge_base else None
        self.synthesizer = EvidenceSynthesizer(llm_client=self.llm_client)
        self.medrgag = MedRGAGPipeline(llm_client=self.llm_client) if use_medrgag else None

    async def run(
        self,
        question: str,
        max_web_results: int = 10,
        max_kb_results: int = 5,
        context: Optional[Dict[str, Any]] = None,
        agent_findings: Optional[List[Dict[str, Any]]] = None,
    ) -> ResearchReport:
        """
        执行深度研究

        Args:
            question: 研究问题
            max_web_results: 最大网络搜索结果数
            max_kb_results: 最大知识库检索结果数

        Returns:
            研究报告
        """
        logger.info(
            f"Starting DeepResearch for: {question} "
            f"(agent_findings={len(agent_findings or [])})"
        )

        # Step 1: 查询规划
        sub_queries = await self._plan_queries(question)
        logger.info(f"Planned {len(sub_queries)} sub-queries")

        # Step 2: 并行搜索
        web_results: List[SearchResult] = []
        kb_results: List[Dict[str, Any]] = []

        search_tasks = []
        task_kinds = []
        ranked_lists = []
        agent_evidence = self._agent_findings_to_evidence(agent_findings or [])
        agent_has_external = any(
            bool(item.url_or_path)
            or bool(item.metadata.get("source_url"))
            or bool(item.metadata.get("document_record_id"))
            or bool(item.metadata.get("chunk_id"))
            for item in agent_evidence
        )
        # 本地 Milvus 文档 ID 不能代表网络搜索已成功；只有 URL/来源 URL
        # 存在时，才允许跳过网络检索，避免把 Agent 摘要误判成外部证据。
        agent_has_web = any(
            str(item.url_or_path).lower().startswith(("http://", "https://"))
            or str(item.metadata.get("source_url", "")).lower().startswith(("http://", "https://"))
            for item in agent_evidence
        )
        source_status = {
            **({"web": "pending"} if self.use_web_search else {}),
            **({source: "pending" for source in self.kb_source_types} if self.use_knowledge_base else {}),
        }
        planned_queries: List[str] = []
        for item in [question, *sub_queries]:
            normalized = item.strip()
            if normalized and normalized not in planned_queries:
                planned_queries.append(normalized)
        planned_queries = planned_queries[:3]

        # ResearchAgent 可能已经调用 deep_research。此时复用其结构化
        # tool_results，避免 MedRGAG 再次完整执行网络研究；若没有可引用
        # 的 Agent 证据，则保留本地知识库作为安全兜底。
        if agent_evidence:
            ranked_lists.append(agent_evidence)
            source_status["agent_findings"] = "available"

        run_web = self.use_web_search and self.web_search and not agent_has_web
        run_kb = self.use_knowledge_base and self.knowledge_base and not agent_has_external
        logger.info(
            "Retrieval plan: agent_evidence={}, agent_has_web={}, run_web={}, run_kb={}",
            len(agent_evidence),
            agent_has_web,
            bool(run_web),
            bool(run_kb),
        )

        if run_web:
            # 网络搜索
            for query in planned_queries:
                task_kinds.append("web")
                search_tasks.append(
                    self.web_search.search_evidence(
                        query,
                        max_results=min(max(1, max_web_results), self.per_source_quota * 3),
                    )
                )

        if run_kb:
            # 从 Milvus 知识库检索
            for query in planned_queries:
                task_kinds.append("kb_sources")
                search_tasks.append(
                    self._search_milvus_by_sources(
                        query,
                        top_k_per_source=min(max(1, max_kb_results), self.per_source_quota),
                    )
                )

        # 并行执行
        if search_tasks:
            results = await asyncio.gather(*search_tasks, return_exceptions=True)

            # 分离结果
            for kind, result in zip(task_kinds, results):
                if isinstance(result, Exception):
                    logger.warning(f"Search task failed: {result}")
                    if kind == "web":
                        source_status["web"] = "failed"
                    elif kind == "kb_sources":
                        for source_type in self.kb_source_types:
                            if source_status.get(source_type) == "pending":
                                source_status[source_type] = "failed"
                    continue

                if kind == "web" and isinstance(result, list):
                    web_results.extend(result)
                    source_status["web"] = "available" if result else "no_results_or_unavailable"
                    grouped_web: Dict[str, List[Any]] = {}
                    for item in result:
                        evidence = evidence_from_web(item)
                        grouped_web.setdefault(evidence.source_kind, []).append(evidence)
                    ranked_lists.extend(grouped_web.values())
                elif kind == "kb_sources" and isinstance(result, dict):
                    for source_type in self.kb_source_types:
                        source_status[source_type] = "available" if result.get(source_type) else "no_results"
                    for source_type, source_results in result.items():
                        kb_results.extend(source_results)
                        evidence_list = [evidence_from_kb(item) for item in source_results]
                        if evidence_list:
                            ranked_lists.append(evidence_list)

        logger.info(f"Collected {len(web_results)} web results, {len(kb_results)} KB results")

        # Step 3: 来源平衡检索 → 原文三步 KGCC → KADS 选择 → 证据综合
        if self.use_medrgag and self.medrgag:
            initial_evidence = rrf_fuse(ranked_lists)
            medrgag_result = await self.medrgag.run(
                question,
                initial_evidence,
                source_status=source_status,
            )
            medrgag_result.trace.planned_queries = planned_queries
            medrgag_result.trace.source_quota = self.per_source_quota
            report = await self.synthesizer.synthesize_evidence(
                query=question,
                evidence=medrgag_result.evidence,
                evidence_trace=medrgag_result.trace.to_dict(),
                unresolved_knowledge=[item.point for item in medrgag_result.missing_knowledge],
            )
        else:
            report = await self.synthesizer.synthesize(
                query=question,
                web_results=web_results,
                kb_results=kb_results
            )
        if not report.key_findings:
            logger.warning("Report has no key findings")

        if not report.summary:
            logger.warning("Report has no summary")

        logger.info("DeepResearch completed")
        return report

    @staticmethod
    def _agent_findings_to_evidence(
        agent_findings: List[Dict[str, Any]],
    ) -> List[EvidenceDocument]:
        """Convert completed Worker tool outputs into MedRGAG candidates."""
        documents: List[EvidenceDocument] = []
        for finding_index, finding in enumerate(agent_findings):
            if not isinstance(finding, dict):
                continue
            tool_results = finding.get("tool_results") or []
            if not isinstance(tool_results, list):
                continue
            for result_index, item in enumerate(tool_results):
                if not isinstance(item, dict):
                    continue
                tool = str(item.get("tool", "agent_finding"))
                payload = item.get("result")
                if not isinstance(payload, dict):
                    payload = {"answer": str(payload)}

                sources = payload.get("citations") or payload.get("sources") or []
                if isinstance(sources, list):
                    for source_index, source in enumerate(sources):
                        if not isinstance(source, dict):
                            continue
                        text = str(
                            source.get("excerpt")
                            or source.get("content")
                            or source.get("snippet")
                            or source.get("text")
                            or ""
                        ).strip()
                        if not text:
                            continue
                        source_type = str(source.get("type") or source.get("source_kind") or tool)
                        url = str(source.get("url") or source.get("source_url") or "")
                        metadata = dict(source.get("metadata") or {})
                        for key in ("document_record_id", "chunk_id", "page", "filename", "source", "publisher", "published_at", "version"):
                            if key in source:
                                metadata[key] = source[key]
                        documents.append(EvidenceDocument(
                            id=str(source.get("id") or f"agent-{finding_index}-{result_index}-{source_index}"),
                            text=text,
                            title=str(source.get("title") or tool),
                            source_kind=source_type,
                            url_or_path=url,
                            authority=float(source.get("authority", 0.7) or 0.7),
                            retrieval_score=float(source.get("score", source.get("retrieval_score", 0.5)) or 0.5),
                            status=str(source.get("status", "retrieved")),
                            metadata=metadata,
                        ))

                answer = str(payload.get("answer") or "").strip()
                if answer and not sources:
                    # Agent 自己生成的总结不是外部引用，只作为候选上下文；
                    # 其 citation_allowed 状态由 snippet_only 明确标记。
                    documents.append(EvidenceDocument(
                        id=f"agent-summary-{finding_index}-{result_index}",
                        text=answer[:5000],
                        title=f"{tool} Agent 摘要",
                        source_kind="agent_finding",
                        authority=0.25,
                        retrieval_score=0.35,
                        status="snippet_only",
                        metadata={"agent_tool": tool, "citation_allowed": False},
                    ))
        return documents

    async def _search_milvus(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        从 Milvus 知识库搜索

        Args:
            query: 查询文本
            top_k: 返回结果数量

        Returns:
            文档列表（字典格式）
        """
        try:
            results = self.knowledge_base.search(query=query, top_k=top_k, filter_type=None)
            logger.debug(f"Milvus search returned {len(results)} results for: {query[:50]}...")
            return results
        except Exception as e:
            logger.error(f"Milvus search failed: {e}")
            return []

    async def _search_milvus_by_sources(
        self,
        query: str,
        top_k_per_source: int,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Retrieve an equal quota from every configured KB corpus."""
        try:
            if hasattr(self.knowledge_base, "search_by_sources"):
                return self.knowledge_base.search_by_sources(
                    query=query,
                    source_types=self.kb_source_types,
                    top_k_per_source=top_k_per_source,
                )
            output: Dict[str, List[Dict[str, Any]]] = {}
            for source_type in self.kb_source_types:
                results = self.knowledge_base.search(
                    query=query,
                    top_k=top_k_per_source,
                    filter_type=source_type,
                )
                if results:
                    output[source_type] = results
            return output
        except Exception as exc:
            logger.error(f"Source-balanced Milvus search failed: {exc}")
            return {}

    async def _plan_queries(self, question: str) -> List[str]:
        """
        查询规划：将复杂问题拆解为多个子查询

        Args:
            question: 原始问题

        Returns:
            子查询列表
        """
        prompt = f"""你是医学研究助手。请将以下问题拆解为 2-3 个更具体的子查询，以便进行深度研究。

原始问题：{question}

要求：
1. 每个子查询应该聚焦一个特定方面
2. 子查询应该互补，覆盖问题的不同角度
3. 子查询应该简洁明确

输出格式：
每行一个子查询，不需要编号。

示例：
原始问题：2型糖尿病如何治疗？
子查询1：2型糖尿病的药物治疗方案
子查询2：2型糖尿病的生活方式管理
子查询3：2型糖尿病的并发症预防
"""

        try:
            response = await self.llm_client.chat([
                {"role": "user", "content": prompt}
            ])

            # 解析子查询
            lines = response.strip().split('\n')
            sub_queries = []

            for line in lines:
                line = line.strip()
                # 移除可能的编号
                line = line.lstrip('0123456789.-:：）) ')
                if line and len(line) > 5:  # 过滤太短的行
                    sub_queries.append(line)

            # 至少包含原始问题
            if not sub_queries:
                sub_queries = [question]

            # 限制数量
            sub_queries = sub_queries[:3]

            return sub_queries

        except Exception as e:
            logger.error(f"Query planning error: {e}")
            # 降级：返回原始问题
            return [question]

    async def research_with_refinement(
        self,
        question: str,
        max_iterations: int = 2
    ) -> ResearchReport:
        """
        带细化的研究（多轮迭代）

        Args:
            question: 研究问题
            max_iterations: 最大迭代次数

        Returns:
            最终研究报告
        """
        logger.info(f"Starting iterative research (max_iterations={max_iterations})")

        report = None

        for iteration in range(max_iterations):
            logger.info(f"Iteration {iteration + 1}/{max_iterations}")

            # 执行研究
            report = await self.run(question)

            # 检查质量
            if report.confidence >= 0.7 and len(report.key_findings) >= 3:
                logger.info(f"High-quality report achieved in iteration {iteration + 1}")
                break

            # 如果是最后一轮，直接返回
            if iteration == max_iterations - 1:
                break

            # 细化查询（基于当前结果）
            if report.key_findings:
                question = f"{question}（关注：{report.key_findings[0]}）"

        return report


# 便捷函数
async def deep_research(
    question: str,
    use_web: bool = True,
    use_kb: bool = True
) -> ResearchReport:
    """
    快速执行深度研究

    Args:
        question: 研究问题
        use_web: 是否使用网络搜索
        use_kb: 是否使用知识库

    Returns:
        研究报告
    """
    workflow = DeepResearchWorkflow(
        use_web_search=use_web,
        use_knowledge_base=use_kb
    )
    return await workflow.run(question)
