// ---- Settings 탭 (관리자) — Base Model · Notification · RunPod GPU 서브탭 ----
// 베이스 모델 목록은 서버 DB(base_models)가 기준이다. 목록 순서가 새 잡 마법사 1단계의 순서다.
// 코드가 이름(식별자)에 기대는 베이스 모델 — 이름을 바꾸거나 지우면 전용 워크플로우·프리셋 연결이 끊길 수 있다.
const BASE_MODEL_CODE_BOUND = /^(krea\.2|minimax-h3|wan.*|illustrious|pony|noobai)$/i;

function setSettingsError(msg){ document.getElementById('settings-error').textContent = msg || ''; }

let settingsSubtab = 'base-models';

async function openSettingsTab(){
  if(settingsSubtab !== 'service-tokens') dismissIssuedServiceToken();
  document.querySelectorAll('#settings-subtabs [data-settings-tab]').forEach(b =>
    b.classList.toggle('active', b.dataset.settingsTab === settingsSubtab));
  document.querySelectorAll('#tab-settings .settings-pane').forEach(p =>
    p.style.display = p.id === 'settings-' + settingsSubtab ? '' : 'none');
  setSettingsError('');
  if(settingsSubtab === 'notifications') return loadNotifySettings();
  if(settingsSubtab === 'runpod-gpus') return loadRunpodGpuSettings();
  if(settingsSubtab === 'auto-power') return loadAutoPowerSettings();
  if(settingsSubtab === 'moodboard') return loadBoardDisplaySettings();
  if(settingsSubtab === 'service-tokens') return loadServiceTokenSettings();
  if(settingsSubtab === 'generate-presets') return loadGeneratePresetSettings();
  await fetchModelRegistry();
  renderBaseModelSettings();
}

document.getElementById('settings-subtabs').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-settings-tab]');
  if(!btn || btn.dataset.settingsTab === settingsSubtab) return;
  settingsSubtab = btn.dataset.settingsTab;
  openSettingsTab();
});

// 서비스 토큰 원문은 현재 발급 응답에서만 화면에 두고, 새로 발급하거나 탭을 떠나면 지운다.
function dismissIssuedServiceToken(){
  document.getElementById('service-token-value').value = '';
  document.getElementById('service-token-copy-status').textContent = '';
  document.getElementById('service-token-issued').hidden = true;
}
document.getElementById('service-token-copy').addEventListener('click', async () => {
  const status = document.getElementById('service-token-copy-status');
  const value = document.getElementById('service-token-value').value;
  if(!value) return;
  try{
    await copyTextToClipboard(value);
    status.textContent = '복사했어요';
  }catch(err){
    status.textContent = '복사하지 못했어요. 직접 선택해 복사해 주세요';
  }
});
window.addEventListener('hashchange', () => { if(location.hash !== '#settings') dismissIssuedServiceToken(); });

let settingsUsers = [];
let settingsPresets = [];
let editingPresetName = null;

async function settingsAdminRequest(url, options){
  const res = await fetch(url, options);
  const data = await res.json().catch(() => ({}));
  if(!res.ok) throw new Error(data.detail || `요청하지 못했어요 (${res.status})`);
  return data;
}

async function loadServiceTokenSettings(){
  try{
    const [users, tokens] = await Promise.all([settingsAdminRequest('/api/admin/users'), settingsAdminRequest('/api/admin/service-tokens')]);
    settingsUsers = users.users.filter(u => u.role !== 'admin' && u.status === 'active');
    const picker = document.getElementById('service-token-user');
    picker.innerHTML = settingsUsers.length ? settingsUsers.map(u => `<option value="${u.id}">${escapeHtml(u.username)}</option>`).join('') : '<option value="">활성 일반 회원이 없어요</option>';
    document.querySelector('#service-token-form button[type="submit"]').disabled = !settingsUsers.length;
    document.getElementById('service-token-list').innerHTML = tokens.tokens.length ? tokens.tokens.map(t => {
      const owner = users.users.find(u => u.id === t.user_id);
      return `<div class="settings-record"><div><strong>${escapeHtml(t.name)}</strong><div class="dl-meta">${escapeHtml(owner?.username || String(t.user_id))} · 끝 4자리 ${escapeHtml(t.last4)} · ${t.revoked_at ? '폐기됨' : '사용 중'}</div></div>${t.revoked_at ? '' : `<button class="modal-btn-secondary" type="button" data-revoke-token="${t.id}">폐기</button>`}</div>`;
    }).join('') : '<div class="empty">발급한 서비스 토큰이 없어요.</div>';
  }catch(err){ setSettingsError(err.message); }
}

document.getElementById('service-token-form').addEventListener('submit', async e => {
  e.preventDefault();
  const btn = e.currentTarget.querySelector('button[type="submit"]');
  btn.disabled = true;
  try{
    const data = await settingsAdminRequest('/api/admin/service-tokens', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({user_id:Number(document.getElementById('service-token-user').value), name:document.getElementById('service-token-name').value.trim()})});
    dismissIssuedServiceToken();
    document.getElementById('service-token-value').value = data.token;
    document.getElementById('service-token-issued').hidden = false;
    document.getElementById('service-token-name').value = '';
    await loadServiceTokenSettings();
    setSettingsError('');
  }catch(err){ setSettingsError(err.message); }
  finally{ btn.disabled = false; }
});

document.getElementById('service-token-list').addEventListener('click', async e => {
  const btn = e.target.closest('[data-revoke-token]');
  if(!btn || !confirm('이 서비스 토큰을 폐기할까요?')) return;
  btn.disabled = true;
  try{
    await settingsAdminRequest(`/api/admin/service-tokens/${btn.dataset.revokeToken}`, {method:'DELETE'});
    dismissIssuedServiceToken();
    await loadServiceTokenSettings();
    setSettingsError(''); flashNotice('토큰을 폐기했어요.');
  }catch(err){ btn.disabled = false; setSettingsError(err.message); }
});

function renderGeneratePresetUsers(selected=[]){
  document.getElementById('generate-preset-users').innerHTML = settingsUsers.length ? settingsUsers.map(u => `<label><input type="checkbox" value="${u.id}"${selected.includes(u.id) ? ' checked' : ''}> ${escapeHtml(u.username)}</label>`).join('') : '<div class="empty">활성 일반 회원이 없어요.</div>';
}

function resetGeneratePresetForm(){
  editingPresetName = null;
  document.getElementById('generate-preset-form').reset();
  document.getElementById('generate-preset-aspects').value = '';
  document.getElementById('generate-preset-enabled').setAttribute('aria-checked', 'true');
  document.getElementById('generate-preset-form-title').textContent = '프리셋 추가';
  document.getElementById('generate-preset-cancel').hidden = true;
  renderGeneratePresetUsers();
}

async function loadGeneratePresetSettings(){
  try{
    const [users, data] = await Promise.all([settingsAdminRequest('/api/admin/users'), settingsAdminRequest('/api/admin/generate-presets')]);
    settingsUsers = users.users.filter(u => u.role !== 'admin' && u.status === 'active');
    settingsPresets = data.presets;
    document.getElementById('generate-preset-list').innerHTML = settingsPresets.length ? settingsPresets.map((p, i) => `<div class="settings-record"><div><strong>${escapeHtml(p.name)}</strong><div class="dl-meta">${p.width} × ${p.height} · ${p.steps}단계 · 최대 ${p.max_count}장 · ${p.enabled ? '사용 중' : '꺼짐'}</div></div><div class="settings-bm-row"><button class="modal-btn-secondary" type="button" data-edit-preset="${i}">편집</button><button class="modal-btn-secondary settings-danger" type="button" data-delete-preset="${i}">삭제</button></div></div>`).join('') : '<div class="empty">등록된 생성 프리셋이 없어요.</div>';
    if(editingPresetName && !settingsPresets.some(p => p.name === editingPresetName)) resetGeneratePresetForm();
    else renderGeneratePresetUsers(editingPresetName ? settingsPresets.find(p => p.name === editingPresetName)?.user_ids : []);
  }catch(err){ setSettingsError(err.message); }
}

document.getElementById('generate-preset-list').addEventListener('click', async e => {
  const edit = e.target.closest('[data-edit-preset]');
  const del = e.target.closest('[data-delete-preset]');
  if(edit){
    const p = settingsPresets[Number(edit.dataset.editPreset)];
    editingPresetName = p.name;
    for(const key of ['name','width','height','steps','max_count']) document.getElementById('generate-preset-' + key.replace('_','-')).value = p[key];
    document.getElementById('generate-preset-aspects').value = JSON.stringify(p.aspects, null, 2);
    document.getElementById('generate-preset-workflow').value = JSON.stringify(p.workflow, null, 2);
    document.getElementById('generate-preset-enabled').setAttribute('aria-checked', String(p.enabled));
    document.getElementById('generate-preset-form-title').textContent = `프리셋 편집: ${p.name}`;
    document.getElementById('generate-preset-cancel').hidden = false;
    renderGeneratePresetUsers(p.user_ids);
    document.getElementById('generate-preset-form').scrollIntoView({block:'nearest'});
  }
  if(del){
    const p = settingsPresets[Number(del.dataset.deletePreset)];
    if(!confirm(`${p.name} 프리셋을 삭제할까요?`)) return;
    del.disabled = true;
    try{
      await settingsAdminRequest(`/api/admin/generate-presets/${encodeURIComponent(p.name)}`, {method:'DELETE'});
      if(editingPresetName === p.name) resetGeneratePresetForm();
      await loadGeneratePresetSettings(); setSettingsError(''); flashNotice('프리셋을 삭제했어요.');
    }catch(err){ del.disabled = false; setSettingsError(err.message); }
  }
});

document.getElementById('generate-preset-enabled').addEventListener('click', e => e.currentTarget.setAttribute('aria-checked', String(e.currentTarget.getAttribute('aria-checked') !== 'true')));
document.getElementById('generate-preset-cancel').addEventListener('click', resetGeneratePresetForm);
document.getElementById('generate-preset-form').addEventListener('submit', async e => {
  e.preventDefault();
  const btn = document.getElementById('generate-preset-save');
  let body;
  try{
    const width = Number(document.getElementById('generate-preset-width').value);
    const height = Number(document.getElementById('generate-preset-height').value);
    const aspectsText = document.getElementById('generate-preset-aspects').value.trim();
    body = {
      name:document.getElementById('generate-preset-name').value.trim(), width, height,
      steps:Number(document.getElementById('generate-preset-steps').value),
      max_count:Number(document.getElementById('generate-preset-max-count').value),
      aspects:aspectsText ? JSON.parse(aspectsText) : {default:[width,height]},
      workflow:JSON.parse(document.getElementById('generate-preset-workflow').value),
      user_ids:[...document.querySelectorAll('#generate-preset-users input:checked')].map(x => Number(x.value)),
      enabled:document.getElementById('generate-preset-enabled').getAttribute('aria-checked') === 'true',
    };
  }catch(err){ setSettingsError('비율별 크기와 워크플로를 올바른 JSON으로 입력해 주세요.'); return; }
  btn.disabled = true; btn.textContent = '저장 중…';
  try{
    const path = editingPresetName ? `/api/admin/generate-presets/${encodeURIComponent(editingPresetName)}` : '/api/admin/generate-presets';
    await settingsAdminRequest(path, {method:editingPresetName ? 'PUT' : 'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    resetGeneratePresetForm(); await loadGeneratePresetSettings(); setSettingsError(''); flashNotice('프리셋을 저장했어요.');
  }catch(err){ setSettingsError(err.message); }
  finally{ btn.disabled = false; btn.textContent = '프리셋 저장'; }
});

// 선택은 미리보기에만 반영하고, 저장이 성공했을 때 보드에 적용한다.
function boardDisplayChoice(){
  return {
    arrow_size: document.querySelector('[name="board-arrow-size"]:checked')?.value || 'medium',
    port_size: document.querySelector('[name="board-port-size"]:checked')?.value || 'medium',
  };
}

function renderBoardDisplayPreview(){
  const choice = boardDisplayChoice();
  const arrow = BOARD_ARROW_SIZES[choice.arrow_size];
  const port = BOARD_PORT_SIZES[choice.port_size];
  document.getElementById('board-display-preview').innerHTML = `<svg viewBox="0 0 260 100" role="img" aria-label="입구 점으로 들어가는 연결선">
    ${arrow ? `<defs><marker id="board-preview-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="${arrow}" markerHeight="${arrow}" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="var(--text-dim)"/></marker></defs>` : ''}
    <path d="M20,72 C85,72 90,50 178,50" fill="none" stroke="var(--text-dim)" stroke-width="2"${arrow ? ' marker-end="url(#board-preview-arrow)"' : ''}/>
    <circle cx="180" cy="50" r="${port / 2}" fill="var(--panel)" stroke="var(--running)" stroke-width="2"/>
  </svg>`;
}

async function loadBoardDisplaySettings(){
  try{
    const res = await fetch('/api/settings/board-display');
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || `설정을 불러오지 못했어요 (${res.status})`);
    for(const [name, size] of [['board-arrow-size', data.arrow_size], ['board-port-size', data.port_size]]){
      const radio = document.querySelector(`[name="${name}"][value="${size}"]`);
      if(radio) radio.checked = true;
    }
    document.getElementById('board-display-state').textContent = '';
    renderBoardDisplayPreview();
  }catch(err){ setSettingsError(err.message); }
}

document.getElementById('settings-moodboard').addEventListener('change', (e) => {
  if(e.target.matches('[type="radio"]')){
    document.getElementById('board-display-state').textContent = '저장하면 보드에 적용돼요.';
    renderBoardDisplayPreview();
  }
});

document.getElementById('board-display-save').addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  try{
    const res = await fetch('/api/settings/board-display', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(boardDisplayChoice()) });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || `저장하지 못했어요 (${res.status})`);
    applyBoardDisplay(data);
    setSettingsError('');
    document.getElementById('board-display-state').textContent = '저장했어요.';
  }catch(err){ setSettingsError(err.message); }
  finally{ btn.disabled = false; }
});

// ---- RunPod GPU — 서버의 패밀리별 순서를 편집하고 한 번에 저장한다 ----
let runpodGpuFamilies = null;
let runpodGpuCatalog = [];
let runpodGpuExcluded = [];
let runpodGpuSaving = false;
let runpodGpuCatalogError = null;

async function loadRunpodGpuSettings(){
  const el = document.getElementById('settings-runpod-gpus');
  el.innerHTML = '<div class="dl-meta">GPU 설정을 불러오는 중…</div>';
  try{
    const [settingsRes, catalogRes] = await Promise.all([
      fetch('/api/settings/runpod-gpus'), fetch('/api/settings/runpod-gpus/catalog')
    ]);
    const settings = await settingsRes.json().catch(() => ({}));
    const catalog = await catalogRes.json().catch(() => ({}));
    if(!settingsRes.ok) throw new Error(settings.detail || `설정을 불러오지 못했어요 (${settingsRes.status})`);
    if(!catalogRes.ok) throw new Error(catalog.detail || `GPU 목록을 불러오지 못했어요 (${catalogRes.status})`);
    runpodGpuFamilies = Object.fromEntries(Object.entries(settings.families).map(([id, f]) =>
      [id, { gpus: [...f.gpus], clouds: [...f.clouds], configured: f.configured }]));
    runpodGpuCatalog = catalog.gpus || [];
    runpodGpuExcluded = catalog.excluded_gpus || settings.excluded_gpus || [];
    runpodGpuCatalogError = catalog.error;
    renderRunpodGpuSettings();
  }catch(err){ el.innerHTML = ''; setSettingsError(err.message); }
}

function runpodGpuPrice(value){
  if(value === null || value === undefined || value === '') return '가격 정보 없음';
  const price = Number(value);
  return Number.isFinite(price) && price >= 0 ? `$${price.toFixed(2)}/hr` : '가격 정보 없음';
}

function renderRunpodGpuSettings(){
  const el = document.getElementById('settings-runpod-gpus');
  const names = { image: '이미지용', video: '영상용' };
  el.innerHTML = `<div class="dl-meta">GPU를 켜고 시도 순서를 정해 주세요. 클라우드는 위에 있는 것부터 시도해요. 각 클라우드에서 일본을 먼저 시도해요. GPU 종류 목록은 실시간 재고나 CUDA 호환을 보장하지 않아요.</div>
    ${runpodGpuCatalogError ? `<div class="settings-gpu-warning">RunPod GPU 목록을 조회하지 못했어요. 기본·저장 후보를 보여 드려요: ${escapeHtml(runpodGpuCatalogError)}</div>` : ''}
    ${Object.entries(runpodGpuFamilies).map(([id, f]) => {
      const byId = new Map(runpodGpuCatalog.map(g => [g.id, g]));
      const ids = [...f.gpus, ...runpodGpuCatalog.map(g => g.id).filter(g => !f.gpus.includes(g))];
      return `<section class="settings-gpu-family" data-family="${escapeHtml(id)}">
        <div class="section-label">${names[id] || escapeHtml(id)} <span class="dl-meta">${f.configured ? '저장된 설정' : '기본 설정'}</span></div>
        <div class="settings-gpu-clouds" role="group" aria-label="${names[id] || escapeHtml(id)} 클라우드">
          ${[...f.clouds, ...['SECURE', 'COMMUNITY'].filter(c => !f.clouds.includes(c))].map((c, index) => `<div class="settings-gpu-cloud">
            <button type="button" class="pod-switch" role="switch" data-cloud="${c}" aria-checked="${f.clouds.includes(c)}" aria-label="${c} 클라우드 사용"></button>
            <span class="settings-gpu-cloud-name">${c === 'SECURE' ? 'Secure' : 'Community'}</span>
            <span class="settings-bm-move"><button type="button" class="modal-btn-secondary" data-cloud-move="up" data-cloud-name="${c}" aria-label="${c} 클라우드 위로"${index === 0 || index >= f.clouds.length ? ' disabled' : ''}>↑</button>
            <button type="button" class="modal-btn-secondary" data-cloud-move="down" data-cloud-name="${c}" aria-label="${c} 클라우드 아래로"${index >= f.clouds.length - 1 ? ' disabled' : ''}>↓</button></span>
          </div>`).join('')}
        </div>
        <div class="settings-gpu-list">${ids.map(gpu => {
          const info = byId.get(gpu) || { displayName: gpu };
          const on = f.gpus.includes(gpu), index = f.gpus.indexOf(gpu), excluded = runpodGpuExcluded.includes(gpu);
          return `<div class="settings-gpu-row" data-gpu="${escapeHtml(gpu)}">
            <button type="button" class="pod-switch" role="switch" data-gpu-toggle aria-checked="${on}" aria-label="${escapeHtml(info.displayName)} 사용"${excluded && !on ? ' disabled' : ''}></button>
            <span class="settings-gpu-name">${escapeHtml(info.displayName)} <span class="dl-meta">${info.memoryInGb ? `${escapeHtml(String(info.memoryInGb))}GB · ` : ''}Secure ${runpodGpuPrice(info.securePrice)} · Community ${runpodGpuPrice(info.communityPrice)}${excluded ? ' · 현재 제외된 GPU' : ''}${!byId.has(gpu) ? ' · 카탈로그에 없음' : ''}</span></span>
            <span class="settings-bm-move"><button type="button" class="modal-btn-secondary" data-gpu-move="up" aria-label="${escapeHtml(info.displayName)} 위로"${!on || index === 0 ? ' disabled' : ''}>↑</button>
            <button type="button" class="modal-btn-secondary" data-gpu-move="down" aria-label="${escapeHtml(info.displayName)} 아래로"${!on || index === f.gpus.length - 1 ? ' disabled' : ''}>↓</button></span>
          </div>`;
        }).join('')}</div>
      </section>`;
    }).join('')}
    <div class="settings-gpu-actions"><button type="button" class="submit-btn" id="runpod-gpu-save"${runpodGpuSaving ? ' disabled' : ''}>${runpodGpuSaving ? '저장 중…' : 'GPU 설정 저장'}</button><span class="dl-meta" id="runpod-gpu-state"></span></div>`;
}

document.getElementById('settings-runpod-gpus').addEventListener('click', async (e) => {
  const btn = e.target.closest('button');
  if(!btn || runpodGpuSaving || !runpodGpuFamilies) return;
  if(btn.id === 'runpod-gpu-save'){
    runpodGpuSaving = true;
    btn.disabled = true; btn.textContent = '저장 중…';
    try{
      const body = Object.fromEntries(Object.entries(runpodGpuFamilies).map(([id, f]) =>
        [id, { gpus: f.gpus, clouds: f.clouds }]));
      const res = await fetch('/api/settings/runpod-gpus', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const data = await res.json().catch(() => ({}));
      if(!res.ok) throw new Error(data.detail || `저장하지 못했어요 (${res.status})`);
      runpodGpuFamilies = Object.fromEntries(Object.entries(data.families).map(([id, f]) => [id, { ...f }]));
      runpodGpuSaving = false;
      setSettingsError('');
      renderRunpodGpuSettings();
      document.getElementById('runpod-gpu-state').textContent = '저장했어요.';
      flashNotice('GPU 설정을 저장했어요.');
    }catch(err){ setSettingsError(err.message); btn.disabled = false; btn.textContent = 'GPU 설정 저장'; }
    finally{ runpodGpuSaving = false; }
    return;
  }
  const section = btn.closest('[data-family]');
  if(!section) return;
  const f = runpodGpuFamilies[section.dataset.family];
  const row = btn.closest('[data-gpu]');
  if(btn.dataset.cloud){
    const cloud = btn.dataset.cloud;
    if(f.clouds.includes(cloud)){
      if(f.clouds.length === 1){ setSettingsError('클라우드를 하나 이상 켜 주세요.'); return; }
      f.clouds = f.clouds.filter(c => c !== cloud);
    }else f.clouds.push(cloud);
  }else if(btn.dataset.cloudMove){
    const i = f.clouds.indexOf(btn.dataset.cloudName), j = i + (btn.dataset.cloudMove === 'up' ? -1 : 1);
    if(i < 0 || j < 0 || j >= f.clouds.length) return;
    [f.clouds[i], f.clouds[j]] = [f.clouds[j], f.clouds[i]];
  }else if(row && btn.hasAttribute('data-gpu-toggle')){
    const gpu = row.dataset.gpu;
    if(f.gpus.includes(gpu)){
      if(f.gpus.length === 1){ setSettingsError('GPU를 하나 이상 켜 주세요.'); return; }
      f.gpus = f.gpus.filter(g => g !== gpu);
    }else f.gpus.push(gpu);
  }else if(row && btn.dataset.gpuMove){
    const i = f.gpus.indexOf(row.dataset.gpu), j = i + (btn.dataset.gpuMove === 'up' ? -1 : 1);
    if(i < 0 || j < 0 || j >= f.gpus.length) return;
    [f.gpus[i], f.gpus[j]] = [f.gpus[j], f.gpus[i]];
  }else return;
  setSettingsError('');
  renderRunpodGpuSettings();
  document.getElementById('runpod-gpu-state').textContent = '저장되지 않은 변경 사항이 있어요.';
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

// ---- 자동 켜기 서브탭(관리자) — 대기 작업이 모이면 이미지·영상 RunPod 워커를 켠다. 끄기는 워커의 자동 끄기(NS-8). ----
// 영상 칸에는 "영상 파드 사용"(서비스 연동 영상 생성을 받을지, 기본 꺼짐)이 더 있다. 등급마다 따로 저장한다.
const AUTO_POWER_TIERS = {
  image: { url: '/api/settings/auto-power', label: '이미지',
           intro: '기다리는 이미지 작업(charchat 삽화 포함)이 정한 개수만큼 모이면 이미지용 RunPod 워커를 자동으로 켜요. 일이 끝나면 그 워커의 자동 끄기로 꺼져요(켤 때 자동 끄기를 켜 둬요).' },
  video: { url: '/api/settings/auto-power/video', label: '영상',
           intro: 'charchat 루프 영상 같은 서비스 연동 영상 작업은 영상용 RunPod 워커에서만 돌아요. 영상 파드 사용을 켜야 영상 작업을 받고, 자동 켜기를 켜면 영상 작업이 모일 때 영상 워커를 켜요.' },
};
const autoPower = { image: null, video: null };
let autoPowerSaving = false;

async function loadAutoPowerSettings(){
  const el = document.getElementById('settings-auto-power');
  el.innerHTML = '<div class="dl-meta">자동 켜기 설정을 불러오는 중…</div>';
  try{
    for(const tier of Object.keys(AUTO_POWER_TIERS)){
      const res = await fetch(AUTO_POWER_TIERS[tier].url);
      const data = await res.json().catch(() => ({}));
      if(!res.ok) throw new Error(data.detail || `설정을 불러오지 못했어요 (${res.status})`);
      autoPower[tier] = data;
    }
    el.innerHTML = Object.keys(AUTO_POWER_TIERS).map(tier => `<section class="auto-power-tier" data-tier="${tier}"></section>`).join('');
    for(const tier of Object.keys(AUTO_POWER_TIERS)) renderAutoPowerSettings(tier);
  }catch(err){ el.innerHTML = ''; setSettingsError(err.message); }
}

function renderAutoPowerSettings(tier){
  const el = document.querySelector(`#settings-auto-power [data-tier="${tier}"]`);
  const info = AUTO_POWER_TIERS[tier], data = autoPower[tier];
  const s = data.settings, st = data.status, lim = data.limits;
  const num = (key, label, unit, help) => `<label class="auto-power-field"><span class="field-label">${label}</span>
    <span class="auto-power-input"><input type="number" inputmode="numeric" data-auto-power="${key}" min="${lim[key][0]}" max="${lim[key][1]}" value="${s[key]}"> ${unit}</span>
    <span class="dl-meta">${help}</span></label>`;
  const toggle = (key, label, on) => `<div class="auto-power-switch"><button type="button" class="pod-switch" role="switch" data-auto-power-switch="${key}" aria-checked="${on}" aria-label="${label}"></button><span data-label="${label}">${label} ${on ? '사용 중' : '꺼짐'}</span></div>`;
  const options = data.candidates.map(p => `<option value="${escapeHtml(p.id)}"${s.pod_id === p.id ? ' selected' : ''}>${escapeHtml(p.name)}${p.on ? ' · 켜짐' : ''}</option>`).join('');
  el.innerHTML = `<h3 class="auto-power-title">${info.label} 워커</h3>
    <div class="dl-meta">${info.intro}</div>
    ${tier === 'video' ? toggle('video_jobs', '영상 파드 사용', s.video_jobs) : ''}
    ${toggle('enabled', `${info.label} 자동 켜기`, s.enabled)}
    ${toggle('paid_immediate', '유료 회원 작업은 바로 켜기', s.paid_immediate !== false)}
    <div class="dl-meta">유료 회원(회원 관리에서 기간을 정한 회원)의 charchat 작업은 관리자 ${info.label} 워커에서 돌아요. 켜 두면 그 작업이 하나만 와도 위 자동 켜기와 상관없이 바로 켜요(하루 상한은 같이 적용).</div>
    <div class="auto-power-fields">
      ${num('min_jobs', '자동으로 켜는 대기 작업 수', '개', '이만큼 모이면 켜요.')}
      ${num('max_wait_hours', '최대 대기 시간', '시간', '가장 오래된 작업이 이만큼 기다리면 개수가 모자라도 켜요. 0이면 개수만 봐요.')}
      ${num('daily_limit', '하루 자동 켜기 상한', '번', '상한에 닿으면 그날은 더 켜지 않아요.')}
      <label class="auto-power-field"><span class="field-label">켤 워커</span>
        <select data-auto-power-pod><option value="">자동으로 고르기(최근에 쓴 ${info.label} 워커)</option>${options}</select>
        <span class="dl-meta">nightshift가 만든 ${info.label}용 RunPod 워커만 고를 수 있어요.</span></label>
    </div>
    <div class="auto-power-status" role="status"><b>지금</b> 기다리는 ${info.label} 작업 ${st.waiting}개${st.waiting ? ` · 가장 오래된 작업 ${Math.floor(st.oldest_hours)}시간 대기` : ''} · 오늘 자동으로 켠 횟수 ${st.starts_today}/${s.daily_limit}<br>${escapeHtml(st.reason)}
      ${data.last.last_reason ? `<br><span class="dl-meta">마지막 자동 켜기: ${escapeHtml(data.last.last_reason)}</span>` : ''}</div>
    <div class="settings-gpu-actions"><button type="button" class="submit-btn" data-auto-power-save${autoPowerSaving ? ' disabled' : ''}>${info.label} 설정 저장</button><span class="dl-meta" data-auto-power-state></span></div>`;
}

document.getElementById('settings-auto-power').addEventListener('click', async (e) => {
  const btn = e.target.closest('button');
  const section = btn && btn.closest('[data-tier]');
  if(!btn || !section || autoPowerSaving) return;
  const tier = section.dataset.tier, info = AUTO_POWER_TIERS[tier];
  if(btn.dataset.autoPowerSwitch){
    const on = btn.getAttribute('aria-checked') !== 'true';
    btn.setAttribute('aria-checked', String(on));
    const label = btn.nextElementSibling;
    label.textContent = `${label.dataset.label} ${on ? '사용 중' : '꺼짐'} · 저장해야 적용돼요`;
    return;
  }
  if(!btn.hasAttribute('data-auto-power-save')) return;
  const body = { pod_id: section.querySelector('[data-auto-power-pod]').value || null };
  for(const sw of section.querySelectorAll('[data-auto-power-switch]')) body[sw.dataset.autoPowerSwitch] = sw.getAttribute('aria-checked') === 'true';
  for(const input of section.querySelectorAll('[data-auto-power]')){
    const value = Number(input.value);
    if(!Number.isInteger(value)){ setSettingsError('숫자를 확인해 주세요.'); input.focus(); return; }
    body[input.dataset.autoPower] = value;
  }
  autoPowerSaving = true; btn.disabled = true; btn.textContent = '저장 중…';
  try{
    const res = await fetch(info.url, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || `저장하지 못했어요 (${res.status})`);
    autoPower[tier] = data; autoPowerSaving = false;
    setSettingsError('');
    renderAutoPowerSettings(tier);
    section.querySelector('[data-auto-power-state]').textContent = '저장했어요.';
    flashNotice(`${info.label} 자동 켜기 설정을 저장했어요.`);
  }catch(err){ setSettingsError(err.message); btn.disabled = false; btn.textContent = `${info.label} 설정 저장`; }
  finally{ autoPowerSaving = false; }
});
