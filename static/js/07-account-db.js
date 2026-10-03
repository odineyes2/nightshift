// ---- 밤 모드 / 낮 모드 ----
const THEME_STORAGE_KEY = 'nightshift-theme';
const themeToggleBtn = document.getElementById('theme-toggle-btn');
const prefersDarkQuery = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

function getSavedTheme(){
  try{ return localStorage.getItem(THEME_STORAGE_KEY); }catch(e){ return null; }
}

function getEffectiveTheme(){
  const saved = getSavedTheme();
  if(saved === 'light' || saved === 'dark') return saved;
  return (prefersDarkQuery && prefersDarkQuery.matches) ? 'dark' : 'light';
}

function updateThemeToggleLabel(theme){
  themeToggleBtn.innerHTML = ico(theme === 'dark' ? 'sun' : 'moon');
  themeToggleBtn.title = theme === 'dark' ? '낮 모드(밝은 화면)로 전환' : '밤 모드(어두운 화면)로 전환';
}

function applyTheme(theme, persist){
  document.documentElement.setAttribute('data-theme', theme);
  if(persist){
    try{ localStorage.setItem(THEME_STORAGE_KEY, theme); }catch(e){ /* 저장 실패해도 이번 세션 안에서는 그대로 동작 */ }
  }
  updateThemeToggleLabel(theme);
}

themeToggleBtn.addEventListener('click', () => {
  const next = getEffectiveTheme() === 'dark' ? 'light' : 'dark';
  applyTheme(next, true);
});

// 사용자가 명시적으로 고른 적 없으면(저장된 값이 없으면) OS 테마가 바뀔 때 같이 따라간다.
if(prefersDarkQuery){
  const onSystemThemeChange = (e) => {
    if(!getSavedTheme()) applyTheme(e.matches ? 'dark' : 'light', false);
  };
  if(prefersDarkQuery.addEventListener) prefersDarkQuery.addEventListener('change', onSystemThemeChange);
  else if(prefersDarkQuery.addListener) prefersDarkQuery.addListener(onSystemThemeChange);
}

applyTheme(getEffectiveTheme(), false);

// ---- NSFW(성인) 콘텐츠 보기 방식 ----
// 프로젝트의 "성인 콘텐츠 포함" 체크박스(project-mature-input)로 표시한 것과 그 프로젝트의
// 결과물은 이 버튼(눌러서 순환 — 영상 자동재생 방식 버튼과 같은 방식)으로 세 단계를 오간다.
//   보기: 그대로 보여준다.
//   블러: 목록에는 있되 미리보기를 가린다(프로젝트 대표 이미지가 없을 때 쓰는 것과 같은 아이콘
//        placeholder — 실제 이미지를 흐리게 처리하는 대신 아예 안 받아오므로 가볍다). 열어서 보는
//        건(라이트박스) 막지 않는다.
//   안 보기: 서버 쪽 hide_nsfw 필터로 목록에서 아예 뺀다(galleryFilterQs 참고).
// 브라우저별 설정이고(테마와 같은 방식), 기본값은 "안 보기"다 — 화면 공유나 다른 사람이 볼 때
// 실수로 노출되지 않도록 안전한 쪽을 기본으로 한다.
const NSFW_MODES = ['show', 'blur', 'hide'];
const NSFW_META = {
  show: { icon: 'eye',     label: '보기',    hint: '성인 콘텐츠를 그대로 보여줘요' },
  blur: { icon: 'eye-off', label: '블러',    hint: '성인 콘텐츠는 목록엔 있되 미리보기를 가려요(열면 볼 수 있어요)' },
  hide: { icon: 'ban',     label: '안 보기', hint: '성인 콘텐츠를 목록에서 아예 빼요' },
};
const NSFW_STORAGE_KEY = 'nightshift-nsfw-mode';
const nsfwToggleBtn = document.getElementById('nsfw-toggle-btn');

function readNsfwMode(){
  try{
    const saved = localStorage.getItem(NSFW_STORAGE_KEY);
    if(NSFW_MODES.includes(saved)) return saved;
    if(saved === '1') return 'hide';   // 예전 2단계(보기/숨김) 값 이관
    if(saved === '0') return 'show';
  }catch(e){ /* 기본값으로 */ }
  return 'hide';
}
let nsfwMode = readNsfwMode();

function isNsfwHidden(){ return nsfwMode === 'hide'; }   // 서버 필터(hide_nsfw)에 쓸 값

function paintNsfwToggle(){
  const meta = NSFW_META[nsfwMode];
  nsfwToggleBtn.innerHTML = ico(meta.icon);
  nsfwToggleBtn.title = `NSFW 콘텐츠: ${meta.label} — ${meta.hint} (눌러서 바꾸기)`;
}

function applyNsfwMode(mode, persist){
  nsfwMode = mode;
  if(persist){
    try{ localStorage.setItem(NSFW_STORAGE_KEY, mode); }catch(e){ /* 이번 세션 안에서는 그대로 동작 */ }
  }
  paintNsfwToggle();
  if(typeof fetchGalleryImages === 'function') fetchGalleryImages();
  if(typeof fetchGalleryVideos === 'function') fetchGalleryVideos();
  if(typeof renderHome === 'function') renderHome();
  if(currentTab === 'prboard' && typeof renderBoard === 'function') renderBoard();
}

nsfwToggleBtn.addEventListener('click', () => {
  const next = NSFW_MODES[(NSFW_MODES.indexOf(nsfwMode) + 1) % NSFW_MODES.length];
  applyNsfwMode(next, true);
  if(typeof flashNotice === 'function') flashNotice(`NSFW 콘텐츠: ${NSFW_META[next].label}`);
});

paintNsfwToggle();

// ---- 로그인 / 회원가입 / 계정 ----
// 로그인은 세션 쿠키다(서버 auth.py). 로그인·로그아웃에 성공하면 화면을 새로 불러와서, 처음부터 그 회원 기준으로 다시
// 시작하게 한다(파드·프로젝트·작업이 회원마다 다르므로 이전 화면의 상태를 이어 쓰지 않는다).
let currentUser = null;
function isAdminUser(){ return !!currentUser && currentUser.role === 'admin'; }

const loginScreen = document.getElementById('login-screen');
const loginScreenError = document.getElementById('login-screen-error');
const loginScreenNotice = document.getElementById('login-screen-notice');

function setAuthMode(mode){
  const register = mode === 'register';
  document.getElementById('login-form').style.display = register ? 'none' : '';
  document.getElementById('register-form').style.display = register ? '' : 'none';
  document.getElementById('login-tab-login').classList.toggle('active', !register);
  document.getElementById('login-tab-register').classList.toggle('active', register);
  document.getElementById('login-tagline').textContent = register
    ? '아이디·이메일·비밀번호로 가입 신청을 하세요.' : '아이디와 비밀번호로 로그인하세요.';
  loginScreenError.textContent = '';
  loginScreenNotice.textContent = '';
  const first = document.getElementById(register ? 'register-username' : 'login-username');
  if(first && loginScreen.style.display !== 'none') first.focus();
}

// 로그인 폼(특히 #login-password, type="password")이 display:none이어도 DOM에 남아
// 있으면 안드로이드 크롬 등의 비밀번호 관리자가 페이지의 다른 입력칸(예: 시드 개수)에
// 포커스만 줘도 "저장된 비밀번호를 사용할까요?"를 계속 들이댄다 — visibility가 아니라
// DOM에 존재하는지만 본다. 로그인한 동안은 아예 떼어 둔다(position:fixed라 body 안
// 어디에 다시 붙여도 자리는 같다). __nightshiftOnSignedOut(세션 끊김)처럼 새로고침 없이
// 다시 보여줘야 할 때는 showLoginScreen이 도로 붙인다.
function hideLoginScreen(){
  loginScreen.style.display = 'none';
  if(loginScreen.isConnected) loginScreen.remove();
}
function showLoginScreen(message, mode){
  if(!loginScreen.isConnected) document.body.appendChild(loginScreen);
  loginScreen.style.display = 'flex';
  setAuthMode(mode || 'login');
  loginScreenError.textContent = message || '';
}
window.__nightshiftOnSignedOut = () => showLoginScreen('로그인이 끝났어요. 다시 로그인하세요.');

document.querySelectorAll('[data-auth-mode]').forEach(btn => btn.addEventListener('click', () => setAuthMode(btn.dataset.authMode)));

async function postAuth(path, body){
  const res = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, status: res.status, data };
}

document.getElementById('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = document.getElementById('login-submit');
  const username = document.getElementById('login-username').value.trim();
  const password = document.getElementById('login-password').value;
  loginScreenError.textContent = '';
  loginScreenNotice.textContent = '';
  if(!username || !password){ loginScreenError.textContent = '아이디와 비밀번호를 입력하세요.'; return; }
  btn.disabled = true;
  try{
    const { ok, data } = await postAuth('/api/auth/login', { username, password });
    if(!ok){ loginScreenError.textContent = data.detail || '로그인하지 못했어요.'; return; }
    location.reload();
  }catch(err){
    loginScreenError.textContent = '서버에 연결하지 못했어요. 잠시 후 다시 시도하세요.';
  }finally{
    btn.disabled = false;
  }
});

document.getElementById('register-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = document.getElementById('register-submit');
  const username = document.getElementById('register-username').value.trim();
  const email = document.getElementById('register-email').value.trim();
  const password = document.getElementById('register-password').value;
  const password2 = document.getElementById('register-password2').value;
  loginScreenError.textContent = '';
  loginScreenNotice.textContent = '';
  if(!username || !email || !password){ loginScreenError.textContent = '아이디, 이메일, 비밀번호를 모두 입력하세요.'; return; }
  if(password !== password2){ loginScreenError.textContent = '비밀번호 확인이 일치하지 않아요.'; return; }
  btn.disabled = true;
  try{
    const { ok, data } = await postAuth('/api/auth/register', { username, email, password });
    if(!ok){ loginScreenError.textContent = data.detail || '가입 신청을 하지 못했어요.'; return; }
    setAuthMode('login');
    document.getElementById('login-username').value = username;
    document.getElementById('register-password').value = '';
    document.getElementById('register-password2').value = '';
    loginScreenNotice.textContent = data.message || '가입 신청이 접수됐어요. 관리자가 승인하면 로그인할 수 있어요.';
  }catch(err){
    loginScreenError.textContent = '서버에 연결하지 못했어요. 잠시 후 다시 시도하세요.';
  }finally{
    btn.disabled = false;
  }
});

async function logout(){
  try{ await postAuth('/api/auth/logout', {}); }catch(e){ /* 그래도 화면은 새로 불러온다 */ }
  location.reload();
}

// 계정 창 — 내 정보와 비밀번호 변경, 개인 API 키, 로그아웃.
const accountModal = document.getElementById('account-modal');
async function openAccountModal(){
  if(!currentUser) return;
  // 평소엔(모달을 안 열었을 때) DOM에서 떼어 둔다 — 아래 hideLoginScreen 주석과 같은 이유
  // (비밀번호·토큰 칸이 display:none이어도 DOM에 남아 있으면 안드로이드 자동완성이 계속
  // 들이댄다). 열 때만 도로 붙인다.
  if(!accountModal.isConnected) document.body.appendChild(accountModal);
  const rows = [['아이디', currentUser.username], ['이메일', currentUser.email], ['권한', currentUser.role === 'admin' ? '관리자' : '일반 회원']];
  document.getElementById('account-facts').innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${escapeHtml(v)}</dd>`).join('');
  for(const id of ['account-old-password', 'account-new-password', 'account-new-password2']) document.getElementById(id).value = '';
  document.getElementById('account-error').textContent = '';
  document.getElementById('account-ok').textContent = '';
  document.getElementById('account-civitai-token').value = '';
  document.getElementById('account-runpod-key').value = '';
  for(const btn of document.querySelectorAll('.account-secret-toggle')){
    document.getElementById(btn.dataset.target).type = 'password';
    btn.innerHTML = '<svg class="ico"><use href="#i-eye"/></svg>';
  }
  accountModal.style.display = 'flex';
  try{
    const res = await fetch('/api/auth/secrets');
    if(res.ok){
      const data = await res.json();
      document.getElementById('account-civitai-token').value = data.civitai_token || '';
      document.getElementById('account-runpod-key').value = data.runpod_api_key || '';
    }
  }catch(e){ /* 못 받아와도 빈 칸으로 두면 그만이다 */ }
}
function closeAccountModal(){
  accountModal.style.display = 'none';
  if(accountModal.isConnected) accountModal.remove();
}
document.getElementById('user-chip').addEventListener('click', openAccountModal);
document.getElementById('account-modal-close').addEventListener('click', closeAccountModal);
accountModal.addEventListener('click', (e) => { if(e.target === accountModal) closeAccountModal(); });
document.getElementById('account-logout-btn').addEventListener('click', logout);

document.querySelectorAll('.account-secret-toggle').forEach(btn => {
  btn.addEventListener('click', () => {
    const input = document.getElementById(btn.dataset.target);
    const showing = input.type === 'text';
    input.type = showing ? 'password' : 'text';
    btn.innerHTML = `<svg class="ico"><use href="#i-${showing ? 'eye' : 'eye-off'}"/></svg>`;
  });
});

// "수정사항 저장" 하나로 비밀번호 변경(입력했을 때만)과 API 키 저장(항상)을 같이 처리한다.
document.getElementById('account-save-btn').addEventListener('click', async () => {
  const errorEl = document.getElementById('account-error');
  const okEl = document.getElementById('account-ok');
  errorEl.textContent = ''; okEl.textContent = '';
  const oldPassword = document.getElementById('account-old-password').value;
  const newPassword = document.getElementById('account-new-password').value;
  const newPassword2 = document.getElementById('account-new-password2').value;
  const wantsPasswordChange = !!(oldPassword || newPassword || newPassword2);
  if(wantsPasswordChange){
    if(!oldPassword || !newPassword){ errorEl.textContent = '비밀번호를 바꾸려면 현재 비밀번호와 새 비밀번호를 모두 입력하세요.'; return; }
    if(newPassword !== newPassword2){ errorEl.textContent = '새 비밀번호 확인이 일치하지 않아요.'; return; }
  }
  const messages = [];
  if(wantsPasswordChange){
    const { ok, data } = await postAuth('/api/auth/change-password', { old_password: oldPassword, new_password: newPassword });
    if(!ok){ errorEl.textContent = data.detail || '비밀번호를 바꾸지 못했어요.'; return; }
    messages.push('비밀번호를 바꿨어요(다른 기기는 로그아웃돼요)');
    for(const id of ['account-old-password', 'account-new-password', 'account-new-password2']) document.getElementById(id).value = '';
  }
  const secretsPayload = {
    civitai_token: document.getElementById('account-civitai-token').value,
    runpod_api_key: document.getElementById('account-runpod-key').value,
  };
  const res = await fetch('/api/auth/secrets', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(secretsPayload) });
  const data = await res.json().catch(() => ({}));
  if(!res.ok){ errorEl.textContent = data.detail || 'API 키를 저장하지 못했어요.'; return; }
  messages.push('API 키를 저장했어요');
  okEl.textContent = messages.join(' · ') + '.';
});

// 여기까지 계정 모달 안 요소를 getElementById로 찾아 리스너를 건 다음에야(리스너는
// 노드 자체에 붙으므로 이후 떼었다 붙여도 그대로 유지된다) 처음 한 번 떼어 둔다 —
// 페이지를 열자마자 비밀번호/토큰 칸이 이유 없이 DOM에 계속 남아 있지 않게.
if(accountModal.isConnected) accountModal.remove();

// 헤더의 사용자 칩과 관리자 전용 탭을 로그인한 회원에 맞춘다.
function applyUserUi(user){
  currentUser = user;
  const chip = document.getElementById('user-chip');
  chip.style.display = user ? '' : 'none';
  if(user){
    document.getElementById('user-chip-name').textContent = user.username;
    document.getElementById('user-chip-role').hidden = user.role !== 'admin';
  }
  const adminBtn = document.getElementById('admin-tab-btn');
  if(adminBtn) adminBtn.style.display = user && user.role === 'admin' ? '' : 'none';
  const dbBtn = document.getElementById('db-tab-btn');
  if(dbBtn) dbBtn.style.display = user && user.role === 'admin' ? '' : 'none';
}

window.__nightshiftAuthReady.then(user => {
  if(user){ hideLoginScreen(); applyUserUi(user); }
  else showLoginScreen('', 'login');
});

// ---- 회원 관리 (관리자 전용) ----
let adminUsers = [];
const ADMIN_STATUS_LABEL = { pending: '승인 대기', active: '활성', disabled: '정지' };

async function fetchAdminUsers(){
  if(!isAdminUser()) return;
  try{
    const res = await fetch('/api/admin/users');
    if(!res.ok) throw new Error('failed');
    adminUsers = (await res.json()).users || [];
    document.getElementById('admin-error').textContent = '';
  }catch(e){
    document.getElementById('admin-error').textContent = '회원 목록을 불러오지 못했어요.';
    return;
  }
  renderAdminUsers();
}

function renderAdminUsers(){
  const count = (status) => adminUsers.filter(u => u.status === status).length;
  const pending = count('pending');
  const badge = document.getElementById('admin-pending-badge');
  badge.hidden = pending === 0;
  badge.textContent = String(pending);
  badge.title = `승인 대기 ${pending}명`;
  document.getElementById('admin-stats').innerHTML = [
    ['전체 회원', adminUsers.length, ''], ['승인 대기', pending, pending ? ' attention' : ''],
    ['활성', count('active'), ''], ['정지', count('disabled'), ''],
  ].map(([label, n, cls]) => `<div class="admin-stat${cls}">${label}<b>${n}</b></div>`).join('');

  document.getElementById('admin-users').innerHTML = adminUsers.map(u => {
    const isAdminRow = u.role === 'admin';
    const actions = isAdminRow ? '' : ({
      pending: `<button class="submit-btn" data-admin-action="approve" data-id="${u.id}" type="button">${ico('check')} 승인</button>
                <button class="del-btn" data-admin-action="delete" data-id="${u.id}" type="button">${ico('trash-2')} 거절</button>`,
      active: `<button class="load-btn" data-admin-action="disable" data-id="${u.id}" type="button">${ico('ban')} 정지</button>
               <button class="load-btn" data-admin-action="reset" data-id="${u.id}" type="button">${ico('key-round')} 비밀번호 초기화</button>
               <button class="del-btn" data-admin-action="delete" data-id="${u.id}" type="button">${ico('trash-2')} 삭제</button>`,
      disabled: `<button class="load-btn" data-admin-action="approve" data-id="${u.id}" type="button">${ico('user-check')} 재개</button>
                 <button class="load-btn" data-admin-action="reset" data-id="${u.id}" type="button">${ico('key-round')} 비밀번호 초기화</button>
                 <button class="del-btn" data-admin-action="delete" data-id="${u.id}" type="button">${ico('trash-2')} 삭제</button>`,
    }[u.status] || '');
    return `
      <div class="admin-user ${u.status}" data-user-id="${u.id}">
        <div>
          <div class="admin-user-name">${escapeHtml(u.username)}${isAdminRow ? '<span class="role-badge">Admin</span>' : ''}</div>
          <div class="admin-user-sub">${escapeHtml(u.email)}</div>
        </div>
        <span class="status-pill ${u.status}">${ADMIN_STATUS_LABEL[u.status] || u.status}</span>
        <div class="admin-user-meta">
          <span>가입 ${escapeHtml(fmtGalleryDateTime(u.created_at))}</span>
          <span>최근 로그인 ${u.last_login_at ? escapeHtml(fmtGalleryDateTime(u.last_login_at)) : '없음'}</span>
          <span>프로젝트 ${u.project_count} · 작업 ${u.job_count} · 결과물 ${u.asset_count}개 (${fmtBytes(u.asset_bytes)})</span>
        </div>
        <div class="admin-user-actions">${actions}</div>
      </div>`;
  }).join('') || '<div class="empty">회원이 없어요</div>';
}

async function adminAction(action, id){
  const user = adminUsers.find(u => u.id === id);
  if(!user) return;
  const errorEl = document.getElementById('admin-error');
  errorEl.textContent = '';
  let path, method = 'POST', body = {};
  if(action === 'approve'){ path = `/api/admin/users/${id}/status`; body = { status: 'active' }; }
  else if(action === 'disable'){
    if(!confirm(`'${user.username}' 계정을 정지할까요? 로그인 중인 기기는 바로 로그아웃돼요.`)) return;
    path = `/api/admin/users/${id}/status`; body = { status: 'disabled' };
  }else if(action === 'reset'){
    const password = window.prompt(`'${user.username}'의 새 비밀번호를 입력하세요 (8자 이상). 로그인 중인 기기는 로그아웃돼요.`);
    if(!password) return;
    path = `/api/admin/users/${id}/reset-password`; body = { new_password: password };
  }else if(action === 'delete'){
    const extra = user.status === 'pending' ? '가입 신청을 거절하고 삭제' : '계정을 삭제';
    if(!confirm(`'${user.username}' ${extra}할까요? 이 회원의 프로젝트·작업·결과물·워커는 지워지지 않고 관리자 소유로 남아요.`)) return;
    path = `/api/admin/users/${id}`; method = 'DELETE'; body = null;
  }else return;
  try{
    const res = await fetch(path, { method, headers: { 'Content-Type': 'application/json' }, body: body === null ? undefined : JSON.stringify(body) });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || '처리하지 못했어요.');
  }catch(e){
    errorEl.textContent = e.message || '처리하지 못했어요.';
    return;
  }
  await fetchAdminUsers();
}

document.getElementById('admin-users').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-admin-action]');
  if(btn) adminAction(btn.dataset.adminAction, Number(btn.dataset.id));
});
document.getElementById('admin-refresh-btn').addEventListener('click', fetchAdminUsers);
// 승인 대기 배지를 최신으로 — 관리자일 때만 30초마다 확인한다.
setInterval(() => { if(isAdminUser()) fetchAdminUsers(); }, 30000);

// ---- DB 탭 — RunPod 세션(사용 내역). ----
let runpodSessions = [];
let runpodSessionSummary = null;
// 기간 거르기(NS-20) — 단위(all/year/month/week/day)와 그 기간의 시작 시각(현지 자정). 경계는 브라우저가
// 현지 시각으로 계산해 UTC ISO로 보내고, 서버는 [start, end) 안에 시작한 세션만 돌려준다. 주는 월요일 시작.
let dbPeriodUnit = localStorage.getItem('dbSessionUnit') || 'month';
let dbPeriodStart = dbPeriodStartOf(dbPeriodUnit, new Date());
let dbSessionsSeq = 0;

function dbPeriodStartOf(unit, d){
  if(unit === 'year') return new Date(d.getFullYear(), 0, 1);
  if(unit === 'month') return new Date(d.getFullYear(), d.getMonth(), 1);
  if(unit === 'week') return new Date(d.getFullYear(), d.getMonth(), d.getDate() - (d.getDay() + 6) % 7);
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

function dbPeriodShift(unit, d, n){
  if(unit === 'year') return new Date(d.getFullYear() + n, 0, 1);
  if(unit === 'month') return new Date(d.getFullYear(), d.getMonth() + n, 1);
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n * (unit === 'week' ? 7 : 1));
}

function dbPeriodIsCurrent(){
  return dbPeriodUnit === 'all' || +dbPeriodStart === +dbPeriodStartOf(dbPeriodUnit, new Date());
}

function dbPeriodLabel(){
  const s = dbPeriodStart, y = s.getFullYear();
  const yp = y === new Date().getFullYear() ? '' : `${y}년 `;
  if(dbPeriodUnit === 'year') return `${y}년`;
  if(dbPeriodUnit === 'month') return `${y}년 ${s.getMonth() + 1}월`;
  if(dbPeriodUnit === 'day') return `${yp}${s.getMonth() + 1}월 ${s.getDate()}일 (${'일월화수목금토'[s.getDay()]})`;
  const e = dbPeriodShift('day', s, 6);
  return `${yp}${s.getMonth() + 1}월 ${s.getDate()}일 – ${e.getMonth() !== s.getMonth() ? `${e.getMonth() + 1}월 ` : ''}${e.getDate()}일`;
}

function renderDbPeriodBar(){
  document.querySelectorAll('#db-period-unit .enhance-mode-btn').forEach(b => {
    const on = b.dataset.unit === dbPeriodUnit;
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', on);
  });
  document.getElementById('db-period-nav').hidden = dbPeriodUnit === 'all';
  document.getElementById('db-period-label').textContent = dbPeriodLabel();
  document.getElementById('db-period-next').disabled = dbPeriodIsCurrent();   // 앞으로의 기간엔 세션이 없다
  document.getElementById('db-period-today').hidden = dbPeriodIsCurrent();
}

function setDbPeriod(unit, start){
  dbPeriodUnit = unit;
  dbPeriodStart = start;
  localStorage.setItem('dbSessionUnit', unit);
  renderDbPeriodBar();
  fetchRunpodSessions();
}

async function fetchRunpodSessions(){
  const errorEl = document.getElementById('db-runpod-sessions-error');
  errorEl.textContent = '';
  renderDbPeriodBar();
  const seq = ++dbSessionsSeq;   // 기간을 빨리 넘길 때 늦게 온 옛 응답이 덮지 않게
  const qs = dbPeriodUnit === 'all' ? '' :
    `?start=${encodeURIComponent(dbPeriodStart.toISOString())}&end=${encodeURIComponent(dbPeriodShift(dbPeriodUnit, dbPeriodStart, 1).toISOString())}`;
  try{
    const res = await fetch('/api/runpod-sessions' + qs);
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || '불러오지 못했어요.');
    if(seq !== dbSessionsSeq) return;
    runpodSessions = data.sessions || [];
    runpodSessionSummary = data.summary || null;
  }catch(e){
    if(seq === dbSessionsSeq) errorEl.textContent = e.message || '불러오지 못했어요 — 연결을 확인하세요.';
    return;
  }
  renderRunpodSessions();
}

function renderDbSessionStats(){
  const el = document.getElementById('db-session-stats');
  const s = runpodSessionSummary;
  if(!s){ el.innerHTML = ''; return; }
  const est = s.estimated_count ? ' *' : '';
  const notes = [];
  if(s.estimated_count) notes.push(`* 진행 중 ${s.estimated_count}개는 지금 시각까지로 추정해 넣었어요`);
  if(s.count > runpodSessions.length) notes.push(`목록은 최근 ${runpodSessions.length}개만 보여요 — 합계는 ${s.count}개 전체예요`);
  el.innerHTML = `
    <div class="admin-stat">총 시간<b>${fmtDbDuration(s.duration_sec)}${est}</b></div>
    <div class="admin-stat">총 비용<b>${fmtDbCost(s.cost_total)}${est}</b></div>
    <div class="admin-stat">세션<b>${s.count}개</b></div>
    ${notes.length ? `<div class="db-session-stats-note">${notes.map(escapeHtml).join(' · ')}</div>` : ''}`;
}

function fmtDbDuration(sec){
  if(sec == null) return '-';
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h > 0 ? `${h}시간 ${m}분` : `${m}분`;
}

function fmtDbCost(v){
  return v == null ? '-' : `$${v.toFixed(2)}`;
}

function renderRunpodSessions(){
  document.getElementById('db-runpod-sessions-count').textContent = `${runpodSessionSummary ? runpodSessionSummary.count : runpodSessions.length}개`;
  renderDbSessionStats();
  const list = document.getElementById('db-runpod-sessions-list');
  if(runpodSessions.length === 0){
    if(dbPeriodUnit !== 'all'){ list.innerHTML = '<div class="comfy-model-empty">이 기간에는 세션이 없어요.</div>'; return; }
    list.innerHTML = '<div class="comfy-model-empty">아직 기록된 세션이 없어요 — Claude에게 RunPod pod를 켜고 nightshift MCP의 sync 도구를 불러달라고 하면 여기 쌓이기 시작해요.</div>';
    return;
  }
  const header = `<div class="db-cols db-header">
    <span>#</span><span>파드 이름</span><span>GPU</span><span>VRAM</span><span>시작</span><span>종료</span><span>시간</span><span>비용</span>
  </div>`;
  const rows = runpodSessions.map(s => `
    <div class="db-row db-cols${s.ended_at ? '' : ' db-row-open'}">
      <span class="db-cell-num">${s.id}</span>
      <span class="db-cell-name" title="${escapeHtml(s.runpod_pod_id + (s.worker_name ? ' — 워커 ' + s.worker_name : ''))}"><span class="db-cell-pod" title="${escapeHtml(s.pod_name || s.runpod_pod_id)}">${escapeHtml(s.pod_name || s.runpod_pod_id)}</span>${s.worker_name ? `<span class="db-cell-worker" title="${escapeHtml(s.worker_name)}">${escapeHtml(s.worker_name)}</span>` : ''}</span>
      <span class="db-cell-dim" data-label="GPU">${s.gpu_type ? escapeHtml(s.gpu_type) : '-'}</span>
      <span class="db-cell-dim" data-label="VRAM">${s.vram_gb ? `${s.vram_gb}GB` : '-'}</span>
      <span data-label="시작">${fmtGalleryDateTime(s.started_at)}</span>
      <span class="${s.ended_at ? '' : 'db-cell-open'}" data-label="종료">${s.ended_at ? fmtGalleryDateTime(s.ended_at) : '진행 중'}</span>
      <span class="db-cell-dim" data-label="시간">${fmtDbDuration(s.duration_sec)}${s.estimated ? ' (추정)' : ''}</span>
      <span class="db-cell-dim" data-label="비용">${fmtDbCost(s.cost_total)}${s.estimated ? ' (추정)' : ''}</span>
    </div>`).join('');
  list.innerHTML = `<div class="db-table db-sessions">${header}${rows}</div>`;
}

document.getElementById('db-runpod-sessions-refresh-btn').addEventListener('click', fetchRunpodSessions);
// 단위를 바꾸면 그 단위의 지금 기간(올해·이번 달·이번 주·오늘)이 자동으로 골라진다.
document.querySelectorAll('#db-period-unit .enhance-mode-btn').forEach(b => b.addEventListener('click', () =>
  setDbPeriod(b.dataset.unit, dbPeriodStartOf(b.dataset.unit, new Date()))));
document.getElementById('db-period-prev').addEventListener('click', () => setDbPeriod(dbPeriodUnit, dbPeriodShift(dbPeriodUnit, dbPeriodStart, -1)));
document.getElementById('db-period-next').addEventListener('click', () => setDbPeriod(dbPeriodUnit, dbPeriodShift(dbPeriodUnit, dbPeriodStart, 1)));
document.getElementById('db-period-today').addEventListener('click', () => setDbPeriod(dbPeriodUnit, dbPeriodStartOf(dbPeriodUnit, new Date())));
// 기간 이름을 누르면 그 자리에서 직접 고르기 — 년은 숫자, 월은 month, 주·일은 date(주는 고른 날의 월요일로).
document.getElementById('db-period-label').addEventListener('click', () => {
  const label = document.getElementById('db-period-label'), pick = document.getElementById('db-period-pick');
  const s = dbPeriodStart, now = new Date(), p = n => String(n).padStart(2, '0');
  const ymd = d => `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  if(dbPeriodUnit === 'year'){ pick.type = 'number'; pick.min = 2020; pick.max = now.getFullYear(); pick.value = s.getFullYear(); }
  else if(dbPeriodUnit === 'month'){ pick.type = 'month'; pick.min = ''; pick.max = `${now.getFullYear()}-${p(now.getMonth() + 1)}`; pick.value = `${s.getFullYear()}-${p(s.getMonth() + 1)}`; }
  else { pick.type = 'date'; pick.min = ''; pick.max = ymd(now); pick.value = ymd(s); }
  label.hidden = true;
  pick.hidden = false;
  pick.focus();
  try{ if(pick.type !== 'number') pick.showPicker(); }catch(_){}   // 지원하지 않는 브라우저는 입력칸만
});
document.getElementById('db-period-pick').addEventListener('change', e => {
  const v = e.target.value;
  if(!v) return;
  const [y, m, d] = v.split('-').map(Number);
  if(dbPeriodUnit === 'year' && !(y >= 2000 && y <= 9999)) return;
  setDbPeriod(dbPeriodUnit, dbPeriodStartOf(dbPeriodUnit, new Date(y, (m || 1) - 1, d || 1)));
  if(e.target.type !== 'number') e.target.blur();
});
document.getElementById('db-period-pick').addEventListener('blur', e => {
  e.target.hidden = true;
  document.getElementById('db-period-label').hidden = false;
});

// ---- DB 탭 — 업데이트 내역(git 커밋 로그, git_log.py). nightshift 저장소의 커밋을
// 그대로 읽어 보여준다 — 따로 기록하는 동작이 없다(커밋 메시지가 곧 이 목록).
// 날짜로 거르는 건 이미 받아 둔 목록 안에서만 하면 되니(최대 300개) 서버를 다시
// 부르지 않고 여기서 필터링한다. ----
let gitLogCommits = [];
let gitlogDateFrom = '';
let gitlogDateTo = '';
let gitlogLimit = LIST_PAGE;   // 앞에서부터 몇 개 그릴지 — 날짜를 바꾸면 처음부터

async function fetchGitLog(){
  const errorEl = document.getElementById('db-git-log-error');
  errorEl.textContent = '';
  try{
    const res = await fetch('/api/git-log');
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || '불러오지 못했어요.');
    gitLogCommits = data.commits || [];
  }catch(e){
    errorEl.textContent = e.message || '불러오지 못했어요 — 연결을 확인하세요.';
    return;
  }
  renderGitLog();
}

function renderGitLog(){
  document.getElementById('db-gitlog-filter-clear').style.display = (gitlogDateFrom || gitlogDateTo) ? '' : 'none';
  const filtered = gitLogCommits.filter(c => {
    const day = (c.date || '').slice(0, 10);
    if(gitlogDateFrom && day < gitlogDateFrom) return false;
    if(gitlogDateTo && day > gitlogDateTo) return false;
    return true;
  });
  document.getElementById('db-git-log-count').textContent = filtered.length === gitLogCommits.length
    ? `${gitLogCommits.length}개` : `${filtered.length} / ${gitLogCommits.length}개`;
  const list = document.getElementById('db-git-log-list');
  if(gitLogCommits.length === 0){
    updateLoadMore('db-git-log-more-btn', 0, '개');
    list.innerHTML = '<div class="comfy-model-empty">불러온 커밋이 없어요 — 이 서버가 git 저장소가 아니거나 git이 없을 수 있어요.</div>';
    return;
  }
  updateLoadMore('db-git-log-more-btn', filtered.length - gitlogLimit, '개');
  if(filtered.length === 0){
    list.innerHTML = '<div class="comfy-model-empty">이 기간에는 커밋이 없어요.</div>';
    return;
  }
  list.innerHTML = `<div class="changelog-list">${filtered.slice(0, gitlogLimit).map(c => `
    <div class="changelog-entry">
      <div class="changelog-head">
        <span>#${c.seq}</span>
        <span>${fmtGalleryDateTime(c.date)}</span>
        <span class="changelog-version" title="${escapeHtml(c.hash)}">${escapeHtml(c.version)}</span>
        ${(c.tags || []).map(t => `<span class="changelog-tag">${escapeHtml(t)}</span>`).join('')}
      </div>
      <div class="changelog-title">${escapeHtml(c.title)}</div>
      ${c.content ? `<div class="changelog-body">${escapeHtml(c.content)}</div>` : ''}
    </div>
  `).join('')}</div>`;
}

document.getElementById('db-git-log-refresh-btn').addEventListener('click', fetchGitLog);
document.getElementById('db-gitlog-date-from').addEventListener('change', (e) => { gitlogDateFrom = e.target.value; gitlogLimit = LIST_PAGE; renderGitLog(); });
document.getElementById('db-gitlog-date-to').addEventListener('change', (e) => { gitlogDateTo = e.target.value; gitlogLimit = LIST_PAGE; renderGitLog(); });
setupLoadMore('db-git-log-more-btn', () => { gitlogLimit += LIST_PAGE; renderGitLog(); });
document.getElementById('db-gitlog-filter-clear').addEventListener('click', () => {
  gitlogDateFrom = ''; gitlogDateTo = ''; gitlogLimit = LIST_PAGE;
  document.getElementById('db-gitlog-date-from').value = '';
  document.getElementById('db-gitlog-date-to').value = '';
  renderGitLog();
});

// ---- DB 탭 — 생성 정보(프롬프트 등). nightshift 큐로 만든 것과 RunPod의 ComfyUI를
// 직접 써서 만든 뒤 가져온 것 모두, 파일에 박힌 메타(assets_index.py가 이미 읽어 둠)를
// 최신순으로 보여준다 — 따로 기록하는 동작이 없다. 종류/날짜로 거르는 건 이미 받아 둔
// 목록 안에서만 하면 되니(최대 300개) 서버를 다시 부르지 않고 여기서 필터링한다. ----
let genlogAssets = [];
let genlogKindFilter = '';
let genlogDateFrom = '';
let genlogDateTo = '';
// 생성 정보(프롬프트·시드·체크포인트)가 하나도 없는 줄 숨기기 — 기본 켜짐, 브라우저에 기억한다(필터 지우기와 무관한 보기 설정).
let genlogHideEmpty = localStorage.getItem('genlogHideEmpty') !== '0';
const genlogHasInfo = (a) => !!(a.prompt || a.negative_prompt || a.checkpoint || a.seed != null);
let genlogLimit = LIST_PAGE;   // 앞에서부터 몇 개 그릴지 — 종류·날짜를 바꾸면 처음부터

async function fetchGenerationLog(){
  const errorEl = document.getElementById('db-genlog-error');
  errorEl.textContent = '';
  try{
    const res = await fetch('/api/generation-log');
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || '불러오지 못했어요.');
    genlogAssets = data.assets || [];
  }catch(e){
    errorEl.textContent = e.message || '불러오지 못했어요 — 연결을 확인하세요.';
    return;
  }
  renderGenerationLog();
}

function renderGenerationLog(){
  document.getElementById('db-genlog-filter-clear').style.display =
    (genlogKindFilter || genlogDateFrom || genlogDateTo) ? '' : 'none';
  const filtered = genlogAssets.filter(a => {
    if(genlogKindFilter && a.kind !== genlogKindFilter) return false;
    if(genlogHideEmpty && !genlogHasInfo(a)) return false;
    const day = (a.created_at || '').slice(0, 10);
    if(genlogDateFrom && day < genlogDateFrom) return false;
    if(genlogDateTo && day > genlogDateTo) return false;
    return true;
  });
  document.getElementById('db-genlog-count').textContent = filtered.length === genlogAssets.length
    ? `${genlogAssets.length}개` : `${filtered.length} / ${genlogAssets.length}개`;
  const list = document.getElementById('db-genlog-list');
  updateLoadMore('db-genlog-more-btn', filtered.length - genlogLimit, '개');
  if(genlogAssets.length === 0){
    list.innerHTML = '<div class="comfy-model-empty">색인된 결과물이 없어요.</div>';
    return;
  }
  if(filtered.length === 0){
    list.innerHTML = '<div class="comfy-model-empty">조건에 맞는 결과물이 없어요.</div>';
    return;
  }
  list.innerHTML = `<div class="genlog-list">${filtered.slice(0, genlogLimit).map(a => {
    let params = {};
    try{ params = a.params_json ? JSON.parse(a.params_json) : {}; }catch(e){ /* 못 읽으면 빈 값 */ }
    const filename = (a.path || '').split('/').pop();
    const source = a.job_label ? `<span class="changelog-tag">${escapeHtml(a.job_label)}</span>`
      : (a.pod_name ? `<span class="changelog-tag">${escapeHtml(a.pod_name)}(직접)</span>` : '');
    const kindIcon = a.kind === 'video' ? 'clapperboard' : 'images';
    const loras = Array.isArray(params.loras) ? params.loras.filter(x => typeof x === 'string') : [];
    return `
      <div class="genlog-entry">
        <div class="genlog-head">
          <span class="genlog-kind genlog-kind-${a.kind}"><svg class="ico"><use href="#i-${kindIcon}"/></svg></span>
          <span title="결과물 번호 — 색인에 들어온 순서라 지우거나 걸러도 바뀌지 않아요">#${a.id}</span>
          <span>${fmtGalleryDateTime(a.created_at)}</span>
          <span title="${escapeHtml(a.path)}">${escapeHtml(filename)}</span>
          ${a.checkpoint ? `<span class="changelog-version">${escapeHtml(a.checkpoint)}</span>` : ''}
          ${a.seed != null ? `<span class="changelog-version">seed ${escapeHtml(String(a.seed))}</span>` : ''}
          ${source}
        </div>
        ${a.prompt ? `<div class="genlog-prompt">${escapeHtml(a.prompt)}</div>`
          : '<div class="comfy-model-empty">ComfyUI가 남긴 생성 정보가 없어요(회전·편집으로 지워졌거나 직접 넣은 파일일 수 있어요).</div>'}
        ${a.negative_prompt ? `<details class="genlog-neg"><summary>네거티브 프롬프트</summary><div class="genlog-prompt">${escapeHtml(a.negative_prompt)}</div></details>` : ''}
        ${loras.length ? `<div class="genlog-loras">LoRA: ${escapeHtml(loras.join(', '))}</div>` : ''}
      </div>`;
  }).join('')}</div>`;
}

document.getElementById('db-genlog-refresh-btn').addEventListener('click', fetchGenerationLog);
document.getElementById('db-genlog-kind-tabs').addEventListener('click', (e) => {
  const btn = e.target.closest('.db-genlog-kind-tab');
  if(!btn || btn.dataset.kind === genlogKindFilter) return;
  genlogKindFilter = btn.dataset.kind;
  genlogLimit = LIST_PAGE;
  document.querySelectorAll('.db-genlog-kind-tab').forEach(b => b.classList.toggle('active', b === btn));
  renderGenerationLog();
});
document.getElementById('db-genlog-date-from').addEventListener('change', (e) => { genlogDateFrom = e.target.value; genlogLimit = LIST_PAGE; renderGenerationLog(); });
document.getElementById('db-genlog-date-to').addEventListener('change', (e) => { genlogDateTo = e.target.value; genlogLimit = LIST_PAGE; renderGenerationLog(); });
setupLoadMore('db-genlog-more-btn', () => { genlogLimit += LIST_PAGE; renderGenerationLog(); });
document.getElementById('db-genlog-hide-empty').checked = genlogHideEmpty;
document.getElementById('db-genlog-hide-empty').addEventListener('change', (e) => {
  genlogHideEmpty = e.target.checked;
  localStorage.setItem('genlogHideEmpty', genlogHideEmpty ? '1' : '0');
  genlogLimit = LIST_PAGE;
  renderGenerationLog();
});
document.getElementById('db-genlog-filter-clear').addEventListener('click', () => {
  genlogKindFilter = ''; genlogDateFrom = ''; genlogDateTo = ''; genlogLimit = LIST_PAGE;
  document.querySelectorAll('.db-genlog-kind-tab').forEach(b => b.classList.toggle('active', b.dataset.kind === ''));
  document.getElementById('db-genlog-date-from').value = '';
  document.getElementById('db-genlog-date-to').value = '';
  renderGenerationLog();
});

// DB 탭 패널 선택 — 모델 탭의 종류 탭(model-kind-tabs)과 같은 .enhance-mode 패턴으로
// 셋 중 하나만 보여준다. 데이터는 (지금은) 탭을 열 때 셋 다 미리 받아 두고, 여기서는
// 화면에 뭘 보여줄지만 바꾼다 — 패널 전환은 새로고침 없이 즉시 되어야 하므로.
let dbPanelActive = 'runpod';
const DB_PANEL_IDS = { runpod: 'db-panel-runpod', gitlog: 'db-panel-gitlog', genlog: 'db-panel-genlog' };

function renderDbPanelTabs(){
  document.querySelectorAll('#db-panel-tabs .db-panel-tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.panel === dbPanelActive);
  });
  for(const [key, id] of Object.entries(DB_PANEL_IDS)){
    document.getElementById(id).style.display = key === dbPanelActive ? '' : 'none';
  }
}

document.getElementById('db-panel-tabs').addEventListener('click', (e) => {
  const btn = e.target.closest('.db-panel-tab');
  if(!btn || btn.dataset.panel === dbPanelActive) return;
  dbPanelActive = btn.dataset.panel;
  renderDbPanelTabs();
});

// 도움말 "?" — 데스크탑은 :hover로 이미 보이지만, 터치 기기는 호버가 없으니
// 탭(클릭)으로 open 클래스를 토글해서 열고, 바깥을 클릭하거나 Esc를 누르면 닫는다.
// 페이지 전체에 위임해 둬서, .help-tip이 몇 개든(지금 DB 탭에 2개) 따로 안 묶어도 된다.
document.addEventListener('click', (e) => {
  const btn = e.target.closest('.help-tip-btn');
  if(btn){
    e.stopPropagation();
    btn.closest('.help-tip').classList.toggle('open');
  }
  document.querySelectorAll('.help-tip.open').forEach(tip => {
    if(!tip.contains(e.target)) tip.classList.remove('open');
  });
});
document.addEventListener('keydown', (e) => {
  if(e.key === 'Escape') document.querySelectorAll('.help-tip.open').forEach(tip => tip.classList.remove('open'));
});

// 진행 중인 세션이 있으면 시간/비용 추정치가 계속 흐르니, DB 탭을 보는 동안만 20초마다 다시 그린다.
setInterval(() => {
  if(!isAdminUser() || currentTab !== 'db') return;
  if(runpodSessions.some(s => !s.ended_at)) fetchRunpodSessions();
}, 20000);

