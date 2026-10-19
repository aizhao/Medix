<script setup>
// Vue adaptation of Beautiful UI ThinkingState; driven only by SSE events.
import UiIcon from './UiIcon.vue'
defineProps({ steps: { type: Array, default: () => [] }, busy: Boolean, label: String })
</script>
<template>
  <details class="thinking-state" :open="busy">
    <summary><span v-if="busy" class="ui-spinner"></span><UiIcon v-else name="check" :size="15" /><span :class="{ shimmer: busy }">{{ label || (busy ? '正在整理证据' : '处理记录') }}</span><UiIcon name="chevron" :size="13" /></summary>
    <ol><li v-for="(step, index) in steps" :key="index"><span :class="['step-marker', { current: busy && index === steps.length - 1 }]"></span>{{ step }}</li></ol>
  </details>
</template>
