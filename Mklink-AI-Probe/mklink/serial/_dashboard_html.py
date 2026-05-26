"""串口调试 Dashboard HTML 模板 — 统一设计规范，完整功能集。

功能：中英文切换、虚拟滚动、CRC 标记、字段单位、高亮规则、
自动应答管理、文件日志控制、Profile/端口信息展示、精确统计、
广播发送、文件发送。
"""
from __future__ import annotations
import json


def build_serial_dashboard_html(ports: list[str], profile_name: str | None = None) -> str:
    ports_json = json.dumps(ports)
    profile_display = json.dumps(profile_name or "")

    return f'''<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Serial Debug{(" — " + profile_name) if profile_name else ""}</title>
<link rel="stylesheet" href="/static/serial_dashboard.css?v=2">
</head>
<body>
<header>
  <h1 data-i18n="title">串口调试</h1>
  <div class="port-indicators" id="port-indicators"></div>
  <span id="sse-badge" class="badge badge-info" data-i18n="connecting">连接中...</span>
  <span id="logger-badge" class="badge badge-off" style="display:none"></span>
  <div class="header-actions">
    <button class="panel-btn" id="btn-lang-toggle" onclick="toggleSerialLang()">中/En</button>
    <button class="panel-btn" data-i18n="clear_log" onclick="vlog.clear()">清除日志</button>
  </div>
</header>

<div id="profile-bar"></div>

<div id="toolbar">
  <div class="mode-toggle">
    <button class="mode-btn active" id="btn-ascii" onclick="setMode('ascii')" data-i18n="ascii">ASCII</button>
    <button class="mode-btn" id="btn-hex" onclick="setMode('hex')" data-i18n="hex">HEX</button>
  </div>
  <div class="stats-bar">
    <span>RX: <span id="stat-rx">0</span></span>
    <span>TX: <span id="stat-tx">0</span></span>
    <span id="stat-rx-bytes"></span>
    <span id="stat-tx-bytes"></span>
    <span><span id="stat-bps">0</span> B/s</span>
  </div>
</div>

<div class="main-area">
  <div class="data-log" id="data-log"></div>
  <div class="right-panel">

    <!-- Send -->
    <div class="panel-section">
      <h3 data-i18n="send">发送</h3>
      <div class="send-row">
        <select class="port-select" id="send-port"></select>
        <input class="send-input" id="send-input" data-i18n-placeholder="send_placeholder" placeholder="输入要发送的数据..." onkeydown="if(event.key==='Enter')doSend()">
        <button class="btn" onclick="doSend()" data-i18n="send">发送</button>
        <button class="btn secondary" onclick="doSendAll()" data-i18n="send_all">广播</button>
      </div>
      <label class="hex-check">
        <input type="checkbox" id="send-hex"> <span data-i18n="send_as_hex">以 HEX 发送</span>
      </label>
      <div class="send-file-row">
        <input class="send-input" id="file-path-input" data-i18n-placeholder="file_path_placeholder" placeholder="文件路径...">
        <button class="btn secondary" onclick="doSendFile()" data-i18n="send_file">发送文件</button>
      </div>
    </div>

    <!-- Command Queue -->
    <div class="panel-section">
      <h3 data-i18n="cmd_queue">命令队列</h3>
      <div class="cmd-list" id="cmd-list"></div>
      <div class="send-row" style="margin-top:8px">
        <input class="send-input" id="cmd-input" data-i18n-placeholder="add_cmd_placeholder" placeholder="添加命令..." onkeydown="if(event.key==='Enter')addCmd()">
        <button class="btn secondary" onclick="addCmd()" data-i18n="add">添加</button>
      </div>
      <div class="auto-send-row">
        <span data-i18n="auto_interval">自动发送间隔</span>
        <input class="interval-input" id="auto-interval" value="1000" placeholder="ms">
        <span>ms</span>
        <button class="btn secondary" id="auto-btn" onclick="toggleAutoSend()" data-i18n="start">开始</button>
      </div>
    </div>

    <!-- Filter -->
    <div class="panel-section">
      <h3 data-i18n="filter">过滤</h3>
      <div class="filter-row">
        <input class="filter-input" id="filter-input" data-i18n-placeholder="filter_placeholder" placeholder="正则过滤..." onkeydown="if(event.key==='Enter')applyFilter()">
        <button class="btn secondary" onclick="applyFilter()" data-i18n="apply">应用</button>
        <button class="btn danger" onclick="clearFilter()" data-i18n="clear">清除</button>
      </div>
    </div>

    <!-- Auto-Reply -->
    <div class="panel-section">
      <h3 data-i18n="auto_reply">自动应答规则</h3>
      <div class="auto-reply-list" id="auto-reply-list"></div>
      <div class="auto-reply-add">
        <div class="send-row">
          <input class="send-input" id="rule-match" data-i18n-placeholder="match_placeholder" placeholder="匹配 (HEX/正则/包含)">
          <input class="send-input" id="rule-reply" data-i18n-placeholder="reply_placeholder" placeholder="应答 (HEX)">
          <button class="btn secondary" onclick="addRule()" data-i18n="add_rule">添加规则</button>
        </div>
      </div>
    </div>

    <!-- Logger -->
    <div class="panel-section">
      <h3 data-i18n="logger">文件日志</h3>
      <div class="logger-controls">
        <input class="send-input" id="log-path" data-i18n-placeholder="log_path_placeholder" placeholder="日志路径..." value="serial_log.txt" style="width:120px">
        <select class="port-select" id="log-format">
          <option value="txt">TXT</option>
          <option value="csv">CSV</option>
        </select>
        <button class="btn success" id="log-btn" onclick="toggleLogger()" data-i18n="log_start">开始记录</button>
      </div>
      <div class="logger-status" id="logger-status" data-i18n="log_inactive">未记录</div>
    </div>

    <!-- Port Info -->
    <div class="panel-section" id="port-info-section" style="display:none">
      <h3 data-i18n="port_info">端口信息</h3>
      <div id="port-info-list"></div>
    </div>

  </div>
</div>

<script src="/static/serial_dashboard.js?v=2"></script>
<script>
(function() {{
  var PORTS = {ports_json};
  var displayMode = 'ascii';
  var filterRegex = null;
  var commands = JSON.parse(localStorage.getItem('serial_cmds') || '[]');
  var autoSendTimer = null, autoSendIdx = 0;
  var loggerActive = false;

  var logContainer = document.getElementById('data-log');
  var sendPort = document.getElementById('send-port');

  // ─── Virtual Log with CRC + unit + highlight ───
  function renderLogLine(evt) {{
    if (evt.type !== 'data') return null;
    var dir = evt.direction || '';
    var dataStr = displayMode === 'hex' ? (evt.raw_hex || '') : (evt.ascii || evt.raw_hex || '');
    var fieldsHtml = '';
    if (evt.fields && Object.keys(evt.fields).length > 0) {{
      var parts = [];
      for (var k in evt.fields) {{
        var f = evt.fields[k];
        var val = (typeof f === 'object') ? f.value : f;
        var unit = (typeof f === 'object' && f.unit) ? '<span class="unit">' + escapeHtml(f.unit) + '</span>' : '';
        parts.push(escapeHtml(k) + '=' + escapeHtml(String(val)) + unit);
      }}
      fieldsHtml = '<span class="decoded">' + parts.join(' ') + '</span>';
    }}
    var crcHtml = '';
    if (evt.crc_valid === true) crcHtml = '<span class="crc-badge ok">' + st('crc_ok') + '</span>';
    else if (evt.crc_valid === false) crcHtml = '<span class="crc-badge fail">' + st('crc_fail') + '</span>';

    var div = document.createElement('div');
    var cls = 'log-line ' + dir.toLowerCase();
    if (evt.crc_valid === false) cls += ' crc-error';
    var hlCls = getHL(dataStr + ' ' + fieldsHtml);
    if (hlCls) cls += ' ' + hlCls;
    div.className = cls;
    div.innerHTML = '<span class="ts">' + (evt.timestamp || '') + '</span>'
      + '<span class="port-tag">' + escapeHtml(evt.port || '') + '</span>'
      + '<span class="direction ' + dir.toLowerCase() + '">' + dir + '</span>'
      + '<span class="data">' + escapeHtml(dataStr) + '</span>'
      + fieldsHtml + crcHtml;
    return div;
  }}

  var vlog = new VirtualLog(logContainer, {{ lineHeight: 22, maxLines: 5000, renderFn: renderLogLine }});
  window.vlog = vlog;

  // ─── Ports ───
  function initPorts() {{
    var ind = document.getElementById('port-indicators');
    ind.innerHTML = PORTS.map(function(p) {{
      return '<div class="port-indicator"><span class="port-dot closed" id="dot-' + p + '"></span>' + p + '</div>';
    }}).join('');
    sendPort.innerHTML = PORTS.map(function(p) {{ return '<option value="' + p + '">' + p + '</option>'; }}).join('');
  }}

  // ─── Mode ───
  window.setMode = function(mode) {{
    displayMode = mode;
    document.getElementById('btn-ascii').classList.toggle('active', mode === 'ascii');
    document.getElementById('btn-hex').classList.toggle('active', mode === 'hex');
    vlog.rerender();
  }};

  // ─── SSE ───
  function connectSSE() {{
    var es = new EventSource('/events');
    es.onopen = function() {{
      var b = document.getElementById('sse-badge'); b.textContent = st('live'); b.className = 'badge badge-ok';
    }};
    es.onmessage = function(e) {{
      var d = JSON.parse(e.data);
      if (d.type === 'status') {{ updatePortStatus(d.ports); }}
      else if (d.type === 'data') {{ vlog.push(d); }}
    }};
    es.onerror = function() {{
      var b = document.getElementById('sse-badge'); b.textContent = st('disconnected'); b.className = 'badge badge-warn';
      setTimeout(connectSSE, 3000);
    }};
  }}
  function updatePortStatus(ports) {{
    for (var p in ports) {{ var dot = document.getElementById('dot-' + p); if (dot) dot.className = 'port-dot ' + ports[p]; }}
  }}

  // ─── Stats polling (accurate from server) ───
  function pollStats() {{
    fetch('/status').then(function(r){{ return r.json(); }}).then(function(d) {{
      if (d.stats) {{
        document.getElementById('stat-rx').textContent = d.stats.rx_count;
        document.getElementById('stat-tx').textContent = d.stats.tx_count;
        document.getElementById('stat-rx-bytes').textContent = fmtBytes(d.stats.rx_bytes);
        document.getElementById('stat-tx-bytes').textContent = fmtBytes(d.stats.tx_bytes);
        document.getElementById('stat-bps').textContent = Math.round(d.stats.bytes_per_sec);
      }}
      if (d.ports) updatePortStatus(d.ports);
    }}).catch(function(){{}});
  }}
  setInterval(pollStats, 1000);

  // ─── Send ───
  window.doSend = function() {{
    var port = sendPort.value, data = document.getElementById('send-input').value;
    var hex = document.getElementById('send-hex').checked;
    if (!data) return;
    fetch('/send', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{port:port,data:data,hex:hex}}) }});
    document.getElementById('send-input').value = '';
  }};
  window.doSendAll = function() {{
    var data = document.getElementById('send-input').value;
    var hex = document.getElementById('send-hex').checked;
    if (!data) return;
    fetch('/send-all', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{data:data,hex:hex}}) }});
    document.getElementById('send-input').value = '';
  }};
  window.doSendFile = function() {{
    var port = sendPort.value, path = document.getElementById('file-path-input').value;
    var hex = document.getElementById('send-hex').checked;
    if (!path) return;
    fetch('/send-file', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{port:port,path:path,hex:hex}}) }});
  }};

  // ─── Command Queue ───
  function renderCmds() {{
    var list = document.getElementById('cmd-list');
    list.innerHTML = commands.map(function(c, i) {{
      return '<div class="cmd-item"><span class="cmd-text">' + escapeHtml(c) + '</span>'
        + '<button class="btn secondary" onclick="sendCmd(' + i + ')">' + st('send') + '</button>'
        + '<button class="btn danger" onclick="delCmd(' + i + ')">X</button></div>';
    }}).join('');
  }}
  window.addCmd = function() {{
    var input = document.getElementById('cmd-input'), val = input.value.trim();
    if (!val) return;
    commands.push(val); localStorage.setItem('serial_cmds', JSON.stringify(commands));
    input.value = ''; renderCmds();
  }};
  window.delCmd = function(i) {{ commands.splice(i,1); localStorage.setItem('serial_cmds', JSON.stringify(commands)); renderCmds(); }};
  window.sendCmd = function(i) {{
    var port = sendPort.value, data = commands[i], hex = document.getElementById('send-hex').checked;
    if (!data) return;
    fetch('/send', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{port:port,data:data,hex:hex}}) }});
  }};
  window.toggleAutoSend = function() {{
    var btn = document.getElementById('auto-btn');
    if (autoSendTimer) {{ clearInterval(autoSendTimer); autoSendTimer = null; btn.textContent = st('start'); return; }}
    var interval = parseInt(document.getElementById('auto-interval').value) || 1000;
    autoSendIdx = 0;
    autoSendTimer = setInterval(function() {{
      if (commands.length === 0) return;
      window.sendCmd(autoSendIdx % commands.length); autoSendIdx++;
    }}, interval);
    btn.textContent = st('stop');
  }};

  // ─── Filter ───
  window.applyFilter = function() {{
    var val = document.getElementById('filter-input').value;
    try {{ filterRegex = val ? new RegExp(val, 'i') : null; }} catch(e) {{ filterRegex = null; }}
    vlog.setFilter(filterRegex ? function(evt) {{
      if (evt.type !== 'data') return false;
      var s = displayMode === 'hex' ? (evt.raw_hex||'') : (evt.ascii||evt.raw_hex||'');
      if (filterRegex.test(s)) return true;
      if (evt.fields) {{ var fstr = JSON.stringify(evt.fields); if (filterRegex.test(fstr)) return true; }}
      return false;
    }} : null);
  }};
  window.clearFilter = function() {{ document.getElementById('filter-input').value = ''; filterRegex = null; vlog.setFilter(null); }};

  // ─── Auto-Reply ───
  function loadRules() {{
    fetch('/auto-reply').then(function(r){{ return r.json(); }}).then(function(d) {{
      var list = document.getElementById('auto-reply-list');
      if (!d.rules || d.rules.length === 0) {{ list.innerHTML = '<em data-i18n="no_rules">' + st('no_rules') + '</em>'; return; }}
      list.innerHTML = d.rules.map(function(r, i) {{
        var match = r.match_hex || r.match_regex || r.match_contains || '?';
        var reply = r.reply_hex || r.reply_ascii || '?';
        var desc = r.description ? ' <span style="color:var(--dim)">(' + escapeHtml(r.description) + ')</span>' : '';
        return '<div class="auto-reply-item">'
          + '<span class="rule-desc"><span class="rule-match">' + escapeHtml(match) + '</span> → <span class="rule-reply">' + escapeHtml(reply) + '</span>' + desc + '</span>'
          + (r.delay ? ' <span style="color:var(--dim);font-size:10px">' + r.delay + 's</span>' : '')
          + '<button class="btn danger" style="padding:2px 6px;font-size:10px" onclick="removeRule(' + i + ')">' + st('remove') + '</button></div>';
      }}).join('');
    }}).catch(function(){{}});
  }}
  window.addRule = function() {{
    var match = document.getElementById('rule-match').value.trim();
    var reply = document.getElementById('rule-reply').value.trim();
    if (!match || !reply) return;
    var rule = {{ match_hex: match, reply_hex: reply }};
    fetch('/auto-reply', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{action:'add',rule:rule}}) }})
      .then(function(){{ loadRules(); document.getElementById('rule-match').value=''; document.getElementById('rule-reply').value=''; }});
  }};
  window.removeRule = function(i) {{
    fetch('/auto-reply', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{action:'remove',index:i}}) }})
      .then(function(){{ loadRules(); }});
  }};

  // ─── Logger ───
  window.toggleLogger = function() {{
    var btn = document.getElementById('log-btn');
    var statusEl = document.getElementById('logger-status');
    var badge = document.getElementById('logger-badge');
    if (!loggerActive) {{
      var path = document.getElementById('log-path').value || 'serial_log.txt';
      var fmt = document.getElementById('log-format').value;
      fetch('/logger', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{action:'start',path:path,format:fmt}}) }})
        .then(function(r){{ return r.json(); }}).then(function(d) {{
          if (d.ok) {{
            loggerActive = true; btn.textContent = st('log_stop'); btn.className = 'btn danger';
            statusEl.textContent = st('log_active') + ' → ' + d.path; statusEl.className = 'logger-status active';
            badge.style.display = ''; badge.textContent = st('log_active'); badge.className = 'badge badge-ok';
          }}
        }});
    }} else {{
      fetch('/logger', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{action:'stop'}}) }})
        .then(function() {{
          loggerActive = false; btn.textContent = st('log_start'); btn.className = 'btn success';
          statusEl.textContent = st('log_inactive'); statusEl.className = 'logger-status';
          badge.style.display = 'none';
        }});
    }}
  }};

  // ─── Profile + Port Info ───
  function loadProfile() {{
    fetch('/profile').then(function(r){{ return r.json(); }}).then(function(d) {{
      if (!d.profile) return;
      var p = d.profile;
      var bar = document.getElementById('profile-bar');
      var items = [];
      if (p.name) items.push('<span class="profile-item"><span class="profile-label">' + st('profile') + ':</span><span class="profile-value">' + escapeHtml(p.name) + '</span></span>');
      if (p.version) items.push('<span class="profile-item"><span class="profile-label">' + st('version') + ':</span><span class="profile-value">' + escapeHtml(p.version) + '</span></span>');
      if (p.frame) {{
        if (p.frame.header) items.push('<span class="profile-item"><span class="profile-label">' + st('frame_header') + ':</span><span class="profile-value">' + escapeHtml(p.frame.header) + '</span></span>');
        if (p.frame.tail) items.push('<span class="profile-item"><span class="profile-label">' + st('frame_tail') + ':</span><span class="profile-value">' + escapeHtml(p.frame.tail) + '</span></span>');
        if (p.frame.crc_algorithm) items.push('<span class="profile-item"><span class="profile-label">' + st('crc') + ':</span><span class="profile-value">' + escapeHtml(p.frame.crc_algorithm) + '</span></span>');
      }}
      bar.innerHTML = items.join('');

      // Port info
      if (p.ports && p.ports.length > 0) {{
        var sec = document.getElementById('port-info-section'); sec.style.display = '';
        var list = document.getElementById('port-info-list');
        list.innerHTML = p.ports.map(function(pt) {{
          return '<div class="port-info-item">'
            + '<strong>' + escapeHtml(pt.port) + '</strong> '
            + st('baud') + ':' + pt.baudrate + ' '
            + st('databits') + ':' + pt.databits + ' '
            + st('stopbits') + ':' + pt.stopbits + ' '
            + st('parity') + ':' + pt.parity
            + '</div>';
        }}).join('');
      }}
    }}).catch(function(){{}});
  }}

  // ─── Init ───
  initPorts();
  renderCmds();
  connectSSE();
  loadRules();
  loadProfile();
  applySerialI18n();
}})();
</script>
</body>
</html>'''
