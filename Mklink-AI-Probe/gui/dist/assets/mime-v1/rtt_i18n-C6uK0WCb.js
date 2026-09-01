// ============================================================
// i18n: Bilingual support (zh/en), default Chinese
// ============================================================
var I18N = {
  zh: {
    // Header
    live: '在线', paused: '已暂停', stopped: '已停止', reconnecting: '重连中...',
    cursors: '光标', save: '保存', load: '加载', thresholds: '阈值',
    cursors_tip: '切换 A/B 测量光标 (C)', cursor_mode_tip: '切换测量模式 (时间/数值)',
    save_project_tip: '保存项目', load_project_tip: '加载项目',
    thresholds_tip: '配置阈值', export_csv_tip: '导出 CSV (Ctrl+E)',
    export_png_tip: '导出 PNG', help_tip: '使用说明',
    x_axis_tip: '横轴：滚轮缩放；按住鼠标左键拖动；双击恢复默认视图',
    x_axis_live_tip: '运行中可缩放和移动；视口保持与最新数据的时间差并继续前移；双击回到最新窗口',
    y_axis_tip: '纵轴：滚轮缩放；按住鼠标左键拖动；双击恢复自动范围',
    // Control toolbar
    start: '开始', pause: '暂停', resume: '恢复', stop: '停止',
    running: '运行中', buffer: '缓冲区', interval: '间隔', apply: '应用',
    buffer_memory_tip: 'Worker 历史缓存预计内存',
    // Trigger toolbar
    trigger: '触发', idle: '空闲', armed: '待触发', triggered: '已触发', done: '完成',
    source: '源', edge: '边沿', level: '电平', mode: '模式', pretrig: '预触发',
    rising: '上升沿', falling: '下降沿', both: '双边沿',
    auto: '自动', normal: '普通', single: '单次',
    force_trigger: '强制触发',
    // SuperWatch
    sw_search_placeholder: '搜索或输入变量名...', add: '添加', time: '时间', inspect: '检查',
    split_channel: '分离', merge_channel: '合并', split_panel: '独立通道',
    split_channel_tip: '将此通道移到独立纵轴面板', merge_channel_tip: '将此通道合并回主面板',
    show_channel_legend_tip: '显示通道列表', hide_channel_legend_tip: '隐藏通道列表',
    // Watch panel
    watch: '监视', columns: '列', columns_tip: '显示/隐藏列', collapse_watch: '折叠监视面板',
    // Raw Log
    raw_log: '原始日志', clear: '清除', clear_log: '清除日志', close_panel: '关闭面板',
    save_raw_log_tip: '保存带时间戳的原始数据',
    // Threshold dialog
    channel: '通道', warn_low: '警告下限', warn_high: '警告上限',
    alarm_low: '报警下限', alarm_high: '报警上限', cancel: '取消',
    // Shutdown
    server_shutdown: '服务器已关闭', server_stopped_msg: '可视化服务器已停止。',
    close_tab_msg: '可以关闭此标签页。',
    // Cursor mode
    time_mode: '时间', value_mode: '数值',
    // Help modal
    help_title: '使用说明', close_esc: '关闭 (Esc)',
    help_chart: '图表交互', help_var_selector: '变量选择器', help_trigger_sys: '触发系统',
    help_watch_panel: 'Watch 面板', help_minimap: '缩略图 (Minimap)',
    help_cursors: '测量光标 (Cursors)', help_export: '数据导出',
    help_shortcuts: '键盘快捷键', help_rawlog: 'Raw Log 面板', help_pause_resume: '暂停 / 恢复',
    help_chart_items: [
      '<strong>图表滚轮</strong> — 调整指针所在面板的 Y 轴范围；按住 Shift 滚动可缩放时间轴',
      '<strong>图表拖拽</strong> — 调整指针所在面板的 Y 轴位置；横向拖动可移动时间轴',
      '<strong>运行中横轴</strong> — 缩放或移动后保持与最新数据的固定时间差并继续前移；双击横轴回到最新窗口'
    ],
    help_var_items: [
      '<strong>单击芯片</strong> — 切换通道显示 / 隐藏',
      '<strong>双击芯片</strong> — 重置该通道为自动缩放',
      '选中的通道可在图表区域使用滚轮调整 Y 轴范围'
    ],
    help_trigger_items: [
      '<strong>Trigger</strong> — 启用 / 禁用触发功能',
      '<strong>Source</strong> — 选择触发源通道',
      '<strong>Edge</strong> — 触发边沿：Rising（上升沿）、Falling（下降沿）、Both（双边沿）',
      '<strong>Level</strong> — 设置触发电平值',
      '<strong>Mode</strong> — Auto（自动超时触发）、Normal（仅触发时捕获）、Single（单次触发后冻结）',
      '<strong>Pre-trig</strong> — 保留触发前的数据点数量',
      '<strong>Force Trigger</strong> — 手动强制触发一次'
    ],
    help_watch_items: [
      '显示每个通道的 Name / Value / Min / Max / Avg / Unit',
      '<strong>拖拽分隔条</strong> — 调整 Watch 面板宽度',
      '<strong>&times; 按钮</strong> — 折叠 Watch 面板'
    ],
    help_minimap_items: [
      '底部显示完整数据的历史概览',
      '<strong>单击</strong> — 移动到对应时间位置；运行中保持新的相对时间差继续前移',
      '<strong>拖拽视口框</strong> — 平移查看范围；运行中保持新的相对时间差继续前移'
    ],
    help_cursors_items: [
      '点击 <strong>Cursors</strong> 按钮或按 <span class="help-kbd">C</span> 键开启 A/B 测量光标',
      '光标默认放置在可视范围的 30% 和 70% 位置',
      '底部显示 Delta 时间差和各通道值差'
    ],
    help_export_items: [
      '<strong>CSV</strong> — 导出当前可见数据为 CSV 文件（<span class="help-kbd">Ctrl</span>+<span class="help-kbd">E</span>）',
      '<strong>PNG</strong> — 截取当前图表为 PNG 图片'
    ],
    help_kbd_rows: [
      ['<span class="help-kbd">Space</span>+拖动', '平移时间轴；运行中保持相对时间差'],
      ['<span class="help-kbd">P</span>', '暂停 / 恢复前端显示'],
      ['<span class="help-kbd">L</span>', '打开 / 关闭 Raw Log 面板'],
      ['<span class="help-kbd">Ctrl</span>+<span class="help-kbd">E</span>', '导出 CSV 文件'],
      ['<span class="help-kbd">C</span>', '切换 A/B 测量光标'],
      ['<span class="help-kbd">Esc</span>', '关闭此帮助窗口']
    ],
    help_rawlog_items: [
      '按 <span class="help-kbd">L</span> 键展开或折叠原始日志面板',
      '显示从设备接收的原始数据流',
      '<strong>Clear</strong> 按钮 — 清除日志内容'
    ],
    help_pause_items: [
      '按 <span class="help-kbd">P</span> 键暂停 / 恢复前端显示；后端采集不会停止',
      '暂停会冻结当前前端历史快照，可缩放、平移和导出已有数据',
      '恢复时会清空冻结快照，并重新跟随最新实时窗口',
      '暂停时，顶部状态显示为 <strong style="color:var(--warn)">paused</strong>'
    ],
    no_inspector_data: '无检查数据',
    lang_label: '中/En'
  },
  en: {
    live: 'live', paused: 'paused', stopped: 'stopped', reconnecting: 'reconnecting...',
    cursors: 'Cursors', save: 'Save', load: 'Load', thresholds: 'Thresholds',
    cursors_tip: 'Toggle A/B Cursors (C)', cursor_mode_tip: 'Switch measurement mode (Time/Value)',
    save_project_tip: 'Save Project', load_project_tip: 'Load Project',
    thresholds_tip: 'Configure Thresholds', export_csv_tip: 'Export CSV (Ctrl+E)',
    export_png_tip: 'Export PNG', help_tip: 'Help',
    x_axis_tip: 'X axis: scroll to zoom; hold the left mouse button and drag; double-click to reset',
    x_axis_live_tip: 'Zoom and pan while running; the view keeps a fixed lag from the latest data and moves forward; double-click to return live',
    y_axis_tip: 'Y axis: scroll to zoom; hold the left mouse button and drag; double-click for auto range',
    start: 'Start', pause: 'Pause', resume: 'Resume', stop: 'Stop',
    running: 'Running', buffer: 'Buffer', interval: 'Interval', apply: 'Apply',
    buffer_memory_tip: 'Estimated Worker history memory',
    trigger: 'Trigger', idle: 'Idle', armed: 'Armed', triggered: 'Triggered', done: 'Done',
    source: 'Source', edge: 'Edge', level: 'Level', mode: 'Mode', pretrig: 'Pre-trig',
    rising: 'Rising', falling: 'Falling', both: 'Both',
    auto: 'Auto', normal: 'Normal', single: 'Single',
    force_trigger: 'Force Trigger',
    sw_search_placeholder: 'Search or type variable name...', add: 'Add', time: 'Time', inspect: 'Inspect',
    split_channel: 'Split', merge_channel: 'Merge', split_panel: 'Split channel',
    split_channel_tip: 'Move this channel to an independent Y-axis panel', merge_channel_tip: 'Merge this channel into the main panel',
    show_channel_legend_tip: 'Show channel list', hide_channel_legend_tip: 'Hide channel list',
    watch: 'Watch', columns: 'Columns', columns_tip: 'Show or hide columns', collapse_watch: 'Collapse Watch',
    raw_log: 'Raw Log', clear: 'Clear', clear_log: 'Clear log', close_panel: 'Close panel',
    save_raw_log_tip: 'Save timestamped raw data',
    channel: 'Channel', warn_low: 'Warn low', warn_high: 'Warn high',
    alarm_low: 'Alarm low', alarm_high: 'Alarm high', cancel: 'Cancel',
    server_shutdown: 'Server Shut Down', server_stopped_msg: 'The visualization server has been stopped.',
    close_tab_msg: 'You can close this tab.',
    time_mode: 'Time', value_mode: 'Value',
    help_title: 'Help', close_esc: 'Close (Esc)',
    help_chart: 'Chart Interaction', help_var_selector: 'Variable Selector', help_trigger_sys: 'Trigger System',
    help_watch_panel: 'Watch Panel', help_minimap: 'Minimap',
    help_cursors: 'Measurement Cursors', help_export: 'Data Export',
    help_shortcuts: 'Keyboard Shortcuts', help_rawlog: 'Raw Log Panel', help_pause_resume: 'Pause / Resume',
    help_chart_items: [
      '<strong>Chart wheel</strong> — Adjust the Y range of the panel under the pointer; Shift+wheel zooms the timeline',
      '<strong>Chart drag</strong> — Adjust the Y position of the panel under the pointer; horizontal drag pans the timeline',
      '<strong>Running X axis</strong> — Zooming or panning keeps a fixed lag from the latest data and continues moving forward; double-click returns live'
    ],
    help_var_items: [
      '<strong>Single click chip</strong> — Toggle channel visibility',
      '<strong>Double click chip</strong> — Reset channel to auto-scale',
      'Selected channel Y-axis can be adjusted with scroll wheel in chart area'
    ],
    help_trigger_items: [
      '<strong>Trigger</strong> — Enable / disable trigger',
      '<strong>Source</strong> — Select trigger source channel',
      '<strong>Edge</strong> — Trigger edge: Rising, Falling, Both',
      '<strong>Level</strong> — Set trigger level value',
      '<strong>Mode</strong> — Auto (timeout trigger), Normal (capture only on trigger), Single (freeze after trigger)',
      '<strong>Pre-trig</strong> — Number of data points to keep before trigger',
      '<strong>Force Trigger</strong> — Manually force a trigger'
    ],
    help_watch_items: [
      'Shows Name / Value / Min / Max / Avg / Unit for each channel',
      '<strong>Drag separator</strong> — Adjust Watch panel width',
      '<strong>&times; button</strong> — Collapse Watch panel'
    ],
    help_minimap_items: [
      'Bottom bar shows full data history overview',
      '<strong>Click</strong> — Move to the corresponding time; while running, keep the new relative lag and move forward',
      '<strong>Drag viewport</strong> — Pan the visible range; while running, keep the new relative lag and move forward'
    ],
    help_cursors_items: [
      'Click <strong>Cursors</strong> button or press <span class="help-kbd">C</span> to enable A/B measurement cursors',
      'Cursors are placed at 30% and 70% of visible range by default',
      'Bottom shows Delta time difference and channel value differences'
    ],
    help_export_items: [
      '<strong>CSV</strong> — Export visible data as CSV file (<span class="help-kbd">Ctrl</span>+<span class="help-kbd">E</span>)',
      '<strong>PNG</strong> — Capture current chart as PNG image'
    ],
    help_kbd_rows: [
      ['<span class="help-kbd">Space</span>+drag', 'Pan the timeline; keep relative lag while running'],
      ['<span class="help-kbd">P</span>', 'Pause / resume frontend rendering'],
      ['<span class="help-kbd">L</span>', 'Open / close Raw Log panel'],
      ['<span class="help-kbd">Ctrl</span>+<span class="help-kbd">E</span>', 'Export CSV file'],
      ['<span class="help-kbd">C</span>', 'Toggle A/B measurement cursors'],
      ['<span class="help-kbd">Esc</span>', 'Close this help window']
    ],
    help_rawlog_items: [
      'Press <span class="help-kbd">L</span> to expand or collapse the raw log panel',
      'Shows raw data stream received from device',
      '<strong>Clear</strong> button — Clear log content'
    ],
    help_pause_items: [
      'Press <span class="help-kbd">P</span> to pause / resume frontend rendering; backend acquisition continues',
      'Pause freezes the current frontend history snapshot for zooming, panning, and export',
      'Resume clears the frozen snapshot and follows the latest live window again',
      'When paused, status shows <strong style="color:var(--warn)">paused</strong>'
    ],
    no_inspector_data: 'No inspector data',
    lang_label: '中/En'
  }
};

var currentLang = (typeof CONFIG !== "undefined" && CONFIG.lang) || "zh";


function t(key) {
  return (I18N[currentLang] && I18N[currentLang][key]) || (I18N.zh[key]) || key;
}

function applyI18n() {
  var els = document.querySelectorAll('[data-i18n]');
  for (var i = 0; i < els.length; i++) {
    var key = els[i].getAttribute('data-i18n');
    var val = t(key);
    if (val) els[i].textContent = val;
  }
  var titleEls = document.querySelectorAll('[data-i18n-title]');
  for (var i = 0; i < titleEls.length; i++) {
    var key = titleEls[i].getAttribute('data-i18n-title');
    var val = t(key);
    if (val) titleEls[i].title = val;
  }
  var phEls = document.querySelectorAll('[data-i18n-placeholder]');
  for (var i = 0; i < phEls.length; i++) {
    var key = phEls[i].getAttribute('data-i18n-placeholder');
    var val = t(key);
    if (val) phEls[i].placeholder = val;
  }
  // Populate help lists
  populateHelpContent();
  // Update lang button
  var langBtn = document.getElementById('btn-lang-toggle');
  if (langBtn) langBtn.textContent = currentLang === 'zh' ? '中/En' : 'En/中';
}

function populateHelpContent() {
  var listMap = {
    'help-chart-list': 'help_chart_items',
    'help-var-list': 'help_var_items',
    'help-trigger-list': 'help_trigger_items',
    'help-watch-list': 'help_watch_items',
    'help-minimap-list': 'help_minimap_items',
    'help-cursors-list': 'help_cursors_items',
    'help-export-list': 'help_export_items',
    'help-rawlog-list': 'help_rawlog_items',
    'help-pause-list': 'help_pause_items'
  };
  for (var id in listMap) {
    var el = document.getElementById(id);
    if (!el) continue;
    var items = I18N[currentLang][listMap[id]] || I18N.zh[listMap[id]] || [];
    el.innerHTML = items.map(function(s) { return '<li>' + s + '</li>'; }).join('');
  }
  // Keyboard shortcuts table
  var kbdTable = document.getElementById('help-kbd-table');
  if (kbdTable) {
    var rows = I18N[currentLang].help_kbd_rows || I18N.zh.help_kbd_rows || [];
    kbdTable.innerHTML = rows.map(function(r) {
      return '<tr><td>' + r[0] + '</td><td>' + r[1] + '</td></tr>';
    }).join('');
  }
}

function setLang(lang) {
  currentLang = lang;
  applyI18n();
  try { localStorage.setItem('mklink_lang', lang); } catch(e) {}
  fetch('/api/lang', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({lang: lang})
  }).catch(function(){});
}

// Language toggle button
document.addEventListener('DOMContentLoaded', function() {
  applyI18n();
  var langBtn = document.getElementById('btn-lang-toggle');
  if (langBtn) {
    langBtn.addEventListener('click', function() {
      setLang(currentLang === 'zh' ? 'en' : 'zh');
    });
  }
});
