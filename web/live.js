'use strict';

const $ = (s, r = document) => r.querySelector(s);
const api = p => `/api/live/${p}`;
const state = {
  session: null,
  hr: null,
  gpsWatch: null,
  btDevice: null,
  btChar: null,
  lastFix: null,
  startTs: null,
  distanceM: 0,
  fixes: [],
  statusTimer: null,
  sampleTimer: null,
};

function isoTime(ms) {
  const d = new Date(ms);
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function fmtPace(secondsPerKm) {
  if (!secondsPerKm || !isFinite(secondsPerKm) || secondsPerKm <= 0) return '–';
  const m = Math.floor(secondsPerKm / 60);
  const s = Math.round(secondsPerKm % 60);
  return `${m}:${String(s).padStart(2, '0')}/km`;
}

function fmtDist(meters) {
  if (!meters && meters !== 0) return '–';
  return `${(meters / 1000).toFixed(2)} km`;
}

function fmtElapsed(ms) {
  if (!ms && ms !== 0) return '–';
  const sec = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
}

function haversine(a, b) {
  const R = 6371000;
  const toRad = d => d * Math.PI / 180;
  const dLat = toRad(b.lat - a.lat);
  const dLon = toRad(b.lon - a.lon);
  const lat1 = toRad(a.lat);
  const lat2 = toRad(b.lat);
  const x = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(x));
}

function paceFromFixes() {
  if (state.fixes.length < 2) return null;
  const first = state.fixes[0];
  const last = state.fixes[state.fixes.length - 1];
  const dist = state.fixes.slice(1).reduce((acc, fix, idx) => acc + haversine(state.fixes[idx], fix), 0);
  const sec = (last.ts - first.ts) / 1000;
  if (dist < 10 || sec <= 0) return null;
  return sec / (dist / 1000);
}

async function post(path, body = {}) {
  const res = await fetch(api(path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

async function refreshStatus() {
  const res = await fetch(api('current'));
  const data = await res.json();
  renderSnapshot(data);
}

function renderSnapshot(data) {
  if (!data) return;
  state.session = data.session || state.session;
  if (data.latest) renderLatest(data.latest);
  if (data.status) renderStatus(data.status);
  if (data.session) renderSession(data.session);
  renderAlerts(data.alerts || []);
}

function renderLatest(latest) {
  $('#hrVal').textContent = latest.hr == null ? '–' : `${latest.hr} bpm`;
  $('#paceVal').textContent = latest.pace || '–';
  $('#distVal').textContent = latest.distance || '–';
  $('#timeVal').textContent = latest.elapsed || '–';
}

function renderStatus(status) {
  const card = $('#statusCard');
  card.className = `card status ${status.level || 'info'}`;
  $('#statusHead').textContent = status.headline || 'Live run';
  $('#statusHint').textContent = status.level ? status.level.toUpperCase() : 'INFO';
  $('#statusBody').textContent = status.message || '';
}

function renderSession(session) {
  $('#sessionId').textContent = `${session.label || 'Run'} · ${session.session_id || 'session'}`;
  $('#sessionMeta').textContent = `Started ${session.started_at ? new Date(session.started_at).toLocaleString() : 'now'} · target HR ${session.target_hr_min} to ${session.target_hr_max} bpm · cap ${session.hard_hr_cap} bpm · samples ${session.sample_count}`;
}

function renderAlerts(alerts) {
  const box = $('#alerts');
  if (!alerts.length) {
    box.textContent = 'No alerts yet.';
    return;
  }
  box.innerHTML = alerts.map(a => `
    <div style="padding:10px 0; border-bottom:1px solid var(--line)">
      <div><strong>${a.headline || 'Alert'}</strong> <span class="pill">${a.level || 'info'}</span></div>
      <div>${a.message || ''}</div>
      <div class="hint">${a.ts ? isoTime(Date.parse(a.ts)) : ''}</div>
    </div>`).join('');
}

async function startRun() {
  const data = await post('start', {
    label: 'Long run',
    target_hr_min: 131,
    target_hr_max: 145,
    hard_hr_cap: 145,
  });
  renderSnapshot(data);
  startGps();
  if (!state.sampleTimer) {
    state.sampleTimer = setInterval(() => sendSample(true), 5000);
  }
}

async function stopRun() {
  try {
    await post('stop', {});
  } catch (e) {
    console.error(e);
  }
  stopGps();
  if (state.sampleTimer) { clearInterval(state.sampleTimer); state.sampleTimer = null; }
  await refreshStatus();
}

async function connectHr() {
  if (!navigator.bluetooth) {
    alert('Web Bluetooth is not available in this browser. Use Android Chrome, or pair a BLE chest strap in a native app later.');
    return;
  }
  try {
    const device = await navigator.bluetooth.requestDevice({
      filters: [{ services: ['heart_rate'] }],
      optionalServices: ['battery_service'],
    });
    const server = await device.gatt.connect();
    const service = await server.getPrimaryService('heart_rate');
    const ch = await service.getCharacteristic('heart_rate_measurement');
    await ch.startNotifications();
    ch.addEventListener('characteristicvaluechanged', ev => {
      const dv = ev.target.value;
      const flags = dv.getUint8(0);
      const hr = flags & 0x1 ? dv.getUint16(1, true) : dv.getUint8(1);
      state.hr = hr;
      $('#hrVal').textContent = `${hr} bpm`;
      sendSample();
    });
    state.btDevice = device;
    state.btChar = ch;
    alert('Heart rate sensor connected.');
  } catch (e) {
    alert(`Could not connect to heart rate sensor: ${e.message || e}`);
  }
}

function startGps() {
  if (!navigator.geolocation) {
    alert('Geolocation is not available in this browser.');
    return;
  }
  if (state.gpsWatch != null) return;
  state.startTs = Date.now();
  state.distanceM = 0;
  state.fixes = [];
  state.lastFix = null;
  state.gpsWatch = navigator.geolocation.watchPosition(pos => {
    const now = Date.now();
    const fix = {
      lat: pos.coords.latitude,
      lon: pos.coords.longitude,
      ts: now,
    };
    if (state.lastFix) {
      const step = haversine(state.lastFix, fix);
      if (isFinite(step) && step > 0 && step < 100) state.distanceM += step;
    }
    state.lastFix = fix;
    state.fixes.push(fix);
    if (state.fixes.length > 10) state.fixes.shift();
    $('#distVal').textContent = fmtDist(state.distanceM);
    $('#timeVal').textContent = fmtElapsed(now - state.startTs);
    const pace = paceFromFixes();
    if (pace) $('#paceVal').textContent = fmtPace(pace);
    sendSample();
  }, err => {
    console.error(err);
    $('#statusBody').textContent = `GPS error: ${err.message}`;
  }, { enableHighAccuracy: true, maximumAge: 5000, timeout: 15000 });
}

function stopGps() {
  if (state.gpsWatch != null && navigator.geolocation) {
    navigator.geolocation.clearWatch(state.gpsWatch);
    state.gpsWatch = null;
  }
}

async function sendSample(force = false) {
  if (!state.session) return;
  const paceText = $('#paceVal').textContent;
  const pace = paceText && paceText !== '–' ? paceText : null;
  const paceSeconds = paceFromFixes();
  const payload = {
    hr: state.hr,
    pace_s_per_km: paceSeconds,
    distance_km: state.distanceM / 1000,
    elapsed_s: state.startTs ? (Date.now() - state.startTs) / 1000 : null,
    lat: state.lastFix ? state.lastFix.lat : null,
    lon: state.lastFix ? state.lastFix.lon : null,
  };
  if (!force && payload.hr == null && payload.pace_s_per_km == null) return;
  try {
    const data = await post('update', payload);
    renderSnapshot(data);
  } catch (e) {
    console.error(e);
    $('#statusBody').textContent = `Bridge error: ${e.message || e}`;
  }
}

$('#startBtn').addEventListener('click', startRun);
$('#stopBtn').addEventListener('click', stopRun);
$('#hrBtn').addEventListener('click', connectHr);
$('#refreshBtn').addEventListener('click', refreshStatus);

refreshStatus();
setInterval(refreshStatus, 10000);
