import{A as e,C as t,D as n,F as r,M as i,N as a,P as o,S as s,T as c,_ as l,a as u,b as d,c as f,d as p,f as m,g as h,h as g,i as _,j as v,k as y,l as b,m as x,n as S,o as C,p as w,s as T,t as E,u as D,v as O,w as k,x as A,y as j}from"./index-DWsf3TdM.js";var M=`http://127.0.0.1:8765`,N=i({});function P(){async function e(){try{let e=await fetch(`${M}/api/session/status`);e.ok&&(N.value=await e.json())}catch{}}async function t(e){try{let t=await fetch(`${M}/api/dash/conflict-check?type=${e}`);if(t.ok)return(await t.json()).conflicts||[]}catch{}return[]}function n(){let e=N.value.mklink_bridge;return e?e.owner:null}return{status:v(N),refresh:e,checkConflict:t,getBridgeOwner:n}}var F=`http://127.0.0.1:8765`;async function I(e,t){let n=await fetch(`${F}${e}`,{...t,headers:{"Content-Type":`application/json`,...t?.headers}});if(!n.ok){let e=await n.json().catch(()=>null);throw Error(e?.detail||n.statusText)}return n.json()}function L(e){let t=i(`idle`),n=i(null);async function r(r){n.value=null;try{let n=r||{};await I(`/api/dash/${e}/start`,{method:`POST`,body:JSON.stringify(n)}),t.value=`running`}catch(e){n.value=e instanceof Error?e.message:String(e),t.value=`error`}}async function a(){try{await I(`/api/dash/${e}/stop`,{method:`POST`}),t.value=`idle`}catch(e){n.value=e instanceof Error?e.message:String(e)}}async function o(){try{await I(`/api/dash/${e}/pause`,{method:`POST`}),t.value=`paused`}catch(e){n.value=e instanceof Error?e.message:String(e)}}async function s(){try{await I(`/api/dash/${e}/resume`,{method:`POST`}),t.value=`running`}catch(e){n.value=e instanceof Error?e.message:String(e)}}async function c(){return I(`/api/dash/${e}/status`)}async function l(){return I(`/api/dash/${e}/history`)}return{state:v(t),error:v(n),start:r,stop:a,pause:o,resume:s,getStatus:c,getHistory:l}}function R(){async function e(e,t){return I(`/api/device/read-memory`,{method:`POST`,body:JSON.stringify({address:e,size:t})})}async function t(e,t){return I(`/api/device/write-memory`,{method:`POST`,body:JSON.stringify({address:e,data_hex:t})})}async function n(e){return I(`/api/device/read-variable`,{method:`POST`,body:JSON.stringify({name:e})})}async function r(e,t){return I(`/api/device/write-variable`,{method:`POST`,body:JSON.stringify({name:e,value:t})})}async function i(e){return I(`/api/device/read-register`,{method:`POST`,body:JSON.stringify({name:e})})}async function a(){return I(`/api/device/core-registers`)}async function o(){return I(`/api/device/hardfault-detail`)}async function s(){return I(`/api/device/memory-map`)}return{readMemory:e,writeMemory:t,readVariable:n,writeVariable:r,readRegister:i,getCoreRegisters:a,getHardfaultDetail:o,getMemoryMap:s}}function z(){async function e(e){return I(`/api/symbols/search?q=${encodeURIComponent(e)}`)}async function t(e){return I(`/api/symbols/typeinfo?name=${encodeURIComponent(e)}`)}return{search:e,typeinfo:t}}var B=`http://127.0.0.1:8765`;function V(e){let n=i([]),r=i(!1),a=i(null),o=null;function s(){c(),n.value=[],a.value=null,o=new EventSource(`${B}${e}`),o.onopen=()=>{r.value=!0,a.value=null},o.onmessage=e=>{try{let t=JSON.parse(e.data);t.event===`data`||t.event===`raw`?(n.value.push(t),n.value.length>500&&(n.value=n.value.slice(-500))):t.event===`history`?n.value=(t.points||[]).slice(-500):t.event===`error`?a.value=t.message:t.event===`stopped`&&(r.value=!1)}catch{}},o.onerror=()=>{r.value=!1,o?.readyState===EventSource.CLOSED&&(a.value=`Connection closed`)}}function c(){o&&=(o.close(),null),r.value=!1}return t(()=>{c()}),{data:v(n),connected:v(r),error:v(a),connect:s,disconnect:c}}var H={class:`control-toolbar`},U=[`disabled`],ee={key:3,class:`status-dot running`},te={key:4,class:`status-dot paused`},ne={key:5,class:`error-text`},re={key:6,class:`point-count`},ie=S(d({__name:`ControlToolbar`,props:{state:{},error:{},deviceConnected:{type:Boolean},pointCount:{}},emits:[`start`,`pause`,`resume`,`stop`],setup(e){return(t,n)=>(k(),l(`div`,H,[e.state===`idle`?(k(),l(`button`,{key:0,class:`btn btn-primary`,onClick:n[0]||=e=>t.$emit(`start`),disabled:!e.deviceConnected},` ▶ 开始 `,8,U)):e.state===`running`?(k(),l(m,{key:1},[x(`button`,{class:`btn`,onClick:n[1]||=e=>t.$emit(`pause`)},`⏸ 暂停`),x(`button`,{class:`btn btn-danger`,onClick:n[2]||=e=>t.$emit(`stop`)},`⏹ 停止`)],64)):e.state===`paused`?(k(),l(m,{key:2},[x(`button`,{class:`btn btn-primary`,onClick:n[3]||=e=>t.$emit(`resume`)},`▶ 继续`),x(`button`,{class:`btn btn-danger`,onClick:n[4]||=e=>t.$emit(`stop`)},`⏹ 停止`)],64)):h(``,!0),e.state===`running`?(k(),l(`span`,ee)):e.state===`paused`?(k(),l(`span`,te)):h(``,!0),e.error?(k(),l(`span`,ne,r(e.error),1)):h(``,!0),(e.pointCount??0)>0?(k(),l(`span`,re,r(e.pointCount??0)+` pts`,1)):h(``,!0)]))}}),[[`__scopeId`,`data-v-1f82989d`]]),W={class:`rtt-view-tab`},G={key:0,class:`alert alert-warn`},K={class:`rtt-view-toolbar`},q={key:0,class:`line-count`},J={key:2,class:`auto-scroll-toggle`},Y={class:`line-num`},ae={class:`timestamp`},oe={key:0,class:`stream-ended`},X=5e3,se=S(d({__name:`RttViewTab`,props:{deviceConnected:{type:Boolean}},setup(e){let t=L(`rtt`),{data:s,connected:u,connect:d,disconnect:f}=V(`/api/dash/rtt/stream`),{checkConflict:p}=P(),g=i([]),_=i(!0),v=i(!1),b=i(null),S=0,w={rtt:`RTT`,superwatch:`SuperWatch`,vofa:`VOFA+`};function T(e){if(!e)return``;let t=new Date(e*1e3);return`${String(t.getHours()).padStart(2,`0`)}:${String(t.getMinutes()).padStart(2,`0`)}:${String(t.getSeconds()).padStart(2,`0`)}.${String(t.getMilliseconds()).padStart(3,`0`)}`}function E(e){return Object.entries(e).filter(([e])=>!e.startsWith(`_`)).map(([e,t])=>typeof t==`number`?`${e}=${t.toFixed(3)}`:`${e}=${t}`).join(`  `)}function D(e,t,n){S++,g.value.push({num:S,ts:T(n),text:e,type:t}),g.value.length>X&&(g.value=g.value.slice(-X))}n(s,(e,t)=>{let n=t?.length||0;for(let t=n;t<e.length;t++){let n=e[t],r=n.event||n._event,i=n._t;r===`raw`?D(n.line,`raw`,i):(r===`data`||!r)&&D(E(n),`data`,i)}_.value&&A(()=>{b.value&&(b.value.scrollTop=b.value.scrollHeight)})},{deep:!0}),n(u,(e,n)=>{n&&!e&&t.state.value===`running`&&(v.value=!0,D(`[Stream ended]`,`stopped`))});function M(){if(!b.value)return;let{scrollTop:e,scrollHeight:t,clientHeight:n}=b.value;t-e-n>50&&(_.value=!1)}function N(){g.value=[],S=0,v.value=!1}async function F(){let e=await p(`rtt`);if(e.length>0){let t=e.map(e=>w[e]||e).join(`、`);if(!confirm(`启动 RTT 将停止当前运行的 ${t} 会话。确认？`))return}N(),await t.start(),setTimeout(()=>{d()},500)}async function I(){f(),await t.stop()}return(n,i)=>(k(),l(`div`,W,[e.deviceConnected?(k(),l(m,{key:1},[x(`div`,K,[j(ie,{state:a(t).state.value,error:a(t).error.value,"device-connected":e.deviceConnected,onStart:F,onPause:i[0]||=e=>a(t).pause(),onResume:i[1]||=e=>a(t).resume(),onStop:I},null,8,[`state`,`error`,`device-connected`]),g.value.length>0?(k(),l(`span`,q,r(g.value.length)+` 行`,1)):h(``,!0),g.value.length>0?(k(),l(`button`,{key:1,class:`btn-clear`,onClick:N},`清除`)):h(``,!0),a(t).state.value===`running`?(k(),l(`label`,J,[y(x(`input`,{type:`checkbox`,"onUpdate:modelValue":i[2]||=e=>_.value=e},null,512),[[C,_.value]]),i[3]||=O(` 自动滚动 `,-1)])):h(``,!0)]),x(`div`,{class:`rtt-view-log`,ref_key:`logBody`,ref:b,onScroll:M},[(k(!0),l(m,null,c(g.value,e=>(k(),l(`div`,{key:e.num,class:`rtt-log-line`},[x(`span`,Y,r(e.num),1),x(`span`,ae,r(e.ts),1),x(`span`,{class:o([`line-content`,e.type])},r(e.text),3)]))),128)),v.value?(k(),l(`div`,oe,`[Stream ended]`)):h(``,!0)],544)],64)):(k(),l(`div`,G,`请先连接设备。`))]))}}),[[`__scopeId`,`data-v-e3eabe43`]]),ce={class:`hardfault-tab`},le={key:0,class:`alert alert-warn`},ue=[`disabled`],de={key:0,class:`fault-report`},fe={key:0,class:`alert alert-ok`},pe={class:`fault-section`},me={class:`fault-summary`},he={key:0,class:`fault-section`},ge={class:`flag-list`},_e={key:1,class:`fault-section`},ve={class:`flag-list`},ye={key:2,class:`fault-section`},be={class:`desc-table`},xe={key:3,class:`fault-section`},Se={class:`loc-addr`},Ce={class:`loc-file`},we=S(d({__name:`HardFaultTab`,props:{deviceConnected:{type:Boolean}},setup(e){let t=R(),n=E(),a=i(!1),o=i(null);async function s(){a.value=!0;try{o.value=await t.getHardfaultDetail()}catch(e){n.error(e instanceof Error?e.message:String(e))}finally{a.value=!1}}function u(){o.value&&(navigator.clipboard.writeText(JSON.stringify(o.value,null,2)),n.success(`已复制到剪贴板`))}return(t,n)=>(k(),l(`div`,ce,[e.deviceConnected?(k(),l(m,{key:1},[x(`button`,{class:`btn btn-primary`,onClick:s,disabled:a.value},r(a.value?`检查中...`:`检查 HardFault`),9,ue),o.value?(k(),l(`div`,de,[o.value.fault?(k(),l(m,{key:1},[x(`div`,pe,[n[0]||=x(`h4`,null,`概要`,-1),x(`p`,me,r(o.value.summary),1)]),o.value.cfsr_flags?.length?(k(),l(`div`,he,[n[1]||=x(`h4`,null,`CFSR 标志`,-1),x(`div`,ge,[(k(!0),l(m,null,c(o.value.cfsr_flags,e=>(k(),l(`span`,{key:e,class:`flag-badge`},r(e),1))),128))])])):h(``,!0),o.value.hfsr_flags?.length?(k(),l(`div`,_e,[n[2]||=x(`h4`,null,`HFSR 标志`,-1),x(`div`,ve,[(k(!0),l(m,null,c(o.value.hfsr_flags,e=>(k(),l(`span`,{key:e,class:`flag-badge`},r(e),1))),128))])])):h(``,!0),o.value.stack_frame?(k(),l(`div`,ye,[n[3]||=x(`h4`,null,`栈帧`,-1),x(`table`,be,[(k(!0),l(m,null,c(o.value.stack_frame,(e,t)=>(k(),l(`tr`,{key:t},[x(`th`,null,r(t),1),x(`td`,null,r(typeof e==`number`?`0x`+e.toString(16).toUpperCase().padStart(8,`0`):e),1)]))),128))])])):h(``,!0),o.value.source_locations?(k(),l(`div`,xe,[n[4]||=x(`h4`,null,`源码位置`,-1),(k(!0),l(m,null,c(o.value.source_locations,(e,t)=>(k(),l(`div`,{key:t,class:`source-loc`},[x(`span`,Se,r(t),1),x(`span`,Ce,r(e),1)]))),128))])):h(``,!0),x(`button`,{class:`btn`,onClick:u},`复制报告`)],64)):(k(),l(`div`,fe,`无 HardFault`))])):h(``,!0)],64)):(k(),l(`div`,le,`请先连接设备。`))]))}}),[[`__scopeId`,`data-v-94d50644`]]),Te={class:`symbols-tab`},Ee={key:0,class:`alert alert-warn`},De={class:`sym-controls`},Oe={key:0,class:`sym-results`},ke=[`onClick`],Ae={class:`sym-name`},je={class:`sym-type`},Me={class:`sym-addr`},Ne={class:`sym-size`},Pe={key:1,class:`sym-empty`},Fe={key:2,class:`sym-detail`},Ie={key:0,class:`desc-table`},Le={key:1,class:`sym-members`},Re=S(d({__name:`SymbolsTab`,props:{deviceConnected:{type:Boolean}},setup(e){let t=z(),n=E(),a=i(``),o=i(!1),s=i([]),u=i(null),d=null;function p(){d&&clearTimeout(d),d=setTimeout(g,300)}async function g(){if(!a.value.trim()){s.value=[];return}o.value=!0;try{s.value=(await t.search(a.value)).results||[]}catch(e){s.value=[],e instanceof Error&&!e.message.includes(`No DWARF`)&&n.error(e.message)}finally{o.value=!1}}async function _(e){try{u.value=await t.typeinfo(e)}catch(e){n.error(e instanceof Error?e.message:String(e))}}function v(e){return e==null?`—`:typeof e==`number`?`0x`+e.toString(16).toUpperCase().padStart(8,`0`):String(e)}return(t,n)=>(k(),l(`div`,Te,[e.deviceConnected?(k(),l(m,{key:1},[x(`div`,De,[y(x(`input`,{class:`form-input`,"onUpdate:modelValue":n[0]||=e=>a.value=e,placeholder:`搜索符号名...`,onInput:p},null,544),[[f,a.value]])]),s.value.length>0?(k(),l(`div`,Oe,[(k(!0),l(m,null,c(s.value,e=>(k(),l(`div`,{key:e.name,class:`sym-item`,onClick:t=>_(e.name)},[x(`span`,Ae,r(e.name),1),x(`span`,je,r(e.type),1),x(`span`,Me,r(v(e.address)),1),x(`span`,Ne,r(e.size)+`B`,1)],8,ke))),128))])):a.value&&!o.value?(k(),l(`div`,Pe,`无匹配符号`)):h(``,!0),u.value?(k(),l(`div`,Fe,[x(`h4`,null,`类型信息: `+r(u.value.name),1),u.value.found?(k(),l(`table`,Ie,[x(`tr`,null,[n[1]||=x(`th`,null,`类型`,-1),x(`td`,null,r(u.value.type),1)]),x(`tr`,null,[n[2]||=x(`th`,null,`大小`,-1),x(`td`,null,r(u.value.size)+` bytes`,1)]),x(`tr`,null,[n[3]||=x(`th`,null,`地址`,-1),x(`td`,null,r(v(u.value.address)),1)])])):h(``,!0),u.value.members?.length?(k(),l(`div`,Le,[n[4]||=x(`h5`,null,`成员`,-1),(k(!0),l(m,null,c(u.value.members,(e,t)=>(k(),l(`div`,{key:t,class:`sym-member`},r(JSON.stringify(e)),1))),128))])):h(``,!0)])):h(``,!0)],64)):(k(),l(`div`,Ee,`请先连接设备。`))]))}}),[[`__scopeId`,`data-v-689bb2d5`]]),ze={class:`hex-view`},Be={class:`hex-header`},Ve={class:`hex-body`},He={class:`hex-addr`},Ue={class:`hex-ascii`},We=S(d({__name:`HexMemoryView`,props:{dataHex:{},baseAddress:{}},setup(e){let t=e,n=w(()=>{let e=t.dataHex||``,n=[];for(let t=0;t<e.length;t+=2){let r=e.slice(t,t+2);n.push({hex:r.toUpperCase(),value:parseInt(r,16)})}let r=typeof t.baseAddress==`string`?parseInt(t.baseAddress):t.baseAddress||0,i=[];for(let e=0;e<n.length;e+=16){let t=n.slice(e,e+16),a=(r+e).toString(16).toUpperCase().padStart(8,`0`),o=t.map(e=>e.value>=32&&e.value<127?String.fromCharCode(e.value):`.`).join(``);i.push({addr:a,bytes:t,ascii:o})}return i});return(e,t)=>(k(),l(`div`,ze,[x(`div`,Be,[t[0]||=x(`span`,{class:`hex-addr-hdr`},`Address`,-1),(k(),l(m,null,c(16,e=>x(`span`,{key:e,class:`hex-byte-hdr`},r((e-1).toString(16).toUpperCase().padStart(2,`0`)),1)),64)),t[1]||=x(`span`,{class:`hex-ascii-hdr`},`ASCII`,-1)]),x(`div`,Ve,[(k(!0),l(m,null,c(n.value,(e,t)=>(k(),l(`div`,{key:t,class:`hex-row`},[x(`span`,He,r(e.addr),1),(k(!0),l(m,null,c(e.bytes,(e,t)=>(k(),l(`span`,{key:t,class:o([`hex-byte`,{zero:e.value===0}])},r(e.hex),3))),128)),x(`span`,Ue,r(e.ascii),1)]))),128))])]))}}),[[`__scopeId`,`data-v-9ac6f23e`]]),Ge={class:`memory-tab`},Ke={key:0,class:`alert alert-warn`},qe={class:`mem-controls`},Je=[`disabled`],Ye={key:0,class:`mem-result`},Xe={key:1,class:`mem-write`},Ze={class:`mem-controls`},Qe=[`disabled`],$e=S(d({__name:`MemoryTab`,props:{deviceConnected:{type:Boolean}},setup(e){let t=R(),n=E(),r=i(`0x20000000`),a=i(64),o=i(``),s=i(``),c=i(!1),u=i(!1),d=i(null);async function p(){c.value=!0;try{d.value=await t.readMemory(r.value,a.value),o.value=d.value.address}catch(e){n.error(e instanceof Error?e.message:String(e))}finally{c.value=!1}}async function g(){if(s.value.trim()){u.value=!0;try{await t.writeMemory(o.value,s.value.replace(/\s/g,``)),n.success(`写入成功`),await p()}catch(e){n.error(e instanceof Error?e.message:String(e))}finally{u.value=!1}}}return(t,n)=>(k(),l(`div`,Ge,[e.deviceConnected?(k(),l(m,{key:1},[x(`div`,qe,[y(x(`input`,{class:`form-input addr-input`,"onUpdate:modelValue":n[0]||=e=>r.value=e,placeholder:`0x20000000`},null,512),[[f,r.value]]),y(x(`input`,{class:`form-input size-input`,"onUpdate:modelValue":n[1]||=e=>a.value=e,type:`number`,placeholder:`64`,min:`1`,max:`4096`},null,512),[[f,a.value,void 0,{number:!0}]]),x(`button`,{class:`btn btn-primary`,onClick:p,disabled:c.value},`读取`,8,Je)]),d.value?(k(),l(`div`,Ye,[j(We,{"data-hex":d.value.data_hex,"base-address":d.value.address},null,8,[`data-hex`,`base-address`])])):h(``,!0),d.value?(k(),l(`div`,Xe,[n[4]||=x(`h4`,null,`写入内存`,-1),x(`div`,Ze,[y(x(`input`,{class:`form-input addr-input`,"onUpdate:modelValue":n[2]||=e=>o.value=e,placeholder:`地址`},null,512),[[f,o.value]]),y(x(`input`,{class:`form-input`,"onUpdate:modelValue":n[3]||=e=>s.value=e,placeholder:`十六进制数据 (如: 0102A0FF)`},null,512),[[f,s.value]]),x(`button`,{class:`btn`,onClick:g,disabled:u.value},`写入`,8,Qe)])])):h(``,!0)],64)):(k(),l(`div`,Ke,`请先连接设备。`))]))}}),[[`__scopeId`,`data-v-bf2c5727`]]),et=`/assets/rtt_i18n-BhEfGqhE.js`,tt=`/assets/rtt_viewer-B__Ob-q1.js`,Z=S(d({__name:`WaveformViewer`,props:{mode:{},deviceConnected:{type:Boolean}},setup(e){let r=e,a=i();s(()=>{if(!a.value)return;let e=a.value;e.innerHTML=o(r.mode),c(e,r.mode)}),n(()=>r.deviceConnected,e=>{let t=window.__waveformViewers;t?.[r.mode]?.setDeviceConnected&&t[r.mode].setDeviceConnected(e)}),t(()=>{try{let e=window.__waveformViewers;e?.[r.mode]?.es&&e[r.mode].es.close(),e&&delete e[r.mode]}catch{}a.value&&(a.value.innerHTML=``)});function o(e){return`
<header>
  <h1>MKLink ${e}</h1>
  <span id="mode-badge" class="badge badge-mode">${e}</span>
  <span id="conn-status" class="badge badge-ok" data-i18n="live">live</span>
  <span id="pts-count" class="badge badge-info">0 pts</span>
  <span id="sample-rate-badge" class="badge badge-info">rate -- Hz</span>
  <div class="header-actions">
    <button id="btn-lang-toggle" class="panel-btn" title="中文/English">中/En</button>
    <button id="btn-cursor-toggle" class="panel-btn" data-i18n-title="cursors_tip" data-i18n="cursors">Cursors</button>
    <button id="btn-cursor-mode" class="panel-btn" style="display:none;" data-i18n-title="cursor_mode_tip">Time</button>
    <button id="btn-save-project" class="panel-btn" data-i18n-title="save_project_tip" data-i18n="save">Save</button>
    <button id="btn-load-project" class="panel-btn" data-i18n-title="load_project_tip" data-i18n="load">Load</button>
    <button id="btn-thresholds" class="panel-btn" data-i18n-title="thresholds_tip" data-i18n="thresholds">Thresholds</button>
    <button id="btn-export-csv" class="panel-btn" data-i18n-title="export_csv_tip">CSV</button>
    <button id="btn-export-png" class="panel-btn" data-i18n-title="export_png_tip">PNG</button>
    <button id="btn-help" class="panel-btn" data-i18n-title="help_tip">?</button>
    <input id="project-load-input" class="hidden-file-input" type="file" accept="application/json,.json">
  </div>
</header>

<div id="control-toolbar">
  <button id="btn-start" class="ctrl-btn active" data-i18n="start">Start</button>
  <button id="btn-pause" class="ctrl-btn" data-i18n="pause">Pause</button>
  <button id="btn-stop" class="ctrl-btn danger" data-i18n="stop">Stop</button>
  <span id="collection-status-badge" class="status-running" data-i18n="running">Running</span>
  <div class="ctrl-sep"></div>
  <label data-i18n="buffer">Buffer</label>
  <input type="number" id="buffer-input" value="10000" min="2" max="200000" step="10">
  <span class="buffer-unit">pts</span>
  <button id="btn-apply-buffer" class="ctrl-btn" data-i18n="apply">Apply</button>
  <div class="ctrl-sep"></div>
  <div id="interval-group">
    <label data-i18n="interval">Interval</label>
    <input type="number" id="interval-input" value="0" step="0.001" min="0" max="60">
    <span class="interval-unit">s</span>
    <button id="btn-apply-interval" class="ctrl-btn" data-i18n="apply">Apply</button>
  </div>
</div>

<div id="trigger-toolbar">
  <button id="trigger-enable-btn" data-i18n="trigger">Trigger</button>
  <span id="trigger-state-badge" class="trigger-state-idle" data-i18n="idle">Idle</span>
  <div class="trigger-sep"></div>
  <label data-i18n="source">Source</label>
  <select id="trigger-source"><option value="">--</option></select>
  <div class="trigger-sep"></div>
  <label data-i18n="edge">Edge</label>
  <select id="trigger-edge">
    <option value="rising" data-i18n="rising">Rising</option>
    <option value="falling" data-i18n="falling">Falling</option>
    <option value="both" data-i18n="both">Both</option>
  </select>
  <div class="trigger-sep"></div>
  <label data-i18n="level">Level</label>
  <input type="number" id="trigger-level" value="0" step="0.1">
  <div class="trigger-sep"></div>
  <label data-i18n="mode">Mode</label>
  <select id="trigger-mode">
    <option value="auto" data-i18n="auto">Auto</option>
    <option value="normal" data-i18n="normal">Normal</option>
    <option value="single" data-i18n="single">Single</option>
  </select>
  <div class="trigger-sep"></div>
  <label data-i18n="pretrig">Pre-trig</label>
  <input type="number" id="trigger-pretrig" value="1000" min="10" max="50000" step="100">
  <div class="trigger-sep"></div>
  <button id="trigger-force-btn" data-i18n="force_trigger">Force Trigger</button>
</div>

<div id="var-selector"></div>

<div id="superwatch-panel" aria-hidden="true">
  <div id="sw-search-wrap" style="position:relative">
    <input id="superwatch-search-input" data-i18n-placeholder="sw_search_placeholder" placeholder="搜索或输入变量名...">
    <ul id="sw-search-dropdown"></ul>
  </div>
  <button id="superwatch-add-btn" class="panel-btn" data-i18n="add">添加</button>
  <label data-i18n="time">时间</label>
  <select id="time-unit-select">
    <option value="us">us</option>
    <option value="ms" selected>ms</option>
    <option value="s">s</option>
  </select>
  <button id="superwatch-inspect-btn" class="panel-btn" data-i18n="inspect">检查</button>
</div>

<main id="debug-main">
  <section id="chart-watch-wrap">
    <div id="enum-tooltip"></div>
    <div id="chart-wrap">
      <canvas id="chart"></canvas>
      <div id="tooltip"></div>
      <div id="cursor-a" class="cursor-line" style="display:none;"></div>
      <div id="cursor-b" class="cursor-line" style="display:none;"></div>
      <div id="cursor-measure-panel" style="display:none;"></div>
    </div>
    <div id="watch-resizer"></div>
    <div id="watch-panel">
      <div class="panel-header">
        <div class="panel-title">
          <span class="panel-dot"></span>
          <span data-i18n="watch">监视</span>
        </div>
        <div class="panel-actions">
          <span id="watch-count" class="panel-count">0 ch</span>
          <button id="watch-columns-btn" class="panel-btn" data-i18n-title="columns_tip" data-i18n="columns">列</button>
          <button id="watch-collapse" class="panel-btn panel-btn-close" data-i18n-title="collapse_watch" title="折叠监视面板">&#x2715;</button>
        </div>
      </div>
      <div id="watch-columns-menu" class="columns-menu" aria-hidden="true"></div>
      <div id="watch-table-wrap">
        <table id="watch-table">
          <thead>
            <tr id="watch-table-head-row"></tr>
          </thead>
          <tbody id="watch-tbody"></tbody>
        </table>
      </div>
    </div>
  </section>

  <div id="minimap-wrap">
    <canvas id="minimap-canvas"></canvas>
    <div id="minimap-viewport"></div>
    <div id="cursor-readout"></div>
  </div>

  <section id="raw-log-panel" data-open="false">
    <div class="panel-resizer" title="Drag to resize"></div>
    <div class="panel-header">
      <div class="panel-title">
        <span class="panel-dot"></span>
        <span data-i18n="raw_log">原始日志</span>
      </div>
      <div class="panel-actions">
        <span id="raw-log-count" class="panel-count">0 lines</span>
        <button id="raw-log-clear" class="panel-btn" data-i18n-title="clear_log" data-i18n="clear">清除</button>
        <button id="raw-log-close" class="panel-btn panel-btn-close" data-i18n-title="close_panel" title="关闭面板">&#x2715;</button>
      </div>
    </div>
    <pre id="raw-log"></pre>
  </section>
  <section id="inspector-panel" aria-hidden="true"></section>
</main>

<footer id="stats-footer"></footer>
<div id="threshold-overlay" class="config-overlay" aria-hidden="true">
  <div class="config-dialog" role="dialog" aria-modal="true" aria-labelledby="threshold-title">
    <h2 id="threshold-title" data-i18n="thresholds">阈值</h2>
    <div class="config-grid">
      <div class="config-field full">
        <label for="threshold-channel" data-i18n="channel">通道</label>
        <select id="threshold-channel"></select>
      </div>
      <div class="config-field">
        <label for="threshold-warn-low" data-i18n="warn_low">警告下限</label>
        <input id="threshold-warn-low" type="number" step="0.1">
      </div>
      <div class="config-field">
        <label for="threshold-warn-high" data-i18n="warn_high">警告上限</label>
        <input id="threshold-warn-high" type="number" step="0.1">
      </div>
      <div class="config-field">
        <label for="threshold-alarm-low" data-i18n="alarm_low">报警下限</label>
        <input id="threshold-alarm-low" type="number" step="0.1">
      </div>
      <div class="config-field">
        <label for="threshold-alarm-high" data-i18n="alarm_high">报警上限</label>
        <input id="threshold-alarm-high" type="number" step="0.1">
      </div>
    </div>
    <div class="config-actions">
      <button id="threshold-clear" class="panel-btn" data-i18n="clear">清除</button>
      <button id="threshold-cancel" class="panel-btn" data-i18n="cancel">取消</button>
      <button id="threshold-apply" class="panel-btn" data-i18n="apply">应用</button>
    </div>
  </div>
</div>
<div id="shutdown-overlay">
  <h2 data-i18n="server_shutdown">服务器已关闭</h2>
  <p data-i18n="server_stopped_msg">可视化服务器已停止。</p>
  <p data-i18n="close_tab_msg">可以关闭此标签页。</p>
</div>

<div id="help-overlay" aria-hidden="true">
  <div id="help-modal" role="dialog" aria-modal="true" aria-labelledby="help-modal-title">
    <div id="help-modal-header">
      <h2 id="help-modal-title" data-i18n="help_title">使用说明</h2>
      <button id="help-close-btn" data-i18n-title="close_esc" title="关闭 (Esc)">&times;</button>
    </div>
    <div id="help-modal-body">
      <div class="help-section"><h3 data-i18n="help_chart">图表交互</h3><ul id="help-chart-list"></ul></div>
      <div class="help-section"><h3 data-i18n="help_var_selector">变量选择器</h3><ul id="help-var-list"></ul></div>
      <div class="help-section"><h3 data-i18n="help_trigger_sys">触发系统</h3><ul id="help-trigger-list"></ul></div>
      <div class="help-section"><h3 data-i18n="help_watch_panel">Watch 面板</h3><ul id="help-watch-list"></ul></div>
      <div class="help-section"><h3 data-i18n="help_minimap">缩略图</h3><ul id="help-minimap-list"></ul></div>
      <div class="help-section"><h3 data-i18n="help_cursors">测量光标</h3><ul id="help-cursors-list"></ul></div>
      <div class="help-section"><h3 data-i18n="help_export">数据导出</h3><ul id="help-export-list"></ul></div>
      <div class="help-section"><h3 data-i18n="help_shortcuts">键盘快捷键</h3><table class="help-kbd-table" id="help-kbd-table"></table></div>
      <div class="help-section"><h3 data-i18n="help_rawlog">Raw Log 面板</h3><ul id="help-rawlog-list"></ul></div>
      <div class="help-section"><h3 data-i18n="help_pause_resume">暂停/恢复</h3><ul id="help-pause-list"></ul></div>
    </div>
  </div>
</div>`}function c(e,t){let n=document.createElement(`script`);n.textContent=`
    var CONFIG = {
      maxPoints: 10000,
      title: "MKLink ${t}",
      mode: "${t}",
      lang: "zh",
      deviceConnected: ${r.deviceConnected}
    };
  `,e.appendChild(n);let i=document.createElement(`script`);i.src=et,i.onload=()=>{typeof window.applyI18n==`function`&&window.applyI18n(),u(e)},e.appendChild(i)}function u(e){let t=document.createElement(`script`);t.src=tt,t.onload=()=>{let e=window.__waveformViewers;e&&!e[r.mode]?e[r.mode]={es:window.es}:e?.[r.mode]&&(e[r.mode].es=window.es)},e.appendChild(t)}return(e,t)=>(k(),l(`div`,{ref_key:`container`,ref:a,class:`waveform-viewer`},null,512))}}),[[`__scopeId`,`data-v-e19e6964`]]),nt=d({__name:`SuperWatchTab`,props:{deviceConnected:{type:Boolean}},setup(e){return(t,n)=>(k(),g(Z,{mode:`SuperWatch`,"device-connected":e.deviceConnected},null,8,[`device-connected`]))}}),rt={key:0,class:`alert alert-warn`},it={class:`form-row`,style:{gap:`8px`,"flex-wrap":`wrap`,"align-items":`end`}},at=[`value`],ot=[`value`],st={key:0,class:`form-row`,style:{gap:`8px`,"margin-top":`8px`}},ct={style:{"font-size":`12px`,display:`flex`,"align-items":`center`,gap:`4px`}},lt=[`disabled`],ut={key:1,style:{"margin-top":`8px`,"font-size":`12px`,color:`#888`,display:`flex`,gap:`16px`}},dt={class:`ts`},ft={class:`hex`},pt={key:0,class:`ascii`},Q=2e3,mt=S(d({__name:`SerialMonitorTab`,props:{deviceConnected:{type:Boolean}},setup(e){let n=`http://127.0.0.1:8765`,a=E(),{listPorts:u}=_(),d=i([]),p=i(``),g=i(115200),v=i(8),b=i(1),S=i(`N`),w=i(!1),j=i(``),M=i(!1),N=i([]),P=i({rx_count:0,tx_count:0,rx_bytes:0,tx_bytes:0,bytes_per_sec:0}),F=i(`closed`),I=i(null),L=null;s(async()=>{try{d.value=await u(),d.value.length&&(p.value=d.value[0].device)}catch{}}),t(()=>{z()});async function R(){if(!p.value){a.error(`请选择端口`);return}try{await fetch(`${n}/api/dash/serial/start`,{method:`POST`,headers:{"Content-Type":`application/json`},body:JSON.stringify({ports:[{port:p.value,baudrate:g.value,databits:v.value,stopbits:b.value,parity:S.value}]})}),w.value=!0,N.value=[],B(),a.success(`串口监控已启动`)}catch(e){a.error(`启动失败: `+e.message)}}function z(){L&&=(L.close(),null),w.value&&(w.value=!1,fetch(`${n}/api/dash/serial/stop`,{method:`POST`}).catch(()=>{}),a.info(`串口监控已停止`))}function B(){L&&=(L.close(),null),L=new EventSource(`${n}/api/dash/serial/stream`),L.onmessage=e=>{try{let t=JSON.parse(e.data);if(t.event===`data`)N.value.push(t),N.value.length>Q&&(N.value=N.value.slice(-Q)),A(()=>{I.value&&(I.value.scrollTop=I.value.scrollHeight)});else if(t.event===`status`){let e=t.ports||{};F.value=Object.values(e).join(`, `)||`open`,t.stats&&(P.value=t.stats)}else t.event===`stopped`&&(w.value=!1,L&&=(L.close(),null))}catch{}},L.onerror=()=>{L?.readyState===EventSource.CLOSED&&(w.value=!1)}}async function V(){if(j.value.trim())try{await fetch(`${n}/api/dash/serial/send`,{method:`POST`,headers:{"Content-Type":`application/json`},body:JSON.stringify({port:p.value,data:j.value,hex:M.value})}),j.value=``}catch(e){a.error(`发送失败: `+e.message)}}function H(){N.value=[]}return(t,n)=>(k(),l(`div`,null,[e.deviceConnected?(k(),l(m,{key:1},[x(`div`,it,[x(`div`,null,[n[7]||=x(`label`,{class:`form-label`,style:{"font-size":`12px`}},`端口`,-1),y(x(`select`,{"onUpdate:modelValue":n[0]||=e=>p.value=e,class:`form-input`,style:{width:`120px`}},[(k(!0),l(m,null,c(d.value,e=>(k(),l(`option`,{key:e.device,value:e.device},r(e.device),9,at))),128))],512),[[T,p.value]])]),x(`div`,null,[n[8]||=x(`label`,{class:`form-label`,style:{"font-size":`12px`}},`波特率`,-1),y(x(`select`,{"onUpdate:modelValue":n[1]||=e=>g.value=e,class:`form-input`,style:{width:`100px`}},[(k(),l(m,null,c([9600,19200,38400,57600,115200,230400,460800,921600],e=>x(`option`,{key:e,value:e},r(e),9,ot)),64))],512),[[T,g.value]])]),x(`div`,null,[n[10]||=x(`label`,{class:`form-label`,style:{"font-size":`12px`}},`数据位`,-1),y(x(`select`,{"onUpdate:modelValue":n[2]||=e=>v.value=e,class:`form-input`,style:{width:`60px`}},[...n[9]||=[x(`option`,{value:8},`8`,-1),x(`option`,{value:7},`7`,-1)]],512),[[T,v.value]])]),x(`div`,null,[n[12]||=x(`label`,{class:`form-label`,style:{"font-size":`12px`}},`停止位`,-1),y(x(`select`,{"onUpdate:modelValue":n[3]||=e=>b.value=e,class:`form-input`,style:{width:`60px`}},[...n[11]||=[x(`option`,{value:1},`1`,-1),x(`option`,{value:2},`2`,-1)]],512),[[T,b.value]])]),x(`div`,null,[n[14]||=x(`label`,{class:`form-label`,style:{"font-size":`12px`}},`校验`,-1),y(x(`select`,{"onUpdate:modelValue":n[4]||=e=>S.value=e,class:`form-input`,style:{width:`60px`}},[...n[13]||=[x(`option`,{value:`N`},`无`,-1),x(`option`,{value:`E`},`偶`,-1),x(`option`,{value:`O`},`奇`,-1)]],512),[[T,S.value]])]),w.value?(k(),l(`button`,{key:1,class:`btn btn-danger`,onClick:z},`停止`)):(k(),l(`button`,{key:0,class:`btn btn-primary`,onClick:R},`开始监控`))]),w.value?(k(),l(`div`,st,[y(x(`input`,{"onUpdate:modelValue":n[5]||=e=>j.value=e,class:`form-input`,style:{flex:`1`},placeholder:`输入要发送的数据...`,onKeydown:D(V,[`enter`])},null,544),[[f,j.value]]),x(`label`,ct,[y(x(`input`,{type:`checkbox`,"onUpdate:modelValue":n[6]||=e=>M.value=e},null,512),[[C,M.value]]),n[15]||=O(` HEX `,-1)]),x(`button`,{class:`btn`,onClick:V,disabled:!j.value.trim()},`发送`,8,lt),x(`button`,{class:`btn`,onClick:H},`清空`)])):h(``,!0),w.value?(k(),l(`div`,ut,[x(`span`,null,`RX: `+r(P.value.rx_count)+` (`+r(P.value.rx_bytes)+`B)`,1),x(`span`,null,`TX: `+r(P.value.tx_count)+` (`+r(P.value.tx_bytes)+`B)`,1),x(`span`,null,`速率: `+r(P.value.bytes_per_sec)+` B/s`,1),x(`span`,null,`端口: `+r(F.value),1)])):h(``,!0),w.value||N.value.length?(k(),l(`div`,{key:2,class:`serial-log`,ref_key:`logEl`,ref:I},[(k(!0),l(m,null,c(N.value,(e,t)=>(k(),l(`div`,{key:t,class:o([`serial-line`,e.direction===`TX`?`tx`:`rx`])},[x(`span`,dt,r(e.timestamp),1),x(`span`,{class:o([`dir`,e.direction])},r(e.direction),3),x(`span`,ft,r(e.raw_hex),1),e.ascii&&e.ascii.trim()?(k(),l(`span`,pt,r(e.ascii.trim()),1)):h(``,!0)],2))),128))],512)):h(``,!0)],64)):(k(),l(`div`,rt,`请先连接设备。`))]))}}),[[`__scopeId`,`data-v-391692b6`]]),ht={key:0,class:`alert alert-warn`},gt={class:`form-row`,style:{gap:`8px`,"flex-wrap":`wrap`,"align-items":`end`}},_t=[`value`],vt=[`value`],yt={class:`form-row`,style:{gap:`8px`,"margin-top":`8px`,"align-items":`end`}},bt={key:0,class:`reg-grid`,style:{"margin-top":`10px`}},xt={class:`reg-addr`},St={class:`reg-hex`},Ct={class:`reg-dec`},wt=[`onClick`],Tt={key:1,style:{"margin-top":`10px`,color:`#888`,"font-size":`13px`}},Et={class:`modal-card`},Dt={class:`card-title`},Ot={class:`form-row`,style:{gap:`8px`}},kt=S(d({__name:`ModbusTab`,props:{deviceConnected:{type:Boolean}},setup(e){let n=`http://127.0.0.1:8765`,a=E(),{listPorts:o}=_(),u=i([]),d=i(``),g=i(1),v=i(9600),b=i(`N`),S=i(1e3),C=i(0),w=i(10),D=i(!1),O=i([]),A=i(null),j=i(``),M=null;s(async()=>{try{u.value=await o(),u.value.length&&(d.value=u.value[0].device)}catch{}}),t(()=>{F()});function N(){let e=[];for(let t=0;t<w.value;t++)e.push({addr:C.value+t,value:null});return e}async function P(){if(!d.value){a.error(`请选择端口`);return}try{let e=[];for(let t=0;t<w.value;t++)e.push({addr:C.value+t,type:`uint16`,name:`R${C.value+t}`});await fetch(`${n}/api/dash/modbus/start`,{method:`POST`,headers:{"Content-Type":`application/json`},body:JSON.stringify({port:d.value,slave:g.value,baudrate:v.value,parity:b.value,registers:e,interval:S.value/1e3})}),D.value=!0,O.value=N(),I(),a.success(`Modbus 已连接`)}catch(e){a.error(`连接失败: `+e.message)}}function F(){M&&=(M.close(),null),D.value&&(D.value=!1,fetch(`${n}/api/dash/modbus/stop`,{method:`POST`}).catch(()=>{}),a.info(`Modbus 已断开`))}function I(){M&&=(M.close(),null),M=new EventSource(`${n}/api/dash/modbus/stream`),M.onmessage=e=>{try{let t=JSON.parse(e.data);if(t.event===`data`&&t.registers)for(let e of O.value){let n=t.registers[e.addr];n&&(e.value=n.value)}else t.event===`error`?a.error(t.message):t.event===`stopped`&&(D.value=!1,M&&=(M.close(),null))}catch{}},M.onerror=()=>{M?.readyState===EventSource.CLOSED&&(D.value=!1)}}function L(e){A.value=e,j.value=``}async function R(){if(!A.value)return;let e,t=j.value.trim();if(e=t.startsWith(`0x`)||t.startsWith(`0X`)?parseInt(t,16):parseInt(t,10),isNaN(e)){a.error(`无效的数值`);return}try{await fetch(`${n}/api/dash/modbus/write`,{method:`POST`,headers:{"Content-Type":`application/json`},body:JSON.stringify({addr:A.value.addr,value:e})}),a.success(`已写入寄存器 ${A.value.addr} = ${e}`),A.value=null}catch(e){a.error(`写入失败: `+e.message)}}return(t,n)=>(k(),l(`div`,null,[e.deviceConnected?(k(),l(m,{key:1},[x(`div`,gt,[x(`div`,null,[n[10]||=x(`label`,{class:`form-label`,style:{"font-size":`12px`}},`端口`,-1),y(x(`select`,{"onUpdate:modelValue":n[0]||=e=>d.value=e,class:`form-input`,style:{width:`120px`}},[(k(!0),l(m,null,c(u.value,e=>(k(),l(`option`,{key:e.device,value:e.device},r(e.device),9,_t))),128))],512),[[T,d.value]])]),x(`div`,null,[n[11]||=x(`label`,{class:`form-label`,style:{"font-size":`12px`}},`从站地址`,-1),y(x(`input`,{type:`number`,"onUpdate:modelValue":n[1]||=e=>g.value=e,class:`form-input`,style:{width:`70px`},min:`1`,max:`247`},null,512),[[f,g.value,void 0,{number:!0}]])]),x(`div`,null,[n[12]||=x(`label`,{class:`form-label`,style:{"font-size":`12px`}},`波特率`,-1),y(x(`select`,{"onUpdate:modelValue":n[2]||=e=>v.value=e,class:`form-input`,style:{width:`90px`}},[(k(),l(m,null,c([9600,19200,38400,57600,115200],e=>x(`option`,{key:e,value:e},r(e),9,vt)),64))],512),[[T,v.value]])]),x(`div`,null,[n[14]||=x(`label`,{class:`form-label`,style:{"font-size":`12px`}},`校验`,-1),y(x(`select`,{"onUpdate:modelValue":n[3]||=e=>b.value=e,class:`form-input`,style:{width:`60px`}},[...n[13]||=[x(`option`,{value:`N`},`无`,-1),x(`option`,{value:`E`},`偶`,-1),x(`option`,{value:`O`},`奇`,-1)]],512),[[T,b.value]])]),x(`div`,null,[n[15]||=x(`label`,{class:`form-label`,style:{"font-size":`12px`}},`轮询(ms)`,-1),y(x(`input`,{type:`number`,"onUpdate:modelValue":n[4]||=e=>S.value=e,class:`form-input`,style:{width:`80px`},min:`100`,step:`100`},null,512),[[f,S.value,void 0,{number:!0}]])]),D.value?(k(),l(`button`,{key:1,class:`btn btn-danger`,onClick:F},`断开`)):(k(),l(`button`,{key:0,class:`btn btn-primary`,onClick:P},`连接`))]),x(`div`,yt,[x(`div`,null,[n[16]||=x(`label`,{class:`form-label`,style:{"font-size":`12px`}},`起始地址`,-1),y(x(`input`,{type:`number`,"onUpdate:modelValue":n[5]||=e=>C.value=e,class:`form-input`,style:{width:`80px`},min:`0`},null,512),[[f,C.value,void 0,{number:!0}]])]),x(`div`,null,[n[17]||=x(`label`,{class:`form-label`,style:{"font-size":`12px`}},`数量`,-1),y(x(`input`,{type:`number`,"onUpdate:modelValue":n[6]||=e=>w.value=e,class:`form-input`,style:{width:`70px`},min:`1`,max:`125`},null,512),[[f,w.value,void 0,{number:!0}]])])]),D.value&&O.value.length?(k(),l(`div`,bt,[n[18]||=x(`div`,{class:`reg-header`},[x(`span`,null,`地址`),x(`span`,null,`HEX`),x(`span`,null,`DEC`),x(`span`,null,`操作`)],-1),(k(!0),l(m,null,c(O.value,e=>(k(),l(`div`,{key:e.addr,class:`reg-row`},[x(`span`,xt,r(e.addr),1),x(`span`,St,`0x`+r((e.value??0).toString(16).toUpperCase().padStart(4,`0`)),1),x(`span`,Ct,r(e.value??`—`),1),x(`button`,{class:`btn btn-sm`,onClick:t=>L(e)},`写`,8,wt)]))),128))])):D.value?(k(),l(`div`,Tt,` 等待数据... `)):h(``,!0),A.value?(k(),l(`div`,{key:2,class:`modal-overlay`,onClick:n[9]||=p(e=>A.value=null,[`self`])},[x(`div`,Et,[x(`div`,Dt,`写入寄存器 `+r(A.value.addr),1),x(`div`,Ot,[y(x(`input`,{"onUpdate:modelValue":n[7]||=e=>j.value=e,class:`form-input`,style:{flex:`1`},placeholder:`值 (十进制或 0xHEX)`},null,512),[[f,j.value]]),x(`button`,{class:`btn btn-primary`,onClick:R},`写入`),x(`button`,{class:`btn`,onClick:n[8]||=e=>A.value=null},`取消`)])])])):h(``,!0)],64)):(k(),l(`div`,ht,`请先连接设备。`))]))}}),[[`__scopeId`,`data-v-aeaaa9f7`]]),At=d({__name:`VofaTab`,props:{deviceConnected:{type:Boolean}},setup(e){return(t,n)=>(k(),g(Z,{mode:`VOFA`,"device-connected":e.deviceConnected},null,8,[`device-connected`]))}}),jt={class:`dash-root`},Mt={class:`card-title-row`},Nt={class:`title-right`},$={key:1,class:`resource-status-inline`},Pt={key:0},Ft={key:1},It={class:`tabs-bar`},Lt={key:0},Rt={key:0,class:`alert alert-warn`},zt={class:`form-row`},Bt={class:`form-row`},Vt={style:{"font-size":`13px`}},Ht={class:`form-row`},Ut={style:{"font-size":`13px`}},Wt={class:`form-row`},Gt=[`disabled`],Kt={key:1},qt={key:0,class:`alert alert-warn`},Jt={key:1,class:`btn-group`},Yt=S(d({__name:`DashboardView`,setup(t){let n=u(),{deviceStatus:s,flashDevice:c,resetDevice:d,eraseDevice:p,haltDevice:v,resumeDevice:S}=_(),T=E(),{refresh:D,getBridgeOwner:A}=P(),M=i(`rtt`),N=i(!1),F=e({firmware:``,verify:!0,reset_after:!0}),I=w(()=>A()),L=w(()=>{let e=I.value;return e?{"user:dashboard:rtt":`RTT View`,"user:dashboard:superwatch":`SuperWatch`,"user:dashboard:vofa":`VOFA+`}[e]||e:``});D(),setInterval(D,3e3);function R(){n.push({name:`config`})}async function z(){N.value=!0;try{let e=await c(F);T.success(`烧录完成: `+JSON.stringify(e))}catch(e){T.error(`烧录失败: `+e.message)}finally{N.value=!1}}async function B(){if(confirm(`确定要复位 CPU？`))try{await d(),T.success(`已复位`)}catch(e){T.error(e.message)}}async function V(){if(confirm(`确定要整片擦除？此操作不可撤销。`))try{await p(),T.success(`整片擦除完成`)}catch(e){T.error(e.message)}}async function H(){if(confirm(`确定要暂停 CPU？`))try{await v(),T.info(`CPU 已暂停`)}catch(e){T.error(e.message)}}async function U(){try{await S(),T.success(`CPU 已恢复`)}catch(e){T.error(e.message)}}return(e,t)=>(k(),l(`div`,jt,[x(`div`,{class:o([`card`,{"card-full":M.value===`superwatch`||M.value===`vofa`}])},[x(`div`,Mt,[t[13]||=x(`div`,{class:`card-title`},`仪表盘`,-1),x(`div`,Nt,[a(s).connected?I.value?(k(),l(`span`,$,[x(`span`,{class:o([`status-dot`,I.value.startsWith(`ai:`)?`dot-ai`:`dot-user`])},null,2),I.value.startsWith(`ai:`)?(k(),l(`span`,Pt,`AI 正在使用设备`)):(k(),l(`span`,Ft,r(L.value),1))])):h(``,!0):(k(),l(`span`,{key:0,class:`device-link`,onClick:R},` 设备未连接，点击连接 `))])]),x(`div`,It,[x(`button`,{class:o([`tab-btn`,{active:M.value===`rtt`}]),onClick:t[0]||=e=>M.value=`rtt`},`RTT View`,2),x(`button`,{class:o([`tab-btn`,{active:M.value===`flash`}]),onClick:t[1]||=e=>M.value=`flash`},`烧录`,2),x(`button`,{class:o([`tab-btn`,{active:M.value===`debug`}]),onClick:t[2]||=e=>M.value=`debug`},`调试控制`,2),x(`button`,{class:o([`tab-btn`,{active:M.value===`hardfault`}]),onClick:t[3]||=e=>M.value=`hardfault`},`HardFault`,2),x(`button`,{class:o([`tab-btn`,{active:M.value===`symbols`}]),onClick:t[4]||=e=>M.value=`symbols`},`符号表`,2),x(`button`,{class:o([`tab-btn`,{active:M.value===`memory`}]),onClick:t[5]||=e=>M.value=`memory`},`内存`,2),x(`button`,{class:o([`tab-btn`,{active:M.value===`superwatch`}]),onClick:t[6]||=e=>M.value=`superwatch`},`SuperWatch`,2),x(`button`,{class:o([`tab-btn`,{active:M.value===`serial`}]),onClick:t[7]||=e=>M.value=`serial`},`串口监控`,2),x(`button`,{class:o([`tab-btn`,{active:M.value===`modbus`}]),onClick:t[8]||=e=>M.value=`modbus`},`Modbus`,2),x(`button`,{class:o([`tab-btn`,{active:M.value===`vofa`}]),onClick:t[9]||=e=>M.value=`vofa`},`VOFA+`,2)]),y(j(se,{"device-connected":a(s).connected},null,8,[`device-connected`]),[[b,M.value===`rtt`]]),M.value===`flash`?(k(),l(`div`,Lt,[a(s).connected?(k(),l(m,{key:1},[x(`div`,zt,[t[14]||=x(`span`,{class:`form-label`},`固件文件`,-1),y(x(`input`,{class:`form-input`,"onUpdate:modelValue":t[10]||=e=>F.firmware=e,placeholder:`.hex 或 .bin 文件路径`},null,512),[[f,F.firmware]])]),x(`div`,Bt,[t[16]||=x(`span`,{class:`form-label`},`烧录后校验`,-1),x(`label`,Vt,[y(x(`input`,{type:`checkbox`,"onUpdate:modelValue":t[11]||=e=>F.verify=e},null,512),[[C,F.verify]]),t[15]||=O(` 启用`,-1)])]),x(`div`,Ht,[t[18]||=x(`span`,{class:`form-label`},`烧录后复位`,-1),x(`label`,Ut,[y(x(`input`,{type:`checkbox`,"onUpdate:modelValue":t[12]||=e=>F.reset_after=e},null,512),[[C,F.reset_after]]),t[17]||=O(` 启用`,-1)])]),x(`div`,Wt,[t[19]||=x(`span`,{class:`form-label`},null,-1),x(`button`,{class:`btn btn-primary`,onClick:z,disabled:N.value},r(N.value?`烧录中...`:`烧录固件`),9,Gt)])],64)):(k(),l(`div`,Rt,`请先连接设备。`))])):h(``,!0),M.value===`debug`?(k(),l(`div`,Kt,[a(s).connected?(k(),l(`div`,Jt,[x(`button`,{class:`btn`,onClick:H},`暂停 CPU`),x(`button`,{class:`btn`,onClick:U},`恢复 CPU`),x(`button`,{class:`btn`,onClick:B},`复位`),x(`button`,{class:`btn btn-danger`,onClick:V},`整片擦除`)])):(k(),l(`div`,qt,`请先连接设备。`))])):h(``,!0),M.value===`hardfault`?(k(),g(we,{key:2,"device-connected":a(s).connected},null,8,[`device-connected`])):h(``,!0),M.value===`symbols`?(k(),g(Re,{key:3,"device-connected":a(s).connected},null,8,[`device-connected`])):h(``,!0),M.value===`memory`?(k(),g($e,{key:4,"device-connected":a(s).connected},null,8,[`device-connected`])):h(``,!0),M.value===`superwatch`?(k(),g(nt,{key:5,"device-connected":a(s).connected},null,8,[`device-connected`])):h(``,!0),y(j(mt,{"device-connected":a(s).connected},null,8,[`device-connected`]),[[b,M.value===`serial`]]),y(j(kt,{"device-connected":a(s).connected},null,8,[`device-connected`]),[[b,M.value===`modbus`]]),M.value===`vofa`?(k(),g(At,{key:6,"device-connected":a(s).connected},null,8,[`device-connected`])):h(``,!0)],2)]))}}),[[`__scopeId`,`data-v-9070a40e`]]);export{Yt as default};