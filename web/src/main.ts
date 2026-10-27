import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import ManageApp from './manage/ManageApp.vue'
import './styles.css'

// Two surfaces share one build: the chat app at "/", and the standalone data
// source workbench at "/manage". The backend SPA fallback serves index.html
// for both paths.
const isManage = window.location.pathname.startsWith('/manage')
createApp(isManage ? ManageApp : App).use(createPinia()).mount('#app')
