import './style.css';
import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import {EditorState, StateEffect, StateField} from '@codemirror/state';
import {Decoration, DecorationSet, EditorView, keymap, lineNumbers} from '@codemirror/view';
import {defaultKeymap} from '@codemirror/commands';
import {cpp} from '@codemirror/lang-cpp';

const q = <T extends HTMLElement>(selector: string) => document.querySelector(selector) as T;
const canvas = q<HTMLCanvasElement>('#dvs');
const ctx = canvas.getContext('2d')!;
const image = ctx.createImageData(112, 126);
const energy = new Float32Array(112 * 126 * 2);
// Tracking view: raw DVS tap rendered on its own canvas with the tracker's
// bounding box overlaid. Same 112x126 backing store as the live view.
const trackCanvas = q<HTMLCanvasElement>('#track');
const trackCtx = trackCanvas.getContext('2d')!;
const trackImage = trackCtx.createImageData(112, 126);
const rawEnergy = new Float32Array(112 * 126 * 2);
// App view: raw DVS tap as background (same 112x126 store) with the selected
// demo app's decoded result overlaid on top.
const appCanvas = q<HTMLCanvasElement>('#app');
const appCtx = appCanvas.getContext('2d')!;
const appImage = appCtx.createImageData(112, 126);
const appEnergy = new Float32Array(112 * 126 * 2);
let paused = false;
let dirty = false;
let transformAtBlockSync = '';
let lastWords = 0, lastPackets = 0, lastRateTime = performance.now();
let stream = {words: 0, packets: 0, dropped: 0};
let rawStream = {words: 0, packets: 0};
let board: any = {connected: false, counters: {}};
let pending: Uint32Array[] = [];
let pendingRaw: Uint32Array[] = [];
let pendingApp: Uint32Array[] = [];   // raw-tap background words for the app view
let appTail: number[] = [];           // carry-over result words awaiting decode

// Binary websocket frames carry a leading 32-bit stream tag (see dashboard.py).
const STREAM_RESULT = 0, STREAM_RAW = 1;
type Mode = 'live' | 'track' | 'app';
let mode: Mode = 'live';

// --- Demo-app registry (sent by the backend in the init message) ------------
type AppParam = {name: string; label: string; min: number; max: number; default: number};
type AppInfo = {prog: string; label: string; blurb: string; params: AppParam[]};
let apps: Record<string, AppInfo> = {};
let currentApp = '';            // id of the app selected in the dropdown
let loadedApp = '';             // id of the app whose firmware is actually on the board
// The result stream only carries the loaded app's status words once its firmware
// is running; before that it echoes per-event words. appActive gates decoding so
// stale event echoes are never mistaken for app records (mirrors trackerActive).
let appActive = false;
// Per-app decoded state, rendered by the app renderers below.
const mayflyWorld = new Uint8Array(126 * 112);   // occupancy world [cy*126 + cx]
let motionCell = {row: 0, col: 0, val: 0, motion: 0, at: 0};
let omsCell = {row: 0, col: 0, val: 0, oms: 0, at: 0};
let stabVec = {dx: 0, dy: 0, oct: 0, mag: 0, at: 0};
type HeartCell = {bin: number; conf: number};
const heartGrid: (HeartCell | null)[] = new Array(8 * 7).fill(null);
let dirCons = {flag: 0, z: 0, row: 0, col: 0, gdir: 0, at: 0};

// Latest tracker status, decoded from the dvs_track result stream. word0 packs
// (locked<<24)|(cx<<16)|(cy<<8)|count; word1 packs (min_x<<24)|(min_y<<16)|(max_x<<8)|max_y.
let trackBox = {locked: 0, cx: 0, cy: 0, count: 0, x0: 0, y0: 0, x1: 0, y1: 0, at: 0};
let trackTail: number[] = [];
// The result stream only carries dvs_track status pairs once its firmware is
// loaded; before that it echoes per-event words, which must not be mistaken for
// tracker records. Set by the "Load tracker firmware" action.
let trackerActive = false;
const emptyBox = () => ({locked: 0, cx: 0, cy: 0, count: 0, x0: 0, y0: 0, x1: 0, y1: 0, at: 0});

const setDiagnostics = StateEffect.define<any[]>();
const diagnosticField = StateField.define<DecorationSet>({
  create: () => Decoration.none,
  update(value, transaction) {
    value = value.map(transaction.changes);
    for (const effect of transaction.effects) if (effect.is(setDiagnostics)) {
      const ranges = effect.value.flatMap(item => {
        if (item.line < 1 || item.line > transaction.state.doc.lines) return [];
        const line = transaction.state.doc.line(item.line);
        return [Decoration.line({attributes: {class: `diagnostic-${item.severity}`,
          title: item.message}}).range(line.from)];
      });
      value = Decoration.set(ranges, true);
    }
    return value;
  },
  provide: field => EditorView.decorations.from(field)
});

const editor = new EditorView({
  state: EditorState.create({doc: '', extensions: [lineNumbers(), cpp(), keymap.of(defaultKeymap),
    diagnosticField,
    EditorView.theme({"&": {backgroundColor: '#0c1115', color: '#d9e1e6'},
      '.cm-content': {caretColor: '#ed6b57'}, '.cm-gutters': {backgroundColor: '#11171c', color: '#56636c', border: 'none'},
      '&.cm-focused .cm-cursor': {borderLeftColor: '#ed6b57'}, '.cm-activeLine': {backgroundColor: '#182027'},
      '.diagnostic-error': {backgroundColor: '#7a2f2848'}, '.diagnostic-warning': {backgroundColor: '#755a2548'}}),
    EditorView.updateListener.of(update => {
      if (update.docChanged) { dirty = true; q('#dirty-state').textContent = 'Unsaved'; updateBlockState(); }
    })]}),
  parent: q('#editor')
});

const dashboardMain = q<HTMLElement>('main');
const workspaceDivider = q<HTMLElement>('#workspace-divider');
function resizeWorkspace(clientX: number) {
  const bounds = dashboardMain.getBoundingClientRect();
  const stageWidth = Math.max(280, Math.min(clientX - bounds.left, bounds.width - 397));
  dashboardMain.style.setProperty('--stage-width', `${stageWidth}px`);
  editor.requestMeasure();
  Blockly.svgResize(workspace);
}
workspaceDivider.onpointerdown = event => {
  workspaceDivider.setPointerCapture(event.pointerId);
  workspaceDivider.classList.add('dragging');
};
workspaceDivider.onpointermove = event => {
  if (workspaceDivider.hasPointerCapture(event.pointerId)) resizeWorkspace(event.clientX);
};
workspaceDivider.onpointerup = event => {
  workspaceDivider.releasePointerCapture(event.pointerId);
  workspaceDivider.classList.remove('dragging');
};
workspaceDivider.ondblclick = () => {
  dashboardMain.style.removeProperty('--stage-width');
  editor.requestMeasure();
  Blockly.svgResize(workspace);
};
workspaceDivider.onkeydown = event => {
  if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
  event.preventDefault();
  const stageWidth = q<HTMLElement>('.stage').getBoundingClientRect().width;
  resizeWorkspace(dashboardMain.getBoundingClientRect().left + stageWidth + (event.key === 'ArrowLeft' ? -20 : 20));
};

function currentSource() { return editor.state.doc.toString(); }
function setSource(source: string) {
  editor.dispatch({changes: {from: 0, to: editor.state.doc.length, insert: source}});
  dirty = false; q('#dirty-state').textContent = 'Saved';
}

async function api(path: string, body?: any) {
  const response = await fetch('/api/' + path, {method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body)});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || 'request failed');
  return value;
}

function defineBlocks() {
  const defs = [
    {type: 'translate', message0: 'translate x %1 y %2', args0: [{type:'field_number',name:'DX',value:0,min:-111,max:111},{type:'field_number',name:'DY',value:0,min:-125,max:125}], colour: 190},
    {type: 'rotate', message0: 'rotate %1 degrees', args0: [{type:'field_dropdown',name:'ANGLE',options:[['45','45'],['90','90'],['180','180'],['270','270']]}], colour: 25},
    {type: 'mirror', message0: 'mirror %1', args0: [{type:'field_dropdown',name:'AXIS',options:[['X (horizontal)','X'],['Y (vertical)','Y']]}], colour: 25},
    {type: 'clamp', message0: 'clamp to sensor', colour: 120},
    {type: 'invert', message0: 'invert polarity', colour: 300},
    {type: 'rect_filter', message0: 'keep rectangle x %1 to %2 y %3 to %4', args0:[{type:'field_number',name:'X0',value:0,min:0,max:111},{type:'field_number',name:'X1',value:111,min:0,max:111},{type:'field_number',name:'Y0',value:0,min:0,max:125},{type:'field_number',name:'Y1',value:125,min:0,max:125}], colour: 55},
    {type: 'pol_filter', message0: 'keep polarity %1', args0:[{type:'field_dropdown',name:'P',options:[['ON','1'],['OFF','0']]}], colour: 55}
  ];
  for (const def of defs) Blockly.Blocks[def.type] = {init: function(this: Blockly.Block) {
    this.jsonInit({...def, previousStatement: null, nextStatement: null});
  }};
}
defineBlocks();
const toolbox = {kind:'flyoutToolbox', contents:['translate','rotate','mirror','clamp','invert','rect_filter','pol_filter'].map(type => ({kind:'block', type}))};
const darkBlocklyTheme = Blockly.Theme.defineTheme('actnow-dark', {
  name: 'actnow-dark',
  base: Blockly.Themes.Zelos,
  componentStyles: {
    workspaceBackgroundColour: '#0c1115',
    toolboxBackgroundColour: '#11171c',
    toolboxForegroundColour: '#d9e1e6',
    flyoutBackgroundColour: '#171f25',
    flyoutForegroundColour: '#d9e1e6',
    flyoutOpacity: 1,
    scrollbarColour: '#66747d',
    scrollbarOpacity: 0.8,
    insertionMarkerColour: '#f07a67',
    insertionMarkerOpacity: 0.45,
    markerColour: '#f07a67',
    cursorColour: '#f07a67',
    selectedGlowColour: '#f07a67',
    selectedGlowOpacity: 0.55,
    replacementGlowColour: '#57c786',
    replacementGlowOpacity: 0.5
  },
  fontStyle: {family: 'Inter, ui-sans-serif, system-ui, sans-serif'}
});
const workspace = Blockly.inject('blockly', {
  toolbox,
  theme: darkBlocklyTheme,
  trashcan: true,
  renderer: 'zelos',
  horizontalLayout: true,
  toolboxPosition: 'end',
  move: {
    scrollbars: {horizontal: true, vertical: true},
    drag: true,
    wheel: true
  }
});
let blocksInitialized = false;
let generateTimer: number | undefined;
workspace.addChangeListener(event => {
  if (!blocksInitialized || event.isUiEvent) return;
  window.clearTimeout(generateTimer);
  generateTimer = window.setTimeout(() => regenerateFromBlocks(), 150);
});

function n(block: Blockly.Block, field: string) { return Number(block.getFieldValue(field)); }
function generateTransform() {
  const lines = [
    '/* ACTNOW_TRANSFORM_BEGIN */',
    'static bool transform_event(uint32_t input, uint32_t *output) {',
    '    uint32_t word = input;',
    '    int32_t x = (int32_t)((word >> X_SHIFT) & 0x7Fu);',
    '    int32_t y = (int32_t)((word >> Y_SHIFT) & 0x7Fu);',
    '    uint32_t p = word & 1u;',
    '    y = (SY - 1) - y;'
  ];
  let block: Blockly.Block | null = workspace.getTopBlocks(true)[0] || null;
  while (block) {
    if (block.type === 'translate') lines.push(`    y += ${n(block,'DX')};`, `    x += ${n(block,'DY')};`);
    if (block.type === 'mirror') lines.push(block.getFieldValue('AXIS') === 'X' ? '    y = (SY - 1) - y;' : '    x = (SX - 1) - x;');
    if (block.type === 'clamp') lines.push('    x = clampi(x, 0, SX - 1);', '    y = clampi(y, 0, SY - 1);');
    if (block.type === 'invert') lines.push('    p ^= 1u;');
    if (block.type === 'rect_filter') lines.push(`    if (y < ${n(block,'X0')} || y > ${n(block,'X1')} || x < ${n(block,'Y0')} || x > ${n(block,'Y1')}) return false;`);
    if (block.type === 'pol_filter') lines.push(`    if (p != ${n(block,'P')}u) return false;`);
    if (block.type === 'rotate') {
      const a = block.getFieldValue('ANGLE');
      lines.push('    {', '        int32_t tx = x - CX;', '        int32_t ty = y - CY;', '        int32_t rx;', '        int32_t ry;');
      if (a === '45') lines.push('        rx = (tx - ty) >> 1;', '        ry = (tx + ty) >> 1;');
      if (a === '90') lines.push('        rx = -ty;', '        ry = tx;');
      if (a === '180') lines.push('        rx = -tx;', '        ry = -ty;');
      if (a === '270') lines.push('        rx = ty;', '        ry = -tx;');
      lines.push('        x = rx + CX;', '        y = ry + CY;', '    }');
    }
    block = block.getNextBlock();
  }
  lines.push('    x = clampi(x, 0, SX - 1);', '    y = clampi(y, 0, SY - 1);',
    '    *output = (word & ~(XY_MASK | 1u)) | ((uint32_t)x << X_SHIFT) |',
    '              ((uint32_t)y << Y_SHIFT) | p;', '    return true;', '}', '/* ACTNOW_TRANSFORM_END */');
  return lines.join('\n');
}

function replaceTransform(source: string, generated: string) {
  const start = source.indexOf('/* ACTNOW_TRANSFORM_BEGIN */');
  const marker = '/* ACTNOW_TRANSFORM_END */';
  const end = source.indexOf(marker);
  if (start < 0 || end < start) throw new Error('transformation markers are missing');
  return source.slice(0, start) + generated + source.slice(end + marker.length);
}

function transformRegion(source: string) {
  const start = source.indexOf('/* ACTNOW_TRANSFORM_BEGIN */');
  const marker = '/* ACTNOW_TRANSFORM_END */';
  const end = source.indexOf(marker);
  return start >= 0 && end >= start ? source.slice(start, end + marker.length) : '';
}

function updateBlockState() {
  const stale = transformAtBlockSync !== '' && transformRegion(currentSource()) !== transformAtBlockSync;
  q('#block-state').textContent = stale ? 'C transform changed; editing blocks will regenerate it' : 'Pipeline synchronized';
  q('#block-state').classList.toggle('warning', stale);
}

async function regenerateFromBlocks() {
  try {
    const source = replaceTransform(currentSource(), generateTransform());
    setSource(source); transformAtBlockSync = transformRegion(source);
    await api('blocks', {blocks: Blockly.serialization.workspaces.save(workspace)});
    updateBlockState();
  } catch (error) { appendLog(String(error), 'error'); }
}

async function firmwareAction(name: string) {
  document.querySelectorAll<HTMLButtonElement>('button').forEach(b => b.disabled = true);
  try {
    const payload = name === 'build' || name === 'apply' ? {source: currentSource()}
      : name === 'track' ? {radius: Number(q<HTMLInputElement>('#radius').value),
                            correlation: Number(q<HTMLInputElement>('#correlation').value)} : {};
    const result = await api(name, payload);
    if (name === 'build' || name === 'apply') { dirty = false; q('#dirty-state').textContent = 'Saved'; showDiagnostics(result.diagnostics || []); }
    // Loading the application firmware turns the result stream back into event
    // echoes; loading the tracker turns it into status pairs (start decoding
    // from a clean, aligned buffer).
    if (name === 'apply') trackerActive = false;
    if (name === 'track') {
      trackerActive = true; trackTail = []; trackBox = emptyBox(); updateTrackReadout();
      showDiagnostics(result.diagnostics || []);
    }
  } catch (error) { appendLog(String(error), 'error'); }
  finally { document.querySelectorAll<HTMLButtonElement>('button').forEach(b => b.disabled = false); }
}
// --- Demo-app selector ------------------------------------------------------
function populateApps(registry: Record<string, AppInfo>) {
  apps = registry;
  const select = q<HTMLSelectElement>('#app-select');
  select.innerHTML = '';
  for (const [id, info] of Object.entries(registry)) {
    const opt = document.createElement('option');
    opt.value = id; opt.textContent = info.label;
    select.appendChild(opt);
  }
  const ids = Object.keys(registry);
  if (ids.length && !currentApp) currentApp = ids[0];
  if (currentApp) select.value = currentApp;
  renderAppControls();
}

// Build the per-app parameter sliders (radius/correlation/...) from the registry.
function renderAppControls() {
  const info = apps[currentApp];
  q('#app-blurb').textContent = info ? info.blurb : '';
  const host = q<HTMLElement>('#app-params');
  host.innerHTML = '';
  if (!info) return;
  for (const p of info.params) {
    const label = document.createElement('label');
    const out = document.createElement('output');
    out.textContent = String(p.default);
    const input = document.createElement('input');
    input.type = 'range'; input.min = String(p.min); input.max = String(p.max);
    input.value = String(p.default); input.dataset.param = p.name;
    input.oninput = () => { out.textContent = input.value; };
    label.append(`${p.label} `, out, input);
    host.appendChild(label);
  }
}

function currentAppParams(): Record<string, number> {
  const params: Record<string, number> = {};
  q<HTMLElement>('#app-params').querySelectorAll<HTMLInputElement>('input[data-param]')
    .forEach(input => { params[input.dataset.param!] = Number(input.value); });
  return params;
}

// Clear all per-app decoded state (called on load / app switch) so the overlay
// starts from a clean slate and no stale record is drawn.
function resetAppState() {
  mayflyWorld.fill(0);
  motionCell = {row: 0, col: 0, val: 0, motion: 0, at: 0};
  omsCell = {row: 0, col: 0, val: 0, oms: 0, at: 0};
  stabVec = {dx: 0, dy: 0, oct: 0, mag: 0, at: 0};
  heartGrid.fill(null);
  dirCons = {flag: 0, z: 0, row: 0, col: 0, gdir: 0, at: 0};
  appEnergy.fill(0); pendingApp.length = 0; appTail = [];
}

async function loadApp() {
  document.querySelectorAll<HTMLButtonElement>('button').forEach(b => b.disabled = true);
  try {
    const info = apps[currentApp];
    q('#app-status').textContent = `building ${info?.label || currentApp}…`;
    const result = await api('load_app', {app: currentApp, params: currentAppParams()});
    showDiagnostics(result.diagnostics || []);
    // Firmware is now running: switch decoding to this app from a clean buffer.
    loadedApp = currentApp; appActive = true; trackerActive = false;
    resetAppState();
    q('#app-loaded').textContent = info?.label || currentApp;
    q('#app-status').textContent = 'running';
  } catch (error) {
    appActive = false;
    q('#app-status').textContent = 'load failed';
    appendLog(String(error), 'error');
  } finally {
    document.querySelectorAll<HTMLButtonElement>('button').forEach(b => b.disabled = false);
  }
}
q<HTMLSelectElement>('#app-select').onchange = e => {
  currentApp = (e.target as HTMLSelectElement).value;
  renderAppControls();
};
q('#app-load').onclick = () => loadApp();

q('#build').onclick = () => firmwareAction('build');
q('#build-apply').onclick = () => firmwareAction('apply');
q('#reset').onclick = () => firmwareAction('reset');
q('#reconnect').onclick = () => firmwareAction('reconnect');
q('#save').onclick = async () => { await api('source', {source: currentSource()}); dirty = false; q('#dirty-state').textContent = 'Saved'; };

function appendLog(message: string, level='info') {
  const log = q<HTMLPreElement>('#log');
  log.textContent += (log.textContent ? '\n' : '') + message;
  if (level === 'error') log.textContent += '  [error]';
  log.scrollTop = log.scrollHeight;
}
function showDiagnostics(items: any[]) {
  editor.dispatch({effects: setDiagnostics.of(items)});
  q('#error-count').textContent = items.length ? String(items.length) : '';
  if (items.length) {
    switchTab('log');
    for (const item of items) appendLog(`${item.line}:${item.column} ${item.severity}: ${item.message}`, item.severity);
  }
}

function switchTab(name: string) {
  document.querySelectorAll('.tab').forEach(e => e.classList.toggle('active', (e as HTMLElement).dataset.tab === name));
  document.querySelectorAll('.panel').forEach(e => e.classList.remove('active'));
  q(`#${name}-panel`).classList.add('active');
  if (name === 'blocks') setTimeout(() => Blockly.svgResize(workspace), 0);
}
document.querySelectorAll<HTMLElement>('.tab').forEach(tab => tab.onclick = () => switchTab(tab.dataset.tab!));

function updateState() {
  const connected = !!board.connected;
  q('#connection').textContent = connected ? 'Online' : 'Offline'; q('#connection').classList.toggle('online', connected);
  q('#firmware-state').textContent = connected ? `Firmware ${String(board.firmware).split('/').pop()}` : 'Board disconnected';
  for (const key of ['drop','fetch','results']) q(`#${key === 'drop' ? 'drops' : key}`).textContent = String(board.counters?.[key] || 0);
  q('#lost').textContent = String(stream.dropped || 0);
}

// Sensor (x,y) -> canvas (row,col), honouring the orientation selector. Shared
// by the live view, the raw tap, and the tracker box so they stay aligned.
function mapEvent(x: number, y: number) {
  const orientation = q<HTMLSelectElement>('#orientation').value;
  if (orientation === 'native') return {row: x, col: y};
  if (orientation === 'mirror') return {row: 125 - x, col: y};
  return {row: x, col: 111 - y};
}

function stampInto(target: Float32Array, words: Uint32Array) {
  for (const word of words) {
    const x = (word >>> 24) & 0x7f, y = (word >>> 17) & 0x7f, p = word & 1;
    if (x >= 126 || y >= 112) continue;
    const {row, col} = mapEvent(x, y);
    target[(row * 112 + col) * 2 + p] = 1;
  }
}

function paint(target: ImageData, source: Float32Array, palette: string) {
  for (let i=0; i<112*126; i++) {
    const off=source[i*2], on=source[i*2+1], j=i*4;
    if (palette === 'mono') target.data.set([255*Math.max(on,off),255*Math.max(on,off),255*Math.max(on,off),255],j);
    else if (palette === 'heat') target.data.set([255*Math.min(1,on+off),150*on,35*off,255],j);
    // 'signal': colour-blind-safe orange (ON) / light blue (OFF) pair.
    else target.data.set([Math.min(255,245*on+70*off), Math.min(255,150*on+170*off), Math.min(255,30*on+245*off), 255],j);
  }
}

function renderLive() {
  const decay = Number(q<HTMLInputElement>('#decay').value) / 100;
  const palette = q<HTMLSelectElement>('#palette').value;
  if (!paused) {
    for (let i=0; i<energy.length; i++) energy[i] *= decay;
    for (const words of pending.splice(0)) stampInto(energy, words);
  }
  paint(image, energy, palette);
  ctx.putImageData(image,0,0);
}

function renderTrack() {
  const decay = Number(q<HTMLInputElement>('#decay').value) / 100;
  const palette = q<HTMLSelectElement>('#palette').value;
  if (!paused) {
    for (let i=0; i<rawEnergy.length; i++) rawEnergy[i] *= decay;
    for (const words of pendingRaw.splice(0)) stampInto(rawEnergy, words);
  }
  paint(trackImage, rawEnergy, palette);
  trackCtx.putImageData(trackImage,0,0);
  drawTrackOverlay();
}

// --- App view rendering -----------------------------------------------------
// Raw event tap as background (like the track view), then the loaded app's
// decoded overlay. Mayfly replaces the background with its own occupancy world.
function renderApp() {
  const decay = Number(q<HTMLInputElement>('#decay').value) / 100;
  const palette = q<HTMLSelectElement>('#palette').value;
  if (!paused) {
    for (let i=0; i<appEnergy.length; i++) appEnergy[i] *= decay;
    for (const words of pendingApp.splice(0)) stampInto(appEnergy, words);
  }
  if (loadedApp === 'dvs_mayfly') paintMayfly(); else paint(appImage, appEnergy, palette);
  appCtx.putImageData(appImage,0,0);
  const overlay = APP_OVERLAYS[loadedApp];
  if (appActive && overlay) overlay();
}

// dvs_mayfly: the app IS the world bitmap, so paint occupancy directly instead
// of the event background (126x112 world -> canvas via mapEvent).
function paintMayfly() {
  for (let i=0; i<112*126; i++) appImage.data.set([9,17,21,255], i*4);
  for (let cx=0; cx<126; cx++) for (let cy=0; cy<112; cy++) {
    if (!mayflyWorld[cy*126 + cx]) continue;
    const {row, col} = mapEvent(cx, cy);
    if (row < 0 || row >= 126 || col < 0 || col >= 112) continue;
    appImage.data.set([255,205,120,255], (row*112 + col)*4);
  }
}

// 8-octant unit vector (x right, y down); N (dy<0) points up. Shared by the
// stabilize + dir-consensus arrows (matches the mirrors' `dirs` table).
const OCTANT_VEC: [number, number][] = [[1,0],[1,-1],[0,-1],[-1,-1],[-1,0],[-1,1],[0,1],[1,1]];

// Turn a sensor-space direction (dx,dy) into a canvas-space direction, honouring
// the current orientation so the arrow points the way the scene actually moves.
function canvasDir(dx: number, dy: number) {
  const a = mapEvent(0, 0), b = mapEvent(dx, dy);
  return {cdx: b.col - a.col, cdy: b.row - a.row};
}

function drawArrow(cx: number, cy: number, cdx: number, cdy: number, len: number, colour: string) {
  const mag = Math.hypot(cdx, cdy) || 1;
  const ux = cdx/mag, uy = cdy/mag;
  const ex = cx + ux*len, ey = cy + uy*len;
  appCtx.strokeStyle = colour; appCtx.fillStyle = colour; appCtx.lineWidth = 1;
  appCtx.beginPath(); appCtx.moveTo(cx, cy); appCtx.lineTo(ex, ey); appCtx.stroke();
  const head = 3, ang = Math.atan2(uy, ux);
  appCtx.beginPath();
  appCtx.moveTo(ex, ey);
  appCtx.lineTo(ex - head*Math.cos(ang - 0.5), ey - head*Math.sin(ang - 0.5));
  appCtx.lineTo(ex - head*Math.cos(ang + 0.5), ey - head*Math.sin(ang + 0.5));
  appCtx.closePath(); appCtx.fill();
}

// Highlight a grid cell (sensor-space cell of `cellPx` px) on the app canvas,
// mapping its two opposite corners through mapEvent so it stays aligned.
function drawCellHighlight(col: number, row: number, cellPx: number, stroke: string, fill?: string) {
  const a = mapEvent(row*cellPx, col*cellPx);
  const b = mapEvent(row*cellPx + cellPx - 1, col*cellPx + cellPx - 1);
  const left = Math.min(a.col, b.col), right = Math.max(a.col, b.col);
  const top = Math.min(a.row, b.row), bottom = Math.max(a.row, b.row);
  if (fill) { appCtx.fillStyle = fill; appCtx.fillRect(left, top, right-left+1, bottom-top+1); }
  appCtx.lineWidth = 1; appCtx.strokeStyle = stroke;
  appCtx.strokeRect(left + 0.5, top + 0.5, (right-left)+1, (bottom-top)+1);
}

const CENTER = () => mapEvent(63, 56);   // sensor centre -> canvas, for global arrows

const APP_OVERLAYS: Record<string, () => void> = {
  dvs_motion() {
    // 4x4 grid, 32x32-px cells (CELL_SHIFT=5). col=x>>5, row=y>>5.
    const stale = performance.now() - motionCell.at > 1500;
    if (stale && motionCell.at === 0) return;
    const colour = motionCell.motion ? '#ffd23c' : '#5cc7ff';
    drawCellHighlight(motionCell.col, motionCell.row, 32, stale ? '#5f6b73' : colour,
      motionCell.motion && !stale ? 'rgba(255,210,60,0.20)' : undefined);
    q('#app-status').textContent = motionCell.motion ? `motion cell val=${motionCell.val}` : `hot cell val=${motionCell.val}`;
  },
  dvs_oms_meister() {
    // 8x8 grid, 16x16-px cells (best_row/col already >>1 into 0..7).
    const stale = performance.now() - omsCell.at > 1500;
    if (stale && omsCell.at === 0) return;
    const heat = Math.min(1, omsCell.val/255);
    const colour = omsCell.oms ? '#ff6b57' : '#5cc7ff';
    drawCellHighlight(omsCell.col, omsCell.row, 16, stale ? '#5f6b73' : colour,
      omsCell.oms && !stale ? `rgba(255,107,87,${0.15 + 0.4*heat})` : undefined);
    q('#app-status').textContent = omsCell.oms ? `OMS fire val=${omsCell.val}` : `activity val=${omsCell.val}`;
  },
  dvs_stabilize() {
    const c = CENTER();
    const still = stabVec.oct === 7 && stabVec.mag === 0;
    if (still) { q('#app-status').textContent = 'still'; return; }
    const [ux, uy] = OCTANT_VEC[stabVec.oct];
    const {cdx, cdy} = canvasDir(ux, uy);
    drawArrow(c.col, c.row, cdx, cdy, 6*(stabVec.mag+1), '#ffd23c');
    const names = ['E','NE','N','NW','W','SW','S','SE'];
    q('#app-status').textContent = `flow ${names[stabVec.oct]} m=${stabVec.mag} (dx=${stabVec.dx}, dy=${stabVec.dy})`;
  },
  dvs_heartbeats() {
    // 8x7 regions, 16x16-px. turbo-ish colour by period_bin, alpha by conf.
    let painted = 0;
    for (let region=0; region<8*7; region++) {
      const cell = heartGrid[region];
      if (!cell) continue;
      painted++;
      const col = region % 8, row = Math.floor(region / 8);
      const hue = 240 - (cell.bin/7)*240;           // fast=blue-ish -> slow=red
      const alpha = 0.2 + 0.8*(cell.conf/8);
      drawCellHighlight(col, row, 16, `hsla(${hue},70%,60%,${alpha})`,
        `hsla(${hue},70%,50%,${alpha*0.5})`);
    }
    q('#app-status').textContent = painted ? `${painted} region(s) reporting` : 'listening…';
  },
  dvs_oms_dirconsensus() {
    // 8x7 tiles, 16x16-px. Highlight the flagged tile + a global-dir arrow.
    const stale = performance.now() - dirCons.at > 1500;
    if (!(stale && dirCons.at === 0)) {
      if (dirCons.flag) {
        const heat = Math.min(1, dirCons.z/31);
        drawCellHighlight(dirCons.col, dirCons.row, 16, stale ? '#5f6b73' : '#ff6b57',
          stale ? undefined : `rgba(255,107,87,${0.15 + 0.5*heat})`);
      }
      const c = CENTER();
      const [ux, uy] = OCTANT_VEC[dirCons.gdir];
      const {cdx, cdy} = canvasDir(ux, uy);
      drawArrow(c.col, c.row, cdx, cdy, 14, stale ? '#5f6b73' : '#8fd0ff');
    }
    const names = ['E','NE','N','NW','W','SW','S','SE'];
    q('#app-status').textContent = dirCons.flag
      ? `independent-motion tile z=${dirCons.z}, global ${names[dirCons.gdir]}`
      : `global ${names[dirCons.gdir]}`;
  },
  dvs_track() {
    // Reuse the tracker box, drawn on the app canvas (same overlay as track mode).
    if (trackRejected()) { q('#app-status').textContent = 'searching…'; return; }
    const stale = performance.now() - trackBox.at > 1500;
    const a = mapEvent(trackBox.x0, trackBox.y0), b = mapEvent(trackBox.x1, trackBox.y1);
    const left = Math.min(a.col, b.col), right = Math.max(a.col, b.col);
    const top = Math.min(a.row, b.row), bottom = Math.max(a.row, b.row);
    appCtx.lineWidth = 1; appCtx.strokeStyle = stale ? '#5f6b73' : '#c15cff';
    appCtx.strokeRect(left + 0.5, top + 0.5, (right-left)+1, (bottom-top)+1);
    const c = mapEvent(trackBox.cx, trackBox.cy);
    appCtx.fillStyle = stale ? '#5f6b73' : (trackBox.locked ? '#ffffff' : '#aeb9c1');
    appCtx.fillRect(c.col - 1, c.row - 1, 3, 3);
    q('#app-status').textContent = trackBox.locked ? 'locked' : 'tracking';
  },
};

// A window with fewer than this many surviving events is treated as noise and
// its box/centroid rejected. Read live from the slider so no reload is needed.
const minEvents = () => Number(q<HTMLInputElement>('#min-events').value);
const trackRejected = () => trackBox.count <= 0 || trackBox.count < minEvents();

// Overlay the tracker's bounding box + centroid on the raw canvas.
function drawTrackOverlay() {
  if (trackRejected()) return;
  const stale = performance.now() - trackBox.at > 1500;
  const a = mapEvent(trackBox.x0, trackBox.y0), b = mapEvent(trackBox.x1, trackBox.y1);
  const left = Math.min(a.col, b.col), right = Math.max(a.col, b.col);
  const top = Math.min(a.row, b.row), bottom = Math.max(a.row, b.row);
  // Neutral so the box stays legible over the orange/blue event field.
  const borderColour = stale ? '#5f6b73' : '#c15cff';
  const markerColour = stale ? '#5f6b73' : (trackBox.locked ? '#ffffff' : '#aeb9c1');
  trackCtx.lineWidth = 1;
  trackCtx.strokeStyle = borderColour;
  trackCtx.strokeRect(left + 0.5, top + 0.5, (right - left) + 1, (bottom - top) + 1);
  const c = mapEvent(trackBox.cx, trackBox.cy);
  trackCtx.fillStyle = markerColour;
  trackCtx.fillRect(c.col - 1, c.row - 1, 3, 3);
}

function render() {
  if (mode === 'track') renderTrack();
  else if (mode === 'app') renderApp();
  else renderLive();
  const fps = Number(q<HTMLInputElement>('#fps').value);
  setTimeout(() => requestAnimationFrame(render), 1000/fps);
}

q('#pause').onclick = () => { paused=!paused; q('#pause').textContent=paused?'Resume':'Pause'; q('#paused-badge').style.display=paused?'block':'none'; };
q('#clear').onclick = () => { energy.fill(0); rawEnergy.fill(0); appEnergy.fill(0); mayflyWorld.fill(0); };

// Decode dvs_track status words (paired word0/word1) and update the overlay.
function decodeTrack(words: Uint32Array) {
  if (!trackerActive) return;
  decodeTrackWords(words);
}

function decodeTrackWords(words: Uint32Array) {
  for (const w of words) trackTail.push(w >>> 0);
  while (trackTail.length >= 2) {
    // Re-sync pair alignment before consuming. A status word (word0) always has
    // its top 7 bits clear because `locked` is only 0 or 1; a bbox word (word1)
    // with min_x >= 2 does not. So if the head is not a plausible word0 the
    // stream has slipped by one (a dropped UDP packet, or residual words from
    // the previously-loaded firmware) -- drop single words until it realigns,
    // otherwise status and bbox stay swapped and every box decodes wrong.
    if ((trackTail[0] & 0xfe000000) !== 0) { trackTail.shift(); continue; }
    const w0 = trackTail.shift()!, w1 = trackTail.shift()!;
    trackBox = {
      locked: (w0 >>> 24) & 0xff, cx: (w0 >>> 16) & 0xff, cy: (w0 >>> 8) & 0xff, count: w0 & 0xff,
      x0: (w1 >>> 24) & 0xff, y0: (w1 >>> 16) & 0xff, x1: (w1 >>> 8) & 0xff, y1: w1 & 0xff,
      at: performance.now(),
    };
  }
  updateTrackReadout();
}

// --- Per-app result decoders ------------------------------------------------
// Each decoder matches its app's host mirror in chips/fpga/ bit-for-bit. Called
// only when the app view is active AND the loaded firmware is that app, so the
// selected app's layout is applied (mirrors trackerActive gating decodeTrack).
function decodeApp(words: Uint32Array) {
  if (!appActive) return;
  const decoder = APP_DECODERS[loadedApp];
  if (decoder) decoder(words);
}

const APP_DECODERS: Record<string, (words: Uint32Array) => void> = {
  // dvs_motion_view.py unpack_status: motion=(w>>14)&1, val=(w>>6)&0xFF,
  // row=(w>>3)&0x7, col=w&0x7 (4x4 grid).
  dvs_motion(words) {
    for (const w of words) {
      motionCell = {motion: (w >>> 14) & 1, val: (w >>> 6) & 0xff,
                    row: (w >>> 3) & 0x7, col: w & 0x7, at: performance.now()};
    }
  },
  // oms_meister_ref.py: word = (oms<<14)|(val<<6)|(row<<3)|col (8x8 grid; the
  // firmware already packs best_row>>1 / best_col>>1 into 0..7).
  dvs_oms_meister(words) {
    for (const w of words) {
      omsCell = {oms: (w >>> 14) & 1, val: (w >>> 6) & 0xff,
                 row: (w >>> 3) & 0x7, col: w & 0x7, at: performance.now()};
    }
  },
  // dvs_stabilize_view.py unpack_status: sx=(w>>15)&1, mx=(w>>11)&0xF,
  // sy=(w>>10)&1, my=(w>>6)&0xF, oct=(w>>3)&0x7, mag=w&0x7.
  dvs_stabilize(words) {
    for (const w of words) {
      const sx = (w >>> 15) & 1, mx = (w >>> 11) & 0xf;
      const sy = (w >>> 10) & 1, my = (w >>> 6) & 0xf;
      stabVec = {dx: sx ? -mx : mx, dy: sy ? -my : my,
                 oct: (w >>> 3) & 0x7, mag: w & 0x7, at: performance.now()};
    }
  },
  // dvs_mayfly_view.py unpack_step: cx=w&0x7F, cy=(w>>7)&0x7F,
  // new_state=(w>>14)&1, step0=(w>>15)&1. Accumulate toggles into the world.
  dvs_mayfly(words) {
    for (const w of words) {
      const cx = w & 0x7f, cy = (w >>> 7) & 0x7f, newState = (w >>> 14) & 1;
      if (cx < 126 && cy < 112) mayflyWorld[cy * 126 + cx] = newState;
    }
  },
  // dvs_heartbeats_view.py unpack_status: region=w&0x3F, period_bin=(w>>6)&0xF,
  // conf=(w>>10). col=region%8, row=region//8 (8 cols x 7 rows).
  dvs_heartbeats(words) {
    for (const w of words) {
      const region = w & 0x3f;
      if (region >= 8 * 7) continue;
      heartGrid[region] = {bin: (w >>> 6) & 0xf, conf: (w >>> 10) & 0xf};
    }
  },
  // dvs_oms_dirconsensus_ref.py: word = (flag<<14)|(zc<<9)|(row<<6)|(col<<3)|gdir.
  // flag=(w>>14)&1, z=(w>>9)&0x1F, row=(w>>6)&0x7, col=(w>>3)&0x7, gdir=w&0x7.
  dvs_oms_dirconsensus(words) {
    for (const w of words) {
      dirCons = {flag: (w >>> 14) & 1, z: (w >>> 9) & 0x1f,
                 row: (w >>> 6) & 0x7, col: (w >>> 3) & 0x7,
                 gdir: w & 0x7, at: performance.now()};
    }
  },
  // dvs_track handled by decodeTrack in track mode; if selected in the app view
  // its centroid/box words are two-word pairs -- reuse the track decoder so the
  // app view shows the same overlay.
  dvs_track(words) { decodeTrackWords(words); },
};

function updateTrackReadout() {
  const fresh = performance.now() - trackBox.at < 1500;
  const rejected = trackBox.count > 0 && trackBox.count < minEvents();
  q('#track-lock').textContent = !trackerActive ? 'not loaded'
    : (trackBox.count <= 0 || !fresh ? 'idle'
    : rejected ? 'noise (rejected)'
    : (trackBox.locked ? 'locked' : 'searching'));
  q('#track-lock').style.color = trackerActive && trackBox.count > 0 && fresh && !rejected
    ? (trackBox.locked ? '#5fb0e6' : '#e08a3c') : '#7f8d96';
  q('#track-count').textContent = String(trackBox.count);
  q('#track-centroid').textContent = `${trackBox.cx}, ${trackBox.cy}`;
  q('#track-box').textContent = trackBox.count > 0 && !rejected
    ? `${trackBox.x0},${trackBox.y0} → ${trackBox.x1},${trackBox.y1}` : '—';
}

function setMode(next: Mode) {
  mode = next;
  q('#mode-live').classList.toggle('active', next === 'live');
  q('#mode-track').classList.toggle('active', next === 'track');
  q('#mode-app').classList.toggle('active', next === 'app');
  q('#dvs').classList.toggle('hidden', next !== 'live');
  q('#track').classList.toggle('hidden', next !== 'track');
  q('#app').classList.toggle('hidden', next !== 'app');
  q('#system-controls').classList.toggle('hidden', next !== 'live');
  q('#track-controls').classList.toggle('hidden', next !== 'track');
  q('#app-controls').classList.toggle('hidden', next !== 'app');
  // Tracking + apps are view-only modes -- hide the code/blocks/log workbench and
  // let the stage fill the width.
  dashboardMain.classList.toggle('stage-only', next !== 'live');
  q('#stage-label').textContent = next === 'track' ? 'RAW TAP + TRACKER'
    : next === 'app' ? 'RAW TAP + APP' : 'FPGA / CORE OUTPUT';
  if (next === 'track') { rawEnergy.fill(0); pendingRaw.length = 0; trackTail = []; updateTrackReadout(); }
  if (next === 'app') { appEnergy.fill(0); pendingApp.length = 0; appTail = []; }
  // The app + track views both need the raw DVS tap for their background, so the
  // backend enables the raw stream whenever we're not in the plain live view.
  const src = next === 'live' ? stream : rawStream;
  lastWords = src.words; lastPackets = src.packets; lastRateTime = performance.now();
  // The backend only forwards the raw tap to clients whose mode != live; send
  // 'track' for the app view too so the tap is enabled.
  const wire = next === 'app' ? 'track' : next;
  if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type: 'mode', mode: wire}));
}
q('#mode-live').onclick = () => setMode('live');
q('#mode-track').onclick = () => setMode('track');
q('#mode-app').onclick = () => setMode('app');
q('#track-start').onclick = () => firmwareAction('track');
for (const id of ['decay','fps']) q<HTMLInputElement>(`#${id}`).oninput = e => q(`#${id}-value`).textContent = (e.target as HTMLInputElement).value + (id==='decay'?'%':'');
q<HTMLInputElement>('#radius').oninput = e => q('#radius-value').textContent = (e.target as HTMLInputElement).value;
q<HTMLInputElement>('#correlation').oninput = e => q('#correlation-value').textContent = (e.target as HTMLInputElement).value;
q<HTMLInputElement>('#min-events').oninput = e => { q('#min-events-value').textContent = (e.target as HTMLInputElement).value; updateTrackReadout(); };

const ws = new WebSocket(`ws://${location.host}/ws`); ws.binaryType = 'arraybuffer';
ws.onopen = () => ws.send(JSON.stringify({type: 'mode', mode}));
ws.onmessage = event => {
  if (event.data instanceof ArrayBuffer) {
    const frame = new Uint32Array(event.data);
    const body = frame.subarray(1);
    if (frame[0] === STREAM_RAW) {
      // Raw DVS tap feeds the track view (pendingRaw) or the app view (pendingApp).
      if (mode === 'app') pendingApp.push(body); else pendingRaw.push(body);
      rawStream.words += body.length; rawStream.packets += 1;
    } else if (mode === 'track') {
      decodeTrack(body);
    } else if (mode === 'app') {
      decodeApp(body);
    } else {
      pending.push(body);
      stream.words += body.length; stream.packets += 1;
    }
    return;
  }
  const message = JSON.parse(event.data);
  if (message.type === 'init') { board=message.board; stream=message.stream; for(const l of message.logs) appendLog(l.message,l.level); if (message.apps) populateApps(message.apps); updateState(); }
  if (message.type === 'state') { board=message.board; stream={...stream,...message.stream}; updateState(); }
  if (message.type === 'log') appendLog(message.message,message.level);
  if (message.type === 'build') showDiagnostics(message.diagnostics || []);
};
ws.onclose = () => { board.connected=false; updateState(); appendLog('Dashboard connection closed','error'); };

setInterval(() => {
  const now=performance.now(), dt=(now-lastRateTime)/1000;
  const src = mode === 'live' ? stream : rawStream;
  q('#event-rate').textContent=`${Math.round((src.words-lastWords)/dt).toLocaleString()}/s`;
  q('#packet-rate').textContent=`${Math.round((src.packets-lastPackets)/dt).toLocaleString()}/s`;
  lastWords=src.words; lastPackets=src.packets; lastRateTime=now;
},1000);

async function initialize() {
  const source = (await api('source')).source; setSource(source); transformAtBlockSync=transformRegion(source);
  const saved = (await api('blocks')).blocks;
  if (saved) Blockly.serialization.workspaces.load(saved, workspace);
  else {
    const rotation = workspace.newBlock('rotate'); rotation.setFieldValue('45','ANGLE'); rotation.initSvg(); rotation.render(); rotation.moveBy(40,30);
    const clamp = workspace.newBlock('clamp'); clamp.initSvg(); clamp.render(); rotation.nextConnection?.connect(clamp.previousConnection!);
  }
  blocksInitialized = true;
  await regenerateFromBlocks();
  render();
}
initialize().catch(error => appendLog(String(error),'error'));
