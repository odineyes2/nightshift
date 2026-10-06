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
  if(btn.classList.contains('base-model-up') || btn.classList.contains('base-model-down')){
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
