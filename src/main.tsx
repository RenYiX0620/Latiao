import { Component, StrictMode, type ReactNode } from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import { I18nProvider } from './i18n'
import './index.css'

// Global error overlay — catches errors outside React render cycle
// Reads language from localStorage to show i18n error / reload labels
const ERROR_TEXTS: Record<string, { title: string; reload: string }> = {
  zh: { title: '⚠️ 应用崩溃', reload: '重新加载' },
  en: { title: '⚠️ App Crashed', reload: 'Reload' },
  ja: { title: '⚠️ アプリがクラッシュ', reload: '再読み込み' },
  ru: { title: '⚠️ Сбой', reload: 'Перезагрузить' },
};

function showErrorOverlay(message: string, stack?: string) {
  const existing = document.getElementById('global-error-overlay')
  if (existing) existing.remove()
  const lang = (localStorage.getItem('latiao_language') || 'zh') as string
  const t = ERROR_TEXTS[lang] || ERROR_TEXTS.zh
  const overlay = document.createElement('div')
  overlay.id = 'global-error-overlay'
  overlay.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;z-index:99999;padding:40px;font-family:system-ui,sans-serif;background:#1a1a2e;color:#eee;overflow:auto'
  overlay.innerHTML = `
    <h1 style="color:#e74c3c">${t.title}</h1>
    <pre style="background:#0d0d1a;padding:20px;border-radius:8px;overflow:auto;font-size:13px;line-height:1.6;color:#ff6b6b;white-space:pre-wrap;word-break:break-all">${message}\n\n${stack || ''}</pre>
    <button style="margin-top:20px;padding:10px 24px;font-size:14px;background:#e74c3c;color:#fff;border:none;border-radius:6px;cursor:pointer" onclick="document.getElementById('global-error-overlay')?.remove();location.reload()">${t.reload}</button>
  `
  document.body.appendChild(overlay)
}

window.addEventListener('error', (e) => {
  showErrorOverlay(e.message, e.error?.stack)
})
window.addEventListener('unhandledrejection', (e) => {
  showErrorOverlay(`Unhandled Promise Rejection: ${e.reason}`, e.reason?.stack)
})

class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  constructor(props: { children: ReactNode }) {
    super(props)
    this.state = { error: null }
  }
  static getDerivedStateFromError(error: Error) {
    return { error }
  }
  componentDidCatch(error: Error) {
    showErrorOverlay(error.message, error.stack)
  }
  render() {
    if (this.state.error) {
      return null // overlay handles display
    }
    return this.props.children
  }
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <I18nProvider>
        <App />
      </I18nProvider>
    </ErrorBoundary>
  </StrictMode>,
)
