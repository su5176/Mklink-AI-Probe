// ============================================================
// Memory View — Keil5-style memory window for SuperWatch
// ============================================================

var MEM_MAX_BUFFER = 8192;

var MemoryView = {
  windows: [null, null, null, null],
  activeIdx: 0,
  visible: false,

  init: function() {
    for (var i = 0; i < 4; i++) {
      this.windows[i] = new MemoryWindow(i);
      this.windows[i].initDom();
    }
    this._bindTabSwitch();
    this._bindWindowTabs();
    this._bindControls();
    this.switchWindow(0);
  },

  _bindTabSwitch: function() {
    var chartTab = document.getElementById('tab-chart');
    var memTab = document.getElementById('tab-memory');
    if (!chartTab || !memTab) return;

    var self = this;
    chartTab.addEventListener('click', function() {
      self.hide();
      chartTab.classList.add('active');
      memTab.classList.remove('active');
    });
    memTab.addEventListener('click', function() {
      self.show();
      memTab.classList.add('active');
      chartTab.classList.remove('active');
    });
  },

  _bindWindowTabs: function() {
    var self = this;
    for (var i = 0; i < 4; i++) {
      (function(idx) {
        var btn = document.getElementById('mem-win-btn-' + idx);
        if (btn) {
          btn.addEventListener('click', function() { self.switchWindow(idx); });
        }
      })(i);
    }
  },

  _bindControls: function() {
    var self = this;
    var readBtn = document.getElementById('mem-read-btn');
    if (readBtn) {
      readBtn.addEventListener('click', function() {
        var win = self.windows[self.activeIdx];
        win.readFromInputs();
        win.refresh();
      });
    }

    var autoCheck = document.getElementById('mem-auto-check');
    if (autoCheck) {
      autoCheck.addEventListener('change', function() {
        var win = self.windows[self.activeIdx];
        if (this.checked) {
          win.startAutoRefresh();
        } else {
          win.stopAutoRefresh();
        }
      });
    }

    var formatSelect = document.getElementById('mem-format-select');
    if (formatSelect) {
      formatSelect.addEventListener('change', function() {
        var win = self.windows[self.activeIdx];
        win.format = this.value;
        win.render();
      });
    }

    var symbolInput = document.getElementById('mem-symbol-input');
    if (symbolInput) {
      var debounceTimer = null;
      symbolInput.addEventListener('input', function() {
        clearTimeout(debounceTimer);
        var q = this.value.trim();
        if (q.length < 1) {
          self._hideSymbolDropdown();
          return;
        }
        debounceTimer = setTimeout(function() {
          self._searchSymbols(q);
        }, 300);
      });
      symbolInput.addEventListener('blur', function() {
        setTimeout(function() { self._hideSymbolDropdown(); }, 200);
      });
    }
  },

  show: function() {
    this.visible = true;
    var chartSection = document.getElementById('chart-watch-wrap');
    var minimapWrap = document.getElementById('minimap-wrap');
    var memMain = document.getElementById('memory-view-main');
    if (chartSection) chartSection.style.display = 'none';
    if (minimapWrap) minimapWrap.style.display = 'none';
    if (memMain) memMain.style.display = 'flex';
  },

  hide: function() {
    this.visible = false;
    var chartSection = document.getElementById('chart-watch-wrap');
    var minimapWrap = document.getElementById('minimap-wrap');
    var memMain = document.getElementById('memory-view-main');
    if (chartSection) chartSection.style.display = '';
    if (minimapWrap) minimapWrap.style.display = '';
    if (memMain) memMain.style.display = 'none';
  },

  switchWindow: function(idx) {
    var prevIdx = this.activeIdx;
    this.activeIdx = idx;
    var win = this.windows[idx];

    // Save scroll position of previous window before hiding
    if (prevIdx !== idx) {
      var prevContainer = document.getElementById('mem-grid-container-' + prevIdx);
      if (prevContainer) {
        this.windows[prevIdx].scrollTop = prevContainer.scrollTop;
      }
    }

    for (var i = 0; i < 4; i++) {
      var container = document.getElementById('mem-grid-container-' + i);
      if (container) container.style.display = (i === idx) ? '' : 'none';
      var btn = document.getElementById('mem-win-btn-' + i);
      if (btn) btn.classList.toggle('active', i === idx);
    }

    // Restore scroll position of new window after showing
    var newContainer = document.getElementById('mem-grid-container-' + idx);
    if (newContainer && win.scrollTop) {
      newContainer.scrollTop = win.scrollTop;
    }

    var addrInput = document.getElementById('mem-addr-input');
    var sizeInput = document.getElementById('mem-size-input');
    var formatSelect = document.getElementById('mem-format-select');
    var autoCheck = document.getElementById('mem-auto-check');
    var intervalInput = document.getElementById('mem-interval-input');
    if (addrInput) addrInput.value = '0x' + win.addr.toString(16).toUpperCase().padStart(8, '0');
    if (sizeInput) sizeInput.value = win.size;
    if (formatSelect) formatSelect.value = win.format;
    if (autoCheck) autoCheck.checked = win.autoRefresh;
    if (intervalInput) intervalInput.value = win.interval;
  },

  _searchSymbols: function(query) {
    var self = this;
    fetch('/api/memory/symbols?q=' + encodeURIComponent(query))
      .then(function(r) { return r.json(); })
      .then(function(data) {
        self._showSymbolDropdown(data.results || []);
      });
  },

  _showSymbolDropdown: function(results) {
    var dropdown = document.getElementById('mem-symbol-dropdown');
    if (!dropdown) return;
    dropdown.innerHTML = '';
    if (results.length === 0) {
      dropdown.style.display = 'none';
      return;
    }
    var self = this;
    results.forEach(function(item) {
      var li = document.createElement('li');
      li.textContent = item.name + ' (' + item.type + ' @ ' + item.addr + ')';
      li.addEventListener('mousedown', function(e) {
        e.preventDefault();
        var addrInput = document.getElementById('mem-addr-input');
        var sizeInput = document.getElementById('mem-size-input');
        if (addrInput) addrInput.value = item.addr;
        if (sizeInput) sizeInput.value = Math.max(item.size, 16);
        document.getElementById('mem-symbol-input').value = item.name;
        self._hideSymbolDropdown();
      });
      dropdown.appendChild(li);
    });
    dropdown.style.display = 'block';
  },

  _hideSymbolDropdown: function() {
    var dropdown = document.getElementById('mem-symbol-dropdown');
    if (dropdown) dropdown.style.display = 'none';
  }
};

// ============================================================
// MemoryWindow — single memory view instance
// ============================================================

function MemoryWindow(idx) {
  this.idx = idx;
  this.addr = 0x20000000;
  this.size = 256;
  this.originalAddr = 0x20000000;
  this.originalSize = 256;
  this.format = 'hex8';
  this.autoRefresh = false;
  this.interval = 500;
  this.data = null;
  this.prevData = null;
  this.timer = null;
  this.editing = false;
  this.scrollTop = 0;
  this._loading = false;
}

MemoryWindow.prototype._getContainer = function() {
  return document.getElementById('mem-grid-container-' + this.idx);
};

MemoryWindow.prototype._getThead = function() {
  var c = this._getContainer();
  return c ? c.querySelector('.mem-grid-thead') : null;
};

MemoryWindow.prototype._getTbody = function() {
  var c = this._getContainer();
  return c ? c.querySelector('.mem-grid-tbody') : null;
};

MemoryWindow.prototype.initDom = function() {
  var container = this._getContainer();
  if (!container) return;
  var self = this;
  container.addEventListener('scroll', function() {
    if (self._loading || self.editing) return;
    var scrollTop = container.scrollTop;
    var scrollHeight = container.scrollHeight;
    var clientHeight = container.clientHeight;
    var threshold = 40;

    if (scrollTop < threshold && self.addr > 0x20000000) {
      self._scrollUp(container);
    } else if (scrollTop + clientHeight > scrollHeight - threshold) {
      self._scrollDown(container);
    }
  });
};

MemoryWindow.prototype.readFromInputs = function() {
  var addrInput = document.getElementById('mem-addr-input');
  var sizeInput = document.getElementById('mem-size-input');
  var intervalInput = document.getElementById('mem-interval-input');
  if (addrInput) {
    var v = addrInput.value.trim();
    this.addr = parseInt(v, 16) || 0x20000000;
  }
  if (sizeInput) {
    this.size = Math.min(2048, Math.max(1, parseInt(sizeInput.value) || 256));
  }
  if (intervalInput) {
    this.interval = Math.min(10000, Math.max(100, parseInt(intervalInput.value) || 500));
  }
  this.originalAddr = this.addr;
  this.originalSize = this.size;
};

MemoryWindow.prototype.refresh = function() {
  var self = this;
  var url = '/api/memory/read?addr=0x' + this.originalAddr.toString(16).toUpperCase().padStart(8, '0') + '&size=' + this.originalSize;
  fetch(url)
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) {
        self._showError(data.error);
        return;
      }
      var raw = atob(data.data);
      var arr = new Uint8Array(raw.length);
      for (var i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
      self.prevData = self.data;

      if (self.data && self.data.length > self.originalSize) {
        var offsetInBuffer = self.originalAddr - self.addr;
        if (offsetInBuffer >= 0 && offsetInBuffer + arr.length <= self.data.length) {
          self.data.set(arr, offsetInBuffer);
        } else {
          self.data = arr;
          self.addr = self.originalAddr;
          self.size = self.originalSize;
        }
      } else {
        self.data = arr;
        self.addr = self.originalAddr;
        self.size = self.originalSize;
      }
      self.render();
    })
    .catch(function(err) {
      self._showError(err.message);
    });
};

MemoryWindow.prototype.startAutoRefresh = function() {
  if (this.timer) {
    clearInterval(this.timer);
    this.timer = null;
  }
  this.autoRefresh = true;
  this.readFromInputs();
  var self = this;
  this.timer = setInterval(function() {
    if (!self.editing) self.refresh();
  }, this.interval);
  if (MemoryView.activeIdx === this.idx) {
    var autoCheck = document.getElementById('mem-auto-check');
    if (autoCheck) autoCheck.checked = true;
  }
};

MemoryWindow.prototype.stopAutoRefresh = function() {
  if (this.timer) {
    clearInterval(this.timer);
    this.timer = null;
  }
  this.autoRefresh = false;
  if (MemoryView.activeIdx === this.idx) {
    var autoCheck = document.getElementById('mem-auto-check');
    if (autoCheck) autoCheck.checked = false;
  }
};

MemoryWindow.prototype.render = function() {
  var container = this._getContainer();
  var thead = this._getThead();
  var tbody = this._getTbody();
  if (!thead || !tbody || !container) return;

  if (!this.data) {
    thead.innerHTML = '';
    tbody.innerHTML = '<tr><td colspan="20" style="text-align:center;color:var(--muted);padding:20px;">No data. Click Read to load.</td></tr>';
    return;
  }

  thead.innerHTML = this._buildHeader();
  tbody.innerHTML = this._buildRows();
  this._bindCellEditing(tbody);

  if (MemoryView.activeIdx === this.idx) {
    var status = document.getElementById('mem-status-text');
    if (status) status.textContent = 'Read ' + this.data.length + ' bytes @ 0x' + this.addr.toString(16).toUpperCase().padStart(8, '0');
  }
};

MemoryWindow.prototype._scrollUp = function(container) {
  var self = this;
  var bytesPerRow = this._getUnitSize(this.format) * this._getColsPerRow(this.format);
  var rowsToLoad = 4;
  var loadBytes = bytesPerRow * rowsToLoad;
  var newAddr = Math.max(0x20000000, this.addr - loadBytes);
  var actualPrepend = this.addr - newAddr;
  if (actualPrepend <= 0) return;

  this._loading = true;
  var url = '/api/memory/read?addr=0x' + newAddr.toString(16).toUpperCase().padStart(8, '0') + '&size=' + actualPrepend;
  fetch(url)
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) { self._loading = false; return; }
      var raw = atob(data.data);
      var prepend = new Uint8Array(raw.length);
      for (var i = 0; i < raw.length; i++) prepend[i] = raw.charCodeAt(i);
      var merged = new Uint8Array(prepend.length + self.data.length);
      merged.set(prepend, 0);
      merged.set(self.data, prepend.length);
      self.prevData = self.data;
      self.addr = newAddr;

      if (merged.length > MEM_MAX_BUFFER) {
        merged = merged.slice(0, MEM_MAX_BUFFER);
      }

      self.data = merged;
      self.size = merged.length;
      self.render();
      if (container) {
        var totalRows = Math.ceil(merged.length / (self._getUnitSize(self.format) * self._getColsPerRow(self.format)));
        var rowH = container.scrollHeight / totalRows;
        container.scrollTop = rowH * rowsToLoad;
      }
      self._syncAddrInput();
      self._loading = false;
    })
    .catch(function() { self._loading = false; });
};

MemoryWindow.prototype._scrollDown = function(container) {
  var self = this;
  var bytesPerRow = this._getUnitSize(this.format) * this._getColsPerRow(this.format);
  var rowsToLoad = 4;
  var loadBytes = bytesPerRow * rowsToLoad;
  var endAddr = this.addr + this.data.length;

  this._loading = true;
  var url = '/api/memory/read?addr=0x' + endAddr.toString(16).toUpperCase().padStart(8, '0') + '&size=' + loadBytes;
  fetch(url)
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) { self._loading = false; return; }
      var raw = atob(data.data);
      var append = new Uint8Array(raw.length);
      for (var i = 0; i < raw.length; i++) append[i] = raw.charCodeAt(i);
      var merged = new Uint8Array(self.data.length + append.length);
      merged.set(self.data, 0);
      merged.set(append, self.data.length);
      self.prevData = self.data;

      if (merged.length > MEM_MAX_BUFFER) {
        var excess = merged.length - MEM_MAX_BUFFER;
        merged = merged.slice(excess);
        self.addr = self.addr + excess;
        self._syncAddrInput();
      }

      self.data = merged;
      self.size = merged.length;
      self.render();
      self._loading = false;
    })
    .catch(function() { self._loading = false; });
};

MemoryWindow.prototype._syncAddrInput = function() {
  var addrInput = document.getElementById('mem-addr-input');
  var sizeInput = document.getElementById('mem-size-input');
  if (addrInput && MemoryView.activeIdx === this.idx) {
    addrInput.value = '0x' + this.addr.toString(16).toUpperCase().padStart(8, '0');
  }
  if (sizeInput && MemoryView.activeIdx === this.idx) {
    sizeInput.value = this.size;
  }
};

MemoryWindow.prototype._buildHeader = function() {
  var fmt = this.format;
  var unitSize = this._getUnitSize(fmt);
  var colsPerRow = this._getColsPerRow(fmt);
  var html = '<tr><th class="mem-addr-cell">Address</th>';
  for (var c = 0; c < colsPerRow; c++) {
    var offset = c * unitSize;
    html += '<th>' + offset.toString(16).toUpperCase().padStart(2, '0') + '</th>';
  }
  if (fmt.startsWith('hex')) {
    html += '<th class="mem-ascii-cell">ASCII</th>';
  }
  html += '</tr>';
  return html;
};

MemoryWindow.prototype._buildRows = function() {
  var fmt = this.format;
  var data = this.data;
  var prev = this.prevData;
  var addr = this.addr;
  var unitSize = this._getUnitSize(fmt);
  var colsPerRow = this._getColsPerRow(fmt);
  var bytesPerRow = unitSize * colsPerRow;
  var rows = Math.ceil(data.length / bytesPerRow);
  var html = '';

  for (var r = 0; r < rows; r++) {
    var rowAddr = addr + r * bytesPerRow;
    html += '<tr class="mem-grid-row"><td class="mem-addr-cell">' +
            rowAddr.toString(16).toUpperCase().padStart(8, '0') + '</td>';

    for (var c = 0; c < colsPerRow; c++) {
      var byteOffset = r * bytesPerRow + c * unitSize;
      if (byteOffset >= data.length) {
        html += '<td class="mem-data-cell"></td>';
        continue;
      }
      var changed = this._isCellChanged(byteOffset, unitSize, prev);
      var cls = 'mem-data-cell' + (changed ? ' mem-changed' : '');
      var val = this._formatCell(data, byteOffset, fmt);
      html += '<td class="' + cls + '" data-offset="' + byteOffset + '" data-unit="' + unitSize + '">' + val + '</td>';
    }

    if (fmt.startsWith('hex')) {
      var ascii = '';
      for (var b = 0; b < bytesPerRow && (r * bytesPerRow + b) < data.length; b++) {
        var byte = data[r * bytesPerRow + b];
        ascii += (byte >= 0x20 && byte <= 0x7E) ? String.fromCharCode(byte) : '.';
      }
      html += '<td class="mem-ascii-cell">' + ascii + '</td>';
    }
    html += '</tr>';
  }
  return html;
};

MemoryWindow.prototype._getUnitSize = function(fmt) {
  switch (fmt) {
    case 'hex8': case 'dec8': return 1;
    case 'hex16': case 'dec16s': return 2;
    case 'hex32': case 'float32': return 4;
    case 'double64': return 8;
    default: return 1;
  }
};

MemoryWindow.prototype._getColsPerRow = function(fmt) {
  switch (fmt) {
    case 'hex8': case 'dec8': return 16;
    case 'hex16': case 'dec16s': return 8;
    case 'hex32': case 'float32': return 4;
    case 'double64': return 2;
    default: return 16;
  }
};

MemoryWindow.prototype._formatCell = function(data, offset, fmt) {
  if (offset >= data.length) return '';
  switch (fmt) {
    case 'hex8':
      return data[offset].toString(16).toUpperCase().padStart(2, '0');
    case 'hex16':
      if (offset + 1 >= data.length) return '--';
      var v16 = data[offset] | (data[offset + 1] << 8);
      return v16.toString(16).toUpperCase().padStart(4, '0');
    case 'hex32':
      if (offset + 3 >= data.length) return '--';
      var v32 = data[offset] | (data[offset + 1] << 8) | (data[offset + 2] << 16) | (data[offset + 3] << 24);
      return (v32 >>> 0).toString(16).toUpperCase().padStart(8, '0');
    case 'dec8':
      return data[offset].toString();
    case 'dec16s':
      if (offset + 1 >= data.length) return '--';
      var u16 = data[offset] | (data[offset + 1] << 8);
      var s16 = u16 > 32767 ? u16 - 65536 : u16;
      return s16.toString();
    case 'float32':
      if (offset + 3 >= data.length) return '--';
      var buf = new ArrayBuffer(4);
      var view = new DataView(buf);
      view.setUint8(0, data[offset]);
      view.setUint8(1, data[offset + 1]);
      view.setUint8(2, data[offset + 2]);
      view.setUint8(3, data[offset + 3]);
      var f = view.getFloat32(0, true);
      return isNaN(f) ? 'NaN' : f.toPrecision(6);
    case 'double64':
      if (offset + 7 >= data.length) return '--';
      var buf8 = new ArrayBuffer(8);
      var dv = new DataView(buf8);
      for (var i = 0; i < 8; i++) dv.setUint8(i, data[offset + i]);
      var d = dv.getFloat64(0, true);
      return isNaN(d) ? 'NaN' : d.toPrecision(10);
    default:
      return data[offset].toString(16).toUpperCase().padStart(2, '0');
  }
};

MemoryWindow.prototype._isCellChanged = function(offset, unitSize, prev) {
  if (!prev) return false;
  for (var i = 0; i < unitSize; i++) {
    if (offset + i >= this.data.length || offset + i >= prev.length) return false;
    if (this.data[offset + i] !== prev[offset + i]) return true;
  }
  return false;
};

MemoryWindow.prototype._bindCellEditing = function(tbody) {
  var self = this;
  var cells = tbody.querySelectorAll('.mem-data-cell[data-offset]');
  cells.forEach(function(cell) {
    cell.addEventListener('dblclick', function() {
      if (self.editing) return;
      self.editing = true;
      var offset = parseInt(cell.getAttribute('data-offset'));
      var unitSize = parseInt(cell.getAttribute('data-unit'));
      var oldText = cell.textContent;

      var input = document.createElement('input');
      input.type = 'text';
      input.className = 'mem-cell-edit';
      input.value = oldText;
      input.style.width = (cell.offsetWidth - 4) + 'px';
      cell.textContent = '';
      cell.appendChild(input);
      input.focus();
      input.select();

      function commit() {
        var newVal = input.value.trim();
        self.editing = false;
        cell.textContent = oldText;
        if (newVal && newVal !== oldText) {
          self._writeCell(offset, unitSize, newVal);
        }
      }

      function cancel() {
        self.editing = false;
        cell.textContent = oldText;
      }

      input.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') { e.preventDefault(); commit(); }
        if (e.key === 'Escape') { e.preventDefault(); cancel(); }
      });
      input.addEventListener('blur', cancel);
    });
  });
};

MemoryWindow.prototype._writeCell = function(offset, unitSize, valueStr) {
  var self = this;
  var writeAddr = this.addr + offset;
  var value;
  try {
    value = parseInt(valueStr, 16);
    if (isNaN(value)) return;
  } catch (e) { return; }

  fetch('/api/memory/write', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      addr: '0x' + writeAddr.toString(16).toUpperCase().padStart(8, '0'),
      value: value.toString(16).toUpperCase(),
      width: unitSize
    })
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.error) {
      self._showError(data.error);
    }
    self.refresh();
  });
};

MemoryWindow.prototype._showError = function(msg) {
  var status = document.getElementById('mem-status-text');
  if (status) {
    status.textContent = 'Error: ' + msg;
    status.style.color = 'var(--danger)';
    setTimeout(function() { status.style.color = ''; }, 3000);
  }
};

// ============================================================
// Init on DOM ready
// ============================================================
document.addEventListener('DOMContentLoaded', function() {
  if (document.getElementById('memory-view-main')) {
    MemoryView.init();
  }
});
