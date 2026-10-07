// ---- 워커 안의 "모델" 탭(#pod/{id}/pmodels) ----
// 그 워커(ComfyUI)에 무엇이 설치돼 있는지와, 대기 작업에 필요한데 그 워커에 없는 것을 한 화면에 보여 준다.
// "모두 받기"는 없는 것 중 등록부(모델 탭)에 받을 주소가 있는 것을 워커의 다운로더 노드로 한꺼번에 받는다 —
// 다 받으면 서버가 스케줄러를 깨워 막혀 있던 작업이 저절로 시작된다(GET/POST /api/pods/{id}/models…).
// 받는 중에는 3초마다 진행률을, 끝나면 설치 목록을 새로 받는다.
// "노드팩 설치 후 ComfyUI 재시작"은 등록부의 노드팩을 켜진 파드에 설치하고 ComfyUI만 다시 띄운다(파드·모델은 그대로) —
// 그동안은 3초마다 진행 기록(nodepack)을 새로 받는다.
let wmPodId = null;
let wmData = { connected: false, models: {}, needed: [] };
let wmDownloader = null;   // /api/models/downloader/status 결과
let wmTimer = null;
let wmActive = 0;          // 지난번에 본 "받는 중" 개수 — 0이 되면 설치 목록을 새로 받는다
let wmNodepackBusy = false; // 노드팩 설치·재시작 진행 중 — 끝나면 설치 목록을 새로 받는다

function openWorkerModels(podId){
  if(wmPodId !== podId){ wmData = { connected: false, models: {}, needed: [] }; wmDownloader = null; }
  wmPodId = podId;
  fetchWorkerModels(false);
  if(!wmTimer) wmTimer = setInterval(pollWorkerDownloads, 3000);
}

function closeWorkerModels(){
  if(wmTimer){ clearInterval(wmTimer); wmTimer = null; }
  wmPodId = null;
}

async function fetchWorkerModels(refresh){
  const podId = wmPodId;
  if(!podId) return;
  document.getElementById('wm-error').textContent = '';
  try{
    // 등록부도 매번 새로 받는다 — 다른 화면·기기에서 방금 등록한 모델이 "없는 것" 목록에 바로 보여야 한다.
    const [res, , dl] = await Promise.all([
      fetch(`/api/pods/${encodeURIComponent(podId)}/models${refresh ? '?refresh=true' : ''}`),
      fetchModelRegistry(),
      fetch(`/api/models/downloader/status?pod_id=${encodeURIComponent(podId)}`).then(r => r.ok ? r.json() : null).catch(() => null),
    ]);
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || '불러오지 못했어요.');
    if(podId !== wmPodId) return;
    wmData = data;
    wmDownloader = dl;
  }catch(e){
    document.getElementById('wm-error').textContent = e.message || '불러오지 못했어요.';
  }
  renderWorkerModels();
  pollWorkerDownloads();
}

async function pollWorkerDownloads(){
  const podId = wmPodId;
  if(wmNodepackBusy){ fetchWorkerModels(false); return; }   // 진행 기록을 받으면 그쪽에서 다시 불린다
  if(!podId || !(wmDownloader && wmDownloader.installed)) return;
  let items = [];
  try{
    const res = await fetch(`/api/models/downloads?pod_id=${encodeURIComponent(podId)}`);
    if(res.ok) items = (await res.json()).downloads || [];
  }catch(e){ return; }
  if(podId !== wmPodId) return;
  const active = items.filter(d => d.status === 'queued' || d.status === 'downloading');
  const recent = items.filter(d => !active.includes(d) && d.finished_at && Date.now() / 1000 - d.finished_at < 600);
  const shown = [...active, ...recent];
  document.getElementById('wm-downloads-section').style.display = shown.length ? '' : 'none';
  document.getElementById('wm-downloads-list').innerHTML = shown.map(d => {
    const pct = d.total ? Math.floor(d.downloaded / d.total * 100) : 0;
    const state = d.status === 'done' ? '다 받았어요' : d.status === 'cancelled' ? '취소됨' : d.error ? `실패 — ${d.error}`
      : d.total ? `${pct}% · ${fmtBytes(d.downloaded)} / ${fmtBytes(d.total)}` : '시작하는 중…';
    return `<div class="wm-row"><span class="wm-name" title="${escapeHtml(d.filename)}">${escapeHtml(d.filename)}</span>
      <span class="wm-kind">${escapeHtml(wmKindLabel(d.kind))}</span>
      <span class="wm-dl-state${d.error ? ' error' : ''}">${escapeHtml(state)}</span>
      ${active.includes(d) ? `<div class="progress-track wm-dl-bar"><div class="progress-fill running" style="width:${pct}%"></div></div>` : ''}</div>`;
  }).join('');
  // 받던 것이 끝났으면 설치 목록과 "없는 모델"을 새로 받는다(서버가 워커의 목록 캐시를 이미 비웠다).
  if(wmActive > 0 && active.length < wmActive) fetchWorkerModels(true);
  wmActive = active.length;
}

function wmKindLabel(kind){
  const k = (modelRegistry.kinds || []).find(x => x.id === kind);
  return k ? k.label : (kind || '');
}

// 소속 베이스 모델마다 태그 하나(여럿일 수 있다)
function wmBaseTags(e){ return basesOf(e).map(b => `<span class="wm-tag">${escapeHtml(b)}</span>`).join(''); }

const WM_NODEPACK_STATE = { queued: '대기', installing: '받는 중', pip: '패키지 설치 중', done: '설치됨', error: '실패' };

function renderNodepackStatus(){
  const run = wmData.nodepack;
  const el = document.getElementById('wm-nodepack-status');
  const busy = !!(run && (run.status === 'installing' || run.status === 'restarting'));
  if(wmNodepackBusy && !busy) fetchWorkerModels(true);   // 막 끝났다 — 새 노드가 들어온 설치 목록을 다시 받는다
  wmNodepackBusy = busy;
  el.style.display = run ? '' : 'none';
  if(!run) return;
  const head = run.status === 'installing' ? '노드팩을 설치하고 있어요 — 끝나면 ComfyUI를 다시 시작해요. 그동안 이 워커는 새 작업을 받지 않아요.'
    : run.status === 'restarting' ? 'ComfyUI를 다시 시작하고 있어요…'
    : run.message || '';
  const packs = (run.packs || []).map(p => `${escapeHtml(p.name)} — ${escapeHtml(WM_NODEPACK_STATE[p.status] || p.status)}${p.error ? `: ${escapeHtml(p.error)}` : ''}`);
  el.classList.toggle('error', run.status === 'error');
  el.innerHTML = `${run.auto ? '[자동] ' : ''}${escapeHtml(head)}${packs.length ? `<br>${packs.join('<br>')}` : ''}`;
}

function renderWorkerModels(){
  const connected = !!wmData.connected;
  renderNodepackStatus();
  document.getElementById('wm-offline').style.display = connected ? 'none' : '';
  for(const id of ['wm-needed-section', 'wm-missing-section', 'wm-installed-section']) document.getElementById(id).style.display = connected ? '' : 'none';
  if(!connected) return;

  // 대기 작업에 필요한데 없는 것
  const needed = wmData.needed || [];
  document.getElementById('wm-needed-count').textContent = needed.length ? `${needed.length}개` : '';
  document.getElementById('wm-needed-list').innerHTML = needed.length ? needed.map(n => {
    const where = n.node ? (n.installable ? '<span class="wm-tag ok" title="모델 탭에 등록된 노드팩이에요 — 위 버튼으로 설치할 수 있어요">노드팩 — 설치 가능</span>'
        : '<span class="wm-tag warn" title="모델 탭에 노드팩(github 주소)을 등록하면 여기서 설치할 수 있어요">노드 — 등록 필요</span>')
      : n.download_url ? '<span class="wm-tag ok" title="모델 탭 등록부에 받을 주소가 있어요">받을 수 있음</span>'
      : '<span class="wm-tag warn" title="모델 탭에서 이 모델의 다운로드 주소를 적어 주면 받을 수 있어요">받을 주소 없음</span>';
    // 같은 템플릿의 작업이 여럿이면 "시드 반복 ×8"처럼 묶는다
    const byLabel = new Map();
    for(const j of n.jobs) byLabel.set(j.label, (byLabel.get(j.label) || 0) + 1);
    const jobsText = [...byLabel].map(([label, c]) => c > 1 ? `${label} ×${c}` : label).join(', ');
    return `<div class="wm-row"><span class="wm-name" title="${escapeHtml(n.name)}">${escapeHtml(n.name)}</span>
      <span class="wm-kind">${escapeHtml(wmKindLabel(n.kind))}</span>${where}
      <span class="wm-jobs" title="${escapeHtml(jobsText)}">작업 ${n.jobs.length}개 — ${escapeHtml(jobsText)}</span></div>`;
  }).join('') : '<div class="comfy-model-empty">대기 작업에 필요한 모델이 이 워커에 다 있어요.</div>';

  // 받기 버튼 — 다운로더가 없으면 안내로 바꾼다
  const fetchable = needed.filter(n => !n.node && n.download_url).length;
  const hasDl = !!(wmDownloader && wmDownloader.installed);
  const btn = document.getElementById('wm-fetch-all-btn');
  btn.style.display = needed.length && hasDl ? '' : 'none';
  btn.disabled = !fetchable;
  btn.title = fetchable ? `받을 주소가 있는 ${fetchable}개를 이 워커에 받아요` : '받을 주소가 있는 모델이 없어요 — 모델 탭에서 다운로드 주소를 적어 주세요';
  const npBtn = document.getElementById('wm-nodepack-btn');
  npBtn.style.display = hasDl && needed.some(n => n.installable) ? '' : 'none';
  npBtn.disabled = wmNodepackBusy;
  const hint = document.getElementById('wm-downloader-hint');
  hint.style.display = needed.length && !hasDl ? '' : 'none';
  if(needed.length && !hasDl){
    hint.innerHTML = `이 워커의 파드에는 nightshift 다운로더가 없어서 여기서 받을 수 없어요. `
      + `<a href="/api/models/downloader/install-script?pod_id=${encodeURIComponent(wmPodId)}" target="_blank" rel="noopener">설치 스크립트</a>를 `
      + `파드의 터미널에서 실행하고 ComfyUI를 재시작하면 받을 수 있어요.`;
  }

  // 등록부(모델 탭)에 있는데 이 워커에 없는 것 — 하나씩 받는다(받을 주소가 있고 다운로더가 있을 때)
  const q = document.getElementById('wm-search').value.trim().toLowerCase();
  const models = wmData.models || {};
  const missing = Object.values(modelRegistry.items)
    .filter(e => models[e.kind] && !models[e.kind].includes(e.filename))
    .sort((a, b) => a.filename.localeCompare(b.filename, undefined, { sensitivity: 'base' }));
  const missingShown = missing.filter(e => !q || e.filename.toLowerCase().includes(q));
  document.getElementById('wm-missing-count').textContent = missing.length ? `${missing.length}개` : '';
  document.getElementById('wm-missing-list').innerHTML = missingShown.length ? missingShown.map(e => {
    const action = !e.download_url ? '<span class="wm-tag warn" title="모델 탭에서 다운로드 주소를 적어 주면 받을 수 있어요">받을 주소 없음</span>'
      : !hasDl ? '<span class="wm-tag" title="이 워커의 파드에 다운로더가 없어요 — 위 안내의 설치 스크립트를 실행하세요">다운로더 없음</span>'
      : `<button type="button" class="load-btn wm-get-btn" data-get-kind="${escapeHtml(e.kind)}" data-get-name="${escapeHtml(e.filename)}" title="이 워커에 받기">${ico('download')} 받기</button>`;
    return `<div class="wm-row"><span class="wm-name" title="${escapeHtml(e.filename)}">${escapeHtml(e.filename)}</span>
      <span class="wm-kind">${escapeHtml(wmKindLabel(e.kind))}</span>${wmBaseTags(e)}${action}</div>`;
  }).join('') : `<div class="comfy-model-empty">${q && missing.length ? '찾는 이름이 없어요.' : '모델 탭에 등록된 모델이 이 워커에 다 있어요.'}</div>`;

  // 설치된 모델 — 종류별, 이름 검색. 등록부에 없는 것은 "등록"으로 모델 탭에 들여온다(관리자만).
  const total = Object.values(models).reduce((s, list) => s + list.length, 0);
  document.getElementById('wm-installed-count').textContent = `${total}개`;
  const kinds = (modelRegistry.kinds && modelRegistry.kinds.length ? modelRegistry.kinds.map(k => k.id) : Object.keys(models));
  const groups = kinds.filter(k => (models[k] || []).length).map(kind => {
    const names = models[kind].filter(n => !q || n.toLowerCase().includes(q));
    if(!names.length) return '';
    const rows = names.map(name => {
      const reg = modelRegistry.items[modelKey(kind, name)];
      const regHtml = reg ? wmBaseTags(reg)
        : (isAdminUser() ? `<button type="button" class="load-btn wm-register-btn" data-reg-kind="${escapeHtml(kind)}" data-reg-name="${escapeHtml(name)}" title="모델 탭 등록부에 이 모델의 정보(베이스 모델·주소 등)를 적으러 가요">등록</button>`
          : '<span class="wm-tag" title="모델 탭 등록부에 없는 모델이에요">미등록</span>');
      return `<div class="wm-row"><span class="wm-name" title="${escapeHtml(name)}">${escapeHtml(name)}</span>${regHtml}</div>`;
    }).join('');
    return `<details class="wm-group"${q ? ' open' : ''}><summary>${escapeHtml(wmKindLabel(kind))} <span class="wm-count">${names.length}${q ? '' : '개'}</span></summary>${rows}</details>`;
  }).join('');
  document.getElementById('wm-installed-list').innerHTML = groups
    || `<div class="comfy-model-empty">${q ? '찾는 이름이 없어요.' : '설치된 모델이 없어요.'}</div>`;
}

document.getElementById('wm-refresh-btn').addEventListener('click', () => fetchWorkerModels(true));
document.getElementById('tab-pmodels').addEventListener('click', async (e) => {
  const reg = e.target.closest('.wm-register-btn');
  if(reg){ openModelAddFor(reg.dataset.regKind, reg.dataset.regName); return; }
  const get = e.target.closest('.wm-get-btn');
  if(!get) return;
  const entry = modelRegistry.items[modelKey(get.dataset.getKind, get.dataset.getName)];
  if(!entry) return;
  get.disabled = true;
  try{
    const res = await fetch('/api/models/download', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pod_id: wmPodId, url: entry.download_url, kind: entry.kind, filename: entry.filename }),
    });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || '받기를 시작하지 못했어요.');
    flashNotice(`${entry.filename} 받기를 시작했어요`);
  }catch(err){
    document.getElementById('wm-error').textContent = err.message;
    get.disabled = false;
  }
  pollWorkerDownloads();
});
document.getElementById('wm-search').addEventListener('input', renderWorkerModels);
document.getElementById('wm-fetch-all-btn').addEventListener('click', async () => {
  const btn = document.getElementById('wm-fetch-all-btn');
  const errorEl = document.getElementById('wm-error');
  btn.disabled = true;
  errorEl.textContent = '';
  try{
    const res = await fetch(`/api/pods/${encodeURIComponent(wmPodId)}/models/fetch-needed`, { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || '받기를 시작하지 못했어요.');
    const parts = [];
    if(data.started.length) parts.push(`${data.started.length}개 받기 시작`);
    if(data.no_url.length) parts.push(`주소 없음 ${data.no_url.length}개`);
    if(data.failed.length) parts.push(`실패 ${data.failed.length}개: ${data.failed.map(f => `${f.name}(${f.detail})`).join(', ')}`);
    flashNotice(parts.join(' · ') || '받을 것이 없었어요');
    if(data.failed.length) errorEl.textContent = parts.join(' · ');
  }catch(e){
    errorEl.textContent = e.message;
  }
  btn.disabled = false;
  pollWorkerDownloads();
});
document.getElementById('wm-nodepack-btn').addEventListener('click', async () => {
  const btn = document.getElementById('wm-nodepack-btn');
  const errorEl = document.getElementById('wm-error');
  if(!confirm('등록된 노드팩을 이 파드에 설치하고 ComfyUI를 다시 시작할까요?\n파드와 받아 둔 모델은 그대로예요. 설치가 끝날 때까지(몇 분~수십 분) 이 워커는 새 작업을 받지 않아요.')) return;
  btn.disabled = true;
  errorEl.textContent = '';
  try{
    const res = await fetch(`/api/pods/${encodeURIComponent(wmPodId)}/nodepacks/install`, { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || `시작하지 못했어요(HTTP ${res.status}).`);
    flashNotice(`노드팩 설치를 시작했어요: ${data.packs.join(', ')}`);
    wmNodepackBusy = true;
  }catch(e){
    errorEl.textContent = e.message;
    btn.disabled = false;
  }
  fetchWorkerModels(false);
});
