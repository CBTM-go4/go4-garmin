'use strict';
const SVGNS = 'http://www.w3.org/2000/svg';
const $ = (s, r = document) => r.querySelector(s);
const tt = $('#tt');
let state = { days: 90, start: null, end: null };
// local-timezone-safe YYYY-MM-DD (toISOString would shift by the UTC offset)
const isoDay = d => { const z = new Date(d.getTime() - d.getTimezoneOffset() * 60000); return z.toISOString().slice(0, 10); };
// build the /api/overview query from the active preset OR custom range
const rangeQuery = () => state.start ? `start=${state.start}&end=${state.end || ''}` : `days=${state.days}`;
// how many days the current view spans (for labels)
const spanDays = () => { if (!state.start) return state.days; const end = state.end ? new Date(state.end) : new Date(); return Math.max(Math.round((end - new Date(state.start)) / 86400000), 1); };

// ---------- formatting ----------
const fmtPace = s => { if (!s) return '–'; const m = Math.floor(s / 60), x = Math.round(s % 60); return `${m}:${String(x).padStart(2, '0')}/km`; };
const fmtDur = s => { s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60; return h ? `${h}:${String(m).padStart(2, '0')}:${String(x).padStart(2, '0')}` : `${m}:${String(x).padStart(2, '0')}`; };
const km = m => (m / 1000).toFixed(2);
const shortDate = iso => { const d = new Date(iso); return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }); };
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

function svg(tag, attrs = {}) { const e = document.createElementNS(SVGNS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; }
function h(tag, attrs = {}, kids = []) {
  const e = document.createElement(tag);
  for (const k in attrs) { if (k === 'class') e.className = attrs[k]; else if (k === 'html') e.innerHTML = attrs[k]; else e.setAttribute(k, attrs[k]); }
  (Array.isArray(kids) ? kids : [kids]).forEach(c => c && e.append(c.nodeType ? c : document.createTextNode(c)));
  return e;
}

// plain-language explanations, shown on hovering a title
const HELP = {
  'Coach': 'Your headline status plus the single most useful thing to do next — derived from your recent training load, form and intensity balance.',
  'Form / Freshness (TSB)': 'Training Stress Balance = Fitness (CTL) − Fatigue (ATL). Positive means fresh/tapered; deeply negative means you are carrying fatigue (where fitness is built).',
  'VO₂max': 'Estimated maximal oxygen uptake (ml/kg/min) — your aerobic ceiling. Garmin computes it from pace vs heart rate on runs. Higher = fitter. Shown from your most recent run day.',
  'ACWR (load ratio)': 'Acute:Chronic Workload Ratio — this week’s training load vs your rolling 4-week average. 0.8–1.3 is the sweet spot; above 1.5 is the zone most linked to injury.',
  'HRV (last night)': 'Overnight heart-rate variability (ms). Higher and stable vs your baseline signals good recovery; a drop can mean fatigue, illness or stress.',
  'Fitness (CTL)': 'Chronic Training Load — a 42-day rolling average of daily training load. Your longer-term fitness; it rises slowly with consistent training.',
  'Volume': 'Total distance run over the selected time range.',
  'Insights': 'Automated observations from your data — load balance, injury-risk ramps, intensity distribution and aerobic trends.',
  'Training Load & Form': 'Fitness (CTL, 42-day avg), Fatigue (ATL, 7-day avg) and Form (TSB = CTL−ATL) over time. Fatigue above fitness = building; fitness above fatigue = fresh.',
  'Weekly Volume': 'Kilometres run each week. Watch for jumps bigger than ~10% week-on-week, which raise injury risk.',
  'Monthly Volume': 'Kilometres run each month (long ranges are bucketed monthly so the chart stays readable).',
  'Intensity Mix': 'Approximate share of running time in each HR zone (Z1 easy → Z5 max), estimated from each run’s average HR. Aim for roughly 80% easy.',
  'Aerobic Efficiency': 'Easy-run speed per heartbeat (m/s per bpm). Rising over time means your aerobic base is improving — you’re faster at the same effort.',
  'Race Predictions': 'Three estimates side by side. Realistic = Daniels VDOT from your best actual effort (only what you’ve demonstrated). HR-based = your pace-vs-HR profile read at threshold HR — reliable only if your faster runs are your higher-HR ones (heat/hills distort it). Garmin = its VO₂max estimate (tends optimistic). Longer distances assume you’ve done the endurance work.',
  'Sleep': 'Last night’s sleep score and stage breakdown (deep / REM / light), plus your recent nightly duration. Deep and REM drive physical and mental recovery; aim for consistency.',
  'Body Battery': 'Garmin’s daily energy model. “Charged” is how much you recovered (mostly overnight), “drained” is how much activity and stress spent — net positive days leave you fresher.',
  'Fitness Trend': 'Your fitness trajectory over time — VO₂max (Garmin computes it only on some run days, so points are sparse) and predicted race times as a % of their earliest value (lower = faster). Direction over weeks matters more than any single point.',
};

function addHelp(el, text) {
  if (!text) return el;
  el.classList.add('help');
  el.setAttribute('title', text);           // native fallback + accessibility
  el.addEventListener('mouseenter', ev => showTip(`<div>${text}</div>`, ev.clientX, ev.clientY));
  el.addEventListener('mousemove', ev => showTip(`<div>${text}</div>`, ev.clientX, ev.clientY));
  el.addEventListener('mouseleave', hideTip);
  return el;
}

function showTip(html, x, y) { tt.innerHTML = html; tt.style.opacity = 1; const r = tt.getBoundingClientRect(); tt.style.left = Math.min(x + 14, innerWidth - r.width - 8) + 'px'; tt.style.top = Math.max(y - r.height - 10, 8) + 'px'; }
function hideTip() { tt.style.opacity = 0; }

// ---------- line chart (multi-series, hover crosshair) ----------
function lineChart(points, series, opts = {}) {
  const W = 760, H = opts.height || 230, P = { l: 40, r: 54, t: 12, b: 24 };
  const s = svg('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img' });
  if (!points.length) return s;
  const xs = points.map((_, i) => P.l + i * (W - P.l - P.r) / Math.max(1, points.length - 1));
  let vmin = Infinity, vmax = -Infinity;
  points.forEach(p => series.forEach(se => { const v = p.values[se.key]; if (v != null) { vmin = Math.min(vmin, v); vmax = Math.max(vmax, v); } }));
  if (opts.baseZero) vmin = Math.min(0, vmin);
  if (vmin === vmax) vmax += 1;
  const pad = (vmax - vmin) * 0.08; vmax += pad; vmin -= pad;
  const y = v => P.t + (vmax - v) / (vmax - vmin) * (H - P.t - P.b);

  // gridlines + y ticks
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const v = vmin + (vmax - vmin) * i / ticks, yy = y(v);
    s.append(svg('line', { x1: P.l, x2: W - P.r, y1: yy, y2: yy, class: 'grid-line' }));
    s.append(Object.assign(svg('text', { x: P.l - 6, y: yy + 3, 'text-anchor': 'end', class: 'axis-label' }), { textContent: Math.round(v) }));
  }
  if (vmin < 0 && vmax > 0) s.append(svg('line', { x1: P.l, x2: W - P.r, y1: y(0), y2: y(0), class: 'zero-line' }));

  // x labels (sparse)
  const step = Math.ceil(points.length / 6);
  points.forEach((p, i) => { if (i % step === 0 || i === points.length - 1) s.append(Object.assign(svg('text', { x: xs[i], y: H - 6, 'text-anchor': 'middle', class: 'axis-label' }), { textContent: shortDate(p.label) })); });

  // lines
  series.forEach(se => {
    const d = points.map((p, i) => p.values[se.key] == null ? null : `${xs[i]},${y(p.values[se.key])}`).filter(Boolean);
    if (!d.length) return;
    s.append(svg('polyline', { points: d.join(' '), fill: 'none', stroke: css(se.color), 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
  });
  // end labels with collision avoidance (push apart if within 12px)
  if (opts.endLabels !== false) {
    const ends = series.map(se => ({ v: points[points.length - 1].values[se.key], color: se.color }))
      .filter(e => e.v != null).map(e => ({ ...e, y: y(e.v) })).sort((a, b) => a.y - b.y);
    for (let i = 1; i < ends.length; i++) if (ends[i].y - ends[i - 1].y < 12) ends[i].y = ends[i - 1].y + 12;
    ends.forEach(e => s.append(Object.assign(svg('text', { x: W - P.r + 6, y: e.y + 3, class: 'axis-label', fill: css(e.color), 'font-weight': 600 }), { textContent: Math.round(e.v) })));
  }

  // hover layer
  const rule = svg('line', { y1: P.t, y2: H - P.b, class: 'zero-line', opacity: 0 });
  const dots = series.map(se => svg('circle', { r: 4, fill: css(se.color), stroke: css('--surface-1'), 'stroke-width': 2, opacity: 0 }));
  s.append(rule); dots.forEach(d => s.append(d));
  const hit = svg('rect', { x: 0, y: 0, width: W, height: H, fill: 'transparent' });
  s.append(hit);
  hit.addEventListener('mousemove', ev => {
    const rect = s.getBoundingClientRect(); const px = (ev.clientX - rect.left) / rect.width * W;
    let i = Math.round((px - P.l) / ((W - P.l - P.r) / Math.max(1, points.length - 1)));
    i = Math.max(0, Math.min(points.length - 1, i));
    rule.setAttribute('x1', xs[i]); rule.setAttribute('x2', xs[i]); rule.setAttribute('opacity', 1);
    let rows = '';
    series.forEach((se, k) => { const v = points[i].values[se.key]; if (v == null) { dots[k].setAttribute('opacity', 0); return; } dots[k].setAttribute('cx', xs[i]); dots[k].setAttribute('cy', y(v)); dots[k].setAttribute('opacity', 1); rows += `<div class="r"><span style="color:${css(se.color)}">${se.name}</span><b>${Math.round(v)}${se.unit || ''}</b></div>`; });
    showTip(`<div class="d">${new Date(points[i].label).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })}</div>${rows}`, ev.clientX, ev.clientY);
  });
  hit.addEventListener('mouseleave', () => { hideTip(); rule.setAttribute('opacity', 0); dots.forEach(d => d.setAttribute('opacity', 0)); });
  return s;
}

// ---------- bar chart ----------
function barChart(items, opts = {}) {
  const W = 760, H = opts.height || 200, P = { l: 36, r: 12, t: 12, b: 30 };
  const s = svg('svg', { viewBox: `0 0 ${W} ${H}` });
  if (!items.length) return s;
  const vmax = Math.max(...items.map(d => d.value), 1) * 1.1;
  const bw = (W - P.l - P.r) / items.length;
  const y = v => P.t + (1 - v / vmax) * (H - P.t - P.b);
  for (let i = 0; i <= 3; i++) { const v = vmax * i / 3, yy = y(v); s.append(svg('line', { x1: P.l, x2: W - P.r, y1: yy, y2: yy, class: 'grid-line' })); s.append(Object.assign(svg('text', { x: P.l - 6, y: yy + 3, 'text-anchor': 'end', class: 'axis-label' }), { textContent: Math.round(v) })); }
  items.forEach((d, i) => {
    const x = P.l + i * bw + 4, w = bw - 8, yy = y(d.value), hgt = Math.max(0, H - P.b - yy);
    const bar = svg('rect', { x, y: yy, width: w, height: hgt, rx: 4, fill: css(opts.color || '--fitness') });
    bar.addEventListener('mousemove', ev => showTip(`<div class="d">${d.label}</div><div class="r"><span>${opts.metric || 'value'}</span><b>${d.value}${opts.unit || ''}</b></div>${d.sub ? `<div class="r"><span>runs</span><b>${d.sub}</b></div>` : ''}`, ev.clientX, ev.clientY));
    bar.addEventListener('mouseleave', hideTip);
    s.append(bar);
    if (i % Math.ceil(items.length / 6) === 0 || i === items.length - 1)
      s.append(Object.assign(svg('text', { x: x + w / 2, y: H - 8, 'text-anchor': 'middle', class: 'axis-label' }), { textContent: d.short }));
  });
  return s;
}

// ---------- horizontal stacked bar (zones) ----------
function stackedBar(segs) {
  const W = 760, H = 46, P = { l: 0, r: 0 };
  const s = svg('svg', { viewBox: `0 0 ${W} ${H}` });
  const total = segs.reduce((a, b) => a + b.value, 0) || 1;
  let x = 0;
  segs.forEach(seg => {
    const w = seg.value / total * W;
    if (w <= 0) return;
    const rect = svg('rect', { x: x + 1, y: 6, width: Math.max(0, w - 2), height: 26, rx: 4, fill: css(seg.color) });
    rect.addEventListener('mousemove', ev => showTip(`<div class="r"><span>${seg.label}</span><b>${seg.pct}%</b></div><div class="r"><span>time</span><b>${fmtDur(seg.value)}</b></div>`, ev.clientX, ev.clientY));
    rect.addEventListener('mouseleave', hideTip);
    s.append(rect);
    if (w > 34) s.append(Object.assign(svg('text', { x: x + w / 2, y: 44, 'text-anchor': 'middle', class: 'axis-label' }), { textContent: `${seg.pct}%` }));
    x += w;
  });
  return s;
}

function legend(items) {
  const box = h('div', { class: 'legend' });
  items.forEach(it => box.append(h('span', {}, [Object.assign(h('i'), { style: `background:${css(it.color)}` }), it.name])));
  return box;
}

// interactive legend: click an item to toggle its series; keeps ≥1 visible.
function toggleLegend(items, visible, onChange) {
  const box = h('div', { class: 'legend' });
  items.forEach(it => {
    const el = h('span', { class: 'leg-toggle' }, [Object.assign(h('i'), { style: `background:${css(it.color)}` }), it.name]);
    const sync = () => el.classList.toggle('off', !visible.has(it.key));
    sync();
    el.addEventListener('click', () => {
      if (visible.has(it.key)) { if (visible.size > 1) visible.delete(it.key); }
      else visible.add(it.key);
      sync();
      onChange();
    });
    box.append(el);
  });
  return box;
}

// ---------- render ----------
async function load() {
  const app = $('#app');
  app.innerHTML = '<div class="loading">Loading your training…</div>';
  let data;
  try { data = await (await fetch(`/api/overview?${rangeQuery()}`)).json(); }
  catch (e) { app.innerHTML = `<div class="banner"><h2>Backend unreachable</h2><div class="muted">${e}</div></div>`; return; }
  $('#demoBadge').style.display = data.demo ? '' : 'none';
  if (!data.available) return renderNotAuthed(app, data);
  render(app, data);
}

function renderNotAuthed(app, data) {
  app.innerHTML = '';
  app.append(h('div', { class: 'banner' }, [
    h('h2', {}, '🔌 Connect your Garmin account'),
    h('div', { class: 'muted', html: (data.error || 'The Garmin MCP server is not authenticated yet.') }),
    h('p', {}, 'Run this once in a terminal, enter your Garmin email/password and MFA code, then hit Refresh:'),
    h('ol', {}, [
      h('li', { html: 'Authenticate: <code>uvx --from git+https://github.com/Taxuspt/garmin_mcp garmin-mcp-auth</code>' }),
      h('li', { html: 'Restart the backend (it launches the MCP for you).' }),
      h('li', { html: 'Or just explore the UI now with <code>GARMIN_COACH_DEMO=1</code>.' }),
    ]),
  ]));
}

function render(app, d) {
  app.innerHTML = '';
  const s = d.load_series || [], last = s[s.length - 1] || {};
  const c = d.coach || {};
  const ts = d.training_status || {}, hrv = d.hrv || {};

  // hero: headline + recommendation
  const rec = c.recommendation || {};
  app.append(h('div', { class: 'hero' }, [
    h('div', { class: 'card' }, [
      h('h3', {}, 'Coach'),
      h('div', { class: 'headline' }, c.headline || '—'),
      h('div', { class: 'rec' }, [
        h('div', { class: 'icon' }, '🎯'),
        h('div', {}, [h('div', {}, [h('span', { class: 'k' }, `Today: ${rec.session || '—'}. `), rec.detail || '']) ]),
      ]),
    ]),
    h('div', { class: 'card' }, [
      h('h3', {}, 'Form / Freshness (TSB)'),
      h('div', { class: 'tile' }, [
        h('div', { class: 'val' }, [String(last.tsb ?? '–'), h('small', {}, ' pts')]),
        formTag(last.tsb),
        h('div', { class: 'hint' }, 'Fitness (CTL) minus fatigue (ATL). Positive = fresh; deep negative = fatigued.'),
      ]),
    ]),
  ]));

  // stat tiles
  const acwr = d.acwr || {};
  app.append(h('div', { class: 'grid cards', style: 'margin-bottom:16px' }, [
    tile('VO₂max', ts.vo2_max ?? '–', '', ts.vo2_max ? 'ml/kg/min' : ''),
    acwrTile(acwr),
    hrvTile(hrv),
    tile('Fitness (CTL)', last.ctl ?? '–', null, 'load'),
    tile('Volume', d.summary?.total_km ?? '–', null, `km · ${spanDays()}d`),
  ]));

  // training load chart — series toggle via the legend
  const pts = s.map(p => ({ label: p.date, values: { ctl: p.ctl, atl: p.atl, tsb: p.tsb } }));
  const loadSeries = [
    { key: 'ctl', name: 'Fitness (CTL)', short: 'Fitness', color: '--fitness' },
    { key: 'atl', name: 'Fatigue (ATL)', short: 'Fatigue', color: '--fatigue' },
    { key: 'tsb', name: 'Form (TSB)', short: 'Form', color: '--form' },
  ];
  const loadVisible = new Set(loadSeries.map(se => se.key));
  const loadCard = h('div', { class: 'card', style: 'margin-bottom:16px' }, [
    h('div', { class: 'chart-title' }, [h('h3', {}, 'Training Load & Form'), h('span', { class: 'hint' }, 'click legend to toggle')]),
  ]);
  const loadHolder = h('div');
  const drawLoad = () => {
    loadHolder.innerHTML = '';
    const vis = loadSeries.filter(se => loadVisible.has(se.key));
    loadHolder.append(lineChart(pts, vis.map(se => ({ key: se.key, name: se.short, color: se.color })),
      { baseZero: loadVisible.has('tsb'), height: 240 }));
  };
  loadCard.append(toggleLegend(loadSeries, loadVisible, drawLoad));
  loadCard.append(loadHolder);
  drawLoad();
  app.append(loadCard);

  // two-up: volume (weekly or monthly) + intensity mix
  const vol = d.volume || { granularity: 'week', rows: [] };
  const volItems = vol.rows.map(w => ({ label: w.label, short: w.short, value: w.km, sub: w.runs }));
  const volTitle = vol.granularity === 'month' ? 'Monthly Volume' : 'Weekly Volume';
  const volCard = h('div', { class: 'card' }, [h('div', { class: 'chart-title' }, [h('h3', {}, volTitle), h('span', { class: 'hint' }, `km/${vol.granularity}`)])]);
  volCard.append(barChart(volItems, { color: '--fitness', metric: 'distance', unit: ' km', height: 210 }));

  const z = d.zones?.zones || [];
  const zColors = ['--z1', '--z2', '--z3', '--z4', '--z5'];
  const segs = z.map((zz, i) => ({ label: `Zone ${zz.zone}`, value: zz.seconds, pct: zz.pct, color: zColors[i] }));
  const zoneCard = h('div', { class: 'card' }, [
    h('div', { class: 'chart-title' }, [h('h3', {}, 'Intensity Mix'), h('span', { class: 'hint' }, 'estimated from avg HR')]),
    legend([1, 2, 3, 4, 5].map(n => ({ name: `Z${n}`, color: zColors[n - 1] }))),
  ]);
  zoneCard.append(stackedBar(segs));
  zoneCard.append(h('div', { class: 'hint', style: 'margin-top:8px' }, `Easy (Z1–2) ≈ ${d.zones?.easy_pct ?? '–'}% · Hard (Z4–5) ≈ ${d.zones?.hard_pct ?? '–'}%`));
  app.append(h('div', { class: 'grid two', style: 'margin-bottom:16px' }, [volCard, zoneCard]));

  // recovery (sleep / body battery)
  const recov = recoverySection(d);
  if (recov) app.append(recov);

  // fitness trend (VO₂max + predicted times over time)
  const ftc = fitnessTrend(d.fitness_trend);
  if (ftc) app.append(ftc);

  // aerobic efficiency
  const eff = d.efficiency || [];
  if (eff.length > 3) {
    const ec = h('div', { class: 'card', style: 'margin-bottom:16px' }, [
      h('div', { class: 'chart-title' }, [h('h3', {}, 'Aerobic Efficiency'), h('span', { class: 'hint' }, 'easy-run speed per heartbeat — up = fitter')]),
    ]);
    ec.append(lineChart(eff.map(e => ({ label: e.date, values: { eff: e.efficiency } })), [{ key: 'eff', name: 'Efficiency', color: '--efficiency' }], { height: 190, endLabels: false }));
    app.append(ec);
  }

  // race predictions (just above insights)
  const rp = racePredictions(d.my_predictions, d.potential_predictions, d.race_predictions);
  if (rp) app.append(rp);

  // coach insights (below the charts)
  if (c.insights?.length) {
    const box = h('div', { class: 'card', style: 'margin-bottom:16px' }, [h('h3', {}, 'Insights')]);
    const ins = h('div', { class: 'insights' });
    c.insights.forEach(i => ins.append(h('div', { class: 'insight ' + i.severity }, [
      h('div', { class: 'ic' }, iconFor(i.severity)),
      h('div', {}, [h('div', { class: 't' }, i.title), h('div', { class: 'x' }, i.text)]),
    ])));
    box.append(ins); app.append(box);
  }

  // runs table
  app.append(runsTable(d.runs || [], d.hr_max, d.hr_rest));

  // attach hover explanations to every title we have help text for
  app.querySelectorAll('h3').forEach(el => {
    const t = el.textContent.trim();
    if (HELP[t]) addHelp(el, HELP[t]);
  });
}

const trimpJS = (hr, dur, hrmax, hrrest) => {
  if (!hr || !dur) return null;
  let hrr = (hr - hrrest) / Math.max(1, hrmax - hrrest);
  hrr = Math.max(0, Math.min(1, hrr));
  return Math.round(dur / 60 * hrr * 0.64 * Math.exp(1.92 * hrr));
};

function runsTable(runs, hrMax = 190, hrRest = 48) {
  const card = h('div', { class: 'card' }, [h('h3', {}, `Recent Runs (${runs.length})`)]);
  const t = h('table');
  const heads = ['Run', 'Date', 'Dist', 'Pace', 'Avg HR', 'Elev', 'Load', 'Drift', 'Temp'];
  const headRow = h('tr', {}, heads.map(x => h('th', {}, x)));
  const driftTh = headRow.children[7], tempTh = headRow.children[8];
  driftTh.setAttribute('title', 'Aerobic decoupling — Pa:HR drift, 1st vs 2nd half. Green <5% coupled · amber 5–8% · red >8% decoupled. Grey = too short to measure.');
  driftTh.classList.add('help');
  tempTh.setAttribute('title', 'Air temperature during the run, from Garmin weather (converted to °C).');
  tempTh.classList.add('help');
  t.append(h('thead', {}, headRow));
  const tb = h('tbody');
  const shown = runs.slice(0, 40);
  const toFill = [];
  shown.forEach(r => {
    const pace = r.duration_seconds && r.distance_meters ? r.duration_seconds / (r.distance_meters / 1000) : 0;
    const race = r.event_type === 'race';
    const load = trimpJS(r.avg_hr_bpm, r.duration_seconds, hrMax, hrRest);
    const drift = h('td', {}, h('span', { class: 'muted', style: 'opacity:.5' }, '…'));
    const temp = h('td', {}, h('span', { class: 'muted', style: 'opacity:.5' }, '…'));
    const tr = h('tr', {}, [
      h('td', {}, [h('span', {}, r.name || 'Run'), race ? h('span', { class: 'pill race', style: 'margin-left:6px' }, 'race') : null]),
      h('td', {}, shortDate(r.start_time)),
      h('td', {}, `${km(r.distance_meters || 0)} km`),
      h('td', {}, fmtPace(pace)),
      h('td', {}, r.avg_hr_bpm ? `${r.avg_hr_bpm}` : '–'),
      h('td', {}, r.elevation_gain_meters != null ? `${Math.round(r.elevation_gain_meters)} m↑` : '–'),
      h('td', {}, load != null ? `${load}` : '–'),
      drift, temp,
    ]);
    tr.addEventListener('click', () => openRun(r.id, r.name));
    tb.append(tr);
    if (r.id != null) toFill.push({ id: r.id, drift, temp });
  });
  t.append(tb); card.append(t);
  fillRunMeta(toFill);   // lazily fill decoupling + temperature
  return card;
}

function setDrift(el, pct) {
  el.innerHTML = '';
  if (pct == null) { el.append(h('span', { class: 'muted' }, '–')); return; }
  const cls = pct < 5 ? 'dg' : pct < 8 ? 'da' : 'dr';
  const word = pct < 5 ? 'coupled' : pct < 8 ? 'mild drift' : 'decoupled';
  el.append(h('span', { class: 'pill ' + cls, title: `Aerobic decoupling ${pct}% — ${word}` }, `${pct}%`));
}

function setTemp(el, c) {
  el.innerHTML = '';
  if (c == null) { el.append(h('span', { class: 'muted' }, '–')); return; }
  el.append(document.createTextNode(`${c}°C`));
}

// Fetch decoupling + temperature for the listed runs with limited concurrency (gentle
// on Garmin), filling each row as its result lands. Cached hard server-side.
async function fillRunMeta(items) {
  let i = 0;
  const worker = async () => {
    while (i < items.length) {
      const it = items[i++];
      try {
        const d = await (await fetch(`/api/run/${it.id}/summary`)).json();
        setDrift(it.drift, d.decoupling_pct);
        setTemp(it.temp, d.temp_c);
      } catch { setDrift(it.drift, null); setTemp(it.temp, null); }
    }
  };
  await Promise.all(Array.from({ length: 3 }, worker));
}

// ---------- run detail modal ----------
async function openRun(id, name) {
  const bg = $('#modalBg'), m = $('#modal');
  m.innerHTML = `<button class="close" id="mClose">×</button><h2>${name || 'Run'}</h2><div class="loading">Loading splits…</div>`;
  bg.classList.add('open');
  $('#mClose').onclick = () => bg.classList.remove('open');
  bg.onclick = e => { if (e.target === bg) bg.classList.remove('open'); };
  let d;
  try { d = await (await fetch(`/api/run/${id}`)).json(); } catch { m.querySelector('.loading').textContent = 'Failed to load.'; return; }
  const a = d.activity || {}, dc = d.decoupling, zd = d.zone_distribution, w = d.weather;
  m.innerHTML = `<button class="close" id="mClose">×</button><h2>${a.name || name}</h2><div class="muted">${a.start_time_local ? new Date(a.start_time_local).toLocaleString() : ''}</div>`;
  $('#mClose').onclick = () => bg.classList.remove('open');
  const pace = a.duration_seconds && a.distance_meters ? a.duration_seconds / (a.distance_meters / 1000) : 0;
  const kv = h('div', { class: 'kv' }, [
    kb('Distance', `${km(a.distance_meters || 0)} km`), kb('Time', fmtDur(a.duration_seconds || 0)),
    kb('Avg pace', fmtPace(pace)), kb('Avg HR', a.avg_hr_bpm ? a.avg_hr_bpm + ' bpm' : '–'),
    kb('Cadence', a.avg_cadence ? Math.round(a.avg_cadence) + ' spm' : '–'),
    kb('Training load', a.training_load != null ? Math.round(a.training_load) : '–'),
    kb('Training effect', a.training_effect != null ? Number(a.training_effect).toFixed(1) : '–'),
    kb('RPE', a.workout_rpe != null ? a.workout_rpe + '/10' : '–'),
    w && w.temp_c != null ? kb('Temp', `${w.temp_c}°C`) : null,
    w && w.feels_c != null && w.feels_c !== w.temp_c ? kb('Feels like', `${w.feels_c}°C`) : null,
    w && w.humidity != null ? kb('Humidity', `${w.humidity}%`) : null,
  ].filter(Boolean));
  m.append(kv);

  if (dc) {
    m.append(h('div', { class: 'section-title' }, 'Aerobic decoupling'));
    const pct = dc.decoupling_pct;
    const sev = pct < 5 ? 'good' : pct < 8 ? 'warning' : 'serious';
    const eff = r => r != null ? (r * 1000).toFixed(1) : '–';   // speed-per-beat, scaled to read nicely

    // the numbers behind the percentage
    m.append(h('div', { class: 'kv', style: 'margin-bottom:10px' }, [
      kb('1st-half efficiency', eff(dc.first_half_ratio)),
      kb('2nd-half efficiency', eff(dc.second_half_ratio)),
      kb('Drift', `${pct}%`),
    ]));

    const headline = sev === 'good' ? 'Aerobically coupled — your engine held steady'
      : sev === 'warning' ? 'Mild drift — normal on long or warm runs'
      : 'Decoupled — effort climbed to hold the pace';
    const detail = sev === 'good'
      ? 'You got essentially the same speed per heartbeat in the second half as the first. That’s the signature of a well-paced, well-fuelled aerobic effort — your cardiovascular system never had to work progressively harder just to hold the pace.'
      : sev === 'warning'
      ? 'Your speed-per-heartbeat slipped a little over the run — you spent a few extra beats to hold pace late on. Common and usually harmless on longer efforts or in the heat, but worth noting if it turns up on a shorter, cooler run.'
      : 'In the second half you needed noticeably more heartbeats to hold the same pace (or slowed at the same heart rate). That “cardiac drift” typically points to fatigue, dehydration, heat, under-fuelling, or simply starting too fast.';

    m.append(h('div', { class: 'insight ' + (sev === 'good' ? 'good' : 'caution') }, [
      h('div', { class: 'ic' }, sev === 'good' ? '✅' : '⚠️'),
      h('div', {}, [
        h('div', { class: 't' }, `${pct}% drift — ${headline}`),
        h('div', { class: 'x' }, detail),
        h('div', { class: 'x', style: 'margin-top:8px; color:var(--muted)' },
          'Efficiency here is speed ÷ heart rate (metres per second per beat, ×1000) — higher is better, and the drift is how much it fell from the first half to the second. For the drift itself, lower is better: 0% means perfectly even effort, while a big positive number means you were paying more and more heartbeats just to hold the pace. Rule of thumb: under 5% is coupled, 5–8% mild, over 8% decoupled. It’s most meaningful on steady runs — on intervals or a planned negative split a high figure is expected, not a warning.'),
      ]),
    ]));
  }
  if (zd?.zones?.length) {
    m.append(h('div', { class: 'section-title' }, 'Time in heart-rate zones'));
    const zColors = ['--z1', '--z2', '--z3', '--z4', '--z5'];
    m.append(stackedBar(zd.zones.map((zz, i) => ({ label: `Zone ${zz.zone}`, value: zz.seconds, pct: zz.pct, color: zColors[Math.min(4, (zz.zone || 1) - 1)] }))));
  }
  if (d.splits?.length) {
    m.append(h('div', { class: 'section-title' }, 'Splits'));
    const items = d.splits.filter(l => l.avg_speed_mps).map((l, i) => ({ label: `Lap ${l.lap_number || i + 1}`, short: `${l.lap_number || i + 1}`, value: Math.round((l.distance_meters || 1000) / l.avg_speed_mps), sub: l.avg_hr_bpm }));
    // show pace bars (lower is faster) — invert by plotting speed
    m.append(barChart(d.splits.filter(l => l.avg_speed_mps).map((l, i) => ({ label: `Lap ${l.lap_number || i + 1} · ${fmtPace((l.distance_meters || 1000) / l.avg_speed_mps)} · ${l.avg_hr_bpm || '–'} bpm`, short: `${l.lap_number || i + 1}`, value: +(l.avg_speed_mps).toFixed(2), sub: l.avg_hr_bpm })), { color: '--form', metric: 'speed', unit: ' m/s', height: 160 }));
  }
}
const kb = (l, v) => h('div', { class: 'b' }, [h('div', { class: 'l' }, l), h('div', { class: 'v' }, String(v))]);

// ---------- small helpers ----------
function tile(label, val, tag, unit) {
  return h('div', { class: 'card tile' }, [
    h('h3', {}, label),
    h('div', { class: 'val' }, [String(val), unit ? h('small', {}, ' ' + unit) : null]),
    tag || null,
  ]);
}
function acwrTile(a) {
  const map = { optimal: 'good', caution: 'warning', 'high-risk': 'critical', detraining: 'warning', 'no-data': 'warning' };
  return h('div', { class: 'card tile' }, [
    h('h3', {}, 'ACWR (load ratio)'),
    h('div', { class: 'val' }, String(a.ratio ?? '–')),
    a.zone ? tagFor(map[a.zone], a.zone.replace('-', ' ')) : null,
  ]);
}
function hrvTile(hrv) {
  const v = hrv.last_night_avg ?? '–';
  const st = hrv.status;
  const tag = st ? tagFor(st.toUpperCase() === 'BALANCED' ? 'good' : 'warning', st.toLowerCase()) : null;
  const t = h('div', { class: 'card tile' }, [
    h('h3', {}, 'HRV (last night)'),
    h('div', { class: 'val' }, [String(v), hrv.last_night_avg ? h('small', {}, ' ms') : null]),
    tag,
  ]);
  if (hrv.baseline_low && hrv.baseline_high)
    t.append(h('div', { class: 'hint' }, `baseline ${hrv.baseline_low}–${hrv.baseline_high} ms`));
  return t;
}

// ---------- race predictions (side-by-side: realistic / potential / garmin) ----------
const RACE_LABELS = { '5K': '5K', '10K': '10K', 'half_marathon': 'Half Marathon', 'marathon': 'Marathon' };
const RACE_ORDER = ['5K', '10K', 'half_marathon', 'marathon'];
function racePredictions(myp, potential, garmin) {
  const cols = [
    { label: 'Realistic', src: myp, tip: 'From your best actual sustained effort (Daniels VDOT). Only credits what you’ve demonstrated.' },
    { label: 'HR-based', src: potential, tip: 'From your pace-vs-HR profile, read at threshold HR (~88% max). Reflects your aerobic signal — but is only reliable if your faster runs are your higher-HR ones; heat/hills can distort it.' },
    { label: 'Garmin', src: garmin, tip: 'Garmin’s own estimate from its VO₂max — tends optimistic for runners who don’t do speedwork.', muted: true },
  ].filter(c => c.src?.predictions);
  if (!cols.length) return null;

  const t = h('table');
  const head = h('tr', {}, [h('th', {}, 'Distance'), ...cols.map(c => {
    const th = h('th', {}, c.label); th.setAttribute('title', c.tip); th.classList.add('help'); return th;
  })]);
  t.append(h('thead', {}, head));
  const tb = h('tbody');
  RACE_ORDER.forEach(k => {
    if (!cols.some(c => c.src.predictions[k]?.time)) return;
    tb.append(h('tr', {}, [
      h('td', {}, RACE_LABELS[k]),
      ...cols.map(c => {
        const p = c.src.predictions[k];
        const td = h('td', { class: c.muted ? 'muted' : '' }, (p?.time || '–') + (p?.extrapolated ? ' *' : ''));
        if (p?.extrapolated) td.setAttribute('title', 'Extrapolated beyond your longest run — treat with caution');
        return td;
      }),
    ]));
  });
  t.append(tb);

  const card = h('div', { class: 'card', style: 'margin-bottom:16px' }, [
    h('div', { class: 'chart-title' }, [h('h3', {}, 'Race Predictions'), h('span', { class: 'hint' }, 'modelled from your runs')]),
  ]);
  card.append(t);

  const notes = [];
  if (myp?.predictions) {
    const anyExt = RACE_ORDER.some(k => myp.predictions[k]?.extrapolated);
    notes.push(`Realistic takes your best actual run at each distance and Riegel-adjusts it${anyExt ? `; * = extrapolated beyond your longest run (${myp.longest_km} km), treat with caution` : ''}.`);
  }
  if (potential) notes.push(`HR-based reads pace-vs-HR at threshold ${potential.threshold_hr} bpm ≈ ${fmtPace(potential.threshold_pace_s_per_km)}; only reliable if your faster runs are your higher-HR ones.`);
  if (notes.length) card.append(h('div', { class: 'hint', style: 'margin-top:10px' }, notes.join(' ')));
  return card;
}

// ---------- fitness trend (VO₂max + predicted times over time) ----------
function fitnessTrend(ft) {
  if (!ft) return null;
  const vo2 = ft.vo2max || [], preds = ft.predictions || [];
  const haveVo2 = vo2.length >= 2, havePreds = preds.length >= 2;
  if (!haveVo2 && !havePreds) {
    // Nothing to plot yet — only surface a note if a backfill is populating history.
    if (!ft.backfilling) return null;
    return h('div', { class: 'card', style: 'margin-bottom:16px' }, [
      h('h3', {}, 'Fitness Trend'),
      h('div', { class: 'hint', style: 'margin-top:6px' }, 'Building your VO₂max history from Garmin… hit ↻ Refresh in a moment.'),
    ]);
  }
  const card = h('div', { class: 'card', style: 'margin-bottom:16px' }, [
    h('div', { class: 'chart-title' }, [h('h3', {}, 'Fitness Trend'),
      h('span', { class: 'hint' }, 'direction over weeks > any single day')]),
  ]);

  if (haveVo2) {
    card.append(legend([{ name: 'VO₂max', color: '--efficiency' }]));
    card.append(lineChart(vo2.map(p => ({ label: p.date, values: { v: p.value } })),
      [{ key: 'v', name: 'VO₂max', color: '--efficiency' }], { height: 180, endLabels: true }));
  }

  if (havePreds) {
    // Different distances live on wildly different scales, so normalize each to a
    // % of its earliest value. Lower = faster than baseline.
    const dists = [
      { key: 'p5k', name: '5K', color: '--fitness' },
      { key: 'p10k', name: '10K', color: '--form' },
      { key: 'phm', name: 'Half', color: '--fatigue' },
      { key: 'pm', name: 'Marathon', color: '--efficiency' },
    ].filter(dd => preds[0][dd.key] != null);
    const base = {};
    dists.forEach(dd => base[dd.key] = preds[0][dd.key]);
    const pts = preds.map(p => ({
      label: p.date,
      values: Object.fromEntries(dists.map(dd =>
        [dd.key, p[dd.key] != null && base[dd.key] ? Math.round(1000 * p[dd.key] / base[dd.key]) / 10 : null])),
    }));
    card.append(h('div', { class: 'hint', style: 'margin-top:14px' }, 'Predicted race times · % of earliest (lower = faster)'));
    card.append(legend(dists.map(dd => ({ name: dd.name, color: dd.color }))));
    card.append(lineChart(pts, dists.map(dd => ({ ...dd, unit: '%' })), { height: 170, endLabels: true }));
  }
  return card;
}

// ---------- recovery: sleep + body battery ----------
function recoverySection(d) {
  const sleep = d.sleep_last, trend = d.sleep_trend || [], bb = d.body_battery || [];
  if (!sleep && !bb.length) return null;
  const cards = [];

  if (sleep) {
    const card = h('div', { class: 'card' }, [
      h('div', { class: 'chart-title' }, [h('h3', {}, 'Sleep'),
        h('span', { class: 'hint' }, sleep.overnight_hrv ? `overnight HRV ${sleep.overnight_hrv} ms` : '')]),
      h('div', { class: 'tile' }, [
        h('div', { class: 'val' }, [String(sleep.hours ?? '–'), h('small', {}, ' h'),
          sleep.score != null ? h('small', {}, `  ·  score ${sleep.score}`) : null]),
        sleep.qualifier ? tagFor(sleepSev(sleep.score), sleep.qualifier) : null,
      ]),
    ]);
    const stageColors = { deep: '--z5', rem: '--efficiency', light: '--z2', awake: '--muted' };
    const segs = [
      { label: 'Deep', value: sleep.deep_s, color: stageColors.deep },
      { label: 'REM', value: sleep.rem_s, color: stageColors.rem },
      { label: 'Light', value: sleep.light_s, color: stageColors.light },
      { label: 'Awake', value: sleep.awake_s, color: stageColors.awake },
    ].filter(s => s.value > 0);
    const tot = segs.reduce((a, b) => a + b.value, 0) || 1;
    segs.forEach(s => s.pct = Math.round(100 * s.value / tot));
    card.append(legend(segs.map(s => ({ name: s.label, color: s.color }))));
    card.append(stackedBar(segs));
    if (trend.length > 2) {
      card.append(h('div', { class: 'hint', style: 'margin-top:10px' }, `Nightly hours · last ${trend.length} days`));
      card.append(lineChart(trend.map(t => ({ label: t.date, values: { h: t.hours } })),
        [{ key: 'h', name: 'Sleep', color: '--form', unit: 'h' }], { height: 150, endLabels: false }));
    }
    cards.push(card);
  }

  if (bb.length) {
    const latest = bb[bb.length - 1];
    const lvlSev = { HIGH: 'good', MODERATE: 'warning', LOW: 'serious' }[latest.level] || 'info';
    const card = h('div', { class: 'card' }, [
      h('div', { class: 'chart-title' }, [h('h3', {}, 'Body Battery')]),
      h('div', { class: 'tile' }, [
        h('div', { class: 'val', style: 'font-size:22px' }, [latest.level || '–',
          h('small', {}, `  +${latest.charged ?? '–'} / −${latest.drained ?? '–'}`)]),
        tagFor(lvlSev, 'today'),
      ]),
      legend([{ name: 'Charged', color: '--good' }, { name: 'Drained', color: '--fatigue' }]),
    ]);
    card.append(lineChart(bb.map(r => ({ label: r.date, values: { c: r.charged, dr: r.drained } })),
      [{ key: 'c', name: 'Charged', color: '--good' }, { key: 'dr', name: 'Drained', color: '--fatigue' }],
      { height: 180 }));
    cards.push(card);
  }

  return h('div', { class: 'grid two', style: 'margin-bottom:16px' }, cards);
}
function sleepSev(score) { if (score == null) return 'info'; if (score >= 80) return 'good'; if (score >= 60) return 'warning'; return 'serious'; }

function formTag(tsb) { if (tsb == null) return null; if (tsb > 8) return tagFor('good', 'fresh'); if (tsb < -20) return tagFor('serious', 'fatigued'); return tagFor('warning', 'building'); }
function tagFor(sev, label) { return h('div', { class: 'tag ' + sev }, [iconFor(sev), label]); }
function iconFor(sev) { return ({ good: '✅', warning: '⚠️', caution: '⚠️', serious: '🔺', critical: '⛔', info: 'ℹ️' })[sev] || 'ℹ️'; }

// ---------- wiring ----------
// Preset buttons: reset to a days-from-today window and mirror it in the date inputs.
$('#rangeSeg').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  [...e.currentTarget.children].forEach(x => x.classList.remove('active')); b.classList.add('active');
  $('#applyRange').classList.remove('active');
  state.days = +b.dataset.days; state.start = null; state.end = null;
  const end = new Date(), start = new Date(); start.setDate(end.getDate() - state.days);
  $('#startDate').value = isoDay(start); $('#endDate').value = isoDay(end);
  load();
});

// Custom range: apply the two date inputs and deselect the presets.
function applyRange() {
  const s = $('#startDate').value, en = $('#endDate').value;
  if (!s) { $('#startDate').focus(); return; }
  if (en && en < s) { $('#endDate').focus(); return; }
  state.start = s; state.end = en || null;
  [...$('#rangeSeg').children].forEach(x => x.classList.remove('active'));
  $('#applyRange').classList.add('active');
  load();
}
$('#applyRange').addEventListener('click', applyRange);
['#startDate', '#endDate'].forEach(sel =>
  $(sel).addEventListener('keydown', e => { if (e.key === 'Enter') applyRange(); }));

$('#refreshBtn').addEventListener('click', async () => { await fetch('/api/refresh', { method: 'POST' }); load(); });

// Seed the date inputs to the default preset window and cap them at today.
(function initRange() {
  const today = isoDay(new Date());
  const start = new Date(); start.setDate(start.getDate() - state.days);
  for (const sel of ['#startDate', '#endDate']) $(sel).max = today;
  $('#startDate').value = isoDay(start); $('#endDate').value = today;
})();
load();
