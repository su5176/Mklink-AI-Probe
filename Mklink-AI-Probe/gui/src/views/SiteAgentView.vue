<template>
  <main class="site-agent-page">
    <section class="page-heading">
      <div>
        <p class="eyebrow">MKLINK.REMOTE</p>
        <h2>{{ tr('现场机连接', 'Site Agent') }}</h2>
        <p>{{ tr('让安装了 Skill 的工程师通过 Claude Code、Codex 等 Agent 平台安全连接这台 Windows 现场机。', 'Let engineers using the Skill from Claude Code, Codex, or another agent platform connect securely to this Windows field machine.') }}</p>
      </div>
      <div :class="['runtime-badge', runtimeTone]" data-testid="site-agent-runtime">
        <span class="status-dot" />{{ runtimeLabel }}
      </div>
    </section>

    <div v-if="loading" class="panel loading-state">{{ tr('正在读取现场服务配置…', 'Loading Site Agent configuration…') }}</div>
    <template v-else>
      <section v-if="errorMessage" class="alert alert-error" role="alert">{{ errorMessage }}</section>

      <section class="overview-grid">
        <article class="metric-card">
          <span>{{ tr('远程入口', 'Remote endpoint') }}</span>
          <strong>{{ endpoint }}</strong>
          <small>{{ config.transport === 'lan-stcp' ? tr('由 STCP 私密隧道转发', 'Forwarded by a private STCP tunnel') : tr('WebSocket 直连', 'Direct WebSocket') }}</small>
        </article>
        <article class="metric-card">
          <span>{{ tr('探针', 'Probe') }}</span>
          <strong>{{ status?.probe_connected ? tr('已连接', 'Connected') : tr('未连接', 'Disconnected') }}</strong>
          <small>{{ tr('与主 GUI 共用同一个设备实例', 'Shared with the main GUI device instance') }}</small>
        </article>
        <article class="metric-card">
          <span>{{ tr('访问令牌', 'Access token') }}</span>
          <strong>{{ secrets.token_configured ? `•••• ${secrets.token_fingerprint ?? ''}` : tr('未配置', 'Not configured') }}</strong>
          <small>{{ tr('仅由 Windows DPAPI 加密保存', 'Encrypted at rest with Windows DPAPI') }}</small>
        </article>
      </section>

      <section class="settings-layout">
        <article class="panel">
          <div class="panel-title">
            <div>
              <h3>{{ tr('服务设置', 'Service settings') }}</h3>
              <p>{{ tr('保存后会重启统一后端，主 GUI 与现场服务仍由同一个 sidecar 承载。', 'Applying changes restarts the unified sidecar that hosts both the main GUI API and Site Agent.') }}</p>
            </div>
            <label class="switch-row">
              <input v-model="config.enabled" data-testid="site-agent-enabled" type="checkbox">
              <span>{{ config.enabled ? tr('已启用', 'Enabled') : tr('已停用', 'Disabled') }}</span>
            </label>
          </div>

          <div class="form-grid">
            <label>
              <span>{{ tr('连接模式', 'Transport') }}</span>
              <select v-model="config.transport" data-testid="site-agent-transport">
                <option value="direct">{{ tr('直连（局域网 / VPN）', 'Direct (LAN / VPN)') }}</option>
                <option value="lan-stcp">{{ tr('LAN STCP 私密隧道', 'LAN STCP private tunnel') }}</option>
              </select>
            </label>
            <label>
              <span>{{ tr('监听地址', 'Bind address') }}</span>
              <select v-model="config.bind_host" data-testid="site-agent-bind">
                <option v-for="address in bindAddresses" :key="address" :value="address">{{ address }}</option>
              </select>
            </label>
            <label>
              <span>{{ tr('监听端口', 'Port') }}</span>
              <input v-model.number="config.port" data-testid="site-agent-port" type="number" min="1" max="65535">
            </label>
            <label v-if="config.transport === 'direct'" class="check-field">
              <input v-model="config.allow_lan" type="checkbox">
              <span>{{ tr('明确允许非回环 LAN / VPN 连接', 'Explicitly allow non-loopback LAN / VPN access') }}</span>
            </label>
          </div>

          <div v-if="config.transport === 'lan-stcp'" class="stcp-grid">
            <label><span>FRP Server</span><input v-model.trim="config.stcp_server_addr" placeholder="192.168.1.20"></label>
            <label><span>{{ tr('服务端口', 'Server port') }}</span><input v-model.number="config.stcp_server_port" type="number" min="1" max="65535"></label>
            <label><span>User</span><input v-model.trim="config.stcp_user"></label>
            <label><span>Proxy Name</span><input v-model.trim="config.stcp_proxy_name"></label>
          </div>

          <div class="actions">
            <button class="btn btn-primary" data-testid="site-agent-save" :disabled="saving" @click="saveAndApply">
              {{ saving ? tr('正在应用…', 'Applying…') : tr('保存并应用', 'Save & Apply') }}
            </button>
            <button class="btn" :disabled="refreshing" @click="refreshStatus">{{ tr('刷新状态', 'Refresh status') }}</button>
          </div>
        </article>

        <aside class="side-stack">
          <article class="panel credential-panel">
            <h3>{{ tr('凭据', 'Credentials') }}</h3>
            <p>{{ tr('令牌只在生成时复制到剪贴板，界面和后端状态都不会回显明文。', 'The token is copied only when generated; neither the UI nor backend status returns its plaintext.') }}</p>
            <button class="btn" data-testid="site-agent-token" :disabled="credentialBusy" @click="generateToken">
              {{ secrets.token_configured ? tr('轮换令牌并复制', 'Rotate & copy token') : tr('生成令牌并复制', 'Generate & copy token') }}
            </button>

            <div v-if="config.transport === 'lan-stcp'" class="secret-fields">
              <label><span>FRP Auth Token</span><input v-model="stcpAuth" type="password" autocomplete="new-password"></label>
              <label><span>STCP Secret</span><input v-model="stcpSecret" type="password" autocomplete="new-password"></label>
              <button class="btn" :disabled="credentialBusy || !stcpAuth || !stcpSecret" @click="saveStcpCredentials">{{ tr('加密保存 STCP 凭据', 'Encrypt & save STCP credentials') }}</button>
            </div>
          </article>

          <article class="panel workflow-panel">
            <h3>{{ tr('工程师连接方式', 'Engineer workflow') }}</h3>
            <ol>
              <li>{{ tr('在工程师机器安装仓库中的 Mklink Skill。', 'Install the repository Mklink Skill on the engineer machine.') }}</li>
              <li>{{ tr('在 Claude Code、Codex 等 Agent 平台配置远程地址和刚复制的令牌。', 'Configure the endpoint and copied token in Claude Code, Codex, or another agent platform.') }}</li>
              <li>{{ tr('先调用健康检查，再按需重连探针和执行烧录、RTT、内存等操作。', 'Run health first, then reconnect the probe and perform flash, RTT, memory, or other operations as needed.') }}</li>
            </ol>
            <code>{{ endpoint }}</code>
          </article>
        </aside>
      </section>
    </template>
  </main>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import { API_BASE, restartRuntimeBackend } from '../lib/runtimeEndpoint'
import { tr } from '../composables/useLanguage'
import { useToast } from '../composables/useToast'

interface SiteAgentConfig {
  schema: string
  enabled: boolean
  transport: 'direct' | 'lan-stcp'
  bind_host: string
  port: number
  allow_lan: boolean
  stcp_server_addr: string
  stcp_server_port: number
  stcp_user: string
  stcp_proxy_name: string
}

interface SecretState {
  token_configured: boolean
  token_fingerprint: string | null
  stcp_credentials_configured: boolean
}

interface AgentStatus {
  enabled: boolean
  running: boolean
  ready: boolean
  probe_connected: boolean
  transport_ready?: boolean
  last_error?: string | null
  configuration_error?: string | null
}

const toast = useToast()
const loading = ref(true)
const saving = ref(false)
const refreshing = ref(false)
const credentialBusy = ref(false)
const bindAddresses = ref<string[]>(['127.0.0.1'])
const status = ref<AgentStatus | null>(null)
const secrets = reactive<SecretState>({ token_configured: false, token_fingerprint: null, stcp_credentials_configured: false })
const config = reactive<SiteAgentConfig>({
  schema: 'mklink.site-agent.config.v1',
  enabled: false,
  transport: 'direct',
  bind_host: '127.0.0.1',
  port: 8766,
  allow_lan: false,
  stcp_server_addr: '',
  stcp_server_port: 7000,
  stcp_user: '',
  stcp_proxy_name: '',
})
const stcpAuth = ref('')
const stcpSecret = ref('')
let timer: ReturnType<typeof setInterval> | null = null

const endpoint = computed(() => `ws://${config.bind_host.includes(':') ? `[${config.bind_host}]` : config.bind_host}:${config.port}`)
const runtimeTone = computed(() => status.value?.ready ? 'ready' : errorMessage.value ? 'failed' : 'stopped')
const runtimeLabel = computed(() => {
  if (status.value?.ready) return tr('运行中', 'Running')
  if (status.value?.last_error) return tr('需要处理', 'Needs attention')
  return tr('已停止', 'Stopped')
})
const errorMessage = computed(() => status.value?.last_error || status.value?.configuration_error || '')

function message(error: unknown) {
  return error instanceof Error ? error.message : String(error)
}

async function loadNativeState() {
  const [saved, secretState, addresses] = await Promise.all([
    invoke<SiteAgentConfig>('site_agent_config_get'),
    invoke<SecretState>('site_agent_secret_state'),
    invoke<string[]>('site_agent_bind_addresses'),
  ])
  Object.assign(config, saved)
  Object.assign(secrets, secretState)
  bindAddresses.value = addresses.includes(saved.bind_host) ? addresses : [saved.bind_host, ...addresses]
}

async function refreshStatus() {
  refreshing.value = true
  try {
    const response = await fetch(`${API_BASE}/api/site-agent/status`, { signal: AbortSignal.timeout(3000) })
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    status.value = await response.json()
  } catch (error) {
    status.value = { enabled: config.enabled, running: false, ready: false, probe_connected: false, last_error: message(error) }
  } finally {
    refreshing.value = false
  }
}

async function restartUnifiedSidecar() {
  await restartRuntimeBackend()
  window.setTimeout(() => void refreshStatus(), 700)
}

async function saveAndApply() {
  saving.value = true
  try {
    await invoke('site_agent_config_save', { config: { ...config } })
    await restartUnifiedSidecar()
    toast.success(tr('现场服务配置已应用', 'Site Agent configuration applied'))
  } catch (error) {
    toast.error(tr('应用现场服务配置失败：', 'Failed to apply Site Agent configuration: ') + message(error))
  } finally {
    saving.value = false
  }
}

async function generateToken() {
  credentialBusy.value = true
  try {
    const result = await invoke<{ fingerprint: string }>('site_agent_generate_token_and_copy')
    Object.assign(secrets, await invoke<SecretState>('site_agent_secret_state'))
    toast.success(tr(`新令牌已复制（指纹 ${result.fingerprint}）`, `New token copied (fingerprint ${result.fingerprint})`), 8000)
    if (config.enabled) await restartUnifiedSidecar()
  } catch (error) {
    toast.error(tr('生成访问令牌失败：', 'Failed to generate access token: ') + message(error))
  } finally {
    credentialBusy.value = false
  }
}

async function saveStcpCredentials() {
  credentialBusy.value = true
  try {
    await invoke('site_agent_stcp_credentials_configure', { authToken: stcpAuth.value, secretKey: stcpSecret.value })
    stcpAuth.value = ''
    stcpSecret.value = ''
    Object.assign(secrets, await invoke<SecretState>('site_agent_secret_state'))
    toast.success(tr('STCP 凭据已加密保存', 'STCP credentials encrypted and saved'))
    if (config.enabled && config.transport === 'lan-stcp') await restartUnifiedSidecar()
  } catch (error) {
    toast.error(tr('保存 STCP 凭据失败：', 'Failed to save STCP credentials: ') + message(error))
  } finally {
    credentialBusy.value = false
  }
}

onMounted(async () => {
  try {
    await loadNativeState()
    await refreshStatus()
    timer = window.setInterval(() => void refreshStatus(), 3000)
  } catch (error) {
    toast.error(tr('读取现场服务配置失败：', 'Failed to load Site Agent configuration: ') + message(error))
  } finally {
    loading.value = false
  }
})

onUnmounted(() => {
  if (timer !== null) window.clearInterval(timer)
})
</script>

<style scoped>
.site-agent-page{display:flex;flex-direction:column;gap:16px;max-width:1280px;margin:0 auto;padding:18px 20px 28px}.page-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:20px}.page-heading h2{font-size:22px;margin:2px 0 5px}.page-heading p{max-width:760px;color:var(--muted);font-size:13px}.eyebrow{font:700 10px/1 var(--font-mono);letter-spacing:.16em;color:var(--accent)!important}.runtime-badge{display:flex;align-items:center;gap:7px;padding:7px 11px;border:1px solid var(--border);border-radius:999px;background:var(--surface);font-size:12px;white-space:nowrap}.status-dot{width:8px;height:8px;border-radius:50%;background:var(--dim)}.runtime-badge.ready{color:var(--success)}.runtime-badge.ready .status-dot{background:var(--success);box-shadow:0 0 0 4px rgb(45 106 79 / 12%)}.runtime-badge.failed{color:var(--danger)}.runtime-badge.failed .status-dot{background:var(--danger)}.panel,.metric-card{border:1px solid var(--border);border-radius:8px;background:var(--surface)}.loading-state{padding:40px;text-align:center;color:var(--muted)}.overview-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.metric-card{display:grid;gap:4px;padding:14px 16px}.metric-card span,.metric-card small{font-size:11px;color:var(--muted)}.metric-card strong{overflow:hidden;text-overflow:ellipsis;font:600 14px/1.45 var(--font-mono)}.settings-layout{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(300px,.75fr);gap:14px}.panel{padding:17px}.panel-title{display:flex;justify-content:space-between;gap:18px;padding-bottom:14px;border-bottom:1px solid var(--border-subtle)}.panel h3{font-size:15px;margin:0 0 5px}.panel p{font-size:12px;color:var(--muted)}.switch-row{display:flex;align-items:center;gap:7px;font-size:12px;font-weight:600;white-space:nowrap}.form-grid,.stcp-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:16px}.form-grid label,.stcp-grid label,.secret-fields label{display:grid;gap:5px}.form-grid label>span,.stcp-grid label>span,.secret-fields label>span{font-size:11px;color:var(--muted)}input,select{width:100%;height:34px;padding:0 9px;border:1px solid var(--border);border-radius:5px;background:#fff;color:var(--fg);font:12px var(--font-body)}.check-field{display:flex!important;grid-column:1/-1;align-items:center;grid-template-columns:auto 1fr!important}.check-field input,.switch-row input{width:16px;height:16px}.actions{display:flex;gap:8px;margin-top:18px}.side-stack{display:grid;gap:14px;align-content:start}.credential-panel>.btn{margin-top:14px}.secret-fields{display:grid;gap:10px;margin-top:15px;padding-top:15px;border-top:1px solid var(--border-subtle)}.workflow-panel ol{display:grid;gap:9px;margin:13px 0 14px;padding-left:20px;color:var(--muted);font-size:12px}.workflow-panel code{display:block;overflow:auto;padding:9px;border-radius:5px;background:var(--bg);font:11px var(--font-mono)}@media(max-width:900px){.overview-grid{grid-template-columns:1fr}.settings-layout{grid-template-columns:1fr}}@media(max-width:620px){.page-heading,.panel-title{flex-direction:column}.form-grid,.stcp-grid{grid-template-columns:1fr}.site-agent-page{padding:14px 12px}}
</style>
