<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import KnowledgeBase from './components/KnowledgeBase.vue'
import UiIcon from './components/ui/UiIcon.vue'
import PromptBar from './components/ui/PromptBar.vue'
import ThinkingState from './components/ui/ThinkingState.vue'

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
const sidebarOpen = ref(false)
const theme = ref(localStorage.getItem('medix-theme') || 'light')
const readiness = ref(null)
const knowledgeStats = ref(null)
const checkedAt = ref('')
const messageList = ref(null)
const followStream = ref(true)
const gateLabel = computed(() => ({ passed: '已通过校验', refused: '证据不足' })[trace.value.evidence_gate?.status] || (busy.value ? '核验中' : '等待证据'))

const navItems = [
  { id: 'chat', label: '临床问答', icon: 'chat' },
  { id: 'knowledge', label: '知识库', icon: 'library' },
  { id: 'operations', label: '运行监控', icon: 'activity' }
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
  const results = await Promise.allSettled([api('/api/v1/ready'), api('/api/v1/knowledge/stats')])
  readiness.value = results[0].status === 'fulfilled' ? results[0].value : null
  knowledgeStats.value = results[1].status === 'fulfilled' ? results[1].value : null
  checkedAt.value = new Date().toLocaleTimeString('zh-CN')
}

async function loadSessions() {
  try { sessions.value = await api('/api/v1/sessions') } catch { sessions.value = [] }
}

function newSession() {
  if (busy.value) return
  currentSession.value = null
  messages.value = []
  citations.value = []
  trace.value = {}
  suggestions.value = []
  selectedCitation.value = null
  activeView.value = 'chat'
  sidebarOpen.value = false
}

async function openSession(session) {
  if (busy.value) return
  try {
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
  sidebarOpen.value = false
  } catch (error) { showToast({ message: error.message, type: 'error' }) }
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
  selectedCitation.value = { citation, citation_id: citation.id }
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
  followStream.value = true
  citations.value = []; trace.value = {}; suggestions.value = []
  abortController.value = new AbortController()
  addMessage('user', message)
  addMessage('assistant', '正在启动证据检索…', '准备中')
  const pending = messages.value[messages.value.length - 1]
  pending.steps = ['已提交问题']
  let completed = false
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
        if (event === 'stage') { pending.meta = data.label; if (data.label && pending.steps.at(-1) !== data.label) pending.steps.push(data.label) }
        if (event === 'answer_delta') pending.content = (pending.content === '正在启动证据检索…' ? '' : pending.content) + data.text
        if (event === 'completed') {
          completed = true
          pending.content = data.answer
          pending.meta = `${data.mode} · ${data.latency_ms} ms`
          citations.value = data.citations || []
          trace.value = data.evidence_trace || {}
          suggestions.value = data.suggestions || []
          pending.metadata = { citations: citations.value, evidence_trace: trace.value, suggestions: suggestions.value }
        }
        if (event === 'error') { completed = true; pending.content = data.message; pending.meta = '请求失败' }
      }
    }
    if (!completed) pending.meta = '连接中断，回答可能不完整'
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
function showAnswerEvidence(message) {
  const metadata = message.metadata || {}
  citations.value = metadata.citations || []; trace.value = metadata.evidence_trace || {}; suggestions.value = metadata.suggestions || []
}
function trackScroll() { const el = messageList.value; if (el) followStream.value = el.scrollHeight - el.scrollTop - el.clientHeight < 100 }
watch(() => messages.value.at(-1)?.content, async () => { await nextTick(); if (followStream.value && messageList.value) messageList.value.scrollTop = messageList.value.scrollHeight })
watch(theme, value => { document.documentElement.dataset.theme = value; localStorage.setItem('medix-theme', value) }, { immediate: true })

onMounted(() => {
  checkHealth(); loadSessions()
})
watch(activeView, value => { window.localStorage.setItem('medix-active-view', value); sidebarOpen.value = false; if (value === 'operations') checkHealth() })
</script>

<template>
  <div class="app-shell">
    <div v-if="sidebarOpen" class="sidebar-scrim" @click="sidebarOpen = false"></div>
    <aside class="app-sidebar" :class="{ 'is-open': sidebarOpen }" aria-label="主导航">
      <div class="brand"><div class="brand-mark"><UiIcon name="activity" :size="21" /></div><div><strong>MediX<span class="brand-beta">WORKSPACE</span></strong><span>医学证据工作台</span></div></div>
      <button class="new-chat" :disabled="busy" @click="newSession"><UiIcon name="plus" :size="17" />新建咨询<kbd>＋</kbd></button>
      <div class="nav-label">工作空间</div>
      <nav class="primary-nav"><button v-for="item in navItems" :key="item.id" :class="['nav-item', { active: activeView === item.id }]" :aria-current="activeView === item.id ? 'page' : undefined" @click="activeView = item.id"><UiIcon :name="item.icon" /><b>{{ item.label }}</b><UiIcon v-if="activeView === item.id" name="chevron" :size="13" /></button></nav>
      <div class="sidebar-section">
        <div class="section-label">最近会话<span>{{ sessions.length }}</span></div>
        <label class="sidebar-search"><UiIcon name="search" :size="15" /><input v-model="sessionSearch" class="session-search" aria-label="搜索会话" placeholder="搜索会话…"></label>
        <div class="session-list"><div v-for="session in visibleSessions" :key="session.id" class="session-row" :class="{ active: currentSession === session.id }">
          <form v-if="editingSession === session.id" @submit.prevent="saveSessionTitle(session)"><input v-model="editingSessionTitle" aria-label="会话名称" maxlength="80"><button>保存</button><button type="button" aria-label="取消重命名" @click="editingSession = null">×</button></form>
          <template v-else><button class="session-item" :disabled="busy" :title="session.title" @click="openSession(session)"><UiIcon name="chat" :size="14" /><span>{{ session.title }}</span></button><div class="session-tools"><button :disabled="busy" aria-label="重命名会话" @click="startRenameSession(session)"><UiIcon name="edit" :size="13" /></button><button :disabled="busy" aria-label="删除会话" @click="requestDeleteSession(session)"><UiIcon name="trash" :size="13" /></button></div></template>
        </div></div><span v-if="!visibleSessions.length" class="muted">{{ sessionSearch ? '没有找到匹配会话' : '从一个问题开始，会话将在这里保存' }}</span>
      </div>
      <div class="sidebar-bottom"><div class="workspace-avatar">M</div><div><b>MediX 工作空间</b><small>可追溯的医学知识</small></div><button class="icon-button theme-toggle" :aria-label="theme === 'dark' ? '切换浅色模式' : '切换深色模式'" @click="theme = theme === 'dark' ? 'light' : 'dark'"><UiIcon :name="theme === 'dark' ? 'sun' : 'moon'" :size="17" /></button></div>
    </aside>
    <main class="main-area">
      <header class="topbar"><div class="topbar-location"><button class="icon-button mobile-menu" aria-label="打开导航" :aria-expanded="sidebarOpen" @click="sidebarOpen = !sidebarOpen"><UiIcon name="panel" /></button><div class="breadcrumb">工作空间 <UiIcon name="chevron" :size="12" /><span>{{ navItems.find(item => item.id === activeView)?.label }}</span></div></div><div class="top-actions"><button :class="['health-pill', health]" @click="checkHealth"><i></i>{{ healthLabel }}</button><span class="topbar-divider"></span><span class="workspace-tag">MediX <span>AI</span></span></div></header>
      <section v-if="activeView === 'chat'" class="chat-layout">
        <div class="conversation-card">
          <div class="conversation-heading"><span><UiIcon name="chat" :size="16" />{{ currentSession ? '当前咨询' : '新的咨询' }}</span><span class="subtle-badge">循证模式</span></div>
          <div v-if="!messages.length" class="empty-chat"><div class="hero-orb"><UiIcon name="activity" :size="32" /></div><div class="welcome-label">YOUR EVIDENCE COMPANION</div><h1>每一个问题，<br>都有据可循。</h1><p>从医学资料中寻找答案，<br class="mobile-only">让每一条建议都有清晰的来处。</p><div class="prompt-grid">
            <button @click="input = '高血压患者如何进行生活方式管理？'"><UiIcon name="activity" /><b>慢病管理</b><small>高血压的生活方式建议</small><UiIcon name="right" :size="15" /></button>
            <button @click="input = '如何判断胸痛是否需要立即就医？'"><UiIcon name="shield" /><b>症状评估</b><small>了解胸痛的危险信号</small><UiIcon name="right" :size="15" /></button>
            <button @click="input = '二型糖尿病有哪些循证治疗方案？'"><UiIcon name="library" /><b>循证研究</b><small>查阅糖尿病治疗证据</small><UiIcon name="right" :size="15" /></button>
          </div></div>
          <div v-else ref="messageList" class="message-list" @scroll="trackScroll"><article v-for="(message, index) in messages" :key="index" :class="['message', message.role]"><div v-if="message.role === 'assistant'" class="assistant-avatar"><UiIcon name="activity" :size="18" /></div><div class="message-body"><div class="message-label">{{ message.role === 'user' ? '你' : 'MediX' }}<span v-if="message.role === 'assistant'">医学证据助手</span></div><ThinkingState v-if="message.steps?.length" :steps="message.steps" :busy="busy && index === messages.length - 1" :label="message.meta" /><div v-if="message.role === 'assistant'" class="message-content markdown-content" :class="{ streaming: busy && index === messages.length - 1 }" v-html="renderMarkdown(message.content)"></div><div v-else class="message-content">{{ message.content }}</div><div class="message-footer"><div v-if="message.meta && !message.steps?.length" class="message-meta">{{ message.meta }}</div><button v-if="message.role === 'assistant' && message.content" class="message-copy" @click="copyMessage(message)"><UiIcon name="copy" :size="13" />复制</button><button v-if="message.metadata?.citations?.length" class="message-copy" @click="showAnswerEvidence(message)"><UiIcon name="layers" :size="13" />查看 {{ message.metadata.citations.length }} 条来源</button></div></div></article></div>
          <div class="composer-wrap"><PromptBar v-model="input" :busy="busy" @send="sendMessage" @stop="stopMessage" @knowledge="activeView = 'knowledge'" /><div class="composer-note">回答仅供参考，不能替代专业医生的诊断和治疗。</div></div>
        </div>
        <aside class="evidence-rail">
          <div class="rail-title"><span><UiIcon name="layers" :size="17" />证据与来源</span><span class="subtle-badge">{{ citations.length }}</span></div>
          <div class="gate-state" :class="trace.evidence_gate?.status"><span class="chip-dot"></span>{{ gateLabel }}</div>
          <div v-if="Object.keys(trace).length" class="trace-card"><div class="trace-row"><span>检索候选</span><b>{{ trace.retrieved_count ?? '—' }}</b></div><div class="trace-row"><span>知识缺口</span><b>{{ trace.missing_knowledge?.length || 0 }}</b></div><div class="trace-row"><span>筛选来源</span><b>{{ trace.selected_ids?.length ?? '—' }}</b></div><details class="trace-details"><summary>查看处理详情</summary><div class="trace-row"><span>流程路由</span><b>{{ trace.route?.route || '—' }}</b></div><div class="trace-row"><span>生成背景</span><b>{{ trace.generated_count || 0 }}</b></div><p v-if="trace.kads_requirements?.length">{{ trace.kads_requirements.join('；') }}</p><p v-if="trace.evidence_gate?.reasons?.length">{{ trace.evidence_gate.reasons.join('；') }}</p></details></div>
          <div v-else class="evidence-placeholder"><div class="evidence-illustration"><UiIcon name="file" :size="30" /><UiIcon name="shield" :size="20" /></div><b>答案背后的依据</b><p>提问后，检索到的来源与核验结果会显示在这里。</p><div class="evidence-steps"><span>检索资料</span><UiIcon name="chevron" :size="12" /><span>核验依据</span><UiIcon name="chevron" :size="12" /><span>回答</span></div></div>
          <div class="rail-section"><div class="rail-heading">关键建议</div><div v-if="suggestions.length" class="suggestion-list"><div v-for="(item, index) in suggestions" :key="index" class="suggestion"><span>{{ String(index + 1).padStart(2, '0') }}</span>{{ item }}</div></div><div v-else class="rail-empty">回答生成后显示结构化建议。</div></div>
          <div class="rail-section"><div class="rail-heading">引用来源 <span>{{ citations.length }}</span></div><div v-if="citations.length" class="citation-list"><div v-for="(citation, index) in citations" :key="citation.id" class="citation"><div class="citation-type"><span class="citation-number">{{ index + 1 }}</span>{{ citation.type }}<span v-if="citation.content_fetched">· 已读取</span></div><button class="citation-link" @click="viewCitation(citation)">{{ citation.title }}</button><small v-if="citation.excerpt">{{ citation.excerpt }}</small><button class="source-open" @click="viewCitation(citation)">查看来源<UiIcon name="right" :size="13" /></button></div></div><div v-else class="rail-empty">尚无引用来源</div></div>
        </aside>
      </section>
      <KnowledgeBase v-else-if="activeView === 'knowledge'" @notify="showToast" />
      <section v-else class="page-content ops-page">
        <div class="page-heading"><div><div class="welcome-label">WORKSPACE HEALTH</div><h1>运行监控</h1><p>查看当前服务连接、知识库状态与持久化配置。</p></div><button class="secondary-button" @click="checkHealth"><UiIcon name="refresh" :size="16" />重新检查</button></div>
        <div class="ops-banner"><div><span class="health-pill" :class="health"><i></i>API 连接</span><h2>{{ health === 'ok' ? '服务可连接' : health === 'checking' ? '正在检查连接' : '无法连接服务' }}</h2><p>{{ readiness?.status === 'ok' ? '数据库、上传目录和模型密钥配置检查通过。' : '依赖检查尚未全部通过，请查看下方状态。' }}</p></div><UiIcon name="activity" :size="44" /></div>
        <div class="ops-grid"><div class="ops-card"><span>API 服务</span><strong>{{ health === 'ok' ? '在线' : '待检查' }}</strong><small>最后检查 {{ checkedAt || '—' }}</small></div><div class="ops-card"><span>知识库文档</span><strong>{{ knowledgeStats?.total ?? '—' }}</strong><small>{{ knowledgeStats?.indexed ?? '—' }} 份可检索</small></div><div class="ops-card"><span>待处理异常</span><strong>{{ knowledgeStats ? (knowledgeStats.failed || 0) + (knowledgeStats.interrupted || 0) : '—' }}</strong><small>失败和中断的任务</small></div><div class="ops-card"><span>持久化存储</span><strong>{{ readiness?.checks?.database === 'ok' ? '可访问' : '待检查' }}</strong><small>文档、切片与会话记录</small></div></div>
        <div class="roadmap-card"><h2>依赖检查</h2><div v-for="(value, key) in readiness?.checks || {}" :key="key" class="dependency-row"><UiIcon :name="value === 'ok' || value === 'configured' ? 'check' : 'activity'" :size="17" /><b>{{ ({ database: '数据库', uploads: '上传目录', llm_key: '模型密钥配置' })[key] || key }}</b><span>{{ ({ ok: '检查通过', configured: '已配置', missing: '未配置' })[value] || value }}</span></div><p v-if="!readiness" class="rail-empty">暂时无法获取依赖检查结果。</p><p class="ops-footnote">密钥已配置不代表模型调用成功；此页面不执行付费模型请求。</p></div>
      </section>
      <div class="toast-stack" aria-live="polite"><button v-for="toast in toasts" :key="toast.id" :class="['app-toast', toast.type]" @click="toasts = toasts.filter(item => item.id !== toast.id)"><UiIcon :name="toast.type === 'error' ? 'activity' : 'check'" :size="16" />{{ toast.message }}</button></div>
      <div v-if="appConfirm" v-dialog="() => appConfirm = null" class="app-confirm-backdrop" @click.self="appConfirm = null"><section class="app-confirm" role="dialog" aria-modal="true" :aria-label="appConfirm.title"><h3>{{ appConfirm.title }}</h3><p>{{ appConfirm.message }}</p><div><button @click="appConfirm = null">取消</button><button class="confirm-danger" @click="runAppConfirm">确认删除</button></div></section></div>
      <div v-if="selectedCitation" v-dialog="() => selectedCitation = null" class="product-modal" @click.self="selectedCitation = null"><section class="chunk-detail" role="dialog" aria-modal="true" aria-label="引用来源"><header><div><h3>引用来源</h3><p>{{ selectedCitation.document?.filename || selectedCitation.citation?.title || selectedCitation.citation_id }}</p></div><button aria-label="关闭引用来源" @click="selectedCitation = null"><UiIcon name="close" :size="16" /></button></header><div class="source-detail-body"><p>{{ selectedCitation.chunk?.content || selectedCitation.citation?.excerpt || selectedCitation.message || selectedCitation.error || '此来源未提供正文片段。' }}</p><a v-if="selectedCitation.document?.id" :href="'/api/v1/knowledge/documents/' + selectedCitation.document.id + '/preview'" target="_blank" rel="noopener noreferrer">打开原文件 ↗</a><a v-else-if="selectedCitation.citation?.url" :href="selectedCitation.citation.url" target="_blank" rel="noopener noreferrer">打开外部来源 ↗</a></div></section></div>
    </main>
  </div>
</template>
