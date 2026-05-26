# Modbus Dashboard 组态提示词

> 本文档是 LLM（如 Claude）为任意 Modbus 设备生成自定义 Web 可视化仪表盘的指南。
> 生成的 HTML 文件保存到项目的 `.mklink/modbus_dashboard.html`，由 `python -m mklink modbus dashboard` 自动加载。

## 1. 服务器协议

### SSE 数据流 (`GET /stream`)

服务器通过 Server-Sent Events 推送实时数据。每条消息是 JSON 格式：

**寄存器快照**（每 1-5 秒）：
```json
{"_t": 1715345678.123, "registers": {"0": 245, "2": 35005, "21": 65, "102": 3}}
```
- `_t` — Unix 时间戳（秒）
- `registers` — `{地址: 原始 uint16/int16 值}` 字典

**写入/命令结果事件**：
```json
{"_event": "write_result", "addr": 200, "value": 1500, "ok": true, "name": "batt_volt_min", "_t": ...}
{"_event": "command_result", "action": "start", "ok": true, "write_addr": 100, "_t": ...}
```

### HTTP 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `GET /` | - | 加载 HTML（自定义或自动生成） |
| `GET /stream` | SSE | 实时数据流 |
| `GET /snapshot` | JSON | 最新寄存器值 |
| `GET /profile` | JSON | 完整 profile JSON |
| `GET /csrf-token` | JSON | `{"token": "..."}` |
| `POST /write` | JSON | 写入寄存器（需 CSRF） |
| `POST /command` | JSON | 执行命令（需 CSRF） |

### 写入寄存器
```json
POST /write
{"addr": 200, "value": 1500, "token": "CSRF_TOKEN"}
→ {"ok": true, "message": "Write successful"}
```
- 服务器校验：addr 必须在 profile 中标记为 `access: "rw"`，value 必须在 min/max 范围内

### 执行命令
```json
POST /command
{"action": "start", "token": "CSRF_TOKEN"}
→ {"ok": true, "message": "Command 'start' sent"}
```
- 带参数的命令：`{"action": "set_level", "params": {"level": 3}, "token": "..."}`

### CSRF 保护
所有 POST 请求必须包含 `token` 字段。token 在页面加载时注入到 JS 全局变量 `CSRF` 中。

## 2. JavaScript 全局变量

生成的 HTML 中会注入以下全局变量（由服务器模板或自动生成器提供）：

```javascript
var PROFILE = {/* 完整 profile JSON */};
var CSRF = "随机token字符串";
var MAX_POINTS = 500;  // 图表最大数据点
```

如果使用自定义 HTML（`.mklink/modbus_dashboard.html`），你需要自行从服务器获取这些值：

```javascript
// 获取 profile 和 CSRF token
fetch('/profile').then(r => r.json()).then(p => { PROFILE = p; init(); });
fetch('/csrf-token').then(r => r.json()).then(d => { CSRF = d.token; });
```

## 3. 数据格式

- 寄存器值为 **原始 uint16 或 int16**（无符号/有符号 16 位整数）
- 显示时需应用 `scale`（如 `scale: 0.1` 表示值需除以 10）
- int16 类型：如果 `raw >= 0x8000`，则实际值 = `raw - 0x10000`
- Profile 中每个寄存器的 `type` 字段标明类型：`"uint16"` 或 `"int16"`

```javascript
function rawToValue(raw, reg) {
  var scale = reg.scale || 1;
  var v = raw * scale;
  if (reg.type === 'int16' && raw >= 0x8000) v = (raw - 0x10000) * scale;
  return v;
}
```

## 4. CSS 暗色主题

使用 CSS 变量定义主题：

```css
:root {
  --bg: #1a1a2e; --surface: #16213e; --border: #2a2a4a;
  --accent: #00d4aa; --text: #e0e0e0; --dim: #8888aa;
  --danger: #ff6b6b; --warn: #ffd93d; --info: #4da6ff;
}
```

### 可复用 CSS 类

| 类名 | 用途 |
|------|------|
| `.metric-card` | 指标卡片（背景 + 边框 + 圆角） |
| `.ctrl-btn` | 控制按钮（边框 + hover 效果） |
| `.ctrl-btn.stop` | 危险操作按钮（红色边框） |
| `.status-pill` | 状态指示标签 |
| `.chip` | 图表变量选择器 |
| `.chip.active` | 已选中的变量 |
| `.param-table` | 参数编辑表格 |
| `.write-btn` | 写入按钮 |
| `.alarm-tile` | 报警状态网格 |
| `.alarm-tile.active` | 活动报警（红色背景） |
| `.badge` / `.badge-ok` / `.badge-err` | 状态徽章 |

## 5. 组件模式

### 指标卡片
```html
<div class="metric-card">
  <div class="label">电池电压</div>
  <div class="value" style="color:#4da6ff">
    <span id="mv-0">-</span><span class="unit">V</span>
  </div>
</div>
```
更新时只修改 `textContent`（防闪烁）。

### 状态栏
```html
<span id="state-pill" class="status-pill">空闲</span>
```
根据枚举值动态设置 `style.background` 和 `style.color`。

### 控制按钮
```html
<button class="ctrl-btn" data-cmd="start">启动</button>
```
点击时发送 `POST /command`。

### 报警网格
```html
<div class="alarm-tile" id="at-0">
  <div class="name">E01</div>
  <div class="desc">Heating Rod Fault</div>
</div>
```
活动时添加 `.active` class（红色），非活动时移除。

### 实时图表（Canvas）
```javascript
// 收集数据
chartFields[name] = {color, points: [{t, y}], visible: true};

// 绘制
ctx.beginPath();
points.forEach(p => ctx.lineTo(tx(p.t), ty(p.y)));
ctx.stroke();
```

### 参数编辑器
表格行：参数名 | 当前值 | 范围 | 默认值 | 编辑/写入/恢复按钮

## 6. HTML 加载方式与模板系统

仪表盘支持 3 种自定义方式，加载优先级由高到低：

### 方式 A：完全自定义 HTML（`.mklink/modbus_dashboard.html`）

完全自主控制 HTML/CSS/JS，需自行从服务器 API 获取数据：

```javascript
fetch('/profile').then(r => r.json()).then(p => { PROFILE = p; init(); });
fetch('/csrf-token').then(r => r.json()).then(d => { CSRF = d.token; });
```

### 方式 B：用户模板（`.mklink/modbus_dashboard_template.html`）— 推荐样式自定义

基于模板修改样式，动态数据（PROFILE/CSRF/MAX_POINTS）由服务器自动注入。拷贝内置模板后修改 CSS/布局即可：

```bash
cp <mklink安装目录>/mklink/modbus/_dashboard_template.html .mklink/modbus_dashboard_template.html
```

模板中必须保留 3 个占位符（服务器启动时 `str.replace()` 替换为实际值）：

| 占位符 | 替换为 | 用途 |
|--------|--------|------|
| `__PROFILE_JSON__` | 完整 profile JSON 对象 | `var PROFILE = __PROFILE_JSON__;` |
| `__CSRF_TOKEN__` | CSRF 令牌字符串 | `var CSRF = "__CSRF_TOKEN__";` |
| `__MAX_POINTS__` | 图表最大数据点数 | `var MAX_POINTS = __MAX_POINTS__;` |

### 方式 C：内置模板（默认）

使用 `_dashboard_template.html`，无需任何配置文件。

### Agent 行为指引

当用户要求"Modbus 可视化"时，Agent 应：
1. 如果用户未指定自定义方式，直接用内置模板启动仪表盘
2. 如果用户提到"自定义样式"或"调整布局"，告知以上 3 种方式，推荐方式 B
3. 如果用户需要全新设计（非基于内置模板），引导使用方式 A

## 7. 生成步骤

1. **分析项目** — 读取 `.mklink/modbus_profile.json`（或从 C 头文件提取寄存器定义）
2. **设计布局** — 确定哪些寄存器作为总览指标、哪些显示状态、哪些需要图表
3. **生成 profile JSON** — 创建 `.mklink/modbus_profile.json`，包含 `groups`、`commands`、`poll_groups`、`dashboard` 部分
4. **生成 HTML** — 根据用户需求选择方式 A（完全自定义）或方式 B（基于模板修改）
5. **测试** — 运行 `python -m mklink modbus dashboard --port COMx --slave N`

## 8. Profile JSON Schema

```json
{
  "schema_version": 1,
  "profile_id": "device-name",
  "slave": 1,
  "baudrate": 9600,
  "groups": [
    {
      "id": "sensors",
      "name": "Sensor Data",
      "poll_group": "fast",
      "registers": [
        {"addr": 0, "type": "uint16|int16", "name": "var_name", "label": "Display Name",
         "unit": "V", "scale": 0.1, "chart": true, "access": "ro|rw",
         "kind": "normal|enum|alarm|bitfield",
         "values": {"0": "Idle", "1": "Running"},
         "bits": [{"bit": 0, "name": "E01", "label": "Error desc", "severity": "stop|warn"}],
         "min": 100, "max": 3000, "default": 1800, "persistent": true}
      ]
    }
  ],
  "commands": [
    {"action": "start", "bit": 0, "confirm_required": true},
    {"action": "set_level", "write_addr": 110, "params": [{"name": "level", "min": 1, "max": 8}]}
  ],
  "poll_groups": {"fast": {"interval": 1.0}, "slow": {"interval": 5.0}},
  "dashboard": {
    "title": "设备控制面板",
    "overview_metrics": [{"addr": 0, "label": "电压", "unit": "V", "scale": 0.1, "color": "#4da6ff"}],
    "status_display": {"state_register": 102, "mode_register": 103, "level_register": 104},
    "control_buttons": [{"action": "start", "label": "启动", "css_class": ""}]
  }
}
```

## 9. 约束

- **独立 HTML** — 零外部依赖，所有 CSS/JS 内联
- **必须连接 SSE** — `new EventSource('/stream')` 接收实时数据
- **写入必须带 CSRF** — 从 `/csrf-token` 获取，POST 请求包含 `token` 字段
- **防闪烁** — DOM 只构建一次，后续只修改 `textContent` 和 CSS class
- **中文界面** — 所有用户可见文字使用中文
- **Canvas 图表** — 使用 `<canvas>` 元素绑制，支持 DPR 缩放
- **响应式** — 使用 `grid` + `flex` 布局，适配不同窗口大小
