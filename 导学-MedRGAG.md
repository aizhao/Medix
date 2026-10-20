# MedRGAG 项目导学

> 目标：理解 MediX 中 MedRGAG 从“候选证据”到“可核验回答”的完整数据流，并能解释它与普通 RAG、Agent、Evidence Gate 的区别。本文只描述当前仓库已经存在的实现；没有固定问答集支撑的效果指标均标记为待测。

## 1. 前置知识（面试高频标注）

| 知识点 | 为何需要 | 在本项目中的位置 | 高频度 |
|---|---|---|---|
| RAG 基本链路 | 理解召回、排序、生成之间的边界 | `research/deep_research_workflow.py` | 高 |
| Dense / Sparse 检索与 RRF | 理解不同来源分数为何不能直接相加 | `research/medrgag.py::rrf_fuse` | 高 |
| Function Calling 与 Agent Loop | 理解 Worker 结果如何成为证据候选 | `core/agent_loop.py`、`swarm/swarm_coordinator.py` | 高 |
| 结构化输出与 JSON 校验 | KGCC、KADS、Gate 都依赖结构化 LLM 输出 | `research/medrgag.py`、`research/trusted_qa.py` | 高 |
| Claim-Evidence Entailment | 理解“相关文档”不等于“支持结论” | `research/trusted_qa.py::EvidenceGate` | 高 |
| 溯源与引用授权 | 区分真实检索材料和生成背景 | `EvidenceDocument`、`public_source()` | 高 |
| 异步并发与超时 | 检索、摘要、背景生成均存在并行任务 | `asyncio.gather`、Coordinator 超时处理 | 中高 |
| 医疗风险分级 | 高风险问题要求权威证据并允许拒答 | `research/query_router.py`、`EvidenceGate` | 高 |

## 2. 重点亮点与学习顺序（先看这个）

| 亮点标题 | 为什么重要 | 通用技术关键词 | 先看哪些文件 | 顺序 |
|---|---|---|---|---:|
| 证据对象与溯源建模 | 所有安全规则都建立在来源身份上 | provenance、typed evidence、citation policy | `research/medrgag.py` 前 180 行 | 1 |
| 多来源召回治理 | 防止同一大语料垄断候选 | query planning、RRF、source balance、rerank | `research/deep_research_workflow.py`、`research/medrgag.py` | 2 |
| 知识缺口驱动的上下文构造 | 从“相关段落”升级为“检查覆盖缺口” | KGCC、extractive fallback、generated context | `MedRGAGPipeline.run` 及三个 KGCC 方法 | 3 |
| 面向需求的证据选择 | 不只取相似度最高，而是覆盖、互补、去冗余 | KADS、coverage、diversity、fallback | `_select_documents`、`select_documents_kads` | 4 |
| Claim 级证据门禁 | 阻止无引用、伪引用和过度推断进入最终答案 | entailment、authority threshold、fail closed | `research/trusted_qa.py` | 5 |
| Agent 与证据流程统一 | 避免 Agent 是否检索成为可信度开关 | unified graph、structured tool result、safe degradation | `swarm/swarm_coordinator.py` | 6 |

## 3. 必备知识点

- [ ] 能画出“查询规划 → 多源检索 → RRF → 来源平衡 → Rerank → KGCC → KADS → 综合 → Evidence Gate”的链路。
- [ ] 能解释 `EvidenceDocument` 中 `source_kind`、`authority`、`status`、`metadata` 和 `citation_allowed` 的作用。
- [ ] 能说清 KGCC 的三个阶段：逐文档提取、知识缺口识别、背景生成。
- [ ] 能说清 KADS 的三个阶段：需求识别、需求映射、平衡选择。
- [ ] 能解释生成背景为什么可以参与推理，却不能进入对外引用。
- [ ] 能说明 Evidence Gate 为什么还要做逐条支持性核验，而不是只检查有没有引用。
- [ ] 能说明高风险问题的权威来源阈值和 fail-closed 策略。
- [ ] 能说明 LLM 失败时的确定性降级路径和当前评测缺口。

## 4. 推荐阅读（结合仓库）

| 主题 | 通用技术点 | 建议阅读位置 | 预计时间 | 读完能回答什么 |
|---|---|---|---:|---|
| 总体入口 | Agent 输出如何进入证据阶段 | `swarm/swarm_coordinator.py` | 25 分钟 | 为什么不是 Agent 与 RAG 二选一 |
| 检索编排 | 查询拆分、Agent 证据复用、网络/本地兜底 | `research/deep_research_workflow.py` | 35 分钟 | 如何避免重复检索和证据缺失 |
| 核心算法 | RRF、来源平衡、Rerank、KGCC、KADS | `research/medrgag.py` | 60 分钟 | MedRGAG 每一步的输入输出是什么 |
| 证据综合 | 结构化报告与生成背景隔离 | `research/evidence_synthesizer.py` | 25 分钟 | 为什么生成背景不会出现在 citations |
| 最终门禁 | 引用合法性、支持性核验、高风险权威要求 | `research/trusted_qa.py` | 40 分钟 | 什么情况下 passed/degraded/refused |
| 安全路由 | 急症、高风险和普通问题分类 | `research/query_router.py` | 15 分钟 | 哪些问题绕过或强制进入证据阶段 |
| 单元测试 | 不变量和回归边界 | `tests/test_medrgag.py`、`tests/test_trusted_qa.py` | 30 分钟 | 当前验证了什么、还没验证什么 |

## 5. 自学提醒

若某文件或原理看不懂，请继续追问 AI；本技能负责给学习路径与题目，不提供逐行讲解。优先从 `EvidenceDocument` 和 `MedRGAGPipeline.run()` 开始，因为它们定义了后续所有阶段的数据形态和主流程。

## 6. 项目技术定位

**AI 应用工程 + 检索系统 + 医疗安全工程的交叉模块。** 它不是单纯的向量检索，也不是独立 Agent，而是位于 Worker 分析之后、最终回答之前的“证据构造与放行层”。

## 7. 核心原理解析

### 7.1 普通 RAG 的缺口

问题：向量相似度只能说明文本与问题相关，不能保证候选覆盖了回答所需的全部知识，也不能保证生成结论被原文直接支持。

机制：MedRGAG 将链路拆成“多来源候选治理、知识缺口分析、需求驱动选择、结构化综合、Claim 级核验”。相关性只是候选入口，最终输出还要满足引用合法、原文支持和高风险权威来源要求。

项目落点：`DeepResearchWorkflow` 负责召回，`MedRGAGPipeline` 负责上下文构造与选择，`EvidenceSynthesizer` 负责结构化报告，`EvidenceGate` 决定放行、降级或拒答。

### 7.2 证据数据模型

问题：如果检索文档、网页摘要、Agent 自由生成内容和 KGCC 背景都用普通字符串表示，后续无法可靠判断哪些可以引用。

机制：每条候选都封装为 `EvidenceDocument`，显式保存 ID、正文、来源类型、URL/路径、权威度、检索分数、状态、标题路径和元数据。`is_external_citation` 只允许非生成、非冲突、非 snippet-only 的条目成为外部引用。

项目落点：生成背景固定为 `source_kind=generated`、`authority=0`，Agent 无来源摘要固定为 `status=snippet_only`，两者都只能参与推理，不能进入 citations。

### 7.3 查询规划与多来源检索

问题：复杂问题只用原问题检索，容易遗漏治疗、安全性、适用人群等不同侧面；直接把不同来源的原始分数混合也没有统一量纲。

机制：先将问题拆成最多三个互补查询，与原问题一起检索；本地知识库按来源类型分别取固定配额，网络检索和本地检索并行。多个有序列表通过 RRF 按排名融合，而不是直接相加不可比较的原始分数。

项目落点：若 Worker 已经产生带 URL 或本地文档定位的结构化来源，流程优先复用；只有证据类型不完整时才继续网络或本地检索，降低无意义重复工作。

### 7.4 来源平衡与重排

问题：单一来源条目较多时，可能在全局 Top-K 中挤掉指南、药品说明书或其他互补来源。

机制：先按 `source_kind` 分组，再轮询抽取每组候选，形成来源平衡池；随后以“检索相关性 0.50 + 词法重叠 0.30 + 权威度 0.20”重排。该公式是当前工程启发式，不应表述为已经经过最佳参数验证。

项目落点：候选池默认最多 24 条，进入 KGCC 前取 Top-K，默认 `top_k=5`。

### 7.5 KGCC：知识缺口驱动的上下文构造

KGCC 在本项目中按三步执行：

1. **逐文档提取**：针对当前问题，只提取每篇文档中的有用信息，无关文档返回 `No useful information`。若 LLM 错误否定了高分文档，则用保守的抽取式片段匹配降级。
2. **缺口识别**：基于有用摘要，识别最多三个尚未覆盖、互不重复的关键知识点；证据充分时可以为空。
3. **背景生成**：围绕缺口生成内部背景；若缺口不足 Top-K，则补充不同视角的问题条件背景。背景不声称引用文献，不允许成为外部来源。

KGCC 的价值不是“凭空补证据”，而是显式暴露当前检索覆盖不足，并为后续选择提供更完整的推理视角。

### 7.6 KADS：知识需求驱动的证据选择

KADS 在本项目中按三步执行：

1. **Knowledge Requirement Identification**：列出回答问题所需的关键知识。
2. **Requirement-to-Passage Mapping**：把候选文档映射到这些知识需求，并标记无关条目。
3. **Balanced Evidence Selection**：从真实检索文档与生成背景中选择最多 Top-K 条互补、低冗余、覆盖完整的上下文。

如果 LLM 选择器失败，确定性算法会按照基础重排分、问题新增覆盖、来源多样性、真实来源奖励和冗余惩罚选择非空候选，并把 `kads_used_fallback=true` 写入 trace。

### 7.7 证据综合与 Evidence Gate

问题：即使 KADS 选出了好材料，生成模型仍可能漏引用、引用不存在的 ID，或从相关材料推导出原文没有表达的结论。

机制：综合阶段要求每条关键发现尽量带真实 `[kb-...]` 或 `[web-...]` ID；Evidence Gate 先过滤无引用和无效引用，再用 LLM 逐条核验 claim 是否被所引原文直接支持。高风险问题还要求核验通过的结论至少命中一个 `authority >= 0.75` 的来源。

项目落点：没有外部证据、没有结论通过核验或高风险缺少权威来源时返回 `refused`；部分阶段失败或部分结论被拒绝时返回 `degraded`；只有所有关键约束满足才返回 `passed`。

### 7.8 统一 Agent 执行图

问题：让 Agent 自主决定是否检索，会把可信度建立在模型的一次工具选择上；让证据流程独立于 Agent，又会丢失 Worker 已经完成的专业分析与工具结果。

机制：非急症请求先由 LeadAgent 和 Worker 处理，医学事实问题随后强制进入 MedRGAG。Worker 的结构化工具结果转换为证据候选；没有来源的 Agent 摘要只能作为内部上下文。若 Evidence Gate 拒绝，Coordinator 强制保留拒答结果，LeadAgent 不能覆盖。

项目落点：单 Agent 和 Swarm 共用同一证据门禁；Worker 超时会保留已完成贡献，取消剩余任务，证据阶段超时则 fail closed。

## 8. 示例：肾功能不全患者的高血压用药

1. 查询规划得到“高血压治疗”“肾功能不全用药注意”“药物禁忌与剂量调整”等子查询。
2. 网络、本地指南、药品说明书候选分别检索，RRF 将多个排名融合。
3. 来源平衡避免普通网页占满候选，权威指南和药品说明书保留进入 Top-K。
4. KGCC 逐篇提取有效信息，并发现当前证据可能缺少具体肾功能分级或特殊人群边界。
5. 生成背景帮助 KADS 认识这些知识需求，但它不具备引用资格。
6. KADS 将“适用人群、禁忌、安全性、疗效”映射到候选，选择互补证据。
7. 综合器输出带真实来源 ID 的关键发现，并把仍未解决的缺口显式写入报告。
8. Evidence Gate 逐条核验原文支持关系；这是高风险问题，如果没有权威来源支持，就拒绝给出具体用药结论。

## 9. 关键设计决策

| 决策 | 备选方案 | 当前取舍 | 风险 | 验证方式 |
|---|---|---|---|---|
| 用 RRF 融合排名 | 直接混合原始相似度 | 避免不同检索器分数量纲不一致 | 只利用名次，可能损失分数幅度信息 | 对比 RRF、归一化加权和学习排序的 Recall@K/MRR |
| 生成背景只作推理上下文 | 将生成内容也当引用 | 阻止模型自证与伪引用 | 背景仍可能影响最终推理 | 检查 citations 中无 `gen-*`，增加污染对抗测试 |
| Gate 采用 fail closed | 核验失败时照常回答 | 医疗场景优先避免无依据输出 | 拒答率可能偏高 | 统计拒答准确率和可回答问题误拒率 |
| 权威度阈值 0.75 | 不区分高风险来源 | 高风险结论必须有高权威来源 | 阈值为人工规则，需校准 | 构建高风险测试集做阈值消融 |
| KADS LLM 失败后确定性降级 | 直接终止 | 保持流程可用且可解释 | 启发式选择质量有限 | 对比 LLM KADS 与 fallback 的覆盖率和冗余度 |
| 复用 Agent 工具证据 | MedRGAG 全量重新检索 | 减少重复调用，同时保留缺失来源兜底 | 证据去重和新鲜度更复杂 | 记录复用率、重复率、延迟和来源完整性 |

## 10. 量化与验证（待测建议）

当前仓库已有对 RRF、来源平衡、KGCC 溯源、KADS 选择、生成内容不可引用、无效引用拒绝和高风险权威来源要求的单元测试，但没有完整问答金标准，因此不要声称“准确率提升”。建议补充：

- **检索层**：Recall@K、MRR、来源覆盖度、候选冗余度。
- **上下文层**：知识需求覆盖率、KADS 选择精确率、生成背景污染率。
- **回答层**：引用正确率、Claim 支持率、忠实度、答案完整性。
- **安全层**：拒答准确率、误拒率、高风险无权威来源拦截率、伪引用拦截率。
- **工程层**：P50/P95 延迟、LLM 调用次数、Token 成本、Agent 证据复用率、各阶段降级比例。
- **消融实验**：普通 RAG、+来源平衡、+KGCC、+KADS、+Evidence Gate、完整统一图逐级对比。

