// ============================================================
// RingBuffer: Float64Array-based circular buffer (Task 7I)
// Replaces Array push/shift with O(1) head/tail pointer ops.
// Supports 50+ channels with zero GC pressure.
// ============================================================
function RingBuffer(capacity) {
  this.capacity = capacity;
  this.buffer = new Float64Array(capacity * 2); // [timestamp, value] pairs
  this.head = 0;
  this.tail = 0;
  this.count = 0;
  // Stats tracking (avoid re-scanning buffer)
  this._min = Infinity;
  this._max = -Infinity;
  this._sum = 0;
  this._count = 0;
}

RingBuffer.prototype.push = function(t, v) {
  var idx = this.head * 2;
  // If buffer is full, advance tail (overwrite oldest)
  if (this.count >= this.capacity) {
    // Subtract oldest from stats
    var oldIdx = this.tail * 2;
    var oldV = this.buffer[oldIdx + 1];
    if (Number.isFinite(oldV)) {
      this._sum -= oldV;
      this._count--;
      // If evicted value was a min/max extreme, recompute from remaining data
      if (oldV <= this._min || oldV >= this._max) {
        // Defer recompute until after new value is written
        this._needRecompute = true;
      }
    }
    this.tail = (this.tail + 1) % this.capacity;
  } else {
    this.count++;
  }
  this.buffer[idx] = t;
  this.buffer[idx + 1] = v;
  this.head = (this.head + 1) % this.capacity;

  // Update stats incrementally
  if (Number.isFinite(v)) {
    if (v < this._min) this._min = v;
    if (v > this._max) this._max = v;
    this._sum += v;
    this._count++;
  }
  // Deferred recompute when old min/max was evicted and new value doesn't cover it
  if (this._needRecompute) {
    this._needRecompute = false;
    this.recomputeStats();
  }
};

RingBuffer.prototype.toArray = function() {
  var result = new Array(this.count);
  for (var i = 0; i < this.count; i++) {
    var idx = ((this.tail + i) % this.capacity) * 2;
    result[i] = { t: this.buffer[idx], y: this.buffer[idx + 1] };
  }
  return result;
};

RingBuffer.prototype.getRange = function(startIdx, endIdx) {
  var result = [];
  var len = Math.min(endIdx, this.count);
  for (var i = startIdx; i < len; i++) {
    var idx = ((this.tail + i) % this.capacity) * 2;
    result.push({ t: this.buffer[idx], y: this.buffer[idx + 1] });
  }
  return result;
};

RingBuffer.prototype.latest = function() {
  if (this.count === 0) return null;
  var idx = ((this.head - 1 + this.capacity) % this.capacity) * 2;
  return { t: this.buffer[idx], y: this.buffer[idx + 1] };
};

RingBuffer.prototype.oldest = function() {
  if (this.count === 0) return null;
  var idx = this.tail * 2;
  return { t: this.buffer[idx], y: this.buffer[idx + 1] };
};

RingBuffer.prototype.recomputeStats = function() {
  this._min = Infinity; this._max = -Infinity;
  this._sum = 0; this._count = 0;
  for (var i = 0; i < this.count; i++) {
    var idx = ((this.tail + i) % this.capacity) * 2;
    var v = this.buffer[idx + 1];
    if (Number.isFinite(v)) {
      if (v < this._min) this._min = v;
      if (v > this._max) this._max = v;
      this._sum += v; this._count++;
    }
  }
};
RingBuffer.prototype.clear = function() {
  this.head = 0; this.tail = 0; this.count = 0;
  this._min = Infinity; this._max = -Infinity;
  this._sum = 0; this._count = 0;
  this._needRecompute = false;
};

// ============================================================
// Constants
// ============================================================
var COLORS = [
  '#c96442','#3898ec','#b58a1b','#2d6a4f','#c084fc',
  '#fb923c','#2dd4bf','#f472b6','#a78bfa','#60a5fa',
  '#ef4444','#22c55e','#eab308','#06b6d4','#8b5cf6',
  '#f97316','#14b8a6','#ec4899','#6366f1','#84cc16',
  '#e11d48','#059669','#d97706','#0891b2','#7c3aed',
  '#ea580c','#0d9488','#db2777','#4f46e5','#65a30d',
  '#be123c','#047857','#b45309','#0e7490','#6d28d9',
  '#c2410c','#0f766e','#be185d','#4338ca','#4d7c0f',
  '#9f1239','#065f46','#92400e','#155e75','#5b21b6',
  '#9a3412','#115e59','#9d174d','#3730a0','#3f6212',
  '#881337','#064e3b','#78350f','#164e63','#4c1d95'
];
var GRID_COLOR = '#e8e6dc';
var TEXT_DIM = '#87867f';
var MAX_POINTS = CONFIG.maxPoints;
var MAX_CHANNELS = 64; // Support 50+ channels (Task 7I)
var RING_BUFFER_CAPACITY = MAX_POINTS; // Ring buffer capacity matches max points

// API path configuration (adapted for FastAPI backend)
var _dashType = CONFIG.mode.toLowerCase(); // 'superwatch' or 'vofa'
var API_STREAM = '/api/dash/' + _dashType + '/stream';
var API_CTRL = '/api/dash/' + _dashType + '/';
var API_SW = '/api/dash/superwatch/';
var API_SYMBOLS = '/api/symbols/';

window.MAX_POINTS = MAX_POINTS;
window.RING_BUFFER_CAPACITY = RING_BUFFER_CAPACITY;

// ============================================================
// State
// ============================================================
var FIELDS = {};
var CHANNEL_METADATA = {};
var colorIdx = 0;
var paused = false;
var tStart = 0;
var rawLogLineCount = 0;
var WATCH_COLUMNS = [
  { key: 'name', label: 'Name', width: 124, minWidth: 72, visible: true },
  { key: 'type', label: 'Type', width: 72, minWidth: 48, visible: true },
  { key: 'value', label: 'Value', width: 200, minWidth: 84, visible: true },
  { key: 'y', label: 'Y Axis', width: 178, minWidth: 92, visible: true },
  { key: 'unit', label: 'Unit', width: 56, minWidth: 44, visible: true }
];
var watchColumnState = {};
for (var wci = 0; wci < WATCH_COLUMNS.length; wci++) {
  watchColumnState[WATCH_COLUMNS[wci].key] = {
    width: WATCH_COLUMNS[wci].width,
    visible: WATCH_COLUMNS[wci].visible
  };
}
var watchColumnResize = null;

function parseNullableNumber(value) {
  if (value === null || value === undefined || value === '') return null;
  var n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function normalizeThresholds(thresholds) {
  if (!thresholds) return null;
  var normalized = {
    warnLow: parseNullableNumber(thresholds.warnLow),
    warnHigh: parseNullableNumber(thresholds.warnHigh),
    alarmLow: parseNullableNumber(thresholds.alarmLow),
    alarmHigh: parseNullableNumber(thresholds.alarmHigh)
  };
  if (
    normalized.warnLow === null && normalized.warnHigh === null &&
    normalized.alarmLow === null && normalized.alarmHigh === null
  ) {
    return null;
  }
  return normalized;
}

function classifyWatchValue(meta, value) {
  if (!Number.isFinite(value)) return 'watch-val-ok';
  var thresholds = normalizeThresholds(meta.thresholds);
  if (thresholds) {
    if (
      (thresholds.alarmLow !== null && value < thresholds.alarmLow) ||
      (thresholds.alarmHigh !== null && value > thresholds.alarmHigh)
    ) {
      return 'watch-val-alarm';
    }
    if (
      (thresholds.warnLow !== null && value < thresholds.warnLow) ||
      (thresholds.warnHigh !== null && value > thresholds.warnHigh)
    ) {
      return 'watch-val-warn';
    }
    return 'watch-val-ok';
  }
  var range = meta.ringBuf._max - meta.ringBuf._min;
  if (range > 0) {
    var pos = (value - meta.ringBuf._min) / range;
    if (pos > 0.9 || pos < 0.1) return 'watch-val-alarm';
    if (pos > 0.8 || pos < 0.2) return 'watch-val-warn';
  }
  return 'watch-val-ok';
}

// Global Y-axis zoom (all channels together, like oscilloscope)
var globalYView = { zoom: 1, offset: 0 };

// Per-channel Y-axis state (Ctrl+wheel for individual channel zoom)
var channelYState = {};  // { name: { zoom: 1, offset: 0, autoRange: true } }
var selectedChannel = null;  // Channel under mouse for Y-axis zoom

// Timeline navigation (Task 5I)
var timelineView = {
  zoom: 1,        // 1 = show all data, >1 = zoomed in
  offset: 0,      // 0..1 fractional offset into data range
  dragging: false,
  dragStartX: 0,
  dragStartOffset: 0
};

// Hover probe: click to activate vertical crosshair + tooltip
var hoverProbe = { active: false, mx: 0, my: 0 };

// Space-held state for hand-tool panning
var spaceHeld = false;

// Measurement cursors (Task 5I)
var draggingTrigger = false;
var probeDownPos = null;
var cursorState = {
  enabled: false,
  a: null,    // { t: timestamp } or null
  b: null,    // { t: timestamp } or null
  dragging: null,  // 'a', 'b', or null
  mode: 'time',    // 'time' or 'value'
};
var cursorReadout = document.getElementById('cursor-readout');
var cursorMeasurePanel = document.getElementById('cursor-measure-panel');

// Watch panel state (Task 6I)
var watchPanel = document.getElementById('watch-panel');
var watchCollapsed = false;

// -- trigger state --
var triggerSettings = {
  enabled: false, source: '', edge: 'rising', level: 0,
  mode: 'auto', preTriggerSamples: 1000, state: 'idle'
};
var preTriggerBuffer = [];
var postTriggerRemaining = 0;
var autoTimeout = null;
var lastTriggerValue = null;
var triggerCaptureData = {};

// Batch read optimization state (Task 7I)
var batchReadPending = false;
var batchReadChannels = [];

// ============================================================
// Canvas setup
// ============================================================
var canvas = document.getElementById('chart');
var ctx = canvas.getContext('2d');
var tooltip = document.getElementById('tooltip');
var wrap = document.getElementById('chart-wrap');

function resize() {
  var r = wrap.getBoundingClientRect();
  var w = r.width || wrap.clientWidth;
  var h = r.height || wrap.clientHeight;
  if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) return false;
  var dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.round(w * dpr));
  canvas.height = Math.max(1, Math.round(h * dpr));
  canvas.style.width = w + 'px';
  canvas.style.height = h + 'px';
  ctx.setTransform(1,0,0,1,0,0);
  ctx.scale(dpr, dpr);
  return true;
}
window.addEventListener('resize', function() { resize(); drawChart(); });
resize();

// Minimap canvas setup
var minimapCanvas = document.getElementById('minimap-canvas');
var minimapCtx = minimapCanvas.getContext('2d');

function resizeMinimap() {
  var mmWrap = document.getElementById('minimap-wrap');
  var r = mmWrap.getBoundingClientRect();
  var w = r.width || mmWrap.clientWidth;
  var h = r.height || mmWrap.clientHeight;
  if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) return;
  var dpr = window.devicePixelRatio || 1;
  minimapCanvas.width = Math.max(1, Math.round(w * dpr));
  minimapCanvas.height = Math.max(1, Math.round(h * dpr));
  minimapCanvas.style.width = w + 'px';
  minimapCanvas.style.height = h + 'px';
  minimapCtx.setTransform(1,0,0,1,0,0);
  minimapCtx.scale(dpr, dpr);
}

// Auto-resize when panel toggles
var debugMain = document.getElementById('debug-main');
new ResizeObserver(function() { resize(); resizeMinimap(); drawChart(); }).observe(debugMain);
document.getElementById('raw-log-panel').addEventListener('transitionend', function(e) {
  if (e.propertyName === 'height') { resize(); drawChart(); }
});

// ============================================================
// SSE connection
// ============================================================
function sseOnMessage(e) {
  try {
    var data = JSON.parse(e.data);
    if (data.event && !data._event) data._event = data.event;
    if (data._event === 'shutdown') {
      es.close();
      document.getElementById('shutdown-overlay').classList.add('visible');
      document.getElementById('conn-status').textContent = t('stopped');
      document.getElementById('conn-status').className = 'badge badge-warn';
      return;
    }
    if (data._event === 'state_change') {
      updateCollectionUI(data.state);
      return;
    }
    if (data._event === 'interval_change') {
      currentInterval = data.interval;
      document.getElementById('interval-input').value = data.interval;
      return;
    }
    if (data._event === 'channel_metadata') {
      applyChannelMetadata(data.channels || {});
      return;
    }
    if (data._event === 'error') {
      var connStatus = document.getElementById('conn-status');
      if (connStatus) {
        connStatus.textContent = data.message || t('error');
        connStatus.className = 'badge badge-err';
      }
      return;
    }
    processPoint(data);
  } catch(_){}
}
function sseOnError() {
  if (es.readyState === EventSource.CLOSED) {
    document.getElementById('conn-status').textContent = t('stopped');
    document.getElementById('conn-status').className = 'badge badge-warn';
  } else {
    document.getElementById('conn-status').textContent = t('reconnecting');
    document.getElementById('conn-status').className = 'badge badge-warn';
  }
}
function sseOnOpen() {
  document.getElementById('conn-status').textContent = t('live');
  document.getElementById('conn-status').className = 'badge badge-ok';
  swTimeOrigin = null;
  for (var k in FIELDS) { if (FIELDS[k] && FIELDS[k].ringBuf) FIELDS[k].ringBuf.clear(); }
}
var es = new EventSource(API_STREAM);
es.onmessage = sseOnMessage;
es.onerror = sseOnError;
es.onopen = sseOnOpen;

// ============================================================
// Collection control (Start/Pause/Stop + Interval)
// ============================================================
var collectionState = 'stopped';
var currentInterval = 0;
var estimatedInterval = 0;
var estimatedRate = 0;
var IS_VOFA_MODE = CONFIG.mode === 'VOFA';
var IS_SUPERWATCH_MODE = CONFIG.mode === 'SuperWatch';
var timeUnit = 'ms';
// Initialize UI to stopped state
updateCollectionUI('stopped');

// Hide interval controls in RTT mode (data rate is firmware-controlled)
if (!IS_VOFA_MODE && !IS_SUPERWATCH_MODE) {
  var ig = document.getElementById('interval-group');
  if (ig) ig.style.display = 'none';
}
if (IS_SUPERWATCH_MODE) {
  var swp = document.getElementById('superwatch-panel');
  if (swp) {
    swp.classList.add('visible');
    swp.setAttribute('aria-hidden', 'false');
  }
}

function updateSampleRateBadge(interval, rate) {
  if (Number.isFinite(Number(interval)) && Number(interval) > 0) {
    estimatedInterval = Number(interval);
  }
  if (Number.isFinite(Number(rate)) && Number(rate) > 0) {
    estimatedRate = Number(rate);
  } else if (estimatedInterval > 0) {
    estimatedRate = 1 / estimatedInterval;
  }
  var badge = document.getElementById('sample-rate-badge');
  if (!badge) return;
  if (estimatedRate > 0) {
    badge.textContent = 'rate ' + estimatedRate.toFixed(2) + ' Hz / ' + (estimatedInterval * 1000).toFixed(1) + ' ms';
  } else {
    badge.textContent = 'rate -- Hz';
  }
}

function updateCollectionUI(state) {
  collectionState = state;
  var btnStart = document.getElementById('btn-start');
  var btnPause = document.getElementById('btn-pause');
  var btnStop = document.getElementById('btn-stop');
  var badge = document.getElementById('collection-status-badge');
  var connStatus = document.getElementById('conn-status');

  btnStart.classList.toggle('active', state === 'running');
  btnPause.classList.toggle('active', state === 'paused');
  btnPause.textContent = (state === 'paused') ? t('resume') : t('pause');

  btnStart.disabled = (state === 'running') || (typeof CONFIG !== 'undefined' && CONFIG.deviceConnected === false);
  btnPause.disabled = (state === 'stopped');
  btnStop.disabled = (state === 'stopped');

  badge.textContent = t(state) || (state.charAt(0).toUpperCase() + state.slice(1));
  badge.className = 'status-' + state;

  if (state === 'running') {
    connStatus.textContent = t('live');
    connStatus.className = 'badge badge-ok';
    paused = false;
  } else if (state === 'paused') {
    renderGeneration++;
    updatePending = false;
    connStatus.textContent = t('paused');
    connStatus.className = 'badge badge-warn';
    paused = true;
  } else {
    connStatus.textContent = t('stopped');
    connStatus.className = 'badge badge-warn';
  }
}

function setDeviceConnected(connected) {
  if (typeof CONFIG !== 'undefined') CONFIG.deviceConnected = connected;
  updateCollectionUI(collectionState);
}
if (typeof window !== 'undefined') {
  if (!window.__waveformViewers) window.__waveformViewers = {};
  window.__waveformViewers[CONFIG ? CONFIG.mode : ''] = { setDeviceConnected: setDeviceConnected };
}

document.getElementById('btn-start').addEventListener('click', function() {
  if (typeof CONFIG !== 'undefined' && CONFIG.deviceConnected === false) return;
  fetch(API_CTRL + 'start', {method:'POST'})
    .then(function(r){return r.json()})
    .then(function(d){
      updateCollectionUI('running');
      // Reconnect SSE if needed
      if (!es || es.readyState === EventSource.CLOSED) {
        if (es) es.close();
        es = new EventSource(API_STREAM);
        es.onmessage = sseOnMessage;
        es.onerror = sseOnError;
        es.onopen = sseOnOpen;
      }
    })
    .catch(function(){});
});
document.getElementById('btn-pause').addEventListener('click', function() {
  var action = (collectionState === 'paused') ? 'resume' : 'pause';
  fetch(API_CTRL + action, {method:'POST'})
    .then(function(r){return r.json()})
    .then(function(d){updateCollectionUI(d.status)})
    .catch(function(){});
});
document.getElementById('btn-stop').addEventListener('click', function() {
  if (!confirm('Stop data collection? This will end the session.')) return;
  fetch(API_CTRL + 'stop', {method:'POST'})
    .then(function(r){return r.json()})
    .then(function(d){updateCollectionUI('stopped')})
    .catch(function(){});
});
document.getElementById('btn-apply-buffer').addEventListener('click', function() {
  var val = parseInt(document.getElementById('buffer-input').value, 10);
  if (!Number.isFinite(val) || val < 2 || val > 200000) {
    alert('Buffer must be between 2 and 200000 points');
    return;
  }
  setBufferCapacity(val);
});
document.getElementById('btn-apply-interval').addEventListener('click', function() {
  var val = parseFloat(document.getElementById('interval-input').value);
  if (isNaN(val) || val < 0 || val > 60) {
    alert('Interval must be between 0 and 60 seconds');
    return;
  }
  fetch(API_CTRL + 'interval', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({interval: val})
  })
    .then(function(r){return r.json()})
    .then(function(d){currentInterval = d.interval})
    .catch(function(){});
});

// Sync initial state from server
fetch(API_CTRL + 'status')
  .then(function(r){return r.json()})
  .then(function(d){
    updateCollectionUI(d.state);
    updateSampleRateBadge(d.estimated_interval, d.estimated_rate);
    applyChannelMetadata(d.channel_metadata || {});
    if (d.interval !== undefined && d.interval > 0) {
      currentInterval = d.interval;
      document.getElementById('interval-input').value = d.interval;
    }
  })
  .catch(function(){});

// ============================================================
// Bottom panel management
// ============================================================
var rawLogPanel = document.getElementById('raw-log-panel');
var rawLogEl = document.getElementById('raw-log');
var rawLogCountEl = document.getElementById('raw-log-count');
var rawLogOpen = false;

function setRawLogOpen(open) {
  rawLogOpen = open;
  rawLogPanel.dataset.open = open ? 'true' : 'false';
  if (open) {
    resize(); resizeMinimap(); drawChart();
  }
}

function toggleRawLog() { setRawLogOpen(!rawLogOpen); }
rawLogPanel.querySelector('.panel-header').addEventListener('click', function(e) {
  if (e.target.closest('button')) return;
  toggleRawLog();
});
document.getElementById('raw-log-close').addEventListener('click', function() { setRawLogOpen(false); });
document.getElementById('raw-log-clear').addEventListener('click', function() {
  rawLogEl.textContent = '';
  rawLogLineCount = 0;
  rawLogCountEl.textContent = '0 lines';
});

// -- drag resize for raw log --
var panelResizer = document.querySelector('#raw-log-panel .panel-resizer');
var resizingRawLog = false;

panelResizer.addEventListener('pointerdown', function(e) {
  resizingRawLog = true;
  panelResizer.setPointerCapture(e.pointerId);
  document.body.style.cursor = 'ns-resize';
  document.body.style.userSelect = 'none';
});

panelResizer.addEventListener('pointermove', function(e) {
  if (!resizingRawLog) return;
  var mainRect = debugMain.getBoundingClientRect();
  var h = Math.round(mainRect.bottom - e.clientY);
  h = Math.max(96, Math.min(h, Math.round(mainRect.height * 0.55)));
  rawLogPanel.style.setProperty('--raw-log-h', h + 'px');
  resize(); drawChart();
});

panelResizer.addEventListener('pointerup', function(e) {
  resizingRawLog = false;
  panelResizer.releasePointerCapture(e.pointerId);
  document.body.style.cursor = '';
  document.body.style.userSelect = '';
});

// ============================================================
// Watch panel management (Task 6I)
// ============================================================
(function initWatchPanel() {
  var watchResizer = document.getElementById('watch-resizer');
  var watchCollapseBtn = document.getElementById('watch-collapse');
  var resizingWatch = false;

  watchCollapseBtn.addEventListener('click', function() {
    watchCollapsed = !watchCollapsed;
    watchPanel.classList.toggle('collapsed', watchCollapsed);
    watchCollapseBtn.textContent = watchCollapsed ? '▶' : '✕';
    resize(); drawChart();
  });

  watchResizer.addEventListener('pointerdown', function(e) {
    resizingWatch = true;
    watchResizer.classList.add('active');
    watchResizer.setPointerCapture(e.pointerId);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    e.preventDefault();
  });

  watchResizer.addEventListener('pointermove', function(e) {
    if (!resizingWatch) return;
    var wrapRect = document.getElementById('chart-watch-wrap').getBoundingClientRect();
    var chartWrap = document.getElementById('chart-wrap');
    var watchWidth = wrapRect.right - e.clientX;
    watchWidth = Math.max(180, Math.min(watchWidth, wrapRect.width * 0.5));
    watchPanel.style.flex = '0 0 ' + watchWidth + 'px';
    resize(); drawChart();
  });

  watchResizer.addEventListener('pointerup', function(e) {
    resizingWatch = false;
    watchResizer.classList.remove('active');
    watchResizer.releasePointerCapture(e.pointerId);
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
  });
})();

// Update watch table with color-coded values
function isIntegerType(typeName) {
  return /^(rt_)?(u?int(8|16|32|64)(_t)?|u?char|short|ushort|int|uint)$/i.test(typeName || '');
}

function isBoolType(typeName) {
  return /^(bool|boolean)$/i.test(typeName || '');
}

function hasEnumValues(meta) {
  return !!(meta && meta.enumValues && Object.keys(meta.enumValues).length);
}

function normalizeValueFormat(format) {
  if (format === 'decimal') return 'dec';
  if (format === 'binary') return 'bin';
  if (format === 'hexadecimal') return 'hex';
  if (format === 'dec' || format === 'hex' || format === 'bin') return format;
  return 'auto';
}

function bitWidthForMeta(meta) {
  var size = parseInt(meta && meta.size, 10);
  if (Number.isFinite(size) && size > 0) return Math.min(64, size * 8);
  return 32;
}

function integerDisplayValue(value, meta) {
  var n = Math.trunc(Number(value));
  var width = bitWidthForMeta(meta);
  if (/^int/i.test(meta.type || '') && n < 0) {
    var mod = Math.pow(2, width);
    return (mod + n) % mod;
  }
  return n;
}

function formatIntegerHex(value, meta) {
  var n = integerDisplayValue(value, meta);
  var digits = Math.max(2, Math.ceil(bitWidthForMeta(meta) / 4));
  var text = n.toString(16).toUpperCase();
  while (text.length < digits) text = '0' + text;
  return '0x' + text;
}

function formatIntegerBin(value, meta) {
  var n = integerDisplayValue(value, meta);
  var digits = bitWidthForMeta(meta);
  var text = n.toString(2);
  while (text.length < digits) text = '0' + text;
  return '0b' + text;
}

function supportsIntegerFormats(meta) {
  return hasEnumValues(meta) || isIntegerType(meta.type) || isBoolType(meta.type);
}

function formatTypedValue(value, meta, formatOverride) {
  if (!Number.isFinite(value)) return '-';
  var typeName = meta.type || 'number';
  var fmt = normalizeValueFormat(formatOverride || meta.format || 'auto');
  if (hasEnumValues(meta)) {
    var enumKey = String(Math.trunc(value));
    if (fmt === 'auto' && meta.enumValues[enumKey] !== undefined) return meta.enumValues[enumKey];
  }
  if (supportsIntegerFormats(meta)) {
    if (fmt === 'hex') return formatIntegerHex(value, meta);
    if (fmt === 'bin') return formatIntegerBin(value, meta);
    if (isBoolType(typeName) && fmt === 'auto') return value ? 'true' : 'false';
    return String(Math.trunc(value));
  }
  if (isBoolType(typeName)) return value ? 'true' : 'false';
  return value.toFixed(meta.precision || 2);
}

function formatOptionsForMeta(meta) {
  if (supportsIntegerFormats(meta)) return ['auto', 'dec', 'hex', 'bin'];
  return ['auto', 'dec'];
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, function(ch) {
    return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[ch];
  });
}

function renderValueFormatSelect(name, meta) {
  var options = formatOptionsForMeta(meta);
  var current = normalizeValueFormat(meta.format || 'auto');
  if (options.indexOf(current) < 0) current = 'auto';
  var labels = {auto: 'Auto', dec: 'Dec', hex: 'Hex', bin: 'Bin'};
  var html = '<select class="value-format-select" data-name="' + escapeHtml(name) + '" title="Value format">';
  for (var i = 0; i < options.length; i++) {
    var opt = options[i];
    html += '<option value="' + opt + '" label="' + labels[opt] + '"' + (opt === current ? ' selected' : '') + '></option>';
  }
  html += '</select>';
  return html;
}

function parseSmartValue(str, typeName) {
  str = str.trim();
  var isFloat = (typeName === 'float' || typeName === 'double');
  if (/^0[xX]/.test(str)) return { value: parseInt(str, 16), float: false };
  if (/^0[bB]/.test(str)) return { value: parseInt(str.slice(2), 2), float: false };
  if (isFloat || /^\-?\d+\.\d+$/.test(str)) {
    var f = parseFloat(str);
    return { value: f, float: true, double: typeName === 'double' };
  }
  return { value: parseInt(str, 10), float: false };
}

function floatToHexBytes(value, isDouble) {
  var buf = new ArrayBuffer(isDouble ? 8 : 4);
  var dv = new DataView(buf);
  if (isDouble) dv.setFloat64(0, value, false);
  else dv.setFloat32(0, value, false);
  var hex = '';
  for (var i = 0; i < buf.byteLength; i++) hex += dv.getUint8(i).toString(16).toUpperCase().padStart(2, '0');
  return { hex: hex, width: buf.byteLength };
}

function _writeWatchValue(name, valueStr, meta, td) {
  var parsed = parseSmartValue(valueStr, meta.type);
  var addr = parseInt(meta.address, 16);
  var width = meta.size;
  var hexValue;
  if (parsed.float) {
    var fb = floatToHexBytes(parsed.value, parsed.double);
    hexValue = fb.hex;
    width = fb.width;
  } else {
    hexValue = parsed.value.toString(16).toUpperCase();
  }
  fetch('/api/memory/write', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      addr: '0x' + addr.toString(16).toUpperCase().padStart(8, '0'),
      value: hexValue,
      width: width
    })
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.error) { console.error('[WatchEdit] Write failed:', data.error); return; }
    td.classList.add('watch-val-written');
    var textEl = td.querySelector('.watch-value-text');
    if (textEl) textEl.textContent = valueStr;
    _watchWrittenValues[name] = { displayText: valueStr, time: Date.now() };
    setTimeout(function() {
      td.classList.remove('watch-val-written');
      delete _watchWrittenValues[name];
    }, 3000);
  });
}

function getWatchColumnDef(key) {
  for (var i = 0; i < WATCH_COLUMNS.length; i++) {
    if (WATCH_COLUMNS[i].key === key) return WATCH_COLUMNS[i];
  }
  return null;
}

function isWatchColumnVisible(key) {
  var st = watchColumnState[key];
  if (st && st.userVisible !== undefined) return !!st.userVisible;
  if (window.innerWidth <= 640 && (key === 'type' || key === 'y')) return false;
  return !st || st.visible !== false;
}

function getWatchColumnWidth(key) {
  var def = getWatchColumnDef(key);
  var st = watchColumnState[key] || {};
  var minW = def ? def.minWidth : 40;
  var width = Number(st.width || (def ? def.width : minW));
  return Math.max(minW, width);
}

function watchColClass(key) {
  return isWatchColumnVisible(key) ? '' : ' watch-col-hidden';
}

function watchColStyle(key) {
  return 'width:' + getWatchColumnWidth(key) + 'px;min-width:' + getWatchColumnWidth(key) + 'px;';
}

function renderWatchHeader() {
  var row = document.getElementById('watch-table-head-row');
  if (!row) return;
  var html = '';
  // Delete button column at the front
  html += '<th class="watch-col-delete"></th>';
  for (var i = 0; i < WATCH_COLUMNS.length; i++) {
    var col = WATCH_COLUMNS[i];
    html += '<th data-col="' + col.key + '" class="' + watchColClass(col.key) + '" style="' + watchColStyle(col.key) + '">' +
      '<span class="watch-th-content"><span class="watch-th-label">' + escapeHtml(col.label) + '</span></span>' +
      '<span class="watch-col-resizer" data-col="' + col.key + '"></span>' +
      '</th>';
  }
  row.innerHTML = html;
  bindWatchColumnResizers();
}

function renderWatchColumnsMenu() {
  var menu = document.getElementById('watch-columns-menu');
  if (!menu) return;
  var html = '';
  for (var i = 0; i < WATCH_COLUMNS.length; i++) {
    var col = WATCH_COLUMNS[i];
    html += '<label><input type="checkbox" class="watch-column-toggle" data-col="' + col.key + '"' + (isWatchColumnVisible(col.key) ? ' checked' : '') + '> ' + escapeHtml(col.label) + '</label>';
  }
  menu.innerHTML = html;
  var toggles = menu.querySelectorAll('.watch-column-toggle');
  for (var ti = 0; ti < toggles.length; ti++) {
    toggles[ti].addEventListener('change', function() {
      setWatchColumnVisible(this.dataset.col, this.checked);
    });
  }
}

function setWatchColumnVisible(key, visible) {
  if (!watchColumnState[key]) watchColumnState[key] = {};
  watchColumnState[key].visible = !!visible;
  watchColumnState[key].userVisible = !!visible;
  _watchStructGen++;
  updateWatchTable();
}

function setWatchColumnWidth(key, width) {
  var def = getWatchColumnDef(key);
  if (!def) return;
  if (!watchColumnState[key]) watchColumnState[key] = {};
  watchColumnState[key].width = Math.max(def.minWidth, Math.round(Number(width) || def.width));
}

function _applyColumnWidthLive(key) {
  var w = getWatchColumnWidth(key);
  var th = document.querySelector('#watch-table thead th[data-col="' + key + '"]');
  if (th) { th.style.width = w + 'px'; th.style.minWidth = w + 'px'; }
  var tds = document.querySelectorAll('#watch-table tbody td[data-col="' + key + '"]');
  for (var i = 0; i < tds.length; i++) {
    tds[i].style.width = w + 'px';
    tds[i].style.minWidth = w + 'px';
  }
}

function bindWatchColumnResizers() {
  var resizers = document.querySelectorAll('.watch-col-resizer');
  var _rafId = 0;

  function _onMove(e) {
    if (!watchColumnResize) return;
    var newW = watchColumnResize.startWidth + e.clientX - watchColumnResize.startX;
    setWatchColumnWidth(watchColumnResize.key, newW);
    cancelAnimationFrame(_rafId);
    _rafId = requestAnimationFrame(function() {
      if (watchColumnResize) _applyColumnWidthLive(watchColumnResize.key);
    });
  }
  function _onUp() {
    if (!watchColumnResize) return;
    var th = document.querySelector('#watch-table thead th[data-col="' + watchColumnResize.key + '"]');
    if (th) th.classList.remove('resizing');
    watchColumnResize = null;
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    document.removeEventListener('mousemove', _onMove);
    document.removeEventListener('mouseup', _onUp);
  }
  for (var i = 0; i < resizers.length; i++) {
    resizers[i].addEventListener('mousedown', function(e) {
      e.preventDefault();
      var key = this.dataset.col;
      watchColumnResize = {
        key: key,
        startX: e.clientX,
        startWidth: getWatchColumnWidth(key)
      };
      var th = this.closest('th');
      if (th) th.classList.add('resizing');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
      document.addEventListener('mousemove', _onMove);
      document.addEventListener('mouseup', _onUp);
    });
  }
}

var _watchStructGen = 0;  // bumped on structural changes (add/remove channel)
var _watchWrittenValues = {};  // name -> {displayText, time} for write highlight
var _watchLastRenderGen = -1;

// Look up a specific tree node by dotted path (e.g. "heaterState.fanConfig")
function _findTreeNode(path) {
  if (!path) return null;
  var parts = path.split('.');
  var baseName = parts[0];
  var tree = _inspectCache[baseName];
  if (!tree) return null;
  // If path is just the root name, return the tree itself
  if (parts.length === 1) return tree;
  // Walk the children for each subsequent part
  var node = tree;
  for (var i = 1; i < parts.length; i++) {
    if (!node.children) return null;
    var found = false;
    for (var j = 0; j < node.children.length; j++) {
      if (node.children[j].name === parts[i]) {
        node = node.children[j];
        found = true;
        break;
      }
    }
    if (!found) return null;
  }
  return node;
}

function _buildWatchRowHtml(name, m) {
  var pts = m.ringBuf;
  var latest = pts ? pts.latest() : null;
  var isStruct = (CHANNEL_METADATA[name] && CHANNEL_METADATA[name].source === 'struct')
    || (!CHANNEL_METADATA[name] && (!latest || (pts && pts.count === 0)) && name.indexOf('.') < 0);
  var cur = latest ? formatTypedValue(latest.y, m) : '-';
  var typeText = m.type || 'number';
  var unitText = m.unit || '-';
  var yState = channelYState[name] || {};
  var yManual = yState.autoRange === false;
  var yMin = (yState.manualMin !== undefined && yState.manualMin !== null) ? yState.manualMin : '';
  var yMax = (yState.manualMax !== undefined && yState.manualMax !== null) ? yState.manualMax : '';
  var valClass = latest ? classifyWatchValue(m, latest.y) : 'watch-val-ok';
  // Precisely check if THIS name is a struct node with expandable children
  var treeNode = _findTreeNode(name);
  var hasChildren;
  if (treeNode) {
    hasChildren = treeNode.children && treeNode.children.length > 0
      && treeNode.kind !== 'bitfield' && !treeNode.enumValues;
    console.log('[watch-row] name=' + name + ' treeNode found, hasChildren=' + hasChildren + ' children=' + (treeNode.children ? treeNode.children.length : 0) + ' kind=' + treeNode.kind);
  } else {
    // No cached tree: show expand button for struct-sourced items or top-level names
    var baseName = name.split('.')[0];
    var treeCached = !!_inspectCache[baseName];
    if (treeCached) {
      hasChildren = false; // tree exists but this path not found -> leaf
      console.log('[watch-row] name=' + name + ' treeCached but path not found -> hasChildren=false');
    } else {
      // No tree cached yet: guess based on metadata / name pattern
      hasChildren = (CHANNEL_METADATA[name] && CHANNEL_METADATA[name].source === 'struct')
        || (name.indexOf('.') < 0);
      console.log('[watch-row] name=' + name + ' no tree cached, guess hasChildren=' + hasChildren + ' source=' + (CHANNEL_METADATA[name] ? CHANNEL_METADATA[name].source : 'null'));
    }
  }
  var isExpanded = !!_expandedRows[name];
  var expandHtml = hasChildren
    ? '<button class="watch-expand-btn' + (isExpanded ? ' expanded' : '') + '" data-name="' + escapeHtml(name) + '" title="Expand struct">' + (isExpanded ? '▼' : '▶') + '</button>'
    : '<span style="display:inline-block;width:14px;flex:0 0 auto;"></span>';
  var visHtml = isStruct
    ? '<span style="display:inline-block;width:14px;flex:0 0 auto;"></span>'
    : '<input type="checkbox" class="watch-visible-toggle" data-name="' + escapeHtml(name) + '"' + (m.visible !== false ? ' checked' : '') + ' title="Show channel">';
  var yHtml = isStruct ? '' : (
    '<span class="watch-y-cell">' +
      '<select class="watch-y-mode" data-name="' + escapeHtml(name) + '" title="Y axis mode">' +
        '<option value="auto"' + (!yManual ? ' selected' : '') + ' label="Auto"></option>' +
        '<option value="manual"' + (yManual ? ' selected' : '') + ' label="Manual"></option>' +
      '</select>' +
      '<input class="watch-y-min" data-name="' + escapeHtml(name) + '" type="number" step="any" placeholder="min" value="' + escapeHtml(yMin) + '"' + (!yManual ? ' disabled' : '') + '>' +
      '<input class="watch-y-max" data-name="' + escapeHtml(name) + '" type="number" step="any" placeholder="max" value="' + escapeHtml(yMax) + '"' + (!yManual ? ' disabled' : '') + '>' +
    '</span>'
  );
  return '<tr data-channel="' + escapeHtml(name) + '">' +
    '<td class="watch-col-delete"><button class="watch-delete-btn" data-name="' + escapeHtml(name) + '" title="Remove">&times;</button></td>' +
    '<td data-col="name" class="' + watchColClass('name') + '" style="' + watchColStyle('name') + 'color:' + m.color + '"><span class="watch-name-cell">' + expandHtml + visHtml + '<span class="watch-name-text">' + escapeHtml(name) + '</span></span></td>' +
    '<td data-col="type" class="' + watchColClass('type') + '" style="' + watchColStyle('type') + '">' + escapeHtml(typeText) + '</td>' +
    '<td data-col="value" class="' + valClass + watchColClass('value') + (_watchWrittenValues[name] ? ' watch-val-written' : '') + '" style="' + watchColStyle('value') + '"><span class="watch-value-cell"><span class="watch-value-text">' + escapeHtml(_watchWrittenValues[name] ? _watchWrittenValues[name].displayText : cur) + '</span>' + (isStruct ? '' : renderValueFormatSelect(name, m)) + '</span></td>' +
    '<td data-col="y" class="' + watchColClass('y') + '" style="' + watchColStyle('y') + '">' + yHtml + '</td>' +
    '<td data-col="unit" class="' + watchColClass('unit') + '" style="' + watchColStyle('unit') + '">' + escapeHtml(unitText) + '</td>' +
    '</tr>';
}

function _buildChildRowHtml(child, parentPath, depth, parentColor, isLast, ancestorPipe) {
  var childPath = parentPath ? parentPath + '.' + child.name : child.name;
  ancestorPipe = ancestorPipe || []; // true at each depth level if vertical line continues
  var typeStr = child.type || child.kind || '-';
  var valStr = child.value !== undefined ? String(child.value) : '-';
  var isSubStruct = child.children && child.children.length > 0 && child.kind !== 'bitfield' && !child.enumValues;
  var isEnum = child.enumValues && child.enumValues.length > 0;
  var isBitfield = child.kind === 'bitfield';
  var isExpanded = !!_expandedRows[childPath];
  // Check if this child is already in FIELDS (plottable)
  var isPlotted = !!FIELDS[childPath];

  // For enum fields, resolve the enum name
  var displayVal = valStr;
  if (isEnum && child.value !== undefined) {
    var numVal = parseInt(child.value);
    for (var ei = 0; ei < child.enumValues.length; ei++) {
      if (child.enumValues[ei].value === numVal) {
        displayVal = child.enumValues[ei].name;
        break;
      }
    }
  }
  // For bitfields, show 0/1 as boolean style
  if (isBitfield && child.value !== undefined) {
    displayVal = child.value === '1' || child.value === 1 ? 'true' : 'false';
  }

  var html = '<tr class="watch-child-row" data-child-path="' + escapeHtml(childPath) + '">';
  // Empty delete column for alignment with parent
  html += '<td class="watch-col-delete"></td>';
  // Name cell with tree guide lines
  html += '<td data-col="name" style="color:' + (parentColor || 'var(--muted)') + '"><span class="watch-name-cell">';
  // Tree guide: ancestor vertical pipes
  html += '<span class="watch-tree-guide">';
  for (var di = 0; di < ancestorPipe.length; di++) {
    html += '<span class="tree-vpipe' + (ancestorPipe[di] ? '' : ' off') + '"></span>';
  }
  // Branch node: lines are pseudo-elements, widget is the only real child
  html += '<span class="tree-node' + (isLast ? '' : ' full') + '">';
  if (isSubStruct) {
    html += '<button class="watch-expand-btn' + (isExpanded ? ' expanded' : '') + '" data-name="' + escapeHtml(childPath) + '" title="Expand sub-struct">' + (isExpanded ? '▼' : '▶') + '</button>';
  } else {
    // Both regular leaf members AND bitfields get the add-to-chart checkbox
    html += '<input type="checkbox" class="watch-child-add-toggle" data-child-path="' + escapeHtml(childPath) + '"' + (isPlotted ? ' checked' : '') + ' title="Add to chart">';
  }
  html += '</span>';
  html += '</span>';
  html += '<span class="watch-name-text">' + escapeHtml(child.name || '-') + '</span></span></td>';
  // Type cell
  html += '<td data-col="type" style="font-size:10px;color:var(--muted)">' + escapeHtml(typeStr);
  if (isBitfield) html += ' [' + (child.bit_offset || 0) + ':' + (child.bit_size || '?') + ']';
  html += '</td>';
  // Value cell
  html += '<td data-col="value">';
  html += '<span class="watch-child-value" style="color:var(--fg)"' +
    (isEnum ? ' data-enum=\'' + escapeHtml(JSON.stringify(child.enumValues)) + '\'' : '') +
    (isBitfield ? ' data-bitfield="1"' : '') +
    '>' + escapeHtml(displayVal) + '</span>';
  // Enum info icon with tooltip
  if (isEnum) {
    var tipLines = [];
    for (var ti = 0; ti < child.enumValues.length; ti++) {
      var ev = child.enumValues[ti];
      tipLines.push(ev.name + ' = ' + ev.value);
    }
    html += ' <span class="enum-info-icon" data-tooltip="' + escapeHtml(tipLines.join('\n')) + '">&#9432;</span>';
  }
  html += '</td>';
  html += '<td data-col="y"></td>';
  html += '<td data-col="unit"></td>';
  html += '</tr>';
  // Recurse for expanded sub-structs or always-rendered bitfields
  if (child.children) {
    // Build ancestor pipe mask for children:
    // current ancestors + whether this node's vertical line continues
    var childAncestor = ancestorPipe.slice();
    if (isSubStruct || (!isSubStruct && !isBitfield)) {
      // For sub-structs and bitfield containers, vertical line continues if not last
      // For bitfield groups, parent line always continues
    }
    childAncestor.push(!isLast);
    if (isSubStruct) {
      if (isExpanded) {
        for (var i = 0; i < child.children.length; i++) {
          html += _buildChildRowHtml(child.children[i], childPath, depth + 1, parentColor, i === child.children.length - 1, childAncestor);
        }
      }
    } else {
      for (var j = 0; j < child.children.length; j++) {
        html += _buildChildRowHtml(child.children[j], childPath, depth + 1, parentColor, j === child.children.length - 1, childAncestor);
      }
    }
  }
  return html;
}

function _rebuildWatchTable() {
  var tbody = document.getElementById('watch-tbody');
  // Skip rebuild if any row is being edited (dblclick editing in progress)
  if (tbody.querySelector('tr.watch-editing')) return;
  var names = Object.keys(FIELDS).sort();
  console.log('[rebuildWatch] names=' + names.join(', ') + ' expanded=' + Object.keys(_expandedRows).join(','));
  var html = '';
  for (var i = 0; i < names.length; i++) {
    var name = names[i];
    // Skip child members if their parent struct is also in FIELDS
    // (they will be shown in the parent's expanded tree instead)
    if (name.indexOf('.') >= 0) {
      var baseName = name.split('.')[0];
      if (FIELDS[baseName]) {
        console.log('[rebuildWatch] skipping child ' + name + ' because parent ' + baseName + ' exists');
        continue;
      }
    }
    html += _buildWatchRowHtml(name, FIELDS[name]);
    // Insert expanded child rows if this row is expanded
    if (_expandedRows[name]) {
      var baseName = name.split('.')[0];
      var tree = _inspectCache[baseName];
      if (tree && tree.children) {
        var color = FIELDS[name] ? FIELDS[name].color : 'var(--muted)';
        for (var c = 0; c < tree.children.length; c++) {
          html += _buildChildRowHtml(tree.children[c], baseName, 0, color, c === tree.children.length - 1, []);
        }
      }
    }
  }
  tbody.innerHTML = html;
  _watchLastRenderGen = _watchStructGen;
  _bindWatchDelegates(tbody);
  renderWatchColumnsMenu();
}

function _bindWatchDelegates(tbody) {
  if (tbody._delegatesBound) return;
  tbody._delegatesBound = true;
  tbody.addEventListener('change', function(e) {
    var el = e.target;
    if (el.classList.contains('watch-visible-toggle')) {
      setChannelVisible(el.dataset.name, el.checked);
    } else if (el.classList.contains('watch-child-add-toggle')) {
      var childPath = el.dataset.childPath;
      console.log('[watch-child-toggle] path=' + childPath + ' checked=' + el.checked);
      if (el.checked) {
        superwatchAddName(childPath);
      } else {
        removeWatchChannel(childPath);
        if (typeof IS_SUPERWATCH_MODE !== 'undefined' && IS_SUPERWATCH_MODE) {
          fetch(API_SW + 'remove', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:childPath})});
        }
      }
    } else if (el.classList.contains('value-format-select')) {
      var ch = FIELDS[el.dataset.name];
      if (ch) {
        ch.format = normalizeValueFormat(el.value);
        var latest = ch.ringBuf ? ch.ringBuf.latest() : null;
        var textEl = el.closest('.watch-value-cell').querySelector('.watch-value-text');
        if (textEl) textEl.textContent = latest ? formatTypedValue(latest.y, ch) : '-';
      }
    } else if (el.classList.contains('watch-y-mode')) {
      setChannelYMode(el.dataset.name, el.value, el.closest('tr'));
    } else if (el.classList.contains('watch-y-min') || el.classList.contains('watch-y-max')) {
      updateManualYFromRow(el.dataset.name, el.closest('tr'));
    }
  });
  tbody.addEventListener('click', function(e) {
    // Struct expand/collapse button (works for both top-level and sub-structs)
    var expandBtn = e.target.closest('.watch-expand-btn');
    if (expandBtn) {
      e.stopPropagation();
      var name = expandBtn.dataset.name;
      if (_expandedRows[name]) {
        delete _expandedRows[name];
        _watchStructGen++;
        _rebuildWatchTable();
      } else {
        var baseName = name.split('.')[0];
        if (_inspectCache[baseName]) {
          _expandedRows[name] = true;
          _watchStructGen++;
          _rebuildWatchTable();
        } else {
          fetch(API_SW + 'inspect?name=' + encodeURIComponent(name))
            .then(function(r){ return r.json(); })
            .then(function(d){
              if (d.tree) {
                _inspectCache[baseName] = d.tree;
                _expandedRows[name] = true;
                _watchStructGen++;
                _rebuildWatchTable();
              }
            })
            .catch(function(){});
        }
      }
      return;
    }
    var btn = e.target.closest('.watch-delete-btn');
    if (!btn) return;
    var n = btn.dataset.name;
    if (!n) return;
    e.stopPropagation();
    removeWatchChannel(n);
    if (typeof IS_SUPERWATCH_MODE !== 'undefined' && IS_SUPERWATCH_MODE) {
      fetch(API_SW + 'remove', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:n})});
    }
  });
  // Global enum tooltip (fixed position, not clipped by overflow)
  var enumTip = document.getElementById('enum-tooltip');
  if (enumTip) {
    tbody.addEventListener('mouseover', function(e) {
      var icon = e.target.closest('.enum-info-icon');
      if (!icon) { enumTip.style.display = 'none'; return; }
      var text = icon.getAttribute('data-tooltip');
      if (!text) return;
      enumTip.textContent = text;
      enumTip.style.display = 'block';
      var r = icon.getBoundingClientRect();
      enumTip.style.left = r.left + 'px';
      enumTip.style.top = (r.bottom + 4) + 'px';
    });
    tbody.addEventListener('mouseout', function(e) {
      var icon = e.target.closest('.enum-info-icon');
      if (icon) enumTip.style.display = 'none';
    });
  }
  // Watch value editing: double-click to edit
  tbody.addEventListener('dblclick', function(e) {
    var td = e.target.closest('td[data-col="value"]');
    if (!td) return;
    var tr = td.closest('tr[data-channel]');
    if (!tr) return;
    var name = tr.getAttribute('data-channel');
    if (!name || !FIELDS[name] || !CHANNEL_METADATA[name]) return;
    if (td.querySelector('.watch-cell-edit')) return;
    var meta = CHANNEL_METADATA[name];
    var textEl = td.querySelector('.watch-value-text');
    if (!textEl) return;
    var oldText = textEl.textContent;
    tr.classList.add('watch-editing');
    var input = document.createElement('input');
    input.type = 'text';
    input.className = 'watch-cell-edit';
    input.value = oldText;
    textEl.style.display = 'none';
    textEl.parentNode.insertBefore(input, textEl);
    input.focus();
    input.select();
    var committed = false;
    function commit() {
      if (committed) return;
      committed = true;
      var newVal = input.value.trim();
      tr.classList.remove('watch-editing');
      textEl.style.display = '';
      if (input.parentNode) input.parentNode.removeChild(input);
      if (newVal && newVal !== oldText) {
        _writeWatchValue(name, newVal, meta, td);
      }
    }
    function cancel() {
      if (committed) return;
      committed = true;
      tr.classList.remove('watch-editing');
      textEl.style.display = '';
      if (input.parentNode) input.parentNode.removeChild(input);
    }
    input.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') { e.preventDefault(); commit(); }
      if (e.key === 'Escape') { e.preventDefault(); cancel(); }
    });
    input.addEventListener('blur', cancel);
  });
}

function updateWatchTable() {
  var tbody = document.getElementById('watch-tbody');
  var names = Object.keys(FIELDS).sort();
  document.getElementById('watch-count').textContent = names.length + ' ch';
  renderWatchHeader();

  // Full rebuild only when structure changed (add/remove channel)
  if (_watchStructGen !== _watchLastRenderGen) {
    _rebuildWatchTable();
    return;
  }
  renderWatchColumnsMenu();

  // Lightweight: only update value cells and classification
  var rows = tbody.querySelectorAll('tr[data-channel]');
  for (var ri = 0; ri < rows.length; ri++) {
    var row = rows[ri];
    if (row.classList.contains('watch-editing')) continue;
    var name = row.getAttribute('data-channel');
    var m = FIELDS[name];
    if (!m) continue;
    var pts = m.ringBuf;
    var latest = pts.latest();
    var cur = latest ? formatTypedValue(latest.y, m) : '-';
    var valClass = latest ? classifyWatchValue(m, latest.y) : 'watch-val-ok';
    var valTd = row.querySelector('td[data-col="value"]');
    if (valTd) {
      // If highlighted (written), check if enough time passed and polled value matches
      if (_watchWrittenValues[name]) {
        var written = _watchWrittenValues[name];
        var elapsed = Date.now() - written.time;
        if (elapsed > 1500 && cur === written.displayText) {
          valTd.classList.remove('watch-val-written');
          delete _watchWrittenValues[name];
        } else {
          continue;
        }
      }
      var textEl = valTd.querySelector('.watch-value-text');
      if (textEl) textEl.textContent = cur;
      // Update classification classes, preserve write highlight
      var baseClass = watchColClass('value');
      var newClass = valClass + baseClass;
      if (valTd.classList.contains('watch-val-written')) newClass += ' watch-val-written';
      valTd.className = newClass;
    }
  }

  // Update child row values from live data
  var childRows = tbody.querySelectorAll('tr.watch-child-row');
  for (var ci = 0; ci < childRows.length; ci++) {
    var cRow = childRows[ci];
    var cPath = cRow.getAttribute('data-child-path');
    var cField = FIELDS[cPath];
    var cValSpan = cRow.querySelector('.watch-child-value');
    if (!cValSpan) continue;
    if (cField && cField.ringBuf) {
      var cLatest = cField.ringBuf.latest();
      if (cLatest) {
        var enumAttr = cValSpan.getAttribute('data-enum');
        var isBf = cValSpan.getAttribute('data-bitfield');
        if (isBf) {
          cValSpan.textContent = (Math.round(cLatest.y) === 1) ? 'true' : 'false';
        } else if (enumAttr) {
          try {
            var evList = JSON.parse(enumAttr);
            var numV = Math.round(cLatest.y);
            var resolved = String(numV);
            for (var ei = 0; ei < evList.length; ei++) {
              if (evList[ei].value === numV) { resolved = evList[ei].name; break; }
            }
            cValSpan.textContent = resolved + ' (' + numV + ')';
          } catch(_) {
            cValSpan.textContent = formatTypedValue(cLatest.y, cField);
          }
        } else {
          cValSpan.textContent = formatTypedValue(cLatest.y, cField);
        }
      }
    }
  }
}

var _removedChannels = {};
var _inspectCache = {};   // name -> tree object from /api/superwatch/inspect
var _expandedRows = {};   // name -> true (tracks which watch rows are expanded)

// Resolve enum name for a dotted path from inspect cache
function _resolveEnumName(path, rawValue) {
  var node = _findTreeNode(path);
  if (!node || !node.enumValues) return null;
  var num = Math.round(rawValue);
  for (var i = 0; i < node.enumValues.length; i++) {
    if (node.enumValues[i].value === num) return node.enumValues[i].name;
  }
  return null;
}

function removeWatchChannel(name) {
  _removedChannels[name] = true;
  delete _expandedRows[name];
  var baseName = name.split('.')[0];
  // Only clear inspect cache if no other channels use the same base struct
  var otherUsers = false;
  for (var k in FIELDS) { if (k !== name && k.split('.')[0] === baseName) { otherUsers = true; break; } }
  if (!otherUsers) delete _inspectCache[baseName];
  if (FIELDS[name]) {
    if (FIELDS[name].ringBuf) FIELDS[name].ringBuf.clear();
    delete FIELDS[name];
  }
  delete CHANNEL_METADATA[name];
  _watchStructGen++;
  updateWatchTable();
  drawChart();
}

function applyChannelMetadata(channels, purge) {
  if (!channels) return;
  for (var name in channels) {
    if (!channels.hasOwnProperty(name)) continue;
    var meta = channels[name] || {};
    console.log('[applyMeta] name=' + name + ' source=' + meta.source + ' isNew=' + !FIELDS[name] + ' purge=' + purge);
    CHANNEL_METADATA[name] = Object.assign({}, CHANNEL_METADATA[name] || {}, meta);
    if (!FIELDS[name]) {
      FIELDS[name] = {
        color: COLORS[Object.keys(FIELDS).length % COLORS.length],
        ringBuf: new RingBuffer(RING_BUFFER_CAPACITY),
        visible: meta.source !== 'struct',
        unit: "",
        type: meta.type || "number",
        size: meta.size || "",
        format: "auto",
        enumValues: null,
        precision: 2,
        thresholds: null
      };
      _watchStructGen++;
    }
    if (FIELDS[name]) {
      if (meta.type !== undefined) FIELDS[name].type = meta.type;
      if (meta.size !== undefined) FIELDS[name].size = meta.size;
      if (meta.unit !== undefined) FIELDS[name].unit = meta.unit;
      if (meta.enumValues !== undefined) FIELDS[name].enumValues = meta.enumValues;
      if (meta.format !== undefined) FIELDS[name].format = normalizeValueFormat(meta.format);
    }
  }
  // Only purge absent channels on full metadata updates (SSE), not on individual adds
  if (purge !== false) {
    var chSet = {};
    for (var k in channels) { if (channels.hasOwnProperty(k)) chSet[k] = true; }
    var removed = [];
    for (var fn in CHANNEL_METADATA) {
      if (CHANNEL_METADATA.hasOwnProperty(fn) && !chSet[fn] && !(_removedChannels[fn])) {
        removed.push(fn);
      }
    }
    for (var ri = 0; ri < removed.length; ri++) {
      _removedChannels[removed[ri]] = true;
      if (FIELDS[removed[ri]] && FIELDS[removed[ri]].ringBuf) FIELDS[removed[ri]].ringBuf.clear();
      delete FIELDS[removed[ri]];
      delete CHANNEL_METADATA[removed[ri]];
    }
    if (removed.length > 0) _watchStructGen++;
  }
  updateWatchTable();
}

function setChannelVisible(name, visible) {
  var meta = FIELDS[name];
  if (!meta) return;
  meta.visible = !!visible;
  var chip = document.querySelector('#var-selector .chip[data-name="' + cssEscape(name) + '"]');
  if (chip) chip.classList.toggle('active', meta.visible);
  updateWatchTable();
  drawChart();
  drawMinimap();
}

function cssEscape(value) {
  if (window.CSS && window.CSS.escape) return window.CSS.escape(value);
  return String(value).replace(/["\\]/g, '\\$&');
}

function ensureChannelYState(name) {
  if (!channelYState[name]) {
    channelYState[name] = { zoom: 1, offset: 0, autoRange: true, manualMin: null, manualMax: null };
  }
  return channelYState[name];
}

function setChannelYMode(name, mode, rowEl) {
  var ys = ensureChannelYState(name);
  if (mode === 'manual') {
    var row = rowEl || findWatchRow(name);
    var rowMin = row ? parseNullableNumber((row.querySelector('.watch-y-min') || {}).value) : null;
    var rowMax = row ? parseNullableNumber((row.querySelector('.watch-y-max') || {}).value) : null;
    ys.autoRange = false;
    ys.zoom = 1;
    ys.offset = 0;
    var range = getChannelYRange(name);
    if (rowMin !== null) ys.manualMin = rowMin;
    else if (ys.manualMin === null || ys.manualMin === undefined) ys.manualMin = range ? range.yMin : 0;
    if (rowMax !== null) ys.manualMax = rowMax;
    else if (ys.manualMax === null || ys.manualMax === undefined) ys.manualMax = range ? range.yMax : 1;
  } else {
    channelYState[name] = { zoom: 1, offset: 0, autoRange: true, manualMin: null, manualMax: null };
  }
  _watchStructGen++;
  updateWatchTable();
  drawChart();
}

function updateManualYFromRow(name, rowEl) {
  var row = rowEl || findWatchRow(name);
  if (!row) return;
  var minInput = row.querySelector('.watch-y-min');
  var maxInput = row.querySelector('.watch-y-max');
  var minVal = parseNullableNumber(minInput ? minInput.value : null);
  var maxVal = parseNullableNumber(maxInput ? maxInput.value : null);
  var ys = ensureChannelYState(name);
  ys.autoRange = false;
  ys.zoom = 1;
  ys.offset = 0;
  ys.manualMin = minVal;
  ys.manualMax = maxVal;
  drawChart();
}

function findWatchRow(name) {
  var rows = document.querySelectorAll('#watch-tbody tr');
  for (var i = 0; i < rows.length; i++) {
    var nameText = rows[i].querySelector('.watch-name-text');
    if (nameText && nameText.textContent.trim() === name) return rows[i];
  }
  return null;
}

function setBufferCapacity(newCapacity) {
  newCapacity = Math.floor(Number(newCapacity));
  if (!Number.isFinite(newCapacity)) return false;
  newCapacity = Math.max(2, Math.min(200000, newCapacity));
  RING_BUFFER_CAPACITY = newCapacity;
  MAX_POINTS = newCapacity;
  window.RING_BUFFER_CAPACITY = RING_BUFFER_CAPACITY;
  window.MAX_POINTS = MAX_POINTS;
  var input = document.getElementById('buffer-input');
  if (input) input.value = String(newCapacity);
  for (var name in FIELDS) {
    if (!FIELDS.hasOwnProperty(name)) continue;
    FIELDS[name].ringBuf = resizeRingBuffer(FIELDS[name].ringBuf, newCapacity);
  }
  _watchStructGen++;
  updateUI();
  updateWatchTable();
  drawChart();
  drawMinimap();
  return true;
}

function resizeRingBuffer(oldBuf, newCapacity) {
  var next = new RingBuffer(newCapacity);
  if (!oldBuf) return next;
  var pts = oldBuf.toArray();
  var start = Math.max(0, pts.length - newCapacity);
  for (var i = start; i < pts.length; i++) {
    next.push(pts[i].t, pts[i].y);
  }
  return next;
}

// ============================================================
// Channel state model
// ============================================================
function defaultChannelState(name, color) {
  return {
    name: name,
    visible: true,
    color: color,
    yMin: null,
    yMax: null,
    yOffset: 0,
    unit: "",
    type: "number",
    size: "",
    format: "auto",
    enumValues: null,
    precision: 2,
    watchOnly: false,
    triggerEnabled: false,
    triggerLevel: 0,
    triggerEdge: "rising",
    thresholds: null
  };
}

function serializeYState(name) {
  var yState = channelYState[name];
  if (!yState) return null;
  return {
    yOffset: yState.offset || 0,
    yZoom: yState.zoom || 1,
    yAutoRange: yState.autoRange !== false,
    yMin: (yState.manualMin !== undefined) ? yState.manualMin : null,
    yMax: (yState.manualMax !== undefined) ? yState.manualMax : null
  };
}

// ============================================================
// Serialization
// ============================================================
function serializeState() {
  var channels = [];
  for (var k in FIELDS) {
    if (!FIELDS.hasOwnProperty(k)) continue;
    var meta = FIELDS[k];
    var ch = defaultChannelState(k, meta.color);
    ch.visible = meta.visible;
    ch.unit = meta.unit || "";
    ch.type = meta.type || "number";
    ch.size = (meta.size !== undefined) ? meta.size : "";
    ch.format = normalizeValueFormat(meta.format || "auto");
    if (meta.enumValues !== undefined) ch.enumValues = meta.enumValues;
    ch.precision = (meta.precision !== undefined) ? meta.precision : 2;
    ch.thresholds = normalizeThresholds(meta.thresholds);
    var yState = serializeYState(k);
    if (yState) {
      ch.yOffset = yState.yOffset;
      ch.yZoom = yState.yZoom;
      ch.yAutoRange = yState.yAutoRange;
      ch.yMin = yState.yMin;
      ch.yMax = yState.yMax;
    }
    if (meta.address !== undefined) ch.address = meta.address;
    if (meta.size !== undefined) ch.size = meta.size;
    if (meta.type !== undefined) ch.type = meta.type;
    channels.push(ch);
  }
  var state = { channels: channels };
  state.globalYView = { zoom: globalYView.zoom, offset: globalYView.offset };
  state.bufferPoints = RING_BUFFER_CAPACITY;
  state.watchColumns = JSON.parse(JSON.stringify(watchColumnState));
  state.triggerSettings = {
    source: triggerSettings.source,
    edge: triggerSettings.edge,
    level: triggerSettings.level,
    mode: triggerSettings.mode,
    preTrigger: triggerSettings.preTriggerSamples
  };
  if (typeof PARSER_MODE !== 'undefined') state.parserMode = PARSER_MODE;
  state.csvExport = {
    includeTimestamp: true,
    delimiter: ',',
    filename: 'jscope_export.csv'
  };
  if (typeof FIRMWARE_HASH !== 'undefined') state.firmwareHash = FIRMWARE_HASH;
  return state;
}

function deserializeState(json) {
  try {
    var state = (typeof json === 'string') ? JSON.parse(json) : json;
    if (!state || !state.channels) return false;
    for (var i = 0; i < state.channels.length; i++) {
      var ch = state.channels[i];
      if (!ch.name) continue;
      if (FIELDS[ch.name]) {
        FIELDS[ch.name].color = ch.color || FIELDS[ch.name].color;
        FIELDS[ch.name].visible = (ch.visible !== undefined) ? ch.visible : FIELDS[ch.name].visible;
        FIELDS[ch.name].unit = ch.unit || FIELDS[ch.name].unit || "";
        FIELDS[ch.name].type = ch.type || FIELDS[ch.name].type || "number";
        FIELDS[ch.name].size = (ch.size !== undefined) ? ch.size : FIELDS[ch.name].size;
        FIELDS[ch.name].format = normalizeValueFormat(ch.format || FIELDS[ch.name].format || "auto");
        if (ch.enumValues !== undefined) FIELDS[ch.name].enumValues = ch.enumValues;
        FIELDS[ch.name].precision = (ch.precision !== undefined) ? ch.precision : FIELDS[ch.name].precision;
        FIELDS[ch.name].thresholds = normalizeThresholds(ch.thresholds);
        if (ch.address !== undefined) FIELDS[ch.name].address = ch.address;
        if (ch.size !== undefined) FIELDS[ch.name].size = ch.size;
        if (ch.type !== undefined) FIELDS[ch.name].type = ch.type;
        if (ch.yZoom !== undefined || ch.yOffset !== undefined || ch.yAutoRange !== undefined || ch.yMin !== undefined || ch.yMax !== undefined) {
          channelYState[ch.name] = {
            zoom: ch.yZoom || 1,
            offset: ch.yOffset || 0,
            autoRange: ch.yAutoRange !== false,
            manualMin: parseNullableNumber(ch.yMin),
            manualMax: parseNullableNumber(ch.yMax)
          };
        }
      }
    }
    if (state.globalYView) {
      globalYView.zoom = state.globalYView.zoom || 1;
      globalYView.offset = state.globalYView.offset || 0;
    }
    if (state.bufferPoints !== undefined) {
      setBufferCapacity(state.bufferPoints);
    }
    if (state.watchColumns) {
      for (var colKey in state.watchColumns) {
        if (!state.watchColumns.hasOwnProperty(colKey)) continue;
        if (!watchColumnState[colKey]) watchColumnState[colKey] = {};
        if (state.watchColumns[colKey].width !== undefined) watchColumnState[colKey].width = state.watchColumns[colKey].width;
        if (state.watchColumns[colKey].visible !== undefined) watchColumnState[colKey].visible = state.watchColumns[colKey].visible;
      }
    }
    if (state.triggerSettings) {
      triggerSettings.source = state.triggerSettings.source || '';
      triggerSettings.edge = state.triggerSettings.edge || 'rising';
      triggerSettings.level = (state.triggerSettings.level !== undefined) ? state.triggerSettings.level : 0;
      triggerSettings.mode = state.triggerSettings.mode || 'auto';
      triggerSettings.preTriggerSamples = (state.triggerSettings.preTrigger !== undefined) ? state.triggerSettings.preTrigger : 1000;
      var srcSel = document.getElementById('trigger-source');
      if (srcSel) srcSel.value = triggerSettings.source;
      var edgeSel = document.getElementById('trigger-edge');
      if (edgeSel) edgeSel.value = triggerSettings.edge;
      var levelInput = document.getElementById('trigger-level');
      if (levelInput) levelInput.value = triggerSettings.level;
      var modeSel = document.getElementById('trigger-mode');
      if (modeSel) modeSel.value = triggerSettings.mode;
      var pretrigInput = document.getElementById('trigger-pretrig');
      if (pretrigInput) pretrigInput.value = triggerSettings.preTriggerSamples;
    }
    if (state.csvExport) {
      window._csvExportConfig = state.csvExport;
    }
    updateUI();
    _watchStructGen++;
    updateWatchTable();
    drawChart();
    return true;
  } catch(e) {
    return false;
  }
}

// ============================================================
// CSV/PNG export
// ============================================================
function exportCSV() {
  var names = Object.keys(FIELDS).sort();
  if (names.length === 0) return;
  var csvCfg = window._csvExportConfig || { includeTimestamp: true, delimiter: ',', filename: 'jscope_export.csv' };
  var delim = csvCfg.delimiter || ',';
  var headers = [];
  if (csvCfg.includeTimestamp !== false) headers.push('timestamp');
  for (var i = 0; i < names.length; i++) {
    var ch = FIELDS[names[i]];
    headers.push(names[i] + (ch.unit ? ' (' + ch.unit + ')' : ''));
  }
  var rows = [headers.join(delim)];

  // Collect all points per channel with index tracking for O(n) merge
  var channelArrays = [];
  var allTimes = [];
  for (var ni = 0; ni < names.length; ni++) {
    var pts = FIELDS[names[ni]].ringBuf.toArray();
    var timeValPairs = [];
    for (var pi = 0; pi < pts.length; pi++) {
      timeValPairs.push(pts[pi]);
      allTimes.push(pts[pi].t);
    }
    channelArrays.push(timeValPairs);
  }

  // Sort all unique timestamps and merge using per-channel binary search
  allTimes.sort(function(a, b) { return a - b; });
  var sortedTimes = [];
  for (var i = 0; i < allTimes.length; i++) {
    if (i === 0 || Math.abs(allTimes[i] - allTimes[i - 1]) > 1e-9) {
      sortedTimes.push(allTimes[i]);
    }
  }

  for (var ti = 0; ti < sortedTimes.length; ti++) {
    var t = sortedTimes[ti];
    var row = [];
    if (csvCfg.includeTimestamp !== false) row.push(t.toFixed(6));
    for (var ni = 0; ni < names.length; ni++) {
      var pts = channelArrays[ni];
      var val = '';
      // Find nearest point within half-sample tolerance
      var lo = 0, hi = pts.length - 1, best = -1, bestDist = Infinity;
      while (lo <= hi) {
        var mid = (lo + hi) >> 1;
        var d = Math.abs(pts[mid].t - t);
        if (d < bestDist) { bestDist = d; best = mid; }
        if (pts[mid].t < t) lo = mid + 1;
        else hi = mid - 1;
      }
      if (best >= 0 && bestDist < 0.5) {
        val = pts[best].y.toFixed(
          (FIELDS[names[ni]].precision !== undefined) ? FIELDS[names[ni]].precision : 4
        );
      }
      row.push(val);
    }
    rows.push(row.join(delim));
  }

  var csvContent = rows.join('\n');
  var blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url;
  a.download = csvCfg.filename || 'jscope_export.csv';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function exportPNG() {
  drawChart();
  var dataURL = canvas.toDataURL('image/png');
  var a = document.createElement('a');
  a.href = dataURL;
  a.download = 'jscope_chart.png';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

// ============================================================
// Data processing (with RingBuffer + batch optimization)
// ============================================================
var updatePending = false;
var renderGeneration = 0;
var swTimeOrigin = null; // SuperWatch mode: subtract first _t so axis starts at 0
function processPoint(point) {
  if (paused) return;
  var pointRenderGeneration = renderGeneration;
  var elapsed = point._t || (performance.now() / 1000);
  var t;
  if (IS_SUPERWATCH_MODE && point._t !== undefined) {
    var rawT = Number(point._t);
    if (!Number.isFinite(rawT)) rawT = 0;
    // Reset origin on backward jump (server restart) or first sample
    if (swTimeOrigin === null || rawT < swTimeOrigin - 1) {
      swTimeOrigin = rawT;
      // Clear ring buffers to avoid mixing old/new time bases
      for (var ck in FIELDS) { if (FIELDS[ck].ringBuf) FIELDS[ck].ringBuf.clear(); }
    }
    t = rawT - swTimeOrigin;
  } else {
    if (!tStart) tStart = elapsed;
    t = elapsed - tStart;
  }
  var capturePoint = {};
  for (var pk in point) {
    if (!point.hasOwnProperty(pk)) continue;
    capturePoint[pk] = point[pk];
  }
  capturePoint._viewT = t;
  if (!checkTrigger(capturePoint)) return;

  var fieldKeys = [];
  for (var k in point) {
    if (!point.hasOwnProperty(k) || k[0] === '_') continue;
    fieldKeys.push(k);
  }

  // Batch optimization for multi-channel sampling (Task 7I)
  // Collect all new fields in one pass before creating ring buffers
  for (var fi = 0; fi < fieldKeys.length; fi++) {
    var k = fieldKeys[fi];
    var v = point[k];
    if (!FIELDS[k]) {
      if (_removedChannels[k]) continue; // Skip explicitly removed channels
      if (!CHANNEL_METADATA[k]) continue; // Only auto-create registered channels
      if (colorIdx >= MAX_CHANNELS) continue; // Enforce channel limit
      FIELDS[k] = {
        color: COLORS[colorIdx % COLORS.length],
        ringBuf: new RingBuffer(RING_BUFFER_CAPACITY),
        visible: true,
        unit: "",
        type: "number",
        size: "",
        format: "auto",
        enumValues: null,
        precision: 2,
        thresholds: null
      };
      if (CHANNEL_METADATA[k]) {
        if (CHANNEL_METADATA[k].type !== undefined) FIELDS[k].type = CHANNEL_METADATA[k].type;
        if (CHANNEL_METADATA[k].size !== undefined) FIELDS[k].size = CHANNEL_METADATA[k].size;
        if (CHANNEL_METADATA[k].unit !== undefined) FIELDS[k].unit = CHANNEL_METADATA[k].unit;
        if (CHANNEL_METADATA[k].format !== undefined) FIELDS[k].format = normalizeValueFormat(CHANNEL_METADATA[k].format);
        if (CHANNEL_METADATA[k].enumValues !== undefined) FIELDS[k].enumValues = CHANNEL_METADATA[k].enumValues;
      }
      colorIdx++;
      _watchStructGen++;
    }
    var m = FIELDS[k];
    v = Number(v);
    if (!Number.isFinite(v)) continue;
    m.ringBuf.push(t, v);
  }

  // Raw log
  rawLogLineCount++;
  rawLogEl.textContent += JSON.stringify(point) + '\n';
  rawLogCountEl.textContent = rawLogLineCount + ' lines';
  if (rawLogOpen) rawLogEl.scrollTop = rawLogEl.scrollHeight;

  updateTriggerSourceOptions();
  if (point._t) {
    var previousInterval = estimatedInterval;
    var previousTime = window._lastSampleTime || 0;
    if (previousTime > 0 && point._t > previousTime) {
      var dt = point._t - previousTime;
      estimatedInterval = previousInterval > 0 ? previousInterval * 0.8 + dt * 0.2 : dt;
      estimatedRate = estimatedInterval > 0 ? 1 / estimatedInterval : 0;
      updateSampleRateBadge(estimatedInterval, estimatedRate);
    }
    window._lastSampleTime = point._t;
  }

  if (!updatePending) {
    updatePending = true;
    requestAnimationFrame(function() {
      try {
        if (paused || pointRenderGeneration !== renderGeneration) return;
        drawChart();
        drawMinimap();
        updateUI();
        updateWatchTable();
      } catch (err) {
        console.error("RTT render error:", err);
      } finally {
        updatePending = false;
      }
    });
  }
}

// ============================================================
// Get visible time range (with timeline zoom/offset)
// ============================================================
function getFullTimeRange() {
  var tMax = 0, tMin = Infinity;
  for (var k in FIELDS) {
    var pts = FIELDS[k].ringBuf;
    if (pts.count === 0) continue;
    var oldest = pts.oldest();
    var latest = pts.latest();
    if (oldest && oldest.t < tMin) tMin = oldest.t;
    if (latest && latest.t > tMax) tMax = latest.t;
  }
  if (!Number.isFinite(tMin)) tMin = 0;
  if (tMax - tMin < 1) tMin = tMax - 1;
  return { tMin: tMin, tMax: tMax };
}

function getVisibleTimeRange() {
  var full = getFullTimeRange();
  var range = full.tMax - full.tMin;
  if (range <= 0) return full;

  // Apply zoom
  var visibleRange = range / timelineView.zoom;
  var offset = timelineView.offset * (range - visibleRange);
  return {
    tMin: full.tMin + offset,
    tMax: full.tMin + offset + visibleRange
  };
}

// ============================================================
// Per-channel Y-axis (Task 5I)
// ============================================================
function getChannelYRange(name) {
  var m = FIELDS[name];
  if (!m || m.ringBuf.count < 2) return null;
  var ys = ensureChannelYState(name);

  var baseMin = m.ringBuf._min;
  var baseMax = m.ringBuf._max;
  if (!Number.isFinite(baseMin)) baseMin = 0;
  if (!Number.isFinite(baseMax)) baseMax = 1;
  var pad = (baseMax - baseMin) * 0.1 || 1;
  baseMin -= pad; baseMax += pad;

  if (
    ys.autoRange === false &&
    Number.isFinite(Number(ys.manualMin)) &&
    Number.isFinite(Number(ys.manualMax)) &&
    Number(ys.manualMax) > Number(ys.manualMin)
  ) {
    return { yMin: Number(ys.manualMin), yMax: Number(ys.manualMax) };
  }
  if (ys.zoom === 1) return { yMin: baseMin, yMax: baseMax };

  var range = baseMax - baseMin;
  var center = (baseMin + baseMax) / 2 + ys.offset;
  var zoomedRange = range / ys.zoom;
  return {
    yMin: center - zoomedRange / 2,
    yMax: center + zoomedRange / 2
  };
}

function resetChannelY(name) {
  channelYState[name] = { zoom: 1, offset: 0, autoRange: true, manualMin: null, manualMax: null };
}

function formatTimeAxisValue(seconds) {
  if (timeUnit === 'us') return Math.round(seconds * 1000000) + 'us';
  if (timeUnit === 's') return seconds.toFixed(3) + 's';
  return (seconds * 1000).toFixed(1) + 'ms';
}

// ============================================================
// Canvas chart drawing (enhanced with per-channel Y, timeline, cursors)
// ============================================================
function drawChart() {
  if (!resize()) return;
  resizeMinimap();
  var W = canvas.clientWidth || parseFloat(canvas.style.width);
  var H = canvas.clientHeight || parseFloat(canvas.style.height);
  if (!Number.isFinite(W) || !Number.isFinite(H) || W <= 0 || H <= 0) return;
  ctx.clearRect(0, 0, W, H);

  // Get visible time range (with zoom/offset)
  var tr = getVisibleTimeRange();
  var tMin = tr.tMin, tMax = tr.tMax;

  // Global Y range (for shared Y mode when no per-channel zoom active)
  var yMin = Infinity, yMax = -Infinity;
  var hasData = false;
  for (var k in FIELDS) {
    if (!FIELDS[k].visible) continue;
    var pts = FIELDS[k].ringBuf;
    if (pts.count < 2) continue;
    hasData = true;
    if (Number.isFinite(pts._min)) yMin = Math.min(yMin, pts._min);
    if (Number.isFinite(pts._max)) yMax = Math.max(yMax, pts._max);
  }
  if (!hasData) return;
  var pad = (yMax - yMin) * 0.1 || 1;
  yMin -= pad; yMax += pad;

  // Apply global Y-axis zoom (oscilloscope style: all channels + grid zoom together)
  if (globalYView.zoom !== 1) {
    var yCenter = (yMin + yMax) / 2 + globalYView.offset;
    var yRange = (yMax - yMin) / globalYView.zoom;
    yMin = yCenter - yRange / 2;
    yMax = yCenter + yRange / 2;
  }

  // Count visible channels
  var visChNames = Object.keys(FIELDS).sort().filter(function(k) {
    return FIELDS[k].visible && FIELDS[k].ringBuf.count >= 2;
  });
  var mr = 16, mt = 8, mb = 32, ml = 16;
  var pw = W - ml - mr;
  var ph = H - mt - mb;
  if (pw <= 0 || ph <= 0) return;

  function tx(v) { return ml + (v - tMin) / (tMax - tMin || 1) * pw; }
  function tyGlobal(v) { return mt + ph - (v - yMin) / (yMax - yMin || 1) * ph; }

  // Per-channel Y: each channel normalized to its own min/max by default
  // (avoids small signals being crushed when channels have very different ranges)
  function tyForChannel(v, name) {
    if (!name) return tyGlobal(v);
    var yr = getChannelYRange(name);
    if (!yr) return tyGlobal(v);
    return mt + ph - (v - yr.yMin) / (yr.yMax - yr.yMin || 1) * ph;
  }

  // Grid — horizontal lines at fixed pixel positions
  ctx.strokeStyle = GRID_COLOR;
  ctx.lineWidth = 0.5;
  for (var i = 0; i <= 5; i++) {
    var yp = Math.round(mt + ph * i / 5) + 0.5;
    ctx.beginPath();
    ctx.moveTo(ml, yp); ctx.lineTo(ml + pw, yp);
    ctx.stroke();
  }
  // Time grid
  for (var i = 0; i <= 5; i++) {
    var xv = tMin + (tMax - tMin) * i / 5;
    var xp = Math.round(tx(xv)) + 0.5;
    ctx.beginPath();
    ctx.moveTo(xp, mt); ctx.lineTo(xp, mt + ph);
    ctx.stroke();
    ctx.fillStyle = TEXT_DIM;
    ctx.font = '11px ' + getComputedStyle(document.body).getPropertyValue('--font-mono');
    ctx.textAlign = 'center';
    ctx.fillText(formatTimeAxisValue(xv), xp, mt + ph + 16);
  }

  ctx.fillStyle = TEXT_DIM;
  ctx.font = '11px ' + getComputedStyle(document.body).getPropertyValue('--font-body');
  ctx.textAlign = 'center';
  ctx.fillText('time (' + timeUnit + ')', ml + pw/2, H - 4);
  ctx.save();
  ctx.translate(10, mt + ph/2);
  ctx.rotate(-Math.PI/2);
  ctx.fillText('value', 0, 0);
  ctx.restore();

  ctx.save();
  ctx.beginPath();
  ctx.rect(ml, mt, pw, ph);
  ctx.clip();

  var names = Object.keys(FIELDS).sort();
  for (var ni = 0; ni < names.length; ni++) {
    var name = names[ni];
    var meta = FIELDS[name];
    if (!meta.visible || meta.ringBuf.count < 2) continue;

    ctx.strokeStyle = meta.color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    var started = false;
    var pts = meta.ringBuf.toArray();
    for (var i = 0; i < pts.length; i++) {
      var p = pts[i];
      var sx = tx(p.t), sy = tyForChannel(p.y, name);
      if (sx < ml - 10 || sx > ml + pw + 10) continue;
      if (!started) { ctx.moveTo(sx, sy); started = true; }
      else ctx.lineTo(sx, sy);
    }
    ctx.stroke();
  }
  ctx.restore();

  // Per-channel Y labels at right edge of chart, staggered to avoid overlap
  // Max labels pinned at top, Min labels pinned at bottom, each 14px apart
  for (var ni = 0; ni < visChNames.length; ni++) {
    var chName = visChNames[ni];
    var chMeta = FIELDS[chName];
    var chRange = getChannelYRange(chName);
    if (!chRange) continue;
    var rng = chRange.yMax - chRange.yMin;
    var dec = (rng < 10) ? 2 : (rng < 1000 ? 1 : 0);
    ctx.fillStyle = chMeta.color;
    ctx.font = 'bold 10px ' + getComputedStyle(document.body).getPropertyValue('--font-mono');
    ctx.textAlign = 'right';
    var lx = ml + pw - 4;
    // Max label at top area
    ctx.fillText(chRange.yMax.toFixed(dec), lx, mt + 10 + ni * 14);
    // Min label at bottom area
    ctx.fillText(chRange.yMin.toFixed(dec), lx, mt + ph - (visChNames.length - 1 - ni) * 14);
  }

  // Hover probe: vertical dashed line
  if (hoverProbe.active) {
    var probeX = hoverProbe.mx;
    if (probeX >= ml && probeX <= ml + pw) {
      ctx.save();
      ctx.strokeStyle = 'rgba(20,20,19,0.35)';
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(probeX + 0.5, mt);
      ctx.lineTo(probeX + 0.5, mt + ph);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.restore();
    }
  }

  // Trigger level line
  if (triggerSettings.enabled && triggerSettings.source) {
    var trigY = tyGlobal(triggerSettings.level);
    if (trigY >= mt && trigY <= mt + ph) {
      ctx.save();
      ctx.strokeStyle = '#b53333';
      ctx.lineWidth = 1;
      ctx.setLineDash([6, 4]);
      ctx.beginPath();
      ctx.moveTo(ml, trigY);
      ctx.lineTo(W - mr, trigY);
      ctx.stroke();
      ctx.setLineDash([]);

      ctx.fillStyle = '#b53333';
      ctx.font = 'bold 10px ' + getComputedStyle(document.body).getPropertyValue('--font-mono');
      ctx.textAlign = 'right';
      ctx.fillText('T: ' + triggerSettings.level.toFixed(2), W - mr - 4, trigY - 4);

      ctx.beginPath();
      ctx.moveTo(ml, trigY);
      ctx.lineTo(ml - 8, trigY - 5);
      ctx.lineTo(ml - 8, trigY + 5);
      ctx.closePath();
      ctx.fillStyle = '#b53333';
      ctx.fill();

      ctx.restore();
    }
  }

  // Measurement cursors A/B (Task 5I)
  if (cursorState.enabled && cursorState.a !== null) {
    var cax = tx(cursorState.a.t);
    if (cax >= ml && cax <= ml + pw) {
      ctx.save();
      ctx.strokeStyle = '#3898ec';
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(cax, mt);
      ctx.lineTo(cax, mt + ph);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = '#3898ec';
      ctx.font = 'bold 9px ' + getComputedStyle(document.body).getPropertyValue('--font-mono');
      ctx.textAlign = 'center';
      ctx.fillText('A', cax, mt - 2);
      ctx.restore();
    }
  }
  if (cursorState.enabled && cursorState.b !== null) {
    var cbx = tx(cursorState.b.t);
    if (cbx >= ml && cbx <= ml + pw) {
      ctx.save();
      ctx.strokeStyle = '#c96442';
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(cbx, mt);
      ctx.lineTo(cbx, mt + ph);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = '#c96442';
      ctx.font = 'bold 9px ' + getComputedStyle(document.body).getPropertyValue('--font-mono');
      ctx.textAlign = 'center';
      ctx.fillText('B', cbx, mt - 2);
      ctx.restore();
    }
  }
  syncCursorOverlay('cursor-a', cursorState.enabled ? cursorState.a : null, tx, ml, ml + pw);
  syncCursorOverlay('cursor-b', cursorState.enabled ? cursorState.b : null, tx, ml, ml + pw);
  updateCursorReadout();

  // Channel legend (only visible, plottable channels)
  var lx = ml + 8, ly = mt + 8;
  for (var ni = 0; ni < names.length; ni++) {
    var name = names[ni];
    var meta = FIELDS[name];
    if (!meta.visible || (CHANNEL_METADATA[name] && CHANNEL_METADATA[name].source === 'struct')) continue;
    ctx.fillStyle = meta.color;
    ctx.fillRect(lx, ly, 12, 3);
    ctx.fillStyle = '#141413';
    ctx.font = '11px ' + getComputedStyle(document.body).getPropertyValue('--font-body');
    ctx.textAlign = 'left';
    ctx.fillText(name, lx + 16, ly + 6);
    ly += 16;
    if (ly > mt + ph - 10) break;
  }

  // ============================================================
  // Mouse interactions: tooltip, Y-axis zoom, timeline pan, cursors
  // ============================================================
  canvas.onmousemove = function(e) {
    var rect = canvas.getBoundingClientRect();
    var mx = e.clientX - rect.left;
    var my = e.clientY - rect.top;

    // Cursor dragging
    if (cursorState.dragging) {
      var hoverT = tMin + (mx - ml) / pw * (tMax - tMin);
      hoverT = Math.max(tMin, Math.min(tMax, hoverT));
      if (cursorState.dragging === 'a') cursorState.a = { t: hoverT };
      else if (cursorState.dragging === 'b') cursorState.b = { t: hoverT };
      updateCursorReadout();
      drawChart();
      return;
    }

    // Update hover probe position
    if (hoverProbe.active) {
      hoverProbe.mx = mx;
      hoverProbe.my = my;
    }

    if (!hoverProbe.active || mx < ml || mx > ml + pw || my < mt || my > mt + ph) {
      tooltip.style.display = 'none';
      if (hoverProbe.active) drawChart();
      return;
    }
    var hoverT = tMin + (mx - ml) / pw * (tMax - tMin);
    var lines = ['<span class="tooltip-row" style="color:var(--dim);font-size:11px">' + escapeHtml(formatTimeAxisValue(hoverT)) + '</span>'];
    for (var k in FIELDS) {
      var pts = FIELDS[k].ringBuf.toArray();
      if (!FIELDS[k].visible || pts.length < 1) continue;
      var best = null, bestDist = Infinity;
      for (var i = 0; i < pts.length; i++) {
        var d = Math.abs(pts[i].t - hoverT);
        if (d < bestDist) { bestDist = d; best = pts[i]; }
      }
      if (best && bestDist < (tMax - tMin) / pw * 15) {
        var tipVal = formatTypedValue(best.y, FIELDS[k]);
        var enumName = _resolveEnumName(k, best.y);
        if (enumName) tipVal = enumName + ' (' + tipVal + ')';
        lines.push(
          '<span class="tooltip-row">' +
          '<span class="tooltip-swatch" style="background:' + FIELDS[k].color + '"></span>' +
          '<span>' + escapeHtml(k) + ': ' + escapeHtml(tipVal) + '</span>' +
          '</span>'
        );
      }
    }
    if (lines.length) {
      tooltip.innerHTML = lines.join('');
      tooltip.style.display = 'block';
      tooltip.style.left = Math.min(mx + 12, W - 180) + 'px';
      tooltip.style.top = Math.max(0, my - 8) + 'px';
    } else { tooltip.style.display = 'none'; }
    drawChart();
  };
  canvas.onmouseleave = function() { tooltip.style.display = 'none'; hoverProbe.active = false; drawChart(); };

  // Mouse wheel: Y-axis zoom, horizontal zoom for timeline
  canvas.onwheel = function(e) {
    e.preventDefault();
    var rect = canvas.getBoundingClientRect();
    var mx = e.clientX - rect.left;
    var my = e.clientY - rect.top;

    if (e.shiftKey) {
      // Shift+wheel: horizontal timeline zoom
      var zoomFactor = e.deltaY > 0 ? 0.8 : 1.25;
      timelineView.zoom = Math.max(1, Math.min(100, timelineView.zoom * zoomFactor));
      // Zoom toward mouse position
      var mouseRatio = (mx - ml) / pw;
      var newOffset = timelineView.offset + (mouseRatio - 0.5) * (1 - 1 / zoomFactor) * 0.1;
      timelineView.offset = Math.max(0, Math.min(1, newOffset));
      drawChart();
      drawMinimap();
      return;
    }

    if (e.ctrlKey) {
      // Ctrl+wheel: per-channel Y-axis zoom
      var hoverT = tMin + (mx - ml) / pw * (tMax - tMin);
      var closestChannel = null;
      var closestDist = Infinity;
      for (var k in FIELDS) {
        if (!FIELDS[k].visible) continue;
        var pts = FIELDS[k].ringBuf.toArray();
        for (var i = 0; i < pts.length; i++) {
          var d = Math.abs(pts[i].t - hoverT);
          if (d < closestDist) { closestDist = d; closestChannel = k; }
        }
      }
      if (closestChannel) {
        if (!channelYState[closestChannel]) {
          channelYState[closestChannel] = { zoom: 1, offset: 0, autoRange: true, manualMin: null, manualMax: null };
        }
        var ys = channelYState[closestChannel];
        ys.autoRange = false;
        ys.manualMin = null;
        ys.manualMax = null;
        var yZoomFactor = e.deltaY > 0 ? 0.8 : 1.25;
        ys.zoom = Math.max(0.1, Math.min(100, ys.zoom * yZoomFactor));
        drawChart();
      }
      return;
    }

    // Default wheel: zoom all visible channels' Y together (percentage-based)
    var yZoomFactor = e.deltaY > 0 ? 0.8 : 1.25;
    for (var k in FIELDS) {
      if (!FIELDS[k].visible || FIELDS[k].ringBuf.count < 2) continue;
      var ys = ensureChannelYState(k);
      ys.autoRange = false;
      ys.manualMin = null;
      ys.manualMax = null;
      ys.zoom = Math.max(0.1, Math.min(100, ys.zoom * yZoomFactor));
    }
    drawChart();
  };

  // Double-click: reset Y zoom
  canvas.ondblclick = function(e) {
    var rect = canvas.getBoundingClientRect();
    var mx = e.clientX - rect.left;
    var my = e.clientY - rect.top;
    if (mx < ml || mx > ml + pw || my < mt || my > mt + ph) return;

    if (e.ctrlKey) {
      // Ctrl+double-click: reset everything
      channelYState = {};
    } else {
      // Double-click: reset all channels' Y zoom
      for (var k in channelYState) {
        channelYState[k] = { zoom: 1, offset: 0, autoRange: true, manualMin: null, manualMax: null };
      }
    }
    drawChart();
  };

  // Mouse down: timeline pan, trigger drag, cursor drag, or hover probe
  canvas.onmousedown = function(e) {
    var rect = canvas.getBoundingClientRect();
    var mx = e.clientX - rect.left;
    var my = e.clientY - rect.top;

    // Track left-button down position for click detection
    if (e.button === 0 && mx >= ml && mx <= ml + pw && my >= mt && my <= mt + ph && !e.altKey) {
      probeDownPos = { x: mx, y: my, wasActive: hoverProbe.active };
    } else {
      probeDownPos = null;
    }

    // Cursor A/B drag
    if (cursorState.enabled && mx >= ml && mx <= ml + pw && my >= mt && my <= mt + ph) {
      var hoverT = tMin + (mx - ml) / pw * (tMax - tMin);
      if (cursorState.a && Math.abs(tx(cursorState.a.t) - mx) < 6) {
        cursorState.dragging = 'a'; e.preventDefault(); return;
      }
      if (cursorState.b && Math.abs(tx(cursorState.b.t) - mx) < 6) {
        cursorState.dragging = 'b'; e.preventDefault(); return;
      }
    }

    // Trigger level line dragging
    if (triggerSettings.enabled && triggerSettings.source) {
      var trigY = tyGlobal(triggerSettings.level);
      if (Math.abs(my - trigY) < 8 && my >= mt && my <= mt + ph) {
        draggingTrigger = true;
        e.preventDefault();
        return;
      }
    }

    // Timeline pan (middle button, alt+left, or plain left when zoomed in)
    if (e.button === 1 || (e.button === 0 && e.altKey) || (e.button === 0 && !e.ctrlKey && !e.shiftKey && !spaceHeld && timelineView.zoom > 1 && mx >= ml && mx <= ml + pw && my >= mt && my <= mt + ph)) {
      timelineView.dragging = true;
      timelineView.dragStartX = mx;
      timelineView.dragStartOffset = timelineView.offset;
      e.preventDefault();
      return;
    }

    // Space+left: hand-tool pan (any zoom level)
    if (e.button === 0 && spaceHeld) {
      timelineView.dragging = true;
      timelineView.dragStartX = mx;
      timelineView.dragStartOffset = timelineView.offset;
      e.preventDefault();
      return;
    }
  };

  canvas.onmousemove = (function(origFn) {
    return function(e) {
      if (cursorState.dragging) {
        var rect = canvas.getBoundingClientRect();
        var mx = e.clientX - rect.left;
        var hoverT = tMin + (mx - ml) / pw * (tMax - tMin);
        hoverT = Math.max(tMin, Math.min(tMax, hoverT));
        if (cursorState.dragging === 'a') cursorState.a = { t: hoverT };
        else if (cursorState.dragging === 'b') cursorState.b = { t: hoverT };
        updateCursorReadout();
        drawChart();
        return;
      }
      if (draggingTrigger) {
        var rect = canvas.getBoundingClientRect();
        var my = e.clientY - rect.top;
        var newLevel = yMin + (1 - (my - mt) / ph) * (yMax - yMin);
        triggerSettings.level = newLevel;
        document.getElementById('trigger-level').value = newLevel.toFixed(2);
        drawChart();
        return;
      }
      if (timelineView.dragging) {
        canvas.style.cursor = 'grabbing';
        var rect = canvas.getBoundingClientRect();
        var mx = e.clientX - rect.left;
        var dx = (mx - timelineView.dragStartX) / pw;
        var full = getFullTimeRange();
        var visibleRange = (full.tMax - full.tMin) / timelineView.zoom;
        var offsetDelta = -dx * visibleRange / (full.tMax - full.tMin);
        timelineView.offset = Math.max(0, Math.min(1, timelineView.dragStartOffset + offsetDelta));
        drawChart();
        drawMinimap();
        return;
      }
      // Show ew-resize cursor when hovering near a measurement cursor line
      if (cursorState.enabled && !cursorState.dragging) {
        var r2 = canvas.getBoundingClientRect();
        var mx2 = e.clientX - r2.left;
        var nearCursor = false;
        if (cursorState.a && Math.abs(tx(cursorState.a.t) - mx2) < 6) nearCursor = true;
        if (cursorState.b && Math.abs(tx(cursorState.b.t) - mx2) < 6) nearCursor = true;
        canvas.style.cursor = nearCursor ? 'ew-resize' : (spaceHeld ? 'grab' : '');
      }
      origFn.call(this, e);
    };
  })(canvas.onmousemove);
}

// ============================================================
// Module-scope event listeners (extracted from drawChart to prevent leak)
// ============================================================
document.addEventListener('keydown', function(e) {
  if (e.key === ' ' && !e.repeat && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') {
    spaceHeld = true;
    canvas.style.cursor = 'grab';
    e.preventDefault();
  }
});
document.addEventListener('keyup', function(e) {
  if (e.key === ' ') {
    spaceHeld = false;
    canvas.style.cursor = '';
  }
});

window.addEventListener('mouseup', function(e) {
  if (probeDownPos && !timelineView.dragging && !draggingTrigger && !cursorState.dragging) {
    var rect = canvas.getBoundingClientRect();
    var mx = e.clientX - rect.left;
    var my = e.clientY - rect.top;
    var dist = Math.sqrt(Math.pow(mx - probeDownPos.x, 2) + Math.pow(my - probeDownPos.y, 2));
    if (dist < 5) {
      if (probeDownPos.wasActive) {
        hoverProbe.active = false;
        tooltip.style.display = 'none';
      } else {
        hoverProbe.active = true;
        hoverProbe.mx = mx;
        hoverProbe.my = my;
      }
      drawChart();
    }
  }
  probeDownPos = null;
  draggingTrigger = false;
  timelineView.dragging = false;
  cursorState.dragging = null;
  canvas.style.cursor = spaceHeld ? 'grab' : '';
});

window.addEventListener('keydown', function(e) {
  if (e.key === 'Escape' && hoverProbe.active) {
    hoverProbe.active = false;
    tooltip.style.display = 'none';
    drawChart();
  }
});

// ============================================================
// Cursor readout (Task 5I)
// ============================================================
function updateCursorReadout() {
  if (!cursorState.enabled || !cursorState.a || !cursorState.b) {
    cursorReadout.textContent = '';
    if (cursorMeasurePanel) cursorMeasurePanel.style.display = 'none';
    return;
  }
  var dt = Math.abs(cursorState.b.t - cursorState.a.t);
  var freq = dt > 0 ? (1 / dt).toFixed(2) : '-';
  var names = Object.keys(FIELDS).sort();
  var deltaLimit = (window.innerWidth <= 640) ? 2 : 6;

  // Minimap compact readout
  var lines = ['dT=' + dt.toFixed(4) + 's', 'f=' + freq + 'Hz'];
  var deltaCount = 0;
  for (var i = 0; i < names.length; i++) {
    var name = names[i];
    var meta = FIELDS[name];
    if (!meta || !meta.visible || !meta.ringBuf || meta.ringBuf.count < 1) continue;
    var av = sampleValueAt(meta.ringBuf, cursorState.a.t);
    var bv = sampleValueAt(meta.ringBuf, cursorState.b.t);
    if (av === null || bv === null) continue;
    if (deltaCount >= deltaLimit) {
      lines.push('+' + (names.length - i) + ' ch');
      break;
    }
    lines.push(name + ' d=' + (bv - av).toFixed(meta.precision || 2));
    deltaCount++;
  }
  cursorReadout.textContent = lines.join('  ');

  // Floating measurement panel
  if (cursorMeasurePanel) {
    var html = '';
    if (cursorState.mode === 'value') {
      // Value mode: emphasize per-channel V@A, V@B, dV
      html += '<div class="cm-row"><span class="cm-label">dT</span><span class="cm-value">' + dt.toFixed(4) + 's</span></div>';
      html += '<div class="cm-divider"></div>';
      var vc = 0;
      for (var j = 0; j < names.length; j++) {
        var vn = names[j];
        var vm = FIELDS[vn];
        if (!vm || !vm.visible || !vm.ringBuf || vm.ringBuf.count < 1) continue;
        var va = sampleValueAt(vm.ringBuf, cursorState.a.t);
        var vb = sampleValueAt(vm.ringBuf, cursorState.b.t);
        if (va === null || vb === null) continue;
        if (vc >= deltaLimit) break;
        var prec = vm.precision || 2;
        html += '<div class="cm-row">';
        html += '<span class="cm-label" style="color:' + vm.color + '">' + escapeHtml(vn) + '</span>';
        html += '<span class="cm-value">A:' + va.toFixed(prec) + ' B:' + vb.toFixed(prec) + ' dV:' + (vb - va).toFixed(prec) + '</span>';
        html += '</div>';
        vc++;
      }
    } else {
      // Time mode (default): dT, frequency, per-channel delta
      html += '<div class="cm-row"><span class="cm-label">dT</span><span class="cm-value">' + dt.toFixed(4) + 's</span></div>';
      html += '<div class="cm-row"><span class="cm-label">freq</span><span class="cm-value">' + freq + 'Hz</span></div>';
      html += '<div class="cm-divider"></div>';
      var tc = 0;
      for (var k = 0; k < names.length; k++) {
        var tn = names[k];
        var tm = FIELDS[tn];
        if (!tm || !tm.visible || !tm.ringBuf || tm.ringBuf.count < 1) continue;
        var ta = sampleValueAt(tm.ringBuf, cursorState.a.t);
        var tb = sampleValueAt(tm.ringBuf, cursorState.b.t);
        if (ta === null || tb === null) continue;
        if (tc >= deltaLimit) break;
        html += '<div class="cm-row">';
        html += '<span class="cm-label" style="color:' + tm.color + '">' + escapeHtml(tn) + '</span>';
        html += '<span class="cm-value">d=' + (tb - ta).toFixed(tm.precision || 2) + '</span>';
        html += '</div>';
        tc++;
      }
    }
    cursorMeasurePanel.innerHTML = html;
    cursorMeasurePanel.style.display = 'block';
  }
}

function sampleValueAt(ringBuf, t) {
  var pts = ringBuf.toArray();
  if (!pts.length) return null;
  var best = null;
  var bestDist = Infinity;
  for (var i = 0; i < pts.length; i++) {
    var d = Math.abs(pts[i].t - t);
    if (d < bestDist) {
      bestDist = d;
      best = pts[i];
    }
  }
  return best ? best.y : null;
}

function syncCursorOverlay(id, cursor, tx, leftBound, rightBound) {
  var el = document.getElementById(id);
  if (!el) return;
  if (!cursor) {
    el.style.display = 'none';
    return;
  }
  var x = tx(cursor.t);
  if (!Number.isFinite(x) || x < leftBound || x > rightBound) {
    el.style.display = 'none';
    return;
  }
  el.style.display = 'block';
  el.style.left = x + 'px';
}

// ============================================================
// Minimap (Task 5I)
// ============================================================
function drawMinimap() {
  resizeMinimap();
  var W = minimapCanvas.clientWidth;
  var H = minimapCanvas.clientHeight;
  if (W <= 0 || H <= 0) return;

  minimapCtx.clearRect(0, 0, W, H);

  var full = getFullTimeRange();
  var range = full.tMax - full.tMin;
  if (range <= 0) return;

  function mtx(v) { return (v - full.tMin) / range * W; }

  // Draw data traces
  var names = Object.keys(FIELDS).sort();
  minimapCtx.globalAlpha = 0.4;
  for (var ni = 0; ni < names.length; ni++) {
    var meta = FIELDS[names[ni]];
    if (!meta.visible || meta.ringBuf.count < 2) continue;
    minimapCtx.strokeStyle = meta.color;
    minimapCtx.lineWidth = 1;
    minimapCtx.beginPath();
    var pts = meta.ringBuf.toArray();
    // Downsample for minimap performance
    var step = Math.max(1, Math.floor(pts.length / W));
    var started = false;
    for (var i = 0; i < pts.length; i += step) {
      var px = mtx(pts[i].t);
      // Normalize Y to minimap height
      var bufMin = Number.isFinite(meta.ringBuf._min) ? meta.ringBuf._min : 0;
      var bufMax = Number.isFinite(meta.ringBuf._max) ? meta.ringBuf._max : 1;
      var yRange = bufMax - bufMin || 1;
      var py = H - ((pts[i].y - bufMin) / yRange) * (H - 4) - 2;
      if (!started) { minimapCtx.moveTo(px, py); started = true; }
      else minimapCtx.lineTo(px, py);
    }
    minimapCtx.stroke();
  }
  minimapCtx.globalAlpha = 1;

  // Viewport indicator
  var vis = getVisibleTimeRange();
  var vpLeft = mtx(vis.tMin);
  var vpRight = mtx(vis.tMax);
  var vpWidth = Math.max(8, vpRight - vpLeft);

  var vpEl = document.getElementById('minimap-viewport');
  vpEl.style.left = vpLeft + 'px';
  vpEl.style.width = vpWidth + 'px';
}

// Minimap events — registered once (outside drawMinimap to avoid listener leak)
(function initMinimapEvents() {
  var vpDragging = false;
  var vpDragStartX = 0;
  var vpDragStartOffset = 0;
  var vpEl = document.getElementById('minimap-viewport');

  vpEl.onmousedown = function(e) {
    vpDragging = true;
    vpDragStartX = e.clientX;
    vpDragStartOffset = timelineView.offset;
    e.preventDefault();
    e.stopPropagation();
  };

  window.addEventListener('mousemove', function(e) {
    if (!vpDragging) return;
    var mmRect = document.getElementById('minimap-wrap').getBoundingClientRect();
    var dx = (e.clientX - vpDragStartX) / mmRect.width;
    timelineView.offset = Math.max(0, Math.min(1, vpDragStartOffset + dx));
    drawChart();
    drawMinimap();
  });

  window.addEventListener('mouseup', function() { vpDragging = false; });

  minimapCanvas.onclick = function(e) {
    if (vpDragging) return;
    var rect = minimapCanvas.getBoundingClientRect();
    var mx = e.clientX - rect.left;
    var full = getFullTimeRange();
    var range = full.tMax - full.tMin;
    if (range <= 0) return;
    var W = minimapCanvas.clientWidth;
    var clickT = full.tMin + (mx / W) * range;
    var visibleRange = range / timelineView.zoom;
    timelineView.offset = Math.max(0, Math.min(1, (clickT - full.tMin - visibleRange / 2) / (range - visibleRange)));
    drawChart();
    drawMinimap();
  };
})();

// ============================================================
// UI updates
// ============================================================
function updateUI() {
  var total = 0, count = 0;
  for (var k in FIELDS) { total += FIELDS[k].ringBuf.count; count++; }
  document.getElementById('pts-count').textContent = (count ? Math.floor(total/count) : 0) + ' pts';

  var sel = document.getElementById('var-selector');
  var existing = {};
  sel.querySelectorAll('.chip').forEach(function(c) { existing[c.dataset.name] = c; });
  var names = Object.keys(FIELDS).sort();
  for (var i = 0; i < names.length; i++) {
    var name = names[i];
    var meta = FIELDS[name];
    var chip = existing[name];
    if (!chip) {
      chip = document.createElement('span');
      chip.className = 'chip active';
      chip.dataset.name = name;
      chip.textContent = name;
      chip.onclick = (function(n, el) { return function() { toggleField(n, el); }; })(name, chip);
      sel.appendChild(chip);
      existing[name] = chip;
    }
    chip.classList.toggle('active', meta.visible);
  }

  var footer = document.getElementById('stats-footer');
  var html = '';
  for (var i = 0; i < names.length; i++) {
    var name = names[i];
    var meta = FIELDS[name];
    var latest = meta.ringBuf.latest();
    var cur = latest ? formatTypedValue(latest.y, meta) : '-';
    var avg = meta.ringBuf._count > 0 ? (meta.ringBuf._sum / meta.ringBuf._count).toFixed(meta.precision || 2) : '-';
    var minV = Number.isFinite(meta.ringBuf._min) ? meta.ringBuf._min.toFixed(meta.precision || 2) : '-';
    var maxV = Number.isFinite(meta.ringBuf._max) ? meta.ringBuf._max.toFixed(meta.precision || 2) : '-';
    html += '<div class="stat"><span class="label">' + name + ':</span><span class="value" style="color:' + meta.color + '">cur=' + cur + ' min=' + minV + ' max=' + maxV + ' avg=' + avg + '</span></div>';
  }
  footer.innerHTML = html;
}

function toggleField(name, chipEl) {
  var meta = FIELDS[name];
  if (!meta) return;
  setChannelVisible(name, !meta.visible);
}

function renderInspectorNode(node, depth) {
  if (!node) return '';
  var pad = new Array((depth || 0) + 1).join('  ');
  var label = pad + (node.name || '-') + ' : ' + (node.type || node.kind || '-');
  if (node.value !== undefined) label += ' = ' + String(node.value);
  if (node.kind === 'bitfield') label += ' [bit ' + node.bit_offset + ':' + node.bit_size + ']';
  var lines = [label];
  var children = node.children || [];
  for (var i = 0; i < children.length; i++) lines.push(renderInspectorNode(children[i], (depth || 0) + 1));
  return lines.join('\n');
}

function showInspectorTree(tree) {
  var panel = document.getElementById('inspector-panel');
  if (!panel) return;
  panel.textContent = renderInspectorNode(tree, 0) || t('no_inspector_data');
  panel.classList.add('visible');
  panel.setAttribute('aria-hidden', 'false');
}

function superwatchAddName(name) {
  name = String(name || '').trim();
  if (!name) return;
  console.log('[superwatchAdd] name=' + name + ' alreadyInFields=' + !!FIELDS[name]);
  fetch(API_SW + 'add', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: name})
  })
    .then(function(r){ return r.json(); })
    .then(function(d){
      console.log('[superwatchAdd] response for ' + name + ':', JSON.stringify(d.item));
      if (d.item && d.item.name) {
        delete _removedChannels[d.item.name];
        var meta = {};
        meta[d.item.name] = d.item;
        applyChannelMetadata(meta, false);
        updateUI();
      } else if (d.item && d.item.error) {
        alert(d.item.error);
      } else if (d.error) {
        alert(d.error);
      }
    })
    .catch(function(){});
}

function superwatchSearch(query) {
  var dropdown = document.getElementById('sw-search-dropdown');
  if (!dropdown) return;
  if (!query || query.length < 1) {
    dropdown.innerHTML = '';
    dropdown.classList.remove('visible');
    return;
  }
  fetch(API_SYMBOLS + 'search?q=' + encodeURIComponent(query))
    .then(function(r){ return r.json(); })
    .then(function(d){
      var results = d.results || [];
      if (results.length === 0) {
        dropdown.innerHTML = '<li style="color:var(--dim);cursor:default">No matches</li>';
        dropdown.classList.add('visible');
        return;
      }
      var html = '';
      for (var i = 0; i < results.length; i++) {
        html += '<li data-name="' + escapeHtml(results[i].name) + '">' +
          escapeHtml(results[i].name) +
          '<span class="sw-type">' + escapeHtml(results[i].type || results[i].kind || '') + '</span>' +
          '</li>';
      }
      dropdown.innerHTML = html;
      dropdown.classList.add('visible');
    })
    .catch(function(){});
}

function superwatchInspectSelected() {
  var names = Object.keys(FIELDS).sort();
  var name = selectedChannel || (names.length ? names[0] : '');
  if (!name) return;
  fetch(API_SW + 'inspect?name=' + encodeURIComponent(name))
    .then(function(r){ return r.json(); })
    .then(function(d){ showInspectorTree(d.tree); })
    .catch(function(){});
}

function downloadJSON(filename, data) {
  var blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json;charset=utf-8' });
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function saveProject() {
  downloadJSON('jscope_project.json', serializeState());
}

function loadProjectFile(file) {
  if (!file) return;
  var reader = new FileReader();
  reader.onload = function() {
    deserializeState(String(reader.result || ''));
  };
  reader.readAsText(file);
}

document.getElementById('watch-columns-btn').addEventListener('click', function(e) {
  var menu = document.getElementById('watch-columns-menu');
  if (!menu) return;
  menu.classList.toggle('visible');
  menu.setAttribute('aria-hidden', menu.classList.contains('visible') ? 'false' : 'true');
  e.stopPropagation();
});

document.addEventListener('click', function(e) {
  var menu = document.getElementById('watch-columns-menu');
  if (!menu || !menu.classList.contains('visible')) return;
  if (menu.contains(e.target) || e.target.id === 'watch-columns-btn') return;
  menu.classList.remove('visible');
  menu.setAttribute('aria-hidden', 'true');
});

if (IS_SUPERWATCH_MODE) {
  var swSearchInput = document.getElementById('superwatch-search-input');
  var swDropdown = document.getElementById('sw-search-dropdown');
  var swTimeUnit = document.getElementById('time-unit-select');
  // Add button: add whatever is typed in the search input
  document.getElementById('superwatch-add-btn').addEventListener('click', function() {
    superwatchAddName(swSearchInput.value);
  });
  // Dropdown: click item fills input (don't auto-add)
  swDropdown.addEventListener('click', function(e) {
    var li = e.target.closest('li');
    if (li && li.dataset.name) {
      swSearchInput.value = li.dataset.name;
      swDropdown.classList.remove('visible');
      swActiveIdx = -1;
    }
  });
  // Search input: filter + keyboard navigation
  var swActiveIdx = -1;
  swSearchInput.addEventListener('input', function() {
    swActiveIdx = -1;
    superwatchSearch(swSearchInput.value);
  });
  swSearchInput.addEventListener('keydown', function(e) {
    var items = swDropdown.querySelectorAll('li[data-name]');
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      swActiveIdx = Math.min(swActiveIdx + 1, items.length - 1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      swActiveIdx = Math.max(swActiveIdx - 1, 0);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (swActiveIdx >= 0 && items[swActiveIdx]) {
        // Fill input with selected item, close dropdown
        swSearchInput.value = items[swActiveIdx].dataset.name;
        swDropdown.classList.remove('visible');
        swActiveIdx = -1;
      } else if (swSearchInput.value.trim()) {
        // No dropdown selection: add whatever is typed
        superwatchAddName(swSearchInput.value);
      }
      return;
    } else if (e.key === 'Escape') {
      swDropdown.classList.remove('visible');
      swActiveIdx = -1;
      return;
    } else { return; }
    // Highlight active item
    for (var i = 0; i < items.length; i++) items[i].classList.toggle('active', i === swActiveIdx);
  });
  // Close dropdown on outside click
  document.addEventListener('click', function(e) {
    if (!e.target.closest('#sw-search-wrap')) swDropdown.classList.remove('visible');
  });
  swTimeUnit.addEventListener('change', function() {
    timeUnit = swTimeUnit.value;
    drawChart();
    drawMinimap();
  });
  document.getElementById('superwatch-inspect-btn').addEventListener('click', superwatchInspectSelected);
}

function getSelectedThresholdChannel() {
  var sel = document.getElementById('threshold-channel');
  return sel ? sel.value : '';
}

function setInputNumber(id, value) {
  var input = document.getElementById(id);
  if (!input) return;
  input.value = (value === null || value === undefined) ? '' : String(value);
}

function readInputNumber(id) {
  var input = document.getElementById(id);
  return input ? parseNullableNumber(input.value) : null;
}

function populateThresholdChannels(preferred) {
  var sel = document.getElementById('threshold-channel');
  if (!sel) return;
  var names = Object.keys(FIELDS).sort();
  var current = preferred || sel.value || names[0] || '';
  sel.innerHTML = '';
  for (var i = 0; i < names.length; i++) {
    var opt = document.createElement('option');
    opt.value = names[i];
    opt.textContent = names[i];
    sel.appendChild(opt);
  }
  if (current && FIELDS[current]) sel.value = current;
}

function loadThresholdForm(name) {
  var meta = FIELDS[name];
  var t = meta ? normalizeThresholds(meta.thresholds) : null;
  setInputNumber('threshold-warn-low', t && t.warnLow);
  setInputNumber('threshold-warn-high', t && t.warnHigh);
  setInputNumber('threshold-alarm-low', t && t.alarmLow);
  setInputNumber('threshold-alarm-high', t && t.alarmHigh);
}

function openThresholdDialog() {
  populateThresholdChannels(selectedChannel);
  loadThresholdForm(getSelectedThresholdChannel());
  var overlay = document.getElementById('threshold-overlay');
  overlay.setAttribute('aria-hidden', 'false');
  overlay.classList.add('visible');
}

function closeThresholdDialog() {
  var overlay = document.getElementById('threshold-overlay');
  overlay.setAttribute('aria-hidden', 'true');
  overlay.classList.remove('visible');
}

function applyThresholdDialog() {
  var name = getSelectedThresholdChannel();
  if (!name || !FIELDS[name]) return;
  FIELDS[name].thresholds = normalizeThresholds({
    warnLow: readInputNumber('threshold-warn-low'),
    warnHigh: readInputNumber('threshold-warn-high'),
    alarmLow: readInputNumber('threshold-alarm-low'),
    alarmHigh: readInputNumber('threshold-alarm-high')
  });
  updateWatchTable();
  closeThresholdDialog();
}

function clearThresholdDialog() {
  var name = getSelectedThresholdChannel();
  if (name && FIELDS[name]) FIELDS[name].thresholds = null;
  loadThresholdForm(name);
  updateWatchTable();
}

// ============================================================
// Trigger system
// ============================================================
function updateTriggerStateBadge() {
  var badge = document.getElementById('trigger-state-badge');
  var stateMap = {
    idle:      { text: 'Idle',      cls: 'trigger-state-idle' },
    armed:     { text: 'Armed',     cls: 'trigger-state-armed' },
    triggered: { text: 'Triggered', cls: 'trigger-state-triggered' },
    done:      { text: 'Done',      cls: 'trigger-state-done' }
  };
  var info = stateMap[triggerSettings.state] || stateMap.idle;
  badge.textContent = info.text;
  badge.className = info.cls;
}

function updateTriggerSourceOptions() {
  var sel = document.getElementById('trigger-source');
  var current = sel.value;
  var names = Object.keys(FIELDS).sort();
  sel.innerHTML = '<option value="">--</option>';
  for (var i = 0; i < names.length; i++) {
    var opt = document.createElement('option');
    opt.value = names[i];
    opt.textContent = names[i];
    sel.appendChild(opt);
  }
  if (current && FIELDS[current]) {
    sel.value = current;
  }
}

function armTrigger() {
  triggerSettings.state = 'armed';
  preTriggerBuffer = [];
  postTriggerRemaining = 0;
  lastTriggerValue = null;
  triggerCaptureData = {};
  updateTriggerStateBadge();

  if (autoTimeout) { clearTimeout(autoTimeout); autoTimeout = null; }
  if (triggerSettings.mode === 'auto') {
    autoTimeout = setTimeout(function() {
      if (triggerSettings.state === 'armed') {
        forceTrigger();
      }
    }, 2000);
  }

  document.getElementById('trigger-force-btn').classList.add('visible');
}

function forceTrigger() {
  triggerSettings.state = 'triggered';
  postTriggerRemaining = triggerSettings.preTriggerSamples;
  triggerCaptureData = {};
  copyBufferedTriggerPoints(preTriggerBuffer);
  updateTriggerStateBadge();
  document.getElementById('trigger-force-btn').classList.remove('visible');
}

function resetTrigger() {
  triggerSettings.state = 'idle';
  preTriggerBuffer = [];
  postTriggerRemaining = 0;
  lastTriggerValue = null;
  triggerCaptureData = {};
  if (autoTimeout) { clearTimeout(autoTimeout); autoTimeout = null; }
  updateTriggerStateBadge();
  document.getElementById('trigger-force-btn').classList.remove('visible');
}

function checkTrigger(point) {
  if (!triggerSettings.enabled) return true;
  if (triggerSettings.state === 'done') {
    if (triggerSettings.mode === 'single') return false;
    armTrigger();
  }
  if (triggerSettings.state === 'idle') return true;

  var val = point[triggerSettings.source];
  if (val === undefined || !Number.isFinite(Number(val))) {
    if (!triggerSettings.source) return true;
    if (triggerSettings.state === 'armed') {
      preTriggerBuffer.push(point);
      if (preTriggerBuffer.length > triggerSettings.preTriggerSamples) {
        preTriggerBuffer.shift();
      }
    }
    return true;
  }

  val = Number(val);

  if (triggerSettings.state === 'armed') {
    preTriggerBuffer.push(point);
    if (preTriggerBuffer.length > triggerSettings.preTriggerSamples) {
      preTriggerBuffer.shift();
    }

    var triggered = false;
    if (lastTriggerValue !== null) {
      var edge = triggerSettings.edge;
      var level = triggerSettings.level;
      if (edge === 'rising') {
        triggered = (lastTriggerValue <= level && val > level);
      } else if (edge === 'falling') {
        triggered = (lastTriggerValue >= level && val < level);
      } else if (edge === 'both') {
        triggered = (lastTriggerValue <= level && val > level) ||
                    (lastTriggerValue >= level && val < level);
      }
    }
    lastTriggerValue = val;

    if (triggered) {
      triggerSettings.state = 'triggered';
      postTriggerRemaining = triggerSettings.preTriggerSamples;
      triggerCaptureData = {};
      copyBufferedTriggerPoints(preTriggerBuffer);
      if (autoTimeout) { clearTimeout(autoTimeout); autoTimeout = null; }
      updateTriggerStateBadge();
      document.getElementById('trigger-force-btn').classList.remove('visible');
    }
    return true;
  }

  if (triggerSettings.state === 'triggered') {
    appendTriggerPoint(point);
    postTriggerRemaining--;
    if (postTriggerRemaining <= 0) {
      triggerSettings.state = 'done';
      updateTriggerStateBadge();
      if (triggerSettings.mode === 'normal' || triggerSettings.mode === 'auto') {
        setTimeout(function() { armTrigger(); }, 100);
      }
    }
    return true;
  }

  return true;
}

function copyBufferedTriggerPoints(points) {
  for (var i = 0; i < points.length; i++) appendTriggerPoint(points[i]);
}

function appendTriggerPoint(point) {
  var t = (point._viewT !== undefined) ? point._viewT : (point._t || 0);
  for (var k in point) {
    if (!point.hasOwnProperty(k) || k[0] === '_') continue;
    if (!triggerCaptureData[k]) triggerCaptureData[k] = [];
    triggerCaptureData[k].push({ t: t, y: Number(point[k]) });
  }
}

// Trigger toolbar event bindings
(function initTriggerUI() {
  var enableBtn = document.getElementById('trigger-enable-btn');
  var forceBtn  = document.getElementById('trigger-force-btn');

  enableBtn.addEventListener('click', function() {
    triggerSettings.enabled = !triggerSettings.enabled;
    enableBtn.classList.toggle('active', triggerSettings.enabled);
    if (triggerSettings.enabled) {
      armTrigger();
    } else {
      resetTrigger();
    }
  });

  forceBtn.addEventListener('click', function() {
    if (triggerSettings.state === 'armed') {
      forceTrigger();
    }
  });

  document.getElementById('trigger-source').addEventListener('change', function() {
    triggerSettings.source = this.value;
    if (triggerSettings.enabled) {
      resetTrigger();
      armTrigger();
    }
  });

  document.getElementById('trigger-edge').addEventListener('change', function() {
    triggerSettings.edge = this.value;
  });

  document.getElementById('trigger-level').addEventListener('input', function() {
    triggerSettings.level = parseFloat(this.value) || 0;
    drawChart();
  });

  document.getElementById('trigger-mode').addEventListener('change', function() {
    triggerSettings.mode = this.value;
    if (triggerSettings.enabled) {
      resetTrigger();
      armTrigger();
    }
  });

  document.getElementById('trigger-pretrig').addEventListener('input', function() {
    triggerSettings.preTriggerSamples = parseInt(this.value) || 1000;
  });
})();

// ============================================================
// Cursor toggle (Task 5I)
// ============================================================
(function initCursors() {
  var btn = document.getElementById('btn-cursor-toggle');
  var modeBtn = document.getElementById('btn-cursor-mode');
  btn.addEventListener('click', function() {
    cursorState.enabled = !cursorState.enabled;
    btn.classList.toggle('active', cursorState.enabled);
    modeBtn.style.display = cursorState.enabled ? '' : 'none';
    if (cursorState.enabled) {
      var tr = getVisibleTimeRange();
      var range = tr.tMax - tr.tMin;
      cursorState.a = { t: tr.tMin + range * 0.3 };
      cursorState.b = { t: tr.tMin + range * 0.7 };
    } else {
      cursorState.a = null;
      cursorState.b = null;
      cursorReadout.textContent = '';
      if (cursorMeasurePanel) cursorMeasurePanel.style.display = 'none';
    }
    drawChart();
  });
  modeBtn.addEventListener('click', function() {
    cursorState.mode = cursorState.mode === 'time' ? 'value' : 'time';
    modeBtn.textContent = cursorState.mode === 'time' ? t('time_mode') : t('value_mode');
    updateCursorReadout();
    drawChart();
  });
})();

// ============================================================
// Keyboard shortcuts
// ============================================================
var helpOpen = false;
document.addEventListener('keydown', function(e) {
  if (helpOpen) return;
  if (e.key === 'p' || e.key === 'P') {
    if (collectionState === 'running') {
      fetch(API_CTRL + 'pause', {method:'POST'}).then(function(r){return r.json()}).then(function(d){updateCollectionUI(d.status)}).catch(function(){});
    } else if (collectionState === 'paused') {
      fetch(API_CTRL + 'resume', {method:'POST'}).then(function(r){return r.json()}).then(function(d){updateCollectionUI(d.status)}).catch(function(){});
    }
  }
  if ((e.key === 'l' || e.key === 'L') && !e.ctrlKey && !e.metaKey && !e.altKey) {
    e.preventDefault();
    toggleRawLog();
  }
  if (e.key === 'e' && (e.ctrlKey || e.metaKey) && !e.altKey) {
    e.preventDefault();
    exportCSV();
  }
  if ((e.key === 'c' || e.key === 'C') && !e.ctrlKey && !e.metaKey && !e.altKey) {
    e.preventDefault();
    document.getElementById('btn-cursor-toggle').click();
  }
});

// -- export button bindings --
document.getElementById('btn-export-csv').addEventListener('click', function() { exportCSV(); });
document.getElementById('btn-export-png').addEventListener('click', function() { exportPNG(); });
document.getElementById('btn-save-project').addEventListener('click', function() { saveProject(); });
document.getElementById('btn-load-project').addEventListener('click', function() {
  document.getElementById('project-load-input').click();
});
document.getElementById('project-load-input').addEventListener('change', function() {
  loadProjectFile(this.files && this.files[0]);
  this.value = '';
});

(function initThresholdDialog() {
  var channelSel = document.getElementById('threshold-channel');
  document.getElementById('btn-thresholds').addEventListener('click', openThresholdDialog);
  document.getElementById('threshold-cancel').addEventListener('click', closeThresholdDialog);
  document.getElementById('threshold-apply').addEventListener('click', applyThresholdDialog);
  document.getElementById('threshold-clear').addEventListener('click', clearThresholdDialog);
  channelSel.addEventListener('change', function() { loadThresholdForm(this.value); });
  document.getElementById('threshold-overlay').addEventListener('click', function(e) {
    if (e.target === this) closeThresholdDialog();
  });
})();

// ============================================================
// Help modal
// ============================================================
(function() {
  var overlay = document.getElementById('help-overlay');
  var btnHelp = document.getElementById('btn-help');
  var btnClose = document.getElementById('help-close-btn');

  function openHelp() {
    helpOpen = true;
    overlay.setAttribute('aria-hidden', 'false');
    overlay.classList.add('visible');
    btnClose.focus();
  }
  function closeHelp() {
    helpOpen = false;
    overlay.setAttribute('aria-hidden', 'true');
    overlay.classList.remove('visible');
    btnHelp.focus();
  }

  btnHelp.addEventListener('click', openHelp);
  btnClose.addEventListener('click', closeHelp);

  overlay.addEventListener('click', function(e) {
    if (e.target === overlay) closeHelp();
  });

  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && helpOpen) {
      e.preventDefault();
      closeHelp();
    }
  });
})();
