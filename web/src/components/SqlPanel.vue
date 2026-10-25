<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ sql: string; params?: string[]; fingerprint?: string }>()

const escaped = computed(() =>
  (props.sql || '-- 本轮没有生成 SQL（例如口径解释类问题）')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;'),
)

const highlighted = computed(() =>
  escaped.value
    .replace(/('[^']*')/g, '<span class="str">$1</span>')
    .replace(
      /\b(SELECT|FROM|WHERE|JOIN|LEFT|RIGHT|INNER|OUTER|ON|GROUP|BY|ORDER|LIMIT|WITH|AS|AND|OR|CASE|WHEN|THEN|ELSE|END|HAVING|COUNT|SUM|AVG|MAX|MIN|DISTINCT|NULLIF|ROUND|STRFTIME|IN|IS|NOT|NULL)\b/g,
      '<span class="kw">$1</span>',
    ),
)
</script>

<template>
  <div>
    <div class="sqlbar">
      <span class="lab">只读 SQL · 语义层编译</span>
      <span v-if="fingerprint" class="lab">FP {{ fingerprint }}</span>
    </div>
    <pre class="sql" v-html="highlighted"></pre>
    <div v-if="params && params.length" class="sqlbar">
      <span class="lab">绑定参数</span>
      <span class="lab">{{ params.join(' · ') }}</span>
    </div>
  </div>
</template>
