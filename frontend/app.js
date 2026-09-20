import { SolarScene } from './scene.js?v=51';

const $ = id => document.getElementById(id);
const hourInput = $('hour-input'), dateInput = $('date-input');
let plant, view, mode = 'synthetic', observations = [], sequence = 0, requestController;
let playing = false, playTimer, weatherSequence = 0, highDetail = true, debounce;
let snapshot = null, dayData = null, daySequence = 0, selected = null, faults = {}, faultKinds = [];

// Kept short on purpose: the long caption used to sit across the middle of
// the sky, which is exactly where the sun and clouds are.
const captions = {
  aerial: ['01 / SITE OVERVIEW', '20 inverter blocks, 200,100 modules.'],
  rows: ['02 / AT MODULE LEVEL', 'Glass, silicon and steel, between the rows.'],
  station: ['03 / GRID CONNECTION', 'Collector switchyard and transformers.'],
  sky: ['04 / LOOKING UP', 'Sun position and cloud cover for this hour.'],
  block: ['05 / ONE BLOCK', 'A single 5 MW inverter block.'],
};

const FLAG_LABEL = {
  normal: 'Matching its peers', underperforming: 'Behind its peers',
  not_producing: 'Not producing', above_model: 'Reading above the model',
};

function status(text) { $('status').textContent = text; }
function hourLabel(value) {
  const minutes = Math.round(value * 60);
  return String(Math.floor(minutes / 60)).padStart(2, '0') + ':' + String(minutes % 60).padStart(2, '0');
}
function pct(value, digits = 1) { return (value * 100).toFixed(digits) + '%'; }
function titleCase(text) { return text.replaceAll('_', ' ').replace(/^./, c => c.toUpperCase()); }

async function json(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error || 'The server could not complete this request.');
  }
  return response.json();
}
function localParts(timestamp) {
  const parts = new Intl.DateTimeFormat('en-CA', {timeZone: plant.location.timezone, year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date(timestamp));
  return Object.fromEntries(parts.map(p => [p.type, p.value]));
}
function fillList(node, rows) {
  node.replaceChildren();
  for (const [name, value] of rows) {
    const dt = document.createElement('dt'), dd = document.createElement('dd');
    dt.textContent = name; dd.textContent = value; node.append(dt, dd);
  }
}

// ---- Plant map ------------------------------------------------------
// Laid out five across and four down, the same order the 3D scene builds
// them in, so a cell's position on screen matches the block's position on
// the ground.

function buildFleetGrid() {
  const grid = $('fleet-grid');
  grid.replaceChildren();
  for (const entry of plant.topology.blocks) {
    const cell = document.createElement('button');
    cell.className = 'fleet-cell'; cell.dataset.blockId = entry.id;
    cell.title = entry.id; cell.setAttribute('aria-label', entry.id);
    cell.innerHTML = '<i></i><span>' + entry.id.slice(-3) + '</span>';
    cell.addEventListener('click', () => selectBlock(entry.id));
    grid.append(cell);
  }
}

function paintFleet(blocks) {
  for (const block of blocks) {
    const cell = $('fleet-grid').querySelector(`[data-block-id="${block.block_id}"]`);
    if (!cell) continue;
    cell.dataset.flag = block.flag;
    cell.classList.toggle('selected', block.block_id === selected);
    const share = Math.max(0, Math.min(1, block.inverter_ac_mw / 5));
    cell.style.setProperty('--fill', (share * 100).toFixed(1) + '%');
    cell.title = `${block.block_id} · ${block.inverter_ac_mw.toFixed(2)} MW · ${FLAG_LABEL[block.flag]}`;
  }
  const flagged = blocks.filter(b => b.flag === 'underperforming' || b.flag === 'not_producing');
  const lost = flagged.reduce((total, b) => total + b.estimated_shortfall_mw, 0);
  $('fleet-summary').textContent = flagged.length
    ? `${flagged.length} block${flagged.length > 1 ? 's' : ''} behind: ${flagged.map(b => b.block_id).join(', ')} · about ${lost.toFixed(2)} MW short.`
    : (blocks.some(b => b.inverter_ac_mw > 0) ? 'All blocks matching their peers.' : 'Plant is dark. Residuals need daylight.');
}

// ---- Block detail ---------------------------------------------------

function selectBlock(blockId) {
  selected = blockId;
  view.highlightBlock(blockId);
  $('block-panel').hidden = false;
  renderBlockPanel();
  if (snapshot) paintFleet(snapshot.fleet.blocks);
}
function closeBlock() {
  selected = null; $('block-panel').hidden = true; view.clearHighlight();
  if (snapshot) paintFleet(snapshot.fleet.blocks);
}

function renderBlockPanel() {
  if (!selected || !snapshot) return;
  const block = snapshot.fleet.blocks.find(b => b.block_id === selected);
  if (!block) return;
  $('block-title').textContent = block.block_id;
  $('block-eyebrow').textContent = block.inverter_id + ' · 5 MW';

  const flagNode = $('block-flag');
  flagNode.dataset.flag = block.flag;
  const relative = block.relative_to_peers === null ? null : pct(block.relative_to_peers, 1);
  flagNode.textContent = FLAG_LABEL[block.flag] + (relative && block.flag !== 'normal' ? ` · ${relative} vs peers` : '');

  // Kept to what distinguishes this block from its neighbours; plant-wide
  // figures live in the telemetry drawer rather than being repeated here.
  const rows = [
    ['Measured AC', block.measured_ac_mw.toFixed(3) + ' MW'],
    ['Twin expects', block.expected_ac_mw.toFixed(3) + ' MW'],
    ['Gap', (block.measured_ac_mw - block.expected_ac_mw).toFixed(3) + ' MW'],
    ['vs fleet median', relative ?? '— (dark)'],
    ['Array DC', block.array_dc_mw.toFixed(3) + ' MW'],
  ];
  if (block.clipping_loss_mw > 0.001) rows.push(['Clipped', block.clipping_loss_mw.toFixed(3) + ' MW']);
  if (block.string_availability < 1) rows.push(['Strings online', pct(block.string_availability, 0)]);
  rows.push(['Soiling', pct(block.soiling_loss_fraction, 2)]);
  rows.push(['Age / degradation', `${block.age_years.toFixed(1)} yr · ${pct(block.degradation_loss_fraction, 1)}`]);
  rows.push(['Inverter', titleCase(block.status)]);
  fillList($('block-metrics'), rows);

  const kind = $('fault-kind');
  const current = faults[block.block_id];
  kind.value = current ? current.kind : 'none';
  const severityWrap = $('severity-wrap');
  severityWrap.hidden = kind.value === 'none' || kind.value === 'inverter_offline';
  if (current && current.kind !== 'inverter_offline') {
    $('fault-severity').value = String(Math.round(current.severity * 100));
    $('severity-out').textContent = Math.round(current.severity * 100) + '%';
  }
  $('clear-fault').disabled = !current;
}

// ---- Charts ---------------------------------------------------------
// Hand-built SVG. A charting library would be a dependency, and the whole
// project runs without one.

function svg(width, height, body) {
  return `<svg viewBox="0 0 ${width} ${height}" role="img" preserveAspectRatio="none">${body}</svg>`;
}

function renderWaterfall(steps) {
  const node = $('waterfall');
  if (!steps || !steps.length) { node.replaceChildren(); return; }
  const start = steps[0].mw;
  if (start <= 0) { node.innerHTML = '<p class="muted">No irradiance on the array at this hour.</p>'; return; }
  let running = start;
  const rows = [`<div class="wf-row wf-start"><span>${titleCase(steps[0].step)}</span>
      <div class="wf-bar"><i style="width:100%"></i></div><b>${start.toFixed(2)}</b></div>`];
  for (const step of steps.slice(1)) {
    const loss = -step.mw;
    running -= loss;
    const share = Math.max(0, loss / start);
    if (loss < 0.0005) continue;   // hide steps that round to nothing at this hour
    rows.push(`<div class="wf-row"><span>${titleCase(step.step)}</span>
      <div class="wf-bar"><i style="width:${Math.min(100, share * 100 * 6).toFixed(2)}%"></i></div>
      <b>−${loss.toFixed(2)}</b></div>`);
  }
  rows.push(`<div class="wf-row wf-end"><span>Net export</span>
      <div class="wf-bar"><i style="width:${(running / start * 100).toFixed(1)}%"></i></div>
      <b>${running.toFixed(2)}</b></div>`);
  node.innerHTML = rows.join('') +
    `<p class="muted wf-note">Bars are scaled ×6 against nameplate so small losses stay visible. Values in MW.</p>`;
}

function renderDayChart(day, currentHour) {
  const node = $('day-chart');
  if (!day) { node.innerHTML = '<p class="muted">Loading the day…</p>'; return; }
  const W = 260, H = 96;
  const peak = Math.max(1, ...day.series.map(p => Math.max(p.net_export_mw, p.expected_net_export_mw)));
  const x = hour => (hour / 24) * W;
  const y = mw => H - (mw / peak) * (H - 6);
  const path = key => day.series.map((p, i) => `${i ? 'L' : 'M'}${x(p.local_hour).toFixed(1)},${y(p[key]).toFixed(1)}`).join('');
  const area = path('net_export_mw') + `L${W},${H}L0,${H}Z`;
  const gaps = day.series.some(p => p.expected_net_export_mw - p.net_export_mw > 0.01);
  node.innerHTML = svg(W, H, `
    <path d="${area}" fill="rgba(216,240,173,.18)" />
    ${gaps ? `<path d="${path('expected_net_export_mw')}" fill="none" stroke="#e8a33d" stroke-width="1" stroke-dasharray="3 2" />` : ''}
    <path d="${path('net_export_mw')}" fill="none" stroke="#d8f0ad" stroke-width="1.6" />
    <line x1="${x(currentHour)}" y1="0" x2="${x(currentHour)}" y2="${H}" stroke="#fff" stroke-width="1" opacity=".55" />
  `) + `<div class="chart-axis"><span>00</span><span>06</span><span>12</span><span>18</span><span>24</span></div>
  ${gaps ? '<p class="chart-key"><i class="dash"></i>what a healthy plant would have made</p>' : ''}`;

  const energy = day.energy;
  fillList($('day-metrics'), [
    ['Exported', energy.export_energy_mwh.toFixed(1) + ' MWh'],
    ['A healthy plant', day.expected_energy.export_energy_mwh.toFixed(1) + ' MWh'],
    ['Lost to faults', day.energy_shortfall_mwh.toFixed(1) + ' MWh'],
    ['Plane-of-array', energy.poa_irradiation_kwh_m2.toFixed(2) + ' kWh/m²'],
    ['Performance ratio', energy.performance_ratio === null ? '—' : energy.performance_ratio.toFixed(4)],
    ['Specific yield', energy.specific_yield_kwh_per_kwp.toFixed(2) + ' kWh/kWp'],
    ['Capacity factor', energy.capacity_factor === null ? '—' : pct(energy.capacity_factor, 1)],
    ['Peak export', energy.peak_export_mw.toFixed(1) + ' MW'],
    ['Curtailed', energy.curtailed_energy_mwh.toFixed(2) + ' MWh'],
  ]);
}

// ---- Snapshot -------------------------------------------------------

function showSnapshot(next) {
  snapshot = next;
  view.updateSnapshot(next);
  view.updateFleet(next.fleet.blocks, 5);

  const grid = next.grid, dc = next.electrical.dc;
  $('net-value').innerHTML = grid.net_export_mw.toFixed(1) + '<small> MW</small>';
  const shortfall = grid.expected_net_export_mw - grid.net_export_mw;
  $('net-delta').textContent = shortfall > 0.05 ? `−${shortfall.toFixed(1)} MW vs healthy` : '';
  $('poa-value').innerHTML = next.array_plane.effective_poa_global_w_m2.toFixed(0) + '<small> W/m²</small>';
  $('temperature-value').innerHTML = dc.cell_temperature_c.toFixed(1) + '<small> °C</small>';
  $('capacity-fill').style.width = Math.min(100, grid.net_export_mw / plant.summary.ac_capacity_mw * 100) + '%';

  const time = localParts(next.timestamp_utc);
  fillList($('metrics-list'), [
    ['Snapshot time', time.hour + ':' + time.minute
      + (next.measured === false ? ' (interpolated)' : '')],
    ['Ambient air', next.conditions.air_temperature_c.toFixed(1) + ' °C'],
    ['Wind at 10 m', next.conditions.wind_speed_10m_m_s.toFixed(1) + ' m/s'],
    ['Sun elevation', (90 - next.solar_position.zenith_deg).toFixed(1) + '°'],
    ['Cloud cover', Math.round(next.cloud_cover_fraction * 100) + '%'],
    ['Incident POA', next.poa.poa_global_w_m2.toFixed(0) + ' W/m²'],
    ['Row shading', pct(next.array_plane.shaded_row_fraction, 1)],
    ['Reflection (IAM)', pct(1 - next.array_plane.incidence_angle_modifier, 1)],
    ['Fleet soiling', pct(next.state.soiling_loss_fraction, 2)],
    ['Degradation', pct(next.state.degradation_loss_fraction, 2)],
    ['Array DC', grid.array_dc_mw.toFixed(2) + ' MW'],
    ['Inverter output', grid.plant_inverter_ac_mw.toFixed(2) + ' MW'],
    ['Net export', grid.net_export_mw.toFixed(2) + ' MW'],
    ['Curtailed', grid.curtailment_mw.toFixed(2) + ' MW'],
    ['Fleet median ratio', next.residuals.fleet_median_ratio === null ? '— (dark)' : next.residuals.fleet_median_ratio.toFixed(4)],
    ['String voltage', next.electrical.string_vmp_v.toFixed(0) + ' V'],
  ]);

  const warnings = [...new Set([...next.solar_position.warnings, ...next.poa.warnings,
    ...next.electrical.warnings, ...next.fleet.warnings, ...next.residuals.warnings,
    ...(next.state.warnings || []), ...(next.weather_warnings || [])])];
  $('warnings').replaceChildren();
  for (const message of warnings) {
    const node = document.createElement('div');
    node.className = 'warning'; node.textContent = message.replaceAll('_', ' ');
    $('warnings').append(node);
  }
  $('details-button').title = warnings.length ? warnings.length + ' model notes' : 'View plant data';

  paintFleet(next.fleet.blocks);
  renderWaterfall(next.fleet.waterfall);
  renderDayChart(dayData, Number(hourInput.value));
  if (selected) renderBlockPanel();
}

async function updateSelection() {
  const mine = ++sequence, hour = Number(hourInput.value);
  $('hour-label').textContent = hourLabel(hour);
  hourInput.setAttribute('aria-valuetext', hourLabel(hour));
  if (requestController) requestController.abort();
  if (mode === 'real' && observations.length) {
    const closest = observations.reduce((a, b) => Math.abs(b.localHour - hour) < Math.abs(a.localHour - hour) ? b : a);
    showSnapshot(closest);
    status('Weather ' + hourLabel(closest.localHour) + ' local · '
           + (closest.measured ? 'measured hourly observation' : 'interpolated between hourly observations'));
    return true;
  }
  requestController = new AbortController();
  try {
    const next = await json('/api/snapshot?date=' + encodeURIComponent(dateInput.value) + '&hour=' + hour,
                            {signal: requestController.signal});
    if (mine !== sequence) return;
    showSnapshot(next);
    status('Drag to orbit · click a block to inspect it');
    return true;
  } catch (error) {
    if (error.name !== 'AbortError' && mine === sequence) status('Unable to update: ' + error.message);
    return false;
  }
}

async function loadDay() {
  const mine = ++daySequence;
  try {
    const next = await json('/api/day?date=' + encodeURIComponent(dateInput.value) + '&steps=2');
    if (mine !== daySequence) return;
    dayData = next;
    const pr = next.energy.performance_ratio;
    $('pr-value').textContent = pr === null ? '—' : pr.toFixed(3);
    $('pr-note').textContent = next.energy_shortfall_mwh > 0.5
      ? `−${next.energy_shortfall_mwh.toFixed(0)} MWh today` : '';
    renderDayChart(next, Number(hourInput.value));
  } catch (error) {
    if (mine === daySequence) { dayData = null; $('pr-value').textContent = '—'; }
  }
}

// ---- Faults ---------------------------------------------------------

async function postFaults(next) {
  const scenario = await json('/api/scenario/faults', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(next),
  });
  faults = scenario.faults;
  clearWeather();               // cached weather snapshots were built under the old state
  await Promise.all([updateSelection(), loadDay()]);
}

async function applyFault() {
  if (!selected) return;
  const kind = $('fault-kind').value;
  const next = {...faults};
  delete next[selected];
  for (const [id, fault] of Object.entries(next)) next[id] = {kind: fault.kind, severity: fault.severity, note: fault.note};
  if (kind !== 'none') {
    next[selected] = kind === 'inverter_offline'
      ? {kind, severity: 1, note: 'declared in the browser'}
      : {kind, severity: Number($('fault-severity').value) / 100, note: 'declared in the browser'};
  }
  status('Applying fault…');
  try { await postFaults(next); status(kind === 'none' ? 'Fault cleared.' : `${titleCase(kind)} declared on ${selected}.`); }
  catch (error) { status('Could not apply the fault: ' + error.message); }
}

async function clearFault() {
  if (!selected) return;
  const next = {};
  for (const [id, fault] of Object.entries(faults)) {
    if (id === selected) continue;
    next[id] = {kind: fault.kind, severity: fault.severity, note: fault.note};
  }
  try { await postFaults(next); status(`Fault cleared on ${selected}.`); }
  catch (error) { status('Could not clear the fault: ' + error.message); }
}

// ---- Wiring ---------------------------------------------------------

function clearWeather() {
  mode = 'synthetic'; observations = []; weatherSequence++;
  $('synthetic-button').hidden = true;
  $('weather-button').disabled = false; $('weather-button').textContent = 'Load weather';
  $('source-label').textContent = 'CLEAR-SKY DEMO';
}

function setPlaying(value) {
  playing = value; clearTimeout(playTimer);
  $('play-button').textContent = playing ? 'Ⅱ' : '▶';
  $('play-button').setAttribute('aria-label', playing ? 'Pause day' : 'Play day');
  $('play-button').setAttribute('aria-pressed', String(playing));
  if (playing) tick();
}
async function tick() {
  if (!playing) return;
  hourInput.value = String((Number(hourInput.value) + .25) % 24);
  await updateSelection();
  if (playing) playTimer = setTimeout(tick, 900);
}

function chooseView(name) {
  view.setView(name); $('tour-button').setAttribute('aria-pressed', 'false');
  document.querySelectorAll('[data-view]').forEach(b => {
    b.classList.toggle('active', b.dataset.view === name);
    b.setAttribute('aria-pressed', String(b.dataset.view === name));
  });
  const [kicker, title] = captions[name] || captions.aerial;
  $('view-kicker').textContent = kicker; $('view-title').textContent = title;
  document.body.classList.toggle('sky-view', name === 'sky');
}

function details(open) { $('telemetry').hidden = !open; $('details-button').setAttribute('aria-expanded', String(open)); }

function chooseTab(name) {
  document.querySelectorAll('[data-tab]').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === name);
    b.setAttribute('aria-selected', String(b.dataset.tab === name));
  });
  document.querySelectorAll('[data-panel]').forEach(p => { p.hidden = p.dataset.panel !== name; });
  if (name === 'day') renderDayChart(dayData, Number(hourInput.value));
}

function wire() {
  document.querySelectorAll('[data-view]').forEach(b => b.addEventListener('click', () => chooseView(b.dataset.view)));
  document.querySelectorAll('[data-tab]').forEach(b => b.addEventListener('click', () => chooseTab(b.dataset.tab)));
  $('details-button').addEventListener('click', () => details($('telemetry').hidden));
  $('close-details').addEventListener('click', () => details(false));
  $('close-block').addEventListener('click', closeBlock);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') { details(false); closeBlock(); } });
  $('reset-button').addEventListener('click', () => chooseView(view.view === 'block' ? 'aerial' : view.view));
  $('tour-button').addEventListener('click', () => {
    view.controls.autoRotate = !view.controls.autoRotate;
    $('tour-button').setAttribute('aria-pressed', String(view.controls.autoRotate));
  });
  $('quality-button').addEventListener('click', () => {
    highDetail = !highDetail; view.setQuality(highDetail);
    $('quality-button').textContent = highDetail ? 'High detail' : 'Performance';
    $('quality-button').setAttribute('aria-pressed', String(highDetail));
  });
  $('overlay-button').addEventListener('click', () => {
    const on = $('overlay-button').getAttribute('aria-pressed') !== 'true';
    view.setOverlayVisible(on); $('overlay-button').setAttribute('aria-pressed', String(on));
  });
  $('focus-block').addEventListener('click', () => {
    if (!selected) return;
    view.focusBlock(selected); chooseViewCaption('block');
  });
  $('fault-kind').addEventListener('change', () => {
    const kind = $('fault-kind').value;
    $('severity-wrap').hidden = kind === 'none' || kind === 'inverter_offline';
  });
  $('fault-severity').addEventListener('input', () => { $('severity-out').textContent = $('fault-severity').value + '%'; });
  $('apply-fault').addEventListener('click', applyFault);
  $('clear-fault').addEventListener('click', clearFault);

  hourInput.addEventListener('input', () => {
    setPlaying(false); $('hour-label').textContent = hourLabel(Number(hourInput.value));
    clearTimeout(debounce); debounce = setTimeout(updateSelection, 80);
  });
  dateInput.addEventListener('change', () => { setPlaying(false); clearWeather(); updateSelection(); loadDay(); });
  $('play-button').addEventListener('click', () => setPlaying(!playing));
  $('synthetic-button').addEventListener('click', () => { clearWeather(); updateSelection(); });
  $('weather-button').addEventListener('click', loadWeather);

  // Click the ground to select a block, but only if the pointer did not move
  // far enough to be an orbit drag.
  const canvas = $('scene');
  let down = null;
  canvas.addEventListener('pointerdown', e => { down = {x: e.clientX, y: e.clientY}; });
  canvas.addEventListener('pointerup', e => {
    if (!down) return;
    const moved = Math.hypot(e.clientX - down.x, e.clientY - down.y);
    down = null;
    if (moved > 5) return;
    const blockId = view.pickBlock(e.clientX, e.clientY);
    if (blockId) selectBlock(blockId); else closeBlock();
  });
}

function chooseViewCaption(name) {
  const [kicker, title] = captions[name] || captions.aerial;
  $('view-kicker').textContent = kicker; $('view-title').textContent = title;
  document.querySelectorAll('[data-view]').forEach(b => { b.classList.remove('active'); b.setAttribute('aria-pressed', 'false'); });
}

async function loadWeather() {
  setPlaying(false);
  const token = ++weatherSequence, chosen = dateInput.value;
  $('weather-button').disabled = true; $('weather-button').textContent = 'Loading…';
  status('Fetching weather and rainfall history for the selected local day…');
  try {
    // The API serves UTC days. Two days cover Pavagada's UTC+5:30 local day.
    const previous = new Date(chosen + 'T00:00:00Z'); previous.setUTCDate(previous.getUTCDate() - 1);
    const days = [previous.toISOString().slice(0, 10), chosen];
    // steps=4 fills the gaps between the archive's hourly observations, so
    // playback moves every 15 minutes instead of holding each hour for four
    // frames and then jumping. Interpolated samples are labelled as such.
    const responses = await Promise.all(days.map(day => json('/api/weather-day?date=' + day + '&steps=4')));
    if (token !== weatherSequence || chosen !== dateInput.value) return;
    observations = responses.flat()
      .filter(s => { const p = localParts(s.timestamp_utc); return p.year + '-' + p.month + '-' + p.day === chosen; })
      .map(s => { const p = localParts(s.timestamp_utc); return {...s, localHour: Number(p.hour) + Number(p.minute) / 60}; });
    if (!observations.length) throw new Error('No weather is available for this local date.');
    mode = 'real'; $('source-label').textContent = 'WEATHER-DRIVEN';
    $('synthetic-button').hidden = false; $('weather-button').textContent = 'Weather loaded';
    await updateSelection();
  } catch (error) {
    if (token === weatherSequence) {
      $('weather-button').disabled = false; $('weather-button').textContent = 'Retry weather';
      status('Weather unavailable: ' + error.message);
    }
  }
}

async function init() {
  plant = await json('/api/plant');
  faults = plant.scenario.faults;
  faultKinds = plant.scenario.fault_kinds;
  $('plant-meta').textContent = 'KARNATAKA, INDIA · ' + plant.summary.ac_capacity_mw + ' MW AC';
  const zone = new Intl.DateTimeFormat('en', {timeZone: plant.location.timezone, timeZoneName: 'short'})
    .formatToParts(new Date()).find(p => p.type === 'timeZoneName').value;
  $('timezone-label').textContent = zone === 'GMT+5:30' ? 'IST' : zone;

  const kindSelect = $('fault-kind');
  kindSelect.replaceChildren(new Option('No fault (healthy)', 'none'));
  for (const entry of faultKinds) kindSelect.append(new Option(titleCase(entry.kind), entry.kind));

  view = new SolarScene($('scene'), plant);
  buildFleetGrid();
  wire();
  chooseView('aerial');   // sets the caption from `captions`, not the HTML default
  if (await updateSelection() === false) throw new Error('Initial simulation unavailable. Reload to retry.');
  loadDay();
  $('loading').classList.add('done');
}

init().catch(error => {
  $('loading-message').textContent = 'Could not start the scene: ' + error.message;
  status(error.message);
});
