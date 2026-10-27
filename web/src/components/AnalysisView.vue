<script setup lang="ts">
import { computed, ref } from 'vue'
import MarkdownBody from './MarkdownBody.vue'
import { useSessionStore } from '../stores/session'

const store = useSessionStore()
const reportEl = ref<HTMLElement | null>(null)
const exportingImage = ref(false)
const report = computed(() => store.analysis?.report || '')
const sections = computed(() => store.analysis?.sections || [])
const sectionCount = computed(() => new Set(sections.value.map((s) => s.section)).size)
const failures = computed(() => store.analysis?.failures?.length || 0)
const writer = computed(() => (store.analysis?.writer === 'llm' ? '模型解读' : '规则解读'))

function exportMarkdown() {
  const text = store.analysis?.report || ''
  if (!text) return
  const stamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, '')
  const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `经营分析_${stamp}.md`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
  store.setToast('已导出 Markdown')
}

async function exportImage() {
  if (!report.value || exportingImage.value) return
  exportingImage.value = true
  store.setToast('正在生成图片…')
  try {
    const width = 1280
    const padding = 64
    const lineHeight = 38
    const probe = document.createElement('canvas').getContext('2d')
    if (!probe) throw new Error('浏览器不支持 Canvas')
    probe.font = '22px sans-serif'
    const lines: { text: string; heading: boolean }[] = []
    for (const raw of report.value.split(/\r?\n/)) {
      const heading = /^#{1,3}\s/.test(raw)
      const text = raw.replace(/^#{1,3}\s+/, '').replace(/[*`|]/g, '')
      if (!text.trim()) {
        lines.push({ text: '', heading: false })
        continue
      }
      const size = heading ? 28 : 22
      probe.font = `${size}px sans-serif`
      let current = ''
      for (const char of text) {
        if (current && probe.measureText(current + char).width > width - padding * 2) {
          lines.push({ text: current, heading })
          current = char
        } else {
          current += char
        }
      }
      if (current) lines.push({ text: current, heading })
    }
    const canvas = document.createElement('canvas')
    canvas.width = width
    canvas.height = Math.max(720, padding * 2 + lines.length * lineHeight)
    const ctx = canvas.getContext('2d')
    if (!ctx) throw new Error('浏览器不支持 Canvas')
    ctx.fillStyle = '#FFFDF8'
    ctx.fillRect(0, 0, canvas.width, canvas.height)
    ctx.fillStyle = '#2B2925'
    ctx.textBaseline = 'top'
    lines.forEach((line, index) => {
      ctx.font = line.heading ? '600 28px sans-serif' : '22px sans-serif'
      ctx.fillText(line.text, padding, padding + index * lineHeight)
    })
    const stamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, '')
    const a = document.createElement('a')
    a.href = canvas.toDataURL('image/png')
    a.download = `经营分析_${stamp}.png`
    document.body.appendChild(a)
    a.click()
    a.remove()
    store.setToast('已导出图片')
  } catch (err: any) {
    store.setToast(`图片导出失败：${err.message || err}`)
  } finally {
    exportingImage.value = false
  }
}
</script>

<template>
  <section class="analysis-view">
    <header class="analysis-view-head">
      <div>
        <div class="eyebrow">BUSINESS REVIEW · 经营分析</div>
        <div class="av-title">经营分析报告</div>
      </div>
      <div class="av-right">
        <div class="av-meta">
          <span>{{ sectionCount }} 个板块 · {{ sections.length }} 个查询</span>
          <span>{{ writer }}</span>
          <span v-if="failures">{{ failures }} 个板块未完成</span>
          <span v-else>全部板块已完成</span>
        </div>
        <button class="ghost" title="导出 Markdown 报告" @click="exportMarkdown">
          导出 MD
        </button>
        <button
          class="ghost"
          title="导出 PNG 图片"
          :disabled="exportingImage"
          @click="exportImage"
        >
          {{ exportingImage ? '生成中…' : '导出图片' }}
        </button>
      </div>
    </header>
    <div class="analysis-view-body">
      <div ref="reportEl" class="report-capture">
        <MarkdownBody :text="report" />
      </div>
    </div>
  </section>
</template>
