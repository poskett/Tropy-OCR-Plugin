// Declaration: Code generated using Anthropic Claude (Sonnet 5.5)
'use strict'
const fs = require('fs')
const path = require('path')
const { spawn } = require('child_process')
const pkg = require('./package.json')

const BUTTON_ID = 'tropy-ocr-button'
const IDLE_LABEL = 'Start OCR'
const ENGINES = ['vision', 'tesseract']
const MODES = ['auto', 'printed', 'handwritten']
const TITLE_PROPERTIES = ['http://purl.org/dc/elements/1.1/title', 'http://purl.org/dc/terms/title']
const DEFAULTS = Object.fromEntries(pkg.options.map(option => [option.field, option.default]))
const NUMBERS = pkg.options.filter(option => option.type === 'number').map(option => option.field)
const BOOLEANS = pkg.options.filter(option => option.type === 'boolean').map(option => option.field)

function mergeOptions(saved, logger) {
  const cleaned = {}
  for (const [key, value] of Object.entries(saved || {})) {
    if (value === undefined || value === null || value === '') continue
    cleaned[key] = value
  }
  const merged = { ...DEFAULTS, ...cleaned }
  for (const key of NUMBERS) {
    const number = Number(merged[key])
    merged[key] = Number.isFinite(number) ? number : DEFAULTS[key]
  }
  for (const key of BOOLEANS) merged[key] = merged[key] === true || merged[key] === 'true'
  for (const [key, allowed] of [['engine', ENGINES], ['mode', MODES]]) {
    const value = String(merged[key]).trim().toLowerCase()
    if (allowed.includes(value)) {
      merged[key] = value
    } else {
      logger.warn(`OCR setting "${key}" must be one of ${allowed.join(', ')}; using "${DEFAULTS[key]}" instead of "${merged[key]}"`)
      merged[key] = DEFAULTS[key]
    }
  }
  return merged
}

class OcrPlugin {
  constructor(options = {}, context = {}) {
    this.context = context
    this.logger = context.logger || console
    this.options = mergeOptions(options, this.logger)
    this.doc = typeof document === 'object' ? document : null
    this.child = null
    this.button = null
    this.progressUrl = null
    this.unsubscribe = null
    this.timer = null
    this.stopOnClose = () => { if (this.child) this.child.kill('SIGTERM') }
    if (!this.doc) return
    window.addEventListener('beforeunload', this.stopOnClose)
    this.doc.getElementById(BUTTON_ID)?.remove()
    this.attach()
  }

  store() {
    return this.context.window?.store || (typeof window === 'object' ? window.store : null) || null
  }

  state() {
    return this.store()?.getState?.() || null
  }

  isProjectWindow() {
    const state = this.state()
    return !!(state && state.nav && state.items)
  }

  isMainView() {
    return this.isProjectWindow() && this.state().nav.mode !== 'item'
  }

  attach() {
    let tries = 0
    const attempt = () => {
      const store = this.store()
      if (!store || !this.doc.body) return false
      this.stopWaiting()
      this.sync()
      if (store.subscribe && !this.unsubscribe) this.unsubscribe = store.subscribe(() => this.sync())
      return true
    }
    if (attempt()) return
    this.timer = setInterval(() => {
      if (attempt()) return
      tries += 1
      if (tries >= OcrPlugin.waitTries) {
        this.stopWaiting()
        this.logger.info('OCR: no Tropy project store found in this window (expected in Preferences); no button added')
      }
    }, 250)
  }

  stopWaiting() {
    if (this.timer) clearInterval(this.timer)
    this.timer = null
  }

  sync() {
    if (!this.isMainView()) {
      if (this.button) this.button.style.display = 'none'
      return
    }
    if (!this.doc.body) return
    if (!this.button) this.addButton()
    this.button.style.display = ''
  }

  addButton() {
    const button = this.doc.createElement('button')
    button.id = BUTTON_ID
    button.type = 'button'
    button.textContent = IDLE_LABEL
    button.style.cssText = 'position:fixed;bottom:12px;left:12px;z-index:9999;padding:6px 12px;border-radius:6px;border:1px solid #888;background:#fff;color:#222;cursor:pointer;transition:background 0.2s'
    button.addEventListener('click', () => this.start())
    this.doc.body.append(button)
    this.button = button
    this.setBusy(!!this.child, this.button.textContent)
  }

  selection() {
    const nav = this.state()?.nav || {}
    const items = (nav.items || []).map(Number).filter(Boolean)
    return { items, list: nav.list || null }
  }

  itemTitle(id) {
    const metadata = this.state()?.metadata?.[id]
    for (const property of TITLE_PROPERTIES) {
      const text = metadata?.[property]?.text
      if (text) return text
    }
    return `Item ${id}`
  }

  runLabel(selection) {
    const lists = this.state()?.lists || {}
    const listName = selection.list ? lists[selection.list]?.name : null
    let what = null
    if (selection.items.length === 1) what = this.itemTitle(selection.items[0])
    else if (selection.items.length > 1) what = `${selection.items.length} items`
    if (what && listName) return `${what} (list: ${listName})`
    return what || (listName ? `List: ${listName}` : '')
  }

  photoCount(selection) {
    const items = this.state()?.items || {}
    return selection.items.reduce((sum, id) => sum + (items[id]?.photos?.length || 0), 0)
  }

  launcher() {
    const o = this.options
    const bundled = path.join(__dirname, 'bin', 'tropy_ocr')
    if (!o.script && !o.python && fs.existsSync(bundled)) return { command: bundled, args: [] }
    const script = o.script || path.join(__dirname, 'tropy_ocr_plugin.py')
    return { command: o.python || 'python3', args: [script] }
  }

  buildArgs(selection) {
    const o = this.options
    const args = [...this.launcher().args, '--api-url', o.apiUrl]
    if (selection.items.length) {
      for (const id of selection.items) args.push('--item', String(id))
    } else if (selection.list) {
      args.push('--list-id', String(selection.list))
    }
    args.push('--engine', o.engine, '--model', o.model, '--ollama-host', o.ollamaHost)
    if (o.tesseractCmd) args.push('--tesseract-cmd', o.tesseractCmd)
    args.push('--tesseract-lang', o.tesseractLang, '--mode', o.mode)
    args.push('--max-dimension', String(o.maxDimension), '--pdf-dpi', String(o.pdfDpi), '--language', o.language)
    args.push('--retries', String(o.retries), '--timeout', String(o.timeout))
    args.push('--max-tokens', String(o.maxTokens), '--max-think-tokens', String(o.maxThinkTokens))
    args.push('--num-ctx', String(o.numCtx), '--repeat-penalty', String(o.repeatPenalty))
    args.push('--temperature', String(o.temperature))
    args.push('--tag', o.tag)
    if (o.noTag) args.push('--no-tag')
    if (o.overwrite) args.push('--overwrite')
    if (o.dryRun) args.push('--dry-run')
    if (o.logFile) args.push('--log-file', o.logFile)
    const label = this.runLabel(selection)
    if (label) args.push('--run-label', label)
    if (o.openProgress) args.push('--progress-port', '0')
    return args
  }

  engineLabel() {
    const o = this.options
    return o.engine === 'tesseract' ? `Tesseract (${o.tesseractLang})` : o.model
  }

  start() {
    if (this.child) {
      this.reopenProgress()
      return
    }
    const selection = this.selection()
    if (!selection.items.length && !selection.list) {
      window.alert('Select one or more items, or open a list, then press Start OCR.')
      return
    }
    let what = 'every item in the open list'
    if (selection.items.length) {
      const photos = this.photoCount(selection)
      what = `${selection.items.length} item(s)${photos ? ` (${photos} photos)` : ''}`
    }
    const note = this.options.overwrite
      ? 'Earlier automated OCR notes will be replaced.'
      : 'Photos that already have an automated OCR note are skipped.'
    const dry = this.options.dryRun ? '\nDry run: nothing will be written.' : ''
    if (!window.confirm(`Transcribe ${what}?\n\nModel: ${this.engineLabel()}\n${note}${dry}`)) return
    this.run(this.buildArgs(selection))
  }

  run(args) {
    this.setBusy(true, 'Starting OCR...')
    const summary = { ok: 0, failed: 0, skipped: 0, failedIds: [], stopped: false }
    let errors = ''
    let buffer = ''
    this.child = spawn(this.launcher().command, args, { stdio: ['ignore', 'pipe', 'pipe'] })
    this.child.stdout.on('data', chunk => {
      buffer += chunk.toString()
      const lines = buffer.split('\n')
      buffer = lines.pop()
      for (const line of lines) this.handleLine(line, summary)
    })
    this.child.stderr.on('data', chunk => { errors += chunk.toString() })
    this.child.on('error', error => this.finish(`Could not start the OCR program: ${error.message}`))
    this.child.on('close', code => {
      if (buffer) this.handleLine(buffer, summary)
      if (code !== 0 && summary.ok + summary.failed + summary.skipped === 0) {
        this.finish(`OCR did not run.\n\n${errors.trim().split('\n').slice(-6).join('\n')}`)
      } else {
        this.finish(this.report(summary))
      }
    })
  }

  handleLine(line, summary) {
    if (!line.startsWith('PROGRESS ')) return
    let event
    try { event = JSON.parse(line.slice(9)) } catch { return }
    if (event.event === 'server') {
      this.progressUrl = event.url
      this.openPage(event.url)
    } else if (event.event === 'start') {
      if (event.total > 0) this.setBusy(true, `Processing 1/${event.total}`)
    } else if (event.event === 'photo') {
      this.setBusy(true, `Processing ${Math.min(event.index + 1, event.total)}/${event.total}`)
      if (event.status === 'failed') this.logger.warn(`OCR photo ${event.photo_id} failed: ${event.error}`)
    } else if (event.event === 'done') {
      summary.ok = event.ok
      summary.failed = event.failed
      summary.skipped = event.skipped
      summary.failedIds = event.failed_ids || []
      summary.stopped = event.stopped
      summary.message = event.message || ''
      summary.tagErrors = event.tag_errors || 0
      summary.repeated = event.repeated || 0
      summary.repeatedIds = event.repeated_ids || []
    }
  }

  openPage(url) {
    try {
      require('electron').shell.openExternal(url)
    } catch {
      this.logger.info(`OCR progress page: ${url}`)
    }
  }

  report(summary) {
    if (summary.ok + summary.failed + summary.skipped === 0) {
      return 'Nothing to do: the selected items have no photos, or every photo already has an automated OCR note. Turn on "Replace earlier automated OCR notes" in the plugin settings to redo them.'
    }
    if (summary.message) {
      return `${summary.message}\n\nTranscribed: ${summary.ok}\nFailed: ${summary.failed}\nSkipped: ${summary.skipped}`
    }
    let text = `OCR finished${summary.stopped ? ' (stopped early)' : ''}.\n\nTranscribed: ${summary.ok}\nFailed: ${summary.failed}\nSkipped: ${summary.skipped}`
    if (summary.failedIds.length) text += `\n\nFailed photo ids: ${summary.failedIds.join(', ')}`
    if (summary.repeated) text += `\n\nThe model began repeating itself on ${summary.repeated} photo(s); the text was cut short and flagged in the note. Please check photo id(s): ${summary.repeatedIds.join(', ')}.`
    if (summary.tagErrors) text += `\n\n${summary.tagErrors} item(s) could not be tagged "${this.options.tag}" (the notes were saved).`
    else if (!this.options.dryRun && !this.options.noTag) text += '\n\nNew notes appear on each photo and the items are tagged.'
    else if (!this.options.dryRun) text += '\n\nNew notes appear on each photo.'
    return text
  }

  setBusy(busy, label = 'Starting OCR...') {
    if (!this.button) return
    this.button.textContent = busy ? label : IDLE_LABEL
    this.button.style.background = busy ? '#1a7f37' : '#fff'
    this.button.style.color = busy ? '#fff' : '#222'
    this.button.style.borderColor = busy ? '#1a7f37' : '#888'
    this.button.title = busy
      ? 'OCR is running. Click to reopen the progress page.'
      : 'Transcribe the selected items (or the open list if none are selected)'
    if (!busy) this.progressUrl = null
  }

  reopenProgress() {
    if (this.progressUrl) this.openPage(this.progressUrl)
    else window.alert('An OCR run is already in progress.')
  }

  finish(message) {
    this.child = null
    this.setBusy(false)
    this.logger.info(message)
    window.alert(message)
  }

  unload() {
    this.stopWaiting()
    this.unsubscribe?.()
    window.removeEventListener('beforeunload', this.stopOnClose)
    if (this.child) this.child.kill('SIGTERM')
    this.button?.remove()
  }
}

OcrPlugin.waitTries = 240

module.exports = OcrPlugin
