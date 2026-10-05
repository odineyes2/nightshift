// ---- 대시보드 (파드 카드 캔버스) ----
// 카드마다 타입이 있고, 타입별 렌더러가 그 카드의 HTML을 만든다. 지금은 파드 카드
// 하나뿐이지만, 통계·최근 산출물·메모 같은 카드를 나중에 같은 그리드에 얹을 수 있게
// 이 구조로 둔다(카드가 자기 크기를 span으로 정한다).
let dashSummary = { pods: [], totals: {} };
let dashTimer = null;

const POD_STATE = {
  disabled: { cls: 'disabled', label: '사용 안 함' },
  busy:     { cls: 'busy',     label: '작업 중' },
  online:   { cls: 'online',   label: '유휴' },
  offline:  { cls: 'offline',  label: '연결 안 됨' },
};

function podState(p){
  if(!p.enabled) return 'disabled';
  if(!p.status || !p.status.connected) return 'offline';
  return (p.running_jobs || []).length ? 'busy' : 'online';
}

// 파드 카드를 "일꾼 로봇"처럼 보이게 하는 아바타. 몸통 색은 파드 id를 해시해 고정
// 팔레트에서 고르므로(같은 파드는 새로고침해도 항상 같은 색), 카드가 여럿이어도
// 서로 구분이 된다. 표정/애니메이션은 podState()를 그대로 따라간다 — 상태를 두 번
// 정의하고 싶지 않아서 dash-dot과 같은 값을 쓴다.
const ROBOT_BODY_COLORS = ['#7c9cff', '#ff9f7c', '#7cd9a8', '#ffd27c', '#c495ff', '#7cd3f0', '#ff8fb3', '#a8d86f'];

function hashToIndex(str, mod){
  let h = 0;
  for(let i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) >>> 0;
  return h % mod;
}

function renderPodRobot(p){
  const state = podState(p);
  const body = ROBOT_BODY_COLORS[hashToIndex(p.id || '', ROBOT_BODY_COLORS.length)];
  const lightColor = { online: 'var(--done)', busy: 'var(--running)', offline: 'var(--failed)', disabled: '#9aa0a6' }[state];

  let face;
  if(state === 'offline'){
    // 연결이 끊긴 로봇 — 눈을 X로, 입은 옅게.
    face = `
      <path d="M12 19l6 6M18 19l-6 6" stroke="#22262b" stroke-width="2" stroke-linecap="round" opacity="0.65"/>
      <path d="M22 19l6 6M28 19l-6 6" stroke="#22262b" stroke-width="2" stroke-linecap="round" opacity="0.65"/>
      <rect x="14" y="28" width="12" height="2" rx="1" fill="#22262b" opacity="0.35"/>`;
  }else if(state === 'disabled'){
    // 꺼둔 로봇 — 눈을 감고 잠들어 있다.
    face = `
      <rect x="12" y="21" width="6" height="2" rx="1" fill="#22262b" opacity="0.45"/>
      <rect x="22" y="21" width="6" height="2" rx="1" fill="#22262b" opacity="0.45"/>
      <rect x="16" y="28" width="8" height="1.6" rx="0.8" fill="#22262b" opacity="0.3"/>`;
  }else{
    // 유휴/작업 중 — 눈을 뜨고 있고, 작업 중일 때만 입이 일자(집중)로 바뀐다.
    const mouth = state === 'busy'
      ? `<rect x="15" y="28" width="10" height="2.4" rx="1.2" fill="#22262b" opacity="0.55"/>`
      : `<path d="M15 28.5 Q20 31.5 25 28.5" stroke="#22262b" stroke-width="2" fill="none" stroke-linecap="round" opacity="0.55"/>`;
    face = `
      <circle cx="15" cy="22" r="3.2" fill="#fff"/><circle cx="25" cy="22" r="3.2" fill="#fff"/>
      <circle cx="15" cy="22" r="1.5" fill="#22262b"/><circle cx="25" cy="22" r="1.5" fill="#22262b"/>
      ${mouth}`;
  }

  return `
    <svg class="dash-robot-svg dash-robot-${state}" width="36" height="36" viewBox="0 0 40 40" aria-hidden="true">
      <line x1="20" y1="4" x2="20" y2="10" stroke="#8a8f98" stroke-width="2" stroke-linecap="round"/>
      <circle class="dash-robot-light" cx="20" cy="4" r="2.6" fill="${lightColor}"/>
      <rect x="6" y="10" width="28" height="24" rx="8" fill="${body}" stroke="rgba(0,0,0,0.12)"/>
      <circle cx="6" cy="22" r="2" fill="rgba(0,0,0,0.15)"/>
      <circle cx="34" cy="22" r="2" fill="rgba(0,0,0,0.15)"/>
      ${face}
    </svg>`;
}

function fmtVram(free, total){
  if(!total) return null;
  const gb = v => (v / (1024 ** 3)).toFixed(1);
  return `${gb(total - (free || 0))}/${gb(total)}GB`;
}

// 디스크/VRAM처럼 큰 값을 위한 표기. 갤러리의 fmtBytes(파일 하나 크기, MB까지)와는
// 쓰임이 다르므로 이름을 따로 둔다 — 같은 이름으로 두 번 선언하면 나중 것이 이겨서
// 조용히 엉뚱한 단위가 나온다.
function fmtDiskSize(n){
  if(!n && n !== 0) return null;
  const gb = n / (1024 ** 3);
  return gb >= 1 ? `${gb.toFixed(1)}GB` : `${(n / (1024 ** 2)).toFixed(0)}MB`;
}

// RunPod API가 붙어 있는 파드(url이 RunPod 프록시 주소이고 서버에 RUNPOD_API_KEY가
// 설정된 경우)라면 card.runpod에 이름/GPU/비용/일시가 실려 온다. 하나도 없으면(키
// 미설정 등) 조용히 빈 줄을 돌려준다 — 카드에 그 부분만 안 보이면 될 뿐.
function renderRunpodLine(info){
  if(!info) return '';
  const parts = [];
  if(info.pod_name) parts.push(`RunPod 이름 <b>${escapeHtml(info.pod_name)}</b>`);
  if(info.gpu_type) parts.push(escapeHtml(info.gpu_type));
  if(info.cost_per_hr != null) parts.push(`$${Number(info.cost_per_hr).toFixed(2)}/hr`);
  if(info.created_at) parts.push(`생성 ${fmtGalleryDateTime(info.created_at)}`);
  if(info.last_started_at) parts.push(`최근 시작 ${fmtGalleryDateTime(info.last_started_at)}`);
  if(!parts.length) return '';
  return `<div class="dash-card-runpod">${parts.map(p => `<span>${p}</span>`).join('')}</div>`;
}

// 워커를 켜고 끄는 것은 세 가지이고 서로 다른 차원이라, 헷갈리지 않게 설정 창 한곳(renderPodControls)에서만 바꾼다.
//   워커 사용   — nightshift가 이 워커로 새 작업을 보낼지(pod.enabled). 설정·기록은 남는다.
//   작업 처리   — 이 워커가 대기 작업을 차례로 처리할지(auto_run). 멈추면 실행 중인 작업도 중단된다.
//   RunPod 전원 — RunPod 파드 자체를 켜고 끈다(과금). 관리자에게만, RunPod 상태를 아는 워커에만.
// 카드에는 버튼 없이 상태만 보여 주고, 누르면 설정 창이 열린다.
// RunPod 전원은 누른 뒤 상태가 실제로 바뀔 때까지(요청 몇 초 + 정보 갱신) "켜는 중…/끄는 중…"으로 붙잡아 둔다 —
// 4초마다 다시 그리면서 스위치가 원래대로 돌아가 "눌렸나?"가 되지 않게.
// { podId: { action, target, until } } — 목표 상태가 보이거나, 실패하거나, 90초가 지나면 푼다.
const runpodPowerPending = {};

// RunPod 전원 상태 — { on, busy, label } 또는 null(RunPod 전원을 다룰 수 없는 워커).
// nightshift가 만든 RunPod 워커(NS-6) — 전원 끄기=파드 지우기(워커는 남김), 켜기=같은 등급으로 새 파드.
const RUNPOD_TIER_LABEL = { image: '이미지용', video: '영상용' };
function managedRunpodTier(podId){ const w = podsById[podId] || {}; return RUNPOD_TIER_LABEL[w.runpod_tier] ? w.runpod_tier : null; }

function runpodPowerState(p){
  const rp = p.card && p.card.runpod;
  const tier = managedRunpodTier(p.id);
  const noPod = tier && !((podsById[p.id] || {}).url);
  const pending = runpodPowerPending[p.id];
  if(pending && ((rp && rp.status === pending.target) || (pending.target === 'TERMINATED' && noPod) || Date.now() > pending.until)) delete runpodPowerPending[p.id];
  if(!isAdminUser()) return null;
  if(runpodPowerPending[p.id]){
    const starting = runpodPowerPending[p.id].action === 'start';
    return { on: starting, busy: true, label: starting ? (tier ? '새 파드를 만드는 중… (재고를 찾느라 1분, ComfyUI까지 1~3분)' : '켜는 중… (1~3분 뒤 ComfyUI가 떠요)')
                                                       : (tier ? '파드를 지우는 중…' : '끄는 중…') };
  }
  if(noPod) return { on: false, busy: false, label: `${p.boot && p.boot.failed ? 'ComfyUI가 끝내 뜨지 않아 파드를 지웠어요' : '꺼져 있어요'} — 파드 없음, 과금 없음. 켜면 ${RUNPOD_TIER_LABEL[tier]} 새 파드를 만들어요` };
  if(!rp || !rp.status) return null;
  const cost = rp.cost_per_hr != null ? ` · $${Number(rp.cost_per_hr).toFixed(2)}/hr` : '';
  if(rp.status === 'RUNNING') return { on: true, busy: false, label: `켜져 있어요 — 과금 중${cost}${tier ? ' · 끄면 파드를 지워요(워커는 남음)' : ''}` };
  if(rp.status === 'EXITED' || rp.status === 'ERROR') return { on: false, busy: false, label: `꺼져 있어요 — 과금 없음${rp.status === 'ERROR' ? ' (오류로 멈춤)' : ''}` };
  if(rp.status === 'PROVISIONING' || rp.status === 'STARTING') return { on: true, busy: true, label: `켜지는 중이에요 — 과금 중${cost}` };
  return { on: false, busy: true, label: `RunPod 상태: ${rp.status}` };
}

// 카드의 상태 표시 — 누르면 설정 창(켜기/끄기)이 열린다. 워커 사용 꺼짐은 카드 자체(회색·"사용 안 함")가 보여 준다.
function renderPodChips(p){
  const chips = [];
  if(p.enabled) chips.push(p.auto_run ? ['on', '작업 처리 켜짐'] : ['off', '작업 처리 멈춤']);
  const power = runpodPowerState(p);
  if(power) chips.push(power.busy ? ['busy', power.label.split(' (')[0]] : power.on ? ['on', 'RunPod 켜짐'] : ['off', 'RunPod 꺼짐']);
  return chips.map(([cls, label]) => `<button type="button" class="pod-chip ${cls}" data-pod-edit="${escapeHtml(p.id)}" title="눌러서 켜기/끄기 설정 열기">${escapeHtml(label)}</button>`).join('');
}

async function runpodPower(podId, action){
  const pod = (dashSummary.pods || []).find(x => x.id === podId) || {};
  const rp = (pod.card && pod.card.runpod) || {};
  const cost = rp.cost_per_hr != null ? `시간당 $${Number(rp.cost_per_hr).toFixed(2)}` : '요금';
  const tier = managedRunpodTier(podId);
  const msg = tier
    ? (action === 'start'
      ? `'${pod.name || podId}'에 ${RUNPOD_TIER_LABEL[tier]} RunPod 파드를 새로 만들까요?\n만드는 순간부터 요금이 나가요. ComfyUI까지 1~3분, 필요한 모델은 작업이 오면 다시 받아요(큰 모델은 10분 넘게 걸릴 수 있어요).`
      : `'${pod.name || podId}'의 RunPod 파드를 지울까요?\n과금이 완전히 멈추고, 받아 둔 모델도 함께 지워져요. 워커는 남고, 다시 켜면 새 파드를 만들어요.`)
    : (action === 'start'
      ? `'${pod.name || podId}'의 RunPod 파드를 켤까요?\n켜는 순간부터 ${cost}이 청구돼요. ComfyUI가 뜨기까지 1~3분 걸려요.`
      : `'${pod.name || podId}'의 RunPod 파드를 끌까요?\n과금이 멈추고, 네트워크 볼륨의 데이터는 그대로 남아요.`);
  if(!confirm(msg)) return;
  runpodPowerPending[podId] = { action, target: action === 'start' ? 'RUNNING' : (tier ? 'TERMINATED' : 'EXITED'), until: Date.now() + (tier && action === 'start' ? 240000 : 90000) };
  renderPodControls();   // 누르자마자 "켜는 중…"으로
  try{
    const res = await fetch(`/api/pods/${encodeURIComponent(podId)}/runpod/${action}`, { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if(!res.ok){ delete runpodPowerPending[podId]; alert(data.detail || `실패했어요(HTTP ${res.status}).`); }
    else if(data.managed){
      await fetchPods();   // 주소가 바뀌었다(새 파드 / 비움)
      const rp = data.runpod || {};
      flashNotice(data.status === 'RUNNING'
        ? `새 파드를 만들었어요 — ${[rp.gpu, rp.data_center, rp.cost_per_hr != null ? `$${rp.cost_per_hr}/hr` : ''].filter(Boolean).join(' · ')}. ComfyUI까지 1~3분.`
        : '파드를 지웠어요 — 과금이 멈췄어요. 다시 켜면 새 파드를 만들어요.');
    }
  }catch(e){
    delete runpodPowerPending[podId];
    alert(`요청하지 못했어요: ${e.message}`);
  }
  await fetchDashboard();
}

// 설정 창의 스위치들 — 누르면 바로 적용된다(새 워커의 결과 가져오기만 "저장" 때 함께). 대시보드를 새로 받을 때마다 다시 그린다.
// 워커 사용·작업 처리는 기존 워커일 때만. RunPod 줄은 RunPod 정보만 있어도 보인다(새 워커 추가 중
// "RunPod 정보 테스트"를 눌렀을 때 — 이때 스위치는 없다).
function renderPodControls(){
  const p = podEditId && (dashSummary.pods || []).find(x => x.id === podEditId);
  const hasInfo = document.getElementById('pod-edit-runpod-fields').children.length > 0;
  const power = p ? runpodPowerState(p) : null;
  for(const id of ['pod-ctl-enabled-row', 'pod-ctl-queue-row']) document.getElementById(id).style.display = p ? '' : 'none';
  document.getElementById('pod-ctl-power-row').style.display = power || hasInfo ? '' : 'none';
  document.getElementById('pod-ctl-power').style.display = power ? '' : 'none';
  document.getElementById('pod-ctl-power-title').textContent = power ? 'RunPod 전원' : 'RunPod 정보';
  document.getElementById('pod-ctl-power-hint').textContent = power ? power.label : '';
  // 자동 끄기(NS-8) — RunPod 전원을 다룰 수 있을 때만(관리자 · RunPod 워커)
  const w = podEditId ? (podsById[podEditId] || {}) : {};
  document.getElementById('pod-ctl-autooff-row').style.display = p && power ? '' : 'none';
  document.getElementById('pod-ctl-autooff').setAttribute('aria-checked', w.auto_power_off ? 'true' : 'false');
  const minEl = document.getElementById('pod-ctl-autooff-min');
  if(document.activeElement !== minEl) minEl.value = w.auto_off_minutes || 15;
  document.getElementById('pod-ctl-autooff-hint').textContent = (w.auto_power_off
    ? `켜짐 — 실행·대기 작업이 모두 끝나고 ${w.auto_off_minutes || 15}분 동안 새 작업이 없으면 RunPod 파드를 꺼요${w.runpod_tier ? '(이 워커는 파드를 지워요)' : ''}.`
    : '꺼짐 — 직접 끌 때까지 켜 둬요.') + (w.auto_off_at ? ` 마지막 자동 끄기: ${fmtGalleryDateTime(w.auto_off_at)}` : '');
  const pull = podEditId ? !!(podsById[podEditId] || {}).pull_outputs : podEditNewPull;
  document.getElementById('pod-ctl-pull').setAttribute('aria-checked', pull ? 'true' : 'false');
  document.getElementById('pod-ctl-pull-hint').textContent = pull
    ? '켜짐 — 작업이 끝나면 결과를 이 서버 갤러리로 받아 와요.' : '꺼짐 — 결과는 파드에만 남아요.';
  const auto = podEditId ? !!(podsById[podEditId] || {}).auto_install_models : podEditNewAuto;
  document.getElementById('pod-ctl-auto').setAttribute('aria-checked', auto ? 'true' : 'false');
  document.getElementById('pod-ctl-auto-hint').textContent = auto
    ? '켜짐 — 대기 작업에 필요한 모델이 없으면 알아서 받아요(모델 탭에 받을 주소가 있는 것만).'
    : '꺼짐 — 없는 모델은 작업 카드나 Models 탭에서 직접 받아요.';
  if(!p) return;
  const setSwitch = (id, on, disabled) => {
    const el = document.getElementById(id);
    el.setAttribute('aria-checked', on ? 'true' : 'false');
    el.disabled = !!disabled;
  };
  setSwitch('pod-ctl-enabled', p.enabled, false);
  document.getElementById('pod-ctl-enabled-hint').textContent = p.enabled
    ? '켜짐 — 새 작업을 이 워커로 보내요.' : '꺼짐 — 새 작업을 보내지 않아요. 설정·기록은 남아요.';
  setSwitch('pod-ctl-queue', p.enabled && p.auto_run, !p.enabled);
  document.getElementById('pod-ctl-queue-hint').textContent = !p.enabled ? '워커 사용을 켜야 처리할 수 있어요.'
    : p.auto_run ? '켜짐 — 대기 중인 작업을 차례로 처리해요.' : '멈춤 — 대기 작업을 시작하지 않아요.';
  if(power) setSwitch('pod-ctl-power', power.on, power.busy);
}

// 워커 레코드의 켜고 끄는 값 하나(결과 가져오기·모델 자동 설치)를 스위치에 묶는다 — 기존 워커는 누르면 바로 저장,
// 새 워커는 화면 값만 바꿔 두고 "저장" 때 함께 보낸다(podEditPayload).
function bindPodFieldSwitch(switchId, field, toggleNew){
  document.getElementById(switchId).addEventListener('click', async (e) => {
    if(!podEditId){ toggleNew(); renderPodControls(); return; }
    const pod = podsById[podEditId];
    if(!pod) return;
    const btn = e.currentTarget;
    btn.disabled = true;
    const res = await fetch(`/api/pods/${encodeURIComponent(pod.id)}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ [field]: !pod[field] }),
    });
    if(!res.ok) alert((await res.json().catch(() => ({}))).detail || '바꾸지 못했어요.');
    await fetchPods();
    btn.disabled = false;
    renderPodControls();
  });
}
bindPodFieldSwitch('pod-ctl-pull', 'pull_outputs', () => { podEditNewPull = !podEditNewPull; });
bindPodFieldSwitch('pod-ctl-auto', 'auto_install_models', () => { podEditNewAuto = !podEditNewAuto; });
bindPodFieldSwitch('pod-ctl-autooff', 'auto_power_off', () => {});   // 새 워커 추가 중엔 안 보인다
document.getElementById('pod-ctl-autooff-min').addEventListener('change', async (e) => {
  const pod = podsById[podEditId];
  if(!pod) return;
  const res = await fetch(`/api/pods/${encodeURIComponent(pod.id)}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ auto_off_minutes: e.currentTarget.value }),
  });
  if(!res.ok) alert((await res.json().catch(() => ({}))).detail || '바꾸지 못했어요.');
  await fetchPods();
  renderPodControls();
});
document.getElementById('pod-ctl-enabled').addEventListener('click', async (e) => {
  const p = (dashSummary.pods || []).find(x => x.id === podEditId);
  if(!p) return;
  e.currentTarget.disabled = true;
  const res = await fetch(`/api/pods/${encodeURIComponent(p.id)}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: !p.enabled }),
  });
  if(!res.ok) alert((await res.json().catch(() => ({}))).detail || '바꾸지 못했어요.');
  await fetchPods();
  await fetchDashboard();
});
document.getElementById('pod-ctl-queue').addEventListener('click', async (e) => {
  const p = (dashSummary.pods || []).find(x => x.id === podEditId);
  if(!p) return;
  const running = (p.running_jobs || []).length;
  if(p.auto_run && running && !confirm(`실행 중인 작업 ${running}개도 중단돼요. 작업 처리를 멈출까요?`)) return;
  e.currentTarget.disabled = true;
  const res = await fetch(`/api/pods/${encodeURIComponent(p.id)}/queue/${p.auto_run ? 'stop' : 'start'}`, { method: 'POST' });
  if(!res.ok) alert((await res.json().catch(() => ({}))).detail || '바꾸지 못했어요.');
  await fetchDashboard();
});
document.getElementById('pod-ctl-power').addEventListener('click', () => {
  const p = (dashSummary.pods || []).find(x => x.id === podEditId);
  const power = p && runpodPowerState(p);
  if(power && !power.busy) runpodPower(p.id, power.on ? 'stop' : 'start');
});

// 파드 바로가기 — 드라이버의 card()가 만들어 준 card.links(ComfyUI 화면, RunPod면 JupyterLab·
// 파일 브라우저)를 새 탭 링크로 그린다. http(s)가 아닌 주소는 그리지 않는다.
function renderPodLinks(links){
  const safe = (links || []).filter(l => l && /^https?:\/\//i.test(l.url || ''));
  if(!safe.length) return '';
  return safe.map(l => `<a class="pod-link" href="${escapeHtml(l.url)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(l.label)} 새 탭으로 열기"><svg class="ico"><use href="#i-external-link"/></svg>${escapeHtml(l.label)}</a>`).join('');
}

// 카드의 "사양" 줄은 워커 종류마다 다르다 — ComfyUI는 주소와 VRAM, 셸 파드는 어디서
// 무슨 폴더에서 도는지와 디스크 여유. 드라이버의 card()가 준 값을 여기서 읽는다.
const POD_SPEC_LINE = {
  comfyui(p){
    const url = (p.status && p.status.url) || p.effective_url || '';
    const vram = fmtVram((p.card || {}).vram_free, (p.card || {}).vram_total);
    return `${escapeHtml(url || '(주소 없음 — 자동 탐지)')}${vram ? ` · ${vram}` : ''}`;
  },
  shell(p){
    const card = p.card || {};
    const target = card.target || (p.url || '이 머신 (로컬)');
    const disk = card.disk_total ? `여유 ${fmtDiskSize(card.disk_free)}/${fmtDiskSize(card.disk_total)}` : '';
    return `${escapeHtml(target)}${card.workdir ? ` · ${escapeHtml(card.workdir)}` : ''}${disk ? ` · ${disk}` : ''}`;
  },
  claude_writer(p){
    // 이 파드는 원격 주소가 없다 — 카드에 실을 것도 "지금 쓰는 모델" 정도뿐이다.
    const model = (p.card || {}).model;
    return `Claude API (Anthropic)${model ? ` · ${escapeHtml(model)}` : ''}`;
  },
};

// "오늘 만든 것"의 단위도 워커 종류마다 다르다 — 이미지 워커는 장수지만, 셸 워커에게
// 같은 숫자는 "명령을 몇 번 돌렸는가"고, 글쓰기 워커에게는 "몇 편 썼는가"다. 서버는
// 진행률(progress.done)의 합만 주므로 이름은 화면이 붙인다.
const POD_TODAY_UNIT = { comfyui: '장', shell: '회', claude_writer: '편' };

function podTodayText(p){
  return `${p.images_today}${POD_TODAY_UNIT[p.kind] || '건'}`;
}

function podSpecLine(p){
  const fn = POD_SPEC_LINE[p.kind] || POD_SPEC_LINE.comfyui;
  return fn(p);
}

// 부팅 상태(NS-18) — 파드는 켜졌는데 ComfyUI가 아직 응답하지 않으면 켠 지 몇 분인지, 부팅 감시가 멈춘 워커면 이유와
// "다시 만들기"(관리자만, RunPod 전원 켜기와 같다 — 같은 등급으로 새 파드).
function renderBootLine(p){
  const boot = p.boot;
  if(!boot) return '';
  if(boot.failed){
    return `<span class="dash-boot failed">부팅 실패 — ComfyUI가 끝내 뜨지 않아 파드를 지웠어요</span>
      ${isAdminUser() ? `<button type="button" class="load-btn" data-pod-reboot="${escapeHtml(p.id)}">다시 만들기</button>` : ''}`;
  }
  const min = Math.max(0, Math.floor((Date.now() - new Date(boot.since).getTime()) / 60000));
  return `<span class="dash-boot">켠 지 ${min}분 — ComfyUI가 아직 응답하지 않아요${boot.retry ? ' (다시 만든 파드)' : ''}</span>`;
}

function renderPodCard(p){
  const state = POD_STATE[podState(p)];
  const job = (p.running_jobs || [])[0];
  const prog = job && job.progress;
  const pct = prog && prog.total > 0 ? Math.min(100, Math.round((prog.done / prog.total) * 100)) : 0;
  const thumbs = (p.recent_images || []).map(name =>
    `<img class="dash-thumb" src="${window.__nightshiftMediaUrl(`/api/output-images/${encodeURI(name)}/thumbnail?size=120`)}" alt="" loading="lazy">`).join('');

  return `
    <div class="dash-card dash-card-clickable${p.enabled ? '' : ' disabled'}" data-card-type="pod" data-pod-id="${escapeHtml(p.id)}" data-pod-open="${escapeHtml(p.id)}" data-pod-state="${state.cls}">
      <div class="dash-card-top">
        <div class="dash-robot" title="${state.label}">${renderPodRobot(p)}</div>
        <div class="dash-card-top-info">
          <div class="dash-card-head">
            <span class="dash-dot ${state.cls}" title="${state.label}"></span>
            <span class="dash-card-name" title="${escapeHtml(p.name)}">${escapeHtml(p.name)}</span>
            <span class="dash-card-kind">${escapeHtml((p.card && p.card.runpod) ? 'RunPod 이미지 생성 모델' : (p.kind_label || p.kind))}</span>
            ${p.owner_name ? `<span class="dash-card-kind owner-badge" title="이 워커의 주인">${ico('user')} ${escapeHtml(p.owner_name)}</span>` : ''}
            <div class="dash-card-head-right">
              <button class="dash-gear-btn" type="button" data-pod-edit="${escapeHtml(p.id)}" title="워커 설정" aria-label="워커 설정"><svg class="ico"><use href="#i-settings"/></svg></button>
            </div>
          </div>
          <div class="dash-card-url">${podSpecLine(p)}</div>
          ${renderRunpodLine(p.card && p.card.runpod)}
          ${(p.card && p.card.links && p.card.links.length) ? `<div class="pod-links">${renderPodLinks(p.card.links)}</div>` : ''}
        </div>
      </div>
      <div class="dash-card-job">
        ${job
          ? `<b>${escapeHtml(job.template_label || '작업')}</b>${prog ? `
               <div class="progress-wrap">
                 <div class="progress-track"><div class="progress-fill running" style="width:${pct}%"></div></div>
                 <span class="progress-text">${prog.done}/${prog.total}</span>
               </div>` : ''}`
          : p.boot ? renderBootLine(p)
          : `<span class="dim">${state.label}${p.waiting_for_pod ? ` · 연결 대기 ${p.waiting_for_pod}건` : ''}</span>`}
      </div>
      <div class="dash-card-stats">
        <div class="dash-stat"><span class="dash-stat-label">큐</span><span class="dash-stat-value">${p.queue_len}</span></div>
        <div class="dash-stat"><span class="dash-stat-label">대기</span><span class="dash-stat-value">${p.pending_count}</span></div>
        <div class="dash-stat"><span class="dash-stat-label">오늘</span><span class="dash-stat-value">${escapeHtml(podTodayText(p))}</span></div>
        <div class="dash-stat"><span class="dash-stat-label">동시</span><span class="dash-stat-value">${p.max_concurrent}</span></div>
      </div>
      ${thumbs ? `<div class="dash-thumbs">${thumbs}</div>` : ''}
      <div class="dash-card-actions">
        ${renderPodChips(p)}
      </div>
    </div>`;
}

// 작업 목록이 파드 안으로 들어가면서 "모든 파드의 작업을 한 화면에서" 보던 자리가
// 비었다. 이 카드가 그 자리를 대신한다 — 파드를 가리지 않고 최근 작업만 훑어보고,
// 행을 누르면 그 작업이 있는 파드의 작업 화면으로 들어간다.
const RECENT_JOB_LIMIT = 8;

function renderRecentJobsCard(){
  const rows = lastJobs.slice(0, RECENT_JOB_LIMIT).map(j => {
    const label = j.template_label || j.template_id;
    return `
      <div class="dash-job-row" data-job-pod="${escapeHtml(j.pod_id || '')}" title="${escapeHtml(label)}">
        <span class="badge ${j.status}"><span class="bdot"></span>${STATUS_LABEL[j.status] || j.status}</span>
        <span class="dash-job-name">${escapeHtml(label)}</span>
        <span class="dash-job-pod">${escapeHtml(podName(j.pod_id) || '')}</span>
      </div>`;
  }).join('');
  return `
    <div class="dash-card" data-card-type="recent-jobs" data-span="2">
      <div class="dash-card-head">
        <span class="dash-card-name">최근 작업</span>
        <span class="dash-card-kind">모든 워커</span>
      </div>
      ${rows ? `<div class="dash-jobs">${rows}</div>`
             : '<div class="dash-card-job"><span class="dim">아직 작업이 없어요</span></div>'}
    </div>`;
}

const CARD_RENDERERS = { pod: renderPodCard, 'recent-jobs': renderRecentJobsCard };

// 로컬 워커(이 서버 자신을 가리키는 워커) — 평소엔 숨긴다(NS-7). 고른 적 없으면 켜져 있을 때만 보인다.
const SHOW_LOCAL_KEY = 'nightshift-show-local-worker';
function isLocalWorker(p){
  if(!p || (p.tags || []).includes('runpod')) return false;
  const url = (p.url || '').trim();
  if(!url) return true;
  try{ return ['127.0.0.1', 'localhost', '[::1]'].includes(new URL(url).hostname); }catch(e){ return false; }
}
function showLocalWorkers(){
  const saved = localStorage.getItem(SHOW_LOCAL_KEY);
  if(saved !== null) return saved === '1';
  return (dashSummary.pods || []).some(p => isLocalWorker(podsById[p.id] || p) && (podsById[p.id] || p).enabled);
}
document.getElementById('dash-show-local').addEventListener('click', () => {
  localStorage.setItem(SHOW_LOCAL_KEY, showLocalWorkers() ? '0' : '1');
  renderDashboard();
});

function renderDashboard(){
  if(podLongPress.active) return;
  const totals = dashSummary.totals || {};
  document.getElementById('dash-totals').innerHTML = [
    ['워커', `${totals.online ?? 0}/${totals.pods ?? 0}`, '연결된 워커 / 전체'],
    ['실행 중', `${totals.running_jobs ?? 0}건`, ''],
    ['큐', `${totals.queued ?? 0}건`, ''],
    ['대기', `${totals.pending ?? 0}건`, ''],
    // 종류가 다른 워커를 합친 숫자라 "장"이라고 못 쓴다 — 이미지 워커의 장수와
    // 셸 워커의 실행 횟수가 같이 들어오므로 중립적인 "건"으로 센다.
    ['오늘 처리', `${totals.images_today ?? 0}건`, '워커 종류가 섞여 있으면 이미지 장수와 실행 횟수를 합친 값이에요'],
  ].map(([label, value, title]) => `
    <div class="dash-total"${title ? ` title="${escapeHtml(title)}"` : ''}>
      <span class="dash-total-label">${label}</span>
      <span class="dash-total-value">${escapeHtml(String(value))}</span>
    </div>`).join('');

  // 카드 목록 — 파드마다 카드 하나에, 파드를 가로지르는 카드(최근 작업)를 덧붙인다.
  // 나중에 통계나 산출물 카드도 같은 자리에 섞으면 된다.
  const allPods = dashSummary.pods || [];
  const hasLocal = allPods.some(p => isLocalWorker(podsById[p.id] || p));
  const showLocal = showLocalWorkers();
  document.getElementById('dash-local-toggle').hidden = !hasLocal;
  document.getElementById('dash-show-local').setAttribute('aria-checked', String(showLocal));
  const cards = allPods.filter(p => showLocal || !isLocalWorker(podsById[p.id] || p)).map(p => ({ type: 'pod', data: p }));
  cards.push({ type: 'recent-jobs' });
  const canvas = document.getElementById('dash-canvas');
  document.getElementById('dash-empty').style.display = cards.length ? 'none' : '';
  canvas.innerHTML = cards.map(c => (CARD_RENDERERS[c.type] || (() => ''))(c.data)).join('');

  canvas.querySelectorAll('[data-pod-edit]').forEach(btn => btn.addEventListener('click', () => {
    showTab('psettings', { podId: btn.dataset.podEdit });
  }));
  canvas.querySelectorAll('[data-pod-reboot]').forEach(btn => btn.addEventListener('click', () => runpodPower(btn.dataset.podReboot, 'start')));
  canvas.querySelectorAll('[data-pod-open]').forEach(card => card.addEventListener('click', (e) => {
    if(e.target.closest('button, select, a')) return; // ⚙ 설정, ▶ 시작/⏸ 정지는 자기 할 일만 한다
    // 워커 카드를 누르면 그 워커의 설정부터(NS-9) — 설정 서브탭이 없는 종류는 첫 서브탭.
    const pod = podsById[card.dataset.podOpen];
    showTab(podSubtabs(pod).includes('psettings') ? 'psettings' : podDefaultTab(pod), { podId: card.dataset.podOpen });
  }));
  canvas.querySelectorAll('[data-job-pod]').forEach(row => row.addEventListener('click', () => {
    showTab('jobs', { podId: row.dataset.jobPod });
  }));
  if(document.getElementById('pod-edit-modal').style.display !== 'none') renderPodControls();   // 설정 창의 켜기/끄기도 새 상태로
}

// ---- 파드 카드를 길게 눌러 삭제 ----
// 카드는 4초마다 다시 그려지므로 리스너는 카드가 아니라 바뀌지 않는 캔버스에 위임해서 붙인다. 손가락을 0.6초 넘게
// 대고 있으면(움직이거나 스크롤하면 취소) 짧게 진동하고 삭제 확인창을 띄운다. 손을 뗄 때 이어지는 클릭(파드 열기)은 막는다.
const POD_LONG_PRESS_MS = 600;
const POD_LONG_PRESS_VISUAL_MS = 220;
const POD_LONG_PRESS_MOVE_PX = 10;
const podLongPress = { active: false, timer: null, visualTimer: null, card: null, x: 0, y: 0, firedAt: 0, touch: false };

function cancelPodLongPress(){
  clearTimeout(podLongPress.timer);
  clearTimeout(podLongPress.visualTimer);
  if(podLongPress.card) podLongPress.card.classList.remove('pod-pressing');
  podLongPress.active = false;
  podLongPress.card = null;
}

// 워커 지우기(카드 길게 누르기·설정 창 공용). RunPod 파드를 가리키는 워커면 파드도 같이 지우는 게 기본이다 —
// 남겨 두면 꺼져 있어도 디스크 요금이 나간다. 첫 확인에서 취소하면 "파드는 남기고 워커만" 지울지 한 번 더 묻는다.
async function deletePodWithConfirm(podId){
  const pod = podsById[podId] || {};
  const name = pod.name || podId;
  const isRunpod = /^https:\/\/[a-z0-9]+-\d+\.proxy\.runpod\.net/i.test(pod.url || '') && isAdminUser();
  let terminate = false;
  if(isRunpod){
    terminate = confirm(`'${name}' 워커와 RunPod 파드를 함께 지울까요?\n파드 안의 모델·파일이 모두 지워지고 요금도 멈춰요(되돌릴 수 없어요).\n\n취소하면 "워커만 지우기"를 물어봐요.`);
    if(!terminate && !confirm(`RunPod 파드는 남기고 '${name}' 워커만 지울까요?\n남은 파드는 꺼져 있어도 디스크 요금이 나가요.`)) return;
  }else if(!confirm(`'${name}' 워커를 삭제할까요?\n이 워커에 배정된 대기 작업은 같은 계정의 다른 워커로 옮겨져요.`)){
    return;
  }
  try{
    const res = await fetch(`/api/pods/${encodeURIComponent(podId)}${terminate ? '?terminate_runpod=true' : ''}`, { method: 'DELETE' });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || '지우지 못했어요.');
    const wasInside = currentPodId === podId;
    await fetchPods();
    if(wasInside) showTab('pods'); else fetchDashboard();
  }catch(e){
    alert(e.message || '지우지 못했어요.');
  }
}

(() => {
  const canvas = document.getElementById('dash-canvas');
  canvas.addEventListener('pointerdown', (e) => {
    podLongPress.touch = e.pointerType === 'touch' || e.pointerType === 'pen';
    if(e.pointerType === 'mouse' && e.button !== 0) return;
    const card = e.target.closest('.dash-card[data-card-type="pod"]');
    if(!card || e.target.closest('button, select, a, input')) return;
    cancelPodLongPress();
    podLongPress.active = true;
    podLongPress.card = card;
    podLongPress.x = e.clientX;
    podLongPress.y = e.clientY;
    const podId = card.dataset.podId;
    podLongPress.visualTimer = setTimeout(() => card.classList.add('pod-pressing'), POD_LONG_PRESS_VISUAL_MS);
    podLongPress.timer = setTimeout(() => {
      podLongPress.firedAt = Date.now();
      cancelPodLongPress();
      if(navigator.vibrate) navigator.vibrate(40);
      deletePodWithConfirm(podId);
    }, POD_LONG_PRESS_MS);
  });
  canvas.addEventListener('pointermove', (e) => {
    if(!podLongPress.active) return;
    if(Math.hypot(e.clientX - podLongPress.x, e.clientY - podLongPress.y) > POD_LONG_PRESS_MOVE_PX) cancelPodLongPress();
  });
  for(const type of ['pointerup', 'pointercancel', 'pointerleave']) canvas.addEventListener(type, cancelPodLongPress);
  // 롱프레스가 끝난 직후의 클릭(카드 열기)은 삼킨다 — 캔버스가 먼저 받는 캡처 단계에서 막는다.
  canvas.addEventListener('click', (e) => {
    if(Date.now() - podLongPress.firedAt < 900){ e.stopPropagation(); e.preventDefault(); }
  }, true);
  // 터치로 길게 누르면 브라우저가 컨텍스트 메뉴/텍스트 선택을 띄우려 한다 — 카드 위에서는 막는다.
  canvas.addEventListener('contextmenu', (e) => {
    if(podLongPress.touch && e.target.closest('.dash-card[data-card-type="pod"]')) e.preventDefault();
  });
})();

async function fetchDashboard(){
  try{
    const res = await fetch('/api/pods/summary');
    if(!res.ok) return;
    dashSummary = await res.json();
  }catch(e){
    return;
  }
  renderDashboard();
  updatePodBarStatus();
  if(currentTab === 'dashboard') fetchProjects();   // 홈의 프로젝트 카드(실행 중 작업 수 등)도 같이 새로고침
}

document.getElementById('dash-refresh-btn').addEventListener('click', fetchDashboard);

// ---- 파드 추가/편집 ----
let podEditId = null;   // null이면 "추가"
let podEditNewPull = true;   // 새 워커를 추가할 때의 "결과 가져오기"(저장 때 함께 보낸다)
let podEditNewAuto = false;  // 새 워커를 추가할 때의 "모델 자동 설치"(저장 때 함께 보낸다) — 받는 시간도 GPU 요금이라 기본은 끔

// RunPod 정보(card.runpod)는 대시보드가 4초마다 폴링해둔 dashSummary.pods에 이미
// 있다 — 설정 모달은 늘 대시보드 카드에서 열리므로, 추가 요청 없이 이걸 그대로
// 재사용한다("RunPod 정보 테스트" 버튼을 눌러야만 새로 조회한다).
function runpodInfoFromDashboard(podId){
  const row = (dashSummary.pods || []).find(r => r.id === podId);
  return (row && row.card && row.card.runpod) || null;
}

// RunPod 정보(이름·GPU·비용·일시) — "RunPod 전원" 줄 안에 함께 보인다. 줄을 보일지는 renderPodControls가 정한다.
// 디스크 종류(NS-9) — 받은 모델이 파드를 끄거나 지운 뒤에도 남는지가 요점이라 그 말까지 붙인다.
function runpodDiskText(info){
  if(info.network_volume_id) return `네트워크 볼륨 연결됨 — 파드를 지워도 모델이 남아요${info.container_disk_gb ? ` · 컨테이너 ${info.container_disk_gb}GB` : ''}`;
  if(info.volume_gb) return `볼륨 ${info.volume_gb}GB(끄면 남음) · 컨테이너 ${info.container_disk_gb || 0}GB(끄면 지워짐)`;
  if(info.container_disk_gb) return `컨테이너 디스크 ${info.container_disk_gb}GB만 — 끄거나 지우면 받은 모델이 지워져요`;
  return '';
}

function renderRunpodInfoBox(info){
  const grid = document.getElementById('pod-edit-runpod-fields');
  const rows = [];
  if(info){
    if(info.pod_name) rows.push(['RunPod 이름', info.pod_name]);
    if(info.gpu_type) rows.push(['GPU', info.gpu_type]);
    if(info.cost_per_hr != null) rows.push(['비용', `$${Number(info.cost_per_hr).toFixed(2)}/hr`]);
    if(info.created_at) rows.push(['생성', fmtGalleryDateTime(info.created_at)]);
    if(info.last_started_at) rows.push(['최근 시작', fmtGalleryDateTime(info.last_started_at)]);
    const disk = runpodDiskText(info);
    if(disk) rows.push(['디스크', disk]);
  }
  grid.innerHTML = rows.map(([k, v]) =>
    `<span class="ri-k">${escapeHtml(k)}</span><span class="ri-v">${escapeHtml(String(v))}</span>`).join('');
  grid.style.display = rows.length ? '' : 'none';
  renderPodControls();
}

// RunPod가 자동으로 붙인 이름을 nightshift 이름 입력칸에 채워주는 제안 — 사용자가
// 일부러 지은 이름을 조용히 덮어쓰면 안 되므로 버튼을 눌러야만 채워진다.
function updatePodNameSuggest(runpodName){
  const el = document.getElementById('pod-edit-name-suggest');
  const nameInput = document.getElementById('pod-edit-name');
  if(!runpodName || runpodName === nameInput.value.trim()){
    el.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  el.style.display = '';
  el.innerHTML = `RunPod 이름: <b>${escapeHtml(runpodName)}</b>`;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.textContent = '이 이름 쓰기';
  btn.addEventListener('click', () => {
    nameInput.value = runpodName;
    el.style.display = 'none';
  });
  el.appendChild(btn);
}

// 설정 서브탭(NS-9) — 같은 설정 창을 모달 대신 #tab-psettings 안으로 옮겨 그린다(.inline). 워커 추가는 여전히 모달.
function podEditInline(){ return document.getElementById('pod-edit-modal').classList.contains('inline'); }
async function openPodSettingsPage(podId){
  const modal = document.getElementById('pod-edit-modal');
  if(!podId) return;
  if(!podsById[podId]) await fetchPods();   // 새로고침·바로가기로 들어오면 워커 목록이 아직 없다
  if(!podsById[podId] || currentTab !== 'psettings' || currentPodId !== podId) return;
  document.getElementById('pod-settings-host').appendChild(modal);
  modal.classList.add('inline');
  openPodEditModal(podId);
}
function closePodSettingsPage(){
  const modal = document.getElementById('pod-edit-modal');
  if(!podEditInline()) return;
  modal.classList.remove('inline');
  document.body.appendChild(modal);
  modal.style.display = 'none';
  podEditId = null;
}

function openPodEditModal(podId){
  if(!podId && podEditInline()) closePodSettingsPage();   // 워커 추가는 모달로
  podEditId = podId || null;
  const pod = podId ? (podsById[podId] || {}) : {};
  document.getElementById('pod-edit-title').innerHTML = podId ? ico('settings') + ' Worker settings' : ico('plus') + ' Add worker';
  document.getElementById('pod-edit-name').value = pod.name || '';
  document.getElementById('pod-edit-url').value = pod.url || '';
  document.getElementById('pod-edit-concurrent').value = pod.max_concurrent || 1;
  podEditNewPull = true;   // 새 워커는 기본으로 결과를 가져온다
  podEditNewAuto = false;
  syncNumStepper();
  document.getElementById('pod-edit-status').textContent = '';
  document.getElementById('pod-edit-error').textContent = '';
  document.getElementById('pod-edit-delete').style.display = podId ? '' : 'none';
  const runpodInfo = podId ? runpodInfoFromDashboard(podId) : null;
  renderRunpodInfoBox(runpodInfo);
  updatePodNameSuggest(runpodInfo && runpodInfo.pod_name);
  const modal = document.getElementById('pod-edit-modal');
  modal.classList.toggle('create-mode', !podId);
  modal.classList.remove('manual-open');
  modal.style.display = 'flex';
  renderPodControls();
  if(!podId) renderPodCreateTiers();
}

// ---- 워커 추가 = RunPod 파드 만들기(#12) ----
// 등급(이미지용·영상용) 카드를 누르면 서버가 RunPod에 파드를 만들고 워커에 바로 연결한다(POST /api/pods/runpod-create).
// 관리자가 아니거나 RunPod 키가 없으면 만들 수 없으니 곧장 주소 입력(고급)을 펼친다.
async function renderPodCreateTiers(){
  const box = document.getElementById('pod-create-tiers');
  const statusEl = document.getElementById('pod-create-status');
  statusEl.textContent = '';
  statusEl.classList.remove('error');
  box.innerHTML = '<div class="comfy-model-empty">불러오는 중…</div>';
  let data = null;
  if(isAdminUser()){
    try{ const res = await fetch('/api/runpod/tiers'); if(res.ok) data = await res.json(); }catch(e){ /* 아래에서 */ }
  }
  if(!data || !data.api_key){
    box.innerHTML = `<div class="comfy-model-empty">${isAdminUser() ? 'RUNPOD_API_KEY가 없어 파드를 만들 수 없어요.' : 'RunPod 파드 만들기는 관리자만 할 수 있어요.'} 아래 주소로 연결해 주세요.</div>`;
    document.getElementById('pod-edit-modal').classList.add('manual-open');
    return;
  }
  box.innerHTML = data.tiers.map(t => `
    <button type="button" class="pod-create-tier" data-tier="${escapeHtml(t.id)}" data-label="${escapeHtml(t.label)}" data-price="${escapeHtml(t.price_hint)}"
            title="GPU 후보(이 순서로 찾아요): ${escapeHtml(t.gpus.map(g => g.replace(/^NVIDIA (GeForce )?/, '')).join(' → '))}">
      <svg class="ico"><use href="#i-${t.id === 'video' ? 'clapperboard' : 'images'}"/></svg>
      <b>${escapeHtml(t.label)}</b><span>${escapeHtml(t.vram)} · ${escapeHtml(t.price_hint)}</span>
    </button>`).join('');
}

document.getElementById('pod-create-tiers').addEventListener('click', async (e) => {
  const card = e.target.closest('.pod-create-tier');
  if(!card || card.disabled) return;
  if(!confirm(`${card.dataset.label} RunPod 파드를 만들까요?\n만드는 순간부터 요금이 나가요(${card.dataset.price}).\nComfyUI가 뜨기까지 1~3분 걸려요.`)) return;
  const statusEl = document.getElementById('pod-create-status');
  const cards = document.querySelectorAll('#pod-create-tiers .pod-create-tier');
  cards.forEach(c => { c.disabled = true; });
  statusEl.classList.remove('error');
  statusEl.innerHTML = `${ico('loader-circle', true)} RunPod에 파드를 만드는 중… (재고를 찾느라 1분쯤 걸릴 수 있어요)`;
  try{
    const res = await fetch('/api/pods/runpod-create', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tier: card.dataset.tier }) });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || '파드를 만들지 못했어요.');
    closePodEditModal();
    await fetchPods();
    fetchDashboard();
    const rp = data.runpod || {};
    flashNotice(`워커 '${data.name}'을(를) 만들었어요 — ${[rp.gpu, rp.data_center, rp.cost_per_hr != null ? `$${rp.cost_per_hr}/hr` : ''].filter(Boolean).join(' · ')}. ComfyUI가 뜨기까지 1~3분 걸려요.`);
  }catch(err){
    statusEl.classList.add('error');
    statusEl.textContent = err.message;
    cards.forEach(c => { c.disabled = false; });
  }
});
document.getElementById('pod-manual-toggle').addEventListener('click', () => {
  document.getElementById('pod-edit-modal').classList.toggle('manual-open');
});

function closePodEditModal(){
  if(podEditInline()) return;   // 서브탭에서는 닫지 않는다(다른 탭으로 가면 closePodSettingsPage)
  document.getElementById('pod-edit-modal').style.display = 'none';
  podEditId = null;
}

// 종류(kind)는 화면에서 고르지 않는다 — 새 워커는 서버 기본(ComfyUI), 기존 워커는 그대로(부분 수정이라 안 보냄).
// 스위치는 누를 때 바로 저장되므로 기존 워커의 결과 가져오기도 여기서는 안 보낸다.
function podEditPayload(){
  return {
    name: document.getElementById('pod-edit-name').value.trim(),
    url: document.getElementById('pod-edit-url').value.trim(),
    max_concurrent: Number(document.getElementById('pod-edit-concurrent').value || 1),
    ...(podEditId ? {} : { pull_outputs: podEditNewPull, auto_install_models: podEditNewAuto }),
  };
}

// 동시 실행 수 — 숫자를 치는 대신 −/+ 로 1~8 사이를 고른다.
document.querySelectorAll('#pod-edit-modal .num-stepper-btn').forEach(btn => btn.addEventListener('click', () => {
  const input = document.getElementById('pod-edit-concurrent');
  const next = Number(input.value || 1) + Number(btn.dataset.step);
  input.value = Math.min(Number(input.max), Math.max(Number(input.min), next));
  syncNumStepper();
}));
function syncNumStepper(){
  const input = document.getElementById('pod-edit-concurrent');
  const [minus, plus] = document.querySelectorAll('#pod-edit-modal .num-stepper-btn');
  minus.disabled = Number(input.value) <= Number(input.min);
  plus.disabled = Number(input.value) >= Number(input.max);
}

document.getElementById('pod-add-btn').addEventListener('click', () => openPodEditModal(null));
document.getElementById('pod-edit-close').addEventListener('click', closePodEditModal);
document.getElementById('pod-edit-cancel').addEventListener('click', closePodEditModal);
document.getElementById('pod-edit-modal').addEventListener('click', (e) => {
  if(e.target === document.getElementById('pod-edit-modal')) closePodEditModal();
});

document.getElementById('pod-edit-save').addEventListener('click', async () => {
  const errorEl = document.getElementById('pod-edit-error');
  errorEl.textContent = '';
  try{
    const url = podEditId ? `/api/pods/${podEditId}` : '/api/pods';
    const res = await fetch(url, {
      method: podEditId ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(podEditPayload()),
    });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || '저장하지 못했어요.');
    const inline = podEditInline();
    closePodEditModal();
    await fetchPods();
    fetchDashboard();
    fetchComfyStatus();
    if(inline){ flashNotice('저장했어요'); renderPodBar && renderPodBar(); }
  }catch(e){
    errorEl.textContent = e.message || '저장하지 못했어요.';
  }
});

document.getElementById('pod-edit-test').addEventListener('click', async () => {
  const statusEl = document.getElementById('pod-edit-status');
  const errorEl = document.getElementById('pod-edit-error');
  errorEl.textContent = '';
  if(!podEditId){
    statusEl.textContent = '';
    errorEl.textContent = '먼저 저장한 뒤에 연결을 확인할 수 있어요.';
    return;
  }
  statusEl.textContent = '확인 중…';
  try{
    const res = await fetch(`/api/pods/${podEditId}/test`, { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || '확인하지 못했어요.');
    statusEl.innerHTML = data.ok
      ? ico('circle-check') + ' ' + escapeHtml(`응답했어요 — ${data.url}`)
      : ico('triangle-alert') + ' ' + escapeHtml(`${data.detail || '응답이 없어요.'}${data.url ? ` (${data.url})` : ''}`);
  }catch(e){
    statusEl.textContent = '';
    errorEl.textContent = e.message || '확인하지 못했어요.';
  }
});

// 대시보드 카드의 RunPod 정보(card.runpod)는 실패를 조용히 삼키고 비워두므로, "왜 안
// 뜨는지"를 직접 확인하려면 캐시를 거치지 않는 이 진단 엔드포인트가 필요하다. 원본
// 응답(raw_response)은 콘솔에 남겨서, 정규화된 필드가 이상해도 원인을 눈으로 볼 수
// 있게 한다.
document.getElementById('pod-edit-runpod-test').addEventListener('click', async () => {
  const statusEl = document.getElementById('pod-edit-status');
  const errorEl = document.getElementById('pod-edit-error');
  errorEl.textContent = '';
  if(!podEditId){
    statusEl.textContent = '';
    errorEl.textContent = '먼저 저장한 뒤에 확인할 수 있어요.';
    return;
  }
  statusEl.textContent = 'RunPod API 확인 중…';
  try{
    const res = await fetch(`/api/pods/${podEditId}/runpod-test`, { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || '확인하지 못했어요.');
    console.log('[runpod-test]', data);
    if(data.error){
      statusEl.innerHTML = ico('triangle-alert') + ' ' + escapeHtml(`${data.error}${data.status_code ? ` (HTTP ${data.status_code})` : ''} — 자세한 응답은 브라우저 콘솔(F12)에 남겨뒀어요.`);
    }else{
      const n = data.normalized || {};
      const parts = [
        n.pod_name, n.gpu_type,
        n.cost_per_hr != null ? `$${Number(n.cost_per_hr).toFixed(2)}/hr` : null,
      ].filter(Boolean);
      statusEl.innerHTML = parts.length
        ? ico('circle-check') + ' ' + escapeHtml(`${parts.join(' · ')} — 원본 응답은 콘솔(F12)에 남겨뒀어요.`)
        : ico('triangle-alert') + ' 응답은 받았지만 알아본 필드가 없어요 — 콘솔(F12)의 원본 응답을 확인해주세요.';
      renderRunpodInfoBox(n);
      updatePodNameSuggest(n.pod_name);
    }
  }catch(e){
    statusEl.textContent = '';
    errorEl.textContent = e.message || '확인하지 못했어요.';
  }
});

document.getElementById('pod-edit-delete').addEventListener('click', async () => {
  if(!podEditId) return;
  const id = podEditId;
  closePodEditModal();
  deletePodWithConfirm(id);   // 카드 길게 누르기와 같은 확인(RunPod 파드도 지울지 포함)
});

// ---- 내 RunPod 대시보드(워커 탭 아래, 관리자만, NS-31) ----
// /api/runpod/dashboard가 파드·볼륨·잔액을 따로 돌려준다. 실패한 항목은 "조회하지 못했어요"만 띄우고 직전에 받은 값은
// 그대로 둔다(빈 목록·0달러로 바꾸지 않는다). "최근 불러온 일시"는 셋 다 성공했을 때만 바뀐다. 값은 이 페이지에만 둔다.
// 볼륨은 쓰는 파드가 없는 것만 지울 수 있다 — 되돌릴 수 없으니 볼륨 이름을 그대로 입력해야 지운다(서버도 다시 본다).
const rpDash = { pods: null, volumes: null, balance: null, fetchedAt: null, busy: false };

function renderRunpodDashboard(data){
  const err = (id, part) => { document.getElementById(id).textContent = part && part.error ? `조회하지 못했어요 — ${part.error}` : ''; };
  err('rp-pods-error', data && data.pods);
  err('rp-volumes-error', data && data.volumes);
  const balErr = data && data.balance && data.balance.error;
  const balEl = document.getElementById('rp-dash-balance');
  balEl.textContent = rpDash.balance != null ? `$${rpDash.balance.toFixed(2)}` : (balErr ? '조회하지 못했어요' : '—');
  balEl.title = balErr || '';
  const partial = data && !data.fetched_at && !data.error;
  document.getElementById('rp-dash-time').textContent =
    (rpDash.fetchedAt ? new Date(rpDash.fetchedAt).toLocaleString() : '아직 불러오지 않았어요') + (partial ? ' (이번엔 일부만 새로 불러왔어요)' : '');

  const podsEl = document.getElementById('rp-pods-list');
  const pods = rpDash.pods;
  document.getElementById('rp-pods-count').textContent = pods && pods.length
    ? `${pods.length}개 · 시간당 $${pods.reduce((s, p) => s + (p.cost_per_hr || 0), 0).toFixed(2)}` : '';
  podsEl.innerHTML = pods == null ? '' : pods.length ? pods.map(p => `<div class="wm-row">
      <span class="wm-name" title="${escapeHtml(p.id)}">${escapeHtml(p.name || p.id)}</span>
      <span class="wm-kind">${p.cost_per_hr != null ? `시간당 $${Number(p.cost_per_hr).toFixed(2)}` : ''}${p.image ? ` · ${escapeHtml(p.image)}` : ''}</span>
      <span class="wm-tag ok" title="RunPod에서 켜 둔 상태(시작 중 포함)">구동 중</span></div>`).join('')
    : '<div class="comfy-model-empty">구동 중인 Pod가 없어요.</div>';

  const listEl = document.getElementById('rp-volumes-list');
  const vols = rpDash.volumes;
  const total = (vols || []).reduce((s, v) => s + (v.monthly_usd || 0), 0);
  document.getElementById('rp-volumes-count').textContent = vols && vols.length ? `${vols.length}개 · 월 약 $${total.toFixed(2)}` : '';
  listEl.innerHTML = vols == null ? '' : vols.length ? vols.map(v => {
    const known = Array.isArray(v.pods);   // 파드 목록을 못 받았으면 null — "쓰는 파드 없음"으로 보지 않는다
    const inUse = !known || v.pods.length > 0;
    const used = !known ? `<span class="wm-tag" title="파드 목록을 받지 못해 확인할 수 없어요">쓰는 파드 확인 못 함</span>`
      : v.pods.length ? `<span class="wm-tag" title="이 볼륨을 붙여 쓰는 RunPod 파드">쓰는 파드: ${escapeHtml(v.pods.join(', '))}</span>`
      : `<span class="wm-tag warn" title="붙여 쓰는 파드가 없어요 — 요금만 나가고 있을 수 있어요">쓰는 파드 없음</span>`;
    return `<div class="wm-row"><span class="wm-name" title="${escapeHtml(v.id)}">${escapeHtml(v.name || v.id)}</span>
      <span class="wm-kind">${v.size_gb != null ? `${v.size_gb}GB` : '?'} · ${escapeHtml(v.data_center || '?')}${v.monthly_usd != null ? ` · 월 약 $${v.monthly_usd.toFixed(2)}` : ''}</span>
      ${used}
      <button type="button" class="del-btn rp-volume-del" data-vol-id="${escapeHtml(v.id)}" data-vol-name="${escapeHtml(v.name)}"${inUse ? ` disabled title="${known ? '쓰는 파드를 먼저 지워야 해요' : '쓰는 파드를 확인한 뒤 지울 수 있어요'}"` : ' title="볼륨 지우기(되돌릴 수 없어요)"'}>${ico('trash-2')} 지우기</button></div>`;
  }).join('') : '<div class="comfy-model-empty">네트워크 볼륨이 없어요.</div>';
}

async function fetchRunpodDashboard(){
  const box = document.getElementById('rp-dash');
  box.style.display = isAdminUser() ? '' : 'none';
  if(!isAdminUser() || rpDash.busy) return;   // 조회 중에 또 누르면 무시한다
  rpDash.busy = true;
  const btn = document.getElementById('rp-dash-refresh');
  const errorEl = document.getElementById('rp-dash-error');
  btn.disabled = true;
  btn.lastChild.textContent = ' 불러오는 중…';
  errorEl.textContent = '';
  let data;
  try{
    const res = await fetch('/api/runpod/dashboard');
    data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || '불러오지 못했어요.');
    if(!data.pods.error) rpDash.pods = data.pods.items;
    if(!data.volumes.error) rpDash.volumes = data.volumes.items;
    if(!data.balance.error) rpDash.balance = data.balance.usd;
    if(data.fetched_at) rpDash.fetchedAt = data.fetched_at;
  }catch(e){
    errorEl.textContent = `조회하지 못했어요 — ${e.message}`;
    data = { error: true };
  }finally{
    rpDash.busy = false;
    btn.disabled = false;
    btn.lastChild.textContent = ' 불러오기';
  }
  renderRunpodDashboard(data);
}

document.getElementById('rp-dash-refresh').addEventListener('click', fetchRunpodDashboard);
document.getElementById('rp-volumes-list').addEventListener('click', async (e) => {
  const btn = e.target.closest('.rp-volume-del');
  if(!btn || btn.disabled) return;
  const name = btn.dataset.volName;
  const typed = prompt(`네트워크 볼륨 '${name}'을(를) 지울까요?
안의 모델·파일이 모두 지워지고 되돌릴 수 없어요.

지우려면 볼륨 이름을 그대로 입력하세요:`);
  if(typed === null) return;
  if(typed !== name){ alert('이름이 달라서 지우지 않았어요.'); return; }
  btn.disabled = true;
  const errorEl = document.getElementById('rp-volumes-error');
  try{
    const res = await fetch(`/api/runpod/network-volumes/${encodeURIComponent(btn.dataset.volId)}`, {
      method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ confirm_name: typed }),
    });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || '지우지 못했어요.');
    flashNotice(`볼륨 '${name}'을(를) 지웠어요`);
  }catch(err){
    errorEl.textContent = err.message;   // 다시 불러오면 이 문구가 지워지므로 실패 때는 목록을 그대로 둔다
    btn.disabled = false;
    return;
  }
  fetchRunpodDashboard();
});
