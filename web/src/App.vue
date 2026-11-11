<script setup lang="ts">
import { onMounted, ref } from 'vue'
import AnalysisDialog from './components/AnalysisDialog.vue'
import ChatPanel from './components/ChatPanel.vue'
import RecordDrawer from './components/RecordDrawer.vue'
import ResultCanvas from './components/ResultCanvas.vue'
import ManageApp from './manage/ManageApp.vue'
import { useSessionStore } from './stores/session'

const store = useSessionStore()
// v2 invalidates widths saved before the 3:2 layout change.
const CHAT_WIDTH_KEY = 'mingzhang.chatWidth.v2'
const MIN_CHAT_WIDTH = 420
const MAX_CHAT_WIDTH = 720
// Left (result canvas) : right (chat) ≈ 3 : 2 → chat is 40% of the viewport.
const CHAT_WIDTH_RATIO = 0.4

function preferredChatWidth(): number {
  // Fixed pixel defaults looked wrong across laptop/monitor widths. Scale with
  // the viewport (3:2 split) and keep the chat inside a readable range.
  const scaled = window.innerWidth * CHAT_WIDTH_RATIO
  return Math.round(Math.min(MAX_CHAT_WIDTH, Math.max(MIN_CHAT_WIDTH, scaled)))
}

function savedChatWidth(): number | null {
  const saved = Number(window.localStorage.getItem(CHAT_WIDTH_KEY) || 0)
  return saved >= MIN_CHAT_WIDTH ? saved : null
}

const chatWidth = ref(savedChatWidth() ?? preferredChatWidth())
const dragging = ref(false)

onMounted(() => store.init())

function resetWidth() {
  window.localStorage.removeItem(CHAT_WIDTH_KEY)
  chatWidth.value = preferredChatWidth()
}

function startDrag(e: MouseEvent) {
  dragging.value = true
  const startX = e.clientX
  const startWidth = chatWidth.value
  const move = (ev: MouseEvent) => {
    // Chat sits on the right (row-reverse), so dragging left grows it.
    const next = startWidth - (ev.clientX - startX)
    chatWidth.value = Math.max(
      360,
      Math.min(Math.round(window.innerWidth * 0.65), next),
    )
  }
  const up = () => {
    dragging.value = false
    window.localStorage.setItem(CHAT_WIDTH_KEY, String(chatWidth.value))
    window.removeEventListener('mousemove', move)
    window.removeEventListener('mouseup', up)
  }
  window.addEventListener('mousemove', move)
  window.addEventListener('mouseup', up)
}
</script>

<template>
  <div class="app" :style="{ '--chat-w': chatWidth + 'px' }">
    <ChatPanel />
    <div
      class="splitter"
      :class="{ dragging }"
      title="拖动调整宽度 · 双击复位"
      @mousedown="startDrag"
      @dblclick="resetWidth"
    ></div>
    <ResultCanvas />

    <div v-if="store.drawerOpen" class="drawer-mask" @click="store.drawerOpen = false"></div>
    <aside v-if="store.drawerOpen" class="session-drawer">
      <header class="drawer-head">
        <b>对话记录</b>
        <span>
          <button class="ghost" @click="store.newChat()">＋ 新建</button>
          <button class="ghost" @click="store.drawerOpen = false">×</button>
        </span>
      </header>
      <div class="drawer-list">
        <div
          v-for="s in store.sessions"
          :key="s.session_id"
          class="drawer-item"
          :class="{ active: s.session_id === store.sessionId }"
          @click="store.loadSession(s.session_id)"
        >
          <div class="dtitle">{{ s.title }}</div>
          <div class="dmeta">{{ s.turns }} 轮 · {{ s.updated_at.replace('T', ' ') }}</div>
          <button class="ddel" title="删除对话" @click.stop="store.deleteChat(s.session_id)">
            删除
          </button>
        </div>
        <div v-if="!store.sessions.length" class="dempty">还没有历史对话</div>
      </div>
    </aside>

    <RecordDrawer />
    <AnalysisDialog />
    <div class="toast" :class="{ show: !!store.toast }">{{ store.toast }}</div>

    <!-- Data management opens inside the app, so the conversation and its
         results stay mounted and nothing is lost when the panel closes. -->
    <div v-if="store.manageOpen" class="manage-layer">
      <ManageApp embedded @close="store.closeManage()" />
    </div>
  </div>
</template>
