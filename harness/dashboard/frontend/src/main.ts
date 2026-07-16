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
// dvs_apophenia: coarse 32-col x 14-row activity grid (mirror of the firmware's
// GRID_COLS x GRID_ROWS). The status words feed this buffer with a gentle host
// decay; the renderer 4-fold mirrors it into a symmetric, breathing inkblot.
const APOPH_COLS = 32, APOPH_ROWS = 14;
const apophGrid = new Float32Array(APOPH_COLS * APOPH_ROWS);   // [yq*COLS + xq]
let apophAt = 0;
// dvs_sonar ("Radial Motion Oracle"): each status word (dominant octant, radius,
// pol, strength, flag) spawns an expanding, fading sonar ring at that polar
// position. We keep a small pool of live rings; the renderer grows + fades each
// frame (mirror of dvs_sonar_view.py's render_sonar). Octant unit vectors reuse
// OCTANT_VEC (defined below). born = performance.now() timestamp.
const SONAR_RADIUS_SHIFT = 1;   // must match RADIUS_SHIFT in software/dvs_sonar/main.c
type SonarRing = {octant: number; radius: number; pol: number; strength: number; flag: number; born: number};
let sonarRings: SonarRing[] = [];
let sonarLast = {octant: 0, radius: 0, pol: 0, strength: 0, flag: 0, at: 0};
// dvs_caustics ("Event-Caustic Refractor"): each status word carries a refracted
// (warped) sample {xr, yr, pol, strength, flag}. We accumulate the samples into
// two decaying float caustic fields (ON/OFF) over the 126x112 sensor frame -- a
// per-frame multiplicative fade + additive cyan splats -- and paint them as
// shimmering underwater light (mirror of dvs_caustics_view.py's accumulate_field /
// underwater_rgb). causticAt = last-update timestamp for the status line.
const CAUSTIC_W = 126, CAUSTIC_H = 112;
const causticON = new Float32Array(CAUSTIC_W * CAUSTIC_H);   // [y*W + x]
const causticOFF = new Float32Array(CAUSTIC_W * CAUSTIC_H);
let causticAt = 0;
let causticLast = {xr: 0, yr: 0, pol: 0, strength: 0, flag: 0};
// dvs_blackhole ("Micro-Event Black Holes"): each status word reports the strongest
// COLLAPSING coarse region {xq, yq, strength, flag} -- where motion was busy then
// abruptly went quiet. We accumulate the REAL (flag=1) collapse cells into two
// decaying float fields over the 126x112 sensor frame: a DARK `well` field (an
// imploding gravity well -- carves darkness) and a bright `ring` field (a
// gravitational-lensing halo). Per frame both fade so wells implode/vanish. Mirror
// of dvs_blackhole_view.py's accumulate_wells / blackhole_rgb. 8-px regions ->
// pixel centre via BH_CELL_PX. blackholeAt = last-update timestamp for the status.
const BH_W = 126, BH_H = 112, BH_CELL_PX = 8;   // BH_CELL_PX = 1<<XQ_SHIFT (8-px regions)
const blackholeWell = new Float32Array(BH_W * BH_H);   // [y*W + x] dark imploding core
const blackholeRing = new Float32Array(BH_W * BH_H);   // [y*W + x] bright lensing halo
let blackholeAt = 0;
let blackholeLast = {xq: 0, yq: 0, strength: 0, flag: 0};
// dvs_flinch ("The Flinch"): each status word carries the looming-detector state
// {flinch, level, cx, cy} (bit-faithful with dvs_flinch_view.py unpack_status). The
// app IS a giant eye: `level` (0..63) dilates the pupil (rising tension), `flinch`
// snaps the lid shut + kicks a screen-shake recoil, and the gaze points toward the
// focus of expansion (cx,cy). flinchShake decays each frame for the recoil; flinchAt
// = last-update timestamp for the status line.
let flinchLast = {flinch: 0, level: 0, cx: 63, cy: 56, at: 0};
let flinchShake = 0;      // screen-shake magnitude, kicked by a flinch, decays per frame
// dvs_loom ("The Finish-Line Loom"): each status word carries one slit-scan
// sample {slit, y, pol, weft, flag} (bit-faithful with dvs_loom_view.py's
// unpack_status; slit=3 is a no-hit sentinel that still carries the live weft
// so the loom keeps advancing). We weave three cloth strips over ON/OFF float
// fields of shape [3 slits][SY=112 warp rows][WEFT_COLS=128 weft columns]
// (mirror of weave_cloth: deposit 1.0 for flagged threads, 0.35 for faint,
// max not sum). When the wrapping weft advances, the column it enters is
// cleared in all strips so the loom overwrites the oldest cloth. loomAt =
// last-update timestamp for the status line.
const LOOM_SY = 112, LOOM_COLS = 128;
const LOOM_SLIT_LABEL = ['x=21', 'x=61', 'x=101'];   // slit centre x labels
const loomON = new Float32Array(3 * LOOM_SY * LOOM_COLS);    // [(slit*SY+y)*COLS+weft]
const loomOFF = new Float32Array(3 * LOOM_SY * LOOM_COLS);
let loomWeft = 0;
let loomAt = 0;
let loomLast = {slit: 3, y: 0, pol: 0, weft: 0, flag: 0};
// dvs_entropy ("Entropy's Bloodhound"): each status word carries the latched
// arrow-of-time state {fwd, rev, verdict, wseq} (bit-faithful with
// dvs_entropy_view.py's unpack_status). fwd counts same-pixel ON->OFF "decay"
// transitions in the last completed window, rev the OFF->ON "kindle" ones;
// verdict = sign of D=fwd-rev outside a MARGIN dead-band (0 undecided,
// 1 time-FORWARD, 2 BACKWARD). We keep one history sample per wseq change
// (i.e. per completed window) for the scrolling D chart; entropyAt = last-
// update timestamp for the status line.
const ENTROPY_HIST = 96;                          // windows kept in the D chart
let entropyHist: {fwd: number, rev: number}[] = [];
let entropyLast = {fwd: 0, rev: 0, verdict: 0, wseq: 0};
let entropyAt = 0;
// dvs_widdershins ("The Widdershins Engine"): each status word carries the
// latched winding state {oct, valid, wind, turns, wseq, radq} (bit-faithful
// with dvs_widdershins_view.py's unpack_status; wind is a 12-bit and turns an
// 8-bit two's-complement field -- sign-extend on decode). wind accumulates
// circular octant differences of a median tracker around frame centre
// (eighth-turns; >0 deosil/clockwise-on-screen, <0 widdershins); turns =
// wind>>3 floor. We keep one history sample per wseq change (i.e. per sample
// period) for the scrolling wind chart; widderAt = last-update timestamp.
const WIDDER_HIST = 112;                          // samples kept in the wind chart (one per band column)
let widderHist: number[] = [];
let widderLast = {oct: 0, valid: 0, wind: 0, turns: 0, wseq: 0, radq: 0};
let widderAt = 0;
// dvs_vital ("The Vitalometer"): each status word carries the latched
// alive-vs-mechanism state {pbin, spread, total, verdict, wseq} (bit-faithful
// with dvs_vital_view.py's unpack_status). spread = #IBI half-octave log-bins
// above the peak>>3 floor in the last completed 1024-event window (metronome
// -> 1-2 bins, living jitter/drift -> many); total = confirmed inter-burst
// intervals in that window; verdict 0=DORMANT, 1=MECHANISM, 2=ALIVE,
// 3=LIMINAL. We keep one history sample per wseq change (i.e. per completed
// window) for the scrolling spread chart; vitalAt = last-update timestamp.
const VITAL_HIST = 112;                           // windows kept in the spread chart (one per band column)
let vitalHist: {spread: number, verdict: number}[] = [];
let vitalLast = {pbin: 0, spread: 0, total: 0, verdict: 0, wseq: 0};
let vitalAt = 0;
// dvs_quartz ("The Human Quartz"): each status word carries the latched
// tap-timing grade {prog, meanq, jit, grade, sseq} (bit-faithful with
// dvs_quartz_view.py's unpack_status). prog = live count of accepted
// inter-tap intervals toward the next grade (0..15); meanq = latched mean
// ITI>>5 (tempo = meanq<<5 ticks); jit = latched MAD jitter in ticks
// (clamped 1023); grade 0=JELLY, 1=MORTAL HAND, 2=METRONOME, 3=QUARTZ;
// sseq = session counter (0 = no measurement yet). We keep one history
// sample per sseq change (i.e. per completed 16-tap measurement) for the
// scrolling jitter chart; quartzAt = last-update timestamp.
const QUARTZ_HIST = 112;                          // sessions kept in the jitter chart (one per band column)
let quartzHist: {jit: number, grade: number}[] = [];
let quartzLast = {prog: 0, meanq: 0, jit: 0, grade: 0, sseq: 0};
let quartzAt = 0;
// dvs_seismo ("Ballroom Seismology"): each status word carries the latched
// oscillation state {disp_q, freqbin, resonance_q, seq} (bit-faithful with
// dvs_seismo_view.py's unpack_status). disp_q is a signed 8-bit displacement
// proxy (two's-complement; host sign-extends via (w & 0xFF) ^ 0x80) - 0x80);
// freqbin 0..31 is a monotone frequency label (0 = no oscillation detected);
// resonance_q 0..1023 is a leaky |D| energy estimate; seq 0..15 is a window
// counter (0 = not yet valid). We keep one history sample per seq change (i.e.
// per completed window) for the scrolling disp_q strip chart; seismoLast holds
// the freshest decoded fields for the resonance bar and status overlay.
const SEISMO_HIST = 112;                          // windows kept in the disp chart
let seismoHist: {disp: number, freqbin: number, resonance: number}[] = [];
let seismoLast = {disp: 0, freqbin: 0, resonance: 0, seq: 0};
let seismoAt = 0;

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
  apophGrid.fill(0); apophAt = 0;
  sonarRings = []; sonarLast = {octant: 0, radius: 0, pol: 0, strength: 0, flag: 0, at: 0};
  causticON.fill(0); causticOFF.fill(0); causticAt = 0;
  causticLast = {xr: 0, yr: 0, pol: 0, strength: 0, flag: 0};
  blackholeWell.fill(0); blackholeRing.fill(0); blackholeAt = 0;
  blackholeLast = {xq: 0, yq: 0, strength: 0, flag: 0};
  flinchLast = {flinch: 0, level: 0, cx: 63, cy: 56, at: 0}; flinchShake = 0;
  loomON.fill(0); loomOFF.fill(0); loomWeft = 0; loomAt = 0;
  loomLast = {slit: 3, y: 0, pol: 0, weft: 0, flag: 0};
  entropyHist = []; entropyLast = {fwd: 0, rev: 0, verdict: 0, wseq: 0}; entropyAt = 0;
  widderHist = []; widderLast = {oct: 0, valid: 0, wind: 0, turns: 0, wseq: 0, radq: 0}; widderAt = 0;
  vitalHist = []; vitalLast = {pbin: 0, spread: 0, total: 0, verdict: 0, wseq: 0}; vitalAt = 0;
  quartzHist = []; quartzLast = {prog: 0, meanq: 0, jit: 0, grade: 0, sseq: 0}; quartzAt = 0;
  seismoHist = []; seismoLast = {disp: 0, freqbin: 0, resonance: 0, seq: 0}; seismoAt = 0;
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
  if (loadedApp === 'dvs_mayfly') paintMayfly();
  else if (loadedApp === 'dvs_apophenia') paintApophenia();
  else if (loadedApp === 'dvs_sonar') paintSonar();
  else if (loadedApp === 'dvs_caustics') paintCaustics(!paused);
  else if (loadedApp === 'dvs_blackhole') paintBlackhole(!paused);
  else if (loadedApp === 'dvs_flinch') paintFlinch(!paused);
  else if (loadedApp === 'dvs_loom') paintLoom();
  else if (loadedApp === 'dvs_entropy') paintEntropy();
  else if (loadedApp === 'dvs_widdershins') paintWiddershins();
  else if (loadedApp === 'dvs_vital') paintVital();
  else if (loadedApp === 'dvs_quartz') paintQuartz();
  else if (loadedApp === 'dvs_seismo') paintSeismo();
  else paint(appImage, appEnergy, palette);
  appCtx.putImageData(appImage,0,0);
  if (loadedApp === 'dvs_sonar' && appActive) drawSonarRings();
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

// dvs_apophenia: the app IS a living Rorschach. Take the coarse 32x14 activity
// grid, 4-fold mirror it (reflect the left half across x, then the top half
// across y) into a symmetric inkblot, then paint it upscaled over the whole
// canvas with a smooth magma-like colormap. Bit-faithful counterpart of
// dvs_apophenia_view.py's mirror4()/render_inkblot(). Replaces the event
// background (like mayfly) so the symmetric shape fills the stage.
function apophColor(t: number): [number, number, number] {
  // Compact magma-ish ramp: black -> deep purple -> magenta -> orange -> pale.
  const stops: [number, number, number, number][] = [
    [0.0,   0,   0,   4],
    [0.25, 60,  15,  90],
    [0.5, 165,  45, 110],
    [0.75,240, 100,  60],
    [1.0, 252, 230, 180],
  ];
  const u = Math.max(0, Math.min(1, t));
  for (let i = 1; i < stops.length; i++) {
    if (u <= stops[i][0]) {
      const a = stops[i-1], b = stops[i];
      const f = (u - a[0]) / (b[0] - a[0] || 1);
      return [a[1] + (b[1]-a[1])*f, a[2] + (b[2]-a[2])*f, a[3] + (b[3]-a[3])*f];
    }
  }
  return [stops[stops.length-1][1], stops[stops.length-1][2], stops[stops.length-1][3]];
}

function paintApophenia() {
  const halfC = APOPH_COLS >> 1, halfR = APOPH_ROWS >> 1;
  // Build the 4-fold mirrored coarse field (same construction as mirror4()):
  // seed = left half of the grid, mirrored onto the right (x-symmetry), then
  // the top half folded onto the bottom (y-symmetry).
  const blot = new Float32Array(APOPH_COLS * APOPH_ROWS);
  let peak = 0;
  for (let yq = 0; yq < APOPH_ROWS; yq++) {
    const syq = yq < halfR ? yq : (APOPH_ROWS - 1 - yq);   // fold rows about the mid-line
    for (let xq = 0; xq < APOPH_COLS; xq++) {
      const sxq = xq < halfC ? xq : (APOPH_COLS - 1 - xq); // fold cols about the mid-line
      const v = apophGrid[syq * APOPH_COLS + sxq];
      blot[yq * APOPH_COLS + xq] = v;
      if (v > peak) peak = v;
    }
  }
  const inv = peak > 0 ? 1 / peak : 0;

  // Paint the 112(w) x 126(h) canvas by sampling the coarse blot with bilinear
  // interpolation so the inkblot is smooth, not blocky. Canvas index is
  // (row*112 + col); map each pixel to grid coords (col->xq axis, row->yq axis).
  for (let row = 0; row < 126; row++) {
    const gy = (row / 125) * (APOPH_ROWS - 1);
    const y0 = Math.floor(gy), y1 = Math.min(APOPH_ROWS - 1, y0 + 1), fy = gy - y0;
    for (let col = 0; col < 112; col++) {
      const gx = (col / 111) * (APOPH_COLS - 1);
      const x0 = Math.floor(gx), x1 = Math.min(APOPH_COLS - 1, x0 + 1), fx = gx - x0;
      const v00 = blot[y0*APOPH_COLS + x0], v01 = blot[y0*APOPH_COLS + x1];
      const v10 = blot[y1*APOPH_COLS + x0], v11 = blot[y1*APOPH_COLS + x1];
      const top = v00 + (v01 - v00)*fx, bot = v10 + (v11 - v10)*fx;
      const v = (top + (bot - top)*fy) * inv;
      const [r, g, b] = apophColor(v);
      appImage.data.set([r, g, b, 255], (row*112 + col)*4);
    }
  }
}

// dvs_sonar: the app IS a radar/sonar oracle. Each emitted (octant, radius) ping
// spawns a ring at that polar birth point (out from the sensor centre) that grows
// outward and fades over its lifetime -- a living oracle display. Bit-faithful
// counterpart of dvs_sonar_view.py's render_sonar(): birth point from the compass
// octant vector scaled by the de-quantized radius, hue by octant, ON/OFF by
// polarity. paintSonar() clears to a dark radar backdrop; drawSonarRings() strokes
// the live rings as arcs on the app canvas.
const SONAR_RING_LIFE_MS = 1400;   // how long a ring lives before fading out
const SONAR_RING_SPEED = 45;       // canvas px a ring expands per second

// Polar (octant, radius) -> canvas birth point (col,row), out from centre. Mirror
// of dvs_sonar_view.py's ripple_position(): de-quantize the radius (<<SHIFT back to
// sensor px), place it along the octant's unit vector from the sensor centre, and
// map through mapEvent so it honours the orientation selector.
function sonarBirthPoint(octant: number, radius: number) {
  const [ux, uy] = OCTANT_VEC[octant];
  const mag = Math.hypot(ux, uy) || 1;
  const rPx = radius << SONAR_RADIUS_SHIFT;           // back to sensor-pixel scale
  const sx = 63 + (ux / mag) * rPx;                   // sensor-space birth point
  const sy = 56 + (uy / mag) * rPx;
  const {row, col} = mapEvent(Math.round(sx), Math.round(sy));
  return {col, row};
}

function sonarColor(octant: number, pol: number): string {
  // Hue spun around the wheel by octant; ON warm / OFF cool via lightness+shift.
  const hue = (octant / 8) * 360;
  const light = pol ? 62 : 48;
  return `hsl(${hue},80%,${light}%)`;
}

function paintSonar() {
  // Dark radar backdrop with a faint centre glow so the rings read clearly.
  const c = mapEvent(63, 56);
  for (let row = 0; row < 126; row++) {
    for (let col = 0; col < 112; col++) {
      const d = Math.hypot(col - c.col, row - c.row);
      const glow = Math.max(0, 1 - d / 90);
      const r = 6 + 10 * glow, g = 14 + 18 * glow, b = 20 + 28 * glow;
      appImage.data.set([r, g, b, 255], (row * 112 + col) * 4);
    }
  }
}

function drawSonarRings() {
  const now = performance.now();
  // Retire dead rings, then stroke each survivor as an expanding, fading arc.
  sonarRings = sonarRings.filter(ring => now - ring.born <= SONAR_RING_LIFE_MS);
  const c = mapEvent(63, 56);
  for (const ring of sonarRings) {
    const age = (now - ring.born) / 1000;             // seconds alive
    const fade = 1 - (now - ring.born) / SONAR_RING_LIFE_MS;   // 1 -> 0
    if (fade <= 0) continue;
    const {col, row} = sonarBirthPoint(ring.octant, ring.radius);
    const ringR = SONAR_RING_SPEED * age;             // grows over time
    const amp = fade * (0.3 + 0.7 * (ring.strength / 31)) * (ring.flag ? 1 : 0.4);
    appCtx.globalAlpha = Math.max(0, Math.min(1, amp));
    appCtx.strokeStyle = sonarColor(ring.octant, ring.pol);
    appCtx.lineWidth = ring.flag ? 1.5 : 1;
    appCtx.beginPath();
    appCtx.arc(col, row, ringR, 0, 2 * Math.PI);
    appCtx.stroke();
  }
  appCtx.globalAlpha = 1;
}

// dvs_caustics: the app IS a shimmering underwater caustic field. The decoder
// splats each refracted sample into the ON/OFF float fields; here we apply a gentle
// per-frame multiplicative fade (so the water ripples/shimmers even between splats)
// and paint the two fields with a blue/cyan "underwater light" colormap -- OFF ->
// deep blue, ON -> bright cyan. Bit-faithful counterpart of dvs_caustics_view.py's
// underwater_rgb(). Replaces the event background (like mayfly/sonar) so the liquid
// light fills the stage. `advance` gates the fade so a paused view holds still.
const CAUSTIC_FRAME_DECAY = 0.94;   // per-frame field fade (shimmer between splats)
function paintCaustics(advance: boolean) {
  if (advance) {
    for (let i = 0; i < causticON.length; i++) { causticON[i] *= CAUSTIC_FRAME_DECAY; causticOFF[i] *= CAUSTIC_FRAME_DECAY; }
  }
  // Peak-normalise so the field stays legible regardless of stream rate.
  let peak = 0;
  for (let i = 0; i < causticON.length; i++) { const t = causticON[i] + causticOFF[i]; if (t > peak) peak = t; }
  const inv = peak > 0 ? 1 / peak : 0;
  // Paint the 112(w) x 126(h) canvas by sampling the 126(w) x 112(h) sensor field
  // through mapEvent so it honours the orientation selector. Canvas index is
  // (row*112 + col).
  for (let sy = 0; sy < CAUSTIC_H; sy++) {
    for (let sx = 0; sx < CAUSTIC_W; sx++) {
      const idx = sy * CAUSTIC_W + sx;
      const on = causticON[idx] * inv, off = causticOFF[idx] * inv;
      const t = Math.min(1, on + off);
      // Underwater gradient: dark navy backdrop -> cyan-white as intensity rises;
      // ON warms/brightens toward cyan, OFF deepens the blue.
      let r = 0.02 + 0.20 * on + 0.05 * t;
      let g = 0.06 + 0.55 * t + 0.30 * on;
      let b = 0.12 + 0.85 * t + 0.10 * off;
      const veil = Math.min(1, t * 3.0);
      r = 0.01 * (1 - veil) + r * veil;
      g = 0.03 * (1 - veil) + g * veil;
      b = 0.07 * (1 - veil) + b * veil;
      const {row, col} = mapEvent(sx, sy);
      if (row < 0 || row >= 126 || col < 0 || col >= 112) continue;
      appImage.data.set([r * 255, g * 255, b * 255, 255], (row * 112 + col) * 4);
    }
  }
}

// dvs_blackhole: the app IS a field of imploding gravity wells. The decoder deepens
// a dark `well` and a bright lensing `ring` at each real collapse cell; here we apply
// a per-frame multiplicative fade (so wells implode/vanish between hits) and paint
// them over a dim deep-space backdrop -- wells SUBTRACT light (carve darkness), rings
// ADD a cool blue-white shimmer. Bit-faithful counterpart of dvs_blackhole_view.py's
// blackhole_rgb(). Replaces the event background (like mayfly/caustics) so the wells
// fill the stage. `advance` gates the fade so a paused view holds still.
const BH_FRAME_DECAY = 0.94;   // per-frame field fade (wells implode between collapses)
function paintBlackhole(advance: boolean) {
  if (advance) {
    for (let i = 0; i < blackholeWell.length; i++) { blackholeWell[i] *= BH_FRAME_DECAY; blackholeRing[i] *= BH_FRAME_DECAY; }
  }
  // Peak-normalise each field so wells/rings stay legible regardless of stream rate.
  let wpeak = 0, rpeak = 0;
  for (let i = 0; i < blackholeWell.length; i++) {
    if (blackholeWell[i] > wpeak) wpeak = blackholeWell[i];
    if (blackholeRing[i] > rpeak) rpeak = blackholeRing[i];
  }
  const winv = wpeak > 0 ? 1 / wpeak : 0, rinv = rpeak > 0 ? 1 / rpeak : 0;
  for (let sy = 0; sy < BH_H; sy++) {
    for (let sx = 0; sx < BH_W; sx++) {
      const idx = sy * BH_W + sx;
      const wn = Math.min(1, blackholeWell[idx] * winv);
      const rn = Math.min(1, blackholeRing[idx] * rinv);
      // Dim deep-space backdrop; wells multiply it down toward black (implosion),
      // then the cool blue-white lensing ring is added on top.
      const dark = 1 - 0.95 * wn;
      let r = 0.04 * dark + 0.55 * rn;
      let g = 0.05 * dark + 0.75 * rn;
      let b = 0.09 * dark + 1.00 * rn;
      const {row, col} = mapEvent(sx, sy);
      if (row < 0 || row >= 126 || col < 0 || col >= 112) continue;
      appImage.data.set([Math.min(255, r * 255), Math.min(255, g * 255), Math.min(255, b * 255), 255], (row * 112 + col) * 4);
    }
  }
}

// dvs_flinch: the app IS a giant eye. The decoder just latches the freshest
// {flinch, level, cx, cy}; here we DRAW the eye straight into the display buffer
// (126 rows x 112 cols). `level` (0..63) sets the iris tension + pupil dilation, the
// gaze points toward the mapped focus (cx,cy), and a flinch snaps the lid shut over a
// red flash while `flinchShake` kicks a screen-shake recoil that decays per frame.
// Counterpart of dvs_flinch_view.py's eye_rgb(). `advance` gates the shake decay so a
// paused view holds still. Replaces the event background (like caustics/blackhole).
const FLINCH_SHAKE_DECAY = 0.80;
function paintFlinch(advance: boolean) {
  const H = 126, W = 112;                  // display buffer geometry (row=x, col=y)
  const ecx0 = W / 2, ecy0 = H / 2;
  // Screen-shake: a small deterministic wobble that decays each frame.
  if (advance) flinchShake *= FLINCH_SHAKE_DECAY;
  if (flinchShake < 0.05) flinchShake = 0;
  const t = performance.now() / 40;
  const shx = flinchShake * Math.sin(t * 1.7);
  const shy = flinchShake * Math.cos(t * 2.3) * 0.6;
  const ecx = ecx0 + shx, ecy = ecy0 + shy;

  const level = flinchLast.level, flinch = flinchLast.flinch;
  const tension = level / 63;
  // Gaze: map the sensor-space focus (cx,cy) into display space, offset from centre.
  const focus = mapEvent(flinchLast.cx, flinchLast.cy);   // {row,col} in [0,126)x[0,112)
  const gx = ecx + (focus.col - ecy0) * 0.5;
  const gy = ecy + (focus.row - ecx0) * 0.5;

  const eyeR = Math.min(W, H) * 0.46;
  const irisR = eyeR * 0.5;
  const pupilR = eyeR * (0.12 + 0.30 * tension);   // pupil dilates with level
  const fresh = performance.now() - flinchLast.at < 2000;

  for (let row = 0; row < H; row++) {
    for (let col = 0; col < W; col++) {
      const dx = col - ecx, dy = row - ecy;
      const rEye = Math.sqrt(dx * dx + dy * dy);
      const sclera = Math.max(0, Math.min(1, 1 - (rEye - eyeR) / 3));
      const rpx = col - gx, rpy = row - gy;
      const rp = Math.sqrt(rpx * rpx + rpy * rpy);
      const iris = Math.max(0, Math.min(1, 1 - (rp - irisR) / 4)) * sclera;
      const pupil = Math.max(0, Math.min(1, 1 - (rp - pupilR) / 2)) * sclera;

      // Dark backdrop -> off-white sclera -> amber/red iris -> black pupil.
      let r = 0.03, g = 0.03, b = 0.05;
      r = r * (1 - sclera) + 0.92 * sclera;
      g = g * (1 - sclera) + 0.90 * sclera;
      b = b * (1 - sclera) + 0.85 * sclera;
      const irR = 0.60 + 0.40 * tension, irG = 0.45 * (1 - tension) + 0.10, irB = 0.10;
      r = r * (1 - iris) + irR * iris;
      g = g * (1 - iris) + irG * iris;
      b = b * (1 - iris) + irB * iris;
      r *= (1 - pupil); g *= (1 - pupil); b *= (1 - pupil);

      // Flinch: the lid slams shut (a horizontal slit) over a red flash.
      if (fresh && flinch) {
        const lid = Math.max(0, Math.min(1, 1 - Math.abs(row - ecy) / (H * 0.06)));
        r = r * lid + 0.25 * (1 - lid);
        g = g * lid + 0.02 * (1 - lid);
        b = b * lid + 0.02 * (1 - lid);
      }
      appImage.data.set([Math.min(255, r * 255), Math.min(255, g * 255), Math.min(255, b * 255), 255], (row * W + col) * 4);
    }
  }
}

// dvs_loom: the app IS three woven cloth strips (one per slit), stacked
// vertically on the 112(w) x 126(h) display buffer: canvas col = warp row (y,
// 0..111 -- exactly the canvas width), and each strip's 42 canvas rows span the
// 128 weft columns (time). Each canvas row covers ~3 weft columns; we take the
// MAX over that span so no thread is skipped by subsampling. ON threads warm
// gold, OFF threads indigo, faint (flag=0) threads carry their 0.35 weight
// already; the strip rows containing the live weft column are gently lit as
// the shuttle cursor. Counterpart of dvs_loom_view.py's render_loom(). The
// cloth is an abstract time-weave (not spatially registered), so like
// apophenia it ignores the orientation selector.
const LOOM_STRIP_H = 42;   // 3 strips * 42 rows = 126 canvas rows
function paintLoom() {
  for (let strip = 0; strip < 3; strip++) {
    for (let r = 0; r < LOOM_STRIP_H; r++) {
      const row = strip * LOOM_STRIP_H + r;
      // Weft span covered by this canvas row (nearest-span, max-sampled).
      const w0 = Math.floor(r * LOOM_COLS / LOOM_STRIP_H);
      const w1 = Math.min(LOOM_COLS, Math.floor((r + 1) * LOOM_COLS / LOOM_STRIP_H));
      const cursor = loomWeft >= w0 && loomWeft < w1;
      const edge = r === LOOM_STRIP_H - 1;   // seam between strips
      for (let col = 0; col < 112; col++) {
        const base = (strip * LOOM_SY + col) * LOOM_COLS;
        let on = 0, off = 0;
        for (let w = w0; w < w1; w++) {
          if (loomON[base + w] > on) on = loomON[base + w];
          if (loomOFF[base + w] > off) off = loomOFF[base + w];
        }
        // Dark loom backdrop; gold ON thread / indigo OFF thread by max.
        let rC = Math.max(0.05, 1.00 * on, 0.35 * off);
        let gC = Math.max(0.04, 0.78 * on, 0.40 * off);
        let bC = Math.max(0.07, 0.25 * on, 0.95 * off);
        if (cursor) { rC = Math.min(1, rC + 0.10); gC = Math.min(1, gC + 0.09); bC = Math.min(1, bC + 0.04); }
        if (edge) { rC *= 0.5; gC *= 0.5; bC *= 0.5; }
        appImage.data.set([rC * 255, gC * 255, bC * 255, 255], (row * 112 + col) * 4);
      }
    }
  }
}

// dvs_entropy: the app IS a thermodynamic verdict gauge, drawn on the
// 112(w) x 126(h) display buffer: rows 0..89 are a scrolling per-window
// D = fwd-rev history chart (newest window at the bottom, gold bars right of
// the centre spine for D>0 / indigo left for D<0, dim ticks at the +/-MARGIN
// dead-band); the lower rows are two horizontal bar meters of the latched
// window counts (gold = fwd "decay" ON->OFF, indigo = rev "kindle" OFF->ON,
// scale 0..1023). The arrow-of-time needle itself is vector-drawn by the
// dvs_entropy overlay in the seam between chart and meters. Counterpart of
// dvs_entropy_view.py's render_entropy(). Like apophenia/loom this is an
// abstract gauge (not spatially registered), so it ignores the orientation
// selector.
const ENTROPY_MARGIN = 16, ENTROPY_CAP = 1023;
const ENTROPY_CHART_ROWS = 90;   // rows 0..89: one completed window per row
function paintEntropy() {
  for (let i = 0; i < 112 * 126; i++) appImage.data.set([13, 11, 16, 255], i * 4);
  const px = (row: number, col: number, r: number, g: number, b: number) => {
    if (col >= 0 && col < 112) appImage.data.set([r, g, b, 255], (row * 112 + col) * 4);
  };
  const mid = 56, half = 54;   // centre spine column + max bar half-length
  const mTick = Math.max(1, Math.round(ENTROPY_MARGIN * half / ENTROPY_CAP));
  for (let r = 0; r < ENTROPY_CHART_ROWS; r++) {
    px(r, mid, 40, 36, 52);                                    // centre spine
    px(r, mid + mTick, 30, 28, 40); px(r, mid - mTick, 30, 28, 40);  // dead-band
    const h = entropyHist.length - ENTROPY_CHART_ROWS + r;     // newest at bottom
    if (h < 0) continue;
    const D = entropyHist[h].fwd - entropyHist[h].rev;
    if (D === 0) continue;
    const len = Math.max(1, Math.min(half, Math.round(Math.abs(D) * half / ENTROPY_CAP)));
    const decisive = Math.abs(D) >= ENTROPY_MARGIN;            // inside the dead-band = dim
    for (let k = 1; k <= len; k++) {
      if (D > 0) px(r, mid + k, decisive ? 232 : 90, decisive ? 184 : 74, decisive ? 75 : 40);
      else px(r, mid - k, decisive ? 90 : 45, decisive ? 95 : 47, decisive ? 212 : 90);
    }
  }
  // Bar meters of the latched window counts (0..1023 across the full width).
  const fLen = Math.round(entropyLast.fwd * 111 / ENTROPY_CAP);
  const rLen = Math.round(entropyLast.rev * 111 / ENTROPY_CAP);
  for (let row = 98; row <= 106; row++) {
    px(row, 0, 60, 50, 30); px(row, 111, 60, 50, 30);          // track ends
    for (let col = 0; col <= fLen; col++) px(row, col, 232, 184, 75);
  }
  for (let row = 114; row <= 122; row++) {
    px(row, 0, 35, 36, 70); px(row, 111, 35, 36, 70);
    for (let col = 0; col <= rLen; col++) px(row, col, 90, 95, 212);
  }
}

// dvs_widdershins: the app IS a brass compass + winding gauge, drawn on the
// 112(w) x 126(h) display buffer: a dial ring centred at (56,50) with dim
// octant-boundary ticks (octant 0 = East/right, index increasing clockwise on
// screen, matching the firmware's y-down octant classifier), and a scrolling
// per-sample wind history band along the bottom (newest at the right, gold
// above the centre line for deosil/positive wind, indigo below for
// widdershins/negative, scale +/-1023). The needle itself is vector-drawn by
// the dvs_widdershins overlay. Counterpart of dvs_widdershins_view.py's
// render_widdershins(). Like entropy/loom this is an abstract gauge (not
// spatially registered), so it ignores the orientation selector.
const WIDDER_CX = 56, WIDDER_CY = 50, WIDDER_R = 40;   // dial centre + radius
const WIDDER_CAP = 1023;                               // |wind| full-scale
const WIDDER_CHART_Y = 110, WIDDER_CHART_HALF = 14;    // history band centre row + half-height
function paintWiddershins() {
  for (let i = 0; i < 112 * 126; i++) appImage.data.set([13, 11, 16, 255], i * 4);
  const px = (row: number, col: number, r: number, g: number, b: number) => {
    if (row >= 0 && row < 126 && col >= 0 && col < 112)
      appImage.data.set([r, g, b, 255], (row * 112 + col) * 4);
  };
  // Dial ring.
  for (let row = WIDDER_CY - WIDDER_R - 2; row <= WIDDER_CY + WIDDER_R + 2; row++)
    for (let col = WIDDER_CX - WIDDER_R - 2; col <= WIDDER_CX + WIDDER_R + 2; col++) {
      const r = Math.hypot(col - WIDDER_CX, row - WIDDER_CY);
      if (Math.abs(r - WIDDER_R) < 1.2) px(row, col, 51, 45, 64);
    }
  // Octant-boundary ticks at 0, 45, 90, ... degrees (E, SE, S, ... on screen).
  for (let k = 0; k < 8; k++) {
    const a = k * Math.PI / 4, ux = Math.cos(a), uy = Math.sin(a);
    for (let t = WIDDER_R - 5; t < WIDDER_R - 1; t++)
      px(Math.round(WIDDER_CY + uy * t), Math.round(WIDDER_CX + ux * t), 70, 62, 88);
  }
  // Scrolling wind history band (one column per sample, newest at the right).
  for (let col = 0; col < 112; col++) px(WIDDER_CHART_Y, col, 40, 36, 52);   // centre line
  for (let col = 0; col < 112; col++) {
    const h = widderHist.length - 112 + col;
    if (h < 0) continue;
    const wind = widderHist[h];
    if (wind === 0) continue;
    const len = Math.max(1, Math.min(WIDDER_CHART_HALF,
      Math.round(Math.abs(wind) * WIDDER_CHART_HALF / WIDDER_CAP)));
    for (let k = 1; k <= len; k++) {
      if (wind > 0) px(WIDDER_CHART_Y - k, col, 232, 184, 75);   // deosil: gold, up
      else px(WIDDER_CHART_Y + k, col, 90, 95, 212);             // widdershins: indigo, down
    }
  }
}

// dvs_vital: the app IS a séance gauge, drawn on the 112(w) x 126(h) display
// buffer: a big verdict lamp (filled disc) at the top coloured by the latched
// verdict (DORMANT=dim, MECHANISM=steel, ALIVE=green, LIMINAL=gold), a
// scrolling per-window spread history chart below it (one column per window,
// newest at the right, bar height = spread 0..32 growing upward, coloured by
// that window's own verdict, dim guide rows at the SPREAD_MECH and
// SPREAD_ALIVE thresholds), and a bottom bar meter of the latched confirmed-
// IBI total (scale 0..255). Counterpart of dvs_vital_view.py's render_vital().
// Like entropy/widdershins this is an abstract gauge (not spatially
// registered), so it ignores the orientation selector.
const VITAL_MECH = 2, VITAL_ALIVE = 5;                 // spread verdict thresholds
const VITAL_TOTAL_CAP = 255;                           // |total| full-scale
const VITAL_LAMP_Y = 34, VITAL_LAMP_X = 56, VITAL_LAMP_R = 24;   // lamp centre + radius
const VITAL_CHART_BASE = 105, VITAL_CHART_ROWS = 32;   // chart baseline row + height (1 row per spread unit)
const VITAL_COLOURS: [number, number, number][] =      // per-verdict RGB
  [[85, 85, 102], [138, 148, 166], [95, 212, 138], [232, 184, 75]];
function paintVital() {
  for (let i = 0; i < 112 * 126; i++) appImage.data.set([13, 11, 16, 255], i * 4);
  const px = (row: number, col: number, r: number, g: number, b: number) => {
    if (row >= 0 && row < 126 && col >= 0 && col < 112)
      appImage.data.set([r, g, b, 255], (row * 112 + col) * 4);
  };
  // Verdict lamp: filled disc + thin rim, coloured by the latched verdict.
  const [lr, lg, lb] = VITAL_COLOURS[vitalLast.verdict];
  for (let row = VITAL_LAMP_Y - VITAL_LAMP_R - 1; row <= VITAL_LAMP_Y + VITAL_LAMP_R + 1; row++)
    for (let col = VITAL_LAMP_X - VITAL_LAMP_R - 1; col <= VITAL_LAMP_X + VITAL_LAMP_R + 1; col++) {
      const r = Math.hypot(col - VITAL_LAMP_X, row - VITAL_LAMP_Y);
      if (r < VITAL_LAMP_R - 1) px(row, col, lr >> 1, lg >> 1, lb >> 1);   // dimmed fill
      else if (r < VITAL_LAMP_R + 0.5) px(row, col, lr, lg, lb);           // bright rim
    }
  // Spread history chart: guide rows at the two thresholds, then one column
  // per completed window (newest at the right), bar height = spread.
  for (let col = 0; col < 112; col++) {
    px(VITAL_CHART_BASE, col, 40, 36, 52);                          // baseline
    px(VITAL_CHART_BASE - VITAL_MECH, col, 60, 64, 74);             // MECHANISM guide
    px(VITAL_CHART_BASE - VITAL_ALIVE, col, 46, 84, 62);            // ALIVE guide
  }
  for (let col = 0; col < 112; col++) {
    const h = vitalHist.length - 112 + col;
    if (h < 0) continue;
    const {spread, verdict} = vitalHist[h];
    if (spread === 0) continue;
    const [br, bg, bb] = VITAL_COLOURS[verdict];
    const len = Math.min(VITAL_CHART_ROWS, spread);
    for (let k = 1; k <= len; k++) px(VITAL_CHART_BASE - k, col, br, bg, bb);
  }
  // Confirmed-IBI total meter (0..255 across the full width).
  const tLen = Math.round(vitalLast.total * 111 / VITAL_TOTAL_CAP);
  for (let row = 114; row <= 120; row++) {
    px(row, 0, 60, 50, 30); px(row, 111, 60, 50, 30);               // track ends
    for (let col = 0; col <= tLen; col++) px(row, col, 232, 184, 75);
  }
}

// dvs_quartz: the app IS an oscillator-grade certificate, drawn on the 112(w)
// x 126(h) display buffer: a crystal glyph (filled diamond) at the top
// coloured by the latched grade (JELLY=dim, MORTAL HAND=steel,
// METRONOME=indigo, QUARTZ=gold; grey until the first measurement), a
// 16-pip progress row for the live prog field (accepted inter-tap intervals
// toward the next grade), a scrolling per-session jitter history chart (one
// column per completed 16-tap measurement, newest at the right, bar height
// log-scaled, coloured by that session's grade, dim guide rows at the
// J_QUARTZ/J_STEADY/J_MORTAL grade thresholds), and a bottom tempo meter of
// the latched mean ITI (meanq, scale 0..2047). Counterpart of
// dvs_quartz_view.py's render_quartz(). Like entropy/widdershins/vital this
// is an abstract gauge (not spatially registered), so it ignores the
// orientation selector.
const QUARTZ_JQ = 16, QUARTZ_JS = 64, QUARTZ_JM = 256;   // jitter grade thresholds
const QUARTZ_CX = 56, QUARTZ_CY = 32, QUARTZ_R = 22;     // crystal centre + half-diagonal
const QUARTZ_PIP_Y = 64;                                 // progress pip row
const QUARTZ_CHART_BASE = 105, QUARTZ_CHART_ROWS = 32;   // chart baseline row + height
const QUARTZ_MEANQ_CAP = 2047;                           // tempo meter full-scale
const QUARTZ_COLOURS: [number, number, number][] =       // per-grade RGB (0=JELLY..3=QUARTZ)
  [[85, 85, 102], [138, 148, 166], [90, 95, 212], [232, 184, 75]];
// Log-scaled bar height for a jitter value (0..1023 -> 1..32 rows); the same
// mapping places the three threshold guide rows.
const quartzBarH = (jit: number) =>
  Math.max(1, Math.min(QUARTZ_CHART_ROWS, Math.round(Math.log2(jit + 1) * 3.2)));
function paintQuartz() {
  for (let i = 0; i < 112 * 126; i++) appImage.data.set([13, 11, 16, 255], i * 4);
  const px = (row: number, col: number, r: number, g: number, b: number) => {
    if (row >= 0 && row < 126 && col >= 0 && col < 112)
      appImage.data.set([r, g, b, 255], (row * 112 + col) * 4);
  };
  // Crystal glyph: filled diamond (|dr|+|dc| < R) + bright rim, coloured by
  // the latched grade; grey until the first completed measurement (sseq==0).
  const [cr, cg, cb] = quartzLast.sseq === 0 ? [70, 70, 80] : QUARTZ_COLOURS[quartzLast.grade];
  for (let dr = -QUARTZ_R; dr <= QUARTZ_R; dr++)
    for (let dc = -QUARTZ_R; dc <= QUARTZ_R; dc++) {
      const m = Math.abs(dr) + Math.abs(dc);
      if (m < QUARTZ_R - 1) px(QUARTZ_CY + dr, QUARTZ_CX + dc, cr >> 1, cg >> 1, cb >> 1);
      else if (m <= QUARTZ_R) px(QUARTZ_CY + dr, QUARTZ_CX + dc, cr, cg, cb);
    }
  // Progress pips: 16 pips, 3 px wide, lit gold while collecting (p < prog).
  for (let p = 0; p < 16; p++) {
    const lit = p < quartzLast.prog;
    for (let k = 0; k < 3; k++)
      px(QUARTZ_PIP_Y, 8 + p * 6 + k, lit ? 232 : 40, lit ? 184 : 36, lit ? 75 : 52);
  }
  // Jitter history chart: baseline + log-scaled guide rows at the three grade
  // thresholds, then one column per completed session (newest at the right).
  for (let col = 0; col < 112; col++) {
    px(QUARTZ_CHART_BASE, col, 40, 36, 52);                             // baseline
    px(QUARTZ_CHART_BASE - quartzBarH(QUARTZ_JQ), col, 74, 62, 34);     // QUARTZ guide
    px(QUARTZ_CHART_BASE - quartzBarH(QUARTZ_JS), col, 46, 48, 84);     // METRONOME guide
    px(QUARTZ_CHART_BASE - quartzBarH(QUARTZ_JM), col, 60, 64, 74);     // MORTAL guide
  }
  for (let col = 0; col < 112; col++) {
    const h = quartzHist.length - 112 + col;
    if (h < 0) continue;
    const {jit, grade} = quartzHist[h];
    const [br, bg, bb] = QUARTZ_COLOURS[grade];
    const len = quartzBarH(jit);
    for (let k = 1; k <= len; k++) px(QUARTZ_CHART_BASE - k, col, br, bg, bb);
  }
  // Latched-tempo meter (meanq 0..2047 across the full width).
  const tLen = Math.round(quartzLast.meanq * 111 / QUARTZ_MEANQ_CAP);
  for (let row = 114; row <= 120; row++) {
    px(row, 0, 60, 50, 30); px(row, 111, 60, 50, 30);                   // track ends
    for (let col = 0; col <= tLen; col++) px(row, col, 232, 184, 75);
  }
}

// dvs_seismo: the app IS a seismograph drawn on the 112(w) x 126(h) display
// buffer. Layout (mirrors dvs_seismo_view.py's render_seismo style):
//   rows 0..79  -- seismograph strip chart: scrolling disp_q history, newest
//                  at the right; bars grow up/down from the centre line (row 40)
//                  in teal when no oscillation is detected or amber when
//                  freqbin>0 (oscillation); centre baseline drawn in dim grey.
//   rows 86..105 -- resonance bar: height proportional to resonance_q (0..1023),
//                   coloured teal (quiet) or amber (freqbin>0), with a dashed
//                   dim guide row at the MIN_RES_VALID threshold (scaled).
//   rows 108..125 -- solid border/frame at the very bottom (visual anchor).
// Like vital/widdershins this is an abstract gauge; ignores orientation.
const SEISMO_BG: [number, number, number]       = [11, 13, 18];
const SEISMO_TEAL: [number, number, number]     = [79, 196, 196];
const SEISMO_AMBER: [number, number, number]    = [232, 168, 58];
const SEISMO_DIM: [number, number, number]      = [42, 46, 56];
const SEISMO_CENTRE_ROW = 40;                   // baseline row for disp strip
const SEISMO_STRIP_HALF = 36;                   // half-height of strip (±36 rows)
const SEISMO_RES_BASE = 105;                    // bottom row of resonance bar
const SEISMO_RES_HEIGHT = 20;                   // max bar height in rows
const SEISMO_MIN_RES_GUIDE = 64;               // MIN_RES_VALID from firmware
function paintSeismo() {
  for (let i = 0; i < 112 * 126; i++) appImage.data.set([...SEISMO_BG, 255], i * 4);
  const px = (row: number, col: number, r: number, g: number, b: number) => {
    if (row >= 0 && row < 126 && col >= 0 && col < 112)
      appImage.data.set([r, g, b, 255], (row * 112 + col) * 4);
  };
  // Baseline (centre line of strip chart)
  for (let col = 0; col < 112; col++) px(SEISMO_CENTRE_ROW, col, ...SEISMO_DIM);
  // Resonance bar track baseline
  for (let col = 0; col < 112; col++) px(SEISMO_RES_BASE, col, ...SEISMO_DIM);
  // Resonance MIN_RES_VALID guide row (dashed)
  const guideRow = SEISMO_RES_BASE - Math.round(SEISMO_MIN_RES_GUIDE * SEISMO_RES_HEIGHT / 1023);
  for (let col = 0; col < 112; col += 4) px(guideRow, col, ...SEISMO_DIM);
  // Strip chart: one column per window, newest at the right
  for (let col = 0; col < 112; col++) {
    const h = seismoHist.length - 112 + col;
    if (h < 0) continue;
    const {disp, freqbin, resonance} = seismoHist[h];
    const [cr, cg, cb] = freqbin > 0 ? SEISMO_AMBER : SEISMO_TEAL;
    // bar height proportional to |disp| (disp in -128..127; scale to ±STRIP_HALF)
    const bar = Math.round(Math.abs(disp) * SEISMO_STRIP_HALF / 128);
    if (disp >= 0) {
      for (let k = 1; k <= bar; k++) px(SEISMO_CENTRE_ROW - k, col, cr, cg, cb);
    } else {
      for (let k = 1; k <= bar; k++) px(SEISMO_CENTRE_ROW + k, col, cr, cg, cb);
    }
    // Also render a faint resonance column beneath the strip
    const rBar = Math.round(resonance * SEISMO_RES_HEIGHT / 1023);
    const [rr, rg, rb] = freqbin > 0 ? SEISMO_AMBER : SEISMO_TEAL;
    for (let k = 1; k <= rBar; k++) px(SEISMO_RES_BASE - k, col, rr >> 1, rg >> 1, rb >> 1);
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
  dvs_apophenia() {
    // The inkblot IS the render (paintApophenia paints the background); no cell
    // overlay. Just report liveness + current peak so the status line moves.
    let peak = 0;
    for (let i = 0; i < apophGrid.length; i++) if (apophGrid[i] > peak) peak = apophGrid[i];
    const fresh = performance.now() - apophAt < 1500;
    q('#app-status').textContent = (fresh && peak > 0)
      ? `inkblot alive, peak=${Math.round(peak)}` : 'listening…';
  },
  dvs_sonar() {
    // The rings ARE the render (paintSonar + drawSonarRings draw the oracle); no
    // cell overlay. Just report the latest ping so the status line moves.
    const names = ['E','NE','N','NW','W','SW','S','SE'];
    const fresh = performance.now() - sonarLast.at < 1500;
    q('#app-status').textContent = (fresh && sonarLast.flag)
      ? `ping ${names[sonarLast.octant]} r=${sonarLast.radius} str=${sonarLast.strength} ${sonarLast.pol ? 'ON' : 'OFF'}`
      : (fresh ? `faint ${names[sonarLast.octant]} r=${sonarLast.radius}` : 'listening…');
  },
  dvs_caustics() {
    // The caustic field IS the render (paintCaustics paints the background); no
    // cell overlay. Just report the latest refracted splat so the status moves.
    const fresh = performance.now() - causticAt < 1500;
    q('#app-status').textContent = (fresh && causticLast.flag)
      ? `caustic (${causticLast.xr},${causticLast.yr}) str=${causticLast.strength} ${causticLast.pol ? 'ON' : 'OFF'}`
      : (fresh ? `faint (${causticLast.xr},${causticLast.yr})` : 'listening…');
  },
  dvs_blackhole() {
    // The imploding wells ARE the render (paintBlackhole paints the background); no
    // cell overlay. Just report the latest collapse so the status line moves.
    const fresh = performance.now() - blackholeAt < 1500;
    q('#app-status').textContent = (fresh && blackholeLast.flag)
      ? `black hole @(${blackholeLast.xq},${blackholeLast.yq}) depth=${blackholeLast.strength}`
      : (fresh ? `faint collapse @(${blackholeLast.xq},${blackholeLast.yq})` : 'listening…');
  },
  dvs_flinch() {
    // The giant eye IS the render (paintFlinch paints the background); no cell
    // overlay. Just report the looming state so the status line moves.
    const fresh = performance.now() - flinchLast.at < 1500;
    q('#app-status').textContent = (fresh && flinchLast.flinch)
      ? `FLINCH! looming @(${flinchLast.cx},${flinchLast.cy})`
      : (fresh ? `tension ${flinchLast.level}/63 @(${flinchLast.cx},${flinchLast.cy})` : 'watching…');
  },
  dvs_loom() {
    // The woven cloth IS the render (paintLoom paints the background); no cell
    // overlay. Just report the latest thread sample so the status line moves.
    const fresh = performance.now() - loomAt < 1500;
    q('#app-status').textContent = !fresh ? 'warping the loom…'
      : loomLast.slit === 3 ? `weft ${loomLast.weft} (no thread)`
      : `${loomLast.flag ? 'thread' : 'faint thread'} slit ${LOOM_SLIT_LABEL[loomLast.slit]} `
        + `y=${loomLast.y} ${loomLast.pol ? 'ON' : 'OFF'} @weft ${loomLast.weft}`;
  },
  dvs_entropy() {
    // The verdict gauge IS the render (paintEntropy paints the chart + meters);
    // here we vector-draw the arrow-of-time needle in the seam between the
    // history chart and the bar meters, and report the verdict.
    const fresh = performance.now() - entropyAt < 1500;
    if (fresh && entropyLast.verdict === 1) drawArrow(40, 93, 1, 0, 32, '#e8b84b');
    else if (fresh && entropyLast.verdict === 2) drawArrow(72, 93, -1, 0, 32, '#5a5fd4');
    const D = entropyLast.fwd - entropyLast.rev;
    const sD = D >= 0 ? `+${D}` : `${D}`;
    q('#app-status').textContent = !fresh ? 'sniffing the arrow of time…'
      : entropyLast.verdict === 1 ? `TIME RUNS FORWARD (D=${sD}: fwd=${entropyLast.fwd}, rev=${entropyLast.rev})`
      : entropyLast.verdict === 2 ? `TIME RUNS BACKWARD (D=${sD}: fwd=${entropyLast.fwd}, rev=${entropyLast.rev})`
      : `undecided (D=${sD}, window ${entropyLast.wseq})`;
  },
  dvs_widdershins() {
    // The compass + wind chart ARE the render (paintWiddershins paints the
    // background); here we vector-draw the needle at the last valid octant's
    // centre angle (45*oct + 22.5 degrees from East, clockwise on screen since
    // the firmware classifies with y down), and report the winding state.
    const fresh = performance.now() - widderAt < 1500;
    if (fresh && widderLast.valid) {
      const a = (45 * widderLast.oct + 22.5) * Math.PI / 180;
      drawArrow(WIDDER_CX, WIDDER_CY, Math.cos(a), Math.sin(a), WIDDER_R - 8, '#e8b84b');
    }
    const w = widderLast.wind, sW = w >= 0 ? `+${w}` : `${w}`;
    const sT = widderLast.turns >= 0 ? `+${widderLast.turns}` : `${widderLast.turns}`;
    q('#app-status').textContent = !fresh ? 'winding the engine…'
      : w >= 8 ? `DEOSIL wind=${sW} (${sT} turns)`
      : w <= -8 ? `WIDDERSHINS wind=${sW} (${sT} turns)`
      : widderLast.valid ? `unwound (wind=${sW}, oct ${widderLast.oct})`
      : `unwound (wind=${sW}, still)`;
  },
  dvs_vital() {
    // The lamp + spread chart ARE the render (paintVital paints the background);
    // no cell overlay. When freshly ALIVE, vector-draw a soft pulsing halo ring
    // around the lamp so life visibly breathes; then report the verdict.
    const fresh = performance.now() - vitalAt < 1500;
    if (fresh && vitalLast.verdict === 2) {
      const pulse = 3 + 2 * Math.sin(performance.now() / 300);
      appCtx.strokeStyle = 'rgba(95,212,138,0.6)'; appCtx.lineWidth = 1;
      appCtx.beginPath();
      appCtx.arc(VITAL_LAMP_X, VITAL_LAMP_Y, VITAL_LAMP_R + pulse, 0, 2 * Math.PI);
      appCtx.stroke();
    }
    const names = ['DORMANT', 'MECHANISM', 'ALIVE', 'LIMINAL'];
    q('#app-status').textContent = !fresh ? 'taking the pulse…'
      : vitalLast.verdict === 0 ? `DORMANT (only ${vitalLast.total} confirmed bursts)`
      : `${names[vitalLast.verdict]} (spread=${vitalLast.spread} bins, ${vitalLast.total} IBIs, peak bin ${vitalLast.pbin})`;
  },
  dvs_quartz() {
    // The crystal + jitter chart ARE the render (paintQuartz paints the
    // background); no cell overlay. When freshly graded QUARTZ, vector-draw a
    // soft pulsing gold ring around the crystal so the certificate gleams;
    // then report the grade / collection progress.
    const fresh = performance.now() - quartzAt < 1500;
    if (fresh && quartzLast.sseq !== 0 && quartzLast.grade === 3) {
      const pulse = 3 + 2 * Math.sin(performance.now() / 300);
      appCtx.strokeStyle = 'rgba(232,184,75,0.6)'; appCtx.lineWidth = 1;
      appCtx.beginPath();
      appCtx.arc(QUARTZ_CX, QUARTZ_CY, QUARTZ_R + pulse, 0, 2 * Math.PI);
      appCtx.stroke();
    }
    const names = ['JELLY', 'MORTAL HAND', 'METRONOME', 'QUARTZ'];
    q('#app-status').textContent = !fresh ? 'listening for taps…'
      : quartzLast.sseq === 0 ? `collecting taps (${quartzLast.prog}/16 intervals)`
      : `${names[quartzLast.grade]} (jitter=${quartzLast.jit} ticks, tempo=${quartzLast.meanq << 5} ticks, ${quartzLast.prog}/16 toward next)`;
  },
  dvs_seismo() {
    // The seismograph strip + resonance bar ARE the render (paintSeismo paints
    // the background); here we just report the text status line.
    const fresh = performance.now() - seismoAt < 1500;
    q('#app-status').textContent = !fresh ? 'watching the edge…'
      : seismoLast.seq === 0 ? 'collecting first window…'
      : seismoLast.freqbin > 0
        ? `SWAYING freqbin=${seismoLast.freqbin} resonance=${seismoLast.resonance} disp=${seismoLast.disp}`
        : `still (resonance=${seismoLast.resonance} disp=${seismoLast.disp})`;
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
  // dvs_apophenia_view.py unpack_status: xq=w&0x1F, yq=(w>>5)&0xF, val=(w>>9)&0xFF,
  // flag=(w>>17)&1. Feed the coarse grid with a gentle host decay (mirror of
  // accumulate_grid): a real peak (flag=1) writes its value, a below-threshold
  // fallback report nudges its cell at half weight so the field never fully dies.
  dvs_apophenia(words) {
    for (const w of words) {
      const xq = w & 0x1f, yq = (w >>> 5) & 0xf, val = (w >>> 9) & 0xff, flag = (w >>> 17) & 1;
      if (xq >= APOPH_COLS || yq >= APOPH_ROWS) continue;
      for (let i = 0; i < apophGrid.length; i++) apophGrid[i] *= 0.90;
      const idx = yq * APOPH_COLS + xq;
      const nudged = flag ? val : val * 0.5;
      if (nudged > apophGrid[idx]) apophGrid[idx] = nudged;
      apophAt = performance.now();
    }
  },
  // dvs_sonar_view.py unpack_status: octant=w&0x7, radius=(w>>3)&0x1F,
  // pol=(w>>8)&1, strength=(w>>9)&0x1F, flag=(w>>14)&1. Each word spawns an
  // expanding sonar ring (drawn by drawSonarRings); keep the last for the status.
  dvs_sonar(words) {
    for (const w of words) {
      const octant = w & 0x7, radius = (w >>> 3) & 0x1f, pol = (w >>> 8) & 1;
      const strength = (w >>> 9) & 0x1f, flag = (w >>> 14) & 1;
      sonarRings.push({octant, radius, pol, strength, flag, born: performance.now()});
      // Cap the ring pool so a fast stream can't grow it without bound; dead rings
      // are already retired each frame in drawSonarRings.
      if (sonarRings.length > 256) sonarRings.splice(0, sonarRings.length - 256);
      sonarLast = {octant, radius, pol, strength, flag, at: performance.now()};
    }
  },
  // dvs_caustics_view.py unpack_status: xr=w&0x7F, yr=(w>>7)&0x7F, pol=(w>>14)&1,
  // strength=(w>>15)&0x1F, flag=(w>>20)&1. Each word splats a refracted sample into
  // the ON/OFF caustic fields (additive gaussian, brightness by strength/flag);
  // paintCaustics fades + paints them. Mirror of accumulate_field().
  dvs_caustics(words) {
    for (const w of words) {
      const xr = w & 0x7f, yr = (w >>> 7) & 0x7f, pol = (w >>> 14) & 1;
      const strength = (w >>> 15) & 0x1f, flag = (w >>> 20) & 1;
      if (xr >= CAUSTIC_W || yr >= CAUSTIC_H) continue;
      const amp = (0.25 + 0.75 * (strength / 31)) * (flag ? 1 : 0.35);
      // Additive 3x3 gaussian-ish splat around (xr,yr) into the polarity field.
      const field = pol ? causticON : causticOFF;
      for (let dy = -2; dy <= 2; dy++) {
        const yy = yr + dy;
        if (yy < 0 || yy >= CAUSTIC_H) continue;
        for (let dx = -2; dx <= 2; dx++) {
          const xx = xr + dx;
          if (xx < 0 || xx >= CAUSTIC_W) continue;
          const g = Math.exp(-(dx * dx + dy * dy) / (2 * 2.2 * 2.2));
          field[yy * CAUSTIC_W + xx] += amp * g;
        }
      }
      causticLast = {xr, yr, pol, strength, flag};
      causticAt = performance.now();
    }
  },
  // dvs_blackhole_view.py unpack_status: xq=w&0xF, yq=(w>>4)&0xF, strength=(w>>8)&0x1F,
  // flag=(w>>13)&1. Each REAL (flag=1) word carves a dark imploding WELL (negative
  // gaussian) at the region centre plus a bright gravitational-lensing RING shell just
  // outside it; paintBlackhole fades + paints them. Mirror of accumulate_wells().
  dvs_blackhole(words) {
    for (const w of words) {
      const xq = w & 0xf, yq = (w >>> 4) & 0xf, strength = (w >>> 8) & 0x1f, flag = (w >>> 13) & 1;
      if (xq >= 16 || yq >= 14) continue;
      blackholeLast = {xq, yq, strength, flag};
      blackholeAt = performance.now();
      if (!flag) continue;   // only real black holes carve a well
      const cx = xq * BH_CELL_PX + BH_CELL_PX / 2;
      const cy = yq * BH_CELL_PX + BH_CELL_PX / 2;
      const depth = 0.25 + 0.75 * (strength / 31);
      const core = 5.0, halo = 8.0;   // dark-core sigma / lensing-ring radius (px)
      // Splat over a window big enough to hold the lensing ring (radius ~halo).
      for (let dy = -12; dy <= 12; dy++) {
        const yy = Math.round(cy) + dy;
        if (yy < 0 || yy >= BH_H) continue;
        for (let dx = -12; dx <= 12; dx++) {
          const xx = Math.round(cx) + dx;
          if (xx < 0 || xx >= BH_W) continue;
          const r2 = (xx - cx) * (xx - cx) + (yy - cy) * (yy - cy);
          // Dark imploding core (negative gaussian) into the well field.
          blackholeWell[yy * BH_W + xx] += Math.exp(-r2 / (2 * core * core)) * depth;
          // Bright lensing ring: a thin gaussian shell at radius `halo`.
          const rr = Math.sqrt(r2);
          blackholeRing[yy * BH_W + xx] += Math.exp(-((rr - halo) * (rr - halo)) / (2 * 2 * 2)) * depth;
        }
      }
    }
  },
  // dvs_flinch_view.py unpack_status: flinch=w&1, level=(w>>1)&0x3F, cx=(w>>7)&0x7F,
  // cy=(w>>14)&0x7F. The eye is drawn by paintFlinch; here we just latch the freshest
  // state and kick the screen-shake on a flinch pulse. Mirror of unpack_status().
  dvs_flinch(words) {
    for (const w of words) {
      const flinch = w & 1, level = (w >>> 1) & 0x3f;
      const cx = (w >>> 7) & 0x7f, cy = (w >>> 14) & 0x7f;
      flinchLast = {flinch, level, cx, cy, at: performance.now()};
      if (flinch) flinchShake = 5;   // kick the recoil (decays in paintFlinch)
    }
  },
  // dvs_loom_view.py unpack_status: slit=w&3, y=(w>>2)&0x7F, pol=(w>>9)&1,
  // weft=(w>>10)&0x7F, flag=(w>>17)&1. slit=3 is a no-hit sentinel that still
  // carries the live weft. Mirror of weave_cloth(): deposit max(1.0 flagged /
  // 0.35 faint) at [slit, y, weft]; when the wrapping weft advances, clear the
  // column it enters in all strips so the loom overwrites the oldest cloth.
  dvs_loom(words) {
    for (const w of words) {
      const slit = w & 3, y = (w >>> 2) & 0x7f, pol = (w >>> 9) & 1;
      const weft = (w >>> 10) & 0x7f, flag = (w >>> 17) & 1;
      if (weft !== loomWeft) {
        loomWeft = weft;
        for (let s = 0; s < 3; s++) for (let yy = 0; yy < LOOM_SY; yy++) {
          const idx = (s * LOOM_SY + yy) * LOOM_COLS + weft;
          loomON[idx] = 0; loomOFF[idx] = 0;
        }
      }
      loomLast = {slit, y, pol, weft, flag};
      loomAt = performance.now();
      if (slit === 3 || y >= LOOM_SY) continue;   // sentinel only advances the weft
      const idx = (slit * LOOM_SY + y) * LOOM_COLS + weft;
      const wgt = flag ? 1.0 : 0.35;
      const field = pol ? loomON : loomOFF;
      if (wgt > field[idx]) field[idx] = wgt;
    }
  },
  // dvs_entropy_view.py unpack_status: fwd=w&0x3FF, rev=(w>>10)&0x3FF,
  // verdict=(w>>20)&3, wseq=(w>>22)&0xF. fwd/rev are the LATCHED counts of the
  // last completed window, re-emitted every batch; wseq only changes when a new
  // window latches, so we push one history sample per wseq change (mirror of
  // render_entropy()'s per-window history collection) and always keep the
  // freshest word for the gauge/status.
  dvs_entropy(words) {
    for (const w of words) {
      const fwd = w & 0x3ff, rev = (w >>> 10) & 0x3ff;
      const verdict = (w >>> 20) & 3, wseq = (w >>> 22) & 0xf;
      if (wseq !== entropyLast.wseq) {
        entropyHist.push({fwd, rev});
        if (entropyHist.length > ENTROPY_HIST) entropyHist.splice(0, entropyHist.length - ENTROPY_HIST);
      }
      entropyLast = {fwd, rev, verdict, wseq};
      entropyAt = performance.now();
    }
  },
  // dvs_widdershins_view.py unpack_status: oct=w&7, valid=(w>>3)&1,
  // wind=(w>>4)&0xFFF sign-extended from 12 bits, turns=(w>>16)&0xFF
  // sign-extended from 8 bits, wseq=(w>>24)&0xF, radq=(w>>28)&0xF. The latched
  // state is re-emitted every batch; wseq only changes when a new sample fires,
  // so we push one wind history sample per wseq change (mirror of
  // render_widdershins()'s per-sample history collection) and always keep the
  // freshest word for the needle/status.
  dvs_widdershins(words) {
    for (const w of words) {
      const oct = w & 7, valid = (w >>> 3) & 1;
      let wind = (w >>> 4) & 0xfff; if (wind >= 2048) wind -= 4096;
      let turns = (w >>> 16) & 0xff; if (turns >= 128) turns -= 256;
      const wseq = (w >>> 24) & 0xf, radq = (w >>> 28) & 0xf;
      if (wseq !== widderLast.wseq) {
        widderHist.push(wind);
        if (widderHist.length > WIDDER_HIST) widderHist.splice(0, widderHist.length - WIDDER_HIST);
      }
      widderLast = {oct, valid, wind, turns, wseq, radq};
      widderAt = performance.now();
    }
  },
  // dvs_vital_view.py unpack_status: pbin=w&0x1F, spread=(w>>5)&0x3F,
  // total=(w>>11)&0xFF, verdict=(w>>19)&3, wseq=(w>>21)&0xF. The latched
  // window stats are re-emitted every batch; wseq only changes when a new
  // window latches, so we push one history sample per wseq change (mirror of
  // render_vital()'s per-window history collection) and always keep the
  // freshest word for the lamp/status.
  dvs_vital(words) {
    for (const w of words) {
      const pbin = w & 0x1f, spread = (w >>> 5) & 0x3f, total = (w >>> 11) & 0xff;
      const verdict = (w >>> 19) & 3, wseq = (w >>> 21) & 0xf;
      if (wseq !== vitalLast.wseq) {
        vitalHist.push({spread, verdict});
        if (vitalHist.length > VITAL_HIST) vitalHist.splice(0, vitalHist.length - VITAL_HIST);
      }
      vitalLast = {pbin, spread, total, verdict, wseq};
      vitalAt = performance.now();
    }
  },
  // dvs_seismo_view.py unpack_status: disp_u8=w&0xFF (sign-extend: (u^0x80)-0x80),
  // freqbin=(w>>8)&0x1F, resonance_q=(w>>13)&0x3FF, seq=(w>>23)&0xF. The latched
  // window fields are re-emitted every batch; seq only changes when a new window
  // latches, so we push one history sample per seq change (mirror of
  // render_seismo()'s per-window history collection) and always keep the freshest
  // word for the strip chart / resonance bar / status. seq=0 means not yet valid.
  dvs_seismo(words) {
    for (const w of words) {
      const disp_u8 = w & 0xff;
      const disp = ((disp_u8 ^ 0x80) - 0x80) | 0;   // sign-extend 8-bit two's-complement
      const freqbin = (w >>> 8) & 0x1f;
      const resonance = (w >>> 13) & 0x3ff;
      const seq = (w >>> 23) & 0xf;
      if (seq !== seismoLast.seq) {
        seismoHist.push({disp, freqbin, resonance});
        if (seismoHist.length > SEISMO_HIST) seismoHist.splice(0, seismoHist.length - SEISMO_HIST);
      }
      seismoLast = {disp, freqbin, resonance, seq};
      seismoAt = performance.now();
    }
  },
  // dvs_quartz_view.py unpack_status: prog=w&0xF, meanq=(w>>4)&0x7FF,
  // jit=(w>>15)&0x3FF, grade=(w>>25)&3, sseq=(w>>27)&0xF. The latched grade
  // is re-emitted every batch (prog is live); sseq only changes when a new
  // 16-tap measurement latches, so we push one history sample per sseq change
  // (mirror of render_quartz()'s per-session history collection) and always
  // keep the freshest word for the crystal/status.
  dvs_quartz(words) {
    for (const w of words) {
      const prog = w & 0xf, meanq = (w >>> 4) & 0x7ff, jit = (w >>> 15) & 0x3ff;
      const grade = (w >>> 25) & 3, sseq = (w >>> 27) & 0xf;
      if (sseq !== quartzLast.sseq) {
        quartzHist.push({jit, grade});
        if (quartzHist.length > QUARTZ_HIST) quartzHist.splice(0, quartzHist.length - QUARTZ_HIST);
      }
      quartzLast = {prog, meanq, jit, grade, sseq};
      quartzAt = performance.now();
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
