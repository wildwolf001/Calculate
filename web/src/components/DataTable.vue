<script setup lang="ts">
defineProps<{
  columns: string[]
  rows: any[][]
  labels: Record<string, string>
  format: (col: string, value: any) => string
  drillable?: boolean
  rowCount?: number
  truncated?: boolean
}>()
const emit = defineEmits<{ (e: 'drill', row: any[]): void }>()
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
        <tr
          v-for="(row, ri) in rows"
          :key="ri"
          :class="{ drillable }"
          @click="drillable && emit('drill', row)"
        >
          <td v-for="(c, ci) in columns" :key="c" :title="String(format(c, row[ci]))">
            {{ format(c, row[ci]) }}
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
      <span v-if="drillable">点击首列可下钻</span>
    </div>
  </div>
</template>
