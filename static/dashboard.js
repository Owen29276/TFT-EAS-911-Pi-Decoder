const socket = io();

// ── badge helpers ──────────────────────────────────────────────────────────
const DANGER_EVENTS  = new Set(['TOR','TOA','HUW','HUA','TSW','TSA','EAN','CEM','CDW','EVI','CAE','LEW','LAE','SPW']);
const WARNING_EVENTS = new Set(['SVR','SVA','HWW','HWA','FFW','FFA','FLW','FLA','WSW','WSA','BZW','SQW','EWW','DSW','SMW']);
const TEST_EVENTS    = new Set(['RWT','RMT','NPT','DMO']);
const BADGE_LABELS   = {
  TOR:'Tornado Warning',TOA:'Tornado Watch',SVR:'Severe Thunderstorm Warning',
  SVA:'Severe Thunderstorm Watch',FFW:'Flash Flood Warning',FFA:'Flash Flood Watch',
  HUW:'Hurricane Warning',HUA:'Hurricane Watch',TSW:'Tsunami Warning',
  EAN:'Emergency Action Notification',CEM:'Civil Emergency Message',
  RWT:'Required Weekly Test',RMT:'Required Monthly Test',NPT:'National Periodic Test',
  DMO:'Practice/Demo',SPS:'Special Weather Statement',FLW:'Flood Warning',
  WSW:'Winter Storm Warning',WSA:'Winter Storm Watch',BZW:'Blizzard Warning',
  CAE:'Child Abduction Emergency',LEW:'Law Enforcement Warning',
  LAE:'Local Area Emergency',SPW:'Shelter in Place Warning',
  EWW:'Extreme Wind Warning',DSW:'Dust Storm Warning',
};
function badgeClass(c) { return DANGER_EVENTS.has(c)?'badge-danger':WARNING_EVENTS.has(c)?'badge-warn':TEST_EVENTS.has(c)?'badge-success':'badge-info'; }
function badgeLabel(c) { return BADGE_LABELS[c] || c || 'Unknown'; }

// ── countdowns ─────────────────────────────────────────────────────────────
function formatCountdown(utc) {
  if (!utc) return {badge:'cd-indefinite',label:'indefinite',time:''};
  const diff = Math.floor((new Date(utc.replace('Z','+00:00')) - new Date()) / 1000);
  if (diff <= 0) return {badge:'cd-expired',label:'expired',time:''};
  const h=Math.floor(diff/3600), m=Math.floor((diff%3600)/60), s=diff%60;
  return {badge:'cd-active',label:'active',time:h?`${h}h ${m}m`:m?`${m}m ${s}s`:`${s}s`};
}
function updateCountdowns() {
  document.querySelectorAll('.alert-item[data-expires]').forEach(el => {
    const cb = el.querySelector('.countdown-badge'), ct = el.querySelector('.countdown-time');
    if (!cb || !ct) return;
    const r = formatCountdown(el.dataset.expires || null);
    cb.className = 'countdown-badge ' + r.badge;
    cb.textContent = r.label;
    ct.textContent = r.time;
  });
}
setInterval(updateCountdowns, 1000);
updateCountdowns();

// ── socket ─────────────────────────────────────────────────────────────────
socket.on('connect', () => {
  document.getElementById('conn-status').innerHTML = '<span class="dot dot-green"></span>live';
});
socket.on('disconnect', () => {
  document.getElementById('conn-status').innerHTML = '<span class="dot dot-red"></span>disconnected';
});
socket.on('new_alert', alert => {
  prependAlert(alert, document.getElementById('alert-feed'));
  updateFeedCount();
  document.getElementById('stat-today').textContent  = +document.getElementById('stat-today').textContent + 1;
  document.getElementById('stat-last').textContent   = alert.received_local || '';
  document.getElementById('stat-total').textContent  = +document.getElementById('stat-total').textContent + 1;
  if (alert.event_code === 'RWT') document.getElementById('stat-rwt').textContent = alert.received_local || '';
});
socket.on('ptt_error', ({error}) => {
  toast('PTT: ' + error, false);
  pttCleanup();
});

// ── log streaming ──────────────────────────────────────────────────────────
socket.on('log_line', ({line}) => {
  const box = document.getElementById('log-box');
  if (!box) return;
  // Clear placeholder text on first real line
  if (box.firstChild && box.firstChild.textContent === 'Waiting for log lines…') box.innerHTML = '';
  const span = document.createElement('span');
  span.className = (line.includes('ERROR')||line.includes('CRIT')) ? 'log-line-error'
                 : line.includes('WARN') ? 'log-line-warning' : 'log-line-info';
  span.textContent = line;
  box.appendChild(span);
  box.appendChild(document.createTextNode('\n'));
  while (box.childNodes.length > 800) box.removeChild(box.firstChild);
  if (document.getElementById('page-logs').classList.contains('active'))
    box.scrollTop = box.scrollHeight;
});

// ── alert rendering ────────────────────────────────────────────────────────
function prependAlert(alert, container) {
  const empty = container.querySelector('.empty');
  if (empty) empty.remove();
  const code = alert.event_code || '???';
  const locs = (alert.locations_pretty||[]).slice(0,3).join(', ') || 'Unknown location';
  const more = (alert.locations_pretty||[]).length > 3 ? ` +${alert.locations_pretty.length-3} more` : '';
  const div  = document.createElement('div');
  div.className = 'alert-item';
  div.dataset.expires = alert.expires_utc || '';
  div.innerHTML = `
    <div>
      <div class="alert-top"><span class="badge ${badgeClass(code)}">${badgeLabel(code)}</span><span class="event-code">${code} · ${alert.originator_code||'???'}</span></div>
      <div class="alert-locations">${locs}${more}</div>
      <div class="alert-meta">${alert.received_local||''} · ${alert.sender||''}</div>
    </div>
    <div class="countdown-col"><div class="countdown-badge cd-active">active</div><div class="countdown-time"></div></div>`;
  container.insertBefore(div, container.firstChild);
  updateCountdowns();
}
function updateFeedCount() {
  document.getElementById('feed-count').textContent = document.querySelectorAll('#alert-feed .alert-item').length + ' alerts';
}

// ── page navigation ────────────────────────────────────────────────────────
function showPage(name, el) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  el.classList.add('active');
  if (name === 'control') checkControlStatus();
  if (name === 'logs') document.getElementById('log-box').scrollTop = document.getElementById('log-box').scrollHeight;
}
function filterHistory() {
  const q = document.getElementById('search').value.toLowerCase();
  document.querySelectorAll('.history-item').forEach(el => {
    el.style.display = (el.dataset.text||'').toLowerCase().includes(q) ? '' : 'none';
  });
}
function downloadLog() { window.location.href = '/api/alerts'; }
function clearLogs() { document.getElementById('log-box').innerHTML = ''; }

// ── toast ──────────────────────────────────────────────────────────────────
function toast(msg, ok=true) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `toast show ${ok?'ok':'fail'}`;
  setTimeout(() => el.className = 'toast', 3500);
}

// ── API helpers ────────────────────────────────────────────────────────────
async function post(url, body={}) {
  try {
    const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    if (r.status === 401) { location.href = '/login'; return {ok:false, error:'Session expired'}; }
    return await r.json();
  } catch(e) {
    return {ok: false, error: 'Network error — is the server running?'};
  }
}
async function apiFetch(url) {
  try {
    const r = await fetch(url);
    if (r.status === 401) { location.href = '/login'; return null; }
    return await r.json();
  } catch(e) {
    return null;
  }
}
// Send an origination; if ERN §1.4(3) needs a basis, ask for it and retry once
async function postOriginate(body) {
  let r = await post('/api/control/originate', body);
  if (r && r.needs_basis) {
    const ans = prompt(`${r.error}\n\nType one of:\n  netops <approver name>\n  agency <agency name>`);
    if (!ans) return {ok:false, error:'Not sent — no ERN basis given'};
    const m = ans.trim().match(/^(netops|agency)\s+(.{2,})$/i);
    if (!m) return {ok:false, error:'Not sent — start with "netops" or "agency" and a name'};
    r = await post('/api/control/originate', {...body,
      ern_basis: m[1].toLowerCase() === 'agency' ? 'official_agency' : 'netops', ern_note: m[2]});
  }
  if (r && r.ok && r.warning) toast(r.warning, false);
  return r;
}
// Origination puts a live alert on air — require the event code to be typed back
function confirmOriginate(event) {
  const typed = prompt(`This will originate a LIVE ${event} alert.\nType ${event} to confirm:`);
  if (typed === null) return null;
  if (typed.trim().toUpperCase() !== event) { toast('Confirmation did not match — not sent', false); return null; }
  return typed.trim().toUpperCase();
}
async function panelCall(url, successMsg) {
  const r = await post(url);
  toast(r.ok ? successMsg : r.error, r.ok);
}

// ── control actions ────────────────────────────────────────────────────────
async function sendRWT(tone=true) {
  const r = await post('/api/control/rwt', {tone});
  toast(r.ok ? (tone?'RWT sent with tone':'RWT sent without tone') : r.error, r.ok);
}
async function sendEOM() {
  const r = await post('/api/control/eom');
  toast(r.ok ? 'EOM sent' : r.error, r.ok);
}
async function confirmReboot() {
  if (!confirm('Reboot the TFT unit?')) return;
  const r = await post('/api/control/reboot');
  toast(r.ok ? 'Reboot command sent' : r.error, r.ok);
}
async function reconnectCOM3() {
  toast('Reconnecting COM3…', true);
  const r = await post('/api/control/reconnect');
  toast(r.connected ? 'COM3 reconnected' : (r.error || 'COM3 still unavailable'), r.connected);
  checkControlStatus();
  refreshPanelStatus();
}
async function recordAnnouncement() {
  const text = document.getElementById('tts-text').value.trim();
  if (!text) { toast('Enter announcement text first', false); return; }
  toast('Recording TTS announcement…', true);
  const r = await post('/api/control/announce', {text});
  toast(r.ok ? 'Announcement recorded' : r.error, r.ok);
}
function _getCheckedLocs(checksId, manualId) {
  const checked = [...document.querySelectorAll(`#${checksId} input[type=checkbox]:checked`)].map(cb => cb.value);
  if (checked.length) return checked.join(',');
  return document.getElementById(manualId).value.trim();
}
async function loadLocationKeys() {
  const keys = await apiFetch('/api/location_keys');
  if (!keys) return;
  ['orig-locs-checks', 'p-orig-locs-checks'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    const entries = Object.entries(keys).sort((a,b) => parseInt(a[0])-parseInt(b[0]));
    if (!entries.length) {
      el.innerHTML = '<span style="font-size:11px;color:var(--muted);font-family:var(--mono)">No keys configured — run setup wizard</span>';
      return;
    }
    el.innerHTML = entries.map(([k,v]) =>
      `<label style="display:flex;align-items:center;gap:4px;font-size:11px;font-family:var(--mono);color:var(--text);cursor:pointer;background:var(--surface2);padding:3px 8px;border-radius:4px;border:1px solid var(--border)">` +
      `<input type="checkbox" value="${k}" style="accent-color:var(--accent)"> ${k} — ${v.name}</label>`
    ).join('');
  });
}
function onOrigAudioChange() {
  const mode = document.getElementById('orig-audio').value;
  document.getElementById('orig-tts-panel').style.display  = mode === 'tts'  ? 'block' : 'none';
  document.getElementById('orig-voip-panel').style.display = mode === 'voip' ? 'block' : 'none';
}
async function previewAnnouncement() {
  const event = (document.getElementById('orig-event').value || '').trim().toUpperCase();
  const locs  = _getCheckedLocs('orig-locs-checks', 'orig-locs');
  const dur   = document.getElementById('orig-dur').value;
  if (!event || !locs) { toast('Select an event and location key(s) to preview', false); return; }
  const r = await post('/api/decode', {event, locations:locs, duration:dur});
  const el = document.getElementById('orig-preview-text');
  if (r.ok && el) { el.textContent = r.text; el.style.display = 'block'; }
  else toast(r.error || 'Preview failed', false);
}

// ── VoIP announcement recording ──────────────────────────────────────────
let _recCtx = null, _recStream = null, _recProc = null, _recActive = false, _recTimer = null;
function toggleVoipRec() { if (_recActive) stopVoipRec(); else startVoipRec(); }
async function startVoipRec() {
  if (_recActive) return;
  try {
    _recStream = await navigator.mediaDevices.getUserMedia({audio: true});
    _recCtx    = new AudioContext({sampleRate: 44100});
    const src  = _recCtx.createMediaStreamSource(_recStream);
    _recProc   = _recCtx.createScriptProcessor(4096, 1, 1);
    _recProc.onaudioprocess = e => {
      const f32 = e.inputBuffer.getChannelData(0);
      const i16 = new Int16Array(f32.length);
      for (let i = 0; i < f32.length; i++) i16[i] = Math.max(-32768, Math.min(32767, f32[i] * 32767));
      socket.emit('rec_chunk', Array.from(i16));
    };
    src.connect(_recProc);
    _recProc.connect(_recCtx.destination);
    socket.emit('rec_start');
    _recActive = true;
    const btn = document.getElementById('orig-rec-btn');
    const sts = document.getElementById('orig-rec-status');
    if (btn) { btn.style.background = 'var(--danger)'; btn.textContent = '⏹ Stop Recording'; }
    let _recSecs = 0;
    if (sts) { sts.textContent = '● REC 0s'; sts.style.color = 'var(--danger)'; }
    _recTimer = setInterval(() => {
      _recSecs++;
      const s = document.getElementById('orig-rec-status');
      if (s) s.textContent = `● REC ${_recSecs}s`;
    }, 1000);
  } catch(e) { toast('Microphone: ' + e.message, false); }
}
function stopVoipRec() {
  if (!_recActive) return;
  if (_recTimer)  { clearInterval(_recTimer); _recTimer = null; }
  if (_recProc)   { _recProc.disconnect();    _recProc  = null; }
  if (_recCtx)    { _recCtx.close();          _recCtx   = null; }
  if (_recStream) { _recStream.getTracks().forEach(t=>t.stop()); _recStream = null; }
  socket.emit('rec_stop');
  _recActive = false;
  const btn = document.getElementById('orig-rec-btn');
  const sts = document.getElementById('orig-rec-status');
  if (btn) { btn.style.background = ''; btn.textContent = '⏺ Start Recording'; }
  if (sts) { sts.textContent = '✓ Ready — click Originate to send'; sts.style.color = 'var(--success)'; }
}
socket.on('rec_error', d => { stopVoipRec(); toast(d.error, false); });

async function originateAlert() {
  const event = document.getElementById('orig-event').value.trim().toUpperCase();
  const locs  = _getCheckedLocs('orig-locs-checks', 'orig-locs');
  const dur   = document.getElementById('orig-dur').value;
  const mode  = document.getElementById('orig-audio').value;
  if (!event || !locs) { toast('Enter event code and select/enter location keys', false); return; }
  const sts = document.getElementById('orig-rec-status');
  if (mode === 'voip' && (!sts || !sts.textContent.startsWith('✓'))) { toast('Record your announcement first', false); return; }
  const confirm = confirmOriginate(event);
  if (!confirm) return;
  if (mode === 'tts') {
    toast('Generating TTS and originating…', true);
    const r = await postOriginate({event, locations:locs, duration:dur, tts:true, confirm});
    if (r.ok) {
      const el = document.getElementById('orig-preview-text');
      if (el) { el.textContent = r.text; el.style.display = 'block'; }
      toast(`${event} originated with TTS`, true);
    } else { toast(r.error, false); }
  } else if (mode === 'voip') {
    const r = await postOriginate({event, locations:locs, duration:dur, audio:'p', confirm});
    toast(r.ok ? `${event} originated with VoIP recording` : r.error, r.ok);
    if (r.ok && sts) { sts.textContent = 'Idle'; sts.style.color = 'var(--muted)'; }
  } else {
    const r = await postOriginate({event, locations:locs, duration:dur, audio:mode, confirm});
    toast(r.ok ? `${event} originated` : r.error, r.ok);
  }
}
async function panelOriginate() {
  const event = document.getElementById('p-orig-event').value.trim().toUpperCase();
  const locs  = _getCheckedLocs('p-orig-locs-checks', 'p-orig-locs');
  const dur   = document.getElementById('p-orig-dur').value;
  const audio = document.getElementById('p-orig-audio').value;
  if (!event || !locs) { toast('Enter event code and select/enter location keys', false); return; }
  const confirm = confirmOriginate(event);
  if (!confirm) return;
  const r = await postOriginate({event, locations:locs, duration:dur, audio, confirm});
  toast(r.ok ? `${event} originated` : r.error, r.ok);
}
async function checkControlStatus() {
  const d = await apiFetch('/api/control/status');
  if (!d) return;
  const el = document.getElementById('control-status');
  if (el) el.innerHTML = d.connected
    ? '<span style="color:var(--success)">● COM3 connected</span>'
    : `<span style="color:var(--warn)">● COM3 not connected</span>${d.error ? `<br><span style="font-size:10px;color:var(--danger)">${d.error}</span>` : ''}`;
}
async function refreshPanelStatus() {
  const d = await apiFetch('/api/control/status');
  if (!d) return;
  const el = document.getElementById('panel-com3-status');
  if (el) { el.textContent = d.connected ? 'connected' : 'not connected'; el.className = 'status-val ' + (d.connected?'ok':'warn'); }
}
setInterval(checkControlStatus, 30000);

// ── PTT ────────────────────────────────────────────────────────────────────
let _pttActive = false, _pttCtx = null, _pttStream = null, _pttProc = null;

async function startPTT() {
  if (_pttActive) return;
  try {
    _pttStream = await navigator.mediaDevices.getUserMedia({audio:true, video:false});
    _pttCtx    = new (window.AudioContext || window.webkitAudioContext)({sampleRate: 44100});
    const src  = _pttCtx.createMediaStreamSource(_pttStream);
    _pttProc   = _pttCtx.createScriptProcessor(2048, 1, 1);
    _pttProc.onaudioprocess = e => {
      const f32 = e.inputBuffer.getChannelData(0);
      const i16 = new Int16Array(f32.length);
      for (let i = 0; i < f32.length; i++)
        i16[i] = Math.max(-32768, Math.min(32767, f32[i] * 32768 | 0));
      socket.emit('ptt_chunk', Array.from(i16));
    };
    src.connect(_pttProc);
    _pttProc.connect(_pttCtx.destination);
    socket.emit('ptt_start');
    _pttActive = true;
    document.getElementById('ptt-btn').classList.add('active');
    document.getElementById('ptt-status').textContent = '● TRANSMITTING';
    document.getElementById('ptt-status').style.color = 'var(--danger)';
  } catch(e) {
    toast('Microphone: ' + e.message, false);
  }
}
function stopPTT() {
  if (!_pttActive) return;
  pttCleanup();
  socket.emit('ptt_stop');
}
function pttCleanup() {
  if (_pttProc)   { _pttProc.disconnect();  _pttProc = null; }
  if (_pttCtx)    { _pttCtx.close();        _pttCtx  = null; }
  if (_pttStream) { _pttStream.getTracks().forEach(t=>t.stop()); _pttStream = null; }
  _pttActive = false;
  const btn = document.getElementById('ptt-btn');
  const sts = document.getElementById('ptt-status');
  if (btn) btn.classList.remove('active');
  if (sts) { sts.textContent = 'Idle — hold to talk'; sts.style.color = ''; }
}

// ── settings page ─────────────────────────────────────────────────────────
let _settingsLoaded = false;
async function loadSettings() {
  if (_settingsLoaded) return;
  const data = await apiFetch('/api/config');
  if (!data) { toast('Failed to load settings', false); return; }
  const st = data.station       || {};
  const ct = data.control       || {};
  const se = data.serial        || {};
  const no = data.notifications || {};
  const we = data.web           || {};
  const al = data.alerts        || {};
  const lo = data.logging       || {};

  document.getElementById('s-callsign').value   = st.callsign  || '';
  document.getElementById('s-fips').value       = st.fips      || '';
  document.getElementById('s-org').value        = st.org       || 'EAS';
  document.getElementById('s-tz').value         = st.tz_offset !== undefined ? st.tz_offset : '';
  document.getElementById('s-ctrl-port').value  = ct.port || '/dev/tft911-cmd';
  document.getElementById('s-ctrl-baud').value  = ct.baud || '9600';
  document.getElementById('s-ctrl-pin').value   = ct.pin  || '';
  document.getElementById('s-ser-port').value   = se.port || '/dev/ttyUSB0';
  document.getElementById('s-ser-baud').value   = se.baud || '1200';
  document.getElementById('s-ntfy').value       = no.ntfy_topic || '';
  document.getElementById('s-web-host').value   = we.host || '0.0.0.0';
  document.getElementById('s-web-port').value   = we.port || '5000';
  document.getElementById('s-dedupe').value     = al.dedupe_window || '120';
  document.getElementById('s-alerts-dir').value = al.alerts_dir   || '';
  document.getElementById('s-log-dir').value    = lo.log_dir      || '';

  renderLKRows(data.location_keys || {});
  document.getElementById('settings-loading').style.display = 'none';
  document.getElementById('settings-content').style.display = 'block';
  _settingsLoaded = true;
}

async function saveAllSettings() {
  const lkData = {};
  document.querySelectorAll('.lk-row').forEach(row => {
    const key  = row.dataset.key;
    const name = row.querySelector('.lk-name').value.trim();
    const fips = [...row.querySelectorAll('.lk-fips-chip')].map(c => c.dataset.fips).join(',');
    if (name && fips) lkData[key] = `${name} | ${fips}`;
    else if (name)    lkData[key] = name;
  });
  const payload = {
    station:       { callsign: document.getElementById('s-callsign').value, fips: document.getElementById('s-fips').value, org: document.getElementById('s-org').value, tz_offset: document.getElementById('s-tz').value },
    control:       { port: document.getElementById('s-ctrl-port').value, baud: document.getElementById('s-ctrl-baud').value, pin: document.getElementById('s-ctrl-pin').value },
    serial:        { port: document.getElementById('s-ser-port').value, baud: document.getElementById('s-ser-baud').value },
    notifications: { ntfy_topic: document.getElementById('s-ntfy').value },
    web:           { host: document.getElementById('s-web-host').value, port: document.getElementById('s-web-port').value },
    alerts:        { dedupe_window: document.getElementById('s-dedupe').value, alerts_dir: document.getElementById('s-alerts-dir').value },
    logging:       { log_dir: document.getElementById('s-log-dir').value },
    location_keys: lkData,
  };
  const res = await post('/api/config', payload);
  toast(res.ok ? 'Settings saved — restart services to apply' : res.error, res.ok);
  if (res.ok) { _settingsLoaded = false; loadLocationKeys(); }
}

function renderLKRows(lkData) {
  const container = document.getElementById('lk-rows');
  container.innerHTML = '';
  for (let i = 1; i <= 14; i++) {
    const key = String(i);
    const val = lkData[key] || '';
    let name = '', fipsList = [];
    if (val.includes('|')) {
      const [n, f] = val.split('|');
      name     = n.trim();
      fipsList = f.trim().split(',').map(s => s.trim()).filter(Boolean);
    } else {
      name = val.trim();
    }
    container.appendChild(buildLKRow(key, name, fipsList));
  }
}

function buildLKRow(key, name, fipsList) {
  const row = document.createElement('div');
  row.className = 'lk-row';
  row.dataset.key = key;
  const chips = fipsList.map(f =>
    `<span class="lk-fips-chip" data-fips="${f}">${f}<button class="lk-chip-rm" onclick="removeFipsChip(this)">×</button></span>`
  ).join('');
  row.innerHTML =
    `<span class="lk-key-num">${key}</span>` +
    `<input class="lk-name cfg-val" placeholder="Name" value="${name.replace(/"/g,'&quot;')}">` +
    `<div class="lk-fips-area">` +
      `<div class="lk-chips">${chips}</div>` +
      `<div class="lk-search-wrap">` +
        `<input class="lk-search cfg-val" placeholder="Search county to add…" oninput="fipsSearchInput(this)" autocomplete="off">` +
        `<div class="lk-search-results" style="display:none"></div>` +
      `</div>` +
    `</div>`;
  return row;
}

function removeFipsChip(btn) { btn.parentElement.remove(); }

let _fipsTimer = null;
function fipsSearchInput(inp) {
  clearTimeout(_fipsTimer);
  const q   = inp.value.trim();
  const res = inp.nextElementSibling;
  if (q.length < 2) { res.style.display = 'none'; return; }
  _fipsTimer = setTimeout(async () => {
    const items = await apiFetch(`/api/fips/search?q=${encodeURIComponent(q)}`);
    if (!items || !items.length) { res.style.display = 'none'; return; }
    res.innerHTML = items.map(([fips, cname]) =>
      `<div class="lk-search-result" data-fips="${fips}" data-name="${cname.replace(/"/g,'&quot;')}">${cname} <span style="color:var(--muted)">${fips}</span></div>`
    ).join('');
    res.querySelectorAll('.lk-search-result').forEach(el => {
      el.addEventListener('mousedown', ev => {
        ev.preventDefault();
        const row   = el.closest('.lk-row');
        const chips = row.querySelector('.lk-chips');
        const fips  = el.dataset.fips;
        const cn    = el.dataset.name;
        if (!row.querySelector(`.lk-fips-chip[data-fips="${fips}"]`)) {
          const chip = document.createElement('span');
          chip.className = 'lk-fips-chip';
          chip.dataset.fips = fips;
          chip.innerHTML = `${fips} ${cn}<button class="lk-chip-rm" onclick="removeFipsChip(this)">×</button>`;
          chips.appendChild(chip);
          const ni = row.querySelector('.lk-name');
          if (!ni.value) ni.value = cn;
        }
        inp.value = '';
        res.style.display = 'none';
      });
    });
    res.style.display = 'block';
  }, 250);
}
document.addEventListener('click', () => {
  document.querySelectorAll('.lk-search-results').forEach(d => d.style.display = 'none');
});

loadLocationKeys();
