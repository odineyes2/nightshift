// CSV 편집 모달의 수식(참조·CONCAT·TEXTJOIN·&)·Ctrl+D 참조 이동·저장을 가짜 DOM으로 검사한다.
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
const tbody = new Element();
tbody.inputs = [];
tbody.querySelectorAll = () => tbody.inputs;
tbody.querySelector = sel => {
  const [, r, c] = sel.match(/data-row="(\d+)"\]\[data-ci="(\d+)"/);
  return tbody.inputs.find(i => i.dataset.row === r && i.dataset.ci === c) || null;
};
const ids = new Map([['csv-editor-tbody', tbody]]);
const el = id => { if(!ids.has(id)) ids.set(id, new Element()); return ids.get(id); };
const files = [];
class FakeFile { constructor(parts, name){ this.text = async () => parts.join(''); this.name = name; } }
const context = vm.createContext({console, escapeHtml: String, File: FakeFile,
  qualityCsvFiles: new WeakMap(), qualityCsvCopies: new WeakMap(), selectedFiles: {csv: null},
  wizardQuality: {}, templatesById: {t: {id: 't', label: 'T', csv_columns: [{name: 'a'}, {name: 'b'}, {name: 'c'}]}},
  templateSelect: {value: 't'},
  setCsvFile: f => { files.push(f); context.selectedFiles.csv = f; },
  document: {getElementById: el, querySelector: el, addEventListener(){}, activeElement: null}});
const src = name => fs.readFileSync(path.join(__dirname, '../static/js', name), 'utf8');
vm.runInContext(src('04a-csv-formula.js'), context);
const tools = src('04-newjob-tools.js');
vm.runInContext(tools.slice(tools.indexOf('let csvEditorSchema'), tools.indexOf('async function fetchComfyStatus')), context);
const run = code => vm.runInContext(code, context);

// 순수 엔진: 헤더가 1행, 첫 데이터 행이 2행
const schema = [{name: 'a'}, {name: 'b'}, {name: 'c'}, {name: 'd'}];
const evalRows = rows => JSON.parse(JSON.stringify(context.evaluateCsvFormulas(schema,
  rows.map(r => Object.fromEntries(schema.map((s, i) => [s.name, r[i] || '']))))));
let out = evalRows([['x', 'cat', 'dog', ''], ['=$B$2', '=B2&", "&C2', '=CONCAT(A2,B2)', '=TEXTJOIN(", ",B2:D2)']]);
assert.deepEqual(out.errors, []);
assert.deepEqual(Object.values(out.rows[1]), ['cat', 'cat, dog', 'xcat', 'cat, dog']);
assert.equal(evalRows([['=A1']]).rows[0].a, 'a');                    // 헤더 참조
assert.equal(evalRows([['=Z9']]).rows[0].a, '');                     // 범위 밖은 빈 값
out = evalRows([['=B2', '=A2'], ['=CONCAT(', '=1+2']]);
assert.deepEqual(out.errors.map(e => e.address + e.value), ['A2#순환', 'B2#순환', 'A3#오류', 'B3#오류']);
assert.equal(evalRows([['=concat("a""b", "c")']]).rows[0].a, 'a"bc');

// shiftFormulaRows: $ 없는 행만, 문자열 안은 그대로
assert.equal(context.shiftFormulaRows('=A2&$B$2&C$2&$D2&"E2"', 2), '=A4&$B$2&C$2&$D4&"E2"');
assert.equal(context.shiftFormulaRows('plain A2', 3), 'plain A2');

// 편집기: 표시·Ctrl+D 참조 이동
function setup(rows){
  run(`csvEditorSchema = [{name: 'a'}, {name: 'b'}, {name: 'c'}]; csvEditorSel = null;`);
  context.rowsIn = rows;
  run(`csvEditorRows = rowsIn.map(([a, b, c]) => ({a, b, c}));`);
  tbody.inputs = [];
  rows.forEach((row, r) => row.forEach((v, c) => {
    const input = new Element();
    Object.assign(input.dataset, {row: String(r), ci: String(c), col: 'abc'[c]});
    input.value = v; input.selectionStart = input.selectionEnd = v.length;
    input.closest = () => input;
    input.parentElement = new Element();
    input.focus = () => {};
    input.setSelectionRange = () => {};
    tbody.inputs.push(input);
  }));
}
const cell = (r, c) => tbody.querySelector(`input[data-row="${r}"][data-ci="${c}"]`);
setup([['p', 'q', '=A2&B2&$A$2'], ['r', 's', ''], ['t', 'u', '']]);
run('refreshCsvFormulaDisplay()');
assert.equal(cell(0, 2).value, 'pqp');
assert.equal(cell(0, 2).parentElement.classList.contains('csv-formula'), true);
run('csvEditorFillDown({r1: 0, r2: 2, c1: 2, c2: 2})');
assert.equal(run('csvEditorRows[2].c'), '=A4&B4&$A$2');
assert.deepEqual([cell(1, 2).value, cell(2, 2).value], ['rsp', 'tup']);
// 포커스하면 원문, 떠나면 결과
tbody.fire('focusin', {target: cell(1, 2)});
assert.equal(cell(1, 2).value, '=A3&B3&$A$2');
cell(1, 2).value = '=B3'; tbody.fire('focusout', {target: cell(1, 2)});
assert.equal(cell(1, 2).value, 's');

(async () => {
  // 저장: 값으로 바뀐 CSV, 수식 원문은 같은 파일 재오픈 때 복원
  el('csv-editor-save-btn').fire('click');
  assert.equal(files.length, 1);
  assert.equal(await files[0].text(), 'a,b,c\r\np,q,pqp\r\nr,s,s\r\nt,u,tup\r\n');
  run('csvEditorRows = []');
  await run('openCsvEditor()');
  assert.equal(run('csvEditorRows[2].c'), '=A4&B4&$A$2');
  // 오류 칸이 있으면 저장하지 않고 위치를 알린다
  run('csvEditorRows[0].a = "=CONCAT("');
  el('csv-editor-save-btn').fire('click');
  assert.equal(files.length, 1);
  assert.match(el('csv-editor-error').textContent, /A2 #오류/);
  console.log('ok test_csv_formula');
})().catch(e => { console.error(e); process.exit(1); });
