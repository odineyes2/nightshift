// ---- 최근 사용한 워크플로우/CSV ----
// 서버가 /api/upload 때마다 워크플로우·CSV 사본을 각각 최근 30개까지 보관해두고
// (app.py의 RecentFileStore), 여기서는 그 목록을 모달로 보여주고 고른 걸 해당
// 슬롯에 채워 넣기만 한다. 워크플로우/CSV 둘 다 구조가 완전히 같아서(버튼 →
// 모달 목록 → 선택 → 슬롯에 채우기) 설정만 다르게 넘겨 하나의 함수로 만든다.
const TRASH_ICON_SVG = `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round">
  <path d="M2.5 4h11M6 4V2.6a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1V4M6.5 7.2v4.8M9.5 7.2v4.8M3.5 4l.6 8.4a1 1 0 0 0 1 .9h5.8a1 1 0 0 0 1-.9L12.5 4"/>
</svg>`;

function setupRecentFilePicker({
  btnId, modalId, closeId, cancelId, listId, emptyId, errorId, retentionId,
  listApi, itemApi, itemsKey, mimeType, setFile, downloadable,
}){
  let byId = {};
  const modal = document.getElementById(modalId);

  async function open(){
    const list = document.getElementById(listId);
    const empty = document.getElementById(emptyId);
    const errorEl = document.getElementById(errorId);
    errorEl.textContent = '';
    empty.style.display = 'none';
    list.innerHTML = '불러오는 중…';
    modal.style.display = 'flex';

    let data;
    try{
      const res = await fetch(listApi);
      data = await res.json();
    }catch(e){
      list.innerHTML = '';
      errorEl.textContent = '목록을 불러오지 못했어요.';
      return;
    }
    if(data.retention !== undefined){
      document.getElementById(retentionId).textContent = data.retention;
    }

    byId = {};
    const items = data[itemsKey] || [];
    if(items.length === 0){
      list.innerHTML = '';
      empty.style.display = 'block';
      return;
    }

    list.innerHTML = items.map(w => {
      byId[w.id] = w;
      const d = new Date(w.uploaded_at);
      const whenText = `${pad2(d.getMonth() + 1)}/${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
      return `
        <div class="recent-workflow-item">
          <span class="recent-workflow-name" data-recent-id="${w.id}" title="${escapeHtml(w.filename)} (눌러서 선택)">${escapeHtml(w.filename)}</span>
          <span class="recent-workflow-time">${whenText}</span>
          <button class="load-btn" data-recent-id="${w.id}" type="button" title="선택"><svg class="ico"><use href="#i-check"/></svg> 선택</button>
          ${downloadable ? `<button class="icon-btn icon-btn-neutral" data-recent-download-id="${w.id}" type="button" aria-label="다운로드" title="다운로드"><svg class="ico"><use href="#i-download"/></svg></button>` : ''}
          <button class="icon-btn" data-recent-remove-id="${w.id}" type="button" aria-label="목록에서 삭제" title="목록에서 삭제">${TRASH_ICON_SVG}</button>
        </div>`;
    }).join('');

    // 파일명(span)과 "선택" 버튼 둘 다 같은 data-recent-id를 갖고 있어서, 둘
    // 중 어느 걸 눌러도 select()가 불리게 한다(파일명이 넓은 클릭 영역 역할).
    list.querySelectorAll('[data-recent-id]').forEach(el => {
      el.addEventListener('click', () => select(el.dataset.recentId));
    });
    list.querySelectorAll('[data-recent-remove-id]').forEach(btn => {
      btn.addEventListener('click', () => remove(btn.dataset.recentRemoveId));
    });
    if(downloadable){
      list.querySelectorAll('[data-recent-download-id]').forEach(btn => {
        btn.addEventListener('click', () => downloadItem(btn.dataset.recentDownloadId));
      });
    }
  }

  // 6번 요구사항 — 목록에서 파일을 고르지(select) 않고 그대로 내 컴퓨터로 받는다.
  // select()와 같은 API(itemApi)로 내용을 받아오되, 폼에 채우는 대신 다운로드만 한다.
  async function downloadItem(id){
    const errorEl = document.getElementById(errorId);
    errorEl.textContent = '';
    const entry = byId[id];
    if(!entry) return;
    try{
      const res = await fetch(itemApi(id));
      if(!res.ok){
        const data = await res.json().catch(() => ({}));
        errorEl.textContent = data.detail || '파일을 불러오지 못했어요.';
        return;
      }
      const blob = await res.blob();
      triggerBlobDownload(blob, entry.filename);
    }catch(e){
      errorEl.textContent = '다운로드에 실패했어요.';
    }
  }

  async function select(id){
    const errorEl = document.getElementById(errorId);
    errorEl.textContent = '';
    const entry = byId[id];
    if(!entry) return;
    try{
      const res = await fetch(itemApi(id));
      if(!res.ok){
        const data = await res.json().catch(() => ({}));
        errorEl.textContent = data.detail || '파일을 불러오지 못했어요.';
        return;
      }
      const text = await res.text();
      setFile(new File([text], entry.filename, { type: mimeType }));
      close();
    }catch(e){
      errorEl.textContent = '파일을 불러오지 못했어요.';
    }
  }

  async function remove(id){
    const errorEl = document.getElementById(errorId);
    errorEl.textContent = '';
    const entry = byId[id];
    if(!entry) return;
    if(!confirm(`'${entry.filename}'을(를) 최근 목록에서 삭제할까요?`)) return;
    try{
      const res = await fetch(itemApi(id), { method: 'DELETE' });
      if(!res.ok){
        const data = await res.json().catch(() => ({}));
        errorEl.textContent = data.detail || '삭제에 실패했어요.';
        return;
      }
      await open();
    }catch(e){
      errorEl.textContent = '삭제에 실패했어요.';
    }
  }

  function close(){
    modal.style.display = 'none';
  }

  document.getElementById(btnId).addEventListener('click', open);
  document.getElementById(closeId).addEventListener('click', close);
  document.getElementById(cancelId).addEventListener('click', close);
  modal.addEventListener('click', (e) => { if(e.target === modal) close(); });
  document.addEventListener('keydown', (e) => {
    if(e.key === 'Escape' && modal.style.display !== 'none') close();
  });
}

setupRecentFilePicker({
  btnId: 'recent-workflows-btn', modalId: 'recent-workflows-modal', closeId: 'recent-workflows-modal-close',
  cancelId: 'recent-workflows-modal-cancel', listId: 'recent-workflows-list', emptyId: 'recent-workflows-empty',
  errorId: 'recent-workflows-error', retentionId: 'recent-workflows-retention',
  listApi: '/api/recent-workflows', itemApi: (id) => `/api/recent-workflows/${id}`, itemsKey: 'workflows',
  mimeType: 'application/json', setFile: setWorkflowFile, downloadable: true,
});

setupRecentFilePicker({
  btnId: 'recent-csvs-btn', modalId: 'recent-csvs-modal', closeId: 'recent-csvs-modal-close',
  cancelId: 'recent-csvs-modal-cancel', listId: 'recent-csvs-list', emptyId: 'recent-csvs-empty',
  errorId: 'recent-csvs-error', retentionId: 'recent-csvs-retention',
  listApi: '/api/recent-csvs', itemApi: (id) => `/api/recent-csvs/${id}`, itemsKey: 'csvs',
  mimeType: 'text/csv', setFile: setCsvFile,
});

// ---- CSV 편집 모달 (표 편집기) ----
// requires_csv 템플릿마다 manifest.json에 적어둔 csv_columns(스키마)를 첫 줄로
// 고정하고, 행만 자유롭게 추가/삭제/편집하는 작은 스프레드시트다. "스키마(열)는
// 편집할 수 없고 값만 입력/수정 가능"하다는 요구를 그대로 반영한다 — 열 이름은
// 화면에서 아예 입력 불가능한 <th>로만 그리고, 실제 입력은 행의 <input>에만 있다.
let csvEditorSchema = [];
let csvEditorRows = [];

function csvEditorEmptyRow(){
  const row = {};
  for(const col of csvEditorSchema) row[col.name] = '';
  return row;
}

function csvEditorRowIsBlank(row){
  return csvEditorSchema.every(col => !(row[col.name] || '').trim());
}

// 아주 단순한 RFC4180 파서 — 큰따옴표로 감싼 필드 안의 콤마/줄바꿈/이스케이프된
// ""를 지원한다. "이미 첨부된 CSV를 편집기에 불러오기"에만 쓰므로, 완벽할 필요는
// 없고 우리가 stringifyCsvRows로 직접 만든 형식 정도만 정확히 되읽으면 충분하다.
function parseCsvText(text){
  const rows = [];
  let row = [];
  let field = '';
  let inQuotes = false;
  for(let i = 0; i < text.length; i++){
    const c = text[i];
    if(inQuotes){
      if(c === '"'){
        if(text[i + 1] === '"'){ field += '"'; i++; } else { inQuotes = false; }
      } else {
        field += c;
      }
      continue;
    }
    if(c === '"'){ inQuotes = true; }
    else if(c === ','){ row.push(field); field = ''; }
    else if(c === '\r'){ /* 무시, \n에서 줄을 끊음 */ }
    else if(c === '\n'){ row.push(field); rows.push(row); row = []; field = ''; }
    else { field += c; }
  }
  if(field !== '' || row.length > 0){ row.push(field); rows.push(row); }
  if(rows.length === 0) return { header: [], rows: [] };
  const header = rows[0];
  const dataRows = rows.slice(1).filter(r => r.some(v => v !== ''));
  return {
    header,
    rows: dataRows.map(r => {
      const obj = {};
      header.forEach((h, idx) => { obj[h] = r[idx] !== undefined ? r[idx] : ''; });
      return obj;
    }),
  };
}

function csvFieldEscape(value){
  value = (value === undefined || value === null) ? '' : String(value);
  if(/[",\r\n]/.test(value)) return '"' + value.replace(/"/g, '""') + '"';
  return value;
}

function stringifyCsvRows(schema, rows){
  const header = schema.map(c => c.name);
  const lines = [header.map(csvFieldEscape).join(',')];
  for(const row of rows) lines.push(header.map(name => csvFieldEscape(row[name])).join(','));
  return lines.join('\r\n') + '\r\n';
}

function renderCsvEditorTable(){
  document.getElementById('csv-editor-thead-row').innerHTML =
    csvEditorSchema.map(col =>
      `<th title="${escapeHtml(col.note || '')}">${escapeHtml(col.label)}${col.required ? '<span class="csv-col-required" title="필수">*</span>' : ''}</th>`
    ).join('') + '<th></th>';

  document.getElementById('csv-editor-tbody').innerHTML = csvEditorRows.map((row, rIdx) =>
    `<tr>` +
    csvEditorSchema.map(col =>
      `<td><input type="text" data-row="${rIdx}" data-col="${escapeHtml(col.name)}" value="${escapeHtml(row[col.name] || '')}"></td>`
    ).join('') +
    `<td class="csv-editor-row-actions"><button type="button" class="csv-editor-del-btn" data-row="${rIdx}" title="이 행 삭제">&times;</button></td>` +
    `</tr>`
  ).join('');

  document.getElementById('csv-editor-row-count').textContent = `${csvEditorRows.length}행`;
}

document.getElementById('csv-editor-tbody').addEventListener('input', (e) => {
  const input = e.target.closest('input[data-col]');
  if(!input) return;
  csvEditorRows[Number(input.dataset.row)][input.dataset.col] = input.value;
});
document.getElementById('csv-editor-tbody').addEventListener('click', (e) => {
  const btn = e.target.closest('.csv-editor-del-btn');
  if(!btn) return;
  csvEditorRows.splice(Number(btn.dataset.row), 1);
  renderCsvEditorTable();
});
document.getElementById('csv-editor-add-row-btn').addEventListener('click', () => {
  csvEditorRows.push(csvEditorEmptyRow());
  renderCsvEditorTable();
  const wrap = document.querySelector('.csv-editor-table-wrap');
  wrap.scrollTop = wrap.scrollHeight;
});

async function openCsvEditor(){
  const template = templatesById[templateSelect.value];
  if(!template || !template.csv_columns || !template.csv_columns.length) return;
  csvEditorSchema = template.csv_columns;
  document.getElementById('csv-editor-modal-title').textContent = `Edit CSV — ${template.label}`;
  document.getElementById('csv-editor-error').textContent = '';

  // 이미 첨부된 CSV가 있으면(업로드/최근/이전 저장) 그 내용을 표에 그대로 불러온다
  // — 스키마와 이름이 맞는 열만 채우고, 안 맞는 열은 조용히 무시한다.
  csvEditorRows = [];
  if(selectedFiles.csv){
    try{
      const text = await selectedFiles.csv.text();
      const parsed = parseCsvText(text);
      csvEditorRows = parsed.rows.map(r => {
        const row = csvEditorEmptyRow();
        for(const col of csvEditorSchema){
          if(r[col.name] !== undefined) row[col.name] = r[col.name];
        }
        return row;
      });
    }catch(e){ /* 못 읽으면 빈 표로 시작 */ }
  }
  while(csvEditorRows.length < 3) csvEditorRows.push(csvEditorEmptyRow());

  renderCsvEditorTable();
  document.getElementById('csv-editor-hint').textContent =
    '* 표시된 열은 값이 있어야 그 행이 처리돼요. 첫 줄(열 이름)은 이 템플릿에 맞게 고정돼 있어 편집할 수 없어요 — 열에 마우스를 올리면 설명이 떠요.';
  document.getElementById('csv-editor-modal').style.display = 'flex';
}

function closeCsvEditor(){
  document.getElementById('csv-editor-modal').style.display = 'none';
}

document.getElementById('csv-editor-btn').addEventListener('click', openCsvEditor);
document.getElementById('csv-editor-modal-close').addEventListener('click', closeCsvEditor);
document.getElementById('csv-editor-cancel-btn').addEventListener('click', closeCsvEditor);
document.getElementById('csv-editor-modal').addEventListener('click', (e) => {
  if(e.target.id === 'csv-editor-modal') closeCsvEditor();
});
document.addEventListener('keydown', (e) => {
  if(e.key === 'Escape' && document.getElementById('csv-editor-modal').style.display !== 'none') closeCsvEditor();
});

function csvEditorFilename(){
  const template = templatesById[templateSelect.value];
  return `${(template && template.id) || 'edited'}.csv`;
}

document.getElementById('csv-editor-save-btn').addEventListener('click', () => {
  const rows = csvEditorRows.filter(r => !csvEditorRowIsBlank(r));
  if(rows.length === 0){
    document.getElementById('csv-editor-error').textContent = '값이 채워진 행이 하나도 없어요.';
    return;
  }
  const text = stringifyCsvRows(csvEditorSchema, rows);
  setCsvFile(new File([text], csvEditorFilename(), { type: 'text/csv' }));
  closeCsvEditor();
  document.getElementById('load-notice').textContent = `표에서 작성한 CSV(${rows.length}행)를 CSV 슬롯에 채웠어요.`;
});

document.getElementById('csv-editor-download-btn').addEventListener('click', () => {
  const rows = csvEditorRows.filter(r => !csvEditorRowIsBlank(r));
  if(rows.length === 0){
    document.getElementById('csv-editor-error').textContent = '값이 채워진 행이 하나도 없어요.';
    return;
  }
  const text = stringifyCsvRows(csvEditorSchema, rows);
  triggerBlobDownload(new Blob([text], { type: 'text/csv' }), csvEditorFilename());
});

// 헤더에 있던 전역 ComfyUI 연결 배지(와 그 주소 설정 모달)는 없앴다 — 파드가
// 여럿인 지금은 "파드 하나"의 연결 상태를 전역에 하나 띄우는 게 오히려 부정확해
// 보인다. 각 파드 카드가 이미 자기 상태를 정확히 보여주므로 그걸로 충분하다.
// 다만 이 폴링 자체는 남겨둔다 — 응답의 pull_outputs가 "기본 파드" 기준 전역
// 갤러리의 "⬇ 결과 가져오기" 버튼 노출 여부(comfyPullOutputs)를 정하기 때문이다.
async function fetchComfyStatus(){
  let data;
  try{
    const res = await fetch('/api/comfy-status');
    data = await res.json();
  }catch(e){
    data = { url: null, connected: false };
  }
  if(data.pull_outputs !== undefined){
    comfyPullOutputs = !!data.pull_outputs;
    applyPullOutputsVisibility();
  }
}

// 갤러리의 "⬇ 결과 가져오기" 버튼은 원격에서 끌어올 게 있을 때만 의미가 있으므로
// pull_outputs가 켜져 있을 때만 보여준다.
let comfyPullOutputs = false;
function applyPullOutputsVisibility(){
  const btn = document.getElementById('gallery-pull-btn');
  if(btn){
    // 파드 갤러리에서는 지금 보고 있는 그 파드의 설정을 본다 — 전역 갤러리는
    // 예전처럼 기본 파드 기준(comfyPullOutputs)이다. 파드가 여럿일 때 "기본 파드"와
    // "지금 보는 파드"가 다를 수 있어서 둘을 섞으면 안 된다.
    const on = galleryPodFilter ? !!(podsById[galleryPodFilter] || {}).pull_outputs : comfyPullOutputs;
    btn.style.display = on ? '' : 'none';
    btn.title = galleryPodFilter
      ? `'${podName(galleryPodFilter)}' 워커가 만든 결과 이미지를 이 서버로 가져와요.`
      : '원격 ComfyUI가 만든 결과 이미지를 이 서버로 가져와요. 작업이 끝날 때마다 자동으로도 가져오지만, pod를 껐다 켠 뒤 밀린 것을 한꺼번에 받을 때 쓰세요.';
  }
  // 영상 갤러리도 이제 파드 갤러리(pvideo)가 있으니 이미지와 같은 규칙을 쓴다 —
  // 지금 보는 파드가 있으면 그 파드 기준, 전역이면 기본 파드(comfyPullOutputs) 기준.
  // 같은 /api/comfy-outputs/sync가 이미지/영상을 함께 받아오므로 백엔드는 그대로 둔다.
  const videoBtn = document.getElementById('video-gallery-pull-btn');
  if(videoBtn){
    const videoOn = videoGalleryPodFilter ? !!(podsById[videoGalleryPodFilter] || {}).pull_outputs : comfyPullOutputs;
    videoBtn.style.display = videoOn ? '' : 'none';
    videoBtn.title = videoGalleryPodFilter
      ? `'${podName(videoGalleryPodFilter)}' 워커가 만든 결과 영상을 이 서버로 가져와요.`
      : '원격 ComfyUI가 만든 결과 영상을 이 서버로 가져와요. nightshift를 거치지 않고 ComfyUI에서 직접 돌린 작업도 ComfyUI 히스토리에 남아있으면 함께 가져와요.';
  }
}

// ---- 설치된 모델/노드 목록 (ComfyUI의 /object_info를 서버가 추려서 넘겨줌) ----
// 워크플로우 JSON은 "노드 이름 + 입력값"일 뿐이라, 이 목록이 있으면 "이 서버에서
// 실제로 쓸 수 있는 게 뭔지"를 화면에서 그대로 다룰 수 있다. 지금은 (a) 헤더의
// "📋 모델" 목록 보기와 (b) 업로드한 워크플로우 호환성 검사에 쓴다.
let comfyObjectInfoCache = null;
// 위 캐시가 어느 파드 것인지. 파드마다 설치된 노드·모델이 다르므로, 다른 파드로
// 옮겨가면(파드 스코프 바에서 파드 전환) 캐시를 버리고 다시 받아야 한다.
let comfyObjectInfoPodId = null;
// 워크플로우/모델 화면이 물어볼 파드를 쿼리로 붙인다 — 안 붙이면 서버가 기본 파드를
// 보므로, 파드 B 안에서 파드 A의 노드 목록을 보는 거짓말이 된다.
function podQuery(extra){
  const params = new URLSearchParams(extra || {});
  const podId = formPodId();
  if(podId) params.set('pod_id', podId);
  else if(formAutoMode()) params.set('pod_id', 'auto');
  const q = params.toString();
  return q ? `?${q}` : '';
}

async function fetchComfyObjectInfo(force){
  const podId = formPodId() || (formAutoMode() ? 'auto' : null);
  if(comfyObjectInfoCache && !force && comfyObjectInfoPodId === podId) return comfyObjectInfoCache;
  const res = await fetch(`/api/comfy-object-info${podQuery(force ? { refresh: 'true' } : null)}`);
  if(!res.ok){
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || '모델 목록을 가져오지 못했어요.');
  }
  comfyObjectInfoCache = await res.json();
  comfyObjectInfoPodId = podId;
  return comfyObjectInfoCache;
}

// LoRA 파일명 -> 트리거 워드. 서버에 저장돼 있는 걸 그대로 캐시해두고, "설치된
// 모델" 모달에서 입력할 때마다 로컬 캐시를 즉시 갱신 + 서버에는 살짝 늦게(디바운스)
// 저장한다 — "워크플로우" 탭에서 LoRA를 고를 때 바로 조회해서 쓴다.
let loraTriggers = {};
// 마법사가 마지막으로 적용한 체크포인트/LoRA — 추가할 때 "켜진 파드 어디에도 없는 모델" 경고에 쓴다(resetForm이 비움).
var lastWizardModels = null;

// 그 모델이 설치된 "연결된" 파드 이름들(modelInventory 기준). 연결된 파드가 하나도 없거나 목록을 아직
// 못 받았으면 null — 모른다는 뜻이라 배지·경고를 띄우지 않는다(파드가 꺼져 있는 건 정상이다).
// ComfyUI는 하위 폴더까지 붙인 이름을 주므로 파일 이름(마지막 경로 조각)으로도 맞춰 본다.
function podsWithModel(kind, name){
  if(!modelInventory || !name) return null;
  const connected = (modelInventory.pods || []).filter(p => p.connected);
  if(!connected.length) return null;
  const base = String(name).split(/[\\/]/).pop();
  return connected.filter(p => ((p.models || {})[kind] || []).some(m => m === name || String(m).split(/[\\/]/).pop() === base))
                  .map(p => p.name);
}

async function fetchLoraTriggers(){
  try{
    const res = await fetch('/api/lora-triggers');
    if(!res.ok) return;
    loraTriggers = await res.json();
  }catch(e){
    // 못 받아와도 트리거 워드 자동완성이 안 될 뿐 나머지 기능엔 영향 없음
  }
}

// {trigger, baseId} 형태를 항상 돌려준다 — 아직 fetch가 안 됐거나 그 이름이 목록에 없을 때를 위한 기본값.
// baseId가 비어 있으면 어떤 베이스 모델을 골라도 보이는 LoRA다.
function getLoraEntry(name){
  const entry = loraTriggers[name];
  if(entry && typeof entry === 'object') return { trigger: entry.trigger || '', baseId: entry.base_id || '' };
  return { trigger: '', baseId: '' };
}

// LoRA의 트리거 키워드. ComfyUI는 하위 폴더까지 붙인 이름(예: sdxl/foo.safetensors)을 주는데
// 등록부에는 파일 이름만 적혀 있을 수 있어서, 정확히 안 맞으면 파일 이름(마지막 경로 조각)으로 한 번 더 찾는다.
function loraTriggerFor(name){
  const exact = getLoraEntry(name).trigger;
  if(exact) return exact;
  const base = String(name || '').split(/[\\/]/).pop();
  for(const [key, entry] of Object.entries(loraTriggers)){
    if(String(key).split(/[\\/]/).pop() === base && entry && entry.trigger) return entry.trigger;
  }
  return '';
}

// 세부 설정 탭의 "LoRA 트리거 키워드" 칸을 고른 LoRA들로 채운다(LoRA가 없으면 숨김).
function setLoraTriggerField(loraNames){
  const field = document.getElementById('lora-trigger-field');
  if(!field) return;
  const input = document.getElementById('lora-trigger-input');
  const hint = document.getElementById('lora-trigger-hint');
  if(!loraNames || loraNames.length === 0){ field.style.display = 'none'; input.value = ''; return; }
  const triggers = [...new Set(loraNames.map(loraTriggerFor).map(t => t.trim()).filter(Boolean))];
  const missing = loraNames.filter(n => !loraTriggerFor(n));
  input.value = triggers.join(', ');
  field.style.display = '';
  hint.textContent = (triggers.length
      ? '작업을 추가할 때 프롬프트 앞에 자동으로 붙어요(CSV 순회면 각 행의 트리거 프롬프트 칸). 이미 들어 있으면 다시 붙이지 않고, 칸을 비우면 붙이지 않아요.'
      : '')
    + (missing.length ? `${triggers.length ? ' ' : ''}트리거 키워드가 등록되지 않은 LoRA: ${missing.join(', ')} — 필요하면 여기 직접 적거나 "모델" 탭에 등록하세요.` : '');
}

// 트리거를 프롬프트 앞에 붙인다. 프롬프트가 비었으면 트리거만, 이미 들어 있으면 그대로.
function prependLoraTrigger(text, trigger){
  const t = (trigger || '').trim();
  const v = text || '';
  if(!t) return v;
  if(!v.trim()) return t;
  if(v.toLowerCase().includes(t.toLowerCase())) return v;
  return `${t}, ${v}`;
}

// CSV 파일의 모든 행에 트리거를 넣은 새 파일을 만든다 — trigger_prompt 칸이 있는 템플릿은 그 칸에,
// 없으면 main_prompt(또는 prompt) 칸에. 원래 파일은 그대로 두고 제출하는 사본만 바꾼다.
async function csvWithLoraTrigger(file, template, trigger){
  const cols = (template.csv_columns || []).map(c => c.name);
  const { header, rows } = parseCsvText(await file.text());
  if(!header.length) return file;
  const target = cols.includes('trigger_prompt') ? 'trigger_prompt'
    : (header.includes('main_prompt') ? 'main_prompt' : (header.includes('prompt') ? 'prompt' : 'main_prompt'));
  const outHeader = header.includes(target) ? header : [...header, target];
  const lines = [outHeader.map(csvFieldEscape).join(',')];
  for(const row of rows){
    // 같은 행의 다른 프롬프트 칸에 이미 들어 있으면(사용자가 직접 넣은 경우) 두 번 붙이지 않는다.
    const already = ['trigger_prompt', 'main_prompt', 'prompt'].some(c => (row[c] || '').toLowerCase().includes(trigger.toLowerCase()));
    if(!already) row[target] = prependLoraTrigger(row[target] || '', trigger);
    lines.push(outHeader.map(h => csvFieldEscape(row[h])).join(','));
  }
  return new File([lines.join('\r\n') + '\r\n'], file.name, { type: 'text/csv' });
}

// ---- 퀄리티 프롬프트 (Danbooru 태그 계열) ----
// Pony는 score_* 태그 체계라 넣지 않는다(NS-19 결정).
const QUALITY_PROMPT_FAMILIES = ['Illustrious', 'NoobAI'];
const DANBOORU_QUALITY_DEFAULT = 'masterpiece, best quality, amazing quality, very aesthetic, absurdres';

// 세부 설정 탭의 "퀄리티 프롬프트" 칸 — 계열이 맞으면 보이고 기본값으로 채운다. 같은 계열로 다시 들어오면
// 사람이 고친 값을 그대로 두고, 계열이 바뀌거나 폼을 초기화할 때(familyLabel 없음)만 다시 채운다.
function setQualityPromptField(familyLabel){
  const field = document.getElementById('quality-prompt-field');
  if(!field) return;
  const input = document.getElementById('quality-prompt-input');
  const show = QUALITY_PROMPT_FAMILIES.includes(familyLabel);
  if(show && field.dataset.family === familyLabel) return;
  field.dataset.family = show ? familyLabel : '';
  field.style.display = show ? '' : 'none';
  input.value = show ? DANBOORU_QUALITY_DEFAULT : '';
}

// 보이는 칸의 값(없으면 '').
function currentQualityPrompt(){
  const field = document.getElementById('quality-prompt-field');
  return field && field.style.display !== 'none' ? document.getElementById('quality-prompt-input').value.trim() : '';
}

// 퀄리티 태그를 프롬프트 뒤에 붙인다. 프롬프트가 비었으면(워크플로우 프롬프트를 그대로 쓰는 경우) 덮어쓰지 않게
// 붙이지 않고, 이미 들어 있으면 그대로 둔다.
function appendQualityPrompt(text, quality){
  const q = (quality || '').trim();
  const v = text || '';
  if(!q || !v.trim()) return v;
  if(v.toLowerCase().includes(q.toLowerCase())) return v;
  return `${v.replace(/[\s,]+$/, '')}, ${q}`;
}

// CSV 사본의 각 행 main_prompt(또는 prompt)에 퀄리티 태그를 붙인다 — quality_prompt 칸을 직접 채운 행은 건너뛴다.
async function csvWithQualityPrompt(file, quality){
  const { header, rows } = parseCsvText(await file.text());
  const target = header.includes('main_prompt') ? 'main_prompt' : (header.includes('prompt') ? 'prompt' : null);
  if(!target) return file;
  const lines = [header.map(csvFieldEscape).join(',')];
  for(const row of rows){
    if(!(row.quality_prompt || '').trim()) row[target] = appendQualityPrompt(row[target] || '', quality);
    lines.push(header.map(h => csvFieldEscape(row[h])).join(','));
  }
  return new File([lines.join('\r\n') + '\r\n'], file.name, { type: 'text/csv' });
}

// ---- 베이스 모델 그룹 ----
// "새 작업 추가" 마법사가 이 그룹을 기준으로 워크플로우 유형/LoRA/ControlNet 프리셋을 걸러서 보여준다. 따로 관리하는
// 데이터가 아니라, 모델 등록부에서 base_model이 적힌 체크포인트를 그 값으로 묶어 서버가 돌려준다
// ({id: {label, checkpoints}} — 지금 들어가 있는 파드에 설치된 체크포인트만).
let baseModelFamilies = {};

async function fetchBaseModelFamilies(){
  try{
    const res = await fetch(`/api/base-model-families${podQuery()}`);
    if(!res.ok) return;
    baseModelFamilies = await res.json();
  }catch(e){
    // 못 받아와도 마법사의 "베이스 모델" 단계가 비어있을 뿐 나머지 기능엔 영향 없음
  }
}

// ---- "새 작업 추가" 마법사 2단계(워크플로우 유형) 카탈로그 — 정적이라 세션당 한 번만 받는다 ----
let workflowTypesCache = null;

async function fetchWorkflowTypes(){
  if(workflowTypesCache) return workflowTypesCache;
  try{
    const res = await fetch('/api/workflow-types');
    if(res.ok) workflowTypesCache = await res.json();
  }catch(e){ /* 마법사 2단계가 비어있을 뿐 나머지 기능엔 영향 없음 */ }
  return workflowTypesCache || {};
}

// ---- family+유형 조합별 ControlNet 프리셋 워크플로우 존재 여부 ----
let workflowPresetsList = [];

async function fetchWorkflowPresetsList(){
  try{
    const res = await fetch('/api/workflow-presets');
    if(res.ok) workflowPresetsList = (await res.json()).presets || [];
  }catch(e){ workflowPresetsList = []; }
  return workflowPresetsList;
}

function presetExists(familyId, typeId){
  return workflowPresetsList.some(p => p.family_id === familyId && p.type_id === typeId);
}

// ---- img2img/USDU 입력 이미지 목록(세트 구분 없는 평평한 목록, input_assets.py) ----
let inputImagesCache = null;

async function fetchInputImages(force){
  if(inputImagesCache && !force) return inputImagesCache;
  try{
    const res = await fetch('/api/input-images');
    inputImagesCache = res.ok ? (await res.json()).images || [] : [];
  }catch(e){ inputImagesCache = []; }
  return inputImagesCache;
}

