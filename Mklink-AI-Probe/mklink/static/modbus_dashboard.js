// i18n for Modbus dashboard
var MB_I18N = {
  zh: {
    mb_online: '在线', mb_offline: '离线', mb_paused: '已暂停', mb_reconnecting: '重连中...',
    mb_pause: '暂停', mb_resume: '继续',
    mb_overview: '总览', mb_charts: '图表', mb_parameters: '参数', mb_alarms: '报警', mb_debug: '调试',
    mb_search_params: '搜索参数...', mb_alarm_history: '报警历史',
    mb_manual_read: '手动读取', mb_manual_write: '手动写入', mb_command: '命令',
    mb_read: '读取', mb_write: '写入', mb_send: '发送',
    mb_server_shutdown: '服务器已关闭', mb_server_stopped_msg: 'Modbus 仪表盘服务已停止。',
    mb_close_tab_msg: '您可以关闭此页面。',
    mb_poll_unit: '次'
  },
  en: {
    mb_online: 'Online', mb_offline: 'Offline', mb_paused: 'Paused', mb_reconnecting: 'Reconnecting...',
    mb_pause: 'Pause', mb_resume: 'Resume',
    mb_overview: 'Overview', mb_charts: 'Charts', mb_parameters: 'Parameters', mb_alarms: 'Alarms', mb_debug: 'Debug',
    mb_search_params: 'Search parameters...', mb_alarm_history: 'Alarm History',
    mb_manual_read: 'Manual Read', mb_manual_write: 'Manual Write', mb_command: 'Command',
    mb_read: 'Read', mb_write: 'Write', mb_send: 'Send',
    mb_server_shutdown: 'Server Shut Down', mb_server_stopped_msg: 'Modbus dashboard service has been stopped.',
    mb_close_tab_msg: 'You can close this tab.',
    mb_poll_unit: 'polls'
  }
};
var mbLang = (typeof CONFIG !== "undefined" && CONFIG.lang) || "zh";

function mt(key) { return (MB_I18N[mbLang] && MB_I18N[mbLang][key]) || MB_I18N.zh[key] || key; }
function mbApplyI18n() {
  document.querySelectorAll('[data-i18n]').forEach(function(el) {
    var v = mt(el.getAttribute('data-i18n'));
    if (v) el.textContent = v;
  });
  document.querySelectorAll('[data-i18n-placeholder]').forEach(function(el) {
    var v = mt(el.getAttribute('data-i18n-placeholder'));
    if (v) el.placeholder = v;
  });
  var langBtn = document.getElementById('lang-btn');
  if (langBtn) langBtn.textContent = mbLang === 'zh' ? '中/En' : 'En/中';
}
function mbSetLang(lang) {
  mbLang = lang;
  mbApplyI18n();
  try { localStorage.setItem('mklink_lang', lang); } catch(e) {}
  fetch('/api/lang', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({lang:lang}) }).catch(function(){});
}
document.addEventListener('DOMContentLoaded', function() {
  mbApplyI18n();
  var langBtn = document.getElementById('lang-btn');
  if (langBtn) langBtn.addEventListener('click', function() { mbSetLang(mbLang === 'zh' ? 'en' : 'zh'); });
});

var PROFILE = CONFIG.profile;
var CSRF = CONFIG.csrf;
var MAX_POINTS = CONFIG.maxPoints;
var COLORS = ['#c96442','#3898ec','#2d6a4f','#b58a1b','#7c5cbf','#d97757','#5e8a6e','#a07040','#5a7ea0','#8a6a5e'];
var GRID_COLOR = '#e8e6dc'; var TEXT_DIM = '#87867f';
var DB = PROFILE.dashboard || {};
var addrMap = {};
var chartFields = {};
var colorIdx = 0;
var paused = false;
var pollCount = 0;
var latestRegs = {};
var metricsInited = false;
var alarmsInited = false;
PROFILE.groups.forEach(function(g) {
  g.registers.forEach(function(r) { addrMap[r.addr] = r; });
});
if (DB.title) document.getElementById('page-title').textContent = DB.title;
document.querySelectorAll('.tab').forEach(function(t) {
  t.addEventListener('click', function() {
    document.querySelectorAll('.tab').forEach(function(x) { x.classList.remove('active'); });
    document.querySelectorAll('.tab-content').forEach(function(x) { x.classList.remove('active'); });
    t.classList.add('active');
    document.getElementById('tab-' + t.dataset.tab).classList.add('active');
    if (t.dataset.tab === 'charts') resizeCanvas();
  });
});
document.getElementById('pause-btn').addEventListener('click', function() {
  paused = !paused;
  this.textContent = paused ? mt('mb_resume') : mt('mb_pause');
  var s = document.getElementById('conn-status');
  s.textContent = paused ? mt('mb_paused') : mt('mb_online');
  s.className = paused ? 'badge badge-warn' : 'badge badge-ok';
});
document.addEventListener('keydown', function(e) {
  if (e.code === 'Space' && e.target.tagName !== 'INPUT') {
    e.preventDefault();
    document.getElementById('pause-btn').click();
  }
});
var es = new EventSource('/stream');
es.onmessage = function(e) {
  try { processData(JSON.parse(e.data)); } catch(err) { console.error(err); }
};
es.onerror = function() {
  if (es.readyState === EventSource.CLOSED) {
    document.getElementById('shutdown-overlay').classList.add('visible');
  } else {
    var s = document.getElementById('conn-status');
    s.textContent = mt('mb_reconnecting');
    s.className = 'badge badge-err';
  }
};
es.onopen = function() {
  if (!paused) {
    var s = document.getElementById('conn-status');
    s.textContent = mt('mb_online');
    s.className = 'badge badge-ok';
  }
};
var updatePending = false;
function processData(data) {
  if (data._event) { handleEvent(data); return; }
  if (paused) return;
  pollCount++;
  document.getElementById('poll-count').textContent = pollCount + ' ' + mt('mb_poll_unit');
  var regs = data.registers || {};
  latestRegs = regs;
  updateOverview(regs);
  updateMetrics(regs);
  updateAlarms(regs);
  updateParams(regs);
  updateChartFields(regs, data._t);
  if (!updatePending) {
    updatePending = true;
    requestAnimationFrame(function() {
      try { drawChart(); updateChartSelector(); } catch(err) { console.error(err); }
      updatePending = false;
    });
  } else if (data._event === 'debug_result') {
    appendDebugLog(data.ok ? 'EVENT OK ' + JSON.stringify(data) : 'EVENT ERR ' + JSON.stringify(data));
  }
}
function handleEvent(data) {
  if (data._event === 'shutdown') {
    es.close();
    document.getElementById('shutdown-overlay').classList.add('visible');
    var s = document.getElementById('conn-status');
    s.textContent = '已停止';
    s.className = 'badge badge-err';
    return;
  }
  if (data._event === 'write_result') {
    var msg = data.ok ? '已写入 ' + data.name + ' = ' + data.value : '写入失败: ' + data.error;
    showFooter(msg, data.ok);
  } else if (data._event === 'command_result') {
    var msg = data.ok ? '命令已发送: ' + data.action : '命令失败: ' + data.error;
    showFooter(msg, data.ok);
  }
}
function showFooter(msg, ok) {
  var f = document.getElementById('footer-bar');
  f.textContent = msg;
  f.style.color = ok ? 'var(--accent)' : 'var(--danger)';
  setTimeout(function() { f.style.color = ''; }, 3000);
}
var ENUM_REGS = [];
var SD = DB.status_display || {};
(function() {
  PROFILE.groups.forEach(function(g) {
    g.registers.forEach(function(r) {
      if (r.kind === 'enum') ENUM_REGS.push({addr: r.addr, reg: r});
    });
  });
})();
function buildStatusBar() {
  var bar = document.getElementById('status-bar');
  var html = '';
  if (SD.state_register) {
    html += '<span id="state-pill" class="status-pill" style="background:#e6f2ea;color:#2d6a4f">--</span>';
  }
  ENUM_REGS.forEach(function(e) {
    if (e.addr === SD.state_register) return;
    html += '<span id="enum-' + e.addr + '" style="font-size:12px;color:var(--muted)">' + e.reg.label + ': --</span>';
  });
  if (SD.level_register) {
    html += '<span id="level-label" style="font-size:12px;color:var(--muted)">档位: --</span>';
  }
  if (SD.hours_register) {
    html += '<span id="hours-label" style="font-size:12px;color:var(--muted)">运行小时: --</span>';
  }
  bar.innerHTML = html;
}
var STATE_COLORS = {
  'Idle':'#e6f2ea,#2d6a4f', 'Preheating':'#f5f0e1,#b58a1b', 'Ignition':'#f5ece6,#c96442',
  'Running':'#e6f2ea,#2d6a4f', 'Shutdown':'#e6eef5,#3898ec', 'Fault':'#f5e6e6,#b53333',
  'Manual':'#e6eef5,#3898ec', 'Auto':'#e6f2ea,#2d6a4f'
};
function updateOverview(regs) {
  if (SD.state_register) {
    var sv = regs[SD.state_register];
    var pill = document.getElementById('state-pill');
    if (pill && sv !== undefined) {
      var sreg = addrMap[SD.state_register];
      var sname = sreg && sreg.values ? (sreg.values[String(sv)] || 'Unknown(' + sv + ')') : String(sv);
      pill.textContent = sname;
      var cols = STATE_COLORS[sname] || '#f0eee6,#87867f';
      pill.style.background = cols.split(',')[0];
      pill.style.color = cols.split(',')[1];
    }
  }
  ENUM_REGS.forEach(function(e) {
    if (e.addr === SD.state_register) return;
    var el = document.getElementById('enum-' + e.addr);
    if (!el) return;
    var raw = regs[e.addr];
    if (raw === undefined) return;
    var vname = e.reg.values ? (e.reg.values[String(raw)] || 'Unknown(' + raw + ')') : String(raw);
    el.textContent = e.reg.label + ': ' + vname;
  });
  if (SD.level_register) {
    var lv = regs[SD.level_register];
    var el = document.getElementById('level-label');
    if (el && lv !== undefined) el.textContent = '档位: ' + lv;
  }
  if (SD.hours_register) {
    var hr = regs[SD.hours_register];
    var el = document.getElementById('hours-label');
    if (el && hr !== undefined) el.textContent = '运行小时: ' + hr;
  }
}
function buildControlButtons() {
  var panel = document.getElementById('control-panel');
  var buttons = DB.control_buttons;
  if (!buttons || buttons.length === 0) {
    buttons = (PROFILE.commands || []).map(function(c) {
      return {action: c.action, label: c.label || c.action, css_class: ''};
    });
  }
  buttons.forEach(function(b) {
    var el = document.createElement('button');
    el.className = 'ctrl-btn ' + (b.css_class || '');
    el.dataset.cmd = b.action;
    el.textContent = b.label;
    el.addEventListener('click', function() {
      var cmd = (PROFILE.commands || []).find(function(c){ return c.action === b.action; });
      if (cmd && cmd.confirm_required) {
        if (!confirm('确认执行: ' + b.label + '?')) return;
      }
      var body = {action: b.action, token: CSRF};
      if (cmd && cmd.params) {
        cmd.params.forEach(function(p) {
          var val = prompt(p.name + ' (' + (p.min || '') + '~' + (p.max || '') + '):');
          if (val === null) return;
          if (!body.params) body.params = {};
          body.params[p.name] = parseInt(val);
        });
        if (body.params === undefined) return;
      }
      fetch('/command', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify(body)
      }).then(function(r){ return r.json(); }).then(function(d) {
        if(!d.ok) showFooter(d.error || '命令失败', false);
        else showFooter('命令已发送: ' + b.label, true);
      }).catch(function(e){ showFooter('网络错误: '+e, false); });
    });
    panel.appendChild(el);
  });
}
var METRIC_DEFS = DB.overview_metrics || (function() {
  var defs = [];
  PROFILE.groups.forEach(function(g) {
    g.registers.forEach(function(r) {
      if (r.chart && !r.hidden && defs.length < 6) {
        defs.push({addr:r.addr, label:r.label, unit:r.unit||'', scale:r.scale||1, color:COLORS[defs.length % COLORS.length], signed:r.type==='int16'});
      }
    });
  });
  return defs;
})();
function initMetrics() {
  var grid = document.getElementById('metrics-grid');
  var html = '';
  METRIC_DEFS.forEach(function(d) {
    html += '<div class="metric-card">'
      + '<div class="label">' + d.label + '</div>'
      + '<div class="value" style="color:' + d.color + '"><span id="mv-' + d.addr + '">-</span><span class="unit">' + d.unit + '</span></div>'
      + '</div>';
  });
  grid.innerHTML = html;
  metricsInited = true;
}
function updateMetrics(regs) {
  if (!metricsInited) initMetrics();
  METRIC_DEFS.forEach(function(d) {
    var raw = regs[d.addr];
    var el = document.getElementById('mv-' + d.addr);
    if (!el) return;
    if (raw === undefined) return;
    var v = raw * (d.scale || 1);
    if (d.signed && raw >= 0x8000) v = (raw - 0x10000) * (d.scale || 1);
    el.textContent = v.toFixed((d.scale || 1) < 1 ? 1 : 0);
  });
}
var ALARM_REGS = [];
var ALARM_DEFS = [];
(function() {
  PROFILE.groups.forEach(function(g) {
    g.registers.forEach(function(r) {
      if (r.kind === 'alarm' && r.bits) {
        ALARM_REGS.push(r);
        r.bits.forEach(function(b) {
          ALARM_DEFS.push({reg_addr: r.addr, bit: b.bit, name: b.name, label: b.label});
        });
      }
    });
  });
})();
function initAlarms() {
  var grid = document.getElementById('alarm-grid');
  var html = '';
  ALARM_DEFS.forEach(function(a, i) {
    html += '<div class="alarm-tile" id="at-' + i + '">'
      + '<div class="name">' + a.name + '</div>'
      + '<div class="desc">' + a.label + '</div></div>';
  });
  grid.innerHTML = html;
  alarmsInited = true;
}
function updateAlarms(regs) {
  if (!alarmsInited) initAlarms();
  var alarmVals = {};
  ALARM_REGS.forEach(function(r) { alarmVals[r.addr] = regs[r.addr] || 0; });
  var activeNames = [];
  ALARM_DEFS.forEach(function(a, i) {
    var val = alarmVals[a.reg_addr] || 0;
    var active = !!(val & (1 << a.bit));
    var tile = document.getElementById('at-' + i);
    if (!tile) return;
    if (active) {
      if (!tile.classList.contains('active')) tile.classList.add('active');
      activeNames.push(a.name + ': ' + a.label);
    } else {
      if (tile.classList.contains('active')) tile.classList.remove('active');
    }
  });
  var banner = document.getElementById('alarm-banner');
  if (activeNames.length > 0) {
    banner.textContent = '当前报警: ' + activeNames.join(' | ');
    if (!banner.classList.contains('visible')) banner.classList.add('visible');
  } else {
    if (banner.classList.contains('visible')) banner.classList.remove('visible');
  }
}
var canvas = document.getElementById('chart');
var ctx = canvas.getContext('2d');
var tooltip = document.getElementById('tooltip');
var chartWrap = document.getElementById('chart-wrap');
function resizeCanvas() {
  var r = chartWrap.getBoundingClientRect();
  var w = r.width || 100, h = r.height || 100;
  if (w <= 0 || h <= 0) return false;
  var dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.round(w * dpr));
  canvas.height = Math.max(1, Math.round(h * dpr));
  canvas.style.width = w + 'px'; canvas.style.height = h + 'px';
  ctx.setTransform(1,0,0,1,0,0); ctx.scale(dpr, dpr);
  return true;
}
window.addEventListener('resize', function() { if (document.querySelector('[data-tab=charts]').classList.contains('active')) resizeCanvas(); });
function updateChartFields(regs, t) {
  PROFILE.groups.forEach(function(g) {
    g.registers.forEach(function(r) {
      if (!r.chart) return;
      var raw = regs[r.addr];
      if (raw === undefined) return;
      var scale = r.scale || 1;
      var v = raw * scale;
      if (r.type === 'int16' && raw >= 0x8000) v = (raw - 0x10000) * scale;
      if (!chartFields[r.name]) {
        chartFields[r.name] = {color: COLORS[colorIdx % COLORS.length], points:[], visible:true, min:Infinity, max:-Infinity, sum:0, count:0};
        colorIdx++;
      }
      var m = chartFields[r.name];
      v = Number(v); if (!isFinite(v)) return;
      m.points.push({t:t, y:v});
      if (m.points.length > MAX_POINTS) {
        m.points.shift();
        m.min=Infinity; m.max=-Infinity; m.sum=0; m.count=0;
        m.points.forEach(function(p) { m.min=Math.min(m.min,p.y); m.max=Math.max(m.max,p.y); m.sum+=p.y; m.count++; });
      } else {
        m.min=Math.min(m.min,v); m.max=Math.max(m.max,v); m.sum+=v; m.count++;
      }
    });
  });
}
function drawChart() {
  if (!resizeCanvas()) return;
  var W = canvas.clientWidth, H = canvas.clientHeight;
  if (W <= 0 || H <= 0) return;
  ctx.clearRect(0, 0, W, H);
  var ml=56, mr=20, mt=16, mb=36;
  var pw=W-ml-mr, ph=H-mt-mb;
  if (pw<=0||ph<=0) return;
  var yMin=Infinity, yMax=-Infinity, hasData=false;
  var names = Object.keys(chartFields).sort();
  names.forEach(function(n) {
    var m=chartFields[n]; if(!m.visible||m.points.length<2) return;
    hasData=true; yMin=Math.min(yMin,m.min); yMax=Math.max(yMax,m.max);
  });
  if(!hasData) return;
  var pad=(yMax-yMin)*0.1||1; yMin-=pad; yMax+=pad;
  var tMax=0, tMin=Infinity;
  names.forEach(function(n) {
    var pts=chartFields[n].points;
    if(pts.length>0) { tMax=Math.max(tMax,pts[pts.length-1].t); tMin=Math.min(tMin,pts[0].t); }
  });
  if(tMax-tMin<1) tMin=tMax-1;
  function tx(v){ return ml+(v-tMin)/(tMax-tMin)*pw; }
  function ty(v){ return mt+ph-(v-yMin)/(yMax-yMin)*ph; }
  ctx.strokeStyle = GRID_COLOR;
  ctx.lineWidth = 0.5;
  ctx.font = '11px Consolas, monospace';
  for(var i=0;i<=5;i++) {
    var yv=yMin+(yMax-yMin)*i/5, yp=Math.round(ty(yv))+0.5;
    ctx.beginPath(); ctx.moveTo(ml,yp); ctx.lineTo(W-mr,yp); ctx.stroke();
    ctx.fillStyle = TEXT_DIM; ctx.textAlign='right';
    ctx.fillText(yv.toFixed(yMax-yMin<10?2:1), ml-8, yp+4);
  }
  for(var i=0;i<=5;i++) {
    var xv=tMin+(tMax-tMin)*i/5, xp=Math.round(tx(xv))+0.5;
    ctx.beginPath(); ctx.moveTo(xp,mt); ctx.lineTo(xp,mt+ph); ctx.stroke();
    ctx.fillStyle = TEXT_DIM; ctx.textAlign='center';
    ctx.fillText(xv.toFixed(1)+'s', xp, mt+ph+18);
  }
  ctx.save(); ctx.beginPath(); ctx.rect(ml,mt,pw,ph); ctx.clip();
  names.forEach(function(n) {
    var m=chartFields[n]; if(!m.visible||m.points.length<2) return;
    ctx.strokeStyle=m.color; ctx.lineWidth=2; ctx.lineJoin='round'; ctx.lineCap='round';
    ctx.beginPath();
    var started=false;
    m.points.forEach(function(p) {
      var sx=tx(p.t), sy=ty(p.y);
      if(!started){ ctx.moveTo(sx,sy); started=true; } else ctx.lineTo(sx,sy);
    });
    ctx.stroke();
  });
  ctx.restore();
  canvas.onmousemove = function(e) {
    var rect=canvas.getBoundingClientRect();
    var mx=e.clientX-rect.left, my=e.clientY-rect.top;
    if(mx<ml||mx>ml+pw||my<mt||my>mt+ph) { tooltip.style.display='none'; return; }
    var hoverT=tMin+(mx-ml)/pw*(tMax-tMin);
    var lines=[];
    names.forEach(function(n) {
      var m=chartFields[n]; if(!m.visible||m.points.length<1) return;
      var best=null, bestDist=Infinity;
      m.points.forEach(function(p) { var d=Math.abs(p.t-hoverT); if(d<bestDist){bestDist=d;best=p;} });
      if(best&&bestDist<(tMax-tMin)/pw*15) lines.push('<span style="color:'+m.color+'">'+n+': '+best.y.toFixed(2)+'</span>');
    });
    if(lines.length) {
      tooltip.innerHTML=lines.join('<br>');
      tooltip.style.display='block';
      tooltip.style.left=Math.min(mx+12,W-180)+'px';
      tooltip.style.top=Math.max(0,my-8)+'px';
    } else tooltip.style.display='none';
  };
  canvas.onmouseleave=function(){ tooltip.style.display='none'; };
}
function updateChartSelector() {
  var sel=document.getElementById('var-selector');
  var existing={};
  sel.querySelectorAll('.chip').forEach(function(c){ existing[c.dataset.name]=c; });
  Object.keys(chartFields).sort().forEach(function(n) {
    var m=chartFields[n];
    var chip=existing[n];
    if(!chip) {
      chip=document.createElement('span');
      chip.className='chip'+(m.visible?' active':'');
      chip.dataset.name=n; chip.textContent=n;
      chip.onclick=(function(name,el){ return function(){ toggleChartField(name,el); }; })(n,chip);
      sel.appendChild(chip);
    }
    chip.classList.toggle('active', m.visible);
  });
}
function toggleChartField(name, el) {
  var m=chartFields[name]; if(!m) return;
  m.visible=!m.visible;
  el.classList.toggle('active', m.visible);
}
function buildParamsUI() {
  var content = document.getElementById('params-content');
  var html = '';
  PROFILE.groups.forEach(function(g) {
    var writableRegs = g.registers.filter(function(r) { return r.access === 'rw' && !r.hidden && r.min !== undefined; });
    if (writableRegs.length === 0) return;
    html += '<div class="param-group" data-group="' + g.id + '"><h3>' + g.name + '</h3>';
    html += '<table class="param-table"><tr><th>参数</th><th>当前值</th><th>范围</th><th>默认值</th><th></th></tr>';
    writableRegs.forEach(function(r) {
      html += '<tr data-addr="' + r.addr + '">';
      html += '<td>' + r.label + '</td>';
      html += '<td><span class="val" id="pval-' + r.addr + '">-</span>';
      html += ' <input type="number" id="pedit-' + r.addr + '" min="' + r.min + '" max="' + r.max + '" value="" style="display:none" />';
      html += ' <button class="write-btn" id="pwrite-' + r.addr + '" style="display:none" data-addr="' + r.addr + '">写入</button>';
      html += ' <button class="btn" style="font-size:10px;padding:2px 8px" onclick="editParam(' + r.addr + ')">编辑</button>';
      html += '</td>';
      html += '<td class="range">' + r.min + ' ~ ' + r.max + '</td>';
      html += '<td class="range">' + r.default + '</td>';
      html += '<td><button class="btn" style="font-size:10px;padding:2px 8px" onclick="resetParam(' + r.addr + ')">恢复</button></td>';
      html += '</tr>';
    });
    html += '</table></div>';
  });
  content.innerHTML = html;
  content.querySelectorAll('.write-btn').forEach(function(btn) {
    btn.addEventListener('click', function() { writeParam(parseInt(btn.dataset.addr)); });
  });
  document.getElementById('param-search').addEventListener('input', function() {
    var q = this.value.toLowerCase();
    content.querySelectorAll('.param-group').forEach(function(g) {
      var rows = g.querySelectorAll('tr[data-addr]');
      var anyVisible = false;
      rows.forEach(function(row) {
        var text = row.textContent.toLowerCase();
        var visible = text.indexOf(q) >= 0;
        row.style.display = visible ? '' : 'none';
        if (visible) anyVisible = true;
      });
      g.style.display = anyVisible ? '' : 'none';
    });
  });
}
function updateParams(regs) {
  PROFILE.groups.forEach(function(g) {
    g.registers.forEach(function(r) {
      if (r.access !== 'rw' || r.hidden || r.min === undefined) return;
      var el = document.getElementById('pval-' + r.addr);
      if (!el) return;
      var raw = regs[r.addr];
      if (raw === undefined) return;
      var scale = r.scale || 1;
      var v = raw * scale;
      if (r.type === 'int16' && raw >= 0x8000) v = (raw - 0x10000) * scale;
      el.textContent = v.toFixed(scale < 1 ? 1 : 0) + (r.unit ? ' ' + r.unit : '');
    });
  });
}
function editParam(addr) {
  var edit = document.getElementById('pedit-' + addr);
  var btn = document.getElementById('pwrite-' + addr);
  edit.value = ''; edit.style.display = ''; btn.style.display = '';
  edit.focus();
}
function writeParam(addr) {
  var edit = document.getElementById('pedit-' + addr);
  var val = parseInt(edit.value);
  if (isNaN(val)) return;
  var reg = addrMap[addr];
  if (!reg) return;
  var scale = reg.scale || 1;
  var rawVal = Math.round(val / scale);
  fetch('/write', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({addr:addr, value:rawVal, token:CSRF})
  }).then(function(r){ return r.json(); }).then(function(d) {
    if(d.ok) { edit.style.display='none'; document.getElementById('pwrite-'+addr).style.display='none'; }
    else showFooter(d.error || '写入失败', false);
  }).catch(function(e){ showFooter('网络错误: '+e, false); });
}
function resetParam(addr) {
  var reg = addrMap[addr];
  if (!reg || reg.default === undefined) return;
  if (!confirm('确认将 ' + reg.label + ' 恢复为默认值 (' + reg.default + ')?')) return;
  fetch('/write', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({addr:addr, value:reg.default, token:CSRF})
  }).then(function(r){ return r.json(); }).then(function(d) {
    if(!d.ok) showFooter(d.error || '恢复失败', false);
  }).catch(function(e){ showFooter('网络错误: '+e, false); });
}
function appendDebugLog(text) {
  var log = document.getElementById('debug-log');
  if (!log) return;
  var ts = new Date().toLocaleTimeString();
  log.textContent += '[' + ts + '] ' + text + '\n';
  log.scrollTop = log.scrollHeight;
}
function parseDebugValues(raw) {
  return raw.split(/[,\s]+/).filter(Boolean).map(function(v) { return parseInt(v); });
}
function initDebugUI() {
  var cmdSel = document.getElementById('dbg-command');
  if (cmdSel) {
    (PROFILE.commands || []).forEach(function(c) {
      var opt = document.createElement('option');
      opt.value = c.action;
      opt.textContent = c.label || c.action;
      cmdSel.appendChild(opt);
    });
  }
  document.getElementById('dbg-read-btn').addEventListener('click', function() {
    var body = {
      token: CSRF,
      fc: parseInt(document.getElementById('dbg-read-fc').value),
      start: parseInt(document.getElementById('dbg-read-start').value),
      quantity: parseInt(document.getElementById('dbg-read-qty').value)
    };
    fetch('/debug/read', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})
      .then(function(r){ return r.json(); })
      .then(function(d){ appendDebugLog((d.ok ? 'READ ' : 'READ FAIL ') + JSON.stringify(d)); })
      .catch(function(e){ appendDebugLog('READ ERROR ' + e); });
  });
  document.getElementById('dbg-write-btn').addEventListener('click', function() {
    var body = {
      token: CSRF,
      fc: parseInt(document.getElementById('dbg-write-fc').value),
      start: parseInt(document.getElementById('dbg-write-start').value),
      values: parseDebugValues(document.getElementById('dbg-write-values').value)
    };
    fetch('/debug/write', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})
      .then(function(r){ return r.json(); })
      .then(function(d){ appendDebugLog((d.ok ? 'WRITE ' : 'WRITE FAIL ') + JSON.stringify(d)); })
      .catch(function(e){ appendDebugLog('WRITE ERROR ' + e); });
  });
  document.getElementById('dbg-command-btn').addEventListener('click', function() {
    var body = {token: CSRF, action: document.getElementById('dbg-command').value};
    var raw = document.getElementById('dbg-command-params').value.trim();
    if (raw) {
      body.params = {};
      raw.split(',').forEach(function(pair) {
        var kv = pair.split('=');
        if (kv.length === 2) body.params[kv[0].trim()] = parseInt(kv[1]);
      });
    }
    fetch('/command', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})
      .then(function(r){ return r.json(); })
      .then(function(d){ appendDebugLog((d.ok ? 'COMMAND ' : 'COMMAND FAIL ') + JSON.stringify(d)); })
      .catch(function(e){ appendDebugLog('COMMAND ERROR ' + e); });
  });
}
buildStatusBar();
buildControlButtons();
initMetrics();
initAlarms();
buildParamsUI();
initDebugUI();
