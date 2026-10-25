<script setup lang="ts">
import { computed } from 'vue'
import DOMPurify from 'dompurify'
import { marked } from 'marked'

const props = defineProps<{ text: string }>()

marked.setOptions({ gfm: true, breaks: true })

const html = computed(() => {
  // marked treats a single "~" as strikethrough, so date ranges such as
  // 2025-11-01~2025-12-01 would be struck out. Escape lone tildes; keep the
  // intentional ~~strikethrough~~ syntax intact.
  const source = (props.text || '').replace(/(?<!~)~(?!~)/g, '\\~')
  const raw = marked.parse(source) as string
  return DOMPurify.sanitize(raw, { USE_PROFILES: { html: true } })
})
</script>

<template>
  <div class="md" v-html="html"></div>
</template>
