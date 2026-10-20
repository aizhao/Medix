"""
SwarmCoordinator：Swarm 入口和智能路由

注意：这不是编排器！
- 只负责路由决策：简单问题 → 单 Agent，复杂问题 → Swarm
- 不控制 Agent 执行
- 不编排任务顺序

类比：交通信号灯，决定车辆走哪条路，但不控制车辆如何行驶
"""
import asyncio
import os
import uuid
from datetime import datetime
from typing import Dict, Any, Optional, List
from loguru import logger

from core import LLMClient
from .shared_context import SharedContext
from .lead_agent import LeadAgent
from .events import Event, EventType
from agents import ConsultationAgent, DiagnosticAgent, ResearchAgent
from memory import SessionSummaryManager, SessionSummary, SQLiteShortTermMemory, LongTermMemory
from backend.storage import SQLiteStore
from research.query_router import classify_query, urgent_response


class SwarmCoordinator:
    """
    Swarm 协调器

    职责：
    1. 智能路由（简单 → 单 Agent，复杂 → Swarm）
    2. 初始化 SharedContext
    3. 启动和监控 Swarm
    4. 生成 SessionSummary

    不做：
    - 不编排 Worker 执行顺序
    - 不直接调用 Worker
    - 不控制任务分配
    """

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        enable_swarm: bool = True,
        conversation_store: Optional[Any] = None,
    ):
        self.llm_client = llm_client or LLMClient()
        self.enable_swarm = enable_swarm

        # 初始化 Agent
        self.lead_agent = LeadAgent(llm_client=self.llm_client)
        self.consultation_agent = ConsultationAgent()
        self.diagnostic_agent = DiagnosticAgent()
        self.research_agent = ResearchAgent()

        # Worker 池
        self.worker_pool: List[Any] = [
            self.consultation_agent,
            self.diagnostic_agent,
            self.research_agent
        ]

        # 记忆管理器
        self.session_manager = SessionSummaryManager()
        # 所有运行入口统一使用 SQLite；API 可注入已有连接，CLI/直接调用则复用默认数据库。
        self.conversation_store = conversation_store or SQLiteStore()
        self.short_term_memory = SQLiteShortTermMemory(self.conversation_store)
        self.long_term_memory = LongTermMemory()
        self._evidence_workflow = None

        # 将短期记忆注入到所有 Worker Agent 的 Loop
        # 注意：LeadAgent 不继承 BaseAgent，没有 loop 属性，不需要注入
        for worker in self.worker_pool:
            if hasattr(worker, 'loop'):
                worker.loop.short_term_memory = self.short_term_memory

        logger.info(f"SwarmCoordinator initialized with {len(self.worker_pool)} workers")
        logger.info(f"Memory system: short_term={self.short_term_memory.storage_type}, long_term={'enabled' if self.long_term_memory.enabled else 'disabled'}")

    def _get_agent_by_id(self, agent_id: str):
        """根据 agent_id 返回对应的 Agent 实例"""
        mapping = {
            "consultation_agent": self.consultation_agent,
            "diagnostic_agent": self.diagnostic_agent,
            "research_agent": self.research_agent
        }
        return mapping.get(agent_id)

    def _get_evidence_workflow(self):
        """Lazily load embeddings and retrieval components on first medical query."""
        if self._evidence_workflow is None:
            from research.trusted_qa import EvidenceFirstMedicalWorkflow

            self._evidence_workflow = EvidenceFirstMedicalWorkflow(llm_client=self.llm_client)
        return self._evidence_workflow

    async def _run_evidence_stage(
        self,
        question: str,
        context: Optional[Dict[str, Any]],
        route: Any,
        agent_findings: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Run MedRGAG as a stage in the unified graph, not as a top-level route."""
        route_data = route.to_dict() if hasattr(route, "to_dict") else dict(route or {})
        if route_data.get("route") != "medical_evidence":
            return None
        try:
            workflow = self._get_evidence_workflow()
            evidence_timeout = max(5.0, float(os.getenv("MEDIX_MEDRGAG_TIMEOUT", "120")))

            async def invoke_workflow():
                try:
                    return await workflow.run(
                        question,
                        context,
                        agent_findings=agent_findings or [],
                    )
                except TypeError:
                    # Compatibility for legacy workflow adapters and test doubles.
                    return await workflow.run(question, context)

            result = await asyncio.wait_for(invoke_workflow(), timeout=evidence_timeout)
            result.setdefault("evidence_trace", {})["route"] = route_data
            result["evidence_trace"]["agent_findings_count"] = len(agent_findings or [])
            return result
        except asyncio.TimeoutError:
            logger.warning(
                "MedRGAG evidence stage timed out after {}s; returning a safe degraded result",
                os.getenv("MEDIX_MEDRGAG_TIMEOUT", "120"),
            )
            return {
                "answer": (
                    "## 当前无法给出可靠的医学结论\n\n"
                    "MedRGAG 证据核验超时。为避免输出未经核验的医学事实，"
                    "本次不提供推测性结论，请稍后重试或咨询医生、药师。"
                ),
                "mode": "evidence_augmented_timeout",
                "agents_involved": ["medrgag_pipeline", "evidence_gate"],
                "citations": [],
                "evidence_trace": {
                    "pipeline": "unified_medrgag",
                    "route": route_data,
                    "agent_findings_count": len(agent_findings or []),
                    "evidence_gate": {"status": "refused", "reasons": ["MedRGAG 证据阶段超时"]},
                },
                "warnings": ["MedRGAG 证据阶段超时，已安全降级。"],
                "disclaimer": "未完成证据核验，本次不提供医学结论。",
            }
        except Exception as exc:
            logger.exception(f"MedRGAG evidence stage failed: {exc}")
            return {
                "answer": (
                    "## 当前无法给出可靠的医学结论\n\n"
                    "统一 Agent 流程已完成，但 MedRGAG 证据阶段暂时不可用。"
                    "为避免无依据回答，本次不输出医学事实结论，请稍后重试或咨询医生、药师。"
                ),
                "mode": "evidence_augmented",
                "agents_involved": ["medrgag_pipeline", "evidence_gate"],
                "citations": [],
                "evidence_trace": {
                    "pipeline": "unified_medrgag",
                    "route": route_data,
                    "evidence_gate": {"status": "refused", "reasons": ["MedRGAG 证据阶段失败"]},
                },
                "warnings": [f"MedRGAG 证据阶段失败：{str(exc)[:300]}"],
                "disclaimer": "未完成证据核验，本次不提供医学结论。",
            }

    def _save_long_term_result(
        self,
        session_id: str,
        question: str,
        result: Dict[str, Any],
        mode: str,
        started_at: datetime,
        user_id: str,
    ) -> None:
        try:
            self.long_term_memory.add_session_summary(
                session_id=session_id,
                question=question,
                answer=str(result.get("answer") or ""),
                user_id=user_id,
                metadata={
                    "mode": mode,
                    "total_time": (datetime.now() - started_at).total_seconds(),
                },
            )
        except Exception as exc:
            logger.error(f"Failed to save to long-term memory: {exc}")

    async def process(
        self,
        question: str,
        context: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        enable_swarm: Optional[bool] = None,
        user_id: str = "anonymous",
    ) -> Dict[str, Any]:
        """
        处理用户问题

        Args:
            question: 用户问题
            context: 额外上下文（年龄、既往史等）
            session_id: 会话ID（如果不提供，将自动生成）

        Returns:
            处理结果
        """
        start_time = datetime.now()
        swarm_enabled_for_request = self.enable_swarm if enable_swarm is None else enable_swarm
        if session_id is None:
            session_id = f"{start_time.strftime('%Y%m%d-%H%M%S')}-{str(uuid.uuid4())[:8]}"

        if hasattr(self.short_term_memory, "begin_turn"):
            self.short_term_memory.begin_turn(session_id, question)

        logger.info(f"Processing question (session={session_id}): {question[:50]}...")

        # ===== 统一的记忆检索（所有模式都使用）=====
        # 1. 检索短期记忆（当前会话历史）
        recent_history = self.short_term_memory.get_recent_messages(
            session_id=session_id,
            limit=10  # 最近5轮对话（10条消息）
        )

        # 2. 检索长期记忆（相似历史会话）
        similar_memories = self.long_term_memory.search_similar_sessions(
            query=question,
            limit=3,
            user_id=user_id,
        )

        # 3. 构建增强上下文
        enhanced_context = dict(context or {})

        # 添加短期记忆
        if recent_history:
            enhanced_context["recent_history"] = [
                {"role": msg.get("role", ""), "content": msg.get("content", "")}
                for msg in recent_history
            ]
            logger.info(f"Loaded {len(recent_history)} recent messages from short-term memory")

        # 添加长期记忆
        if similar_memories:
            enhanced_context["historical_cases"] = [
                {
                    "summary": mem["content"],
                    "score": mem["score"]
                }
                for mem in similar_memories
            ]
            logger.info(f"Found {len(similar_memories)} similar historical cases from long-term memory")

        # Evidence routing is deterministic: factual medical requests cannot
        # skip retrieval merely because an Agent chose not to call a tool.
        query_route = classify_query(question, enhanced_context)
        logger.info(f"Deterministic route: {query_route.route} ({query_route.reason})")
        if query_route.route == "urgent":
            result = urgent_response(question)
            result["session_id"] = session_id
            result["route_reason"] = query_route.reason
            self._save_long_term_result(session_id, question, result, "urgent_safety", start_time, user_id)
            return result

        # Routing now selects a policy, not a separate execution graph.  All
        # non-urgent requests continue through LeadAgent and the Worker pool;
        # MedRGAG 不参与任务拆解；它在 Worker 产出后、最终生成前运行。
        enhanced_context["route_policy"] = query_route.to_dict()

        # Step 1: LeadAgent 分解任务
        assessment = await self.lead_agent.assess_and_decompose(question, enhanced_context)
        subtasks = assessment.get("subtasks", [])

        logger.info(f"LeadAgent 分解任务：{len(subtasks)} 个")

        # Step 2: 根据任务数量路由
        final_answer = None
        mode = None
        evidence_result = None

        if len(subtasks) == 1:
            # 单任务 → 直接调用对应 Agent
            task = subtasks[0]
            agent_id = task.get("assigned_agent")
            agent = self._get_agent_by_id(agent_id)

            if agent is None:
                # 如果找不到 Agent，降级到 ConsultationAgent
                logger.warning(f"Unknown agent_id: {agent_id}, fallback to ConsultationAgent")
                agent = self.consultation_agent

            logger.info(f"Route: Single Agent ({agent_id})")
            mode = "single_agent"
            worker_context = dict(enhanced_context)
            evidence_result = await self._run_evidence_stage(
                question=question,
                context=worker_context,
                route=query_route,
            )
            if evidence_result is not None:
                worker_context["medrgag_evidence"] = evidence_result
                logger.info(
                    "{}: pre-generation MedRGAG completed (citations={})",
                    agent.agent_id,
                    len(evidence_result.get("citations", [])),
                )
            result = await agent.process({
                'question': question,
                'context': worker_context,
                'session_id': session_id
            })
            final_answer = result.get('answer', '')

            if evidence_result is not None:
                gate_status = (
                    evidence_result.get("evidence_trace", {})
                    .get("evidence_gate", {})
                    .get("status")
                )
                result.update({
                    "citations": evidence_result.get("citations", []),
                    "evidence_trace": evidence_result.get("evidence_trace", {}),
                    "warnings": evidence_result.get("warnings", []),
                    "disclaimer": evidence_result.get("disclaimer", result.get("disclaimer", "")),
                    "agents_involved": [agent.agent_id, *evidence_result.get("agents_involved", [])],
                    "mode": "medrgag_guided_single",
                })
                # 证据门禁拒绝时，Agent 生成结果不能越过安全边界。
                if gate_status == "refused":
                    result["answer"] = evidence_result.get("answer", final_answer)
                else:
                    result["answer"] = await self.lead_agent.synthesize_single_result(
                        question=question,
                        agent_result=result,
                        evidence=evidence_result,
                    )
                final_answer = result["answer"]

            result.update({
                'swarm_enabled': False,
                'session_id': session_id,
                'route_reason': f'单任务路由到 {agent_id}'
            })

            # 确保单Agent模式下也有 disclaimer 字段
            if 'disclaimer' not in result:
                result['disclaimer'] = "⚠️ 以上信息仅供参考，不能替代专业医生的诊断和治疗。如有疑虑，请及时就医。"

            # 确保单Agent模式下也有 suggestions 字段
            if 'suggestions' not in result:
                result['suggestions'] = []

        elif len(subtasks) >= 2 and swarm_enabled_for_request:
            # 多任务 → 启动 Swarm
            logger.info(f"Route: Swarm (Multi-Agent Collaboration) - {len(subtasks)} tasks")
            mode = "swarm"
            result = await self._process_with_swarm(
                question=question,
                context=enhanced_context,
                assessment=assessment,
                session_id=session_id,
                start_time=start_time,
                user_id=user_id
            )
            final_answer = result.get('answer', '')

            # Swarm 模式已经在 _process_with_swarm 中保存了长期记忆，直接返回
            return result

        else:
            # 0个任务或Swarm关闭 → 降级到 ConsultationAgent
            if len(subtasks) == 0:
                logger.warning("No subtasks generated, fallback to ConsultationAgent")
                mode = "fallback"
            else:
                logger.info("Swarm disabled, fallback to ConsultationAgent")
                mode = "disabled_swarm"

            result = await self.consultation_agent.process({
                'question': question,
                'context': enhanced_context,
                'session_id': session_id
            })
            final_answer = result.get('answer', '')
            result.update({
                'swarm_enabled': False,
                'session_id': session_id
            })

        # ===== 统一的记忆保存（非 Swarm 模式）=====
        end_time = datetime.now()

        # 注意：短期记忆已经在 Agent Loop 中保存了，这里不需要重复保存

        # 保存到长期记忆
        try:
            self.long_term_memory.add_session_summary(
                session_id=session_id,
                question=question,
                answer=final_answer,
                user_id=user_id,
                metadata={
                    "mode": mode,
                    "subtasks_count": len(subtasks),
                    "total_time": (end_time - start_time).total_seconds(),
                }
            )
            logger.info(f"Saved to long-term memory (session={session_id}, mode={mode})")
        except Exception as e:
            logger.error(f"Failed to save to long-term memory: {e}")

        return result

    async def _process_with_swarm(
        self,
        question: str,
        context: Optional[Dict[str, Any]],
        assessment: Dict[str, Any],
        session_id: str,
        start_time: datetime,
        user_id: str = "anonymous"
    ) -> Dict[str, Any]:
        """
        使用 Swarm 处理复杂问题

        这是群体智能的核心流程

        注意：context 已经包含了长短期记忆（在 process() 中注入）
        """
        # context 已经包含 recent_history 和 historical_cases
        # 无需重复检索

        # 创建 SharedContext
        shared_context = SharedContext(session_id=session_id)
        shared_context.data["question"] = question
        shared_context.data["agent_context"] = context or {}

        # 附加 SharedContext 到所有 Worker
        for worker in self.worker_pool:
            worker.attach_shared_context(shared_context)

        # 发布 Swarm 启动事件
        shared_context.publish_event(Event(
            type=EventType.SWARM_STARTED,
            source_agent="swarm_coordinator",
            data={
                "question": question,
                "num_subtasks": len(assessment.get("subtasks", []))
            }
        ))

        # Step 1: LeadAgent 分解任务
        subtasks = self.lead_agent.create_subtasks(assessment, shared_context)
        logger.info(f"Created {len(subtasks)} subtasks")

        # Step 2: Worker 执行分配的任务（并行）
        tasks = []
        for worker in self.worker_pool:
            task = asyncio.create_task(
                self._worker_execute_assigned_tasks(worker, shared_context)
            )
            tasks.append(task)

        # 等待所有 Worker 完成（或超时）。使用 asyncio.wait，避免
        # wait_for(gather(...)) 在超时时整体取消并丢失已完成 Agent 的结果。
        timeout_occurred = False
        worker_timeout = max(30.0, float(os.getenv("MEDIX_WORKER_TIMEOUT", "180")))
        done, pending = await asyncio.wait(tasks, timeout=worker_timeout)
        for task in done:
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(f"Worker task failed: {exc}")

        if pending:
            timeout_occurred = True
            logger.warning("Swarm execution timeout ({}s)", worker_timeout)
            # 记录哪些 Agent 已完成，哪些未完成
            completed_agents = list(shared_context.agent_contributions.keys())
            claimed_tasks = [
                (subtask.assigned_to, subtask.type)
                for subtask in shared_context.task_decomposition.values()
                if subtask.status.value == "claimed"
            ]
            logger.info(f"Completed agents: {completed_agents}")
            logger.info(f"Timed out tasks: {claimed_tasks}")
            # 保留 done 中的贡献后，再取消未完成任务，避免后台任务继续占用
            # LLM、Milvus 和网络连接。
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        # Worker 先完成分析，MedRGAG 再基于这些结果检索和核验；最后由
        # LeadAgent 使用核验后的证据生成最终回答。
        route_policy = (context or {}).get("route_policy", {})
        evidence_result = self._collect_worker_evidence(shared_context)

        # Step 3: LeadAgent 汇总结果
        # 即使超时，也尝试汇总已完成的部分结果
        final_answer = await self.lead_agent.synthesize_results(
            question=question,
            shared_context=shared_context,
            timeout_occurred=timeout_occurred,
            evidence=evidence_result,
        )
        # 证据门禁拒绝时，不能让 LeadAgent 用自由生成内容覆盖安全降级。
        if evidence_result:
            gate_status = (
                evidence_result.get("evidence_trace", {})
                .get("evidence_gate", {})
                .get("status")
            )
            if gate_status == "refused":
                final_answer = evidence_result.get("answer", final_answer)

        end_time = datetime.now()

        # Step 4: 生成 SessionSummary
        try:
            summary = SessionSummary.from_shared_context(
                session_id=session_id,
                question=question,
                shared_context=shared_context,
                final_answer=final_answer,
                start_time=start_time,
                end_time=end_time
            )
            self.session_manager.save_summary(summary)
        except Exception as e:
            logger.error(f"Failed to generate session summary: {e}")

        # 注意：短期记忆已经在 Agent Loop 中保存了，这里不需要重复保存
        # Agent Loop 保存了完整的对话历史（user + assistant + tool messages）

        # 保存到 Mem0 长期记忆
        try:
            # 保存会话总结
            self.long_term_memory.add_session_summary(
                session_id=session_id,
                question=question,
                answer=final_answer,
                user_id=user_id,
                metadata={
                    "mode": "swarm",
                    "agents_count": len(shared_context.agent_contributions),
                    "total_time": (end_time - start_time).total_seconds(),
                    "timeout_occurred": timeout_occurred
                }
            )

            logger.info(f"Saved to Mem0 long-term memory (session={session_id})")

        except Exception as e:
            logger.error(f"Failed to save to Mem0: {e}")

        # 发布 Swarm 完成事件
        shared_context.publish_event(Event(
            type=EventType.SWARM_COMPLETED,
            source_agent="swarm_coordinator",
            data={
                "duration": (end_time - start_time).total_seconds(),
                "agents_count": len(shared_context.agent_contributions)
            }
        ))

        # 返回结果
        completed_agents = list(shared_context.agent_contributions.keys())
        result = {
            'answer': final_answer,
            'swarm_enabled': True,
            'session_id': session_id,
            'agents_involved': completed_agents,
            'subtasks_completed': len(shared_context.get_all_completed_subtasks()),
            'total_time': (end_time - start_time).total_seconds(),
            'swarm_metadata': shared_context.get_summary(),
            'timeout_occurred': timeout_occurred
        }
        # Preserve structured evidence returned by ResearchAgent/DeepResearch.
        result['tool_results'] = []
        for contribution in shared_context.get_contributions():
            contribution_result = contribution.result or {}
            result['tool_results'].extend(contribution_result.get('tool_results', []))

        # 提取建议和免责声明（简化实现）
        result['suggestions'] = self._extract_suggestions(final_answer)
        if evidence_result:
            result['citations'] = evidence_result.get('citations', [])
            result['evidence_trace'] = evidence_result.get('evidence_trace', {})
            result['warnings'] = evidence_result.get('warnings', [])
            result['agents_involved'].extend(evidence_result.get('agents_involved', []))

        # 根据是否超时调整免责声明
        if timeout_occurred and not completed_agents:
            result['disclaimer'] = "由于系统超时，未能提供完整分析。建议简化问题重试，或在紧急情况下立即就医。"
        elif timeout_occurred:
            result['disclaimer'] = f"以上分析基于 {len(completed_agents)} 个 Agent 的部分协作结果（部分分析模块超时未完成），仅供参考，不能替代医生诊断。"
        else:
            result['disclaimer'] = "以上分析基于多个专业 Agent 的协作，仅供参考，不能替代医生诊断。"
        if evidence_result:
            gate_status = (
                evidence_result.get("evidence_trace", {})
                .get("evidence_gate", {})
                .get("status")
            )
            if gate_status == "refused":
                result['disclaimer'] = evidence_result.get(
                    'disclaimer', result['disclaimer']
                )

        return result

    async def _worker_execute_assigned_tasks(
        self,
        worker: Any,
        shared_context: SharedContext
    ):
        """
        Worker 执行分配给它的任务

        简化后的流程：
        - 查找分配给自己的任务
        - 执行任务
        - 记录结果
        """
        try:
            # 获取分配给该 Agent 的任务
            assigned_tasks = shared_context.get_subtasks_for_agent(worker.agent_id)

            if not assigned_tasks:
                logger.debug(f"{worker.agent_id}: No assigned tasks")
                return

            # 并行执行所有分配的任务
            tasks = []
            for subtask in assigned_tasks:
                logger.info(f"{worker.agent_id}: Starting {subtask.type}")
                shared_context.start_subtask(subtask.id)

                task = asyncio.create_task(
                    self._execute_single_subtask(worker, subtask, shared_context)
                )
                tasks.append(task)

            # 等待所有任务完成
            await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            logger.error(f"{worker.agent_id}: Error processing subtask: {e}")

    async def _execute_single_subtask(self, worker, subtask, shared_context):
        """执行单个子任务"""
        try:
            agent_context = shared_context.data.get("agent_context", {})
            worker_context = dict(agent_context)
            route = worker_context.get("route_policy", {})
            worker_evidence = None
            if route:
                # 关键顺序：先为当前子任务准备证据，再让 Worker 生成分析。
                worker_evidence = await self._run_evidence_stage(
                    question=subtask.description,
                    context=worker_context,
                    route=route,
                )
                if worker_evidence is not None:
                    worker_context["medrgag_evidence"] = worker_evidence
                    logger.info(
                        "{}: pre-generation MedRGAG completed for {} (citations={})",
                        worker.agent_id,
                        subtask.type,
                        len(worker_evidence.get("citations", [])),
                    )
            try:
                result = await worker.process_subtask(subtask, context=worker_context)
            except TypeError:
                # 兼容旧 Worker 适配器。
                result = await worker.process_subtask(subtask)

            # 证据包随贡献写入 SharedContext，供 LeadAgent 最终融合。
            if worker_evidence is not None:
                result = dict(result)
                result["medrgag_evidence"] = worker_evidence
                result["citations"] = worker_evidence.get("citations", [])
                result["evidence_trace"] = worker_evidence.get("evidence_trace", {})
                result["evidence_warnings"] = worker_evidence.get("warnings", [])
            shared_context.complete_subtask(subtask.id, worker.agent_id, result)
            logger.info(f"{worker.agent_id}: Completed {subtask.type}")
        except Exception as e:
            logger.error(f"{worker.agent_id}: Error in {subtask.type}: {e}")

    @staticmethod
    def _collect_worker_evidence(shared_context: SharedContext) -> Optional[Dict[str, Any]]:
        """Fuse per-Worker MedRGAG packages without running a duplicate RAG pass."""
        packages = []
        citations = []
        warnings = []
        seen_ids = set()
        for contribution in shared_context.get_contributions():
            package = (contribution.result or {}).get("medrgag_evidence")
            if not package:
                continue
            packages.append({
                "agent_id": contribution.agent_id,
                "subtask_id": contribution.subtask_id,
                "answer": package.get("answer", ""),
                "evidence_trace": package.get("evidence_trace", {}),
            })
            warnings.extend(package.get("warnings", []))
            for citation in package.get("citations", []):
                citation_id = citation.get("id") if isinstance(citation, dict) else str(citation)
                if citation_id not in seen_ids:
                    seen_ids.add(citation_id)
                    citations.append(citation)
        if not packages:
            return None
        refused = all(
            item.get("evidence_trace", {}).get("evidence_gate", {}).get("status") == "refused"
            for item in packages
        )
        return {
            "answer": "\n\n".join(item["answer"] for item in packages if item["answer"]),
            "citations": citations,
            "warnings": warnings,
            "evidence_trace": {
                "pipeline": "worker_medrgag_fusion",
                "worker_packages": packages,
                "evidence_gate": {
                    "status": "refused" if refused else "passed",
                    "worker_count": len(packages),
                },
            },
            "agents_involved": ["worker_medrgag", "evidence_gate"],
        }

    def _extract_suggestions(self, final_answer: str) -> List[str]:
        """从最终答案中提取建议（简化实现）"""
        suggestions = []

        # 简单的文本匹配
        if "【核心建议】" in final_answer:
            # 提取核心建议部分
            start_idx = final_answer.find("【核心建议】")
            end_idx = final_answer.find("【", start_idx + 1)
            if end_idx == -1:
                end_idx = len(final_answer)

            suggestions_text = final_answer[start_idx:end_idx]

            # 提取编号列表
            import re
            matches = re.findall(r'\d+\.\s*([^\n]+)', suggestions_text)
            suggestions = matches[:5]  # 最多5条

        return suggestions or ["请遵循医嘱，注意休息和营养"]

async def process_with_swarm(
    question: str,
    context: Optional[Dict[str, Any]] = None,
    enable_swarm: bool = True,
    session_id: Optional[str] = None,
    user_id: str = "anonymous",
) -> Dict[str, Any]:
    """
    便捷函数：使用 Swarm 处理问题

    Args:
        question: 用户问题
        context: 额外上下文
        enable_swarm: 是否启用 Swarm（False 则总是用单 Agent）
        session_id: 会话ID（如果提供，将使用该ID而不是生成新的）

    Returns:
        处理结果
    """
    coordinator = SwarmCoordinator(enable_swarm=enable_swarm)
    return await coordinator.process(question, context, session_id=session_id, user_id=user_id)
