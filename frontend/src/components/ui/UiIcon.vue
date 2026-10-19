<script setup>
defineProps({ name: String, size: { type: Number, default: 18 } })
const paths = {
  chat: 'M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5Z',
  library: 'M4 4h4v16H4z M11 4h4v16h-4z M18 4l4 15 M4 8h4 M11 8h4',
  activity: 'M3 12h4l3-8 4 16 3-8h4',
  plus: 'M12 5v14 M5 12h14',
  search: 'M21 21l-5-5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  arrow: 'M12 19V5 M5 12l7-7 7 7',
  right: 'M5 12h14 M12 5l7 7-7 7',
  chevron: 'M9 5l7 7-7 7',
  close: 'M6 6l12 12 M6 18 18 6',
  shield: 'M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z M8 12l3 3 5-6',
  file: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6Z M14 2v6h6 M8 13h8 M8 17h5',
  upload: 'M12 16V3 M7 8l5-5 5 5 M4 16v4h16v-4',
  refresh: 'M20 7v5h-5 M4 17v-5h5 M6 7a7 7 0 0 1 12-1l2 6 M4 12l2 6a7 7 0 0 0 12-1',
  layers: 'M12 3 2 8l10 5 10-5-10-5Z M2 12l10 5 10-5 M2 16l10 5 10-5',
  check: 'M5 12l4 4L19 6',
  moon: 'M20 15.6A9 9 0 0 1 8.4 4 9 9 0 1 0 20 15.6Z',
  sun: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M12 2v2 M12 20v2 M2 12h2 M20 12h2 M5 5l1 1 M18 18l1 1 M5 19l1-1 M18 6l1-1',
  panel: 'M3 4h18v16H3z M8 4v16',
  edit: 'm16 3 5 5-12 12-6 1 1-6L16 3Z M13 6l5 5',
  trash: 'M3 6h18 M9 6V3h6v3 M5 6l1 15h12l1-15 M10 10v7 M14 10v7',
  copy: 'M9 9h12v12H9z M15 9V3H3v12h6',
  sparkle: 'm12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z',
  stop: 'M6 6h12v12H6z',
  external: 'M14 3h7v7 M21 3 10 14 M10 3H3v18h18v-7',
}
</script>
<template><svg :width="size" :height="size" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" class="ui-icon"><path :d="paths[name] || paths.file" /></svg></template>
