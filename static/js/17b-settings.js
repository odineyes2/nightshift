// ---- Settings 탭 (관리자) — 지금은 Base Model 서브탭 하나 ----
// 베이스 모델 목록은 서버 DB(base_models)가 기준이다. 목록 순서가 새 잡 마법사 1단계의 순서다.
// 코드가 이름(식별자)에 기대는 베이스 모델 — 이름을 바꾸거나 지우면 전용 워크플로우·프리셋 연결이 끊길 수 있다.
const BASE_MODEL_CODE_BOUND = /^(krea\.2|minimax-h3|wan.*|illustrious|pony|noobai)$/i;

function setSettingsError(msg){ document.getElementById('settings-error').textContent = msg || ''; }

async function openSettingsTab(){
  await fetchModelRegistry();
  renderBaseModelSettings();
}

function renderBaseModelSettings(){
  const el = document.getElementById('settings-base-models');
  const list = modelRegistry.base_model_usage || [];
  const rows = list.map((b, i) => `
      <div class="settings-bm-row" data-name="${escapeHtml(b.name)}">
        <span class="settings-bm-move">
          <button type="button" class="modal-btn-secondary base-model-up" aria-label="위로" title="위로"${i === 0 ? ' disabled' : ''}>↑</button>
          <button type="button" class="modal-btn-secondary base-model-down" aria-label="아래로" title="아래로"${i === list.length - 1 ? ' disabled' : ''}>↓</button>
        </span>
        <input type="text" class="option-input" value="${escapeHtml(b.name)}" maxlength="100" aria-label="베이스 모델 이름">
        <span class="dl-meta">사용 ${b.count}개</span>
        <button type="button" class="modal-btn-secondary base-model-detail">세부 설정</button>
        <button type="button" class="modal-btn-secondary base-model-rename">저장</button>
        <button type="button" class="modal-btn-secondary base-model-delete">삭제</button>
      </div>`).join('');
  el.innerHTML = `
    <div class="dl-meta">베이스 모델 목록이에요. 이 순서대로 새 잡의 베이스 모델 고르기에 보여요. 이름을 바꾸면 그 이름을 쓰는 모델도 함께 바뀌어요.</div>
    <div class="settings-bm-list">${rows || '<div class="dl-meta">아직 베이스 모델이 없어요</div>'}</div>
    <div class="settings-bm-row">
      <input type="text" class="option-input" id="base-model-new" maxlength="100" placeholder="새 베이스 모델 이름 (예: Anima)">
      <button type="button" class="submit-btn" id="base-model-add">추가</button>
    </div>`;
}

// 저장 중에는 버튼을 막고, 끝나면 등록부를 다시 받아 목록(과 열려 있으면 모델 탭)을 새로 그린다.
async function baseModelRequest(btn, method, url, body){
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = '저장 중…';
  try{
    const res = await fetch(url, { method, headers: body ? { 'Content-Type': 'application/json' } : {},
                                   body: body ? JSON.stringify(body) : undefined });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || `실패했어요 (${res.status})`);
    setSettingsError('');
    await fetchModelRegistry();
    renderBaseModelSettings();
    return true;
  }catch(err){
    setSettingsError(err.message);
    btn.disabled = false; btn.textContent = label;
    return false;
  }
}

document.getElementById('settings-base-models').addEventListener('click', async (e) => {
  const btn = e.target.closest('button');
  if(!btn) return;
  if(btn.id === 'base-model-add'){
    const name = document.getElementById('base-model-new').value.trim();
    if(!name){ setSettingsError('베이스 모델 이름을 적어 주세요.'); return; }
    if(await baseModelRequest(btn, 'POST', '/api/base-models', { name })) flashNotice('추가했어요.');
    return;
  }
  const row = btn.closest('[data-name]');
  if(!row) return;
  const old = row.dataset.name;
  const usage = modelRegistry.base_model_usage || [];
  const count = (usage.find(b => b.name === old) || {}).count || 0;
  const bound = BASE_MODEL_CODE_BOUND.test(old) ? '\n이 이름에 묶인 전용 워크플로우가 동작하지 않을 수 있어요.' : '';
  if(btn.classList.contains('base-model-detail')){
    openBaseModelModal(old);
  }else if(btn.classList.contains('base-model-up') || btn.classList.contains('base-model-down')){
    const names = usage.map(b => b.name);
    const i = names.indexOf(old), j = i + (btn.classList.contains('base-model-up') ? -1 : 1);
    if(i < 0 || j < 0 || j >= names.length) return;
    [names[i], names[j]] = [names[j], names[i]];
    await baseModelRequest(btn, 'PUT', '/api/base-models/order', { names });
  }else if(btn.classList.contains('base-model-rename')){
    const name = row.querySelector('input').value.trim();
    if(!name || name === old) return;
    const msg = ((count ? `모델 ${count}개의 베이스 모델도 함께 바뀌어요.` : '') + bound).trim();
    if(msg && !confirm(`"${old}" → "${name}"\n${msg}`)) return;
    if(await baseModelRequest(btn, 'PUT', '/api/base-models', { old, new: name })) flashNotice('이름을 바꿨어요.');
  }else if(btn.classList.contains('base-model-delete')){
    if(!confirm(`"${old}" 베이스 모델을 삭제할까요?${bound}`)) return;
    if(await baseModelRequest(btn, 'DELETE', '/api/base-models?name=' + encodeURIComponent(old))) flashNotice('삭제했어요.');
  }
});

// ---- 베이스 모델 세부 설정 모달 — 소속 모델(추가·제외)과 UNet 구성 점검 ----
// 제외는 연결만 푼다(등록부 항목은 남는다). 마지막 소속을 빼면 "선택 안 함"이 되고, 다른 정보도 없으면 등록부 행이 사라진다.
const baseModelModal = document.getElementById('base-model-modal');
let baseModelModalName = '';

function setBaseModelModalError(msg){ document.getElementById('base-model-modal-error').textContent = msg || ''; }

async function openBaseModelModal(name){
  baseModelModalName = name;
  document.getElementById('base-model-modal-title').textContent = name;
  setBaseModelModalError('');
  document.getElementById('base-model-modal-body').innerHTML = '<div class="dl-meta">불러오는 중…</div>';
  baseModelModal.style.display = 'flex';
  await loadBaseModelDetail();
}

function closeBaseModelModal(){
  baseModelModal.style.display = 'none';
  renderBaseModelSettings();   // 사용 수가 바뀌었을 수 있다
}

async function loadBaseModelDetail(){
  try{
    const res = await fetch('/api/base-models/detail?name=' + encodeURIComponent(baseModelModalName));
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || `불러오지 못했어요 (${res.status})`);
    renderBaseModelDetail(data);
  }catch(err){ setBaseModelModalError(err.message); }
}

const BASE_PART_LABEL = { clip: '텍스트 인코더', vae: 'VAE' };

function renderBaseModelAssembly(asm){
  if(!asm) return '<div class="dl-meta">UNet(디퓨전 모델) 소속이 없어요. 체크포인트는 텍스트 인코더·VAE가 파일 안에 있어요.</div>';
  if(asm.dedicated) return '<div class="dl-meta">전용 워크플로우가 조립하는 계열이라 범용 UNet 구성 점검 대상이 아니에요.</div>';
  const line = (label, file, note) => `<div class="settings-part"><span class="settings-part-label">${label}</span><code>${escapeHtml(file)}</code>${note ? `<span class="dl-meta">${note}</span>` : ''}</div>`;
  const parts = asm.unet.map(f => line('UNet', f, '소속 모델'));
  for(const k of ['clip', 'vae']){
    const p = asm.parts[k];
    parts.push(p ? line(BASE_PART_LABEL[k], p.filename, p.source === 'registry' ? '등록부 지정' : '코드 기본값')
                 : `<div class="settings-part settings-part-missing"><span class="settings-part-label">${BASE_PART_LABEL[k]}</span>빠졌어요 — 아래 소속 모델에 추가해 주세요</div>`);
  }
  return parts.join('');
}

function renderBaseModelDetail(d){
  const kinds = modelRegistry.kinds || [];
  const kindLabel = Object.fromEntries(kinds.map(k => [k.id, k.label]));
  const groups = Object.entries(d.members).map(([kind, files]) => `
    <div class="settings-member-group">
      <div class="field-label">${escapeHtml(kindLabel[kind] || kind)} <span class="dl-meta">${files.length}개</span></div>
      ${files.map(f => `<div class="settings-bm-row" data-kind="${escapeHtml(kind)}" data-file="${escapeHtml(f)}">
        <code class="settings-member-file">${escapeHtml(f)}</code>
        <button type="button" class="modal-btn-secondary base-member-remove">제외</button></div>`).join('')}
    </div>`).join('');
  document.getElementById('base-model-modal-body').innerHTML = `
    <div class="section-label">구성 점검</div>
    <div class="settings-parts">${renderBaseModelAssembly(d.assembly)}</div>
    <div class="section-label">소속 모델</div>
    <div class="dl-meta">제외하면 이 베이스 모델과의 연결만 풀려요. 모델 정보는 지워지지 않아요(소속이 모두 없고 다른 정보도 없으면 등록부에서 빠져요).</div>
    ${groups || '<div class="dl-meta">소속 모델이 없어요</div>'}
    <div class="settings-bm-row settings-member-add">
      <select class="option-input" id="base-member-kind" aria-label="종류">${kinds.map(k => `<option value="${escapeHtml(k.id)}">${escapeHtml(k.label)}</option>`).join('')}</select>
      <input type="text" class="option-input" id="base-member-file" list="base-member-files" maxlength="500" placeholder="파일명 (등록부에서 고르거나 입력)" aria-label="파일명">
      <datalist id="base-member-files"></datalist>
      <button type="button" class="submit-btn" id="base-member-add">추가</button>
    </div>`;
  fillBaseMemberFiles();
}

// 고른 종류의 등록부 항목 중 아직 소속이 아닌 것만 제안한다.
function fillBaseMemberFiles(){
  const kind = document.getElementById('base-member-kind').value;
  const name = baseModelModalName.toLowerCase();
  document.getElementById('base-member-files').innerHTML = Object.values(modelRegistry.items || {})
    .filter(it => it.kind === kind && !(it.base_models || []).some(b => b.toLowerCase() === name))
    .map(it => `<option value="${escapeHtml(it.filename)}"></option>`).join('');
}

async function setBaseMember(btn, kind, filename, member){
  btn.disabled = true;
  try{
    const res = await fetch('/api/base-models/members', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: baseModelModalName, kind, filename, member }) });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || `실패했어요 (${res.status})`);
    setBaseModelModalError('');
    await fetchModelRegistry();
    await loadBaseModelDetail();
  }catch(err){ setBaseModelModalError(err.message); btn.disabled = false; }
}

baseModelModal.addEventListener('change', (e) => { if(e.target.id === 'base-member-kind') fillBaseMemberFiles(); });
baseModelModal.addEventListener('click', (e) => {
  if(e.target === baseModelModal) return closeBaseModelModal();
  const btn = e.target.closest('button');
  if(!btn) return;
  if(btn.id === 'base-model-modal-close') return closeBaseModelModal();
  if(btn.id === 'base-member-add'){
    const filename = document.getElementById('base-member-file').value.trim();
    if(!filename){ setBaseModelModalError('파일명을 적어 주세요.'); return; }
    setBaseMember(btn, document.getElementById('base-member-kind').value, filename, true);
  }else if(btn.classList.contains('base-member-remove')){
    const row = btn.closest('[data-file]');
    if(!confirm(`"${row.dataset.file}"을(를) ${baseModelModalName}에서 제외할까요?\n모델 정보는 지워지지 않아요.`)) return;
    setBaseMember(btn, row.dataset.kind, row.dataset.file, false);
  }
});
document.addEventListener('keydown', (e) => {
  if(e.key === 'Escape' && baseModelModal.style.display !== 'none'){ e.stopImmediatePropagation(); closeBaseModelModal(); }
});
