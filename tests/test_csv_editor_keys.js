// CSV 편집 모달의 ↓↑ 이동·블록 선택·Ctrl+D 아래로 채우기를 가짜 DOM으로 검사한다.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(){ this.listeners = {}; this.style = {}; this.dataset = {}; this.value = ''; this.textContent = '';
    const cls = this.cls = new Set();
    this.classList = {toggle: (n, on) => on ? cls.add(n) : cls.delete(n), contains: n => cls.has(n)}; }
  addEventListener(type, fn){ (this.listeners[type] ||= []).push(fn); }
  fire(type, e){ for(const fn of this.listeners[type] || []) fn(e); }
}
// 표 tbody: innerHTML 대신 입력 칸을 직접 만든다
const tbody = new Element();
tbody.inputs = [];
tbody.querySelectorAll = () => tbody.inputs;
tbody.querySelector = sel => {
  const [, r, c] = sel.match(/data-row="(\d+)"\]\[data-ci="(\d+)"/);
  return tbody.inputs.find(i => i.dataset.row === r && i.dataset.ci === c) || null;
};
const ids = new Map([['csv-editor-tbody', tbody]]);
const el = id => { if(!ids.has(id)) ids.set(id, new Element()); return ids.get(id); };
const context = vm.createContext({console, escapeHtml: String,
  document: {getElementById: el, querySelector: el, addEventListener(){}}});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/js/04a-csv-formula.js'), 'utf8'), context);
const tools = fs.readFileSync(path.join(__dirname, '../static/js/04-newjob-tools.js'), 'utf8');
vm.runInContext(tools.slice(tools.indexOf('let csvEditorSchema'), tools.indexOf('async function fetchComfyStatus')), context);
const run = code => vm.runInContext(code, context);

let focused = null;
function setup(rows){
  run(`csvEditorSchema = [{name: 'a'}, {name: 'b'}]; csvEditorSel = null;`);
  context.rowsIn = rows;
  run(`csvEditorRows = rowsIn.map(([a, b]) => ({a, b}));`);
  tbody.inputs = [];
  rows.forEach((row, r) => row.forEach((v, c) => {
    const input = new Element();
    Object.assign(input.dataset, {row: String(r), ci: String(c)});
    input.value = v; input.selectionStart = input.selectionEnd = v.length;
    input.closest = () => input;
    input.parentElement = new Element();
    input.focus = () => { focused = input; };
    input.setSelectionRange = (s, e) => { input.selectionStart = s; input.selectionEnd = e; };
    tbody.inputs.push(input);
  }));
}
const cell = (r, c) => tbody.querySelector(`input[data-row="${r}"][data-ci="${c}"]`);
function key(r, c, k, mods = {}){
  const e = {key: k, target: cell(r, c), prevented: false, preventDefault(){ this.prevented = true; }, ...mods};
  run('csvEditorKeydown')(e);
  return e;
}
const values = () => JSON.parse(run('JSON.stringify(csvEditorRows.map(r => [r.a, r.b]))'));

// ↓↑ 이동: 같은 열, 끝에서는 멈춤
setup([['1', 'x'], ['2', 'y'], ['3', 'z']]);
assert.equal(key(0, 1, 'ArrowDown').prevented, true);
assert.equal(focused, cell(1, 1));
assert.equal(focused.selectionStart, 1);
key(1, 1, 'ArrowUp'); assert.equal(focused, cell(0, 1));
key(2, 0, 'ArrowDown'); assert.equal(focused, cell(2, 0));
// IME 조합 중에는 무시
focused = null;
assert.equal(key(0, 0, 'ArrowDown', {isComposing: true}).prevented, false);
assert.equal(focused, null);

// Shift+↓로 블록 선택 → Ctrl+D는 열마다 맨 위 값을 아래로
setup([['top', 'T'], ['', 'm'], ['old', ''], ['keep', 'K']]);
key(0, 0, 'ArrowDown', {shiftKey: true});
key(0, 0, 'ArrowDown', {shiftKey: true});
key(0, 0, 'ArrowRight', {shiftKey: true});   // 커서가 칸 끝 → 오른쪽 열로 늘림
assert.deepEqual(JSON.parse(run('JSON.stringify(csvEditorSelRect())')), {r1: 0, r2: 2, c1: 0, c2: 1});
assert.equal(cell(2, 1).parentElement.classList.contains('csv-cell-selected'), true);
assert.equal(cell(3, 0).parentElement.classList.contains('csv-cell-selected'), false);
const e = key(0, 0, 'd', {ctrlKey: true});
assert.equal(e.prevented, true);
assert.deepEqual(values(), [['top', 'T'], ['top', 'T'], ['top', 'T'], ['keep', 'K']]);
assert.equal(cell(2, 1).value, 'T');

// Shift+← 는 커서가 칸 처음이 아니면 텍스트 선택으로 둔다
setup([['ab', 'c']]);
assert.equal(key(0, 1, 'ArrowLeft', {shiftKey: true}).prevented, false);

// 한 칸 Ctrl+D(mac Cmd+D)는 바로 위 칸 값을 가져온다
setup([['up', 'U'], ['', 'v']]);
assert.equal(key(1, 1, 'D', {metaKey: true}).prevented, true);
assert.deepEqual(values(), [['up', 'U'], ['', 'U']]);
// 첫 행 한 칸은 바꿀 것이 없다
key(0, 0, 'd', {ctrlKey: true});
assert.deepEqual(values(), [['up', 'U'], ['', 'U']]);

// 마우스 드래그로 블록 선택
setup([['a1', 'b1'], ['a2', 'b2'], ['a3', 'b3']]);
tbody.fire('mousedown', {target: cell(0, 1), button: 0});
tbody.fire('mouseover', {target: cell(2, 1)});
run('csvEditorDragging = false');
key(1, 1, 'd', {ctrlKey: true});
assert.deepEqual(values(), [['a1', 'b1'], ['a2', 'b1'], ['a3', 'b1']]);
console.log('ok');
