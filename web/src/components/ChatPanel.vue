<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import MarkdownBody from './MarkdownBody.vue'
import { useSessionStore } from '../stores/session'

const store = useSessionStore()
const input = ref('')
const box = ref<HTMLTextAreaElement | null>(null)
const msgs = ref<HTMLElement | null>(null)

const sessionLabel = computed(() =>
  store.sessionId ? store.sessionId.slice(-6).toUpperCase() : '—',
)

function send(text?: string) {
  const value = (text ?? input.value).trim()
  if (!value || store.busy) return
  input.value = ''
  resize()
  store.ask(value)
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    send()
  }
}

function onDatasetChange(e: Event) {
  const value = (e.target as HTMLSelectElement).value
  store.switchDataset(value)
}

function resize() {
  const el = box.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, 120)}px`
}

watch(
  () => [store.messages.length, store.busy],
  async () => {
    await nextTick()
    if (msgs.value) msgs.value.scrollTop = msgs.value.scrollHeight
  },
)

const now = ref(Date.now())
let ticker: number | undefined
watch(
  () => store.busy,
  (busy) => {
    if (busy) {
      now.value = Date.now()
      ticker = window.setInterval(() => {
        now.value = Date.now()
      }, 500)
    } else if (ticker) {
      window.clearInterval(ticker)
      ticker = undefined
    }
  },
)
onUnmounted(() => {
  if (ticker) window.clearInterval(ticker)
})
const elapsed = computed(() =>
  store.busy && store.busySince
    ? Math.max(0, Math.floor((now.value - store.busySince) / 1000))
    : 0,
)
const slow = computed(() => elapsed.value >= 30)
</script>

<template>
  <aside class="chat">
    <header class="chat-head">
      <div class="brand">
        <div class="mark">账</div>
        <div>
          <div class="bname">明账</div>
          <div class="bsub">MINGZHANG · CONVERSATIONAL BI</div>
        </div>
      </div>
      <div class="session">
        <span class="dataset">
          <span class="dot-live" :class="{ 'dot-off': store.meta && !store.meta.llm.enabled }"></span>
          <select
            class="dataset-select"
            :value="store.datasetId"
            :disabled="store.busy || store.datasets.length <= 1"
            title="切换数据源"
            @change="onDatasetChange"
          >
            <option v-if="!store.datasets.length" value="">无数据源</option>
            <option v-for="d in store.datasets" :key="d.dataset_id" :value="d.dataset_id">
              {{ d.name }}{{ d.status === 'published' ? '' : ' · 草稿' }}
            </option>
          </select>
          <span
            v-if="store.meta"
            class="mode-badge"
            :class="store.meta.mode === 'draft' ? 'draft' : 'live'"
          >
            {{ store.meta.mode === 'draft' ? '草稿口径' : '已发布' }}
          </span>
        </span>
        <span class="session-right">
          <span class="llm-hint">{{ store.meta?.llm.enabled ? '模型在线' : '离线规则' }}</span>
          <span>SESSION {{ sessionLabel }}</span>
          <span class="sbtns">
            <a class="sbtn manage" href="/manage" target="_blank" title="管理数据源">管理</a>
            <button class="sbtn" title="新建对话" @click="store.newChat()">＋</button>
            <button class="sbtn" title="对话记录" @click="store.toggleDrawer()">☰</button>
          </span>
        </span>
      </div>
    </header>

    <div ref="msgs" class="msgs">
      <div v-if="!store.datasetId" class="no-data">
        <b>还没有数据源</b>
        <p>导入 CSV/Excel、确认口径并发布后，这里就能开始问答。</p>
        <a class="primary-link" href="/manage">去导入数据源 →</a>
      </div>
      <div
        v-for="m in store.messages"
        :key="m.id"
        class="msg"
        :class="[m.role, m.kind === 'error' ? 'error' : '']"
      >
        <div class="who">
          <i></i>{{ m.role === 'user' ? 'YOU' : 'MINGZHANG' }}
          <span v-if="m.semanticStatus === 'draft'" class="msg-badge">草稿口径</span>
        </div>
        <div v-if="m.role === 'user' || m.kind === 'error'" class="body">{{ m.text }}</div>
        <MarkdownBody v-else :text="m.text" />
        <div v-if="m.options && m.options.length" class="clarify-opts">
          <button
            v-for="opt in m.options.slice(0, 12)"
            :key="opt"
            class="chip"
            @click="store.submitClarification(opt)"
          >
            {{ opt }}
          </button>
        </div>
        <div v-if="m.trace && m.trace.length" class="trace">
          <span v-for="(t, i) in m.trace.slice(-7)" :key="`${m.id}-${i}`">{{ t }}</span>
        </div>
      </div>
      <div v-if="store.busy" class="msg assistant">
        <div class="who"><i></i>MINGZHANG</div>
        <div class="typing"><i></i><i></i><i></i></div>
        <div class="typing-line">
          {{ store.stage || '处理中' }} · {{ elapsed }}s
          <span v-if="slow">· 模型响应较慢，仍在处理</span>
        </div>
      </div>
    </div>

    <div class="composer">
      <div class="chips">
        <button
          v-for="s in store.suggestions"
          :key="s"
          class="chip"
          :disabled="store.busy"
          @click="send(s)"
        >
          {{ s }}
        </button>
      </div>
      <div class="input-wrap">
        <textarea
          ref="box"
          v-model="input"
          class="input"
          rows="1"
          :placeholder="store.datasetId ? '问一个经营问题…' : '请先导入数据源'"
          :disabled="!store.datasetId"
          @keydown="onKeydown"
          @input="resize"
        ></textarea>
        <button
          class="send"
          :disabled="!store.datasetId || store.busy || !input.trim()"
          @click="send()"
        >
          发送 ↵
        </button>
      </div>
      <div class="hint">
        <span>Enter 发送 · Shift+Enter 换行</span>
        <span>只读 · 不写库</span>
      </div>
    </div>
  </aside>
</template>
