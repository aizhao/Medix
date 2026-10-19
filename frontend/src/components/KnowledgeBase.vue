<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'

const emit = defineEmits(['notify'])

const documents = ref([])
const stats = ref({})
const loading = ref(true)
const uploadBusy = ref(false)
const selectedIds = ref([])
const search = ref('')
const statusFilter = ref('all')
const categoryFilter = ref('all')
const uploadInput = ref(null)
const uploadSettings = reactive({ category: 'clinical_guideline', source_type: 'internal', authority_level: 'high' })
const previewDoc = ref(null)
const editDoc = ref(null)
const editForm = reactive({ category: '', source_type: '', authority_level: '', tags: '' })
const chunksDoc = ref(null)
const chunks = ref([])
const chunkQuery = ref('')
const chunkPage = ref(1)
const chunkPages = ref(1)
const chunkTotal = ref(0)
const jobsDoc = ref(null)
const jobs = ref([])
const expandedChunk = ref(null)
const confirmState = reactive({ open: false, title: '', message: '', danger: false, action: null, busy: false })
let pollTimer

const categories = [
  ['unclassified', '未分类'],
  ['clinical_guideline', '临床指南'],
  ['drug_label', '药品说明书'],
  ['textbook', '医学教材'],
  ['research', '研究论文'],
]

const filteredDocuments = computed(() => {
  const keyword = search.value.trim().toLowerCase()
  return documents.value.filter(doc => !keyword || [doc.filename, doc.category, ...(doc.tags || [])].join(' ').toLowerCase().includes(keyword))
})
const allSelected = computed(() => filteredDocuments.value.length > 0 && filteredDocuments.value.every(doc => selectedIds.value.includes(doc.id)))
const processingCount = computed(() => (stats.value.processing || 0) + (stats.value.indexing || 0))

async function api(path, options = {}) {
  const response = await fetch(path, options)
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = data.detail
    throw new Error(typeof detail === 'object' ? (detail.message || JSON.stringify(detail)) : (detail || '请求失败'))
  }
  return data
}

function notify(message, type = 'success') { emit('notify', { message, type }) }

async function loadKnowledge(silent = false) {
  if (!silent) loading.value = true
  try {
    const query = new URLSearchParams()
    if (statusFilter.value !== 'all') query.set('status', statusFilter.value)
    if (categoryFilter.value !== 'all') query.set('category', categoryFilter.value)
    const [items, summary] = await Promise.all([
      api(`/api/v1/knowledge/documents${query.toString() ? `?${query}` : ''}`),
      api('/api/v1/knowledge/stats'),
    ])
    documents.value = items
    stats.value = summary
    selectedIds.value = selectedIds.value.filter(id => items.some(doc => doc.id === id))
  } catch (error) {
    notify(error.message, 'error')
  } finally { loading.value = false }
}

async function uploadDocuments(event) {
  const files = [...(event.target.files || [])]
  if (!files.length) return
  uploadBusy.value = true
  let succeeded = 0
  for (const file of files) {
    const body = new FormData()
    body.append('file', file)
    body.append('category', uploadSettings.category)
    body.append('source_type', uploadSettings.source_type)
    body.append('authority_level', uploadSettings.authority_level)
    try {
      await api('/api/v1/knowledge/upload', { method: 'POST', body })
      succeeded += 1
    } catch (error) {
      notify(`${file.name}：${error.message}`, 'error')
    }
  }
  if (succeeded) notify(`已上传 ${succeeded} 个文件`)
  uploadBusy.value = false
  event.target.value = ''
  await loadKnowledge(true)
}

async function runAction(doc, action, successMessage) {
  try {
    await api(`/api/v1/knowledge/documents/${doc.id}/${action}`, { method: 'POST' })
    notify(successMessage)
    await loadKnowledge(true)
  } catch (error) { notify(error.message, 'error') }
}

function prepare(doc) { return runAction(doc, 'prepare', '已开始解析并切片') }
function retry(doc) { return runAction(doc, 'retry', '已重新提交失败任务') }
function reparse(doc) {
  askConfirm('重新解析文档？', '将重新生成切片并使当前向量失效，完成后需要重新生成向量。', () => runAction(doc, 'reparse', '已开始重新解析'))
}
function indexDoc(doc, reindex = false) {
  askConfirm(reindex ? '重新生成向量？' : '生成向量？', `将把 ${doc.total_chunks || 0} 个切片写入向量知识库。`, () => runAction(doc, reindex ? 'reindex' : 'index', reindex ? '已开始重新生成向量' : '已开始生成向量'))
}

function askConfirm(title, message, action, danger = false) {
  Object.assign(confirmState, { open: true, title, message, action, danger, busy: false })
}
async function confirmAction() {
  confirmState.busy = true
  try { await confirmState.action?.(); confirmState.open = false }
  finally { confirmState.busy = false }
}

async function deleteDocument(doc) {
  askConfirm('删除文档？', `“${doc.filename}”的原文件、切片、任务记录和向量将被永久删除。`, async () => {
    await api(`/api/v1/knowledge/documents/${doc.id}`, { method: 'DELETE' })
    notify('文档及关联数据已删除')
    selectedIds.value = selectedIds.value.filter(id => id !== doc.id)
    await loadKnowledge(true)
  }, true)
}

function openEditor(doc) {
  editDoc.value = doc
  editForm.category = doc.category || 'unclassified'
  editForm.source_type = doc.source_type || 'internal'
  editForm.authority_level = doc.authority_level || 'unknown'
  editForm.tags = (doc.tags || []).join('，')
}
async function saveMetadata() {
  try {
    await api(`/api/v1/knowledge/documents/${editDoc.value.id}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...editForm, tags: editForm.tags.split(/[，,]/).map(item => item.trim()).filter(Boolean) }),
    })
    editDoc.value = null
    notify('文档信息已更新')
    await loadKnowledge(true)
  } catch (error) { notify(error.message, 'error') }
}

async function openChunks(doc, page = 1) {
  chunksDoc.value = doc
  chunkPage.value = page
  try {
    const query = new URLSearchParams({ q: chunkQuery.value, page: String(page), page_size: '12' })
    const data = await api(`/api/v1/knowledge/documents/${doc.id}/chunks/search?${query}`)
    chunks.value = data.items
    chunkPages.value = data.pages
    chunkTotal.value = data.total
  } catch (error) { notify(error.message, 'error') }
}
async function openJobs(doc) {
  jobsDoc.value = doc
  try { jobs.value = await api(`/api/v1/knowledge/documents/${doc.id}/jobs`) }
  catch (error) { notify(error.message, 'error') }
}

function toggleAll() {
  const ids = filteredDocuments.value.map(doc => doc.id)
  selectedIds.value = allSelected.value ? selectedIds.value.filter(id => !ids.includes(id)) : [...new Set([...selectedIds.value, ...ids])]
}
async function batchAction(action) {
  const targets = documents.value.filter(doc => selectedIds.value.includes(doc.id))
  if (!targets.length) return
  const labels = { prepare: '解析并切片', index: '生成向量', delete: '删除' }
  askConfirm(`批量${labels[action]}？`, `将处理已选择的 ${targets.length} 个文档。`, async () => {
    let completed = 0
    for (const doc of targets) {
      try {
        if (action === 'delete') await api(`/api/v1/knowledge/documents/${doc.id}`, { method: 'DELETE' })
        else await api(`/api/v1/knowledge/documents/${doc.id}/${action}`, { method: 'POST' })
        completed += 1
      } catch (error) { notify(`${doc.filename}：${error.message}`, 'error') }
    }
    notify(`已提交 ${completed}/${targets.length} 个文档`)
    selectedIds.value = []
    await loadKnowledge(true)
  }, action === 'delete')
}

function statusLabel(status) {
  return ({ uploaded: '待解析', processing: '解析切片中', parsed: '待向量化', indexing: '向量化中', indexed: '可检索', failed: '处理失败', interrupted: '任务中断' })[status] || status
}
function stageLabel(stage) {
  return ({ uploaded: '等待处理', parsing: '读取和解析内容', chunking: '生成逻辑切片', parsed: '切片已保存', embedding: '生成语义向量', milvus_insert: '写入向量库', indexed: '处理完成', parse_failed: '解析失败', index_failed: '向量化失败' })[stage] || stage || '等待处理'
}
function categoryLabel(value) { return categories.find(item => item[0] === value)?.[1] || value }
function fileKind(doc) { const name = doc.filename?.toLowerCase() || ''; return name.endsWith('.pdf') ? 'PDF' : /\.(docx?)$/.test(name) ? 'DOC' : /\.(xlsx?|xlsm)$/.test(name) ? 'XLS' : 'TXT' }
function formatBytes(value) { if (!value) return '0 KB'; return value > 1024 * 1024 ? `${(value / 1024 / 1024).toFixed(1)} MB` : `${Math.ceil(value / 1024)} KB` }
function formatTime(value) { return value ? new Date(value).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—' }
function previewable(doc) { return fileKind(doc) === 'PDF' || fileKind(doc) === 'TXT' }

onMounted(() => {
  loadKnowledge()
  pollTimer = window.setInterval(() => { if (processingCount.value) loadKnowledge(true) }, 2000)
})
onUnmounted(() => window.clearInterval(pollTimer))
</script>

<template>
  <section class="kb-workspace">
    <header class="kb-hero">
      <div>
        <p>知识库工作区</p>
        <h2>把原始资料整理成可核验的医学证据</h2>
        <span>文件、切片、向量和任务记录保持同一条生命周期。</span>
      </div>
      <div class="kb-hero-actions">
        <button class="secondary-button" @click="loadKnowledge()">刷新状态</button>
        <button class="primary-button" :disabled="uploadBusy" @click="uploadInput?.click()">{{ uploadBusy ? '正在上传' : '上传资料' }}</button>
        <input ref="uploadInput" hidden type="file" multiple accept=".pdf,.docx,.xlsm,.xlsx,.txt,.md" @change="uploadDocuments">
      </div>
    </header>

    <div class="kb-summary">
      <div><strong>{{ stats.total || 0 }}</strong><span>全部文档</span></div>
      <div><strong>{{ stats.indexed || 0 }}</strong><span>可参与检索</span></div>
      <div><strong>{{ processingCount }}</strong><span>正在处理</span></div>
      <div><strong>{{ stats.failed || 0 }}</strong><span>需要处理</span></div>
      <div class="kb-path"><span>当前流程</span><b>上传 → 解析并切片 → 向量化 → 检索</b></div>
    </div>

    <div class="kb-controls">
      <label class="search-field"><span>搜索</span><input v-model="search" placeholder="文件名、分类或标签"></label>
      <label><span>分类</span><select v-model="categoryFilter" @change="loadKnowledge()"><option value="all">全部分类</option><option v-for="item in categories" :key="item[0]" :value="item[0]">{{ item[1] }}</option></select></label>
      <label><span>状态</span><select v-model="statusFilter" @change="loadKnowledge()"><option value="all">全部状态</option><option value="uploaded">待解析</option><option value="processing">处理中</option><option value="parsed">待向量化</option><option value="indexed">可检索</option><option value="failed">失败</option><option value="interrupted">中断</option></select></label>
      <details class="upload-settings"><summary>上传默认设置</summary><div><label>资料分类<select v-model="uploadSettings.category"><option v-for="item in categories" :key="item[0]" :value="item[0]">{{ item[1] }}</option></select></label><label>来源类型<select v-model="uploadSettings.source_type"><option value="internal">内部资料</option><option value="external">外部资料</option></select></label><label>权威等级<select v-model="uploadSettings.authority_level"><option value="high">高</option><option value="medium">中</option><option value="unknown">未评估</option></select></label></div></details>
    </div>

    <div v-if="selectedIds.length" class="batch-bar">
      <span>已选择 <b>{{ selectedIds.length }}</b> 项</span>
      <button @click="batchAction('prepare')">解析并切片</button>
      <button @click="batchAction('index')">生成向量</button>
      <button class="danger-text" @click="batchAction('delete')">删除</button>
      <button @click="selectedIds = []">取消选择</button>
    </div>

    <div class="kb-table-head"><label><input type="checkbox" :checked="allSelected" @change="toggleAll"><span>文档</span></label><span>处理状态</span><span>生命周期</span><span>操作</span></div>
    <div v-if="loading" class="kb-loading"><i v-for="item in 4" :key="item"></i></div>
    <div v-else-if="!filteredDocuments.length" class="kb-empty"><h3>这里还没有匹配的资料</h3><p>上传 PDF、Word、Excel、Markdown 或文本文件，建立第一条证据链。</p><button class="primary-button" @click="uploadInput?.click()">选择文件</button></div>
    <div v-else class="kb-document-list">
      <article v-for="doc in filteredDocuments" :key="doc.id" class="kb-document" :class="{ selected: selectedIds.includes(doc.id) }">
        <div class="document-identity"><input v-model="selectedIds" type="checkbox" :value="doc.id"><span class="file-badge">{{ fileKind(doc) }}</span><div><button class="document-name" @click="previewDoc = doc">{{ doc.filename }}</button><p>{{ categoryLabel(doc.category) }} · {{ formatBytes(doc.size_bytes) }} · v{{ doc.version || 1 }}</p><div class="document-tags"><span v-for="tag in doc.tags" :key="tag">{{ tag }}</span></div></div></div>
        <div class="document-status"><span class="status-dot" :class="doc.status"></span><div><b>{{ statusLabel(doc.status) }}</b><small>{{ stageLabel(doc.current_stage) }}</small><em v-if="doc.error_message">{{ doc.error_message }}</em></div></div>
        <div class="document-progress"><div><i :style="{ width: `${Math.min(100, doc.progress || 0)}%` }"></i></div><span>{{ doc.progress || 0 }}%</span><small :class="{ 'count-warning': doc.total_chunks && doc.indexed_chunks > doc.total_chunks }">{{ doc.total_chunks ? (doc.indexed_chunks > doc.total_chunks ? '历史向量数不一致，请重新生成' : `${doc.indexed_chunks || 0}/${doc.total_chunks} 个切片已入库`) : formatTime(doc.updated_at) }}</small></div>
        <div class="document-actions"><button @click="previewDoc = doc">原文</button><button v-if="doc.total_chunks" @click="chunkQuery = ''; openChunks(doc)">切片</button><button @click="openEditor(doc)">信息</button><button @click="openJobs(doc)">记录</button><button v-if="doc.status === 'uploaded'" class="action-main" @click="prepare(doc)">解析并切片</button><button v-else-if="doc.status === 'parsed'" class="action-main" @click="indexDoc(doc)">生成向量</button><button v-else-if="doc.status === 'failed' || doc.status === 'interrupted'" class="action-main" @click="retry(doc)">重试</button><details class="row-menu"><summary>更多</summary><div><button v-if="doc.total_chunks" @click="reparse(doc)">重新解析</button><button v-if="doc.status === 'indexed'" @click="indexDoc(doc, true)">重新生成向量</button><a :href="`/api/v1/knowledge/documents/${doc.id}/file`" download>下载原文件</a><button class="danger-text" @click="deleteDocument(doc)">删除文档</button></div></details></div>
      </article>
    </div>

    <div v-if="previewDoc" class="product-modal" @click.self="previewDoc = null"><section class="preview-dialog"><header><div><h3>{{ previewDoc.filename }}</h3><p>{{ categoryLabel(previewDoc.category) }} · 原文件预览</p></div><div><a :href="`/api/v1/knowledge/documents/${previewDoc.id}/file`" download>下载</a><button @click="previewDoc = null">关闭</button></div></header><iframe v-if="previewable(previewDoc)" :src="`/api/v1/knowledge/documents/${previewDoc.id}/preview`"></iframe><div v-else class="office-fallback"><b>{{ fileKind(previewDoc) }}</b><h4>浏览器无法直接预览此格式</h4><p>可以下载原文件，或查看解析后的结构化切片。</p><button v-if="previewDoc.total_chunks" class="primary-button" @click="chunksDoc = previewDoc; previewDoc = null; openChunks(chunksDoc)">查看切片</button></div></section></div>

    <div v-if="editDoc" class="product-modal" @click.self="editDoc = null"><section class="form-dialog"><header><div><h3>编辑文档信息</h3><p>{{ editDoc.filename }}</p></div><button @click="editDoc = null">关闭</button></header><div class="form-grid"><label>资料分类<select v-model="editForm.category"><option v-for="item in categories" :key="item[0]" :value="item[0]">{{ item[1] }}</option></select></label><label>来源类型<select v-model="editForm.source_type"><option value="internal">内部资料</option><option value="external">外部资料</option></select></label><label>权威等级<select v-model="editForm.authority_level"><option value="high">高</option><option value="medium">中</option><option value="low">低</option><option value="unknown">未评估</option></select></label><label class="full-field">标签<input v-model="editForm.tags" placeholder="使用逗号分隔，例如：高血压，2024指南"></label></div><footer><button @click="editDoc = null">取消</button><button class="primary-button" @click="saveMetadata">保存更改</button></footer></section></div>

    <div v-if="chunksDoc" class="product-modal" @click.self="chunksDoc = null"><section class="chunks-dialog"><header><div><h3>切片预览</h3><p>{{ chunksDoc.filename }} · 共 {{ chunkTotal }} 个切片</p></div><button @click="chunksDoc = null">关闭</button></header><form class="chunk-search" @submit.prevent="openChunks(chunksDoc, 1)"><input v-model="chunkQuery" placeholder="搜索切片正文或标题路径"><button>搜索</button></form><div class="chunk-results"><article v-for="chunk in chunks" :key="chunk.id" @click="expandedChunk = chunk"><div><b>#{{ String(chunk.chunk_index + 1).padStart(3, '0') }}</b><span>{{ chunk.metadata?.heading_path || chunk.metadata?.path || '正文' }}</span><small>{{ chunk.content.length }} 字</small></div><p>{{ chunk.content }}</p><button>展开全文</button></article><div v-if="!chunks.length" class="mini-empty">没有匹配的切片</div></div><footer class="pagination"><button :disabled="chunkPage <= 1" @click="openChunks(chunksDoc, chunkPage - 1)">上一页</button><span>{{ chunkPage }} / {{ chunkPages }}</span><button :disabled="chunkPage >= chunkPages" @click="openChunks(chunksDoc, chunkPage + 1)">下一页</button></footer></section></div>

    <div v-if="expandedChunk" class="product-modal nested" @click.self="expandedChunk = null"><section class="chunk-detail"><header><div><h3>切片 #{{ expandedChunk.chunk_index + 1 }}</h3><p>{{ expandedChunk.metadata?.heading_path || expandedChunk.metadata?.path || '正文' }}</p></div><button @click="expandedChunk = null">关闭</button></header><pre>{{ expandedChunk.content }}</pre></section></div>

    <div v-if="jobsDoc" class="product-modal" @click.self="jobsDoc = null"><section class="jobs-dialog"><header><div><h3>任务记录</h3><p>{{ jobsDoc.filename }}</p></div><button @click="jobsDoc = null">关闭</button></header><div v-if="jobs.length" class="job-list"><article v-for="job in jobs" :key="job.id"><span class="status-dot" :class="job.status"></span><div><b>{{ job.operation }} · {{ stageLabel(job.stage) }}</b><p>{{ job.error_message || `进度 ${job.progress}%` }}</p></div><time>{{ formatTime(job.created_at) }}</time></article></div><div v-else class="mini-empty">暂无任务记录</div></section></div>

    <div v-if="confirmState.open" class="product-modal nested" @click.self="confirmState.open = false"><section class="confirm-dialog"><h3>{{ confirmState.title }}</h3><p>{{ confirmState.message }}</p><footer><button @click="confirmState.open = false">取消</button><button :class="confirmState.danger ? 'danger-button' : 'primary-button'" :disabled="confirmState.busy" @click="confirmAction">{{ confirmState.busy ? '处理中' : '确认' }}</button></footer></section></div>
  </section>
</template>

<style src="../knowledge.css"></style>
