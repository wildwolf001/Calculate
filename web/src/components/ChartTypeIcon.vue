<script setup lang="ts">
import type { ChartType } from '../types'

defineProps<{ type: ChartType }>()
</script>

<template>
  <svg class="ct-icon" viewBox="0 0 18 18" aria-hidden="true">
    <template v-if="type === 'line'">
      <path d="M2.5 13.5 6 9l3 2 5-7" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>
      <circle cx="6" cy="9" r="1.2" fill="currentColor"/><circle cx="9" cy="11" r="1.2" fill="currentColor"/>
    </template>
    <template v-else-if="type === 'area'">
      <path d="M2.5 14V9l3-2 3 1 5-5 1.5 2v9z" fill="currentColor" opacity=".35"/>
      <path d="M2.5 14V9l3-2 3 1 5-5 1.5 2" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
    </template>
    <template v-else-if="type === 'bar' || type === 'grouped_bar' || type === 'stacked_bar'">
      <rect x="3" y="8" width="2.8" height="7" fill="currentColor" opacity=".85"/>
      <rect x="7.2" y="4" width="2.8" height="11" fill="currentColor" opacity=".55"/>
      <rect x="11.4" y="6" width="2.8" height="9" fill="currentColor" opacity=".7"/>
      <rect x="15" y="10" width="1.5" height="5" fill="currentColor" opacity=".35"/>
    </template>
    <template v-else-if="type === 'horizontal_bar'">
      <path d="M2 4.5h10M2 8.5h7M2 12.5h4" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
    </template>
    <template v-else-if="type === 'donut' || type === 'pie'">
      <circle cx="9" cy="9" r="5.4" fill="none" stroke="currentColor" stroke-width="3.2" stroke-dasharray="12 5"/>
    </template>
    <template v-else-if="type === 'scatter'">
      <circle cx="4" cy="12" r="1.6" fill="currentColor"/><circle cx="8" cy="6" r="1.6" fill="currentColor"/>
      <circle cx="13" cy="9" r="1.6" fill="currentColor"/><circle cx="10" cy="14" r="1.6" fill="currentColor"/>
    </template>
    <template v-else-if="type === 'heatmap'">
      <rect x="2" y="3" width="3.2" height="3.2" fill="currentColor" opacity=".85"/>
      <rect x="7" y="3" width="3.2" height="3.2" fill="currentColor" opacity=".4"/>
      <rect x="12" y="3" width="3.2" height="3.2" fill="currentColor" opacity=".2"/>
      <rect x="2" y="8" width="3.2" height="3.2" fill="currentColor" opacity=".3"/>
      <rect x="7" y="8" width="3.2" height="3.2" fill="currentColor" opacity=".6"/>
      <rect x="12" y="8" width="3.2" height="3.2" fill="currentColor" opacity=".95"/>
    </template>
    <template v-else-if="type === 'waterfall'">
      <path d="M2 13h3v3H2zM6 9h3v7H6zM10 5h3v11h-3z" fill="currentColor" opacity=".6"/>
      <path d="M2 8h3M6 5h3M10 2h3" stroke="currentColor" stroke-width="1" opacity=".5"/>
    </template>
    <template v-else-if="type === 'kpi'">
      <path d="M2 5h6M2 9h4M11 5h5v8h-5z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
    </template>
    <template v-else>
      <path d="M2 3.5h14M2 8h14M2 12.5h14M6 2v14" fill="none" stroke="currentColor" stroke-width="1.2"/>
    </template>
  </svg>
</template>

<style scoped>
.ct-icon { width: 15px; height: 15px; display: block; }
</style>
