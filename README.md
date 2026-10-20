# MediX 多智能体医疗助手

> 面向医学问答的证据优先、多智能体协作与可追溯知识库系统。

MediX 是 Python + FastAPI + Vue 3 的医疗 AI 应用，将急症分流、来源平衡检索、KGCC/KADS 证据流程、Skills-Agent、Agent Swarm、SQLite 会话记忆和 Milvus Lite 知识库组合成一条可审计的问答链路。

> **医疗安全声明**：本项目仅用于医学信息检索、健康教育和研究辅助，不能替代医生诊断、处方或急救服务。高风险或紧急情况请立即联系专业医务人员。

## 项目简介

普通 LLM 能生成流畅的医学回答，但可能缺少来源、混淆证据等级，或在急症场景下继续生成不适当建议。MediX 把医学问答拆成可验证步骤：先识别急症和问题类型，再从本地知识库及医学网络来源检索证据，最后检查引用、来源权限和证据支持关系。

项目适合医学知识检索、临床指南查询、症状风险初筛、研究资料整理和多轮问答原型，也提供带知识库管理、证据链和运行监控的全栈工作台。

## 核心能力

### 证据优先医学问答

- 确定性路由、分源检索、KGCC 原文处理、KADS 知识需求选择、证据综合和引用校验。
- 答案带来源、片段、标题路径、检索分数和核验状态；证据失败时拒绝输出推测性医学结论。

### Skills-Agent 与 Agent Swarm

- SkillRegistry 将 Python Skill 转为 OpenAI function calling 格式，Agent Loop 负责工具调用和迭代。
- LeadAgent 分解复杂问题，ConsultationAgent、DiagnosticAgent 和 ResearchAgent 并行处理子任务，SharedContext 共享结果与事件。
- 简单问题走单 Agent，复杂问题再启用 Swarm，兼顾成本与分析覆盖度。

### 多格式知识库

- 支持 PDF、DOCX、XLSX、XLSM、TXT 和 Markdown。
- 统一解析为中间表示，按标题树、表格行组和逻辑主题边界切分，并保留标题路径、版本、来源类型和 chunk ID。

### 统一会话与 Harness 约束

- HTTP API、CLI 和 Agent Loop 复用 SQLite 会话事实源；Mem0 作为可选跨会话语义记忆。
- YAML 定义 Agent 能力边界，运行时校验 Skill 调用和输出，并自动补充免责声明、高危提醒和记忆熵管理。

## 效果展示

![知识库桌面端](output/playwright/beautiful-knowledge-desktop.png)

![聊天桌面端](output/playwright/beautiful-chat-desktop.png)

## 应用场景

- 健康咨询原型、临床指南检索、复杂症状风险分析。
- 医疗研究辅助、知识库运营和可追溯证据问答。

## 安装部署

### 环境要求

- Python 3.10+（Docker 使用 Python 3.12）
- Node.js 22+ 与 npm（本地构建前端）
- Docker Desktop 与 Docker Compose（容器部署）
- OpenAI 兼容 LLM 接口；首次建库需要 BAAI/bge-small-zh-v1.5

### 方式一：让 AI 自己安装

~~~text
请帮我部署 MediX：
1. 克隆仓库：git clone git@github.com:aizhao/Medix.git
2. 复制 .env.example 为 .env，填写 MEDIX_LLM_API_KEY
3. 安装依赖或使用 Docker Compose
4. 初始化知识库并启动 API
5. 访问 /api/v1/health 和 /docs 验证安装
~~~

### 方式二：Docker Compose

~~~bash
git clone git@github.com:aizhao/Medix.git
cd Medix
cp .env.example .env
# 编辑 .env，至少填写 MEDIX_LLM_API_KEY
docker compose up --build
~~~

访问 http://localhost:8000/、http://localhost:8000/docs 和 http://localhost:8000/api/v1/health。

### 方式三：本地安装

~~~bash
git clone git@github.com:aizhao/Medix.git
cd Medix
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cd frontend && npm ci && npm run build && cd ..
python knowledge/scripts/import_documents.py knowledge/data/documents
uvicorn backend.app:app --host 0.0.0.0 --port 8000 --reload
~~~

也可以运行 ./scripts/start.sh 启动 Compose 环境。

## 快速开始

~~~bash
uvicorn backend.app:app --host 0.0.0.0 --port 8000 --reload
~~~

浏览器打开 http://localhost:8000/；命令行交互运行 python main.py。

~~~bash
python -m pytest -q
python examples/test_all.py
~~~

## 使用说明

### Web 工作台

1. 在聊天页输入问题，系统自动选择急症、证据优先、单 Agent 或 Swarm 路径。
2. 在证据栏查看来源、excerpt、标题路径、文档记录 ID、chunk ID 和核验状态。
3. 在知识库页上传文档，执行解析，再确认索引；失败任务可以重试。

### API 示例

~~~bash
curl http://localhost:8000/api/v1/health

curl -X POST http://localhost:8000/api/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"高血压患者日常生活需要注意什么？","user_id":"demo-user","enable_swarm":true}'
~~~

上传、解析和索引：

~~~bash
curl -X POST http://localhost:8000/api/v1/knowledge/upload \
  -F 'file=@knowledge/data/documents/20_guideline_hypertension.txt' \
  -F 'category=clinical_guideline'
curl -X POST http://localhost:8000/api/v1/knowledge/documents/{document_id}/parse
curl -X POST http://localhost:8000/api/v1/knowledge/documents/{document_id}/index
~~~

### 主要 API

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | /api/v1/health | 健康检查 |
| GET | /api/v1/ready | 依赖与配置就绪检查 |
| POST | /api/v1/chat | 结构化医学问答 |
| POST | /api/v1/chat/stream | SSE 流式问答 |
| GET | /api/v1/sessions/{session_id} | 读取会话 |
| GET/POST | /api/v1/memories | 用户作用域记忆 |
| POST | /api/v1/knowledge/upload | 上传多格式文档 |
| POST | /api/v1/knowledge/documents/{id}/parse | 解析并持久化 chunks |
| POST | /api/v1/knowledge/documents/{id}/index | 写入 Milvus Lite |
| GET | /api/v1/knowledge/jobs/{job_id} | 查询任务状态 |
| GET | /api/v1/knowledge/citations/{citation_id} | 定位本地引用 chunk |

## 系统架构

~~~mermaid
flowchart TD
    User[用户] --> Web[Vue 3 工作台]
    User --> CLI[main.py CLI]
    Web --> API[FastAPI]
    CLI --> Coordinator[SwarmCoordinator]
    API --> Coordinator
    Coordinator --> Route[确定性路由]
    Route --> Safety[急症安全响应]
    Route --> Evidence[证据优先流程]
    Route --> Lead[LeadAgent]
    Lead --> Single[单 Agent]
    Lead --> Swarm[Agent Swarm]
    Swarm --> C[ConsultationAgent]
    Swarm --> D[DiagnosticAgent]
    Swarm --> R[ResearchAgent]
    Single --> Skills[SkillRegistry / Agent Loop]
    C --> Skills
    D --> Skills
    R --> Skills
    Evidence --> RAG[MedRGAG / 证据综合]
    Skills --> KB[Milvus Lite]
    RAG --> KB
    RAG --> WebSearch[医学网络搜索]
    Coordinator --> SQLite[(SQLite 会话/文档/任务/记忆)]
    Coordinator -. 可选 .-> Mem0[Mem0 长期记忆]
~~~

## 核心工作流程

~~~mermaid
flowchart LR
    Q[用户问题] --> M[SQLite 会话历史]
    M --> L[Mem0 相似记忆]
    L --> R{确定性路由}
    R -->|急症| U[安全响应]
    R -->|医学事实| S[分源检索]
    S --> K[KGCC 原文处理]
    K --> D[KADS 文档选择]
    D --> G[证据综合]
    R -->|一般任务| A[LeadAgent 分解]
    A --> W[Worker 并行执行 Skills]
    W --> G
    G --> V[引用与证据校验]
    U --> O[结构化回答]
    V --> O
    O --> P[写回 SQLite]
~~~

1. API 写入用户消息，以 session_id 和 user_id 建立隔离边界。
2. Coordinator 读取最近历史并尝试加载 Mem0 相似案例。
3. classify_query 先识别急症和医学证据型问题。
4. 证据型问题进入来源平衡检索和 KGCC/KADS；一般问题由 LeadAgent 决定单 Agent 或 Swarm。
5. Agent Loop 执行受限 Skill，Harness 校验调用和输出。
6. API 检查引用是否存在，再返回答案、证据链和警告。

## AI 工作流程

### Agent 架构

~~~mermaid
flowchart TD
    Router[SwarmCoordinator] --> Lead[LeadAgent]
    Lead --> C[ConsultationAgent]
    Lead --> D[DiagnosticAgent]
    Lead --> R[ResearchAgent]
    C --> Loop[Agent Loop]
    D --> Loop
    R --> Loop
    Loop --> Registry[SkillRegistry]
    Registry --> Tools[医学 Skills]
    C --> Context[SharedContext]
    D --> Context
    R --> Context
~~~

Worker 共享知识库搜索、生活方式建议、风险评估、症状分析、ICD-10、临床指南、深度研究、会话历史和相似案例等 Skill。

### RAG 与证据流程

~~~mermaid
flowchart TD
    Doc[医学文档] --> Parse[多格式解析]
    Parse --> Tree[标题树/表格语义化]
    Tree --> Chunk[主题边界 + token 上限]
    Chunk --> Embed[BGE 中文 Embedding]
    Embed --> Store[(Milvus Lite)]
    Query[医学问题] --> Search[按来源类型等额检索]
    Store --> Search
    Web[医学网络来源] --> Search
    Search --> KGCC[KGCC 原文处理]
    KGCC --> KADS[KADS 需求-文档选择]
    KADS --> Synthesis[证据综合]
    Synthesis --> Gate[引用与证据门禁]
    Gate --> Answer[可追溯回答]
~~~

- 本地文档先按标题树和表格形成逻辑主题块，再按句子边界和 token 上限兜底。
- 每类语料独立获取候选，避免大型语料挤占指南、药品说明书或教材配额。
- 生成背景与可引用证据分离；引用不存在或证据不足时降级为警告或拒答。

## 技术栈

| 层级 | 技术 | 用途 |
|---|---|---|
| API | FastAPI + Uvicorn | REST、SSE、Swagger |
| 前端 | Vue 3 + Vite | 聊天和知识库工作台 |
| Agent | Python asyncio | Agent Loop 和并行 Worker |
| LLM | OpenAI-compatible API | 路由、规划、工具调用和综合 |
| Embedding | Sentence Transformers / BGE | 中文向量化 |
| 向量库 | Milvus Lite | 本地语义检索 |
| 关系存储 | SQLite | 会话、文档、任务和记忆 |
| 可选长期记忆 | Mem0 | 跨会话相似记忆 |
| 文档解析 | PyMuPDF、pdfplumber、python-docx、openpyxl | 多格式和表格抽取 |

## 项目结构

~~~text
medix-agent-swarm/
├── agents/                 # 三个专业 Agent
├── core/                   # LLMClient、AgentLoop、SkillRegistry
├── swarm/                  # LeadAgent、Coordinator、SharedContext、事件
├── research/               # 查询路由、Web 搜索、DeepResearch、证据综合
├── knowledge/              # 解析器、切分、Milvus、批量导入
├── memory/                 # SQLite 短期记忆、Mem0、会话总结、熵管理
├── constraints/            # YAML 约束与运行时验证
├── validation/             # 输出自动修复
├── backend/                # FastAPI 路由和 SQLiteStore
├── frontend/               # Vue 3 工作台
├── tests/                  # API、切分、RAG、可信问答测试
├── examples/test_all.py    # 综合示例测试
├── scripts/start.sh        # Compose 启动入口
├── Dockerfile              # 前端构建和 Python 运行时镜像
└── .env.example            # 配置模板
~~~

## 配置说明

复制 .env.example 为 .env，密钥不要提交。

| 配置项 | 必需 | 默认值 | 说明 |
|---|---|---|---|
| MEDIX_LLM_API_KEY | 是 | 空 | LLM 接口密钥 |
| MEDIX_LLM_MODEL | 否 | qwen3.8-max | 模型名称 |
| MEDIX_LLM_BASE_URL | 否 | 项目默认兼容接口 | API 根地址 |
| MEDIX_LLM_TEMPERATURE | 否 | 0.7 | 生成温度 |
| MEDIX_LLM_MAX_TOKENS | 否 | 8192 | 生成上限 |
| MEM0_API_KEY | 否 | 空 | 长期记忆，未设置时降级 |
| MEDIX_DB_PATH | 否 | data/medix.db | SQLite 路径 |
| MEDIX_MILVUS_DB_PATH | 否 | knowledge/data/milvus_lite.db | Milvus Lite 路径 |
| MEDIX_EMBEDDING_MODEL | 否 | BAAI/bge-small-zh-v1.5 | Embedding 模型 |
| MEDIX_EMBEDDING_BATCH_SIZE | 否 | 16 | 向量化批大小 |
| MEDIX_PARSE_CONCURRENCY | 否 | 2 | 解析并发上限 |
| MEDIX_CHUNK_MAX_TOKENS | 否 | 384 | chunk token 上限 |
| MEDIX_CHUNK_OVERLAP_TOKENS | 否 | 48 | 语义单元重叠上限 |
| MEDIX_KB_SOURCE_TYPES | 否 | 多种医学语料 | 分源检索类型 |
| MEDIX_RETRIEVAL_PER_SOURCE | 否 | 3 | 每类来源候选配额 |

## 性能与安全边界

- 首次启动需要加载 Embedding 模型；单例和模型缓存避免重复加载。
- 解析并发受 MEDIX_PARSE_CONCURRENCY 限制，Milvus Lite 写入使用进程内锁。
- 当前 SQLite 和后台任务适合单机 MVP；生产多实例应引入共享数据库、任务队列、鉴权和对象存储。
- 急症路由、免责声明、引用校验和证据不足拒答构成医学安全门禁。
- 会话通过 session_id、用户记忆通过 user_id 隔离；上传文件使用随机文件名并限制下载目录。
- 当前 API 尚未提供完整登录认证、细粒度权限、审计日志和多租户隔离；生产环境必须收紧 CORS。
- 系统不应被用于独立诊断、处方或替代急救服务。

## 项目亮点

1. **证据门禁而非单纯 RAG**：来源平衡、KGCC/KADS、引用校验和失败拒答形成闭环。
2. **逻辑主题切分**：标题路径、表格字段语义和 token 上限保护医学上下文完整性。
3. **路由与协作分离**：Coordinator 选路径，LeadAgent 分任务，Worker 通过 SharedContext 协作。
4. **记忆事实源统一**：HTTP、CLI 和 Agent Loop 复用 SQLite，Mem0 只承担可选语义记忆。
5. **Harness Engineering 落地**：约束、自动修复、熵管理和失败状态进入运行时。
6. **可审计知识库流水线**：SHA256 去重、版本、解析任务、chunks 和索引状态均可追踪。

## Roadmap

- [x] FastAPI + Vue 3 工作台
- [x] SQLite 统一会话、用户记忆、文档和任务登记
- [x] 多格式导入、逻辑主题切分和 Milvus Lite 检索
- [x] 证据优先问答、引用校验和急症分流
- [x] Agent Loop、Skills-Agent 和 Agent Swarm
- [ ] 完整认证、角色权限与多租户隔离
- [ ] 共享任务队列和多实例部署
- [ ] 记忆提取、冲突处理、过期清理和管理 UI 完整化
- [ ] Agent checkpoint、断点恢复和评测基准

## 贡献指南

~~~bash
git checkout -b feature/your-feature
python -m pytest -q
git commit
git push origin feature/your-feature
~~~

请不要提交 .env、模型缓存和本地数据库临时文件；新增功能应补充 API、切分、检索和失败路径测试。

## FAQ

### 没有 LLM API Key 能运行吗？

可以运行部分离线测试和查看前端，但真正的 Agent 问答需要 MEDIX_LLM_API_KEY。未配置 Mem0 时，系统仍使用 SQLite 会话记忆。

### 为什么首次启动很慢？

首次运行需要下载 BGE 中文 Embedding 模型并创建 Milvus Lite collection；Compose 会持久化模型缓存。

### 批量导入和 API 上传有什么区别？

批量脚本和 API 复用同一套 SQLite 文档登记、SHA256 去重、版本、解析、chunk 和 Milvus 索引机制；API 通过 parse/index 接口显式推进任务。

### 可以替换模型吗？

可以。修改 MEDIX_LLM_BASE_URL、MEDIX_LLM_MODEL 和 MEDIX_LLM_API_KEY 即可，但工具调用兼容性需自行验证。

## License

当前仓库未包含 LICENSE 文件，公开发布前请补充明确的开源许可证和第三方依赖声明。
