<script setup lang="ts">
defineProps<{
  columns: string[]
  rows: any[][]
  labels: Record<string, string>
  format: (col: string, value: any) => string
  drillable?: boolean
  dimensions?: string[]
  rowCount?: number
  truncated?: boolean
}>()
const emit = defineEmits<{
  (e: 'cell', payload: {
    column: string
    label: string
    value: string
    row: any[]
    x: number
    y: number
  }): void
}>()

function clickCell(event: MouseEvent, column: string, label: string,
                   value: any, row: any[]) {
  const target = event.currentTarget as HTMLElement
  if (!target.classList.contains('drill-cell')) return
  emit('cell', {
    column,
    label,
    value: String(value ?? ''),
    row,
    x: event.clientX,
    y: event.clientY,
  })
}
</script>

<template>
  <div class="tbl-wrap">
    <table class="data">
      <thead>
        <tr>
          <th v-for="c in columns" :key="c">{{ labels[c] || c }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(row, ri) in rows" :key="ri">
          <td
            v-for="(c, ci) in columns"
            :key="c"
            :class="{ 'drill-cell': (dimensions || []).includes(c) }"
            :title="(dimensions || []).includes(c) ? '点击：筛选 / 下钻 / 看明细' : String(format(c, row[ci]))"
            @click="clickCell($event, c, labels[c] || c, row[ci], row)"
          >
            {{ format(c, row[ci]) }}
            <span v-if="(dimensions || []).includes(c)" class="drill-mark">↘</span>
          </td>
        </tr>
      </tbody>
    </table>
    <div class="tbl-foot">
      <span>
        显示 <b>{{ rows.length }}</b> 行
        <template v-if="rowCount !== undefined"> / 共 {{ rowCount }} 行</template>
        <template v-if="truncated"> · 已截断</template>
      </span>
      <span v-if="drillable">点击维度值可筛选 / 下钻</span>
    </div>
  </div>
</template>

<style scoped>
.drill-cell { cursor: pointer; color: var(--ink); font-weight: 500; }
.drill-cell:hover { color: var(--accent-2); background: rgba(23, 94, 88, .05); }
.drill-mark { color: var(--ink-3); font-size: 9px; margin-left: 5px; opacity: .55; }
.drill-cell:hover .drill-mark { color: var(--accent-2); opacity: 1; }
</style>
