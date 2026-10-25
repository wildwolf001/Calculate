<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useSessionStore } from '../stores/session'
import type { AnalysisModule } from '../types'

const store = useSessionStore()
const selected = ref<string[]>([])
const customOn = ref(false)
const customText = ref('')
const error = ref('')

watch(() => store.analysisDialogOpen, (open) => {
  if (!open) return
  selected.value = []
  customOn.value = false
  customText.value = ''
  error.value = ''
})

const grouped = computed<[string, AnalysisModule[]][]>(() => {
  const map: Record<string, AnalysisModule[]> = {}
  for (const m of store.analysisModules) {
    ;(map[m.section] ||= []).push(m)
  }
  return Object.entries(map)
})

const queryCount = computed(() => {
  const counts: Record<string, number> = {
    trend: 1, structure: 1, driver: 2, fulfillment: 1, payment: 1,
    reputation: 2, customer: 1, seller: 1, product: 1,
  }
  return selected.value.reduce((n, id) => n + (counts[id] || 1), 0)
})

const customEntered = computed(() => customOn.value && customText.value.trim().length > 0)
const canSubmit = computed(() =>
  !store.busy && (!customOn.value || customText.value.trim().length > 0),
)

function toggle(id: string) {
  selected.value = selected.value.includes(id)
    ? selected.value.filter((x) => x !== id)
    : [...selected.value, id]
}

function selectDefaults() {
  selected.value = store.analysisModules.filter((m) => m.default).map((m) => m.id)
}

function clearAll() {
  selected.value = []
  customOn.value = false
  customText.value = ''
  error.value = ''
}

function appendHint(value: string) {
  const current = customText.value.trim()
  if (!current) {
    customText.value = value
  } else if (!current.includes(value)) {
    customText.value = `${current} ${value}`
  }
}

function submit() {
  if (!canSubmit.value) {
    error.value = '勾选了「其他」时，请先写下你的分析想法。'
    return
  }
  error.value = ''
  const custom = customEntered.value ? customText.value.trim() : ''
  const title = custom
    ? `自定义：${custom.slice(0, 16)}${custom.length > 16 ? '…' : ''}`
    : ''
  store.runAnalysis({
    sections: [...selected.value],
    customText: custom,
    customTitle: title,
  })
}
</script>

<template>
  <div
    v-if="store.analysisDialogOpen"
    class="analysis-mask"
    @click.self="store.closeAnalysisDialog()"
  >
    <section class="analysis-dialog">
      <header class="ad-head">
        <div>
          <div class="eyebrow">BUSINESS REVIEW · 生成经营分析</div>
          <div class="ad-title">选择分析范围</div>
          <div class="ad-sub">不选择任何板块、也不填写自定义分析，将生成默认完整报告。</div>
        </div>
        <button class="ghost" title="关闭" @click="store.closeAnalysisDialog()">×</button>
      </header>

      <div class="ad-body">
        <div class="ad-toolbar">
          <button class="ad-link" @click="selectDefaults">选择默认板块</button>
          <button class="ad-link" @click="clearAll">清空</button>
          <span>{{ selected.length ? `已选 ${selected.length} 个板块` : '未选择 = 默认完整报告' }}</span>
        </div>

        <div v-for="[section, mods] in grouped" :key="section" class="ad-group">
          <div class="ad-group-title">{{ section }}</div>
          <div class="ad-mods">
            <button
              v-for="m in mods"
              :key="m.id"
              class="ad-mod"
              :class="{ on: selected.includes(m.id) }"
              @click="toggle(m.id)"
            >
              <span class="ad-mod-name">{{ m.label }}</span>
              <span class="ad-mod-desc">{{ m.description }}</span>
              <em v-if="m.default">默认</em>
            </button>
          </div>
        </div>

        <div class="ad-group">
          <div class="ad-group-title">其他</div>
          <button
            class="ad-mod ad-custom"
            :class="{ on: customOn }"
            @click="customOn = !customOn"
          >
            <span class="ad-mod-name">自定义分析</span>
            <span class="ad-mod-desc">
              写下你想要的维度，作为独立板块与上面所选板块一起生成
            </span>
          </button>
          <div v-if="customOn" class="ad-custom-wrap">
            <div class="ad-custom-main">
              <textarea
                v-model="customText"
                class="ad-text"
                rows="5"
                maxlength="500"
                placeholder="例如：看看数码家电的复购和差评集中在哪些商家"
              ></textarea>
              <div class="ad-hint">
                {{ customText.length }}/500 · 自定义板块会走语义层校验，数据不支持时会明确说明
              </div>
            </div>
            <aside class="ad-hints">
              <div class="ad-hints-title">数据里可用</div>
              <div v-if="!store.analysisHints" class="ad-hints-meta">暂无可用值提示</div>
              <template v-else>
                <div class="ad-hints-sec">
                  <div class="ad-hints-label">品类（点击填入）</div>
                  <div class="ad-hints-chips">
                    <button
                      v-for="c in store.analysisHints.categories"
                      :key="c"
                      class="ad-hint-chip"
                      @click="appendHint(c)"
                    >
                      {{ c }}
                    </button>
                  </div>
                </div>
                <div class="ad-hints-sec">
                  <div class="ad-hints-label">商品示例（点击填入）</div>
                  <div class="ad-hints-chips">
                    <button
                      v-for="p in store.analysisHints.products"
                      :key="p"
                      class="ad-hint-chip"
                      @click="appendHint(p)"
                    >
                      {{ p }}
                    </button>
                  </div>
                </div>
                <div v-if="store.analysisHints.time_range?.length" class="ad-hints-meta">
                  数据覆盖 {{ store.analysisHints.time_range[0] }} 至
                  {{ store.analysisHints.time_range[1] }}
                </div>
              </template>
            </aside>
          </div>
        </div>

        <div v-if="error" class="ad-error">{{ error }}</div>
      </div>

      <footer class="ad-foot">
        <span class="ad-count">
          <template v-if="selected.length">预计 {{ queryCount }} 个预设查询</template>
          <template v-else-if="customEntered">仅自定义板块</template>
          <template v-else>默认完整报告</template>
          <template v-if="customEntered && selected.length"> + 自定义板块</template>
        </span>
        <span class="ad-actions">
          <button class="ghost" @click="store.closeAnalysisDialog()">取消</button>
          <button class="analysis-btn" :disabled="!canSubmit" @click="submit">
            生成分析
          </button>
        </span>
      </footer>
    </section>
  </div>
</template>
