// ---- 작업을 다른 파드로 옮기기 ----
// 파드마다 큐가 따로 돌기 때문에 파드 하나가 죽으면 그 큐만 멈춘다(옆 파드가 놀아도
// 자동으로 안 넘어간다). 그때 손으로 푸는 탈출구다.
let jobMoveTargetId = null;

function openJobMoveModal(jobId){
  const job = lastJobs.find(j => j.id === jobId);
  if(!job) return;
  jobMoveTargetId = jobId;
  document.getElementById('job-move-error').textContent = '';
  document.getElementById('job-move-current').textContent =
    `${job.template_label || job.template_id} · 지금 워커: ${podName(job.pod_id) || '(없음)'}`;
  const select = document.getElementById('job-move-select');
  const canAuto = job.status === 'pending' || job.status === 'queued';
  select.innerHTML = (canAuto ? '<option value="">자동 — 모델이 갖춰진 워커 (워커 지정 해제)</option>' : '')
    + podsCache
      .filter(p => p.enabled && p.id !== job.pod_id)
      .map(p => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)}</option>`)
      .join('');
  document.getElementById('job-move-modal').style.display = 'flex';
}

function closeJobMoveModal(){
  document.getElementById('job-move-modal').style.display = 'none';
  jobMoveTargetId = null;
}

document.getElementById('job-move-modal-close').addEventListener('click', closeJobMoveModal);
document.getElementById('job-move-cancel').addEventListener('click', closeJobMoveModal);
document.getElementById('job-move-modal').addEventListener('click', (e) => {
  if(e.target === document.getElementById('job-move-modal')) closeJobMoveModal();
});
document.getElementById('job-move-confirm').addEventListener('click', async () => {
  const podId = document.getElementById('job-move-select').value;
  const errorEl = document.getElementById('job-move-error');
  if(!jobMoveTargetId || document.getElementById('job-move-select').options.length === 0){ errorEl.textContent = '옮길 워커를 골라주세요.'; return; }
  try{
    const res = await fetch(`/api/jobs/${jobMoveTargetId}/move`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pod_id: podId }),
    });
    const data = await res.json().catch(() => ({}));
    if(!res.ok) throw new Error(data.detail || '옮기지 못했어요.');
    closeJobMoveModal();
    if(preflightCount(data.preflight) > 0){
      flashNotice('옮긴 워커에 없는 모델이 있어요 — ' + preflightLines(data.preflight).slice(0, 3).join(' / '));
    }
    fetchJobs();
  }catch(e){
    errorEl.textContent = e.message || '옮기지 못했어요.';
  }
});

// 상세 모달이 열려 있는 작업의 로그를 받아온다 — 모달 인스턴스가 하나뿐이라
// 항상 고정된 #job-detail-log를 대상으로 한다(예전엔 작업마다 log-${id} 요소가
// 따로 있었다).
async function loadLog(id){
  const res = await fetch(`/api/jobs/${id}/log`);
  // 401 등으로 실패하면 "아직 출력이 없어요"로 잘못 보이지 않게, 마지막으로
  // 받아온 로그를 그대로 두고 다음 폴링(2초 뒤)에서 다시 시도한다.
  if(!res.ok) return;
  const data = await res.json();
  const el = document.getElementById('job-detail-log');
  if(!el) return;
  const text = data.log || '(아직 출력이 없어요)';
  // 내용이 실제로 같으면 손대지 않는다 — textContent를 다시 대입하면 문자열이
  // 같아도 DOM 텍스트 노드가 새로 생겨서, 그 안의 텍스트를 드래그로 선택해
  // 복사하려던 게 풀려버린다(끝난 작업의 에러 메시지를 복사하기 불편하다는
  // 문제의 원인 중 하나였다 — 나머지 원인은 예전 tbody 재생성이었다. 지금은
  // render()가 매 tick마다 상세 모달을 다시 그려도 이 <pre> 자체는 절대 다시
  // 만들지 않으므로 그 문제가 재발하지 않는다).
  if(el.textContent !== text) el.textContent = text;
}

let templatesById = {};
let selectedFiles = { workflow: null, csv: null };

// onFile: 파일이 바뀔 때마다(비우는 것 포함) 부르는 선택적 콜백 — 워크플로우
// 슬롯이 이걸로 "이 서버에서 돌아갈 수 있는지" 검사를 건다. 최근 목록에서 고르거나
// 작업 설정을 불러올 때도 결국 여기 setFile을 거치므로 모든 경로가 함께 검사된다.
function setupSlot(slotId, inputId, key, accept, onFile, onUserFile){
  const slot = document.getElementById(slotId);
  const input = document.getElementById(inputId);
  const placeholder = slot.textContent;

  // fromUser: 사람이 방금 이 칸을 직접 눌러 고르거나 끌어다 놓았을 때만 true다(마법사가
  // 만든 파일을 채워 넣거나, "최근"/작업 불러오기가 채우는 프로그램적 호출은 false) —
  // onUserFile은 "사용자가 직접 올렸다"는 신호가 필요한 콜백(예: 마법사 역채움)에만 쓴다.
  function setFile(file, fromUser){
    selectedFiles[key] = file || null;
    if(file){
      slot.textContent = file.name;
      slot.classList.add('filled');
    } else {
      slot.textContent = placeholder;
      slot.classList.remove('filled');
    }
    if(onFile) onFile(file || null);
    if(fromUser && onUserFile) onUserFile(file || null);
  }

  slot.addEventListener('click', () => input.click());
  input.addEventListener('change', (e) => setFile(e.target.files[0], true));
  ['dragenter','dragover'].forEach(evt =>
    slot.addEventListener(evt, (e) => { e.preventDefault(); slot.classList.add('drag'); })
  );
  ['dragleave','drop'].forEach(evt =>
    slot.addEventListener(evt, (e) => { e.preventDefault(); slot.classList.remove('drag'); })
  );
  slot.addEventListener('drop', (e) => {
    const dropped = e.dataTransfer.files[0];
    if(dropped && dropped.name.toLowerCase().endsWith(accept)) setFile(dropped, true);
  });

  return setFile;
}

const setWorkflowFile = setupSlot('workflow-slot', 'workflow-input', 'workflow', '.json', checkWorkflowCompatibility, populateWizardFromUploadedWorkflow);
const setVideoWorkflowFile = setupSlot('video-workflow-slot', 'video-workflow-input', 'video_workflow', '.json');
const setCsvFile = setupSlot('csv-slot', 'csv-input', 'csv', '.csv');

function resetForm(){
  setLoraTriggerField([]);
  lastWizardModels = null;
  setWorkflowFile(null);
  setVideoWorkflowFile(null);
  setCsvFile(null);
}

const templateSelect = document.getElementById('template-select');
const optionsFields = document.getElementById('options-fields');
// 큐에 추가하기 직전에 실행할 콜백 목록 — 지금은 seed_batch의 "시드마다 다른
// Danbooru 프롬프트" 체크박스가 여기 자기 재계산 함수를 등록해서, 제출 시점의
// 최신 시드 개수/Danbooru 상태로 마지막에 한 번 더 갱신한다(renderOptionFields가
// 템플릿을 바꿀 때마다 비움).
const danbooruPreSubmitHooks = [];
const csvField = document.getElementById('csv-field');
const uploadError = document.getElementById('upload-error');

let assetsTreeByKind = { pose: [], depth: [], lineart: [] };
// /api/assets?kind=X 캐시 종류별 — 각각 [{name: char_no, pose_sets: [{name, count}, ...]}, ...]
// (서버 응답 키는 하위호환으로 "pose_sets"라는 이름을 그대로 쓴다, kind와 무관).
// "인물 수"(char_no)/"참조 세트" 캐스케이딩 드롭다운에 씀.

async function fetchAssets(){
  await Promise.all(['pose', 'depth', 'lineart'].map(async (kind) => {
    try{
      const res = await fetch(`/api/assets?kind=${kind}`);
      const data = await res.json();
      assetsTreeByKind[kind] = data.char_nos || [];
    }catch(e){
      assetsTreeByKind[kind] = [];
    }
  }));
}

function refSetsForCharNo(kind, charNo){
  const tree = assetsTreeByKind[kind] || [];
  const entry = tree.find(t => t.name === charNo);
  return entry ? entry.pose_sets : [];
}

// 옵션이 어느 참조 종류(pose/depth/lineart)를 다루는지 정한다 — app.py의
// resolve_option_kind와 같은 규칙: "kind"를 정적으로 선언했으면 그대로(주 참조),
// "kind_from"을 선언했으면 그 이름의 다른 옵션(보조 참조 종류 선택 select)의
// 현재 값을 그대로 따라간다(그 옵션이 아직 안 그려졌거나 값이 없으면 "none").
// 둘 다 없으면 기존 pose_batch/pose_csv_batch 매니페스트와의 하위호환을 위해
// "pose"를 기본값으로 쓴다.
function resolveOptionKind(opt){
  if(opt.kind) return opt.kind;
  if(opt.kind_from){
    const sourceSelect = optionsFields.querySelector(`[data-name="${opt.kind_from}"]`);
    return sourceSelect ? sourceSelect.value : 'none';
  }
  return 'pose';
}

function choicesFor(opt){
  const kind = resolveOptionKind(opt);
  if(kind === 'none') return [];
  if(opt.type === 'char_no'){
    const tree = assetsTreeByKind[kind] || [];
    return tree.map(t => ({ value: t.name, label: `${t.name}명`, disabled: false }));
  }
  if(opt.type === 'asset_folder'){
    // 참조 세트는 char_no 스코프 안에서 골라야 하므로, 같은 폼에 이미 렌더링돼
    // 있는 char_no 옵션(opt.char_no_option, 기본 "char_no")의 현재 선택값을 그
    // 자리에서 읽어와 필터링한다(manifest에서 그 char_no 옵션이 이 asset_folder
    // 옵션보다 앞에 선언돼 있어야 함).
    const charNoOptName = opt.char_no_option || 'char_no';
    const charNoSelect = optionsFields.querySelector(`[data-name="${charNoOptName}"]`);
    const charNo = charNoSelect ? charNoSelect.value : '';
    return refSetsForCharNo(kind, charNo).map(ps => ({ value: ps.name, label: `${ps.name} (${ps.count}장)`, disabled: ps.count === 0 }));
  }
  return (opt.choices || []).map(c => ({ value: c, label: c, disabled: false }));
}

function emptyTextFor(opt){
  const kind = resolveOptionKind(opt);
  if(kind === 'none') return '(보조 참조 사용 안 함)';
  if(opt.type === 'char_no') return '(사용 가능한 인물 수 없음)';
  if(opt.type === 'asset_folder') return `(사용 가능한 ${kind} 세트 없음)`;
  return '(선택지 없음)';
}

function populateSelectOptions(select, choices, emptyText){
  select.innerHTML = '';
  if(choices.length === 0){
    const empty = document.createElement('option');
    empty.value = '';
    empty.textContent = emptyText;
    select.appendChild(empty);
    select.disabled = true;
    return;
  }
  select.disabled = false;
  for(const c of choices){
    const o = document.createElement('option');
    o.value = c.value;
    o.textContent = c.label;
    o.disabled = c.disabled;
    select.appendChild(o);
  }
}

// 텍스트를 클립보드에 복사한다. Clipboard API는 보안 컨텍스트(https 또는
// localhost)에서만 동작하므로, 사설 IP를 http로 접속하는 등 그 밖의 상황에서는
// 숨겨진 textarea + execCommand('copy')로 대체한다.
async function copyTextToClipboard(text){
  if(navigator.clipboard && window.isSecureContext){
    await navigator.clipboard.writeText(text);
    return;
  }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  const ok = document.execCommand('copy');
  document.body.removeChild(ta);
  if(!ok) throw new Error('복사에 실패했어요.');
}

// getText()가 돌려주는 텍스트를 복사하는 작은 버튼. 성공/실패를 버튼 라벨과
// 색상으로 잠깐 보여주고 원래 상태로 되돌아간다.
function createCopyButton(getText){
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'copy-btn';
  btn.title = '복사';
  btn.innerHTML = ico('copy') + ' 복사';
  let resetTimer = null;
  btn.addEventListener('click', async () => {
    const text = getText();
    if(!text) return;
    try{
      await copyTextToClipboard(text);
      btn.innerHTML = ico('circle-check') + ' 복사됨';
      btn.classList.remove('copy-error');
      btn.classList.add('copied');
    }catch(e){
      btn.innerHTML = ico('triangle-alert') + ' 실패';
      btn.classList.remove('copied');
      btn.classList.add('copy-error');
    }
    clearTimeout(resetTimer);
    resetTimer = setTimeout(() => {
      btn.innerHTML = ico('copy') + ' 복사';
      btn.classList.remove('copied', 'copy-error');
    }, 1500);
  });
  return btn;
}

// getText()가 돌려주는 텍스트를 Danbooru 탭의 "자유 추가" 카테고리로 보내는
// 작은 버튼 — danbooruSendToMainPrompt(Danbooru → 메인 프롬프트)의 반대 방향.
// 실제 전송 로직(danbooruSendTextToExtra)은 이 함수보다 아래에서 정의되지만
// function 선언이라 호이스팅되어 여기서 바로 참조해도 문제없다(randomizeBtn 등과
// 같은 패턴).
function createSendToDanbooruButton(getText){
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'copy-btn';
  btn.title = 'Danbooru 탭의 "자유 추가"로 보내기 (쉼표 기준으로 태그를 나눠 추가·선택해요)';
  btn.innerHTML = ico('send') + ' Danbooru로';
  let resetTimer = null;
  btn.addEventListener('click', () => {
    const ok = danbooruSendTextToExtra(getText());
    btn.innerHTML = ok ? ico('circle-check') + ' 보냄' : ico('triangle-alert') + ' 내용 없음';
    btn.classList.remove('copied', 'copy-error');
    btn.classList.add(ok ? 'copied' : 'copy-error');
    clearTimeout(resetTimer);
    resetTimer = setTimeout(() => {
      btn.innerHTML = ico('send') + ' Danbooru로';
      btn.classList.remove('copied', 'copy-error');
    }, 1500);
  });
  return btn;
}

function buildSelectControl(opt){
  const select = document.createElement('select');
  select.className = 'option-input';
  select.dataset.name = opt.name;

  populateSelectOptions(select, choicesFor(opt), emptyTextFor(opt));
  if(!select.disabled && opt.default !== undefined && opt.default !== null && opt.default !== ''){
    select.value = opt.default;
  }
  return select;
}

// "메인 프롬프트" 옵션 전용 — 왼쪽에 사용자가 직접 쓰는 텍스트, 가운데 모드 토글
// (자연어/Danbooru) + "Prompt Enhance" 버튼, 오른쪽에 개선 결과(직접 수정 가능)를
// 두고, 실제로 큐에 보낼 값은 숨겨진 input(.option-input[data-name="main_prompt"])
// 에 모아둔다: 오른쪽(개선 결과)에 값이 있으면 그걸, 없으면 왼쪽(직접 입력) 값을
// 그대로 쓴다 — 두 텍스트 에어리어 모두 비어 있으면 hidden input도 빈 문자열이라
// 기존 로직대로 워크플로우의 프롬프트를 그대로 둔다.
function buildPromptEnhanceControl(opt){
  const wrap = document.createElement('div');
  wrap.className = 'prompt-enhance';
  let mode = 'natural';

  const rawCol = document.createElement('div');
  rawCol.className = 'prompt-enhance-col';
  const rawLabelRow = document.createElement('div');
  rawLabelRow.className = 'prompt-enhance-subtitle-row';
  const rawLabel = document.createElement('div');
  rawLabel.className = 'prompt-enhance-subtitle';
  rawLabel.textContent = '직접 입력';
  const rawTextarea = document.createElement('textarea');
  rawTextarea.className = 'enhance-textarea enhance-raw';
  rawTextarea.rows = 4;
  if(opt.placeholder) rawTextarea.placeholder = opt.placeholder;
  if(opt.default !== undefined && opt.default !== null) rawTextarea.value = opt.default;
  const rawActions = document.createElement('div');
  rawActions.className = 'prompt-enhance-actions';
  rawActions.appendChild(createCopyButton(() => rawTextarea.value.trim()));
  rawActions.appendChild(createSendToDanbooruButton(() => rawTextarea.value.trim()));
  rawLabelRow.appendChild(rawLabel);
  rawLabelRow.appendChild(rawActions);
  rawCol.appendChild(rawLabelRow);
  rawCol.appendChild(rawTextarea);

  // 시드 배치에서만: "시드마다 다른 프롬프트" 체크박스. seed_count 필드가 있는
  // 템플릿(지금은 seed_batch만)에서만 나타난다 — manifest에서 seed_count가
  // main_prompt보다 앞에 선언돼 있어서 이 시점엔 이미 DOM에 렌더링돼 있다.
  // 켜면 지금 Danbooru 탭의 잠금/선택 상태를 기준으로 시드 개수만큼 독립적으로
  // 뽑은 프롬프트를 숨겨진 필드(danbooru_seed_prompts)에 담아 큐에 추가할 때
  // 함께 보낸다 — seed_batch.py는 이 값이 있으면 시드마다 그중 하나씩 쓰고,
  // 없으면(체크 안 하면) 지금처럼 main_prompt 하나를 모든 시드에 그대로 쓴다.
  const seedCountInput = optionsFields.querySelector('[data-name="seed_count"]');
  if(seedCountInput){
    // SFW/NSFW 체크박스 두 개로 나눠서 켜되, 서로 배타적으로 하나만 켜지게 한다
    // (하나를 켜면 다른 하나는 자동으로 꺼짐) — "시드마다 다른 프롬프트 생성"이라는
    // 하나의 기능을 어느 모드로 켤지 고르는 것뿐이라, 둘 다 켜진 상태는 없다.
    function makePerSeedRow(mode, labelText){
      const row = document.createElement('label');
      row.className = 'danbooru-per-seed-row';
      row.title = `켜면 시드마다 독립적으로 ${mode === 'sfw' ? 'SFW(NSFW 제외)' : 'NSFW 포함'} 무작위 조합을 다시 뽑아요 — Danbooru 탭에서 고정해둔 항목은 모든 시드에서 그대로 유지됩니다.`;
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      const text = document.createElement('span');
      text.textContent = labelText;
      row.appendChild(checkbox);
      row.appendChild(text);
      return { row, checkbox };
    }
    const sfwOpt = makePerSeedRow('sfw', '시드마다 랜덤(SFW) (고정 항목은 유지)');
    const nsfwOpt = makePerSeedRow('nsfw', '시드마다 랜덤(NSFW) (고정 항목은 유지)');

    const seedPromptsHidden = document.createElement('input');
    seedPromptsHidden.type = 'hidden';
    seedPromptsHidden.className = 'option-input';
    seedPromptsHidden.dataset.name = 'danbooru_seed_prompts';

    let previousRawValue = null;
    let activeSeedMode = null; // null | 'sfw' | 'nsfw'

    function recomputeSeedPrompts(){
      if(!activeSeedMode){
        seedPromptsHidden.value = '';
        return;
      }
      danbooruSetNsfwMode(activeSeedMode);
      const n = Math.max(1, parseInt(seedCountInput.value, 10) || 1);
      const prompts = danbooruGenerateSeedPrompts(n);
      seedPromptsHidden.value = JSON.stringify(prompts);
      rawTextarea.value = prompts[0] || '';
      rawTextarea.dispatchEvent(new Event('input', { bubbles: true }));
    }

    function setActiveSeedMode(mode){
      const wasActive = activeSeedMode !== null;
      activeSeedMode = mode;
      sfwOpt.checkbox.checked = mode === 'sfw';
      nsfwOpt.checkbox.checked = mode === 'nsfw';
      if(mode){
        if(!wasActive) previousRawValue = rawTextarea.value;
        rawTextarea.disabled = true;
        recomputeSeedPrompts();
      }else{
        rawTextarea.disabled = false;
        seedPromptsHidden.value = '';
        if(previousRawValue !== null){
          rawTextarea.value = previousRawValue;
          rawTextarea.dispatchEvent(new Event('input', { bubbles: true }));
        }
      }
    }

    sfwOpt.checkbox.addEventListener('change', () => setActiveSeedMode(sfwOpt.checkbox.checked ? 'sfw' : null));
    nsfwOpt.checkbox.addEventListener('change', () => setActiveSeedMode(nsfwOpt.checkbox.checked ? 'nsfw' : null));
    // 체크된 상태로 시드 개수를 바꾸면 미리보기/개수를 그때그때 다시 맞춘다.
    seedCountInput.addEventListener('input', () => {
      if(activeSeedMode) recomputeSeedPrompts();
    });
    // 제출 직전에 한 번 더 — Danbooru 탭에서 잠금/선택을 바꾸고 바로 큐에
    // 추가해도 그 최신 상태로 다시 계산되게 한다.
    danbooruPreSubmitHooks.push(recomputeSeedPrompts);

    rawCol.appendChild(sfwOpt.row);
    rawCol.appendChild(nsfwOpt.row);
    rawCol.appendChild(seedPromptsHidden);
  }

  // 자주 쓰는 랜덤생성 버튼은 항상 보이게 두고(quickRow), 실제로는 잘 안 쓴다는
  // 피드백을 받은 "Prompt Enhance"(자연어/Danbooru 모드로 AI가 다시 써주는 것)만
  // <details>로 접어서 기본은 숨겨 둔다 — 필요할 때 펼쳐서 쓴다.
  const quickRow = document.createElement('div');
  quickRow.className = 'prompt-enhance-quick';

  // Danbooru 프롬프트 탭의 카테고리/규칙/랜덤 로직(danbooruRandomizeAll)을 그대로
  // 재사용해서 위 "직접 입력"을 채운다 — 그 탭에서 고정해둔 카테고리는
  // 손대지 않고 나머지만 새로 뽑고, 모순 태그를 피하는 검증도 그대로 적용된다
  // (danbooruRandomizeAll/danbooruDrawRange/danbooruSetNsfwMode 등은 이 함수보다
  // 아래에서 정의되지만 function 선언이라 호이스팅되어 여기서 바로 참조해도
  // 문제없다). SFW/NSFW 두 버튼으로 나눠서, 누를 때마다 전역 nsfwMode(Danbooru
  // 탭의 토글과 공유)를 그 값으로 맞추고 뽑는다 — Danbooru 탭에 돌아가도 마지막
  // 으로 누른 버튼의 모드가 토글에 그대로 반영돼 있다.
  function makeRandomizeBtn(mode, label){
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'load-btn';
    btn.innerHTML = ico('dices') + ' ' + label;
    btn.title = `Danbooru 프롬프트 탭에서 고정해둔 항목은 그대로 두고 나머지만 ${mode === 'sfw' ? 'SFW(NSFW 제외)' : 'NSFW 포함'}로 무작위로 뽑아 위에 채워요`;
    btn.addEventListener('click', () => {
      danbooruSetNsfwMode(mode);
      danbooruRandomizeAll();
      rawTextarea.value = danbooruAssemblePrompt();
      rawTextarea.dispatchEvent(new Event('input', { bubbles: true }));
    });
    return btn;
  }
  quickRow.appendChild(makeRandomizeBtn('sfw', '랜덤생성(SFW)'));
  quickRow.appendChild(makeRandomizeBtn('nsfw', '랜덤생성(NSFW)'));

  // Danbooru 탭 전체를 모달로 띄워 태그를 골라가며 프롬프트를 짜는 진입점 —
  // 태그식 프롬프트가 실제로 맞는 계열(Illustrious/Pony/NoobAI)을 골랐을 때만
  // 보인다(currentFamilySupportsDanbooru). 직접 템플릿 선택/워크플로우 직접
  // 업로드처럼 계열을 알 수 없는 경로에서는 안 보인다.
  if(currentFamilySupportsDanbooru()){
    const openDanbooruBtn = document.createElement('button');
    openDanbooruBtn.type = 'button';
    openDanbooruBtn.className = 'load-btn';
    openDanbooruBtn.innerHTML = ico('tag') + ' Danbooru로 프롬프트 만들기';
    openDanbooruBtn.title = 'Danbooru 태그를 골라가며 프롬프트를 짜요 — "프롬프트로 전송"을 누르면 여기 위에 반영돼요';
    openDanbooruBtn.addEventListener('click', openDanbooruModal);
    quickRow.appendChild(openDanbooruBtn);
  }

  const details = document.createElement('details');
  details.className = 'prompt-enhance-details';
  const summary = document.createElement('summary');
  summary.textContent = 'Prompt Enhance — AI로 다시 써보기 (선택)';
  details.appendChild(summary);

  const middle = document.createElement('div');
  middle.className = 'prompt-enhance-middle';

  const modeToggle = document.createElement('div');
  modeToggle.className = 'enhance-mode';
  const modeBtns = [
    { value: 'natural', label: '자연어' },
    { value: 'danbooru', label: 'Danbooru' },
  ].map(({ value, label }) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'enhance-mode-btn' + (value === mode ? ' active' : '');
    btn.textContent = label;
    btn.dataset.mode = value;
    btn.addEventListener('click', () => {
      mode = value;
      modeToggle.querySelectorAll('.enhance-mode-btn').forEach(b => b.classList.toggle('active', b.dataset.mode === mode));
    });
    modeToggle.appendChild(btn);
    return btn;
  });

  const enhanceBtn = document.createElement('button');
  enhanceBtn.type = 'button';
  enhanceBtn.className = 'enhance-btn';
  const enhanceSpinner = document.createElement('span');
  enhanceSpinner.className = 'enhance-spinner';
  enhanceSpinner.hidden = true;
  const enhanceLabel = document.createElement('span');
  enhanceLabel.innerHTML = ico('sparkles') + ' Enhance';
  enhanceBtn.appendChild(enhanceSpinner);
  enhanceBtn.appendChild(enhanceLabel);

  const status = document.createElement('div');
  status.className = 'enhance-status';
  middle.appendChild(modeToggle);
  middle.appendChild(enhanceBtn);
  middle.appendChild(status);

  const resultCol = document.createElement('div');
  resultCol.className = 'prompt-enhance-col';
  const resultLabelRow = document.createElement('div');
  resultLabelRow.className = 'prompt-enhance-subtitle-row';
  const resultLabel = document.createElement('div');
  resultLabel.className = 'prompt-enhance-subtitle';
  resultLabel.textContent = 'Prompt Enhance 결과';
  const resultTextarea = document.createElement('textarea');
  resultTextarea.className = 'enhance-textarea enhance-result';
  resultTextarea.rows = 4;
  resultTextarea.placeholder = 'Prompt Enhance를 누르면 여기에 개선된 프롬프트가 표시돼요 (직접 수정 가능)';
  const resultActions = document.createElement('div');
  resultActions.className = 'prompt-enhance-actions';
  resultActions.appendChild(createCopyButton(() => resultTextarea.value.trim()));
  resultActions.appendChild(createSendToDanbooruButton(() => resultTextarea.value.trim()));
  resultLabelRow.appendChild(resultLabel);
  resultLabelRow.appendChild(resultActions);
  resultCol.appendChild(resultLabelRow);
  resultCol.appendChild(resultTextarea);

  const hidden = document.createElement('input');
  hidden.type = 'hidden';
  hidden.className = 'option-input';
  hidden.dataset.name = opt.name;

  function sync(){
    const enhancedVal = resultTextarea.value.trim();
    hidden.value = enhancedVal ? resultTextarea.value : rawTextarea.value;
  }
  rawTextarea.addEventListener('input', sync);
  resultTextarea.addEventListener('input', sync);
  sync();

  enhanceBtn.addEventListener('click', async () => {
    const text = rawTextarea.value.trim();
    status.textContent = '';
    status.className = 'enhance-status';
    if(!text){
      status.textContent = '먼저 위에 프롬프트를 입력하세요.';
      status.classList.add('error');
      return;
    }
    enhanceBtn.disabled = true;
    modeBtns.forEach(b => { b.disabled = true; });
    enhanceSpinner.hidden = false;
    enhanceLabel.textContent = '개선하는 중…';
    status.textContent = 'ComfyUI로 요청 중…';
    try{
      const res = await fetch('/api/enhance-prompt', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: text, mode }),
      });
      const data = await res.json().catch(() => ({}));
      if(!res.ok){
        status.textContent = data.detail || '프롬프트 개선에 실패했어요.';
        status.classList.add('error');
        return;
      }
      resultTextarea.value = data.enhanced || '';
      sync();
      status.textContent = '완료';
      status.classList.add('success');
    }catch(e){
      status.textContent = '프롬프트 개선에 실패했어요.';
      status.classList.add('error');
    }finally{
      enhanceBtn.disabled = false;
      modeBtns.forEach(b => { b.disabled = false; });
      enhanceSpinner.hidden = true;
      enhanceLabel.innerHTML = ico('sparkles') + ' Enhance';
    }
  });

  details.appendChild(middle);
  details.appendChild(resultCol);

  wrap.appendChild(rawCol);
  wrap.appendChild(quickRow);
  wrap.appendChild(details);
  wrap.appendChild(hidden);
  return wrap;
}

// width/height 한 쌍을 위한 "화면비·해상도" 프리셋. 값 자체(w/h)는 화면비 계산에,
// 라벨은 드롭다운 표시에 쓴다.
const ASPECT_RATIOS = [
  { value: '1:1', label: '1:1 (Square)', w: 1, h: 1 },
  { value: '2:3', label: '2:3 (Portrait Photo)', w: 2, h: 3 },
  { value: '3:2', label: '3:2 (Photo)', w: 3, h: 2 },
  { value: '3:4', label: '3:4 (Portrait Standard)', w: 3, h: 4 },
  { value: '4:3', label: '4:3 (Standard)', w: 4, h: 3 },
  { value: '9:16', label: '9:16 (Portrait Widescreen)', w: 9, h: 16 },
  { value: '16:9', label: '16:9 (Widescreen)', w: 16, h: 9 },
  { value: '9:21', label: '9:21 (Portrait Ultrawide)', w: 9, h: 21 },
  { value: '21:9', label: '21:9 (Ultrawide)', w: 21, h: 9 },
  { value: '6:13', label: '6:13 (V-GalaxyS26+)', w: 6, h: 13 },
  { value: '13:6', label: '13:6 (H-GalaxyS26+)', w: 13, h: 6 },
];

// 1MP/2MP는 총 픽셀 수(면적) 기준, 2K/4K는 긴 변 픽셀 수 기준 — 두 단위 체계가
// 섞여 있는 건 실제로 "MP"와 "K" 표기가 관용적으로 그렇게 쓰이기 때문이다(1K=1024
// 기준으로 2K=2048, 4K=4096인 DCI/전통적 표기를 따름).
const RESOLUTION_TIERS = {
  '1MP': { kind: 'area', value: 1024 * 1024 },
  '2MP': { kind: 'area', value: 2 * 1024 * 1024 },
  '2K': { kind: 'edge', value: 2048 },
  '4K': { kind: 'edge', value: 4096 },
};

function roundToMultiple(n, step){
  return Math.max(step, Math.round(n / step) * step);
}

// 화면비 + 해상도 티어로부터 실제 픽셀 너비/높이를 계산한다. ComfyUI 계열
// VAE(다운샘플 8배수)와 호환되도록 8의 배수로 반올림한다.
function computeResolutionPx(ratioValue, tierKey){
  const ratio = ASPECT_RATIOS.find(r => r.value === ratioValue);
  const tier = RESOLUTION_TIERS[tierKey];
  if(!ratio || !tier) return null;
  const STEP = 8;
  let w, h;
  if(tier.kind === 'area'){
    const scale = Math.sqrt(tier.value / (ratio.w * ratio.h));
    w = ratio.w * scale;
    h = ratio.h * scale;
  } else {
    if(ratio.w >= ratio.h){ w = tier.value; h = tier.value * ratio.h / ratio.w; }
    else { h = tier.value; w = tier.value * ratio.w / ratio.h; }
  }
  return { width: roundToMultiple(w, STEP), height: roundToMultiple(h, STEP) };
}

// "width"+"height" 옵션 한 쌍을 위한 전용 컨트롤 — "직접 입력"과 "화면비·해상도"
// 두 방식을 모드 토글로 오간다. 실제로 값이 담기는 곳은 항상 이 두 number
// input(.option-input[data-name="width"/"height"])이라서, "화면비·해상도" 모드는
// 그 값을 계산해서 채워주는 보조 입력 방식일 뿐이다 — 제출/설정 불러오기 등
// 다른 로직은 이 컨트롤의 존재를 몰라도 기존 그대로 동작한다.
function buildResolutionControl(widthOpt, heightOpt){
  const wrap = document.createElement('div');
  wrap.className = 'resolution-control';

  const modeToggle = document.createElement('div');
  modeToggle.className = 'enhance-mode';
  const modeBtns = [
    { value: 'preset', label: '화면비·해상도' },
    { value: 'manual', label: '직접 입력' },
  ].map(({ value, label }) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'enhance-mode-btn' + (value === 'preset' ? ' active' : '');
    btn.textContent = label;
    btn.dataset.mode = value;
    modeToggle.appendChild(btn);
    return btn;
  });

  function makeField(labelText, input){
    const field = document.createElement('div');
    field.className = 'field';
    const label = document.createElement('label');
    label.className = 'field-label';
    label.textContent = labelText;
    field.appendChild(label);
    field.appendChild(input);
    return field;
  }

  const manualPane = document.createElement('div');
  manualPane.className = 'resolution-manual';
  const widthInput = document.createElement('input');
  widthInput.type = 'number';
  widthInput.className = 'option-input';
  widthInput.dataset.name = widthOpt.name;
  if(widthOpt.placeholder) widthInput.placeholder = widthOpt.placeholder;
  const heightInput = document.createElement('input');
  heightInput.type = 'number';
  heightInput.className = 'option-input';
  heightInput.dataset.name = heightOpt.name;
  if(heightOpt.placeholder) heightInput.placeholder = heightOpt.placeholder;
  manualPane.appendChild(makeField(widthOpt.label, widthInput));
  manualPane.appendChild(makeField(heightOpt.label, heightInput));

  const presetPane = document.createElement('div');
  presetPane.className = 'resolution-preset';
  manualPane.hidden = true;

  const aspectSelect = document.createElement('select');
  aspectSelect.className = 'option-input';
  for(const r of ASPECT_RATIOS){
    const o = document.createElement('option');
    o.value = r.value;
    o.textContent = r.label;
    aspectSelect.appendChild(o);
  }
  const tierSelect = document.createElement('select');
  tierSelect.className = 'option-input';
  for(const key of Object.keys(RESOLUTION_TIERS)){
    const o = document.createElement('option');
    o.value = key;
    o.textContent = key;
    tierSelect.appendChild(o);
  }
  const preview = document.createElement('div');
  preview.className = 'resolution-preview';

  presetPane.appendChild(makeField('화면비', aspectSelect));
  presetPane.appendChild(makeField('해상도', tierSelect));
  presetPane.appendChild(preview);

  function applyPreset(){
    const result = computeResolutionPx(aspectSelect.value, tierSelect.value);
    if(!result) return;
    widthInput.value = result.width;
    heightInput.value = result.height;
    preview.textContent = `→ ${result.width} × ${result.height} px`;
  }
  aspectSelect.addEventListener('change', applyPreset);
  tierSelect.addEventListener('change', applyPreset);

  modeBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      modeBtns.forEach(b => b.classList.toggle('active', b === btn));
      const isPreset = btn.dataset.mode === 'preset';
      manualPane.hidden = isPreset;
      presetPane.hidden = !isPreset;
      if(isPreset) applyPreset();
    });
  });

  wrap.appendChild(modeToggle);
  wrap.appendChild(manualPane);
  wrap.appendChild(presetPane);
  applyPreset(); // 기본 활성 모드가 "화면비·해상도"라, 처음부터 계산값/미리보기를 채워둔다.
  return wrap;
}

// ComfyUI에 실제로 설치된 모델 중에서 고르는 드롭다운(체크포인트/LoRA 등).
// 선택지는 manifest가 아니라 서버가 ComfyUI에서 받아온 목록(comfyObjectInfoCache)에서
// 온다 — renderOptionFields가 이 타입의 옵션이 있으면 그리기 전에 미리 받아두므로
// 여기서는 캐시를 그대로 읽어 쓴다. 첫 항목은 항상 "그대로" — 비워두면 워크플로우에
// 이미 들어있는 모델을 안 건드린다는 뜻이라, 지금까지처럼 업로드한 워크플로우만으로
// 돌리는 방식이 기본값 그대로 유지된다.
// 고를 수 있는 모델 이름 — 파드에 연결돼 있으면 그 파드에 설치된 것, 아니면(파드를 안 정했거나 꺼져 있으면) 모델 등록부에
// 적힌 것. 등록부 이름을 골라 둬도 그 파일을 가진 파드가 살아날 때 스케줄러가 배정하므로 작업은 구상할 수 있다.
function modelChoices(kind){
  const info = comfyObjectInfoCache;
  if(info && info.connected) return info.models[kind] || [];
  return (info && info.catalog && info.catalog[kind]) || [];
}

function fillComfyModelSelect(select, opt){
  const choices = [{ value: '', label: '워크플로우 값 그대로', disabled: false }];
  for(const name of modelChoices(opt.model_kind)){
    choices.push({ value: name, label: name, disabled: false });
  }
  populateSelectOptions(select, choices, '(선택지 없음)');
  const info = comfyObjectInfoCache;
  select.title = (info && info.connected) ? ''
    : '지금은 워커에 연결하지 않아, 모델 등록부에 적힌 이름을 보여줘요 — 그 모델을 갖춘 워커가 살아나면 작업이 시작돼요.';
}

function buildComfyModelControl(opt){
  const select = document.createElement('select');
  select.className = 'option-input';
  select.dataset.name = opt.name;
  fillComfyModelSelect(select, opt);
  if(opt.default) select.value = opt.default;
  return select;
}

// 실행 파드를 바꿨을 때 이미 그려진 모델 드롭다운을 그 파드 기준으로 다시 채운다(고른 값은 그대로 둔다).
function refreshComfyModelSelects(){
  const template = templatesById[templateSelect.value];
  for(const opt of (template ? template.options || [] : [])){
    if(opt.type !== 'comfy_model') continue;
    const select = optionsFields.querySelector(`[data-name="${opt.name}"]`);
    if(!select) continue;
    const previous = select.value;
    fillComfyModelSelect(select, opt);
    const values = Array.from(select.options).map(o => o.value);
    if(previous && !values.includes(previous)){
      const extra = document.createElement('option');
      extra.value = previous; extra.textContent = previous;
      select.appendChild(extra);
    }
    select.value = previous;
  }
}

// "입력 이미지" 선택 — 드롭다운(기존) 옆에 "⬆ 업로드"/"🖼 갤러리에서 선택" 버튼을
// 붙여서, img2img류뿐 아니라 영상 생성(WAN2.2 i2v/flf2v)의 시작/끝 이미지 선택도
// 같은 컨트롤로 처리한다. 어느 경로로 채워지든 결국 입력 이미지 풀(input_assets.py)에
// 파일 하나가 생기고 그 파일명이 select.value가 되므로, 제출 로직(.option-input
// 일괄 수집)은 손댈 필요가 없다.
function buildInputImageControl(opt){
  const wrap = document.createElement('div');
  wrap.className = 'input-image-control';

  const select = document.createElement('select');
  select.className = 'option-input';
  select.dataset.name = opt.name;
  populateSelectOptions(select, inputImageChoices(), '(입력 이미지 없음)');
  if(!select.disabled && opt.default) select.value = opt.default;

  const uploadBtn = document.createElement('button');
  uploadBtn.type = 'button';
  uploadBtn.className = 'load-btn';
  uploadBtn.innerHTML = ico('upload') + ' 업로드';
  uploadBtn.title = '내 컴퓨터에서 이미지 업로드';

  const fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.accept = '.png,.jpg,.jpeg,.webp';
  fileInput.style.display = 'none';
  uploadBtn.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', async () => {
    const file = fileInput.files[0];
    fileInput.value = '';
    if(!file) return;
    uploadBtn.disabled = true;
    const original = uploadBtn.innerHTML;
    uploadBtn.innerHTML = ico('loader-circle', true) + ' 업로드 중…';
    try{
      const form = new FormData();
      form.append('image', file);
      const res = await fetch('/api/input-images', { method: 'POST', body: form });
      const data = await res.json().catch(() => ({}));
      if(!res.ok) throw new Error(data.detail || '업로드에 실패했어요.');
      await fetchInputImages(true);
      populateSelectOptions(select, inputImageChoices(), '(입력 이미지 없음)');
      select.value = data.name;
    }catch(e){
      alert(e.message || '업로드에 실패했어요.');
    }finally{
      uploadBtn.disabled = false;
      uploadBtn.innerHTML = original;
    }
  });

  const galleryBtn = document.createElement('button');
  galleryBtn.type = 'button';
  galleryBtn.className = 'load-btn';
  galleryBtn.innerHTML = ico('images') + ' 갤러리에서 선택';
  galleryBtn.title = '결과 이미지 갤러리에서 사본으로 가져오기';
  galleryBtn.addEventListener('click', () => openInputImageGalleryPicker(async (name) => {
    const res = await fetch('/api/input-images/import-from-output', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ names: [name] }),
    });
    const data = await res.json().catch(() => ({}));
    if(!res.ok || !(data.added || []).length){
      throw new Error(data.detail || (data.skipped && data.skipped[0] && data.skipped[0].reason) || '가져오지 못했어요.');
    }
    await fetchInputImages(true);
    populateSelectOptions(select, inputImageChoices(), '(입력 이미지 없음)');
    select.value = data.added[0];
  }));

  wrap.appendChild(select);
  wrap.appendChild(uploadBtn);
  wrap.appendChild(galleryBtn);
  wrap.appendChild(fileInput);
  return wrap;
}

function inputImageChoices(){
  return (inputImagesCache || []).map(img => ({ value: img.name, label: img.name, disabled: false }));
}

