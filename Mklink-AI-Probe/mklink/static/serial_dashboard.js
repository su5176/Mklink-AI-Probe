// ============================================================
// Serial Dashboard — i18n + VirtualLog (reusable core)
// ============================================================
var SERIAL_I18N = {
  zh: {
    title: '串口调试', ascii: 'ASCII', hex: 'HEX',
    live: '在线', disconnected: '已断开', connecting: '连接中...',
    send: '发送', send_placeholder: '输入要发送的数据...',
    send_as_hex: '以 HEX 发送', send_all: '广播',
    send_file: '发送文件', file_path_placeholder: '文件路径...',
    cmd_queue: '命令队列', add: '添加', add_cmd_placeholder: '添加命令...',
    auto_interval: '自动发送间隔', start: '开始', stop: '停止',
    filter: '过滤', filter_placeholder: '正则过滤...', apply: '应用', clear: '清除',
    auto_reply: '自动应答规则', no_rules: '未加载规则',
    add_rule: '添加规则', match_placeholder: '匹配 (HEX/正则/包含)',
    reply_placeholder: '应答 (HEX)', remove: '删除',
    logger: '文件日志', log_start: '开始记录', log_stop: '停止记录',
    log_format: '格式', log_path_placeholder: '日志路径...',
    log_active: '● 记录中', log_inactive: '未记录',
    clear_log: '清除日志',
    profile: '协议', version: '版本',
    frame_header: '帧头', frame_tail: '帧尾', crc: 'CRC',
    port_info: '端口信息', baud: '波特率', databits: '数据位',
    stopbits: '停止位', parity: '校验',
    stats: '统计', rx_bytes: 'RX 字节', tx_bytes: 'TX 字节', bps: '速率',
    crc_ok: 'CRC✓', crc_fail: 'CRC✗', lang_label: '中/En'
  },
  en: {
    title: 'Serial Debug', ascii: 'ASCII', hex: 'HEX',
    live: 'Live', disconnected: 'Disconnected', connecting: 'Connecting...',
    send: 'Send', send_placeholder: 'Type data to send...',
    send_as_hex: 'Send as HEX', send_all: 'Broadcast',
    send_file: 'Send File', file_path_placeholder: 'File path...',
    cmd_queue: 'Command Queue', add: 'Add', add_cmd_placeholder: 'Add command...',
    auto_interval: 'Auto-send interval', start: 'Start', stop: 'Stop',
    filter: 'Filter', filter_placeholder: 'Regex filter...', apply: 'Apply', clear: 'Clear',
    auto_reply: 'Auto-Reply Rules', no_rules: 'No rules loaded',
    add_rule: 'Add Rule', match_placeholder: 'Match (HEX/regex/contains)',
    reply_placeholder: 'Reply (HEX)', remove: 'Remove',
    logger: 'File Logger', log_start: 'Start', log_stop: 'Stop',
    log_format: 'Format', log_path_placeholder: 'Log path...',
    log_active: '● Recording', log_inactive: 'Inactive',
    clear_log: 'Clear Log',
    profile: 'Profile', version: 'Version',
    frame_header: 'Header', frame_tail: 'Tail', crc: 'CRC',
    port_info: 'Port Info', baud: 'Baud', databits: 'Data bits',
    stopbits: 'Stop bits', parity: 'Parity',
    stats: 'Stats', rx_bytes: 'RX Bytes', tx_bytes: 'TX Bytes', bps: 'Rate',
    crc_ok: 'CRC✓', crc_fail: 'CRC✗', lang_label: 'En/中'
  }
};
var serialLang = 'zh';
function st(key) { return (SERIAL_I18N[serialLang]||{})[key] || SERIAL_I18N.zh[key] || key; }
function applySerialI18n() {
  document.querySelectorAll('[data-i18n]').forEach(function(el) { el.textContent = st(el.getAttribute('data-i18n')); });
  document.querySelectorAll('[data-i18n-placeholder]').forEach(function(el) { el.placeholder = st(el.getAttribute('data-i18n-placeholder')); });
  var lb = document.getElementById('btn-lang-toggle'); if (lb) lb.textContent = st('lang_label');
}
function toggleSerialLang() { serialLang = serialLang === 'zh' ? 'en' : 'zh'; applySerialI18n(); }

// ─── Highlight rules (matching CLI _DEFAULT_HIGHLIGHTS) ───
var HL_RULES = [{p:/(?:ERROR|FAIL)/i,c:'hl-error'},{p:/WARN/i,c:'hl-warn'},{p:/\bOK\b|PASS/i,c:'hl-ok'}];
function getHL(t){for(var i=0;i<HL_RULES.length;i++)if(HL_RULES[i].p.test(t))return HL_RULES[i].c;return '';}

// ─── Virtual Scroll Log ───
function VirtualLog(el,opts){
  this.el=el;this.lh=opts.lineHeight||22;this.max=opts.maxLines||5000;
  this.renderFn=opts.renderFn;this.filterFn=null;
  this.all=[];this.vis=[];this.auto=true;this.raf=false;
  this._range={s:-1,e:-1};this._dirty=false;
  this.vp=document.createElement('div');this.vp.className='log-viewport';
  el.appendChild(this.vp);el.addEventListener('scroll',this._onScroll.bind(this));
}
VirtualLog.prototype.push=function(item){
  if(this.all.length>=this.max){this.all.shift();this._dirty=true;}
  this.all.push(item);
  if(!this.filterFn||this.filterFn(item))this.vis.push(this.all.length-1);
  this._sched();
};
VirtualLog.prototype.setFilter=function(fn){this.filterFn=fn;this._rebuild();this._sched();};
VirtualLog.prototype.clear=function(){this.all=[];this.vis=[];this.vp.style.height='0';this.vp.innerHTML='';this._range={s:-1,e:-1};};
VirtualLog.prototype.rerender=function(){this._rebuild();this._range={s:-1,e:-1};this._sched();};
VirtualLog.prototype._rebuild=function(){
  this.vis=[];for(var i=0;i<this.all.length;i++)if(!this.filterFn||this.filterFn(this.all[i]))this.vis.push(i);
  this._dirty=false;
};
VirtualLog.prototype._onScroll=function(){this.auto=this.el.scrollHeight-this.el.scrollTop-this.el.clientHeight<40;this._sched();};
VirtualLog.prototype._sched=function(){if(this.raf)return;this.raf=true;requestAnimationFrame(this._render.bind(this));};
VirtualLog.prototype._render=function(){
  this.raf=false;if(this._dirty)this._rebuild();
  var n=this.vis.length,h=n*this.lh;this.vp.style.height=h+'px';
  var st=this.el.scrollTop,vh=this.el.clientHeight;
  if(this.auto&&h>vh){this.el.scrollTop=h-vh;st=this.el.scrollTop;}
  var os=5,s=Math.max(0,Math.floor(st/this.lh)-os),e=Math.min(n,Math.ceil((st+vh)/this.lh)+os);
  if(s===this._range.s&&e===this._range.e)return;this._range={s:s,e:e};
  var f=document.createDocumentFragment();
  var t=document.createElement('div');t.style.height=(s*this.lh)+'px';f.appendChild(t);
  for(var i=s;i<e;i++){var el=this.renderFn(this.all[this.vis[i]]);if(el)f.appendChild(el);}
  var b=document.createElement('div');b.style.height=((n-e)*this.lh)+'px';f.appendChild(b);
  this.vp.innerHTML='';this.vp.appendChild(f);
};

// ─── Utility ───
function escapeHtml(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function fmtBytes(n){if(n<1024)return n+' B';if(n<1048576)return(n/1024).toFixed(1)+' KB';return(n/1048576).toFixed(1)+' MB';}
