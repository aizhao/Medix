<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import KnowledgeBase from './components/KnowledgeBase.vue'

const activeView = ref(window.localStorage.getItem('medix-active-view') || 'chat')
const sessions = ref([])
const currentSession = ref(null)
const messages = ref([])
const input = ref('')
const busy = ref(false)
const health = ref('checking')
const citations = ref([])
const trace = ref({})
const suggestions = ref([])
const selectedCitation = ref(null)
const abortController = ref(null)
const toasts = ref([])
const sessionSearch = ref('')
const editingSession = ref(null)
const editingSessionTitle = ref('')
const appConfirm = ref(null)

const navItems = [
  { id: 'chat', label: '临床问答', icon: '◉', caption: 'Evidence workspace' },
  { id: 'knowledge', label: '知识库', icon: '▤', caption: 'Documents & indexing' },
  { id: 'operations', label: '运行监控', icon: '⌁', caption: 'System health' }
]

const healthLabel = computed(() => health.value === 'ok' ? '系统正常' : health.value === 'checking' ? '连接中' : '需检查')
const visibleSessions = computed(() => {
  const keyword = sessionSearch.value.trim().toLowerCase()
  return sessions.value.filter(item => !keyword || item.title.toLowerCase().includes(keyword)).slice(0, 12)
})

function showToast(payload) {
  const content = typeof payload === 'string' ? { message: payload } : payload
  const toast = { id: Date.now() + Math.random(), message: content.message, type: content.type || 'success' }
  toasts.value.push(toast)
  window.setTimeout(() => { toasts.value = toasts.value.filter(item => item.id !== toast.id) }, 3500)
}

function startRenameSession(session) { editingSession.value = session.id; editingSessionTitle.value = session.title }
async function saveSessionTitle(session) {
  const title = editingSessionTitle.value.trim()
  if (!title) return
  try {
    await api(`/api/v1/sessions/${session.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title }) })
    editingSession.value = null
    showToast({ message: '会话名称已更新' })
    await loadSessions()
  } catch (error) { showToast({ message: error.message, type: 'error' }) }
}
function requestDeleteSession(session) {
  appConfirm.value = { title: '删除会话？', message: `“${session.title}”及其全部消息将被删除。`, action: async () => {
    await api(`/api/v1/sessions/${session.id}`, { method: 'DELETE' })
    if (currentSession.value === session.id) newSession()
    await loadSessions()
    showToast({ message: '会话已删除' })
  } }
}
async function runAppConfirm() {
  const action = appConfirm.value?.action
  try { await action?.(); appConfirm.value = null }
  catch (error) { showToast({ message: error.message, type: 'error' }) }
}

async function api(path, options = {}) {
  const response = await fetch(path, options)
  const data = await response.json()
  if (!response.ok) throw new Error(data.detail || '请求失败')
  return data
}

async function checkHealth() {
  try { await api('/api/v1/health'); health.value = 'ok' } catch { health.value = 'error' }
}

async function loadSessions() {
  try { sessions.value = await api('/api/v1/sessions') } catch { sessions.value = [] }
}

function newSession() {
  currentSession.value = null
  messages.value = []
  citations.value = []
  trace.value = {}
  suggestions.value = []
  selectedCitation.value = null
  activeView.value = 'chat'
}

async function openSession(session) {
  const data = await api(`/api/v1/sessions/${session.id}`)
  currentSession.value = session.id
  messages.value = data.messages
  const lastAnswer = [...messages.value].reverse().find(item => item.role === 'assistant')
  const metadata = lastAnswer?.metadata || {}
  citations.value = metadata.citations || []
  trace.value = metadata.evidence_trace || {}
  suggestions.value = metadata.suggestions || []
  selectedCitation.value = null
  activeView.value = 'chat'
}

function addMessage(role, content, meta = '') { messages.value.push({ role, content, meta }) }

async function copyMessage(message) {
  try { await navigator.clipboard.writeText(message.content); showToast({ message: '回答已复制' }) }
  catch { showToast({ message: '复制失败，请手动选择文本', type: 'error' }) }
}

function escapeHtml(value) {
  return String(value || '').replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character])
}

function renderMarkdown(value) {
  let html = escapeHtml(value)
  html = html.replace(/```([\s\S]*?)```/g, '<pre><code>$1</code></pre>')
  html = html.replace(/^### (.+)$/gm, '<h4>$1</h4>').replace(/^## (.+)$/gm, '<h3>$1</h3>').replace(/^# (.+)$/gm, '<h2>$1</h2>')
  html = html.replace(/^[-*] (.+)$/gm, '<li>$1</li>')
  html = html.replace(/(<li>.*<\/li>)/gs, '<ul>$1</ul>')
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/`([^`]+)`/g, '<code>$1</code>')
  return html.replace(/\n/g, '<br>')
}

async function viewCitation(citation) {
  selectedCitation.value = citation
  if (citation.document_record_id != null) {
    try {
      selectedCitation.value = await api(`/api/v1/knowledge/citations/${encodeURIComponent(citation.id)}?document_id=${citation.document_record_id}${citation.chunk_id != null ? `&chunk_id=${citation.chunk_id}` : ''}`)
    } catch (error) {
      selectedCitation.value = { citation_id: citation.id, error: error.message, citation }
    }
  }
}

function parseSSE(buffer) {
  const chunks = buffer.split('\n\n')
  return { events: chunks.slice(0, -1), rest: chunks[chunks.length - 1] }
}

async function sendMessage() {
  const message = input.value.trim()
  if (!message || busy.value) return
  busy.value = true
  abortController.value = new AbortController()
  addMessage('user', message)
  addMessage('assistant', '正在启动证据检索…', '准备中')
  const pending = messages.value[messages.value.length - 1]
  input.value = ''
  try {
    const response = await fetch('/api/v1/chat/stream', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, session_id: currentSession.value }),
      signal: abortController.value.signal
    })
    if (!response.ok) throw new Error('流式接口请求失败')
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const parsed = parseSSE(buffer); buffer = parsed.rest
      for (const raw of parsed.events) {
        const event = raw.match(/^event: (.+)$/m)?.[1]
        const dataText = raw.match(/^data: (.+)$/m)?.[1]
        if (!dataText) continue
        const data = JSON.parse(dataText)
        if (event === 'accepted') currentSession.value = data.session_id
        if (event === 'stage') pending.meta = data.label
        if (event === 'answer_delta') pending.content = (pending.content === '正在启动证据检索…' ? '' : pending.content) + data.text
        if (event === 'completed') {
          pending.content = data.answer
          pending.meta = `${data.mode} · ${data.latency_ms} ms`
          citations.value = data.citations || []
          trace.value = data.evidence_trace || {}
          suggestions.value = data.suggestions || []
        }
        if (event === 'error') { pending.content = data.message; pending.meta = '请求失败' }
      }
    }
    await loadSessions()
  } catch (error) {
    if (error.name === 'AbortError') {
      pending.meta = '已停止生成'
    } else {
    pending.content = error.message
    pending.meta = '请求失败，请检查服务状态'
    }
  } finally { busy.value = false; abortController.value = null }
}

function stopMessage() { abortController.value?.abort() }

function formatTime(value) { return value ? new Date(value).toLocaleString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : '' }

onMounted(() => {
  checkHealth(); loadSessions()
})
watch(activeView, value => window.localStorage.setItem('medix-active-view', value))
</script>

<template>
  <div class="app-shell">
    <aside class="app-sidebar">
      <div class="brand"><div class="brand-mark">✚</div><div><strong>MediX</strong><span>Evidence OS</span></div></div>
      <button class="new-chat" @click="newSession"><span>＋</span> 新建咨询</button>
      <nav class="primary-nav">
        <button v-for="item in navItems" :key="item.id" :class="['nav-item', { active: activeView === item.id }]" @click="activeView = item.id">
          <span class="nav-icon">{{ item.icon }}</span><span><b>{{ item.label }}</b><small>{{ item.caption }}</small></span>
        </button>
      </nav>
      <div class="sidebar-section"><div class="section-label">最近会话</div><input v-model="sessionSearch" class="session-search" placeholder="搜索会话"><div class="session-list"><div v-for="session in visibleSessions" :key="session.id" class="session-row" :class="{ active: currentSession === session.id }"><form v-if="editingSession === session.id" @submit.prevent="saveSessionTitle(session)"><input v-model="editingSessionTitle" maxlength="80" autofocus><button>保存</button></form><template v-else><button class="session-item" @click="openSession(session)"><span>{{ session.title }}</span><small>{{ formatTime(session.updated_at) }}</small></button><div class="session-tools"><button title="重命名" @click="startRenameSession(session)">改</button><button title="删除" @click="requestDeleteSession(session)">删</button></div></template></div></div><span v-if="!visibleSessions.length" class="muted">暂无匹配会话</span></div>
      <div class="sidebar-bottom"><div class="privacy-dot"></div><div><b>本地安全模式</b><small>数据由当前服务管理</small></div></div>
    </aside>

    <main class="main-area">
      <header class="topbar"><div><div class="breadcrumb">MediX <span>/</span> {{ activeView === 'chat' ? '临床问答' : activeView === 'knowledge' ? '知识库管理' : '运行监控' }}</div><h1>{{ activeView === 'chat' ? '临床证据工作台' : activeView === 'knowledge' ? '知识库管理' : '系统运行监控' }}</h1></div><div class="top-actions"><span :class="['health-pill', health]"><i></i>{{ healthLabel }}</span><div class="avatar">M</div></div></header>

      <section v-if="activeView === 'chat'" class="chat-layout">
        <div class="conversation-card">
          <div v-if="!messages.length" class="empty-chat"><div class="hero-orb">✦</div><h2>把问题交给证据</h2><p>基于知识库、权威网络来源和多 Agent 协作，生成可追溯的医学回答。</p><div class="prompt-grid"><button @click="input = '高血压患者如何进行生活方式管理？'">高血压生活方式管理 <span>→</span></button><button @click="input = '如何判断胸痛是否需要立即就医？'">胸痛风险分级 <span>→</span></button><button @click="input = '二型糖尿病有哪些循证治疗方案？'">糖尿病循证治疗 <span>→</span></button></div></div>
          <div v-else class="message-list"><article v-for="(message, index) in messages" :key="index" :class="['message', message.role]"><div v-if="message.role === 'assistant'" class="assistant-avatar">✚</div><div class="message-body"><div class="message-label">{{ message.role === 'user' ? '你' : 'MediX' }}</div><div v-if="message.role === 'assistant'" class="message-content markdown-content" v-html="renderMarkdown(message.content)"></div><div v-else class="message-content">{{ message.content }}</div><div class="message-footer"><div v-if="message.meta" class="message-meta">{{ message.meta }}</div><button v-if="message.role === 'assistant' && message.content" class="message-copy" @click="copyMessage(message)">复制回答</button></div></div></article></div>
          <form class="composer" @submit.prevent="sendMessage"><textarea v-model="input" :disabled="busy" placeholder="描述你的症状、疾病或研究问题…" @keydown.enter.exact.prevent="sendMessage"></textarea><button v-if="busy" type="button" class="send-btn stop" @click="stopMessage">停止</button><button v-else class="send-btn" :disabled="!input.trim()">发送 <span>↗</span></button></form>
          <div class="composer-note">MediX 可能产生错误信息。涉及诊断、用药和急症时，请咨询合格医生。</div>
        </div>
        <aside class="evidence-rail"><div class="rail-title"><span>证据链</span><span class="live-tag">{{ trace.evidence_gate?.status === 'passed' ? 'VERIFIED' : trace.evidence_gate?.status === 'refused' ? 'REFUSED' : 'CHECKED' }}</span></div><div v-if="Object.keys(trace).length" class="trace-card"><div class="trace-row"><span>流程路由</span><b>{{ trace.route?.route || '—' }}</b></div><div class="trace-row"><span>分源候选</span><b>{{ trace.retrieved_count || '—' }}</b></div><div class="trace-row"><span>KGCC 缺口</span><b>{{ trace.missing_knowledge?.length || 0 }}</b></div><div class="trace-row"><span>生成背景</span><b>{{ trace.generated_count || 0 }}</b></div><div class="trace-row"><span>KADS 选中</span><b>{{ trace.selected_ids?.length || '—' }}</b></div><div class="trace-row"><span>证据门控</span><b>{{ trace.evidence_gate?.status || '—' }}</b></div><div v-if="trace.kads_requirements?.length" class="trace-warning">知识需求：{{ trace.kads_requirements.join('；') }}</div><div v-if="trace.evidence_gate?.reasons?.length" class="trace-warning">门控说明：{{ trace.evidence_gate.reasons.join('；') }}</div></div><div v-else class="rail-empty">提交问题后，这里会显示分源检索、KGCC、KADS 和证据门控轨迹。</div><div class="rail-section"><div class="rail-heading">关键建议</div><div v-if="suggestions.length" class="suggestion-list"><div v-for="item in suggestions" :key="item" class="suggestion">{{ item }}</div></div><div v-else class="rail-empty">暂无结构化建议</div></div><div class="rail-section"><div class="rail-heading">引用来源 <span>{{ citations.length }}</span></div><div v-if="citations.length" class="citation-list"><div v-for="citation in citations" :key="citation.id" class="citation"><div class="citation-type">{{ citation.type }} · 权威度 {{ citation.authority != null ? Math.round(citation.authority * 100) + '%' : '未评估' }}</div><button class="citation-link" @click="viewCitation(citation)">{{ citation.title }}</button><small>{{ citation.status }}<span v-if="citation.heading_path"> · {{ citation.heading_path }}</span><span v-if="citation.content_fetched"> · 已读取正文</span></small><small v-if="citation.excerpt">{{ citation.excerpt }}</small><a v-if="citation.url" :href="citation.url" target="_blank" rel="noreferrer">打开外部来源 ↗</a></div></div><div v-else class="rail-empty">当前回答未返回通过门控的来源。</div></div><div v-if="selectedCitation" class="citation-detail"><button class="close-detail" @click="selectedCitation = null">关闭</button><h3>{{ selectedCitation.document?.filename || selectedCitation.citation?.title || selectedCitation.citation_id }}</h3><p v-if="selectedCitation.chunk">{{ selectedCitation.chunk.content }}</p><p v-else-if="selectedCitation.citation?.excerpt">{{ selectedCitation.citation.excerpt }}</p><p v-else>{{ selectedCitation.message || selectedCitation.error }}</p></div></aside>
      </section>

      <KnowledgeBase v-else-if="activeView === 'knowledge'" @notify="showToast" />

      <section v-else class="page-content"><div class="ops-banner"><div><div class="eyebrow">SYSTEM OVERVIEW</div><h2>服务运行正常</h2><p>API、检索和会话存储均已连接。生产部署前请配置 Redis 和持久化数据库。</p></div><div class="pulse">●</div></div><div class="ops-grid"><div class="ops-card"><span>API 服务</span><strong class="green-text">ONLINE</strong><small>FastAPI · /api/v1</small></div><div class="ops-card"><span>会话存储</span><strong>SQLite</strong><small>可迁移至 Redis/PostgreSQL</small></div><div class="ops-card"><span>知识检索</span><strong>Milvus</strong><small>Dense + MedRGAG pipeline</small></div><div class="ops-card"><span>前端应用</span><strong>Vue 3</strong><small>Vite development workspace</small></div></div><div class="roadmap-card"><h2>下一阶段工程化</h2><div class="roadmap-row"><span>01</span><b>流式证据链</b><small>已完成 SSE 阶段事件与最终结构化响应</small><em>已完成</em></div><div class="roadmap-row"><span>02</span><b>知识库整理</b><small>上传队列已接入，下一步接入解析和 Milvus 索引</small><em>进行中</em></div><div class="roadmap-row"><span>03</span><b>生产持久化</b><small>当前使用 SQLite，部署时切换 Redis/PostgreSQL</small><em>待办</em></div></div></section>
      <div class="toast-stack"><button v-for="toast in toasts" :key="toast.id" :class="['app-toast', toast.type]" @click="toasts = toasts.filter(item => item.id !== toast.id)">{{ toast.message }}</button></div>
      <div v-if="appConfirm" class="app-confirm-backdrop" @click.self="appConfirm = null"><section class="app-confirm"><h3>{{ appConfirm.title }}</h3><p>{{ appConfirm.message }}</p><div><button @click="appConfirm = null">取消</button><button class="confirm-danger" @click="runAppConfirm">确认删除</button></div></section></div>
    </main>
  </div>
</template>
