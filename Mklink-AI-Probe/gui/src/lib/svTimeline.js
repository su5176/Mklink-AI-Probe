/**
 * SystemView 任务切换时间轴 —— 框架无关的 canvas 交互式渲染器。
 *
 * 被 HTML 报告（systemview-report，内联）与 Vue Dashboard（SystemViewTab，import）共用。
 *
 * 能力（核心三件 + 派生分析）：
 *   - 滚轮缩放（以光标为中心）
 *   - 拖拽平移
 *   - hover 提示（任务名 / 时长 / 起止时间）
 *   - 自适应时间标尺
 *   - 图例点击隐藏/显示任务泳道
 *   - 「可见窗口内重算 CPU%」
 *   - 缩放全览 / 重置
 *
 * 用法：
 *   const tl = new SvTimeline(
 *     { canvas, tooltip, legend, vcpu, resetBtn, hint },
 *     { intervals: [{tid,name,start,end}], unit: 'us'|'tk' }
 *   );
 *   tl.setData(newIntervals);   // Vue 实时更新
 *   tl.destroy();
 *
 * 区间 start/end 用同一时间单位（µs 或 ticks），与 unit 一致。
 */
export class SvTimeline {
  constructor(roots, data) {
    this.roots = roots;
    this.canvas = roots.canvas;
    this.ctx = this.canvas.getContext('2d');
    this.unit = (data && data.unit) || 'us';
    this.tickHz = Number((data && data.tickHz) || 0);
    this.PALETTE = ['#5b8cff','#21c7a8','#f5a623','#e056fd','#ff7675','#fdcb6e',
      '#00cec9','#a29bfe','#55efc4','#fab1a0','#74b9ff','#fd79a8'];
    this.nameColW = 116;
    this.rulerH = 26;
    this.laneH = 22;
    this.padR = 8;
    this.hidden = new Set();
    this.hover = null;
    this.dragging = false;
    this.dragX0 = 0;
    this.dragView0 = null;
    this.viewStart = null;
    this.viewEnd = null;
    this._hadIntervals = false;
    this._taskOrder = [];
    this._taskMeta = new Map();
    this.follow = (data && data.follow) !== false;
    this.windowSize = Number((data && data.windowSize) || 0);
    this.followEase = 0.22;
    this._followRaf = 0;
    this.setData((data && data.intervals) || []);
    this._bind();
    this._resize();
    window.addEventListener('resize', this._resize);
  }

  setData(intervals) {
    // 缓冲溢出丢包会在时间轴上留下巨大假缺口（abs_time 跳变），把真实活动压到
    // 一小撮。先剔到最密连续段再渲染。
    intervals = this._filterContinuous(intervals || []);
    // 按任务汇总，确定泳道顺序（总运行时间降序，最多 12 条）
    const hadIntervalsBefore = this._hadIntervals;
    this._hadIntervals = intervals.length > 0;
    this.intervals = intervals;
    const run = new Map(), names = new Map();
    for (const it of this.intervals) {
      run.set(it.tid, (run.get(it.tid) || 0) + (it.end - it.start));
      names.set(it.tid, it.name);
    }
    this.tasks = this._mergeTasks(run, names);
    this.taskOf = new Map(this.tasks.map(t => [t.tid, t]));
    // 时间范围
    if (this.intervals.length) {
      this.tMin = Math.min(...this.intervals.map(i => i.start));
      this.tMax = Math.max(...this.intervals.map(i => i.end));
    } else { this.tMin = 0; this.tMax = 1; }
    if (this.tMax <= this.tMin) this.tMax = this.tMin + 1;
    const viewInvalid = this.viewStart == null || this.viewEnd == null || this.viewEnd <= this.viewStart;
    const shouldFollow = this.follow && this.windowSize > 0 && this.intervals.length;
    if (shouldFollow && (viewInvalid || !hadIntervalsBefore)) {
      this._snapFollowRange();
    } else if (shouldFollow) {
      const span = this.windowSize;
      if (Math.abs((this.viewEnd - this.viewStart) - span) > 0.001) {
        this.viewStart = this.viewEnd - span;
      }
    } else {
      const viewOutsideData = this.viewEnd < this.tMin || this.viewStart > this.tMax;
      if (viewInvalid || viewOutsideData || (!hadIntervalsBefore && this.intervals.length)) {
      this.viewStart = this.tMin;
      this.viewEnd = this.tMax;
      } else {
      this.viewStart = Math.max(this.tMin, this.viewStart);
      this.viewEnd = Math.min(this.tMax, this.viewEnd);
      if (this.viewEnd <= this.viewStart) {
        this.viewStart = this.tMin;
        this.viewEnd = this.tMax;
      }
      }
    }
    this._layout();
    this._draw();
    this._updateStatus();
    if (shouldFollow && !viewInvalid && hadIntervalsBefore) this._scheduleFollow();
  }

  _mergeTasks(run, names) {
    if (!this._taskOrder) this._taskOrder = [];
    if (!this._taskMeta) this._taskMeta = new Map();

    const ranked = [...run.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, 12)
      .map(([tid]) => tid);
    const active = new Set(ranked);

    for (const tid of ranked) {
      const name = names.get(tid) || ('0x' + (tid >>> 0).toString(16).toUpperCase());
      if (!this._taskMeta.has(tid)) {
        this._taskMeta.set(tid, {
          tid,
          name,
          color: this.PALETTE[this._taskMeta.size % this.PALETTE.length],
        });
      } else if (name) {
        this._taskMeta.get(tid).name = name;
      }
      if (!this._taskOrder.includes(tid)) this._taskOrder.push(tid);
    }

    this._taskOrder = this._taskOrder.filter(tid => active.has(tid));
    for (const tid of ranked) {
      if (!this._taskOrder.includes(tid)) this._taskOrder.push(tid);
    }

    return this._taskOrder
      .map(tid => this._taskMeta.get(tid))
      .filter(Boolean)
      .slice(0, 12);
  }

  setWindowSize(windowSize) {
    this.windowSize = Math.max(0, Number(windowSize) || 0);
    if (this.follow && this.windowSize > 0) this._snapFollowRange();
  }

  setFollowMode(enabled) {
    this.follow = !!enabled;
    if (this.follow && this.windowSize > 0) this._snapFollowRange();
  }

  _targetFollowRange() {
    if (!this.windowSize || this.windowSize <= 0) {
      return { start: this.tMin, end: this.tMax };
    }
    const end = this.tMax;
    return { start: end - this.windowSize, end };
  }

  _snapFollowRange() {
    const target = this._targetFollowRange();
    this.viewStart = target.start;
    this.viewEnd = target.end;
    if (this.W && this.H) {
      this._draw();
      this._updateStatus();
    }
  }

  _scheduleFollow() {
    if (!this.follow || this.windowSize <= 0 || this._followRaf) return;
    const step = () => {
      this._followRaf = 0;
      if (!this.follow || this.windowSize <= 0) return;
      const target = this._targetFollowRange();
      const currentEnd = Number.isFinite(this.viewEnd) ? this.viewEnd : target.end;
      const delta = target.end - currentEnd;
      if (Math.abs(delta) < 0.5) {
        this.viewEnd = target.end;
        this.viewStart = target.start;
      } else {
        this.viewEnd = currentEnd + delta * this.followEase;
        this.viewStart = this.viewEnd - this.windowSize;
      }
      this._draw();
      this._updateStatus();
      if (Math.abs(target.end - this.viewEnd) >= 0.5) {
        this._followRaf = requestAnimationFrame(step);
      }
    };
    this._followRaf = requestAnimationFrame(step);
  }

  _layout() {
    this.lanes = this.tasks.filter(t => !this.hidden.has(t.tid));
    const dpr = window.devicePixelRatio || 1;
    const cssW = this.canvas.clientWidth || 800;
    const cssH = this.rulerH + this.lanes.length * this.laneH + 4;
    this.canvas.width = cssW * dpr; this.canvas.height = cssH * dpr;
    this.canvas.style.height = cssH + 'px';
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.W = cssW; this.H = cssH;
    this.plotX0 = this.nameColW;
    this.plotX1 = this.W - this.padR;
    this.plotW = this.plotX1 - this.plotX0;
  }

  _resize = () => { this._layout(); this._draw(); }

  _filterContinuous(intervals) {
    if (intervals.length < 8) return intervals;
    let maxGap = 0, maxIdx = 0;
    let tMin = Infinity, tMax = -Infinity;
    for (let i = 0; i < intervals.length - 1; i++) {
      const g = intervals[i + 1].start - intervals[i].end;
      if (g > maxGap) { maxGap = g; maxIdx = i; }
      if (intervals[i].start < tMin) tMin = intervals[i].start;
      if (intervals[i].end > tMax) tMax = intervals[i].end;
    }
    const last = intervals[intervals.length - 1];
    if (last.start < tMin) tMin = last.start;
    if (last.end > tMax) tMax = last.end;
    const durs = intervals.map(it => it.end - it.start).sort((a, b) => a - b);
    const med = durs[Math.floor(durs.length / 2)] || 1;
    if (maxGap <= this._largeGapThreshold(Math.max(0, tMax - tMin), med)) return intervals;
    if (maxGap <= med * 200) return intervals; // 无离群缺口
    const left = intervals.slice(0, maxIdx + 1), right = intervals.slice(maxIdx + 1);
    return this._filterContinuous(left.length >= right.length ? left : right);
  }

  _largeGapThreshold(span, medianDuration) {
    const oneSecond = this.unit === 'us'
      ? 1_000_000
      : (this.tickHz > 0 ? this.tickHz : 1_000_000);
    const windowThreshold = this.windowSize > 0 ? this.windowSize * 2 : span * 0.2;
    return Math.max(medianDuration * 200, oneSecond * 2, windowThreshold);
  }

  _t2x(t) { return this.plotX0 + (t - this.viewStart) / (this.viewEnd - this.viewStart) * this.plotW; }
  _x2t(x) { return this.viewStart + (x - this.plotX0) / this.plotW * (this.viewEnd - this.viewStart); }

  _fmtLegacy(t) {
    if (this.unit === 'us') {
      if (t >= 1e6) return (t / 1e6).toFixed(3) + ' s';
      if (t >= 1e3) return (t / 1e3).toFixed(2) + ' ms';
      return t.toFixed(0) + ' µs';
    }
    return Math.round(t).toLocaleString() + ' tk';
  }

  _fmtTime(us) {
    const abs = Math.abs(us);
    if (abs >= 1e6) return (us / 1e6).toFixed(3) + ' s';
    if (abs >= 1e3) return (us / 1e3).toFixed(2) + ' ms';
    return us.toFixed(0) + ' us';
  }

  _ticksFromUs(us) {
    return this.tickHz > 0 ? us * this.tickHz / 1_000_000 : null;
  }

  _fmtTicks(ticks, compact = false) {
    if (!Number.isFinite(ticks)) return '';
    const rounded = Math.round(ticks);
    const abs = Math.abs(rounded);
    if (compact && abs >= 1_000_000) return (rounded / 1_000_000).toFixed(2).replace(/\.?0+$/, '') + 'M tk';
    if (compact && abs >= 1_000) return (rounded / 1_000).toFixed(1).replace(/\.?0+$/, '') + 'k tk';
    return rounded.toLocaleString() + ' tk';
  }

  _fmt(t, withTicks = false) {
    if (this.unit === 'us') {
      const time = this._fmtTime(t);
      if (withTicks && this.tickHz > 0) return time + ' / ' + this._fmtTicks(this._ticksFromUs(t), true);
      return time;
    }
    return this._fmtTicks(t);
  }

  _fmtPoint(time, ticks) {
    if (this.unit !== 'us') return this._fmt(time);
    const tickValue = Number.isFinite(ticks) ? ticks : this._ticksFromUs(time);
    const tickText = this._fmtTicks(tickValue);
    return tickText ? `${this._fmtTime(time)} (${tickText})` : this._fmtTime(time);
  }

  _fmtDuration(it) {
    const duration = it.end - it.start;
    if (this.unit !== 'us') return this._fmt(duration);
    const hasExactTicks = Number.isFinite(it.startTk) && Number.isFinite(it.endTk);
    const durationTicks = hasExactTicks ? it.endTk - it.startTk : this._ticksFromUs(duration);
    const tickText = this._fmtTicks(durationTicks);
    return tickText ? `${this._fmtTime(duration)} (${tickText})` : this._fmtTime(duration);
  }

  _niceStep(rawStep) {
    if (!Number.isFinite(rawStep) || rawStep <= 0) return 1;
    const magnitude = Math.pow(10, Math.floor(Math.log10(rawStep)));
    const normalized = rawStep / magnitude;
    const multiplier = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
    return multiplier * magnitude;
  }

  _labelWidth(text) {
    if (this.ctx && typeof this.ctx.measureText === 'function') {
      return this.ctx.measureText(text).width;
    }
    return String(text).length * 7;
  }

  _draw() {
    const ctx = this.ctx;
    ctx.clearRect(0, 0, this.W, this.H);
    ctx.font = '11px -apple-system,Segoe UI,Roboto,sans-serif';
    // 时间标尺
    this._drawRuler();
    // 泳道
    this.lanes.forEach((task, i) => {
      const y = this.rulerH + i * this.laneH;
      // 背景交替
      ctx.fillStyle = '#fffdf8';
      ctx.fillRect(0, y, this.nameColW, this.laneH);
      ctx.fillStyle = '#d4cabc';
      ctx.fillRect(this.nameColW - 1, y, 1, this.laneH);
      ctx.fillStyle = i % 2 ? '#f6f4ed' : '#fbfaf5';
      ctx.fillRect(this.plotX0, y, this.plotW, this.laneH);
      // 任务名
      ctx.fillStyle = '#605a50'; ctx.textBaseline = 'middle'; ctx.textAlign = 'right';
      ctx.fillText(task.name.slice(0, 14), this.nameColW - 18, y + this.laneH / 2);
      // 颜色点
      ctx.fillStyle = task.color;
      ctx.fillRect(this.nameColW - 11, y + this.laneH / 2 - 3, 6, 6);
    });
    // 区间
    ctx.textAlign = 'left';
    for (const it of this.intervals) {
      const task = this.taskOf.get(it.tid);
      if (!task || this.hidden.has(it.tid)) continue;
      const laneIdx = this.lanes.indexOf(task);
      if (laneIdx < 0) continue;
      if (it.end < this.viewStart || it.start > this.viewEnd) continue;
      const x0 = Math.max(this._t2x(it.start), this.plotX0);
      const x1 = Math.min(this._t2x(it.end), this.plotX1);
      if (x1 - x0 < 0.4) continue; // 太细跳过
      const y = this.rulerH + laneIdx * this.laneH + 4;
      const w = Math.max(x1 - x0, 0.8);
      const h = this.laneH - 8;
      ctx.fillStyle = task.color;
      ctx.fillRect(x0, y, w, h);
      ctx.strokeStyle = 'rgba(31, 41, 55, 0.22)';
      ctx.lineWidth = 1;
      ctx.strokeRect(x0 + 0.5, y + 0.5, Math.max(w - 1, 0.8), Math.max(h - 1, 1));
    }
    // hover 高亮
    if (this.hover) {
      const task = this.taskOf.get(this.hover.tid);
      const laneIdx = this.lanes.indexOf(task);
      if (laneIdx >= 0) {
        const x0 = Math.max(this._t2x(this.hover.start), this.plotX0);
        const x1 = Math.min(this._t2x(this.hover.end), this.plotX1);
        const y = this.rulerH + laneIdx * this.laneH + 2;
        ctx.strokeStyle = '#111827'; ctx.lineWidth = 1.25;
        ctx.strokeRect(x0 - 0.5, y - 0.5, Math.max(x1 - x0, 1.5) + 1, this.laneH - 4);
      }
    }
  }

  _drawRuler() {
    const ctx = this.ctx;
    ctx.fillStyle = '#f4f1e8'; ctx.fillRect(0, 0, this.W, this.rulerH);
    ctx.fillStyle = '#ddd8ca'; ctx.fillRect(0, this.rulerH - 1, this.W, 1);
    const span = this.viewEnd - this.viewStart;
    const targetLabels = Math.max(2, Math.floor(this.plotW / 150));
    const step = this._niceStep(span / targetLabels);
    ctx.fillStyle = '#746d61'; ctx.textBaseline = 'middle'; ctx.textAlign = 'center'; ctx.font = '10px monospace';
    const t0 = Math.ceil(this.viewStart / step) * step;
    let lastLabelRight = -Infinity;
    for (let t = t0; t <= this.viewEnd; t += step) {
      const x = this._t2x(t);
      if (x < this.plotX0 || x > this.plotX1) continue;
      ctx.strokeStyle = '#e4ded1'; ctx.beginPath(); ctx.moveTo(x, 4); ctx.lineTo(x, this.rulerH - 2); ctx.stroke();
      const label = this._fmt(t, true);
      const half = this._labelWidth(label) / 2;
      const labelX = Math.min(Math.max(x, this.plotX0 + half + 2), this.plotX1 - half - 2);
      const labelLeft = labelX - half;
      const labelRight = labelX + half;
      if (labelLeft <= lastLabelRight + 10) continue;
      ctx.fillText(label, labelX, this.rulerH / 2);
      lastLabelRight = labelRight;
    }
  }

  _hitTest(mx, my) {
    if (mx < this.plotX0 || mx > this.plotX1) return null;
    const laneIdx = Math.floor((my - this.rulerH) / this.laneH);
    if (laneIdx < 0 || laneIdx >= this.lanes.length) return null;
    const t = this._x2t(mx);
    // 该泳道里找命中的区间（取最后一个，即最上层）
    let hit = null;
    for (const it of this.intervals) {
      if (it.tid !== this.lanes[laneIdx].tid) continue;
      if (it.start <= t && it.end >= t) hit = it;
    }
    return hit;
  }

  _bind() {
    if (this._listenersBound) return;

    this._onWheel = (e) => {
      if (!this._shouldZoomWheel(e)) return;
      e.preventDefault();
      this.setFollowMode(false);
      const rect = this.canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const t = this._x2t(mx);
      const factor = e.deltaY < 0 ? 0.8 : 1.25; // 缩放因子
      let ns = t - (t - this.viewStart) * factor;
      let ne = t + (this.viewEnd - t) * factor;
      // 限制最小窗口
      if (ne - ns < (this.tMax - this.tMin) * 1e-5) { ns = t - (ne - ns) / 2; ne = t + (ne - ns) / 2; }
      this.viewStart = Math.max(this.tMin, ns);
      this.viewEnd = Math.min(this.tMax, ne);
      this._draw(); this._updateStatus();
    };

    this._onMouseDown = (e) => {
      this.setFollowMode(false);
      this.dragging = true; this.dragX0 = e.clientX; this.dragView0 = [this.viewStart, this.viewEnd];
      this.canvas.style.cursor = 'grabbing';
    };

    this._onMouseMove = (e) => {
      const rect = this.canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left, my = e.clientY - rect.top;
      if (this.dragging) {
        const dx = e.clientX - this.dragX0;
        const dt = -dx / this.plotW * (this.dragView0[1] - this.dragView0[0]);
        let ns = this.dragView0[0] + dt, ne = this.dragView0[1] + dt;
        if (ns < this.tMin) { ne += this.tMin - ns; ns = this.tMin; }
        if (ne > this.tMax) { ns -= ne - this.tMax; ne = this.tMax; }
        this.viewStart = ns; this.viewEnd = ne;
        this._draw(); this._updateStatus();
      } else if (mx >= 0 && mx <= this.W && my >= 0 && my <= this.H) {
        const hit = this._hitTest(mx, my);
        this.hover = hit;
        this.canvas.style.cursor = hit ? 'pointer' : 'crosshair';
        if (hit) this._showTip(e.clientX, e.clientY, hit); else this._hideTip();
        this._draw();
      }
    };

    this._onMouseUp = () => { this.dragging = false; this.canvas.style.cursor = 'crosshair'; };
    this._onMouseLeave = () => { this.hover = null; this._hideTip(); this._draw(); };

    this.canvas.addEventListener('wheel', this._onWheel, { passive: false });
    this.canvas.addEventListener('mousedown', this._onMouseDown);
    window.addEventListener('mousemove', this._onMouseMove);
    window.addEventListener('mouseup', this._onMouseUp);
    this.canvas.addEventListener('mouseleave', this._onMouseLeave);
    if (this.roots.resetBtn) this.roots.resetBtn.onclick = () => this.reset();
    this._listenersBound = true;
  }

  _shouldZoomWheel(e) {
    return !!(e.ctrlKey || e.shiftKey);
  }

  _escapeHtml(value) {
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  _showTip(cx, cy, it) {
    const tip = this.roots.tooltip; if (!tip) return;
    tip.style.display = 'block';
    tip.innerHTML = `<b>${this._escapeHtml(it.name)}</b><br>duration ${this._fmtDuration(it)}<br>`
      + `start ${this._fmtPoint(it.start, it.startTk)}<br>end ${this._fmtPoint(it.end, it.endTk)}`;
    const w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = (cx + 14) + 'px'; tip.style.top = (cy + 14) + 'px';
  }
  _hideTip() { if (this.roots.tooltip) this.roots.tooltip.style.display = 'none'; }

  _updateStatus() {
    // 可见窗口内每任务 CPU% = 任务在窗口内的运行 / 窗口内所有任务运行
    const vis = this.intervals.filter(it =>
      it.end > this.viewStart && it.start < this.viewEnd && !this.hidden.has(it.tid));
    const run = new Map();
    for (const it of vis) {
      const s = Math.max(it.start, this.viewStart), e = Math.min(it.end, this.viewEnd);
      run.set(it.tid, (run.get(it.tid) || 0) + (e - s));
    }
    const total = [...run.values()].reduce((a, b) => a + b, 0) || 1;
    const items = this.tasks.map(t => ({ ...t, pct: ((run.get(t.tid) || 0) / total * 100) }))
      .filter(t => t.pct > 0.001);
    // 图例
    if (this.roots.legend) {
      this.roots.legend.innerHTML = items.map(t =>
        `<span class="sv-lg${this.hidden.has(t.tid) ? ' sv-lg-off' : ''}" data-tid="${t.tid}">`
        + `<i style="background:${t.color}"></i>${t.name.slice(0,12)} <em>${t.pct.toFixed(1)}%</em></span>`
      ).join('');
      this.roots.legend.querySelectorAll('.sv-lg').forEach(el => {
        el.onclick = () => { const tid = +el.dataset.tid; this.toggleTask(tid); };
      });
    }
    // 可见 CPU 面板
    if (this.roots.vcpu) {
      this.roots.vcpu.innerHTML = items.map(t =>
        `<div class="sv-vcpu-row"><span class="sv-vcpu-n">${t.name.slice(0,14)}</span>`
        + `<div class="sv-vcpu-bg"><div class="sv-vcpu-bar" style="width:${Math.max(0.3,t.pct)}%;background:${t.color}"></div></div>`
        + `<span class="sv-vcpu-p">${t.pct.toFixed(2)}%</span></div>`
      ).join('') || '<div class="sv-empty">窗口内无任务</div>';
    }
  }

  toggleTask(tid) {
    if (this.hidden.has(tid)) this.hidden.delete(tid); else this.hidden.add(tid);
    this._layout(); this._draw(); this._updateStatus();
  }

  reset() {
    if (this.windowSize > 0) {
      this.setFollowMode(true);
      return;
    }
    this.viewStart = this.tMin; this.viewEnd = this.tMax; this._draw(); this._updateStatus();
  }

  destroy() {
    if (this._followRaf) {
      cancelAnimationFrame(this._followRaf);
      this._followRaf = 0;
    }
    window.removeEventListener('resize', this._resize);
    if (this._listenersBound) {
      this.canvas.removeEventListener('wheel', this._onWheel);
      this.canvas.removeEventListener('mousedown', this._onMouseDown);
      window.removeEventListener('mousemove', this._onMouseMove);
      window.removeEventListener('mouseup', this._onMouseUp);
      this.canvas.removeEventListener('mouseleave', this._onMouseLeave);
      if (this.roots.resetBtn) this.roots.resetBtn.onclick = null;
      this._listenersBound = false;
    }
    this._hideTip();
    // （其它监听挂在 window/canvas，组件卸载时随 DOM 释放；简单场景可接受）
  }
}

// 供 HTML 报告（非 module）使用：挂到 window
if (typeof window !== 'undefined') window.SvTimeline = SvTimeline;
