import { createApp } from 'vue'
import App from './App.vue'
import './styles.css'
import './product.css'
import './beautiful.css'
import { dialog } from './dialog'

createApp(App).directive('dialog', dialog).mount('#app')
