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
  // Fixed-capacity monotonic deques keep exact finite extrema under overwrite.
  // Sequence IDs make eviction independent from physical ring wrap.
  this._nextSequence = 0;
  this._minValues = new Float64Array(capacity);
  this._minSequences = new Float64Array(capacity);
  this._minHead = 0;
  this._minCount = 0;
  this._maxValues = new Float64Array(capacity);
  this._maxSequences = new Float64Array(capacity);
  this._maxHead = 0;
  this._maxCount = 0;
}

RingBuffer.prototype.push = function(t, v) {
  var idx = this.head * 2;
  var sequence = this._nextSequence++;
  // If buffer is full, advance tail (overwrite oldest)
  if (this.count >= this.capacity) {
    // Subtract oldest from stats
    var oldIdx = this.tail * 2;
    var oldV = this.buffer[oldIdx + 1];
    if (Number.isFinite(oldV)) {
      this._sum -= oldV;
      this._count--;
    }
    var evictedSequence = sequence - this.capacity;
    while (this._minCount > 0 && this._minSequences[this._minHead] <= evictedSequence) {
      this._minHead = (this._minHead + 1) % this.capacity;
      this._minCount--;
    }
    while (this._maxCount > 0 && this._maxSequences[this._maxHead] <= evictedSequence) {
      this._maxHead = (this._maxHead + 1) % this.capacity;
      this._maxCount--;
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
    while (this._minCount > 0) {
      var minTail = (this._minHead + this._minCount - 1) % this.capacity;
      if (this._minValues[minTail] < v) break;
      this._minCount--;
    }
    var minInsert = (this._minHead + this._minCount) % this.capacity;
    this._minValues[minInsert] = v;
    this._minSequences[minInsert] = sequence;
    this._minCount++;

    while (this._maxCount > 0) {
      var maxTail = (this._maxHead + this._maxCount - 1) % this.capacity;
      if (this._maxValues[maxTail] > v) break;
      this._maxCount--;
    }
    var maxInsert = (this._maxHead + this._maxCount) % this.capacity;
    this._maxValues[maxInsert] = v;
    this._maxSequences[maxInsert] = sequence;
    this._maxCount++;

    this._sum += v;
    this._count++;
  }
  this._min = this._minCount > 0 ? this._minValues[this._minHead] : Infinity;
  this._max = this._maxCount > 0 ? this._maxValues[this._maxHead] : -Infinity;
};

RingBuffer.prototype.toArray = function() {
  var result = new Array(this.count);
  for (var i = 0; i < this.count; i++) {
    var idx = ((this.tail + i) % this.capacity) * 2;
    result[i] = { t: this.buffer[idx], y: this.buffer[idx + 1] };
  }
  return result;
};

RingBuffer.prototype.timeAt = function(logicalIndex) {
  if (logicalIndex < 0 || logicalIndex >= this.count) return NaN;
  var idx = ((this.tail + logicalIndex) % this.capacity) * 2;
  return this.buffer[idx];
};

RingBuffer.prototype.valueAt = function(logicalIndex) {
  if (logicalIndex < 0 || logicalIndex >= this.count) return NaN;
  var idx = ((this.tail + logicalIndex) % this.capacity) * 2;
  return this.buffer[idx + 1];
};

RingBuffer.prototype.lowerBoundTime = function(target) {
  var low = 0, high = this.count;
  while (low < high) {
    var middle = low + ((high - low) >> 1);
    if (this.timeAt(middle) < target) low = middle + 1;
    else high = middle;
  }
  return low;
};

RingBuffer.prototype.nearestSample = function(target) {
  if (this.count < 1 || !Number.isFinite(target)) return null;
  var after = this.lowerBoundTime(target);
  var selected;
  if (after <= 0) selected = 0;
  else if (after >= this.count) selected = this.count - 1;
  else {
    var before = after - 1;
    selected = target - this.timeAt(before) <= this.timeAt(after) - target
      ? before : after;
  }

  // Preserve the former linear scan's first-match behavior when logical
  // timestamps repeat, without walking backward through an arbitrary run.
  var selectedTime = this.timeAt(selected);
  selected = this.lowerBoundTime(selectedTime);
  return {
    index: selected,
    time: selectedTime,
    value: this.valueAt(selected),
    distance: Math.abs(selectedTime - target)
  };
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

RingBuffer.prototype.clear = function() {
  this.head = 0; this.tail = 0; this.count = 0;
  this._min = Infinity; this._max = -Infinity;
  this._sum = 0; this._count = 0;
  this._nextSequence = 0;
  this._minHead = 0; this._minCount = 0;
  this._maxHead = 0; this._maxCount = 0;
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
var MIN_POINTS = Number.isFinite(Number(CONFIG.minPoints)) ? Math.floor(Number(CONFIG.minPoints)) : 2;
var MAX_POINTS = CONFIG.maxPoints;
var MAX_BUFFER_POINTS = 1000000;
var MAX_CHANNELS = 64; // Support 50+ channels (Task 7I)
var RING_BUFFER_CAPACITY = MAX_POINTS; // Ring buffer capacity matches max points
var INITIAL_RING_BUFFER_CAPACITY = RING_BUFFER_CAPACITY;
var RING_BUFFER_BYTES_PER_POINT = 48;
var SUPERWATCH_MAIN_RING_CAPACITY = 2;

function waveformMainRingCapacity() {
  return IS_SUPERWATCH_MODE && !binaryDetailEnabled
    ? SUPERWATCH_MAIN_RING_CAPACITY
    : RING_BUFFER_CAPACITY;
}

// API path configuration (adapted for FastAPI backend)
var _dashType = CONFIG.mode.toLowerCase(); // 'superwatch' or 'vofa'
var API_BASE = CONFIG.apiBase || '';
var API_STREAM = API_BASE + '/api/dash/' + _dashType + '/stream';
var API_CTRL = API_BASE + '/api/dash/' + _dashType + '/';
var API_SW = API_BASE + '/api/dash/superwatch/';
var viewerAbortController = new AbortController();

function addViewerGlobalListener(target, type, listener) {
  target.addEventListener(type, listener, { signal: viewerAbortController.signal });
}

window.MAX_POINTS = MAX_POINTS;
window.RING_BUFFER_CAPACITY = RING_BUFFER_CAPACITY;

// ============================================================
// State
// ============================================================
var FIELDS = {};
var hiddenChannelNames = {};
var FIELD_ORDER = [];
var fieldOrderDirty = true;
function markFieldOrderDirty() { fieldOrderDirty = true; }
function sortedFieldNames() {
  if (fieldOrderDirty) {
    FIELD_ORDER = Object.keys(FIELDS);
    FIELD_ORDER.sort();
    fieldOrderDirty = false;
  }
  return FIELD_ORDER;
}
var CHANNEL_METADATA = {};
var colorIdx = 0;
var paused = false;
var renderPaused = false;
var tStart = 0;
var rawLogLineCount = 0;
var vofaChannels = [];
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
var watchColumnResizeAbortController = null;
var watchColumnResizeRafId = 0;

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
var globalYView = {
  zoom: 1,
  offset: 0,
  autoRange: true,
  manualMin: null,
  manualMax: null
};

// Per-channel Y-axis state (Ctrl+wheel for individual channel zoom)
var channelYState = {};  // { name: { zoom: 1, offset: 0, autoRange: true } }
var selectedChannel = null;  // Channel under mouse for Y-axis zoom
var splitChannelName = null;
var chartPanelLayout = null;

// Timeline navigation (Task 5I)
var DEFAULT_TIMELINE_ZOOM = CONFIG.mode === 'SuperWatch' ? 2 : 1;
var DEFAULT_TIMELINE_OFFSET = CONFIG.mode === 'SuperWatch' ? 1 : 0;
var MAX_TIMELINE_ZOOM = CONFIG.mode === 'SuperWatch' ? 10000 : 100;
var superwatchTimelineSpan = null;
var superwatchDefaultTimelineSpan = null;
var superwatchTimelineCapturePending = false;
var superwatchTimelineLag = 0;
var timelineView = {
  zoom: DEFAULT_TIMELINE_ZOOM,        // 1 = show all data, >1 = zoomed in
  offset: DEFAULT_TIMELINE_OFFSET,    // 0..1 fractional offset into data range
  dragging: false,
  dragStartX: 0,
  dragStartOffset: 0
};

function resetTimelineView() {
  timelineView.zoom = DEFAULT_TIMELINE_ZOOM;
  timelineView.offset = DEFAULT_TIMELINE_OFFSET;
  if (IS_SUPERWATCH_MODE) {
    superwatchTimelineSpan = superwatchDefaultTimelineSpan;
    superwatchTimelineLag = 0;
  }
}

// Hover probe: click to activate vertical crosshair + tooltip
var hoverProbe = { active: false, mx: 0, my: 0 };

// Space-held state for hand-tool panning
var spaceHeld = false;

// Measurement cursors (Task 5I)
var draggingTrigger = false;
var draggingTriggerPanel = null;
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
var xAxisHit = document.getElementById('x-axis-hit');
var yAxisHit = document.getElementById('y-axis-hit');
var chartLegend = document.getElementById('chart-legend');
var chartLegendToggle = document.getElementById('chart-legend-toggle');
var splitPanelControl = document.getElementById('split-panel-control');
var splitPanelName = document.getElementById('split-panel-name');
var splitPanelMerge = document.getElementById('split-panel-merge');
var chartLegendVisible = true;
var chartLegendHideTimer = null;

function updateTimelineInteractionUI() {
  if (!IS_SUPERWATCH_MODE) return;
  if (xAxisHit) {
    var titleKey = collectionState === 'running' ? 'x_axis_live_tip' : 'x_axis_tip';
    xAxisHit.classList.remove('is-live-locked');
    xAxisHit.dataset.i18nTitle = titleKey;
    xAxisHit.title = t(titleKey);
  }
  var minimapWrap = document.getElementById('minimap-wrap');
  if (minimapWrap) minimapWrap.classList.remove('is-live-locked');
}

function clearChartLegendHideTimer() {
  if (chartLegendHideTimer === null) return;
  clearTimeout(chartLegendHideTimer);
  chartLegendHideTimer = null;
}

function setChartLegendVisible(visible) {
  chartLegendVisible = !!visible;
  if (chartLegend) {
    chartLegend.classList.toggle('is-visible', chartLegendVisible);
    chartLegend.setAttribute('aria-hidden', chartLegendVisible ? 'false' : 'true');
  }
  if (chartLegendToggle) {
    var titleKey = chartLegendVisible ? 'hide_channel_legend_tip' : 'show_channel_legend_tip';
    var title = t(titleKey);
    chartLegendToggle.setAttribute('aria-expanded', chartLegendVisible ? 'true' : 'false');
    chartLegendToggle.dataset.i18nTitle = titleKey;
    chartLegendToggle.title = title;
    chartLegendToggle.setAttribute('aria-label', title);
  }
}

function hideChartLegend() {
  clearChartLegendHideTimer();
  setChartLegendVisible(false);
}

function scheduleChartLegendHide(delay) {
  clearChartLegendHideTimer();
  if (!chartLegendVisible) return;
  chartLegendHideTimer = setTimeout(hideChartLegend, delay === undefined ? 700 : delay);
}

function showChartLegend(autoHideDelay) {
  clearChartLegendHideTimer();
  setChartLegendVisible(true);
  if (autoHideDelay !== undefined) scheduleChartLegendHide(autoHideDelay);
}

function revealChartLegendAfterAction() {
  showChartLegend();
  var pointerInside = (chartLegend && chartLegend.matches(':hover')) ||
    (chartLegendToggle && chartLegendToggle.matches(':hover'));
  if (!pointerInside) scheduleChartLegendHide(2000);
}

function visibleChartChannelNames() {
  var names = [];
  var sorted = sortedFieldNames();
  for (var i = 0; i < sorted.length; i++) {
    var name = sorted[i];
    var meta = FIELDS[name];
    if (!meta || !meta.visible || (CHANNEL_METADATA[name] && CHANNEL_METADATA[name].source === 'struct')) continue;
    names.push(name);
  }
  return names;
}

function updateChartLegend() {
  if (!chartLegend) return;
  var names = visibleChartChannelNames();
  var signature = (IS_SUPERWATCH_MODE ? 'superwatch:' : 'vofa:') + splitChannelName + ':' + names.join('|');
  if (chartLegend.dataset.signature === signature) return;
  chartLegend.dataset.signature = signature;
  chartLegend.textContent = '';
  var hasLegend = IS_BINARY_WAVEFORM_MODE && names.length > 0;
  if (chartLegendToggle) chartLegendToggle.hidden = !hasLegend;
  if (!hasLegend) {
    clearChartLegendHideTimer();
    setChartLegendVisible(false);
    return;
  }
  for (var i = 0; i < names.length; i++) {
    var name = names[i];
    var meta = FIELDS[name];
    var row = document.createElement('div');
    row.className = 'chart-legend-row' + (name === splitChannelName ? ' is-split' : '');
    var swatch = document.createElement('span');
    swatch.className = 'chart-legend-swatch';
    swatch.style.background = meta.color;
    var label = document.createElement('span');
    label.className = 'chart-legend-name';
    label.textContent = name;
    row.appendChild(swatch);
    row.appendChild(label);
    if (IS_SUPERWATCH_MODE) {
      var action = document.createElement('button');
      action.type = 'button';
      action.className = 'chart-legend-action';
      action.dataset.channel = name;
      action.dataset.i18n = name === splitChannelName ? 'merge_channel' : 'split_channel';
      action.dataset.i18nTitle = name === splitChannelName ? 'merge_channel_tip' : 'split_channel_tip';
      action.textContent = t(action.dataset.i18n);
      action.title = t(action.dataset.i18nTitle);
      action.setAttribute('aria-label', action.title);
      row.appendChild(action);
    }
    chartLegend.appendChild(row);
  }
  showChartLegend(2000);
}

function updateSplitPanelControl(active, dividerY) {
  if (!splitPanelControl) return;
  var visible = !!active && !!splitChannelName;
  splitPanelControl.hidden = !visible;
  if (!visible) return;
  if (splitPanelName) splitPanelName.textContent = splitChannelName;
  if (Number.isFinite(dividerY)) splitPanelControl.style.top = Math.round(dividerY) + 'px';
  if (splitPanelMerge) {
    splitPanelMerge.title = t('merge_channel_tip');
    splitPanelMerge.setAttribute('aria-label', t('merge_channel_tip') + ': ' + splitChannelName);
  }
}

function setSplitChannel(name) {
  if (!IS_SUPERWATCH_MODE || !FIELDS[name] || !FIELDS[name].visible) return false;
  var names = visibleChartChannelNames();
  if (names.length < 2) return false;
  splitChannelName = (splitChannelName === name) ? null : name;
  updateChartLegend();
  revealChartLegendAfterAction();
  drawChart();
  drawMinimap();
  return true;
}

function clearSplitChannel() {
  if (splitChannelName === null) return;
  splitChannelName = null;
  updateSplitPanelControl(false);
  updateChartLegend();
  revealChartLegendAfterAction();
  drawChart();
  drawMinimap();
}

if (splitPanelMerge) {
  splitPanelMerge.addEventListener('click', function(e) {
    e.preventDefault();
    e.stopPropagation();
    clearSplitChannel();
  }, { signal: viewerAbortController.signal });
}

if (chartLegend) {
  chartLegend.addEventListener('click', function(e) {
    var target = e.target;
    if (!target || !target.closest) return;
    var action = target.closest('.chart-legend-action');
    if (!action) return;
    e.preventDefault();
    e.stopPropagation();
    setSplitChannel(action.dataset.channel || '');
  }, { signal: viewerAbortController.signal });
  chartLegend.addEventListener('mouseenter', clearChartLegendHideTimer,
    { signal: viewerAbortController.signal });
  chartLegend.addEventListener('mouseleave', function() { scheduleChartLegendHide(); },
    { signal: viewerAbortController.signal });
}

if (chartLegendToggle) {
  chartLegendToggle.addEventListener('mouseenter', function() { showChartLegend(); },
    { signal: viewerAbortController.signal });
  chartLegendToggle.addEventListener('mouseleave', function() { scheduleChartLegendHide(); },
    { signal: viewerAbortController.signal });
  chartLegendToggle.addEventListener('click', function() {
    if (chartLegendVisible) hideChartLegend();
    else showChartLegend();
  }, { signal: viewerAbortController.signal });
}

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
addViewerGlobalListener(window, 'resize', function() { resize(); drawChart(); });
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
var viewerResizeObserver = new ResizeObserver(function() { resize(); resizeMinimap(); drawChart(); });
viewerResizeObserver.observe(debugMain);
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
    if (data._event === 'status') {
      syncDashboardStatus(data);
      return;
    }
    if (data._event === 'interval_change') {
      syncIntervalFromServer(data.interval);
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
var es = (CONFIG.mode === 'VOFA' || CONFIG.mode === 'SuperWatch') ? null : new EventSource(API_STREAM);
if (es) {
  es.onmessage = sseOnMessage;
  es.onerror = sseOnError;
  es.onopen = sseOnOpen;
}

// ============================================================
// Collection control (Start/Pause/Stop + Interval)
// ============================================================
var IS_VOFA_MODE = CONFIG.mode === 'VOFA';
var IS_SUPERWATCH_MODE = CONFIG.mode === 'SuperWatch';
var IS_BINARY_WAVEFORM_MODE = IS_VOFA_MODE || IS_SUPERWATCH_MODE;
var collectionState = 'stopped';
var currentInterval = IS_SUPERWATCH_MODE ? 0.001 : 0;
var intervalInput = document.getElementById('interval-input');
var intervalDirty = false;
var intervalUpdatePending = false;
var estimatedInterval = 0;
var estimatedRate = 0;
var timeUnit = 'ms';
// Initialize UI to stopped state
updateCollectionUI('stopped');

function syncIntervalFromServer(value) {
  var normalized = Number(value);
  if (!Number.isFinite(normalized) || normalized <= 0 || normalized > 60) return;
  currentInterval = normalized;
  if (!intervalDirty && !intervalUpdatePending) {
    intervalInput.value = String(normalized);
  }
}

intervalInput.addEventListener('input', function() {
  intervalDirty = true;
});

// Hide interval controls in RTT mode (data rate is firmware-controlled)
if (!IS_VOFA_MODE && !IS_SUPERWATCH_MODE) {
  var ig = document.getElementById('interval-group');
  if (ig) ig.style.display = 'none';
}
function updateSampleRateBadge(interval, rate, allowIntervalEstimate) {
  if (Number.isFinite(Number(interval)) && Number(interval) > 0) {
    estimatedInterval = Number(interval);
  }
  var hasReportedRate = Number.isFinite(Number(rate)) && Number(rate) >= 0;
  if (hasReportedRate) {
    estimatedRate = Number(rate);
  } else if (allowIntervalEstimate !== false && estimatedInterval > 0) {
    estimatedRate = 1 / estimatedInterval;
  }
  var badge = document.getElementById('sample-rate-badge');
  if (!badge) return;
  if (estimatedRate > 0 || (allowIntervalEstimate === false && hasReportedRate)) {
    badge.textContent = estimatedRate.toFixed(2) + ' Hz';
  } else {
    badge.textContent = '-- Hz';
  }
}

function normalizeVofaChannels(channels) {
  if (!Array.isArray(channels)) return [];
  var out = [];
  for (var i = 0; i < channels.length; i++) {
    var ch = channels[i] || {};
    var addr = ch.addr !== undefined ? ch.addr : ch.address;
    if (addr === undefined || addr === null || addr === '') continue;
    out.push({
      name: ch.name || String(addr),
      addr: addr,
      type: ch.type || 'float',
      size: ch.size || 4
    });
  }
  return out;
}

function notifyVofaChannels() {
  if (!IS_VOFA_MODE || typeof window === 'undefined' || typeof CustomEvent === 'undefined') return;
  window.dispatchEvent(new CustomEvent('mklink:vofa-channels', {
    detail: vofaChannels.slice()
  }));
}

function notifyVofaStreamState(state) {
  if (!IS_BINARY_WAVEFORM_MODE || typeof window === 'undefined' || typeof CustomEvent === 'undefined') return;
  window.dispatchEvent(new CustomEvent('mklink:vofa-stream-state', { detail: state }));
}

function apiErrorMessage(payload, status) {
  if (payload && payload.detail) {
    if (typeof payload.detail === 'string') return payload.detail;
    try { return JSON.stringify(payload.detail); } catch (_) {}
  }
  if (payload && payload.error) return String(payload.error);
  return 'Request failed' + (status ? ' (' + status + ')' : '');
}

var controlErrorUntil = 0;

function showControlError(message) {
  var connStatus = document.getElementById('conn-status');
  if (!connStatus) return;
  controlErrorUntil = Date.now() + 5000;
  connStatus.textContent = message || t('error');
  connStatus.className = 'badge badge-err';
}

function superwatchErrorMessage(message, name) {
  var text = String(message || '');
  if (/Cannot resolve/i.test(text) && /outside SRAM|not found/i.test(text)) {
    return '无法监视“' + String(name || '') + '”：符号不存在或地址不在 SRAM 范围内。';
  }
  return '添加变量失败：' + (text || '未知错误');
}

function syncDashboardStatus(d) {
  if (!d) return;
  if (IS_VOFA_MODE && Array.isArray(d.channels)) {
    vofaChannels = normalizeVofaChannels(d.channels);
    notifyVofaChannels();
  }
  var nextState = d.state;
  if (IS_BINARY_WAVEFORM_MODE && renderPaused && nextState === 'running') {
    nextState = 'paused';
  }
  if (!nextState && d.running !== undefined) {
    nextState = d.running ? (renderPaused ? 'paused' : 'running') : 'stopped';
  }
  updateCollectionUI(nextState || 'stopped');
  if (IS_VOFA_MODE) updateSampleRateBadge(d.interval, d.actual_rate, false);
  else if (IS_SUPERWATCH_MODE && Number.isFinite(Number(d.actual_rate))) {
    var actualRate = Number(d.actual_rate);
    updateSampleRateBadge(actualRate > 0 ? 1 / actualRate : undefined, actualRate, false);
  } else if (!IS_SUPERWATCH_MODE) {
    updateSampleRateBadge(d.estimated_interval, d.estimated_rate, true);
  }
  if (d.channel_metadata !== undefined) applyChannelMetadata(d.channel_metadata);
  if (d.interval !== undefined && d.interval > 0) {
    syncIntervalFromServer(d.interval);
  }
}

function updateCollectionUI(state) {
  var previousState = collectionState;
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
    if (IS_SUPERWATCH_MODE && previousState !== 'running') {
      if (paused) discardPausedBinarySnapshot();
      else resetTimelineView();
    }
    clearFrozenView();
    if (Date.now() >= controlErrorUntil) {
      connStatus.textContent = t('live');
      connStatus.className = 'badge badge-ok';
    }
    paused = false;
  } else if (state === 'paused') {
    if (!paused) freezeRenderedView();
    renderGeneration++;
    updatePending = false;
    if (Date.now() >= controlErrorUntil) {
      connStatus.textContent = t('paused');
      connStatus.className = 'badge badge-warn';
    }
    paused = true;
  } else {
    var retainedView = IS_SUPERWATCH_MODE && previousState !== 'stopped' &&
      (paused || freezeRenderedView());
    if (Date.now() >= controlErrorUntil) {
      connStatus.textContent = t('stopped');
      connStatus.className = 'badge badge-warn';
    }
    if (retainedView) paused = true;
  }
  updateTimelineInteractionUI();
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
  renderPaused = false;
  var opts = {method:'POST'};
  if (IS_VOFA_MODE && vofaChannels.length > 0) {
    opts.headers = {'Content-Type': 'application/json'};
    opts.body = JSON.stringify({
      channels: vofaChannels,
      interval: currentInterval > 0 ? currentInterval : 0.1
    });
  }
  fetch(API_CTRL + 'start', opts)
    .then(function(r){
      return r.json().catch(function(){ return {}; }).then(function(d){
        if (!r.ok) throw new Error(apiErrorMessage(d, r.status));
        return d;
      });
    })
    .then(function(d){
      if (IS_VOFA_MODE && Array.isArray(d.channels)) {
        vofaChannels = normalizeVofaChannels(d.channels);
        notifyVofaChannels();
      }
      updateCollectionUI('running');
      notifyVofaStreamState('running');
      // Reconnect SSE if needed
      if (!IS_BINARY_WAVEFORM_MODE && (!es || es.readyState === EventSource.CLOSED)) {
        if (es) es.close();
        es = new EventSource(API_STREAM);
        es.onmessage = sseOnMessage;
        es.onerror = sseOnError;
        es.onopen = sseOnOpen;
      }
    })
    .catch(function(err){
      updateCollectionUI('stopped');
      showControlError(err && err.message ? err.message : t('error'));
    });
});
document.getElementById('btn-pause').addEventListener('click', function() {
  if (collectionState === 'stopped') return;
  renderPaused = collectionState !== 'paused';
  updateCollectionUI(renderPaused ? 'paused' : 'running');
});
document.getElementById('btn-stop').addEventListener('click', function() {
  fetch(API_CTRL + 'stop', {method:'POST'})
    .then(function(r){
      return r.json().catch(function(){ return {}; }).then(function(d){
        if (!r.ok) throw new Error(apiErrorMessage(d, r.status));
        return d;
      });
    })
    .then(function(d){
      renderPaused = false;
      updateCollectionUI('stopped');
      notifyVofaStreamState('stopped');
    })
    .catch(function(err){
      showControlError(err && err.message ? err.message : t('error'));
    });
});
var bufferInput = document.getElementById('buffer-input');
bufferInput.addEventListener('input', updateBufferMemoryEstimate);
updateBufferMemoryEstimate();
document.getElementById('btn-apply-buffer').addEventListener('click', function() {
  var val = parseInt(bufferInput.value, 10);
  if (!Number.isFinite(val) || val < MIN_POINTS || val > MAX_BUFFER_POINTS) {
    alert('Buffer must be between ' + MIN_POINTS + ' and ' + MAX_BUFFER_POINTS + ' points per channel');
    return;
  }
  setBufferCapacity(val);
});
document.getElementById('btn-apply-interval').addEventListener('click', function() {
  var val = parseFloat(intervalInput.value);
  var minimumInterval = IS_SUPERWATCH_MODE ? 0.00001 : 0;
  if (!Number.isFinite(val) || val < minimumInterval || val > 60) {
    alert('Interval must be between ' + minimumInterval + ' and 60 seconds');
    return;
  }
  intervalDirty = false;
  intervalUpdatePending = true;
  fetch(API_CTRL + 'interval', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({interval: val})
  })
    .then(function(r){
      return r.json().catch(function(){ return {}; }).then(function(d){
        if (!r.ok) throw new Error(apiErrorMessage(d, r.status));
        var normalized = Number(d.interval);
        if (!Number.isFinite(normalized) || normalized <= 0 || normalized > 60) {
          throw new Error('Invalid interval response');
        }
        return normalized;
      });
    })
    .then(function(normalized){
      intervalUpdatePending = false;
      syncIntervalFromServer(normalized);
    })
    .catch(function(err){
      intervalUpdatePending = false;
      intervalDirty = false;
      intervalInput.value = String(currentInterval);
      showControlError(err && err.message ? err.message : t('error'));
    });
});

// Sync initial state from server
fetch(API_CTRL + 'status')
  .then(function(r){return r.json()})
  .then(function(d){
    syncDashboardStatus(d);
  })
  .catch(function(){});

// ============================================================
// Bottom panel management
// ============================================================
var rawLogPanel = document.getElementById('raw-log-panel');
var rawLogEl = document.getElementById('raw-log');
var rawLogCountEl = document.getElementById('raw-log-count');
var rawLogOpen = false;
var RAW_LOG_CAPACITY = 5000;
var rawLogLines = new Array(RAW_LOG_CAPACITY);
var rawLogHead = 0;
var rawLogStoredCount = 0;
var rawLogLastPaint = 0;

function rawLogSnapshot() {
  var snapshot = new Array(rawLogStoredCount);
  for (var i = 0; i < rawLogStoredCount; i++) {
    snapshot[i] = rawLogLines[(rawLogHead + i) % RAW_LOG_CAPACITY];
  }
  return snapshot;
}

function paintRawLog(force) {
  if (!rawLogOpen) return;
  var now = performance.now();
  if (!force && now - rawLogLastPaint < 100) return;
  rawLogLastPaint = now;
  var snapshot = rawLogSnapshot();
  rawLogEl.textContent = snapshot.join('\n') + (snapshot.length ? '\n' : '');
  rawLogCountEl.textContent = rawLogLineCount + ' lines';
  rawLogEl.scrollTop = rawLogEl.scrollHeight;
}

function appendRawLogLine(line) {
  if (rawLogStoredCount < RAW_LOG_CAPACITY) {
    rawLogLines[(rawLogHead + rawLogStoredCount) % RAW_LOG_CAPACITY] = line;
    rawLogStoredCount++;
  } else {
    rawLogLines[rawLogHead] = line;
    rawLogHead = (rawLogHead + 1) % RAW_LOG_CAPACITY;
  }
  rawLogLineCount++;
  paintRawLog(false);
}

function formatRawLogTimestamp(milliseconds) {
  var date = new Date(milliseconds);
  if (!Number.isFinite(date.getTime())) return '0000-00-00 00:00:00.000';
  function pad(value, width) { return String(value).padStart(width, '0'); }
  return pad(date.getFullYear(), 4) + '-' + pad(date.getMonth() + 1, 2) + '-' +
    pad(date.getDate(), 2) + ' ' + pad(date.getHours(), 2) + ':' +
    pad(date.getMinutes(), 2) + ':' + pad(date.getSeconds(), 2) + '.' +
    pad(date.getMilliseconds(), 3);
}

function formatRawLogValue(value) {
  if (!Number.isFinite(value)) return 'NaN';
  return String(Number(value.toPrecision(9)));
}

function appendBinaryRawSamples(times, values, itemCount, channelCount) {
  if (!IS_SUPERWATCH_MODE || !rawLogOpen) return;
  for (var sample = 0; sample < itemCount; sample++) {
    var fields = new Array(channelCount);
    for (var channel = 0; channel < channelCount; channel++) {
      fields[channel] = binaryChannelNames[channel] + '=' +
        formatRawLogValue(values[sample * channelCount + channel]);
    }
    appendRawLogLine('[' + formatRawLogTimestamp(times[sample]) + '] ' + fields.join(', '));
  }
}

function clearRawLog() {
  rawLogEl.textContent = '';
  rawLogLines = new Array(RAW_LOG_CAPACITY);
  rawLogHead = 0;
  rawLogStoredCount = 0;
  rawLogLineCount = 0;
  rawLogCountEl.textContent = '0 lines';
}

function saveTextContent(filename, content, mimeType) {
  var nativeSave = window.__MKLINK_SAVE_FILE__;
  if (typeof nativeSave === 'function') {
    var nativeResult;
    try {
      nativeResult = nativeSave(filename, content);
    } catch (error) {
      showControlError('Save failed: ' + (error && error.message ? error.message : String(error)));
      return false;
    }
    if (nativeResult && typeof nativeResult.catch === 'function') {
      nativeResult.catch(function(error) {
        showControlError('Save failed: ' + (error && error.message ? error.message : String(error)));
      });
    }
    return true;
  }
  var blob = new Blob([content], { type: mimeType });
  var url = URL.createObjectURL(blob);
  var link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
  return true;
}

function saveRawLog() {
  var snapshot = rawLogSnapshot();
  if (!snapshot.length) return false;
  var content = snapshot.join('\n') + '\n';
  var now = new Date();
  function pad(value, width) { return String(value).padStart(width, '0'); }
  var suffix = pad(now.getFullYear(), 4) + pad(now.getMonth() + 1, 2) +
    pad(now.getDate(), 2) + '-' + pad(now.getHours(), 2) +
    pad(now.getMinutes(), 2) + pad(now.getSeconds(), 2);
  var filename = 'superwatch-raw-' + suffix + '.txt';
  return saveTextContent(filename, content, 'text/plain;charset=utf-8;');
}

function setRawLogOpen(open) {
  rawLogOpen = open;
  rawLogPanel.dataset.open = open ? 'true' : 'false';
  if (open) {
    paintRawLog(true);
    resize(); resizeMinimap(); drawChart();
  }
}

function toggleRawLog() { setRawLogOpen(!rawLogOpen); }
rawLogPanel.querySelector('.panel-header').addEventListener('click', function(e) {
  if (e.target.closest('button')) return;
  toggleRawLog();
});
document.getElementById('raw-log-close').addEventListener('click', function() { setRawLogOpen(false); });
document.getElementById('raw-log-save').addEventListener('click', saveRawLog);
document.getElementById('raw-log-clear').addEventListener('click', clearRawLog);

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
  stopWatchColumnResize();
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

function stopWatchColumnResize() {
  if (watchColumnResize) {
    var th = document.querySelector(
      '#watch-table thead th[data-col="' + watchColumnResize.key + '"]'
    );
    if (th) th.classList.remove('resizing');
  }
  watchColumnResize = null;
  if (watchColumnResizeRafId) {
    cancelAnimationFrame(watchColumnResizeRafId);
    watchColumnResizeRafId = 0;
  }
  document.body.style.cursor = '';
  document.body.style.userSelect = '';
  if (watchColumnResizeAbortController) {
    var controller = watchColumnResizeAbortController;
    watchColumnResizeAbortController = null;
    controller.abort();
  }
}

function bindWatchColumnResizers() {
  var resizers = document.querySelectorAll('.watch-col-resizer');

  function _onMove(e) {
    if (!watchColumnResize) return;
    var newW = watchColumnResize.startWidth + e.clientX - watchColumnResize.startX;
    setWatchColumnWidth(watchColumnResize.key, newW);
    cancelAnimationFrame(watchColumnResizeRafId);
    watchColumnResizeRafId = requestAnimationFrame(function() {
      watchColumnResizeRafId = 0;
      if (watchColumnResize) _applyColumnWidthLive(watchColumnResize.key);
    });
  }
  function _onUp() {
    stopWatchColumnResize();
  }
  for (var i = 0; i < resizers.length; i++) {
    resizers[i].addEventListener('mousedown', function(e) {
      e.preventDefault();
      stopWatchColumnResize();
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
      watchColumnResizeAbortController = new AbortController();
      document.addEventListener('mousemove', _onMove, {
        signal: watchColumnResizeAbortController.signal
      });
      document.addEventListener('mouseup', _onUp, {
        signal: watchColumnResizeAbortController.signal
      });
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
  var names = sortedFieldNames();
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
  var names = sortedFieldNames();
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
    markFieldOrderDirty();
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
        ringBuf: new RingBuffer(waveformMainRingCapacity()),
        visible: meta.source !== 'struct',
        unit: "",
        type: meta.type || "number",
        size: meta.size || "",
        format: "auto",
        enumValues: null,
        precision: 2,
        thresholds: null
      };
      markFieldOrderDirty();
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
      markFieldOrderDirty();
      delete CHANNEL_METADATA[removed[ri]];
    }
    if (removed.length > 0) _watchStructGen++;
  }
  if (splitChannelName && !FIELDS[splitChannelName]) clearSplitChannel();
  updateWatchTable();
}

function setChannelVisible(name, visible) {
  var meta = FIELDS[name];
  if (!meta) return;
  meta.visible = !!visible;
  if (!meta.visible && splitChannelName === name) clearSplitChannel();
  if (meta.visible) delete hiddenChannelNames[name];
  else hiddenChannelNames[name] = true;
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

function captureSuperwatchTimelineSpanIfReady() {
  if (!IS_SUPERWATCH_MODE || superwatchDefaultTimelineSpan !== null ||
      !superwatchTimelineCapturePending) return false;
  var dataSpan = 0;
  if (binaryBufferedSamples >= RING_BUFFER_CAPACITY &&
      Number.isFinite(binaryBufferStart) && Number.isFinite(binaryBufferEnd)) {
    dataSpan = binaryBufferEnd - binaryBufferStart;
  } else {
    for (var name in FIELDS) {
      if (!FIELDS.hasOwnProperty(name)) continue;
      var ring = FIELDS[name].ringBuf;
      if (!ring || ring.count < INITIAL_RING_BUFFER_CAPACITY) continue;
      var oldest = ring.oldest();
      var latest = ring.latest();
      if (oldest && latest) dataSpan = Math.max(dataSpan, latest.t - oldest.t);
    }
  }
  if (!(dataSpan > 0)) return false;
  superwatchDefaultTimelineSpan = dataSpan / DEFAULT_TIMELINE_ZOOM;
  superwatchTimelineSpan = superwatchDefaultTimelineSpan;
  superwatchTimelineLag = 0;
  superwatchTimelineCapturePending = false;
  timelineView.offset = 1;
  return true;
}

function setBufferCapacity(newCapacity) {
  newCapacity = Math.floor(Number(newCapacity));
  if (!Number.isFinite(newCapacity)) return false;
  newCapacity = Math.max(MIN_POINTS, Math.min(MAX_BUFFER_POINTS, newCapacity));
  var visibleSpanBeforeResize = null;
  if (IS_SUPERWATCH_MODE) {
    var visibleBeforeResize = getVisibleTimeRange();
    visibleSpanBeforeResize = visibleBeforeResize.tMax - visibleBeforeResize.tMin;
    if (superwatchDefaultTimelineSpan === null) {
      superwatchTimelineCapturePending = true;
      captureSuperwatchTimelineSpanIfReady();
    }
  }
  RING_BUFFER_CAPACITY = newCapacity;
  MAX_POINTS = newCapacity;
  window.RING_BUFFER_CAPACITY = RING_BUFFER_CAPACITY;
  window.MAX_POINTS = MAX_POINTS;
  var input = document.getElementById('buffer-input');
  if (input) input.value = String(newCapacity);
  for (var name in FIELDS) {
    if (!FIELDS.hasOwnProperty(name)) continue;
    FIELDS[name].ringBuf = resizeRingBuffer(FIELDS[name].ringBuf, waveformMainRingCapacity());
  }
  if (IS_SUPERWATCH_MODE) {
    if (Number.isFinite(visibleSpanBeforeResize) && visibleSpanBeforeResize > 0) {
      superwatchTimelineSpan = visibleSpanBeforeResize;
    }
  }
  updateBufferMemoryEstimate();
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
  var start = Math.max(0, oldBuf.count - newCapacity);
  for (var i = start; i < oldBuf.count; i++) {
    next.push(oldBuf.timeAt(i), oldBuf.valueAt(i));
  }
  return next;
}

function resizeWaveformMainRings(newCapacity) {
  for (var name in FIELDS) {
    if (!FIELDS.hasOwnProperty(name) || !FIELDS[name].ringBuf ||
        FIELDS[name].ringBuf.capacity === newCapacity) continue;
    FIELDS[name].ringBuf = resizeRingBuffer(FIELDS[name].ringBuf, newCapacity);
  }
}

function bufferedChannelCount() {
  if (IS_BINARY_WAVEFORM_MODE && Array.isArray(binaryChannelNames) && binaryChannelNames.length) {
    return binaryChannelNames.length;
  }
  return Object.keys(FIELDS).length;
}

function updateBufferMemoryEstimate() {
  var estimate = document.getElementById('buffer-memory-estimate');
  if (!estimate) return;
  var requestedCapacity = Math.floor(Number(bufferInput && bufferInput.value));
  if (!Number.isFinite(requestedCapacity) || requestedCapacity < 0) requestedCapacity = RING_BUFFER_CAPACITY;
  var channels = bufferedChannelCount();
  var bytes = IS_SUPERWATCH_MODE
    ? requestedCapacity * (8 + channels * 4)
    : requestedCapacity * channels * RING_BUFFER_BYTES_PER_POINT;
  var mib = bytes / (1024 * 1024);
  var displayMib = mib < 10 ? mib.toFixed(1) : String(Math.round(mib));
  estimate.textContent = '~' + displayMib + ' MB';
  estimate.title = t('buffer_memory_tip') + ': ' + channels + ' ch x ' + requestedCapacity + ' pts';
  estimate.classList.toggle('is-high', mib >= 512);
  estimate.dataset.bytes = String(bytes);
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
  state.splitChannel = splitChannelName;
  state.globalYView = {
    zoom: globalYView.zoom,
    offset: globalYView.offset,
    autoRange: globalYView.autoRange !== false,
    manualMin: globalYView.manualMin,
    manualMax: globalYView.manualMax
  };
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
    globalYView = { zoom: 1, offset: 0, autoRange: true, manualMin: null, manualMax: null };
    splitChannelName = null;
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
    if (IS_SUPERWATCH_MODE && state.splitChannel && FIELDS[state.splitChannel] &&
        FIELDS[state.splitChannel].visible && visibleChartChannelNames().length >= 2) {
      splitChannelName = state.splitChannel;
    }
    if (state.globalYView) {
      globalYView.zoom = state.globalYView.zoom || 1;
      globalYView.offset = state.globalYView.offset || 0;
      if (state.globalYView.autoRange === false) {
        setManualSharedYRange(
          parseNullableNumber(state.globalYView.manualMin),
          parseNullableNumber(state.globalYView.manualMax)
        );
      }
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
  if (IS_SUPERWATCH_MODE && binaryHistoryRequester) {
    if (!binaryExportPending) {
      binaryExportPending = true;
      binaryHistoryRequester();
    }
    return;
  }
  var names = sortedFieldNames();
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
  saveTextContent(
    csvCfg.filename || 'jscope_export.csv',
    csvContent,
    'text/csv;charset=utf-8;'
  );
}

function exportBinaryHistorySnapshot(snapshot) {
  if (!IS_SUPERWATCH_MODE || !binaryExportPending || !snapshot) return false;
  binaryExportPending = false;
  var times = new Float64Array(snapshot.times);
  var values = new Float32Array(snapshot.values);
  if (snapshot.channelCount !== binaryChannelNames.length ||
      values.length !== snapshot.itemCount * snapshot.channelCount ||
      times.length !== snapshot.itemCount) return false;
  var csvCfg = window._csvExportConfig || { includeTimestamp: true, delimiter: ',', filename: 'jscope_export.csv' };
  var delim = csvCfg.delimiter || ',';
  var names = sortedFieldNames();
  var headers = [];
  if (csvCfg.includeTimestamp !== false) headers.push('timestamp');
  for (var nameIndex = 0; nameIndex < names.length; nameIndex++) {
    var field = FIELDS[names[nameIndex]];
    headers.push(names[nameIndex] + (field.unit ? ' (' + field.unit + ')' : ''));
  }
  var rows = new Array(snapshot.itemCount + 1);
  rows[0] = headers.join(delim);
  var originSeconds = binaryTimeOrigin !== null
    ? binaryTimeOrigin
    : (times.length ? times[0] / 1000 : 0);
  for (var sample = 0; sample < snapshot.itemCount; sample++) {
    var row = [];
    if (csvCfg.includeTimestamp !== false) row.push((times[sample] / 1000 - originSeconds).toFixed(6));
    for (var column = 0; column < names.length; column++) {
      var channel = binaryChannelIndex[names[column]];
      var value = channel === undefined ? NaN : values[sample * snapshot.channelCount + channel];
      row.push(Number.isFinite(value) ? value.toFixed(
        FIELDS[names[column]].precision !== undefined ? FIELDS[names[column]].precision : 4
      ) : '');
    }
    rows[sample + 1] = row.join(delim);
  }
  saveTextContent(csvCfg.filename || 'jscope_export.csv', rows.join('\n'), 'text/csv;charset=utf-8;');
  return true;
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
        ringBuf: new RingBuffer(waveformMainRingCapacity()),
        visible: true,
        unit: "",
        type: "number",
        size: "",
        format: "auto",
        enumValues: null,
        precision: 2,
        thresholds: null
      };
      markFieldOrderDirty();
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
  appendRawLogLine(JSON.stringify(point));

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

function setHiddenChannels(names) {
  hiddenChannelNames = {};
  if (Array.isArray(names)) {
    for (var i = 0; i < names.length; i++) hiddenChannelNames[String(names[i])] = true;
  }
  for (var name in FIELDS) {
    if (!FIELDS.hasOwnProperty(name)) continue;
    FIELDS[name].visible = !hiddenChannelNames[name];
  }
  updateWatchTable();
  drawChart();
  drawMinimap();
}

function removeArraySnapshotField() {
  if (!arraySnapshotName) return;
  var name = arraySnapshotName;
  if (FIELDS[name] && FIELDS[name].isArraySnapshot) {
    if (FIELDS[name].ringBuf) FIELDS[name].ringBuf.clear();
    delete FIELDS[name];
    delete CHANNEL_METADATA[name];
    markFieldOrderDirty();
    _watchStructGen++;
  }
  arraySnapshotName = null;
  if (splitChannelName === name) clearSplitChannel();
}

function setArraySnapshot(snapshot) {
  if (!IS_SUPERWATCH_MODE) return false;
  if (!snapshot || !snapshot.name || !Array.isArray(snapshot.values)) {
    removeArraySnapshotField();
    updateChartLegend();
    drawChart();
    drawMinimap();
    updateWatchTable();
    return true;
  }
  var name = String(snapshot.name);
  var values = snapshot.values.map(Number).filter(Number.isFinite);
  if (!values.length) return false;
  if (arraySnapshotName && arraySnapshotName !== name) removeArraySnapshotField();
  var field = FIELDS[name];
  if (field && !field.isArraySnapshot) return false;
  var sequence = Number(snapshot.sequence) || 0;
  if (field && field.isArraySnapshot && field.arraySequence === sequence &&
      field.arrayStartIndex === (Number(snapshot.start_index) || 0) &&
      field.arrayValues && field.arrayValues.length === values.length) return false;
  if (!field) {
    field = FIELDS[name] = {
      color: COLORS[colorIdx % COLORS.length],
      ringBuf: new RingBuffer(2),
      visible: !hiddenChannelNames[name],
      unit: '',
      type: String(snapshot.type_name || 'array') + '[]',
      size: snapshot.element_size || '',
      format: 'auto',
      enumValues: null,
      precision: 2,
      thresholds: null,
      isArraySnapshot: true,
      arrayValues: values,
      arrayStartIndex: Number(snapshot.start_index) || 0,
      arraySequence: sequence,
    };
    colorIdx++;
    markFieldOrderDirty();
    _watchStructGen++;
  } else {
    field.arrayValues = values;
    field.arrayStartIndex = Number(snapshot.start_index) || 0;
    field.arraySequence = sequence;
  }
  field.type = String(snapshot.type_name || field.type || 'array') + '[]';
  field.size = snapshot.element_size || field.size || '';
  field.ringBuf.clear();
  field.ringBuf.push(Number(snapshot.timestamp_us || Date.now() * 1000) / 1000000, values[values.length - 1]);
  arraySnapshotName = name;
  CHANNEL_METADATA[name] = { source: 'array-snapshot', type: field.type, size: field.size };
  updateChartLegend();
  drawChart();
  drawMinimap();
  updateWatchTable();
  return true;
}

// Binary waveform bridge. SuperWatch history remains in the Worker; the main
// thread receives summaries and bounded render envelopes. VOFA still accepts
// transferable sample-major Float32 batches directly.
var binaryChannelNames = [];
var binaryChannelSignature = null;
var binaryTimeOrigin = null;
var binaryLastTimestamp = null;
var binaryLastSequence = null;
var binaryLastDetailSequence = null;
var binaryEnvelope = null;
var binaryBufferStart = null;
var binaryBufferEnd = null;
var binaryBufferedSamples = 0;
var binaryHistoryRequester = null;
var binaryDetailRequester = null;
var binaryVisibleRangeRequester = null;
var binaryVisibleRangeRequestCount = 0;
var binaryDetailEnabled = false;
var binaryExportPending = false;
var binaryChannelIndex = {};
var binaryLastUiPaint = -Infinity;
var arraySnapshotName = null;
var BINARY_RATE_WINDOW_NS = 500000000n;
var binaryRateWindowStartNs = null;
var binaryRateLastTimestampNs = null;
var binaryRateSampleIntervals = 0;
var frozenFullTimeRange = null;
var frozenSharedYRange = null;

function resetBinarySampleRate() {
  if (!IS_SUPERWATCH_MODE) return;
  binaryRateWindowStartNs = null;
  binaryRateLastTimestampNs = null;
  binaryRateSampleIntervals = 0;
  estimatedInterval = 0;
  estimatedRate = 0;
  updateSampleRateBadge(undefined, undefined, false);
}

function updateBinarySampleRate(batch) {
  if (!IS_SUPERWATCH_MODE || typeof batch.timestampNs !== 'bigint') return;
  var timestampNs = batch.timestampNs;
  if (binaryRateLastTimestampNs === null || timestampNs <= binaryRateLastTimestampNs) {
    binaryRateWindowStartNs = timestampNs;
    binaryRateLastTimestampNs = timestampNs;
    binaryRateSampleIntervals = 0;
    return;
  }
  if (binaryRateWindowStartNs === null) binaryRateWindowStartNs = binaryRateLastTimestampNs;
  binaryRateSampleIntervals += batch.itemCount;
  binaryRateLastTimestampNs = timestampNs;
  var elapsedNs = timestampNs - binaryRateWindowStartNs;
  if (elapsedNs < BINARY_RATE_WINDOW_NS) return;
  var rate = binaryRateSampleIntervals * 1000000000 / Number(elapsedNs);
  if (Number.isFinite(rate) && rate > 0) {
    updateSampleRateBadge(1 / rate, rate, false);
  }
  binaryRateWindowStartNs = timestampNs;
  binaryRateSampleIntervals = 0;
}

function clearFrozenView() {
  frozenFullTimeRange = null;
  frozenSharedYRange = null;
}

function freezeRenderedView() {
  if (!IS_SUPERWATCH_MODE) return false;
  if (frozenFullTimeRange) return true;
  var hasData = binaryEnvelope !== null;
  if (!hasData) {
    for (var name in FIELDS) {
      if (FIELDS[name].ringBuf && FIELDS[name].ringBuf.count >= 2) {
        hasData = true;
        break;
      }
    }
  }
  if (!hasData) return false;
  frozenFullTimeRange = computeFullTimeRange();
  frozenSharedYRange = getSharedYRange();
  return true;
}

function discardPausedBinarySnapshot() {
  binaryEnvelope = null;
  for (var channel = 0; channel < binaryChannelNames.length; channel++) {
    var field = FIELDS[binaryChannelNames[channel]];
    if (field && field.ringBuf) field.ringBuf.clear();
  }
  resetTimelineView();
}

function configureBinaryChannels(channels) {
  if (!IS_BINARY_WAVEFORM_MODE || !Array.isArray(channels)) return;
  var metadata = {};
  var nextNames = [];
  var signatureRows = [];
  for (var i = 0; i < channels.length; i++) {
    var channel = channels[i] || {};
    var name = channel.name || String(channel.addr !== undefined ? channel.addr : i);
    nextNames.push(name);
    metadata[name] = {
      type: channel.type || 'float',
      size: channel.size || 4,
      address: channel.addr,
      unit: channel.unit || ''
    };
    signatureRows.push([
      name, metadata[name].type, metadata[name].size,
      metadata[name].address, metadata[name].unit
    ]);
  }
  var nextSignature = JSON.stringify(signatureRows);
  if (nextSignature === binaryChannelSignature) return;

  // VOFA channel status is a complete ordered snapshot. Replace it in one
  // synchronous step so a same-count rename cannot leave stale rings or
  // metadata paired with the new binary column order.
  for (var oldName in FIELDS) {
    if (!FIELDS.hasOwnProperty(oldName)) continue;
    if (FIELDS[oldName].isArraySnapshot) continue;
    if (FIELDS[oldName].ringBuf) FIELDS[oldName].ringBuf.clear();
    delete FIELDS[oldName];
  }
  for (var metadataName in CHANNEL_METADATA) {
    if (CHANNEL_METADATA.hasOwnProperty(metadataName)) {
      delete CHANNEL_METADATA[metadataName];
    }
  }
  markFieldOrderDirty();
  _watchStructGen++;
  channelYState = {};
  _inspectCache = {};
  _expandedRows = {};
  _watchWrittenValues = {};
  _removedChannels = {};
  selectedChannel = null;
  splitChannelName = null;
  resetTimelineView();
  hoverProbe.active = false;

  triggerSettings.enabled = false;
  triggerSettings.source = '';
  resetTrigger();
  var triggerEnable = document.getElementById('trigger-enable-btn');
  if (triggerEnable) triggerEnable.classList.remove('active');
  var triggerSource = document.getElementById('trigger-source');
  if (triggerSource) triggerSource.value = '';

  cursorState.enabled = false;
  cursorState.a = null;
  cursorState.b = null;
  cursorState.dragging = null;
  cursorReadout.textContent = '';
  if (cursorMeasurePanel) cursorMeasurePanel.style.display = 'none';
  var cursorToggle = document.getElementById('btn-cursor-toggle');
  if (cursorToggle) cursorToggle.classList.remove('active');
  var cursorMode = document.getElementById('btn-cursor-mode');
  if (cursorMode) cursorMode.style.display = 'none';

  binaryChannelNames = nextNames;
  binaryChannelIndex = {};
  for (var channelIndex = 0; channelIndex < nextNames.length; channelIndex++) {
    binaryChannelIndex[nextNames[channelIndex]] = channelIndex;
  }
  binaryChannelSignature = nextSignature;
  binaryTimeOrigin = null;
  binaryLastTimestamp = null;
  binaryLastSequence = null;
  binaryLastDetailSequence = null;
  binaryEnvelope = null;
  binaryBufferStart = null;
  binaryBufferEnd = null;
  binaryBufferedSamples = 0;
  binaryLastUiPaint = -Infinity;
  resetBinarySampleRate();
  applyChannelMetadata(metadata, false);
  setHiddenChannels(Object.keys(hiddenChannelNames));
  updateTriggerSourceOptions();
  updateChartLegend();
  updateBufferMemoryEstimate();
}

function acceptBinaryBatch(batch, channels) {
  if (!IS_BINARY_WAVEFORM_MODE || !batch || batch.layout !== 'sample-major-float32') return false;
  if (channels) configureBinaryChannels(channels);
  if (!binaryChannelNames.length || batch.channelCount !== binaryChannelNames.length) return false;
  var previousSequence = IS_SUPERWATCH_MODE ? binaryLastDetailSequence : binaryLastSequence;
  if (previousSequence !== null && batch.sequence <= previousSequence) return false;
  var values = new Float32Array(batch.values);
  if (values.length !== batch.itemCount * batch.channelCount) return false;
  if (!batch.times || batch.times.byteLength !== batch.itemCount * 8) return false;
  var times = new Float64Array(batch.times);
  var previousTimeMs = -Infinity;
  for (var validateIndex = 0; validateIndex < times.length; validateIndex++) {
    if (!Number.isFinite(times[validateIndex]) || times[validateIndex] <= previousTimeMs) return false;
    previousTimeMs = times[validateIndex];
  }
  if (!times.length) return false;

  // SuperWatch full batches are requested only while trigger detail is active.
  // Temporarily restore full main-thread history for trigger evaluation.
  if (IS_SUPERWATCH_MODE) {
    binaryDetailEnabled = true;
    resizeWaveformMainRings(RING_BUFFER_CAPACITY);
  }

  var summaryAlreadyAccepted = IS_SUPERWATCH_MODE && binaryLastSequence === batch.sequence;
  if (IS_SUPERWATCH_MODE) {
    binaryLastDetailSequence = batch.sequence;
    if (binaryLastSequence === null || batch.sequence > binaryLastSequence) {
      binaryLastSequence = batch.sequence;
    }
  } else binaryLastSequence = batch.sequence;
  binaryLastTimestamp = times[times.length - 1] / 1000;
  if (!summaryAlreadyAccepted) updateBinarySampleRate(batch);
  if (IS_SUPERWATCH_MODE && paused) return true;

  if (binaryTimeOrigin === null) binaryTimeOrigin = times[0] / 1000;
  for (var sample = 0; sample < batch.itemCount; sample++) {
    var sampleSeconds = times[sample] / 1000;
    var viewTime = sampleSeconds - binaryTimeOrigin;
    var capture = null;
    if (triggerSettings.enabled) {
      capture = { _t: sampleSeconds, _viewT: viewTime };
      for (var captureChannel = 0; captureChannel < batch.channelCount; captureChannel++) {
        capture[binaryChannelNames[captureChannel]] = values[sample * batch.channelCount + captureChannel];
      }
      if (!checkTrigger(capture)) continue;
    }
    for (var channelIndex = 0; channelIndex < batch.channelCount; channelIndex++) {
      var name = binaryChannelNames[channelIndex];
      var field = FIELDS[name];
      if (!field || !field.ringBuf) continue;
      field.ringBuf.push(viewTime, values[sample * batch.channelCount + channelIndex]);
    }
  }
  captureSuperwatchTimelineSpanIfReady();
  if (IS_SUPERWATCH_MODE) appendBinaryRawSamples(times, values, batch.itemCount, batch.channelCount);
  else appendRawLogLine('[binary] seq=' + String(batch.sequence) + ' samples=' + batch.itemCount);
  return true;
}

function acceptBinarySummary(summary, channels) {
  if (!IS_SUPERWATCH_MODE || !summary) return false;
  if (channels) configureBinaryChannels(channels);
  if (!binaryChannelNames.length || summary.channelCount !== binaryChannelNames.length) return false;
  if (binaryLastSequence !== null && summary.sequence <= binaryLastSequence) return false;
  var values = new Float32Array(summary.latestValues);
  if (values.length !== summary.channelCount || !Number.isFinite(summary.latestTimeMs)) return false;
  for (var channel = 0; channel < values.length; channel++) {
    if (!Number.isFinite(values[channel])) return false;
  }
  if (!Number.isFinite(summary.bufferStartMs) || !Number.isFinite(summary.bufferEndMs) ||
      summary.bufferEndMs < summary.bufferStartMs) return false;

  if (!binaryDetailEnabled) resizeWaveformMainRings(SUPERWATCH_MAIN_RING_CAPACITY);

  binaryLastSequence = summary.sequence;
  binaryLastTimestamp = summary.latestTimeMs / 1000;
  binaryBufferStart = summary.bufferStartMs / 1000;
  binaryBufferEnd = summary.bufferEndMs / 1000;
  binaryBufferedSamples = Math.max(0, Math.floor(Number(summary.bufferedItemCount) || 0));
  if (binaryTimeOrigin === null) binaryTimeOrigin = binaryBufferStart;
  updateBinarySampleRate({
    timestampNs: summary.timestampNs,
    itemCount: summary.collectedItemCount
  });

  var viewTime = summary.latestTimeMs / 1000 - binaryTimeOrigin;
  for (var index = 0; index < summary.channelCount; index++) {
    var field = FIELDS[binaryChannelNames[index]];
    if (!field || !field.ringBuf) continue;
    // The main thread retains only the latest row. Full history lives in the Worker.
    field.ringBuf.clear();
    field.ringBuf.push(viewTime, values[index]);
  }
  captureSuperwatchTimelineSpanIfReady();
  return true;
}

function setBinaryHistoryRequester(requester) {
  binaryHistoryRequester = typeof requester === 'function' ? requester : null;
}

function setBinaryDetailRequester(requester) {
  binaryDetailRequester = typeof requester === 'function' ? requester : null;
}

function setBinaryVisibleRangeRequester(requester) {
  binaryVisibleRangeRequester = typeof requester === 'function' ? requester : null;
}

function requestBinaryVisibleRangeAfterTimelineChange() {
  if (IS_SUPERWATCH_MODE && collectionState !== 'running' && binaryVisibleRangeRequester) {
    binaryVisibleRangeRequestCount++;
    canvas.dataset.binaryVisibleRangeRequests = String(binaryVisibleRangeRequestCount);
    binaryVisibleRangeRequester();
  }
}

function requestBinaryDetail(enabled) {
  binaryDetailEnabled = !!enabled;
  if (IS_SUPERWATCH_MODE && binaryDetailRequester) binaryDetailRequester(binaryDetailEnabled);
  if (IS_SUPERWATCH_MODE) {
    resizeWaveformMainRings(binaryDetailEnabled ? RING_BUFFER_CAPACITY : SUPERWATCH_MAIN_RING_CAPACITY);
  }
}

function getBinaryStorageDiagnostics() {
  var mainRingCapacity = 0;
  var mainRingMaxCount = 0;
  for (var name in FIELDS) {
    if (!FIELDS.hasOwnProperty(name) || !FIELDS[name].ringBuf) continue;
    mainRingCapacity = Math.max(mainRingCapacity, Number(FIELDS[name].ringBuf.capacity) || 0);
    mainRingMaxCount = Math.max(mainRingMaxCount, Number(FIELDS[name].ringBuf.count) || 0);
  }
  return {
    historyOwner: IS_SUPERWATCH_MODE ? 'worker' : 'main-thread',
    workerCapacity: IS_SUPERWATCH_MODE ? RING_BUFFER_CAPACITY : 0,
    workerBufferedSamples: IS_SUPERWATCH_MODE ? binaryBufferedSamples : 0,
    mainRingCapacity: mainRingCapacity,
    mainRingMaxCount: mainRingMaxCount,
    bufferStart: binaryBufferStart,
    bufferEnd: binaryBufferEnd,
    detailEnabled: binaryDetailEnabled
  };
}

function getBinaryVisibleRange() {
  if (!IS_BINARY_WAVEFORM_MODE || binaryTimeOrigin === null) return null;
  var range = getVisibleTimeRange();
  var pixelWidth = Math.max(1, Math.floor(canvas.clientWidth || 1));
  return {
    start: (binaryTimeOrigin + range.tMin) * 1000,
    end: (binaryTimeOrigin + range.tMax) * 1000,
    pixelWidth: pixelWidth
  };
}

function parseBinaryEnvelope(envelope) {
  if (!envelope || envelope.mode !== 'min-max-v1' ||
      envelope.timestampKind !== 'sample-milliseconds' ||
      envelope.channelCount !== binaryChannelNames.length ||
      envelope.pointCount > 2 * envelope.pixelWidth * envelope.channelCount) return null;
  var times = new Float64Array(envelope.times);
  var timeIndices = new Uint32Array(envelope.timeIndices);
  var values = new Float32Array(envelope.values);
  var offsets = new Uint32Array(envelope.channelOffsets);
  if (timeIndices.length !== envelope.pointCount || values.length !== envelope.pointCount ||
      offsets.length !== envelope.channelCount + 1 || offsets[0] !== 0 ||
      offsets[offsets.length - 1] !== envelope.pointCount) return null;
  for (var channel = 0; channel < envelope.channelCount; channel++) {
    if (offsets[channel] > offsets[channel + 1]) return null;
  }
  for (var point = 0; point < envelope.pointCount; point++) {
    if (timeIndices[point] >= times.length || !Number.isFinite(values[point])) return null;
  }
  for (var time = 0; time < times.length; time++) {
    if (!Number.isFinite(times[time])) return null;
  }
  return { times: times, timeIndices: timeIndices, values: values, offsets: offsets };
}

function getBinaryEnvelopeChannelRange(name) {
  if (!IS_SUPERWATCH_MODE || !binaryEnvelope) return null;
  var channel = binaryChannelIndex[name];
  if (channel === undefined) return null;
  var start = binaryEnvelope.offsets[channel];
  var end = binaryEnvelope.offsets[channel + 1];
  if (end <= start) return null;
  var minimum = Infinity;
  var maximum = -Infinity;
  for (var point = start; point < end; point++) {
    var value = binaryEnvelope.values[point];
    if (value < minimum) minimum = value;
    if (value > maximum) maximum = value;
  }
  return Number.isFinite(minimum) && Number.isFinite(maximum)
    ? { min: minimum, max: maximum, count: end - start }
    : null;
}

function nearestBinaryEnvelopeSample(name, viewTime) {
  if (!IS_SUPERWATCH_MODE || !binaryEnvelope || binaryTimeOrigin === null) return null;
  var channel = binaryChannelIndex[name];
  if (channel === undefined) return null;
  var targetMs = (binaryTimeOrigin + viewTime) * 1000;
  var start = binaryEnvelope.offsets[channel];
  var end = binaryEnvelope.offsets[channel + 1];
  var best = null;
  var bestDistance = Infinity;
  for (var point = start; point < end; point++) {
    var timeMs = binaryEnvelope.times[binaryEnvelope.timeIndices[point]];
    var distance = Math.abs(timeMs - targetMs) / 1000;
    if (distance < bestDistance) {
      bestDistance = distance;
      best = { value: binaryEnvelope.values[point], distance: distance };
    }
  }
  return best;
}

function renderBinaryEnvelope(envelope, renderWhilePaused) {
  if (!IS_BINARY_WAVEFORM_MODE) return false;
  canvas.dataset.binaryEnvelopeRequestId = String(envelope && envelope.requestId !== undefined
    ? envelope.requestId : '');
  canvas.dataset.binaryEnvelopeInteractive = renderWhilePaused ? 'true' : 'false';
  // Keep background updates from replacing the frozen pause/stop snapshot.
  // An explicit viewport request may still paint its historical range.
  if (IS_SUPERWATCH_MODE && paused && !renderWhilePaused) {
    canvas.dataset.binaryEnvelopeState = 'frozen';
    return true;
  }
  var parsed = parseBinaryEnvelope(envelope);
  if (!parsed) {
    canvas.dataset.binaryEnvelopeState = 'invalid';
    return false;
  }
  canvas.dataset.binaryEnvelopeState = 'rendered';
  canvas.dataset.binaryEnvelopePoints = String(envelope.pointCount);
  binaryEnvelope = parsed;
  if (!paused || renderWhilePaused) {
    drawChart();
    drawMinimap();
    var now = performance.now();
    if (now - binaryLastUiPaint >= 200) {
      binaryLastUiPaint = now;
      updateUI();
      updateWatchTable();
    }
  }
  return true;
}

function resetBinaryStream() {
  if (!IS_BINARY_WAVEFORM_MODE) return;
  binaryTimeOrigin = null;
  binaryLastTimestamp = null;
  binaryLastSequence = null;
  binaryLastDetailSequence = null;
  binaryEnvelope = null;
  binaryBufferStart = null;
  binaryBufferEnd = null;
  binaryBufferedSamples = 0;
  requestBinaryDetail(false);
  resetBinarySampleRate();
  clearFrozenView();
  for (var channel = 0; channel < binaryChannelNames.length; channel++) {
    var field = FIELDS[binaryChannelNames[channel]];
    if (field && field.ringBuf) field.ringBuf.clear();
  }
}

function updateBinaryHealth(health) {
  if (!IS_BINARY_WAVEFORM_MODE || !health) return;
  var stateBadge = document.getElementById('transport-state-badge');
  var healthBadge = document.getElementById('transport-health-badge');
  var phase = String(health.phase || 'stopped');
  if (stateBadge) {
    stateBadge.textContent = 'transport ' + phase +
      (health.reconnectDelayMs ? ' (' + health.reconnectDelayMs + ' ms)' : '');
    stateBadge.className = 'badge ' + (
      phase === 'connected' ? 'badge-ok' :
      phase === 'error' ? 'badge-err' :
      phase === 'stopped' ? 'badge-info' : 'badge-warn'
    );
    if (health.error) stateBadge.title = String(health.error);
    else stateBadge.removeAttribute('title');
  }
  if (healthBadge) {
    var transportDroppedBatches = Math.max(0, Number(health.transportDroppedBatches) || 0);
    var backendDroppedBatches = Math.max(0, Number(health.backendDroppedBatches) || 0);
    var backendDroppedItems = Math.max(0, Number(health.backendDroppedItems) || 0);
    var bufferedSamples = Math.max(0, Number(health.bufferedSamples) || 0);
    var healthText = 'transport ' + transportDroppedBatches +
      ' / backend ' + backendDroppedBatches + '/' + backendDroppedItems +
      ' / buffer ' + bufferedSamples;
    healthBadge.textContent = healthText;
    healthBadge.title = healthText;
    healthBadge.className = 'badge ' + (
      transportDroppedBatches || backendDroppedBatches || backendDroppedItems
        ? 'badge-warn' : 'badge-info'
    );
  }
}

function renderBinaryFrame() {
  if (!IS_BINARY_WAVEFORM_MODE || paused) return;
  drawChart();
  drawMinimap();
  updateUI();
  updateWatchTable();
}

function disposeViewer() {
  clearChartLegendHideTimer();
  stopWatchColumnResize();
  viewerResizeObserver.disconnect();
  viewerAbortController.abort();
  if (es) es.close();
}

if (typeof window !== 'undefined') {
  if (!window.__waveformViewers) window.__waveformViewers = {};
  var binaryViewer = window.__waveformViewers[CONFIG.mode] || {};
  binaryViewer.configureBinaryChannels = configureBinaryChannels;
  binaryViewer.acceptBinaryBatch = acceptBinaryBatch;
  binaryViewer.acceptBinarySummary = acceptBinarySummary;
  binaryViewer.getBinaryVisibleRange = getBinaryVisibleRange;
  binaryViewer.renderBinaryEnvelope = renderBinaryEnvelope;
  binaryViewer.setBinaryHistoryRequester = setBinaryHistoryRequester;
  binaryViewer.setBinaryDetailRequester = setBinaryDetailRequester;
  binaryViewer.setBinaryVisibleRangeRequester = setBinaryVisibleRangeRequester;
  binaryViewer.getStorageDiagnostics = getBinaryStorageDiagnostics;
  binaryViewer.exportBinaryHistorySnapshot = exportBinaryHistorySnapshot;
  binaryViewer.resetBinaryStream = resetBinaryStream;
  binaryViewer.updateBinaryHealth = updateBinaryHealth;
  binaryViewer.updateAcquisitionStatus = syncDashboardStatus;
  binaryViewer.setHiddenChannels = setHiddenChannels;
  binaryViewer.setArraySnapshot = setArraySnapshot;
  binaryViewer.renderBinaryFrame = renderBinaryFrame;
  binaryViewer.setDeviceConnected = setDeviceConnected;
  binaryViewer.dispose = disposeViewer;
  binaryViewer.es = es;
  window.__waveformViewers[CONFIG.mode] = binaryViewer;
}

// ============================================================
// Get visible time range (with timeline zoom/offset)
// ============================================================
function computeFullTimeRange() {
  if (IS_SUPERWATCH_MODE && binaryTimeOrigin !== null &&
      Number.isFinite(binaryBufferStart) && Number.isFinite(binaryBufferEnd)) {
    var retainedMin = binaryBufferStart - binaryTimeOrigin;
    var retainedMax = binaryBufferEnd - binaryTimeOrigin;
    if (retainedMax - retainedMin < 1) retainedMin = retainedMax - 1;
    return { tMin: retainedMin, tMax: retainedMax };
  }
  var tMax = 0, tMin = Infinity;
  for (var k in FIELDS) {
    var pts = FIELDS[k].ringBuf;
    if (FIELDS[k].isArraySnapshot) continue;
    if (pts.count === 0) continue;
    var oldest = pts.oldest();
    var latest = pts.latest();
    if (oldest && oldest.t < tMin) tMin = oldest.t;
    if (latest && latest.t > tMax) tMax = latest.t;
  }
  if (!Number.isFinite(tMin)) tMin = 0;
  if (IS_SUPERWATCH_MODE && Number.isFinite(currentInterval) && currentInterval > 0) {
    // Reserve only the live viewport during prefill. Retained history grows
    // independently behind it and becomes navigable after pause.
    var prefillSpan = Number.isFinite(superwatchTimelineSpan)
      ? superwatchTimelineSpan
      : Math.max(1, (INITIAL_RING_BUFFER_CAPACITY - 1) * currentInterval);
    if (tMax - tMin < prefillSpan) tMin = tMax - prefillSpan;
  }
  if (tMax - tMin < 1) tMin = tMax - 1;
  return { tMin: tMin, tMax: tMax };
}

function getFullTimeRange() {
  if (IS_SUPERWATCH_MODE && paused && frozenFullTimeRange) {
    return { tMin: frozenFullTimeRange.tMin, tMax: frozenFullTimeRange.tMax };
  }
  return computeFullTimeRange();
}

function getVisibleTimeRange() {
  var full = getFullTimeRange();
  var range = full.tMax - full.tMin;
  if (range <= 0) return full;

  var visibleRange;
  if (IS_SUPERWATCH_MODE && superwatchTimelineSpan !== null) {
    visibleRange = superwatchTimelineSpan === Infinity
      ? range
      : Math.min(range, superwatchTimelineSpan);
    timelineView.zoom = range / visibleRange;
  } else {
    visibleRange = range / timelineView.zoom;
  }
  if (IS_SUPERWATCH_MODE && collectionState === 'running') {
    var liveAvailable = Math.max(0, range - visibleRange);
    superwatchTimelineLag = Math.max(0, Math.min(liveAvailable, superwatchTimelineLag));
    timelineView.offset = liveAvailable > 0
      ? 1 - superwatchTimelineLag / liveAvailable
      : 1;
  }
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
  var envelopeRange = getBinaryEnvelopeChannelRange(name);
  if (!m || (m.isArraySnapshot ? !m.arrayValues || m.arrayValues.length < 1 :
      (!envelopeRange && m.ringBuf.count < 1))) return null;
  var ys = ensureChannelYState(name);

  var baseMin = m.isArraySnapshot ? Math.min.apply(null, m.arrayValues) :
    (envelopeRange ? envelopeRange.min : m.ringBuf._min);
  var baseMax = m.isArraySnapshot ? Math.max.apply(null, m.arrayValues) :
    (envelopeRange ? envelopeRange.max : m.ringBuf._max);
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

function getAutoSharedYRange(excludedName) {
  var yMin = Infinity;
  var yMax = -Infinity;
  for (var name in FIELDS) {
    var field = FIELDS[name];
    if (excludedName && name === excludedName) continue;
    if (!field.visible) continue;
    if (field.isArraySnapshot) {
      if (!field.arrayValues || !field.arrayValues.length) continue;
      yMin = Math.min(yMin, Math.min.apply(null, field.arrayValues));
      yMax = Math.max(yMax, Math.max.apply(null, field.arrayValues));
    } else {
      var envelopeRange = getBinaryEnvelopeChannelRange(name);
      if (envelopeRange) {
        yMin = Math.min(yMin, envelopeRange.min);
        yMax = Math.max(yMax, envelopeRange.max);
      } else {
        if (!field.ringBuf || field.ringBuf.count < 1) continue;
        if (Number.isFinite(field.ringBuf._min)) yMin = Math.min(yMin, field.ringBuf._min);
        if (Number.isFinite(field.ringBuf._max)) yMax = Math.max(yMax, field.ringBuf._max);
      }
    }
  }
  if (!Number.isFinite(yMin) || !Number.isFinite(yMax)) return null;
  var pad = (yMax - yMin) * 0.1 || 1;
  yMin -= pad;
  yMax += pad;
  return { yMin: yMin, yMax: yMax };
}

function hasManualSharedYRange() {
  return globalYView.autoRange === false &&
    globalYView.manualMin !== null && globalYView.manualMin !== undefined && globalYView.manualMin !== '' &&
    globalYView.manualMax !== null && globalYView.manualMax !== undefined && globalYView.manualMax !== '' &&
    Number.isFinite(Number(globalYView.manualMin)) &&
    Number.isFinite(Number(globalYView.manualMax)) &&
    Number(globalYView.manualMax) > Number(globalYView.manualMin);
}

function getSharedYRange(excludedName) {
  if (hasManualSharedYRange()) {
    return {
      yMin: Number(globalYView.manualMin),
      yMax: Number(globalYView.manualMax)
    };
  }
  if (IS_SUPERWATCH_MODE && paused && frozenSharedYRange && !excludedName) {
    return { yMin: frozenSharedYRange.yMin, yMax: frozenSharedYRange.yMax };
  }
  var autoRange = getAutoSharedYRange(excludedName);
  if (!autoRange) return null;
  var yMin = autoRange.yMin;
  var yMax = autoRange.yMax;
  var range = yMax - yMin;
  var center = (yMin + yMax) / 2 + globalYView.offset;
  var visibleRange = range / globalYView.zoom;
  return {
    yMin: center - visibleRange / 2,
    yMax: center + visibleRange / 2
  };
}

function getSuperwatchPanelAtY(localY) {
  if (!IS_SUPERWATCH_MODE || !chartPanelLayout || !chartPanelLayout.split) {
    return { kind: 'main', top: chartPanelLayout ? chartPanelLayout.mainTop : 0, height: chartPanelLayout ? chartPanelLayout.mainHeight : 1 };
  }
  if (localY >= chartPanelLayout.splitTop && localY <= chartPanelLayout.splitTop + chartPanelLayout.splitHeight) {
    return { kind: 'split', name: splitChannelName, top: chartPanelLayout.splitTop, height: chartPanelLayout.splitHeight };
  }
  return { kind: 'main', top: chartPanelLayout.mainTop, height: chartPanelLayout.mainHeight };
}

function getPanelYRange(panel) {
  if (panel && panel.kind === 'split' && panel.name) return getChannelYRange(panel.name);
  return getSharedYRange(splitChannelName);
}

function setPanelYRange(panel, yMin, yMax) {
  if (panel && panel.kind === 'split' && panel.name) {
    var ys = ensureChannelYState(panel.name);
    yMin = Number(yMin);
    yMax = Number(yMax);
    if (!Number.isFinite(yMin) || !Number.isFinite(yMax) || !(yMax > yMin)) return false;
    ys.autoRange = false;
    ys.zoom = 1;
    ys.offset = 0;
    ys.manualMin = yMin;
    ys.manualMax = yMax;
    return true;
  }
  return setManualSharedYRange(yMin, yMax);
}

function zoomPanelYAt(panel, anchorRatio, deltaY) {
  var current = getPanelYRange(panel);
  if (!current) return false;
  var ratio = Number(anchorRatio);
  if (!Number.isFinite(ratio)) ratio = 0.5;
  ratio = Math.max(0, Math.min(1, ratio));
  var span = current.yMax - current.yMin;
  if (!(span > 0)) return false;
  var anchor = current.yMax - ratio * span;
  var nextSpan = span * (deltaY > 0 ? 1.25 : 0.8);
  return setPanelYRange(
    panel,
    anchor - (1 - ratio) * nextSpan,
    anchor + ratio * nextSpan
  );
}

function resetPanelY(panel) {
  if (panel && panel.kind === 'split' && panel.name) resetChannelY(panel.name);
  else globalYView = { zoom: 1, offset: 0, autoRange: true, manualMin: null, manualMax: null };
  drawChart();
}

function setManualSharedYRange(yMin, yMax) {
  if (yMin === null || yMin === undefined || yMin === '' ||
      yMax === null || yMax === undefined || yMax === '') return false;
  yMin = Number(yMin);
  yMax = Number(yMax);
  if (!Number.isFinite(yMin) || !Number.isFinite(yMax) || !(yMax > yMin)) return false;
  globalYView.autoRange = false;
  globalYView.manualMin = yMin;
  globalYView.manualMax = yMax;
  globalYView.zoom = 1;
  globalYView.offset = 0;
  return true;
}

function zoomSharedYAt(anchorRatio, deltaY) {
  var current = getSharedYRange();
  if (!current) return false;
  var ratio = Number(anchorRatio);
  if (!Number.isFinite(ratio)) ratio = 0.5;
  ratio = Math.max(0, Math.min(1, ratio));
  var span = current.yMax - current.yMin;
  if (!(span > 0)) return false;
  var anchor = current.yMax - ratio * span;
  var nextSpan = span * (deltaY > 0 ? 1.25 : 0.8);
  return setManualSharedYRange(
    anchor - (1 - ratio) * nextSpan,
    anchor + ratio * nextSpan
  );
}

function resetChannelY(name) {
  channelYState[name] = { zoom: 1, offset: 0, autoRange: true, manualMin: null, manualMax: null };
}

function clampAxisOffset(value) {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(1, value));
}

function captureSuperwatchTimelineLag() {
  if (!IS_SUPERWATCH_MODE || collectionState !== 'running') return;
  var full = getFullTimeRange();
  var fullRange = full.tMax - full.tMin;
  if (!(fullRange > 0)) return;
  var visibleRange = superwatchTimelineSpan !== null
    ? (superwatchTimelineSpan === Infinity ? fullRange : Math.min(fullRange, superwatchTimelineSpan))
    : fullRange / timelineView.zoom;
  var available = Math.max(0, fullRange - visibleRange);
  superwatchTimelineLag = (1 - clampAxisOffset(timelineView.offset)) * available;
}

function zoomTimelineAt(clientX, deltaY) {
  var full = getFullTimeRange();
  var fullRange = full.tMax - full.tMin;
  if (!(fullRange > 0)) return;
  var current = getVisibleTimeRange();
  var rect = canvas.getBoundingClientRect();
  var pointerX = Number.isFinite(clientX) ? clientX : rect.left + rect.width / 2;
  var ratio = Math.max(0, Math.min(1, (pointerX - rect.left) / Math.max(1, rect.width)));
  var anchor = current.tMin + ratio * (current.tMax - current.tMin);
  var factor = deltaY > 0 ? 0.8 : 1.25;
  var nextZoom = Math.max(1, Math.min(MAX_TIMELINE_ZOOM, timelineView.zoom * factor));
  timelineView.zoom = nextZoom;
  if (nextZoom === 1) {
    timelineView.offset = 0;
    if (IS_SUPERWATCH_MODE) superwatchTimelineSpan = Infinity;
  } else {
    var nextRange = fullRange / nextZoom;
    if (IS_SUPERWATCH_MODE) superwatchTimelineSpan = nextRange;
    var available = fullRange - nextRange;
    timelineView.offset = clampAxisOffset((anchor - ratio * nextRange - full.tMin) / available);
  }
  captureSuperwatchTimelineLag();
  drawChart();
  drawMinimap();
  requestBinaryVisibleRangeAfterTimelineChange();
}

function zoomVisibleY(deltaY, anchorRatio, panel) {
  var factor = deltaY > 0 ? 0.8 : 1.25;
  if (IS_SUPERWATCH_MODE) {
    zoomPanelYAt(panel || { kind: 'main' }, anchorRatio, deltaY);
    drawChart();
    return;
  }
  for (var name in FIELDS) {
    if (!FIELDS[name].visible || FIELDS[name].ringBuf.count < 2) continue;
    var state = ensureChannelYState(name);
    state.autoRange = false;
    state.manualMin = null;
    state.manualMax = null;
    state.zoom = Math.max(0.1, Math.min(100, state.zoom * factor));
  }
  drawChart();
}

function resetVisibleY() {
  globalYView = { zoom: 1, offset: 0, autoRange: true, manualMin: null, manualMax: null };
  for (var name in channelYState) resetChannelY(name);
  drawChart();
}

var axisDrag = null;

if (xAxisHit && yAxisHit) {
  xAxisHit.addEventListener('wheel', function(e) {
    e.preventDefault();
    zoomTimelineAt(e.clientX, e.deltaY);
  }, { passive: false, signal: viewerAbortController.signal });
  yAxisHit.addEventListener('wheel', function(e) {
    e.preventDefault();
    var rect = yAxisHit.getBoundingClientRect();
    var canvasRect = canvas.getBoundingClientRect();
    var panel = IS_SUPERWATCH_MODE
      ? getSuperwatchPanelAtY(e.clientY - canvasRect.top)
      : null;
    var ratio = panel
      ? (e.clientY - canvasRect.top - panel.top) / Math.max(1, panel.height)
      : (e.clientY - rect.top) / Math.max(1, rect.height);
    zoomVisibleY(e.deltaY, ratio, panel);
  }, { passive: false, signal: viewerAbortController.signal });

  xAxisHit.addEventListener('mousedown', function(e) {
    if (e.button !== 0 || timelineView.zoom <= 1) return;
    axisDrag = { mode: 'x', startX: e.clientX, startOffset: timelineView.offset };
    e.preventDefault();
  }, { signal: viewerAbortController.signal });
  yAxisHit.addEventListener('mousedown', function(e) {
    if (e.button !== 0) return;
    if (IS_SUPERWATCH_MODE) {
      var yRect = canvas.getBoundingClientRect();
      var panel = getSuperwatchPanelAtY(e.clientY - yRect.top);
      var panelRange = getPanelYRange(panel);
      if (!panelRange) return;
      axisDrag = {
        mode: 'y-panel',
        panel: panel,
        startY: e.clientY,
        yMin: panelRange.yMin,
        yMax: panelRange.yMax,
        range: panelRange.yMax - panelRange.yMin,
        height: Math.max(1, panel.height)
      };
      e.preventDefault();
      return;
    }
    var offsets = {};
    var ranges = {};
    for (var name in FIELDS) {
      if (!FIELDS[name].visible ||
          (FIELDS[name].isArraySnapshot ? !FIELDS[name].arrayValues || FIELDS[name].arrayValues.length < 2 : FIELDS[name].ringBuf.count < 2)) continue;
      var state = ensureChannelYState(name);
      var range = getChannelYRange(name);
      state.autoRange = false;
      state.manualMin = null;
      state.manualMax = null;
      offsets[name] = state.offset;
      ranges[name] = range ? range.yMax - range.yMin : 1;
    }
    axisDrag = { mode: 'y', startY: e.clientY, offsets: offsets, ranges: ranges };
    e.preventDefault();
  }, { signal: viewerAbortController.signal });

  addViewerGlobalListener(window, 'mousemove', function(e) {
    if (!axisDrag) return;
    var rect = canvas.getBoundingClientRect();
    if (axisDrag.mode === 'plot-panel') {
      var plotPixelDx = e.clientX - axisDrag.startX;
      var plotPixelDy = e.clientY - axisDrag.startY;
      if (!axisDrag.panX && axisDrag.panTimeline && Math.abs(plotPixelDx) >= 5) axisDrag.panX = true;
      if (!axisDrag.panY && Math.abs(plotPixelDy) >= 5) axisDrag.panY = true;
      if (!axisDrag.panX && !axisDrag.panY) return;
      if (!axisDrag.moved) {
        axisDrag.moved = true;
        probeDownPos = null;
      }
      if (axisDrag.panY) {
        var plotShift = plotPixelDy / axisDrag.plotHeight * axisDrag.range;
        setPanelYRange(axisDrag.panel, axisDrag.yMin + plotShift, axisDrag.yMax + plotShift);
      }
      if (axisDrag.panX) {
        var plotDx = plotPixelDx / axisDrag.plotWidth;
        timelineView.offset = clampAxisOffset(
          axisDrag.startTimelineOffset - plotDx * axisDrag.timelinePanScale
        );
        captureSuperwatchTimelineLag();
        drawMinimap();
        requestBinaryVisibleRangeAfterTimelineChange();
      }
      drawChart();
    } else if (axisDrag.mode === 'x') {
      var dx = (e.clientX - axisDrag.startX) / Math.max(1, rect.width);
      timelineView.offset = clampAxisOffset(axisDrag.startOffset - dx);
      captureSuperwatchTimelineLag();
      drawChart();
      drawMinimap();
      requestBinaryVisibleRangeAfterTimelineChange();
    } else if (axisDrag.mode === 'y-panel') {
      var sharedDy = (e.clientY - axisDrag.startY) / axisDrag.height;
      var sharedShift = sharedDy * axisDrag.range;
      setPanelYRange(axisDrag.panel, axisDrag.yMin + sharedShift, axisDrag.yMax + sharedShift);
      drawChart();
    } else {
      var dy = (e.clientY - axisDrag.startY) / Math.max(1, rect.height);
      for (var name in axisDrag.offsets) {
        ensureChannelYState(name).offset = axisDrag.offsets[name] + dy * axisDrag.ranges[name];
      }
      drawChart();
    }
  });
  addViewerGlobalListener(window, 'mouseup', function() { axisDrag = null; });

  xAxisHit.addEventListener('dblclick', function() {
    resetTimelineView();
    drawChart();
    drawMinimap();
    requestBinaryVisibleRangeAfterTimelineChange();
  }, { signal: viewerAbortController.signal });
  yAxisHit.addEventListener('dblclick', function(e) {
    if (!IS_SUPERWATCH_MODE) {
      resetVisibleY();
      return;
    }
    var canvasRect = canvas.getBoundingClientRect();
    resetPanelY(getSuperwatchPanelAtY(e.clientY - canvasRect.top));
  }, { signal: viewerAbortController.signal });
}

function formatTimeAxisValue(seconds) {
  if (timeUnit === 'us') return Math.round(seconds * 1000000) + 'us';
  if (timeUnit === 's') return seconds.toFixed(3) + 's';
  return (seconds * 1000).toFixed(1) + 'ms';
}

function formatYAxisValue(value, span) {
  var absValue = Math.abs(value);
  var absSpan = Math.abs(span);
  if (
    absValue >= 1000000 || (absValue > 0 && absValue < 0.0001) ||
    absSpan >= 10000000 || (absSpan > 0 && absSpan < 0.001)
  ) {
    return value.toExponential(2);
  }
  var step = absSpan / 5;
  var decimals = step >= 100 ? 0 : (step >= 10 ? 1 : (step >= 1 ? 2 : (step >= 0.1 ? 3 : 4)));
  var text = value.toFixed(decimals);
  if (decimals > 0) text = text.replace(/\.?0+$/, '');
  return text === '-0' ? '0' : text;
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

  // The accepted envelope is the frozen snapshot while paused/stopped. Keep
  // drawing it during timeline interaction; background responses are filtered
  // by renderBinaryEnvelope and cannot replace it.
  var useBinaryEnvelope = IS_BINARY_WAVEFORM_MODE && binaryEnvelope;

  // Count visible channels
  var visChNames = sortedFieldNames().filter(function(k) {
    if (!FIELDS[k].visible) return false;
    if (FIELDS[k].isArraySnapshot) return !!FIELDS[k].arrayValues && FIELDS[k].arrayValues.length >= 2;
    var envelopeIndex = binaryChannelIndex[k];
    if (useBinaryEnvelope && envelopeIndex !== undefined) {
      return binaryEnvelope.offsets[envelopeIndex + 1] - binaryEnvelope.offsets[envelopeIndex] >= 2;
    }
    return FIELDS[k].ringBuf.count >= 1;
  });
  var mr = 16, mt = 8, mb = 32, ml = IS_SUPERWATCH_MODE ? 64 : 16;
  var pw = W - ml - mr;
  var ph = H - mt - mb;
  if (pw <= 0 || ph <= 0) return;

  var splitActive = IS_SUPERWATCH_MODE && splitChannelName &&
    FIELDS[splitChannelName] && FIELDS[splitChannelName].visible &&
    visChNames.length >= 2;
  if (IS_SUPERWATCH_MODE && splitChannelName && !splitActive) {
    splitChannelName = null;
    updateChartLegend();
  }
  var panelGap = splitActive ? 12 : 0;
  var mainHeight = splitActive ? Math.max(1, Math.floor((ph - panelGap) / 2)) : ph;
  var splitHeight = splitActive ? Math.max(1, ph - panelGap - mainHeight) : 0;
  var mainTop = mt;
  var splitTop = splitActive ? mainTop + mainHeight + panelGap : 0;
  chartPanelLayout = {
    split: !!splitActive,
    mainTop: mainTop,
    mainHeight: mainHeight,
    splitTop: splitTop,
    splitHeight: splitHeight,
    left: ml,
    width: pw
  };
  updateSplitPanelControl(splitActive, splitActive ? mainTop + mainHeight + panelGap / 2 : null);

  // Global Y range (shared in SuperWatch main panel, legacy per-channel in VOFA)
  var yMin = Infinity, yMax = -Infinity;
  var hasData = false;
  var mainYRange = null;
  var splitYRange = null;
  if (IS_SUPERWATCH_MODE) {
    mainYRange = getSharedYRange(splitActive ? splitChannelName : null);
    if (!mainYRange) return;
    yMin = mainYRange.yMin;
    yMax = mainYRange.yMax;
    if (splitActive) {
      splitYRange = getChannelYRange(splitChannelName);
      if (!splitYRange) return;
    }
  } else {
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
    if (globalYView.zoom !== 1) {
      var yCenter = (yMin + yMax) / 2 + globalYView.offset;
      var yRange = (yMax - yMin) / globalYView.zoom;
      yMin = yCenter - yRange / 2;
      yMax = yCenter + yRange / 2;
    }
  }

  function tx(v) { return ml + (v - tMin) / (tMax - tMin || 1) * pw; }
  function tyGlobal(v) { return mainTop + mainHeight - (v - yMin) / (yMax - yMin || 1) * mainHeight; }

  // Per-channel Y: each channel normalized to its own min/max by default
  // (avoids small signals being crushed when channels have very different ranges)
  function tyForChannel(v, name) {
    if (IS_SUPERWATCH_MODE) {
      if (splitActive && name === splitChannelName && splitYRange) {
        return splitTop + splitHeight - (v - splitYRange.yMin) /
          (splitYRange.yMax - splitYRange.yMin || 1) * splitHeight;
      }
      return tyGlobal(v);
    }
    if (!name) return tyGlobal(v);
    var yr = getChannelYRange(name);
    if (!yr) return tyGlobal(v);
    return mt + ph - (v - yr.yMin) / (yr.yMax - yr.yMin || 1) * ph;
  }

  // Grid — horizontal lines and Y labels are independent per SuperWatch panel.
  ctx.strokeStyle = GRID_COLOR;
  ctx.lineWidth = 0.5;
  function drawPanelGrid(panelTop, panelHeight, panelRange) {
    for (var panelTick = 0; panelTick <= 5; panelTick++) {
      var panelY = Math.round(panelTop + panelHeight * panelTick / 5) + 0.5;
      ctx.beginPath();
      ctx.moveTo(ml, panelY); ctx.lineTo(ml + pw, panelY);
      ctx.stroke();
      if (IS_SUPERWATCH_MODE && panelRange) {
        var panelValue = panelRange.yMax - (panelRange.yMax - panelRange.yMin) * panelTick / 5;
        ctx.fillStyle = TEXT_DIM;
        ctx.font = '11px ' + getComputedStyle(document.body).getPropertyValue('--font-mono');
        ctx.textAlign = 'right';
        ctx.fillText(formatYAxisValue(panelValue, panelRange.yMax - panelRange.yMin), ml - 8, panelY + 4);
      }
    }
  }
  drawPanelGrid(mainTop, mainHeight, IS_SUPERWATCH_MODE ? mainYRange : { yMin: yMin, yMax: yMax });
  if (splitActive) drawPanelGrid(splitTop, splitHeight, splitYRange);

  // Time grid is shared, but each split panel gets its own vertical strokes.
  for (var i = 0; i <= 5; i++) {
    var xv = tMin + (tMax - tMin) * i / 5;
    var xp = Math.round(tx(xv)) + 0.5;
    ctx.beginPath();
    ctx.moveTo(xp, mainTop); ctx.lineTo(xp, mainTop + mainHeight);
    if (splitActive) {
      ctx.moveTo(xp, splitTop); ctx.lineTo(xp, splitTop + splitHeight);
    }
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
  ctx.translate(10, mainTop + mainHeight/2);
  ctx.rotate(-Math.PI/2);
  ctx.fillText('value', 0, 0);
  ctx.restore();
  if (splitActive) {
    ctx.save();
    ctx.translate(10, splitTop + splitHeight/2);
    ctx.rotate(-Math.PI/2);
    ctx.fillText('value', 0, 0);
    ctx.restore();
    ctx.fillStyle = GRID_COLOR;
    ctx.fillRect(ml, mainTop + mainHeight + Math.floor(panelGap / 2), pw, 1);
  }

  var names = sortedFieldNames();
  for (var ni = 0; ni < names.length; ni++) {
    var name = names[ni];
    var meta = FIELDS[name];
    var envelopeChannelRange = getBinaryEnvelopeChannelRange(name);
    if (!meta.visible || (!meta.isArraySnapshot && meta.ringBuf.count < 1 && !envelopeChannelRange) ||
        (meta.isArraySnapshot && (!meta.arrayValues || meta.arrayValues.length < 2))) continue;

    var curveTop = splitActive && name === splitChannelName ? splitTop : mainTop;
    var curveHeight = splitActive && name === splitChannelName ? splitHeight : mainHeight;
    ctx.save();
    ctx.beginPath();
    ctx.rect(ml, curveTop, pw, curveHeight);
    ctx.clip();

    ctx.strokeStyle = meta.color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    var started = false;
    var ring = meta.ringBuf;
    if (meta.isArraySnapshot) {
      var arrayValues = meta.arrayValues;
      for (var arrayIndex = 0; arrayIndex < arrayValues.length; arrayIndex++) {
        var arrayX = ml + (arrayIndex / Math.max(1, arrayValues.length - 1)) * pw;
        var arrayY = tyForChannel(arrayValues[arrayIndex], name);
        if (arrayIndex === 0) { ctx.moveTo(arrayX, arrayY); started = true; }
        else ctx.lineTo(arrayX, arrayY);
      }
      ctx.stroke();
      ctx.restore();
      continue;
    }
    var envelopeChannel = binaryChannelIndex[name];
    if (useBinaryEnvelope && envelopeChannel !== undefined) {
      var envelopeStart = binaryEnvelope.offsets[envelopeChannel];
      var envelopeEnd = binaryEnvelope.offsets[envelopeChannel + 1];
      for (var envelopePoint = envelopeStart; envelopePoint < envelopeEnd; envelopePoint++) {
        var timeIndex = binaryEnvelope.timeIndices[envelopePoint];
        var envelopeTime = binaryEnvelope.times[timeIndex] / 1000 - binaryTimeOrigin;
        var envelopeX = tx(envelopeTime);
        var envelopeY = tyForChannel(binaryEnvelope.values[envelopePoint], name);
        if (envelopeX < ml - 10 || envelopeX > ml + pw + 10) continue;
        if (!started) { ctx.moveTo(envelopeX, envelopeY); started = true; }
        else ctx.lineTo(envelopeX, envelopeY);
      }
    } else {
      var ringStart = 0;
      var ringEnd = ring.count;
      var ringStep = 1;
      if (IS_SUPERWATCH_MODE && paused) {
        ringStart = Math.max(0, ring.lowerBoundTime(tMin) - 1);
        ringEnd = Math.min(ring.count, ring.lowerBoundTime(tMax) + 1);
        ringStep = Math.max(1, Math.ceil((ringEnd - ringStart) / Math.max(2, Math.floor(pw))));
      }
      for (var i = ringStart; i < ringEnd; i += ringStep) {
        var sx = tx(ring.timeAt(i)), sy = tyForChannel(ring.valueAt(i), name);
        if (sx < ml - 10 || sx > ml + pw + 10) continue;
        if (!started) { ctx.moveTo(sx, sy); started = true; }
        else ctx.lineTo(sx, sy);
      }
    }
    ctx.stroke();
    ctx.restore();
  }

  // Per-channel Y labels at right edge of chart, staggered to avoid overlap
  // Max labels pinned at top, Min labels pinned at bottom, each 14px apart
  for (var ni = 0; !IS_SUPERWATCH_MODE && ni < visChNames.length; ni++) {
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
    var triggerPanel = splitActive && triggerSettings.source === splitChannelName
      ? { kind: 'split', name: splitChannelName }
      : { kind: 'main' };
    var trigY = tyForChannel(triggerSettings.level, triggerSettings.source);
    var triggerTop = triggerPanel.kind === 'split' ? splitTop : mainTop;
    var triggerBottom = triggerPanel.kind === 'split' ? splitTop + splitHeight : mainTop + mainHeight;
    if (trigY >= triggerTop && trigY <= triggerBottom) {
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

  updateChartLegend();

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
      var hoverRing = FIELDS[k].ringBuf;
      if (!FIELDS[k].visible || (hoverRing.count < 1 && !binaryEnvelope)) continue;
      var hoverSample = IS_SUPERWATCH_MODE
        ? nearestBinaryEnvelopeSample(k, hoverT)
        : hoverRing.nearestSample(hoverT);
      var bestY = hoverSample ? hoverSample.value : null;
      var bestDist = hoverSample ? hoverSample.distance : Infinity;
      if (bestY !== null && bestDist < (tMax - tMin) / pw * 15) {
        var tipVal = formatTypedValue(bestY, FIELDS[k]);
        var enumName = _resolveEnumName(k, bestY);
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
      zoomTimelineAt(e.clientX, e.deltaY);
      return;
    }

    if (IS_SUPERWATCH_MODE) {
      var wheelPanel = getSuperwatchPanelAtY(my);
      zoomVisibleY(
        e.deltaY,
        (my - wheelPanel.top) / Math.max(1, wheelPanel.height),
        wheelPanel
      );
      return;
    }

    if (e.ctrlKey) {
      // Ctrl+wheel: per-channel Y-axis zoom
      var hoverT = tMin + (mx - ml) / pw * (tMax - tMin);
      var closestChannel = null;
      var closestDist = Infinity;
      for (var k in FIELDS) {
        if (!FIELDS[k].visible) continue;
        var zoomRing = FIELDS[k].ringBuf;
        var zoomSample = zoomRing.nearestSample(hoverT);
        if (zoomSample && zoomSample.distance < closestDist) {
          closestDist = zoomSample.distance;
          closestChannel = k;
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
      if (!FIELDS[k].visible ||
          (FIELDS[k].isArraySnapshot ? !FIELDS[k].arrayValues || FIELDS[k].arrayValues.length < 2 : FIELDS[k].ringBuf.count < 2)) continue;
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

    if (IS_SUPERWATCH_MODE) {
      resetPanelY(getSuperwatchPanelAtY(my));
      return;
    }

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
      var triggerPanel = splitActive && triggerSettings.source === splitChannelName
        ? { kind: 'split', name: splitChannelName }
        : { kind: 'main' };
      var trigY = tyForChannel(triggerSettings.level, triggerSettings.source);
      var triggerTop = triggerPanel.kind === 'split' ? splitTop : mainTop;
      var triggerBottom = triggerPanel.kind === 'split' ? splitTop + splitHeight : mainTop + mainHeight;
      if (Math.abs(my - trigY) < 8 && my >= triggerTop && my <= triggerBottom) {
        draggingTrigger = true;
        draggingTriggerPanel = triggerPanel;
        e.preventDefault();
        return;
      }
    }

    // Timeline hand tools remain horizontal-only.
    if (e.button === 1 || (e.button === 0 && e.altKey)) {
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

    if (IS_SUPERWATCH_MODE && e.button === 0 && !e.ctrlKey && !e.shiftKey && !e.altKey && !spaceHeld &&
        mx >= ml && mx <= ml + pw && my >= mt && my <= mt + ph) {
      var plotPanel = getSuperwatchPanelAtY(my);
      var plotRange = getPanelYRange(plotPanel);
      if (!plotRange) return;
      var fullTimeline = getFullTimeRange();
      var fullTimelineSpan = fullTimeline.tMax - fullTimeline.tMin;
      var visibleTimelineSpan = fullTimelineSpan / timelineView.zoom;
      var availableTimelineSpan = fullTimelineSpan - visibleTimelineSpan;
      var timelinePanScale = availableTimelineSpan > 0
        ? visibleTimelineSpan / availableTimelineSpan
        : 0;
      axisDrag = {
        mode: 'plot-panel',
        panel: plotPanel,
        startX: e.clientX,
        startY: e.clientY,
        yMin: plotRange.yMin,
        yMax: plotRange.yMax,
        range: plotRange.yMax - plotRange.yMin,
        plotWidth: Math.max(1, pw),
        plotHeight: Math.max(1, plotPanel.height),
        startTimelineOffset: timelineView.offset,
        panTimeline: timelineView.zoom > 1 && timelinePanScale > 0,
        timelinePanScale: timelinePanScale,
        panX: false,
        panY: false,
        moved: false
      };
      e.preventDefault();
      return;
    }

    // Preserve VOFA's plain-left timeline pan when zoomed in.
    if (e.button === 0 && !e.ctrlKey && !e.shiftKey && !spaceHeld && timelineView.zoom > 1 &&
        mx >= ml && mx <= ml + pw && my >= mt && my <= mt + ph) {
      timelineView.dragging = true;
      timelineView.dragStartX = mx;
      timelineView.dragStartOffset = timelineView.offset;
      e.preventDefault();
      return;
    }
  };

  canvas.onmousemove = (function(origFn) {
    return function(e) {
      if (axisDrag && axisDrag.mode === 'plot-panel') return;
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
        var triggerTarget = draggingTriggerPanel || { kind: 'main' };
        var triggerRange = getPanelYRange(triggerTarget) || { yMin: yMin, yMax: yMax };
        var triggerTop = triggerTarget.kind === 'split' ? splitTop : mainTop;
        var triggerHeight = triggerTarget.kind === 'split' ? splitHeight : mainHeight;
        var newLevel = triggerRange.yMin +
          (1 - (my - triggerTop) / Math.max(1, triggerHeight)) *
          (triggerRange.yMax - triggerRange.yMin);
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
        captureSuperwatchTimelineLag();
        drawChart();
        drawMinimap();
        requestBinaryVisibleRangeAfterTimelineChange();
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
function isEditableKeyboardTarget(target) {
  if (!(target instanceof Element)) return false;
  return !!target.closest(
    'input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="textbox"]'
  );
}

addViewerGlobalListener(document, 'keydown', function(e) {
  if (e.key === ' ' && !e.repeat && !isEditableKeyboardTarget(e.target)) {
    spaceHeld = true;
    canvas.style.cursor = 'grab';
    e.preventDefault();
  }
});
addViewerGlobalListener(document, 'keyup', function(e) {
  if (e.key === ' ') {
    spaceHeld = false;
    canvas.style.cursor = '';
  }
});

addViewerGlobalListener(window, 'mouseup', function(e) {
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
  draggingTriggerPanel = null;
  timelineView.dragging = false;
  cursorState.dragging = null;
  canvas.style.cursor = spaceHeld ? 'grab' : '';
});

addViewerGlobalListener(window, 'keydown', function(e) {
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
  var names = sortedFieldNames();
  var deltaLimit = (window.innerWidth <= 640) ? 2 : 6;

  // Minimap compact readout
  var lines = ['dT=' + dt.toFixed(4) + 's', 'f=' + freq + 'Hz'];
  var deltaCount = 0;
  for (var i = 0; i < names.length; i++) {
    var name = names[i];
    var meta = FIELDS[name];
    if (!meta || !meta.visible || !meta.ringBuf || (meta.ringBuf.count < 1 && !binaryEnvelope)) continue;
    var aSample = IS_SUPERWATCH_MODE ? nearestBinaryEnvelopeSample(name, cursorState.a.t) : null;
    var bSample = IS_SUPERWATCH_MODE ? nearestBinaryEnvelopeSample(name, cursorState.b.t) : null;
    var av = aSample ? aSample.value : sampleValueAt(meta.ringBuf, cursorState.a.t);
    var bv = bSample ? bSample.value : sampleValueAt(meta.ringBuf, cursorState.b.t);
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
        if (!vm || !vm.visible || !vm.ringBuf || (vm.ringBuf.count < 1 && !binaryEnvelope)) continue;
        var vaSample = IS_SUPERWATCH_MODE ? nearestBinaryEnvelopeSample(vn, cursorState.a.t) : null;
        var vbSample = IS_SUPERWATCH_MODE ? nearestBinaryEnvelopeSample(vn, cursorState.b.t) : null;
        var va = vaSample ? vaSample.value : sampleValueAt(vm.ringBuf, cursorState.a.t);
        var vb = vbSample ? vbSample.value : sampleValueAt(vm.ringBuf, cursorState.b.t);
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
  var count = ringBuf ? ringBuf.count : 0;
  if (count < 1) return null;
  var firstTime = ringBuf.timeAt(0);
  if (t <= firstTime) return ringBuf.valueAt(0);
  var lastIndex = count - 1;
  var lastTime = ringBuf.timeAt(lastIndex);
  if (t >= lastTime) return ringBuf.valueAt(lastIndex);

  // Ring timestamps are logical-order monotonic. Find the first sample at or
  // after t without materializing point objects, then compare its predecessor.
  var low = 1, high = lastIndex;
  while (low < high) {
    var mid = low + ((high - low) >> 1);
    if (ringBuf.timeAt(mid) < t) low = mid + 1;
    else high = mid;
  }
  var before = low - 1;
  return (t - ringBuf.timeAt(before) <= ringBuf.timeAt(low) - t)
    ? ringBuf.valueAt(before)
    : ringBuf.valueAt(low);
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
  var names = sortedFieldNames();
  var useBinaryEnvelope = IS_BINARY_WAVEFORM_MODE && binaryEnvelope;
  minimapCtx.globalAlpha = 0.4;
  for (var ni = 0; ni < names.length; ni++) {
    var meta = FIELDS[names[ni]];
    if (!meta.visible || (!meta.isArraySnapshot && meta.ringBuf.count < 2) ||
        (meta.isArraySnapshot && (!meta.arrayValues || meta.arrayValues.length < 2))) continue;
    minimapCtx.strokeStyle = meta.color;
    minimapCtx.lineWidth = 1;
    minimapCtx.beginPath();
    var ring = meta.ringBuf;
    var started = false;
    var bufMin = meta.isArraySnapshot ? Math.min.apply(null, meta.arrayValues) : (Number.isFinite(meta.ringBuf._min) ? meta.ringBuf._min : 0);
    var bufMax = meta.isArraySnapshot ? Math.max.apply(null, meta.arrayValues) : (Number.isFinite(meta.ringBuf._max) ? meta.ringBuf._max : 1);
    var yRange = bufMax - bufMin || 1;
    if (meta.isArraySnapshot) {
      for (var arrayPoint = 0; arrayPoint < meta.arrayValues.length; arrayPoint++) {
        var arrayPx = arrayPoint / Math.max(1, meta.arrayValues.length - 1) * W;
        var arrayPy = H - ((meta.arrayValues[arrayPoint] - bufMin) / yRange) * (H - 4) - 2;
        if (!started) { minimapCtx.moveTo(arrayPx, arrayPy); started = true; }
        else minimapCtx.lineTo(arrayPx, arrayPy);
      }
      minimapCtx.stroke();
      continue;
    }
    var minimapChannel = binaryChannelIndex[names[ni]];
    if (useBinaryEnvelope && minimapChannel !== undefined) {
      var minimapStart = binaryEnvelope.offsets[minimapChannel];
      var minimapEnd = binaryEnvelope.offsets[minimapChannel + 1];
      for (var minimapPoint = minimapStart; minimapPoint < minimapEnd; minimapPoint++) {
        var minimapTimeIndex = binaryEnvelope.timeIndices[minimapPoint];
        var minimapTime = binaryEnvelope.times[minimapTimeIndex] / 1000 - binaryTimeOrigin;
        var envelopePx = mtx(minimapTime);
        var envelopePy = H - ((binaryEnvelope.values[minimapPoint] - bufMin) / yRange) * (H - 4) - 2;
        if (!started) { minimapCtx.moveTo(envelopePx, envelopePy); started = true; }
        else minimapCtx.lineTo(envelopePx, envelopePy);
      }
    } else {
      // Downsample for minimap performance
      var step = Math.max(1, Math.floor(ring.count / W));
      for (var i = 0; i < ring.count; i += step) {
        var px = mtx(ring.timeAt(i));
        var py = H - ((ring.valueAt(i) - bufMin) / yRange) * (H - 4) - 2;
        if (!started) { minimapCtx.moveTo(px, py); started = true; }
        else minimapCtx.lineTo(px, py);
      }
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

  addViewerGlobalListener(window, 'mousemove', function(e) {
    if (!vpDragging) return;
    var mmRect = document.getElementById('minimap-wrap').getBoundingClientRect();
    var dx = (e.clientX - vpDragStartX) / mmRect.width;
    timelineView.offset = Math.max(0, Math.min(1, vpDragStartOffset + dx));
    captureSuperwatchTimelineLag();
    drawChart();
    drawMinimap();
    requestBinaryVisibleRangeAfterTimelineChange();
  });

  addViewerGlobalListener(window, 'mouseup', function() { vpDragging = false; });

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
    captureSuperwatchTimelineLag();
    drawChart();
    drawMinimap();
    requestBinaryVisibleRangeAfterTimelineChange();
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
  var names = sortedFieldNames();
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
        showControlError(superwatchErrorMessage(d.item.error, name));
      } else if (d.error) {
        showControlError(superwatchErrorMessage(d.error, name));
      }
    })
    .catch(function(err){
      showControlError(superwatchErrorMessage(err && err.message, name));
    });
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

addViewerGlobalListener(document, 'click', function(e) {
  var menu = document.getElementById('watch-columns-menu');
  if (!menu || !menu.classList.contains('visible')) return;
  if (menu.contains(e.target) || e.target.id === 'watch-columns-btn') return;
  menu.classList.remove('visible');
  menu.setAttribute('aria-hidden', 'true');
});

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
  var names = sortedFieldNames();
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
  var names = sortedFieldNames();
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
  requestBinaryDetail(true);
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
  requestBinaryDetail(false);
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
      if (triggerSettings.mode === 'single') requestBinaryDetail(false);
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
addViewerGlobalListener(document, 'keydown', function(e) {
  if (helpOpen || isEditableKeyboardTarget(e.target)) return;
  if (e.key === 'p' || e.key === 'P') {
    if (collectionState === 'running') {
      renderPaused = true;
      updateCollectionUI('paused');
    } else if (collectionState === 'paused') {
      renderPaused = false;
      updateCollectionUI('running');
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

  addViewerGlobalListener(document, 'keydown', function(e) {
    if (e.key === 'Escape' && helpOpen) {
      e.preventDefault();
      closeHelp();
    }
  });
})();
