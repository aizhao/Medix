<script setup>
import { nextTick, ref, watch } from 'vue'
import UiIcon from './UiIcon.vue'
const props = defineProps({ modelValue: String, busy: Boolean })
const emit = defineEmits(['update:modelValue', 'send', 'stop', 'knowledge'])
const field = ref(null)
watch(() => props.modelValue, async () => { await nextTick(); if (field.value) { field.value.style.height = 'auto'; field.value.style.height = `${Math.min(180, field.value.scrollHeight)}px` } })
function onKey(event) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault()
    if (!props.busy && props.modelValue?.trim()) emit('send')
  }
}
</script>
<template>
  <form class="prompt-bar" @submit.prevent="emit('send')">
    <textarea ref="field" aria-label="医学问题" :value="modelValue" :disabled="busy" rows="2" maxlength="8000" placeholder="描述你的症状、疾病，或想了解的医学问题…" @input="emit('update:modelValue', $event.target.value)" @keydown="onKey"></textarea>
    <div class="prompt-toolbar"><button type="button" class="context-chip" @click="emit('knowledge')"><UiIcon name="library" :size="15" />知识库<span class="chip-dot"></span></button><span class="prompt-mode"><UiIcon name="shield" :size="14" />循证问答</span><span class="prompt-shortcut">Shift + Enter 换行</span><button v-if="busy" type="button" class="prompt-send" aria-label="停止生成" @click="emit('stop')"><UiIcon name="stop" :size="17" /></button><button v-else class="prompt-send" :disabled="!modelValue?.trim()" aria-label="发送问题"><UiIcon name="arrow" :size="19" /></button></div>
  </form>
</template>
