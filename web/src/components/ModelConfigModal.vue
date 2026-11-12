<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { getLLMConfig, saveLLMConfig, testLLMConfig } from '../api'
import type { LLMConfig } from '../types'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'saved', cfg: LLMConfig): void }>()

const config = ref<LLMConfig | null>(null)
const provider = ref('deepseek')
const model = ref('')
const baseUrl = ref('')
const apiKey = ref('')
const clearKey = ref(false)
const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const error = ref('')
const notice = ref('')

const preset = computed(() => config.value?.presets?.[provider.value])
const canSave = computed(() => !saving.value && !!model.value.trim() && !!baseUrl.value.trim())

function applyProvider(next: string) {
  const p = config.value?.presets?.[next]
  if (!p) return
  model.value = p.model
  baseUrl.value = p.base_url
}

async function load() {
  loading.value = true
  error.value = ''
  notice.value = ''
  try {
    const cfg = await getLLMConfig()
    config.value = cfg
    provider.value = cfg.provider
    model.value = cfg.model
    baseUrl.value = cfg.base_url
    apiKey.value = ''
    clearKey.value = false
  } catch (err: any) {
    error.value = err?.message || String(err)
  } finally {
    loading.value = false
  }
}

watch(
  () => props.open,
  (open) => { if (open) void load() },
  { immediate: true },
)

function onProviderChange(e: Event) {
  const value = (e.target as HTMLSelectElement).value
  provider.value = value
  applyProvider(value)
}

async function save() {
  if (!canSave.value) return
  saving.value = true
  error.value = ''
  notice.value = ''
  try {
    const body: Record<string, any> = {
      provider: provider.value,
      model: model.value.trim(),
      base_url: baseUrl.value.trim(),
      clear_api_key: clearKey.value,
    }
    if (apiKey.value.trim()) body.api_key = apiKey.value.trim()
    const cfg = await saveLLMConfig(body)
    config.value = cfg
    apiKey.value = ''
    clearKey.value = false
    notice.value = '已保存，下一轮对话会使用新模型。'
    emit('saved', cfg)
  } catch (err: any) {
    error.value = err?.message || String(err)
  } finally {
    saving.value = false
  }
}

async function test() {
  testing.value = true
  error.value = ''
  notice.value = ''
  try {
    const result = await testLLMConfig({
      provider: provider.value,
      model: model.value.trim(),
      base_url: baseUrl.value.trim(),
      api_key: apiKey.value.trim() || undefined,
    })
    notice.value = result?.ok ? `连接成功：${result.model}` : '连接测试未通过'
  } catch (err: any) {
    error.value = err?.message || String(err)
  } finally {
    testing.value = false
  }
}

function close() {
  if (saving.value || testing.value) return
  emit('close')
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="llm-overlay" @click.self="close">
      <div class="llm-modal" role="dialog" aria-modal="true" aria-label="模型配置">
        <div class="llm-head">
          <div>
            <div class="llm-kicker">LLM SETTINGS</div>
            <h2>模型与 API Key</h2>
          </div>
          <button class="llm-close" title="关闭" @click="close">×</button>
        </div>

        <div v-if="loading" class="llm-loading">正在读取配置…</div>
        <template v-else>
          <div class="llm-status" :class="{ on: config?.enabled, off: !config?.enabled }">
            <span class="llm-dot"></span>
            {{ config?.enabled ? '模型已启用' : '当前未启用模型' }}
            <span v-if="config?.has_api_key" class="llm-key-hint">已保存 {{ config.api_key_masked }}</span>
            <span v-else class="llm-key-hint">还没有保存 API Key</span>
          </div>

          <label class="llm-field">
            <span>模型提供方</span>
            <select :value="provider" @change="onProviderChange">
              <option v-for="(p, key) in (config?.presets || {})" :key="key" :value="key">
                {{ p.label }}
              </option>
            </select>
          </label>

          <div class="llm-grid">
            <label class="llm-field">
              <span>模型名</span>
              <input v-model="model" type="text" autocomplete="off" spellcheck="false" />
            </label>
            <label class="llm-field">
              <span>API Key</span>
              <input
                v-model="apiKey"
                type="password"
                autocomplete="new-password"
                :placeholder="config?.has_api_key ? '留空保持不变' : '请输入 API Key'"
              />
            </label>
          </div>

          <label class="llm-field">
            <span>Base URL</span>
            <input v-model="baseUrl" type="text" autocomplete="off" spellcheck="false" />
          </label>

          <div class="llm-preset-line">
            <span v-if="preset">预设：{{ preset.protocol === 'openai' ? 'OpenAI 兼容协议' : 'Anthropic 兼容协议' }}</span>
            <label v-if="config?.has_api_key" class="llm-clear">
              <input v-model="clearKey" type="checkbox" />
              清除已保存 Key
            </label>
          </div>

          <div v-if="error" class="llm-error">{{ error }}</div>
          <div v-else-if="notice" class="llm-notice">{{ notice }}</div>

          <p class="llm-footnote">
            配置保存在本机 <code>data/runtime/llm_config.json</code>，Key 不会回显到页面。
            DeepSeek 使用 Anthropic 兼容协议，Qwen 使用 OpenAI 兼容协议。
          </p>

          <div class="llm-actions">
            <button class="llm-btn ghost" :disabled="testing || saving" @click="test">
              {{ testing ? '测试中…' : '测试连接' }}
            </button>
            <div class="llm-spacer"></div>
            <button class="llm-btn ghost" :disabled="saving || testing" @click="close">取消</button>
            <button class="llm-btn primary" :disabled="!canSave || testing" @click="save">
              {{ saving ? '保存中…' : '保存配置' }}
            </button>
          </div>
        </template>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.llm-overlay {
  position: fixed;
  inset: 0;
  z-index: 1000;
  background: rgba(26, 24, 20, .38);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.llm-modal {
  width: min(540px, 100%);
  max-height: calc(100vh - 48px);
  overflow: auto;
  background: var(--paper, #F7F2E9);
  color: var(--ink, #1A1814);
  border: 1px solid var(--rule, #D8D0C0);
  border-radius: 4px;
  box-shadow: 0 24px 70px rgba(26, 24, 20, .22);
  padding: 24px;
}
.llm-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.llm-kicker { color: var(--ink-3, #8A8174); font: 9px var(--mono, monospace); letter-spacing: .12em; }
.llm-head h2 { margin: 4px 0 0; font-family: var(--serif, serif); font-size: 22px; font-weight: 500; }
.llm-close { border: 0; background: transparent; color: var(--ink-3, #8A8174); font-size: 24px; line-height: 1; cursor: pointer; }
.llm-close:hover { color: var(--ink, #1A1814); }
.llm-loading { padding: 30px 0; color: var(--ink-3, #8A8174); font-size: 12px; }
.llm-status {
  display: flex; align-items: center; flex-wrap: wrap; gap: 8px;
  margin: 18px 0 16px; padding: 9px 10px;
  border: 1px solid var(--rule, #D8D0C0); background: var(--card, #FFFDF8);
  font-size: 11px;
}
.llm-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--rust, #B23B2B); }
.llm-status.on .llm-dot { background: var(--accent, #175E58); }
.llm-status.off .llm-dot { background: var(--rust, #B23B2B); }
.llm-key-hint { margin-left: auto; color: var(--ink-3, #8A8174); font: 10px var(--mono, monospace); }
.llm-field { display: flex; flex-direction: column; gap: 5px; margin: 11px 0; font-size: 10px; color: var(--ink-3, #8A8174); }
.llm-field input, .llm-field select {
  width: 100%; box-sizing: border-box;
  border: 1px solid var(--rule, #D8D0C0); background: var(--card, #FFFDF8);
  color: var(--ink, #1A1814); padding: 9px 10px;
  font: 12px var(--mono, monospace); outline: none; border-radius: 2px;
}
.llm-field input:focus, .llm-field select:focus { border-color: var(--accent, #175E58); }
.llm-grid { display: grid; grid-template-columns: 1fr 1.15fr; gap: 0 12px; }
.llm-preset-line { display: flex; justify-content: space-between; gap: 10px; flex-wrap: wrap; color: var(--ink-3, #8A8174); font-size: 10px; margin: 8px 0 4px; }
.llm-clear { display: inline-flex; align-items: center; gap: 5px; cursor: pointer; }
.llm-error, .llm-notice { margin: 10px 0 0; padding: 8px 10px; font-size: 11px; line-height: 1.5; border: 1px solid; }
.llm-error { color: #7A251B; background: #F8E7E1; border-color: #D9A79C; }
.llm-notice { color: #175E58; background: #E8F1EC; border-color: #B9D3C8; }
.llm-footnote { margin: 14px 0 0; color: var(--ink-3, #8A8174); font-size: 10px; line-height: 1.6; }
.llm-footnote code { font-family: var(--mono, monospace); color: var(--ink-2, #5C554A); }
.llm-actions { display: flex; align-items: center; gap: 8px; margin-top: 18px; padding-top: 14px; border-top: 1px solid var(--rule, #D8D0C0); }
.llm-spacer { flex: 1; }
.llm-btn { border: 1px solid var(--rule, #D8D0C0); background: transparent; padding: 8px 12px; font-size: 11px; cursor: pointer; border-radius: 2px; }
.llm-btn.primary { background: var(--ink, #1A1814); color: var(--paper, #F7F2E9); border-color: var(--ink, #1A1814); }
.llm-btn:disabled { opacity: .45; cursor: not-allowed; }
.llm-btn.ghost:hover:not(:disabled) { background: var(--card, #FFFDF8); }
@media (max-width: 560px) {
  .llm-modal { padding: 18px; }
  .llm-grid { grid-template-columns: 1fr; }
  .llm-key-hint { margin-left: 0; }
}
</style>
