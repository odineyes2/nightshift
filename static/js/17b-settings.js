// ---- Settings 탭 (관리자) — Base Model · Notification 서브탭 ----
// 베이스 모델 목록은 서버 DB(base_models)가 기준이다. 목록 순서가 새 잡 마법사 1단계의 순서다.
// 코드가 이름(식별자)에 기대는 베이스 모델 — 이름을 바꾸거나 지우면 전용 워크플로우·프리셋 연결이 끊길 수 있다.
const BASE_MODEL_CODE_BOUND = /^(krea\.2|minimax-h3|wan.*|illustrious|pony|noobai)$/i;

function setSettingsError(msg){ document.getElementById('settings-error').textContent = msg || ''; }

let settingsSubtab = 'base-models';

async function openSettingsTab(){
  document.querySelectorAll('#settings-subtabs [data-settings-tab]').forEach(b =>
    b.classList.toggle('active', b.dataset.settingsTab === settingsSubtab));
  document.querySelectorAll('#tab-settings .settings-pane').forEach(p =>
    p.style.display = p.id === 'settings-' + settingsSubtab ? '' : 'none');
  setSettingsError('');
  if(settingsSubtab === 'notifications') return loadNotifySettings();
  await fetchModelRegistry();
  renderBaseModelSettings();
}

document.getElementById('settings-subtabs').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-settings-tab]');
  if(!btn || btn.dataset.settingsTab === settingsSubtab) return;
  settingsSubtab = btn.dataset.settingsTab;
  openSettingsTab();
});

// ---- Notification 서브탭 — 잡 종료 ntfy 알림(서버 notify.py). 토큰은 원문 대신 token_set만 받는다 ----
const notifyEnabled = document.getElementById('notify-enabled');
notifyEnabled.addEventListener('click', () =>
  notifyEnabled.setAttribute('aria-checked', String(notifyEnabled.getAttribute('aria-checked') !== 'true')));

function renderNotifySettings(s){
  notifyEnabled.setAttribute('aria-checked', String(!!s.enabled));
  document.querySelectorAll('input[name="notify-mode"]').forEach(r => r.checked = r.value === s.mode);
  document.getElementById('notify-server').value = s.server || '';
  document.getElementById('notify-topic').value = s.topic || '';
  document.getElementById('notify-token').value = '';
  document.getElementById('notify-token-state').textContent = s.token_set ? '설정됨' : '';
  document.getElementById('notify-token-clear').hidden = !s.token_set;
}

// 저장·지우기·시험 공용 — 저장 중에는 버튼을 막는다. 성공하면 서버가 돌려준 설정으로 폼을 다시 그린다.
async function notifyRequest(btn, busyLabel, method, url, body){
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = busyLabel;
  try{
    const res = await fetch(url, { method, headers: body ? { 'Content-Type': 'application/json' } : {},
                                   body: body ? JSON.stringify(body) : undefined });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || `실패했어요 (${res.status})`);
    setSettingsError('');
    return data;
  }catch(err){
    setSettingsError(err.message);
    return null;
  }finally{
    btn.disabled = false; btn.textContent = label;
  }
}

async function loadNotifySettings(){
  try{
    const res = await fetch('/api/settings/notifications');
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || `불러오지 못했어요 (${res.status})`);
    renderNotifySettings(data);
  }catch(err){ setSettingsError(err.message); }
}

function notifyFormBody(){
  const mode = document.querySelector('input[name="notify-mode"]:checked');
  return {
    enabled: notifyEnabled.getAttribute('aria-checked') === 'true',
    mode: mode ? mode.value : 'all',
    server: document.getElementById('notify-server').value.trim(),
    topic: document.getElementById('notify-topic').value.trim(),
    token: document.getElementById('notify-token').value,
  };
}

async function saveNotifySettings(btn){
  const data = await notifyRequest(btn, '저장 중…', 'PUT', '/api/settings/notifications', notifyFormBody());
  if(data) renderNotifySettings(data);
  return !!data;
}

document.getElementById('notify-save').addEventListener('click', async (e) => {
  if(await saveNotifySettings(e.currentTarget)) flashNotice('저장했어요.');
});

// 시험 알림은 저장된 설정으로 보내므로 먼저 지금 폼을 저장한다.
document.getElementById('notify-test').addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  if(!await saveNotifySettings(btn)) return;
  const data = await notifyRequest(btn, '보내는 중…', 'POST', '/api/settings/notifications/test');
  if(!data) return;
  if(data.ok) flashNotice('시험 알림을 보냈어요. 폰에 왔는지 확인해 주세요.');
  else setSettingsError(data.error || '알림을 보내지 못했어요.');
});

document.getElementById('notify-token-clear').addEventListener('click', async (e) => {
  if(!confirm('저장된 액세스 토큰을 지울까요?')) return;
  const data = await notifyRequest(e.currentTarget, '지우는 중…', 'PUT', '/api/settings/notifications', { clear_token: true });
  if(data){ renderNotifySettings(data); flashNotice('토큰을 지웠어요.'); }
});

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
let baseModelDetailData = null;

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
    await fetchWorkflowTypes();
    await fetchWorkflowPresetsList();
    baseModelDetailData = data;
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
    <div class="section-label">워크플로우 유형</div>
    ${renderBaseWorkflowTypes(d)}
    <div class="section-label">스윗 포인트</div>
    ${renderBaseSweet(d.sweet || {})}
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

// ---- 워크플로우 유형 허용 목록 — null이면 자동(마법사의 호환 규칙), 목록이면 그것만 그 순서로 ----
// 카탈로그는 코드(WORKFLOW_TYPES)의 base·post·preset. 같은 id(face_detailer)는 하나로 본다.
function baseWorkflowCatalog(){
  const out = new Map();
  for(const group of ['base', 'post', 'preset'])
    for(const t of (workflowTypesCache || {})[group] || []) if(!out.has(t.id)) out.set(t.id, { ...t, group });
  return out;
}

// 유형 하나의 조립 방식 라벨과 이 계열에서 안 맞는 이유(없으면 '').
function baseWorkflowTypeInfo(t, d){
  const ckpt = !!(d.members.checkpoints || []).length, asm = d.assembly;
  if(t.family_id) return { how: '전용', warn: t.family_id === d.id ? '' : `${t.family_id} 계열 전용이에요` };
  if(t.group === 'preset') return { how: '프리셋', warn: presetExists(d.id, t.id) ? '' : '프리셋 파일이 없어요' };
  if(t.family_kind === 'checkpoints') return { how: 'Checkpoint', warn: ckpt ? '' : '체크포인트형 계열에서만 써요' };
  const how = [ckpt && 'Checkpoint', asm && 'UNet'].filter(Boolean).join('/');
  if(ckpt) return { how, warn: '' };
  if(!asm) return { how, warn: '조립할 체크포인트·UNet 소속이 없어요' };
  if(asm.dedicated) return { how, warn: '전용 빌더 계열이라 범용 조립을 못 해요' };
  if(asm.missing.length) return { how, warn: `부품이 빠졌어요: ${asm.missing.map(k => BASE_PART_LABEL[k]).join(', ')}` };
  return { how, warn: '' };
}

function renderBaseWorkflowTypes(d){
  const catalog = baseWorkflowCatalog();
  const label = t => { const { how, warn } = baseWorkflowTypeInfo(t, d);
    return `${escapeHtml(t.label)}${how ? ` <span class="dl-meta">(${escapeHtml(how)})</span>` : ''}${warn
      ? ` <span class="settings-part-missing"><svg class="ico"><use href="#i-triangle-alert"/></svg> ${escapeHtml(warn)}</span>` : ''}`; };
  const custom = Array.isArray(d.workflow_types);
  const toggle = `<label class="settings-bm-row"><input type="checkbox" id="base-wt-custom"${custom ? ' checked' : ''}> 직접 지정
    <span class="dl-meta">끄면 자동 — 이 계열에 맞는 유형이 모두 보여요</span></label>`;
  if(!custom){
    const auto = [...catalog.values()].filter(t => !baseWorkflowTypeInfo(t, d).warn);
    return toggle + `<div class="dl-meta">지금 보이는 유형: ${auto.map(t => escapeHtml(t.label)).join(', ') || '없음'}</div>`;
  }
  const list = d.workflow_types;
  const rows = list.map((id, i) => `<div class="settings-bm-row" data-wt="${escapeHtml(id)}">
      <span class="settings-bm-move">
        <button type="button" class="modal-btn-secondary base-wt-up" aria-label="위로" title="위로"${i === 0 ? ' disabled' : ''}>↑</button>
        <button type="button" class="modal-btn-secondary base-wt-down" aria-label="아래로" title="아래로"${i === list.length - 1 ? ' disabled' : ''}>↓</button>
      </span>
      <span class="settings-member-file">${catalog.has(id) ? label(catalog.get(id)) : escapeHtml(id)}</span>
      <button type="button" class="modal-btn-secondary base-wt-remove">삭제</button></div>`).join('');
  const rest = [...catalog.values()].filter(t => !list.includes(t.id));
  return toggle + `<div class="settings-bm-list">${rows || '<div class="dl-meta">허용한 유형이 없어요 — 마법사 2단계에 아무것도 안 보여요</div>'}</div>
    ${rest.length ? `<div class="settings-bm-row">
      <select class="option-input" id="base-wt-new" aria-label="추가할 유형">${rest.map(t => {
        const { how, warn } = baseWorkflowTypeInfo(t, d);
        return `<option value="${escapeHtml(t.id)}">${escapeHtml(t.label)}${how ? ` (${escapeHtml(how)})` : ''}${warn ? ' ⚠' : ''}</option>`; }).join('')}</select>
      <button type="button" class="submit-btn" id="base-wt-add">추가</button></div>` : ''}`;
}

async function saveBaseWorkflowTypes(btn, types){
  btn.disabled = true;
  try{
    const res = await fetch('/api/base-models/workflow-types', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: baseModelModalName, types }) });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || `실패했어요 (${res.status})`);
    setBaseModelModalError('');
    await Promise.all([loadBaseModelDetail(), fetchBaseModelFamilies()]);   // 마법사도 새 목록을 쓰게 한다
  }catch(err){ setBaseModelModalError(err.message); btn.disabled = false; }
}

function onBaseWorkflowTypeClick(btn){
  const d = baseModelDetailData, list = [...(d.workflow_types || [])];
  if(btn.id === 'base-wt-add') return saveBaseWorkflowTypes(btn, [...list, document.getElementById('base-wt-new').value]);
  const row = btn.closest('[data-wt]');
  const i = row ? list.indexOf(row.dataset.wt) : -1;
  if(i < 0) return;
  if(btn.classList.contains('base-wt-remove')) list.splice(i, 1);
  else{
    const j = btn.classList.contains('base-wt-up') ? i - 1 : i + 1;
    [list[i], list[j]] = [list[j], list[i]];
  }
  saveBaseWorkflowTypes(btn, list);
}

// 스윗 포인트 — 마법사가 빌더 spec(cfg·steps·sampler·negative)에 넣고, 접두어는 프롬프트 앞에 붙이고, 팁은 안내로 보여 준다.
function renderBaseSweet(s){
  const v = k => escapeHtml(s[k] == null ? '' : String(s[k]));
  return `<div class="dl-meta">비운 칸은 마법사의 기본값을 써요.</div>
    <div class="settings-bm-row">
      <label>cfg <input type="number" class="option-input" id="base-sweet-cfg" min="0" max="100" step="0.1" value="${v('cfg')}"></label>
      <label>steps <input type="number" class="option-input" id="base-sweet-steps" min="1" max="1000" step="1" value="${v('steps')}"></label>
      <label>sampler <input type="text" class="option-input" id="base-sweet-sampler_name" maxlength="64" placeholder="예: euler_ancestral" value="${v('sampler_name')}"></label>
    </div>
    <label class="field-label" for="base-sweet-positive_prefix">추천 positive 접두어 <span class="dl-meta">프롬프트 앞에 붙어요</span></label>
    <textarea class="option-input" id="base-sweet-positive_prefix" rows="2" maxlength="1000">${v('positive_prefix')}</textarea>
    <label class="field-label" for="base-sweet-negative">추천 negative</label>
    <textarea class="option-input" id="base-sweet-negative" rows="2" maxlength="2000">${v('negative')}</textarea>
    <label class="field-label" for="base-sweet-prompt_tips">프롬프트 팁 <span class="dl-meta">마법사에 안내로 보여요</span></label>
    <textarea class="option-input" id="base-sweet-prompt_tips" rows="3" maxlength="2000">${v('prompt_tips')}</textarea>
    <div class="settings-bm-row"><button type="button" class="submit-btn" id="base-sweet-save">스윗 포인트 저장</button></div>`;
}

async function saveBaseSweet(btn){
  const sweet = {};
  for(const k of ['cfg', 'steps', 'sampler_name', 'positive_prefix', 'negative', 'prompt_tips']){
    const val = document.getElementById(`base-sweet-${k}`).value.trim();
    if(val) sweet[k] = (k === 'cfg' || k === 'steps') ? Number(val) : val;
  }
  btn.disabled = true;
  try{
    const res = await fetch('/api/base-models/sweet', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: baseModelModalName, sweet }) });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || `실패했어요 (${res.status})`);
    setBaseModelModalError('');
    await Promise.all([loadBaseModelDetail(), fetchBaseModelFamilies()]);
  }catch(err){ setBaseModelModalError(err.message); btn.disabled = false; }
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

baseModelModal.addEventListener('change', (e) => {
  if(e.target.id === 'base-member-kind') fillBaseMemberFiles();
  if(e.target.id === 'base-wt-custom'){
    // 직접 지정으로 바꿀 때는 지금 자동으로 보이는 유형으로 시작한다
    const d = baseModelDetailData;
    const start = [...baseWorkflowCatalog().values()].filter(t => !baseWorkflowTypeInfo(t, d).warn).map(t => t.id);
    saveBaseWorkflowTypes(e.target, e.target.checked ? start : null);
  }
});
baseModelModal.addEventListener('click', (e) => {
  if(e.target === baseModelModal) return closeBaseModelModal();
  const btn = e.target.closest('button');
  if(!btn) return;
  if(btn.id === 'base-model-modal-close') return closeBaseModelModal();
  if(btn.id === 'base-wt-add' || btn.closest('[data-wt]')) return onBaseWorkflowTypeClick(btn);
  if(btn.id === 'base-sweet-save') return saveBaseSweet(btn);
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
