// ---- 프로젝트 보드 — 생성 카드(kind 'gen') ----
// 보드 프리셋을 꺼낸 카드. 카드가 프리셋 내용(템플릿·워크플로우·고정 값)과 입구(slots)·꺼낸
// 옵션(fields) 정의, 꺼낸 옵션의 지금 값(values)을 전부 자기 data에 들고 있어서, 프리셋을 지우거나
// 고쳐도 이미 놓은 카드는 그대로다(서버 _board_gen_data_from_preset). 입구는 템플릿의 입력 이미지
// 칸마다 하나 — 이미지 카드에서 선을 이어 채운다. 꺼낸 옵션은 카드 안에서 바로 고친다.
function boardGenFieldHtml(nodeId, f, value){
  const attrs = `class="board-gen-input" data-gen-node="${nodeId}" data-gen-field="${escapeHtml(f.name)}"`;
  const ph = f.placeholder ? ` placeholder="${escapeHtml(f.placeholder)}"` : '';
  let control;
  if(f.type === 'textarea'){
    control = `<textarea ${attrs} rows="3"${ph}>${escapeHtml(value)}</textarea>`;
  }else if(f.type === 'select'){
    const choices = (f.choices || []).map(c => typeof c === 'object' && c !== null ? c : { value: c, label: c });
    control = `<select ${attrs}>` + choices.map(c => `<option value="${escapeHtml(c.value)}"${String(c.value) === String(value) ? ' selected' : ''}>${escapeHtml(c.label ?? c.value)}</option>`).join('') + '</select>';
  }else{
    control = `<input type="number" step="any" ${attrs} value="${escapeHtml(value)}"${ph}>`;
  }
  return `<label class="board-gen-field"><span class="board-gen-field-label">${escapeHtml(f.label)}</span>${control}</label>`;
}

function boardGenCardHtml(node, style, delBtn){
  const d = node.data || {};
  const slots = (d.slots || []).map(sl => `<div class="board-gen-slot" data-gen-slot="${escapeHtml(sl.name)}">`
    + `<span class="board-gen-port" title="이미지 카드를 이어 채우는 입구"></span>`
    + `<span class="board-gen-slot-label">${escapeHtml(sl.label)}${sl.required ? '' : ' <em>(선택)</em>'}</span>`
    + `<span class="board-gen-slot-state">비어 있음</span></div>`).join('');
  const fields = (d.fields || []).map(f => boardGenFieldHtml(node.id, f, (d.values || {})[f.name] ?? '')).join('');
  return `<div class="board-node board-node-gen" data-node-id="${node.id}" style="${style}">`
    + `<div class="board-gen-head">${ico('wand-sparkles')}<span class="board-gen-name">${escapeHtml(d.preset_name || '생성')}</span>`
    + `<span class="board-gen-tpl">${escapeHtml(d.template_label || d.template_id || '')}</span></div>`
    + (slots ? `<div class="board-gen-slots">${slots}</div>` : '')
    + `<div class="board-gen-fields">${fields}</div>`
    + `<div class="board-gen-foot"><div class="board-gen-status">${boardGenStatusHtml(boardJobInfo.get(node.id), node)}</div>`
    + `<button type="button" class="board-gen-btn" data-gen-detail="${node.id}" title="새 작업 창을 이 카드 값으로 채워 열기(복잡한 설정은 거기서)">자세히</button>`
    + `<button type="button" class="board-gen-btn primary" data-gen-run="${node.id}">${ico('play')} 실행</button></div>`
    + `${delBtn}</div>`;
}

// 카드 아래 줄의 마지막 실행 상태 — 작업 카드와 같은 정보(GET .../board/jobs)를 짧게.
function boardGenStatusHtml(info, node){
  const runs = ((node && node.data && node.data.runs) || []).length;
  if(!runs) return '<span>아직 실행하지 않았어요</span>';
  if(!info) return '<span>불러오는 중…</span>';
  if(info.missing) return `<span>마지막 작업이 지워졌어요 · ${runs}회 실행</span>`;
  const pr = info.progress;
  const progress = pr && pr.total ? `<span>${pr.done}/${pr.total}</span>` : '';
  // 대기 사유는 길 수 있어 맨 뒤에 두고 말줄임(전체 문구는 마우스를 올리면) — 실행 횟수가 잘리지 않게.
  const waiting = info.status === 'queued' && info.waiting_reason
    ? `<span class="board-gen-reason" title="${escapeHtml(info.waiting_reason)}">${escapeHtml(info.waiting_reason)}</span>` : '';
  const spread = ((node && node.data && node.data.runs) || []).reduce((n, r) => n + ((r.spread || []).length), 0);
  return `<span class="badge ${escapeHtml(info.status || '')}"><span class="bdot"></span>${STATUS_LABEL[info.status] || escapeHtml(info.status || '')}</span>`
    + progress + `<span>${info.run_count || runs}회${spread ? ` · 결과 ${spread}` : ''}</span>` + waiting;
}

// 입구 줄 — 이어진 이미지가 있으면 작은 미리보기(NSFW 설정 따름)와 "연결됨", 없으면 "비어 있음".
// 필수 입구가 비면 실행 버튼을 막고 이유를 버튼 설명에 적는다. 선을 다시 그릴 때마다 부른다.
function paintBoardGenSlots(){
  for(const el of document.querySelectorAll('#board-world .board-node-gen')){
    const node = boardNodes.find(n => n.id === Number(el.dataset.nodeId));
    if(!node || !node.data) continue;
    const empty = [];
    for(const sl of node.data.slots || []){
      const row = el.querySelector(`.board-gen-slot[data-gen-slot="${CSS.escape(sl.name)}"]`);
      if(!row) continue;
      const edge = boardEdges.find(e => e.to_node_id === node.id && e.to_slot === sl.name);
      const src = edge && boardNodes.find(n => n.id === edge.from_node_id);
      let html = '비어 있음';
      if(src){
        const hidden = src.nsfw && nsfwMode !== 'show';
        html = hidden || src.kind !== 'image' ? '연결됨'
          : `<img src="${window.__nightshiftMediaUrl(`/api/output-images/${encodeURIComponent(src.asset_path)}/thumbnail?size=60&fit=cover`)}" alt=""> 연결됨`;
      }else if(sl.required){
        empty.push(sl.label);
      }
      row.classList.toggle('filled', !!src);
      const state = row.querySelector('.board-gen-slot-state');
      if(state && state._html !== html){ state.innerHTML = html; state._html = html; }
    }
    const runBtn = el.querySelector('[data-gen-run]');
    if(runBtn){
      // 마지막 실행이 아직 대기·실행 중이면 버튼을 막고 일시정지 아이콘으로 — 같은 작업을 겹쳐 쏘지 않게.
      const info = boardJobInfo.get(node.id);
      const busy = !!(info && !info.missing && info.active);
      runBtn.disabled = busy || empty.length > 0;
      runBtn.classList.toggle('busy', busy);
      runBtn.title = busy ? '이전 실행이 끝나면 다시 실행할 수 있어요'
        : empty.length ? `이미지 카드를 이어 주세요: ${empty.join(', ')}` : '이 카드 설정으로 작업을 만들어 바로 대기 큐에 넣어요';
      const html = busy ? `${ico('pause')} ${info.status === 'running' ? '실행 중' : '대기 중'}` : `${ico('play')} 실행`;
      if(runBtn._html !== html){ runBtn.innerHTML = html; runBtn._html = html; }
    }
  }
}

// 실행 — 쓰던 값을 먼저 저장하고 서버가 입구 이미지를 가져와 작업을 만든다(파드는 자동).
async function runBoardGen(nodeId){
  const projectId = currentProjectId;
  if(typeof projectId !== 'number') return;
  const btn = document.querySelector(`#board-world [data-gen-run="${nodeId}"]`);
  if(btn) btn.disabled = true;
  await flushAllBoardGenSaves(false);
  let res = null, data = {};
  try{
    res = await fetch(`/api/projects/${projectId}/board/nodes/${nodeId}/run`, { method: 'POST' });
    data = await res.json().catch(() => ({}));
  }catch(e){ /* 아래에서 실패로 처리 */ }
  if(projectId !== currentProjectId) return;
  if(!res || !res.ok){
    flashNotice(data.detail || '실행하지 못했어요. 잠시 뒤 다시 해 주세요.');
    paintBoardGenSlots();
    return;
  }
  const node = boardNodes.find(n => n.id === nodeId);
  if(node && data.node) node.data = data.node.data;
  boardJobInfo.set(nodeId, { job_id: data.job.id, status: data.job.status, active: true,
                             run_count: (node && node.data && node.data.runs || []).length });
  paintBoardJobCards();
  paintBoardGenSlots();
  flashNotice('실행했어요 — 필요한 모델을 갖춘 파드가 잡히면 시작해요.');
  fetchBoardJobs();
}

// 결과 펼치기 — 서버가 아직 안 펼친 결과를 이미지/영상 카드로 만들어 생성 카드 오른쪽에 놓고 선으로
// 잇는다(두 번 펼치지 않게 서버가 기록). 자동으로 하는 일이라 되돌리기 기록에는 남기지 않는다 —
// 펼친 카드는 평소처럼 지울 수 있고, 지워도 다시 생기지 않는다.
const boardSpreading = new Set();
async function spreadBoardGen(nodeId){
  const projectId = currentProjectId;
  if(typeof projectId !== 'number' || boardSpreading.has(nodeId)) return;
  boardSpreading.add(nodeId);
  try{
    const res = await boardApi(projectId, 'POST', `/nodes/${nodeId}/spread`);
    if(!res || projectId !== currentProjectId) return;
    const data = await res.json();
    const fresh = (data.nodes || []).filter(n => !boardNodes.some(b => b.id === n.id));
    boardNodes.push(...fresh);
    appendBoardNodeEls(fresh);
    for(const e of data.edges || []) if(!boardEdges.some(b => b.id === e.id)) boardEdges.push(e);
    const node = boardNodes.find(n => n.id === nodeId);
    if(node && data.node) node.data = data.node.data;
    if(fresh.length){
      renderBoardEdges();
      paintBoardJobCards();
      flashNotice(`생성 결과 ${fresh.length}개를 보드에 펼쳤어요.`);
    }
  }finally{
    boardSpreading.delete(nodeId);
  }
}

// 자세히 — 이 카드 값(입구 이미지 포함)으로 새 작업 창을 채워 연다. 복잡한 설정은 그 창에서.
async function openBoardGenDetail(nodeId){
  const projectId = currentProjectId;
  if(typeof projectId !== 'number') return;
  await flushAllBoardGenSaves(false);
  let res = null, data = {};
  try{
    res = await fetch(`/api/projects/${projectId}/board/nodes/${nodeId}/prepare`, { method: 'POST' });
    data = await res.json().catch(() => ({}));
  }catch(e){ /* 아래에서 실패로 처리 */ }
  if(!res || !res.ok){ flashNotice(data.detail || '카드 설정을 불러오지 못했어요.'); return; }
  // 입구 이미지는 방금 입력 이미지 풀로 들어왔을 수 있다 — 폼의 선택 목록에 있어야 값이 들어가므로 먼저 새로 받는다
  // (갤러리 "영상 만들기"와 같은 순서).
  await fetchInputImages(true);
  showTab('jobs', { projectId });
  await loadJobSettings({ id: null, template_id: data.template_id, options: data.options }, data);
  // 템플릿·워크플로우·값이 다 채워졌으니 바로 "2. 세부 설정"을 보여 준다. goToNewJobDetails는 마법사
  // 설정을 다시 적용할 수 있어(카드 워크플로우를 덮을 수 있음) 쓰지 않고 탭만 바꾼다.
  if(newJobReadyToAdvance()){
    newJobActiveTab = 'details';
    renderNewJobTabs();
  }
}

// 꺼낸 옵션 값 저장 — 입력하다 멈추면 잠깐 뒤에, 칸을 벗어나면(change) 바로. 칸 하나를 고친 게
// 되돌리기 한 칸이 된다(글자마다가 아니라). 대기 중인 저장은 카드·옵션마다 하나씩.
const boardGenPending = new Map();   // "노드id:옵션이름" -> { projectId, nodeId, name, value, timer }

function scheduleBoardGenSave(nodeId, name, value){
  const key = `${nodeId}:${name}`;
  const prev = boardGenPending.get(key);
  if(prev) clearTimeout(prev.timer);
  boardGenPending.set(key, { projectId: currentProjectId, nodeId, name, value,
    timer: setTimeout(() => flushBoardGenSave(key), BOARD_TEXT_SAVE_DELAY_MS) });
}

async function flushBoardGenSave(key, keepalive){
  const p = boardGenPending.get(key);
  if(!p) return;
  clearTimeout(p.timer);
  boardGenPending.delete(key);
  if(typeof p.projectId !== 'number') return;
  if(!await boardApi(p.projectId, 'PATCH', `/nodes/${p.nodeId}`, { values: { [p.name]: p.value } }, keepalive)){
    flashNotice('카드 옵션을 저장하지 못했어요. 연결을 확인한 뒤 다시 고쳐 주세요.');
  }
}

function flushAllBoardGenSaves(keepalive){
  return Promise.all([...boardGenPending.keys()].map(key => flushBoardGenSave(key, keepalive)));
}

function setBoardGenValueLocal(nodeId, name, value){
  const node = boardNodes.find(n => n.id === nodeId);
  if(node && node.data){ node.data.values = { ...(node.data.values || {}), [name]: value }; }
  const el = document.querySelector(`#board-world .board-gen-input[data-gen-node="${nodeId}"][data-gen-field="${CSS.escape(name)}"]`);
  if(el && el.value !== value) el.value = value;
}

function boardNodeHtml(node){
  const style = `left:${node.x}px; top:${node.y}px; width:${node.width}px; height:${node.height}px;`;
  const delBtn = `<button type="button" class="board-node-del" data-board-del="${node.id}" title="삭제"><svg class="ico"><use href="#i-trash-2"/></svg></button>`
    + `<span class="board-node-handle" title="끌어서 다른 카드와 잇기"></span>`;
  if(node.kind === 'gen') return boardGenCardHtml(node, style, delBtn);
  if(node.kind === 'frame'){
    return `<div class="board-node board-frame" data-node-id="${node.id}" style="${style}">`
      + `<div class="board-frame-bar" title="끌어서 묶음째 옮기기 · 고른 상태에서 한 번 더 누르면 제목 고치기">`
      + `<input class="board-node-textarea board-frame-title" data-board-text="${node.id}" value="${escapeHtml(node.text)}" maxlength="80" placeholder="묶음" spellcheck="false"></div>`
      + `<button type="button" class="board-node-del" data-board-del="${node.id}" title="묶음 틀 지우기(안의 카드는 남아요)"><svg class="ico"><use href="#i-trash-2"/></svg></button>`
      + `<span class="board-frame-resize" title="끌어서 크기 바꾸기"></span></div>`;
  }
  if(node.kind === 'job'){
    return `<div class="board-node board-node-job" data-node-id="${node.id}" style="${style}" title="눌러서 작업 자세히 보기">`
      + `<div class="board-job-body">${boardJobBodyHtml(boardJobInfo.get(node.id))}</div>${delBtn}</div>`;
  }
  if((node.kind === 'image' || node.kind === 'video') && node.nsfw && nsfwMode !== 'show'){
    const hidden = nsfwMode === 'hide';
    const title = hidden ? 'NSFW 안 보기 중이라 가렸어요' : 'NSFW 블러 중이라 가렸어요';
    return `<div class="board-node board-node-${node.kind} board-node-nsfw" data-node-id="${node.id}" style="${style}" title="${title} — 헤더의 NSFW 버튼으로 바꿀 수 있어요">`
      + `<div class="board-nsfw-cover">${ico(hidden ? 'ban' : 'eye-off')}<span>NSFW</span></div>${delBtn}<span class="board-node-resize" title="끌어서 크기 바꾸기"></span></div>`;
  }
  if(node.kind === 'image'){
    const url = window.__nightshiftMediaUrl(`/api/output-images/${encodeURIComponent(node.asset_path)}/thumbnail?size=400&fit=cover`);
    return `<div class="board-node board-node-image" data-node-id="${node.id}" style="${style}"><img src="${url}" loading="lazy" alt="">${delBtn}<span class="board-node-resize" title="끌어서 크기 바꾸기"></span></div>`;
  }
  if(node.kind === 'video'){
    const url = window.__nightshiftMediaUrl(`/api/output-videos/${encodeURIComponent(node.asset_path)}`);
    return `<div class="board-node board-node-video" data-node-id="${node.id}" style="${style}"><video muted preload="metadata" data-src="${url}#t=0.1"></video>${delBtn}<span class="board-node-resize" title="끌어서 크기 바꾸기"></span></div>`;
  }
  return `<div class="board-node board-node-text" data-node-id="${node.id}" style="${style}">`
    + `<div class="board-node-grip" title="끌어서 옮기기"></div><textarea class="board-node-textarea" data-board-text="${node.id}" placeholder="메모나 프롬프트를 적으세요">${escapeHtml(node.text)}</textarea>${delBtn}<span class="board-node-resize" title="끌어서 크기 바꾸기"></span></div>`;
}

// 선은 두 카드의 서로 마주 보는 변 가운데를 잇는 3차 베지어 곡선이다. 가로로 더
// 떨어져 있으면 좌우 변, 세로로 더 떨어져 있으면 위아래 변을 쓰고, 제어점은 그 변에서
// 바깥쪽으로 뻗어 나가게 둬서 선이 카드를 파고들지 않고 부드럽게 휘게 한다.
function boardEdgeCurve(a, b){
  const ax = a.x + a.width / 2, ay = a.y + a.height / 2;
  const bx = b.x + b.width / 2, by = b.y + b.height / 2;
  const dx = bx - ax, dy = by - ay;
  let p0, p3, n0, n3;
  if(Math.abs(dx) >= Math.abs(dy)){
    const s = dx >= 0 ? 1 : -1;
    p0 = { x: ax + s * a.width / 2, y: ay };
    p3 = { x: bx - s * b.width / 2, y: by };
    n0 = { x: s, y: 0 }; n3 = { x: -s, y: 0 };
  }else{
    const s = dy >= 0 ? 1 : -1;
    p0 = { x: ax, y: ay + s * a.height / 2 };
    p3 = { x: bx, y: by - s * b.height / 2 };
    n0 = { x: 0, y: s }; n3 = { x: 0, y: -s };
  }
  const k = Math.max(30, Math.min(160, Math.hypot(p3.x - p0.x, p3.y - p0.y) / 2));
  const p1 = { x: p0.x + n0.x * k, y: p0.y + n0.y * k };
  const p2 = { x: p3.x + n3.x * k, y: p3.y + n3.y * k };
  return {
    d: `M${p0.x},${p0.y} C${p1.x},${p1.y} ${p2.x},${p2.y} ${p3.x},${p3.y}`,
    // 베지어의 t=0.5 지점 — "끊기" 버튼을 여기 띄운다.
    mid: { x: (p0.x + 3 * p1.x + 3 * p2.x + p3.x) / 8, y: (p0.y + 3 * p1.y + 3 * p2.y + p3.y) / 8 },
  };
}

function boardEdgeCurveById(edge){
  const a = boardNodes.find(n => n.id === edge.from_node_id);
  const b = boardNodes.find(n => n.id === edge.to_node_id);
  if(!a || !b) return null;
  if(edge.to_slot && b.kind === 'gen'){
    const i = ((b.data && b.data.slots) || []).findIndex(sl => sl.name === edge.to_slot);
    if(i >= 0) return boardEdgeCurveToPort(a, { x: b.x - 1, y: b.y + BOARD_GEN_PORT_Y0 + BOARD_GEN_SLOT_H * i });
  }
  return boardEdgeCurve(a, b);
}

// 생성 카드의 입구 점 위치(카드 기준) — CSS .board-gen-head(40) + .board-gen-slots 위 여백(6) + 줄 가운데(15).
const BOARD_GEN_PORT_Y0 = 61, BOARD_GEN_SLOT_H = 30;

// 입구로 들어가는 선 — 입구 점에는 늘 왼쪽에서 가로로 들어오게 하고, 출발 쪽은 입구를 향한 변에서 나간다.
function boardEdgeCurveToPort(a, p3){
  const ax = a.x + a.width / 2, ay = a.y + a.height / 2;
  let p0, n0;
  if(a.x + a.width <= p3.x){ p0 = { x: a.x + a.width, y: ay }; n0 = { x: 1, y: 0 }; }
  else if(p3.y > a.y + a.height){ p0 = { x: ax, y: a.y + a.height }; n0 = { x: 0, y: 1 }; }
  else if(p3.y < a.y){ p0 = { x: ax, y: a.y }; n0 = { x: 0, y: -1 }; }
  else{ p0 = { x: a.x, y: ay }; n0 = { x: -1, y: 0 }; }
  const k = Math.max(40, Math.min(160, Math.hypot(p3.x - p0.x, p3.y - p0.y) / 2));
  const p1 = { x: p0.x + n0.x * k, y: p0.y + n0.y * k };
  const p2 = { x: p3.x - k, y: p3.y };
  return {
    d: `M${p0.x},${p0.y} C${p1.x},${p1.y} ${p2.x},${p2.y} ${p3.x},${p3.y}`,
    mid: { x: (p0.x + 3 * p1.x + 3 * p2.x + p3.x) / 8, y: (p0.y + 3 * p1.y + 3 * p2.y + p3.y) / 8 },
  };
}

function boardEdgesSvg(){
  const paths = boardEdges.map(edge => {
    const c = boardEdgeCurveById(edge);
    if(!c) return '';
    const sel = edge.id === boardSelectedEdgeId;
    return `<path class="board-edge-hit" data-edge-id="${edge.id}" d="${c.d}"/>`
      + `<path class="board-edge-line${sel ? ' selected' : ''}" data-edge-id="${edge.id}" d="${c.d}" marker-end="url(#${sel ? 'board-arrow-sel' : 'board-arrow'})"/>`;
  }).join('');
  const marker = (id) => `<marker id="${id}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z"/></marker>`;
  return `<svg class="board-edges" id="board-edges"><defs>${marker('board-arrow')}${marker('board-arrow-sel')}</defs>${paths}`
    + `<path class="board-edge-temp" id="board-edge-temp" d=""/></svg>`;
}

function boardEdgeDelHtml(){
  if(boardSelectedEdgeId === null) return '';
  const edge = boardEdges.find(e => e.id === boardSelectedEdgeId);
  const c = edge && boardEdgeCurveById(edge);
  if(!c) return '';
  return `<button type="button" class="board-edge-del" id="board-edge-del" data-edge-del="${edge.id}" title="연결 끊기" `
    + `style="left:${c.mid.x}px; top:${c.mid.y}px; transform:scale(${1 / boardState.scale});"><svg class="ico"><use href="#i-x"/></svg></button>`;
}

// 선 층(svg)을 카드보다 먼저 넣어 카드 아래에 깔리게 한다.
function renderBoard(){
  const world = document.getElementById('board-world');
  if(!world) return;
  if(boardMediaObserver) boardMediaObserver.disconnect();   // 통째로 갈아 끼우므로 옛 카드 감시는 버린다
  // 묶음 틀은 선·카드보다 먼저 넣어 맨 뒤에 깔고, 큰 틀부터 넣어 안쪽(작은) 틀의 제목 띠가 위에 오게 한다.
  const frames = boardNodes.filter(n => n.kind === 'frame').sort((a, b) => b.width * b.height - a.width * a.height);
  world.innerHTML = frames.map(boardNodeHtml).join('') + boardEdgesSvg()
    + boardNodes.filter(n => n.kind !== 'frame').map(boardNodeHtml).join('') + boardEdgeDelHtml();
  boardObserveMedia(world);
  paintBoardSelection();
  paintBoardGenSlots();
}

// 보드 전체를 다시 그리지 않고 카드 몇 장만 붙인다 — 다시 그리면 영상 카드가 전부
// 다시 로드되며 깜빡인다.
function appendBoardNodeEls(nodes){
  const world = document.getElementById('board-world');
  if(!document.getElementById('board-edges')){ renderBoard(); return; }
  world.insertAdjacentHTML('afterbegin', nodes.filter(n => n.kind === 'frame').map(boardNodeHtml).join(''));   // 틀은 맨 뒤
  world.insertAdjacentHTML('beforeend', nodes.filter(n => n.kind !== 'frame').map(boardNodeHtml).join(''));
  const edgeDel = document.getElementById('board-edge-del');
  if(edgeDel) world.appendChild(edgeDel);   // 끊기 버튼은 늘 맨 위
  boardObserveMedia(world);
  paintBoardSelection();
  paintBoardGenSlots();
}

// 카드 몇 장을 화면에서만 뺀다(서버 요청은 부르는 쪽이) — 붙은 선·선택·대기 중인 글 저장도 같이 정리.
function removeBoardNodesLocal(ids){
  for(const id of ids){
    const pending = boardTextPending.get(id);
    if(pending){ clearTimeout(pending.timer); boardTextPending.delete(id); }
    const el = document.querySelector(`#board-world .board-node[data-node-id="${id}"]`);
    if(el) el.remove();
  }
  boardNodes = boardNodes.filter(n => !ids.has(n.id));
  // 서버는 이 카드들에 붙은 선을 CASCADE로 같이 지운다 — 화면도 똑같이 맞춘다.
  const edgeCount = boardEdges.length;
  boardEdges = boardEdges.filter(e => !ids.has(e.from_node_id) && !ids.has(e.to_node_id));
  if(boardSelectedEdgeId !== null && !boardEdges.some(e => e.id === boardSelectedEdgeId)) boardSelectedEdgeId = null;
  if(boardEdges.length !== edgeCount) renderBoardEdges();
  if([...ids].some(id => boardSelectedIds.has(id))){
    setBoardSelection(new Set([...boardSelectedIds].filter(id => !ids.has(id))));
  }
}

// 되돌리기용으로 카드를 그대로 떠 둔다 — 글칸에 아직 저장 안 된 글이 있으면 그 글로.
function boardNodeSnapshot(node){
  const ta = document.querySelector(`#board-world .board-node-textarea[data-board-text="${node.id}"]`);
  // data(생성 카드 내용)는 깊게 복사한다 — 그 뒤에 카드 값을 고쳐도 되돌리기 기록이 같이 바뀌지 않게.
  return { ...node, text: ta ? ta.value : node.text, data: node.data ? JSON.parse(JSON.stringify(node.data)) : node.data };
}

// 영상 카드는 화면(과 그 둘레 조금) 안에 들어올 때 처음 src를 붙인다 — 보드에 영상이
// 많아도 여는 순간 전부 내려받지 않게(특히 모바일 데이터). 이미지는 loading="lazy"가
// 같은 일을 한다.
let boardMediaObserver = null;
function boardObserveMedia(root){
  const videos = root.querySelectorAll('video[data-src]');
  if(!videos.length) return;
  const load = (v) => { v.src = v.dataset.src; v.removeAttribute('data-src'); };
  if(!('IntersectionObserver' in window)){ videos.forEach(load); return; }
  if(!boardMediaObserver){
    boardMediaObserver = new IntersectionObserver((entries) => {
      for(const entry of entries){
        if(!entry.isIntersecting) continue;
        boardMediaObserver.unobserve(entry.target);
        load(entry.target);
      }
    }, { root: document.getElementById('board-viewport'), rootMargin: '300px' });
  }
  videos.forEach(v => boardMediaObserver.observe(v));
}

// 선을 잇거나 끊을 때는 선 층과 끊기 버튼만 갈아 끼운다(카드는 그대로).
function renderBoardEdges(){
  const svg = document.getElementById('board-edges');
  if(!svg){ renderBoard(); return; }
  svg.outerHTML = boardEdgesSvg();
  const oldBtn = document.getElementById('board-edge-del');
  if(oldBtn) oldBtn.remove();
  document.getElementById('board-world').insertAdjacentHTML('beforeend', boardEdgeDelHtml());
  paintBoardGenSlots();
}

// 카드를 끄는 동안 픽셀마다 불린다 — 그 카드들(id 하나 또는 Set)에 붙은 선의 d만
// 고쳐서 가볍게 유지한다.
function updateBoardEdgePaths(nodeIds){
  const ids = nodeIds instanceof Set ? nodeIds : new Set([nodeIds]);
  const world = document.getElementById('board-world');
  for(const edge of boardEdges){
    if(!ids.has(edge.from_node_id) && !ids.has(edge.to_node_id)) continue;
    const c = boardEdgeCurveById(edge);
    if(!c) continue;
    world.querySelectorAll(`path[data-edge-id="${edge.id}"]`).forEach(p => p.setAttribute('d', c.d));
    if(edge.id === boardSelectedEdgeId){
      const btn = document.getElementById('board-edge-del');
      if(btn){ btn.style.left = c.mid.x + 'px'; btn.style.top = c.mid.y + 'px'; }
    }
  }
}

// 선을 잇는 중 연결 점(출발 카드 오른쪽 변 가운데)에서 커서까지 점선을 그린다.
// 커서는 화면 좌표라 지금 팬/줌을 되돌려 월드 좌표로 바꾼다.
function updateBoardTempEdge(fromId, clientX, clientY){
  const temp = document.getElementById('board-edge-temp');
  const node = boardNodes.find(n => n.id === fromId);
  if(!temp || !node) return;
  const rect = document.getElementById('board-viewport').getBoundingClientRect();
  const x = (clientX - rect.left - boardState.x) / boardState.scale;
  const y = (clientY - rect.top - boardState.y) / boardState.scale;
  const x0 = node.x + node.width, y0 = node.y + node.height / 2;
  const k = Math.max(30, Math.min(160, Math.abs(x - x0) / 2));
  temp.setAttribute('d', `M${x0},${y0} C${x0 + k},${y0} ${x - k},${y} ${x},${y}`);
}

// 선 고르기/풀기는 카드까지 다시 그리지 않고(영상 카드가 다시 로드되며 깜빡이고,
// 막 포인터 캡처를 건 연결 점이 사라진다) 선 모양과 끊기 버튼만 바꾼다.
function selectBoardEdge(edgeId){
  if(edgeId === boardSelectedEdgeId) return;
  const world = document.getElementById('board-world');
  if(!world) return;
  world.querySelectorAll('.board-edge-line.selected').forEach(p => {
    p.classList.remove('selected');
    p.setAttribute('marker-end', 'url(#board-arrow)');
  });
  const oldBtn = document.getElementById('board-edge-del');
  if(oldBtn) oldBtn.remove();
  boardSelectedEdgeId = edgeId;
  if(edgeId === null) return;
  const line = world.querySelector(`.board-edge-line[data-edge-id="${edgeId}"]`);
  if(line){
    line.classList.add('selected');
    line.setAttribute('marker-end', 'url(#board-arrow-sel)');
  }
  world.insertAdjacentHTML('beforeend', boardEdgeDelHtml());
}

// 보드 API 호출 — 실패(네트워크 끊김·서버 오류)면 null. 호출한 순간의 프로젝트 id를
// 받아서 보낸다 — 응답을 기다리는 사이 프로젝트를 바꿔도 엉뚱한 보드에 저장되지 않게.
async function boardApi(projectId, method, path, body, keepalive){
  try{
    const res = await fetch(`/api/projects/${projectId}/board${path}`, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      keepalive: !!keepalive,
    });
    return res.ok ? res : null;
  }catch(e){
    return null;
  }
}

// 옮기기/지우기처럼 화면을 먼저 바꾸고 저장하는 변경이 실패하면 화면이 서버와
// 달라진 것이다 — 조용히 넘기면 새로고침 때 되돌아가 "저장된 줄 알았는데" 가
// 된다. 알리고, 서버에 실제로 저장된 상태로 다시 맞춘다(쓰던 글은 먼저 보낸다).
async function boardSaveFailed(projectId){
  flashNotice('보드 변경을 저장하지 못했어요. 저장된 상태로 다시 불러올게요.');
  if(projectId !== currentProjectId || currentTab !== 'prboard') return;
  await flushAllBoardTextSaves(false);
  fetchBoard();
}

// toSlot을 주면 생성 카드의 그 입구로 잇는다 — 입구 하나에는 선 하나라, 서버가 그 입구로 오던 선(과
// 같은 두 카드 사이의 선)을 지우고 replaced로 알려 준다. 되돌리기는 그 선들을 되살린다.
async function createBoardEdge(fromId, toId, toSlot){
  const projectId = currentProjectId;
  if(typeof projectId !== 'number') return;
  const body = { from_node_id: fromId, to_node_id: toId };
  if(toSlot) body.to_slot = toSlot;
  const res = await boardApi(projectId, 'POST', '/edges', body);
  if(!res){ flashNotice('카드를 잇지 못했어요. 잠시 뒤 다시 해 주세요.'); return; }
  const data = await res.json();
  if(projectId !== currentProjectId) return;
  const replaced = data.replaced || [];
  const edge = { ...data };
  delete edge.replaced;
  if(replaced.length){
    const gone = new Set(replaced.map(e => e.id));
    boardEdges = boardEdges.filter(e => !gone.has(e.id));
    if(gone.has(boardSelectedEdgeId)) boardSelectedEdgeId = null;
  }
  // 같은 두 카드 사이에 이미 선이 있으면 서버가 그 선을 돌려준다 — 중복으로 넣지 않는다.
  if(boardEdges.some(e => e.id === edge.id)){ if(replaced.length) renderBoardEdges(); return; }
  boardEdges.push(edge);
  renderBoardEdges();
  pushBoardHistory({ type: 'edge-add', edge: { ...edge }, replaced });
}

async function deleteBoardEdge(edgeId){
  const projectId = currentProjectId;
  if(typeof projectId !== 'number') return;
  const edge = boardEdges.find(e => e.id === edgeId);
  if(!edge) return;
  boardEdges = boardEdges.filter(e => e.id !== edgeId);
  if(boardSelectedEdgeId === edgeId) boardSelectedEdgeId = null;
  renderBoardEdges();
  pushBoardHistory({ type: 'edge-del', edge: { ...edge } });
  if(!await boardApi(projectId, 'DELETE', `/edges/${edgeId}`)) boardSaveFailed(projectId);
}

// 지금 화면 가운데(뷰포트 중심)가 가리키는 월드 좌표에 새 카드를 놓는다 — 팬/줌해
// 둔 위치와 무관하게 항상 "지금 보고 있는 자리"에 생기게.
function boardViewportCenterWorld(){
  const viewport = document.getElementById('board-viewport');
  const rect = viewport.getBoundingClientRect();
  return {
    x: (rect.width / 2 - boardState.x) / boardState.scale,
    y: (rect.height / 2 - boardState.y) / boardState.scale,
  };
}

// geom({ x, y, width, height })을 주면 그 자리에 놓는다(묶음 틀을 고른 카드 둘레에 만들 때).
async function createBoardNode(kind, assetPath, text, geom){
  const projectId = currentProjectId;
  if(typeof projectId !== 'number') return;
  const width = kind === 'frame' ? 480 : kind === 'job' ? 260 : 220;
  const height = kind === 'frame' ? 320 : kind === 'text' ? 160 : 220;
  const center = boardViewportCenterWorld();
  const body = { kind, x: center.x - width / 2, y: center.y - height / 2, width, height, text: text || '', ...(geom || {}) };
  if(kind === 'image' || kind === 'video') body.asset_path = assetPath;
  if(kind === 'job') body.job_id = assetPath;   // 고르기 창은 작업 id를 같은 자리로 넘긴다
  if(kind === 'gen'){
    // 프리셋 id. 크기는 서버가 입구·꺼낸 옵션 수로 정한다(여기서는 아직 모른다).
    body.preset_id = Number(assetPath);
    delete body.width;
    delete body.height;
  }
  const res = await boardApi(projectId, 'POST', '/nodes', body);
  if(!res){ flashNotice('카드를 만들지 못했어요. 잠시 뒤 다시 해 주세요.'); return; }
  const node = await res.json();
  if(projectId !== currentProjectId) return;
  boardNodes.push(node);
  appendBoardNodeEls([node]);
  setBoardSelection(new Set([node.id]));
  pushBoardHistory({ type: 'create', node: { ...node } });
  if(kind === 'job') fetchBoardJobs();
}

// 묶음 틀 — 고른 카드가 있으면 그 둘레를 감싸고(제목 띠 자리만큼 위를 더 띄움), 없으면
// 화면 가운데에 빈 틀을 놓는다. 틀 안에 든 카드는 틀을 옮길 때 같이 움직인다(boardNodeInside).
const BOARD_FRAME_MIN_W = 160, BOARD_FRAME_MIN_H = 100;
const BOARD_MIN_SIZE = {   // 크기 바꾸기의 최소 크기(월드 px)
  frame: { w: BOARD_FRAME_MIN_W, h: BOARD_FRAME_MIN_H },
  image: { w: 80, h: 80 }, video: { w: 80, h: 80 }, text: { w: 120, h: 80 },
};
const BOARD_FRAME_PAD = 30, BOARD_FRAME_BAR = 40;

function boardNodeInside(n, f){
  return n.x >= f.x && n.y >= f.y && n.x + n.width <= f.x + f.width && n.y + n.height <= f.y + f.height;
}

function createBoardFrame(){
  const sel = boardNodes.filter(n => boardSelectedIds.has(n.id));
  if(!sel.length){ createBoardNode('frame', null, '묶음'); return; }
  const x0 = Math.min(...sel.map(n => n.x)) - BOARD_FRAME_PAD;
  const y0 = Math.min(...sel.map(n => n.y)) - BOARD_FRAME_PAD - BOARD_FRAME_BAR;
  const x1 = Math.max(...sel.map(n => n.x + n.width)) + BOARD_FRAME_PAD;
  const y1 = Math.max(...sel.map(n => n.y + n.height)) + BOARD_FRAME_PAD;
  createBoardNode('frame', null, '묶음', { x: x0, y: y0, width: x1 - x0, height: y1 - y0 });
}

async function saveBoardNodeSize(nodeId, size){
  const projectId = currentProjectId;
  if(typeof projectId !== 'number') return;
  if(!await boardApi(projectId, 'PATCH', `/nodes/${nodeId}`, size)) boardSaveFailed(projectId);
}

// 끌기를 마친 카드들의 위치를 요청 하나로 저장한다 — positions = [{ id, x, y }, ...].
async function saveBoardPositions(positions){
  const projectId = currentProjectId;
  if(typeof projectId !== 'number') return;
  if(!await boardApi(projectId, 'PATCH', '/nodes', { nodes: positions })) boardSaveFailed(projectId);
}

// 텍스트 카드 저장 — 입력하다 멈추면 BOARD_TEXT_SAVE_DELAY_MS 뒤에, 글칸을 벗어나거나
// 화면이 가려지면 바로 보낸다. 카드마다 대기 중인 저장을 하나씩만 둔다.
const BOARD_TEXT_SAVE_DELAY_MS = 800;
const boardTextPending = new Map();   // nodeId -> { projectId, text, timer }

function scheduleBoardTextSave(nodeId, text){
  const prev = boardTextPending.get(nodeId);
  if(prev) clearTimeout(prev.timer);
  boardTextPending.set(nodeId, {
    projectId: currentProjectId, text,
    timer: setTimeout(() => flushBoardTextSave(nodeId, text), BOARD_TEXT_SAVE_DELAY_MS),
  });
}

function flushBoardTextSave(nodeId, text, keepalive){
  const prev = boardTextPending.get(nodeId);
  if(prev){ clearTimeout(prev.timer); boardTextPending.delete(nodeId); }
  return saveBoardNodeText(prev ? prev.projectId : currentProjectId, nodeId, text, keepalive);
}

function flushAllBoardTextSaves(keepalive){
  // 생성 카드의 꺼낸 옵션도 같이 — 보드를 다시 받거나 되돌리기 전에 쓰던 값이 먼저 서버에 가 있게.
  return Promise.all([...boardTextPending.entries()].map(([nodeId, p]) => flushBoardTextSave(nodeId, p.text, keepalive))
    .concat([flushAllBoardGenSaves(keepalive)]));
}

async function saveBoardNodeText(projectId, nodeId, text, keepalive){
  if(typeof projectId !== 'number') return;
  const node = projectId === currentProjectId ? boardNodes.find(n => n.id === nodeId) : null;
  if(node && node.text === text) return;   // 서버에 이미 있는 글이면 안 보낸다
  if(await boardApi(projectId, 'PATCH', `/nodes/${nodeId}`, { text }, keepalive)){
    if(node) node.text = text;
    return;
  }
  // 글은 글칸에 그대로 남긴다(다시 불러오면 쓴 글이 사라지므로 여기선 안 맞춘다).
  // node.text를 안 바꿔 뒀으니 다음에 입력하거나 글칸을 벗어나면 다시 보낸다.
  flashNotice('메모를 저장하지 못했어요. 글은 화면에 남아 있으니 연결을 확인한 뒤 다른 곳을 눌러 다시 저장해 주세요.');
}

// 카드 여러 장을 지운다(카드의 휴지통 · 선택 막대 · Delete 키). 되돌리기로 살릴 수
// 있게 지우기 전의 카드와 붙어 있던 선을 떠서 기록에 남긴다.
async function deleteBoardNodes(nodeIds){
  const projectId = currentProjectId;
  if(typeof projectId !== 'number') return;
  const ids = new Set(nodeIds);
  const nodes = boardNodes.filter(n => ids.has(n.id)).map(boardNodeSnapshot);
  if(!nodes.length) return;
  const edges = boardEdges.filter(e => ids.has(e.from_node_id) || ids.has(e.to_node_id)).map(e => ({ ...e }));
  removeBoardNodesLocal(ids);
  pushBoardHistory({ type: 'delete', nodes, edges });
  if(!await boardApi(projectId, 'POST', '/nodes/delete', { ids: [...ids] })) boardSaveFailed(projectId);
}

// ---- 프로젝트 보드 — 되돌리기 / 다시 하기 ----
// 사람이 한 변경 하나(카드 옮기기·놓기·지우기, 글쓰기 한 번, 선 잇기·끊기)를 기록 한 칸으로
// 쌓는다. 되돌리기/다시 하기는 그 칸의 반대/같은 변경을 화면에 먼저 반영하고 서버에 보낸다.
// 기록은 지금 열어 둔 보드에서만 유효하다 — 새로고침하거나 보드를 다시 받으면(프로젝트 전환,
// 저장 실패 후 다시 맞추기) 비운다(fetchBoard 참고).
//   { type: 'move', items: [{ id, from: {x, y}, to: {x, y} }] }
//   { type: 'create', node, edges? }        — edges는 되돌릴 때 그 사이 붙은 선을 떠 둔 것
//   { type: 'delete', nodes, edges }
//   { type: 'text', id, from, to }
//   { type: 'edge-add', edge } / { type: 'edge-del', edge }
//   { type: 'resize', id, from: {width, height}, to: {width, height} }   — 묶음 틀 크기
//   { type: 'gen-value', id, name, from, to }                           — 생성 카드의 꺼낸 옵션 하나
const BOARD_HISTORY_LIMIT = 100;
let boardUndoStack = [];
let boardRedoStack = [];
let boardHistoryBusy = false;   // 되돌리기 요청이 오가는 동안은 다음 되돌리기를 받지 않는다

function paintBoardHistoryButtons(){
  const undoBtn = document.getElementById('board-undo-btn');
  const redoBtn = document.getElementById('board-redo-btn');
  if(undoBtn) undoBtn.disabled = boardHistoryBusy || !boardUndoStack.length;
  if(redoBtn) redoBtn.disabled = boardHistoryBusy || !boardRedoStack.length;
}
function resetBoardHistory(){
  boardUndoStack = [];
  boardRedoStack = [];
  paintBoardHistoryButtons();
}
function pushBoardHistory(entry){
  boardUndoStack.push(entry);
  if(boardUndoStack.length > BOARD_HISTORY_LIMIT) boardUndoStack.shift();
  boardRedoStack = [];   // 새 변경을 하면 "다시 하기"할 갈래는 사라진다
  paintBoardHistoryButtons();
}

async function boardUndoRedo(dir){
  if(boardHistoryBusy) return;
  const projectId = currentProjectId;
  if(typeof projectId !== 'number') return;
  // 글을 쓰던 중이면 먼저 그 글쓰기를 마쳐(기록 한 칸 + 저장) 두고 시작한다 — 안 그러면
  // 되돌린 뒤에 남아 있던 글 저장이 되돌린 내용을 다시 덮는다.
  const active = document.activeElement;
  if(active && active.classList && active.classList.contains('board-node-textarea')) active.blur();
  await flushAllBoardTextSaves(false);
  const from = dir === 'undo' ? boardUndoStack : boardRedoStack;
  const entry = from.pop();
  if(!entry){ paintBoardHistoryButtons(); return; }
  boardHistoryBusy = true;
  paintBoardHistoryButtons();
  let ok = false;
  try{
    ok = await applyBoardHistoryEntry(projectId, entry, dir);
  }catch(e){
    ok = false;
  }
  boardHistoryBusy = false;
  if(projectId !== currentProjectId) return;
  if(ok){
    (dir === 'undo' ? boardRedoStack : boardUndoStack).push(entry);
    paintBoardHistoryButtons();
  }else{
    // 서버와 화면이 어긋났을 수 있다 — 서버 상태로 다시 맞추고 기록은 버린다(fetchBoard가 비움).
    boardSaveFailed(projectId);
  }
}

function setBoardNodePosLocal(id, x, y){
  const node = boardNodes.find(n => n.id === id);
  if(!node) return;
  node.x = x; node.y = y;
  const el = document.querySelector(`#board-world .board-node[data-node-id="${id}"]`);
  if(el){ el.style.left = x + 'px'; el.style.top = y + 'px'; }
}

// 되살린 카드/선의 id가 원래와 달라졌으면(그 사이 다른 카드가 그 id를 가져감) 기록 전체의
// id를 새 것으로 고쳐 쓴다 — 안 그러면 이후 되돌리기가 없는 카드를 가리킨다.
function remapBoardHistory(extraEntry, nodeMap, edgeMap){
  if(!nodeMap.size && !edgeMap.size) return;
  const n = (id) => nodeMap.has(id) ? nodeMap.get(id) : id;
  const fixEdge = (e) => {
    if(edgeMap.has(e.id)) e.id = edgeMap.get(e.id);
    e.from_node_id = n(e.from_node_id);
    e.to_node_id = n(e.to_node_id);
  };
  for(const entry of [...boardUndoStack, ...boardRedoStack, extraEntry]){
    if(entry.items) entry.items.forEach(it => { it.id = n(it.id); });
    if(entry.type === 'text' || entry.type === 'resize' || entry.type === 'gen-value') entry.id = n(entry.id);
    if(entry.node) entry.node.id = n(entry.node.id);
    if(entry.nodes) entry.nodes.forEach(nd => { nd.id = n(nd.id); });
    if(entry.edges) entry.edges.forEach(fixEdge);
    if(entry.edge) fixEdge(entry.edge);
    if(entry.replaced) entry.replaced.forEach(fixEdge);
  }
}

// 지운 카드·선을 서버에서 되살리고 화면에 붙인다.
async function restoreBoardItems(projectId, entry, nodes, edges){
  const res = await boardApi(projectId, 'POST', '/restore', { nodes, edges });
  if(!res) return false;
  const data = await res.json();
  if(projectId !== currentProjectId) return true;
  const nodeMap = new Map(Object.entries(data.id_map || {}).map(([k, v]) => [Number(k), v]).filter(([k, v]) => k !== v));
  const edgeMap = new Map();
  edges.forEach((e, i) => { if(data.edges[i] && data.edges[i].id !== e.id) edgeMap.set(e.id, data.edges[i].id); });
  remapBoardHistory(entry, nodeMap, edgeMap);
  const restoredNodes = data.nodes.filter(nd => !boardNodes.some(b => b.id === nd.id));
  boardNodes.push(...restoredNodes);
  appendBoardNodeEls(restoredNodes);
  if(restoredNodes.some(n => n.kind === 'job')) fetchBoardJobs();
  for(const e of data.edges) if(!boardEdges.some(b => b.id === e.id)) boardEdges.push(e);
  if(data.edges.length) renderBoardEdges();
  return true;
}

async function applyBoardHistoryEntry(projectId, entry, dir){
  const undo = dir === 'undo';
  if(entry.type === 'move'){
    const positions = entry.items.map(it => ({ id: it.id, ...(undo ? it.from : it.to) }));
    positions.forEach(p => setBoardNodePosLocal(p.id, p.x, p.y));
    updateBoardEdgePaths(new Set(positions.map(p => p.id)));
    return !!await boardApi(projectId, 'PATCH', '/nodes', { nodes: positions });
  }
  if(entry.type === 'text'){
    const text = undo ? entry.from : entry.to;
    const node = boardNodes.find(n => n.id === entry.id);
    if(node) node.text = text;
    const ta = document.querySelector(`#board-world .board-node-textarea[data-board-text="${entry.id}"]`);
    if(ta) ta.value = text;
    return !!await boardApi(projectId, 'PATCH', `/nodes/${entry.id}`, { text });
  }
  if(entry.type === 'create' || entry.type === 'delete'){
    // 놓기를 되돌리기 = 지우기, 지우기를 되돌리기 = 되살리기(다시 하기는 그 반대)
    const removing = (entry.type === 'create') === undo;
    if(removing){
      if(entry.type === 'create'){
        // 그 사이 이 카드에 붙은 선이 있으면 같이 지워지므로, 다시 하기 때 살리게 떠 둔다.
        const cur = boardNodes.find(n => n.id === entry.node.id);
        if(cur) entry.node = boardNodeSnapshot(cur);
        entry.edges = boardEdges.filter(e => e.from_node_id === entry.node.id || e.to_node_id === entry.node.id).map(e => ({ ...e }));
      }
      const ids = entry.type === 'create' ? [entry.node.id] : entry.nodes.map(n => n.id);
      removeBoardNodesLocal(new Set(ids));
      return !!await boardApi(projectId, 'POST', '/nodes/delete', { ids });
    }
    const nodes = entry.type === 'create' ? [entry.node] : entry.nodes;
    return restoreBoardItems(projectId, entry, nodes, entry.edges || []);
  }
  if(entry.type === 'edge-add' || entry.type === 'edge-del'){
    const removing = (entry.type === 'edge-add') === undo;
    // 입구로 이을 때 밀려난 선(replaced) — 잇기를 되돌리면 되살리고, 다시 하면 다시 지운다.
    const replaced = entry.type === 'edge-add' ? (entry.replaced || []) : [];
    if(removing){
      boardEdges = boardEdges.filter(e => e.id !== entry.edge.id);
      if(boardSelectedEdgeId === entry.edge.id) boardSelectedEdgeId = null;
      renderBoardEdges();
      if(!await boardApi(projectId, 'DELETE', `/edges/${entry.edge.id}`)) return false;
      return replaced.length ? restoreBoardItems(projectId, entry, [], replaced) : true;
    }
    for(const r of replaced){
      boardEdges = boardEdges.filter(e => e.id !== r.id);
      renderBoardEdges();
      if(!await boardApi(projectId, 'DELETE', `/edges/${r.id}`)) return false;
    }
    return restoreBoardItems(projectId, entry, [], [entry.edge]);
  }
  if(entry.type === 'gen-value'){
    const value = undo ? entry.from : entry.to;
    setBoardGenValueLocal(entry.id, entry.name, value);
    return !!await boardApi(projectId, 'PATCH', `/nodes/${entry.id}`, { values: { [entry.name]: value } });
  }
  if(entry.type === 'resize'){
    const size = undo ? entry.from : entry.to;
    const node = boardNodes.find(n => n.id === entry.id);
    if(node){ node.width = size.width; node.height = size.height; }
    const el = document.querySelector(`#board-world .board-node[data-node-id="${entry.id}"]`);
    if(el){ el.style.width = size.width + 'px'; el.style.height = size.height + 'px'; }
    return !!await boardApi(projectId, 'PATCH', `/nodes/${entry.id}`, size);
  }
  return false;
}

// 모든 카드가 화면 안에 들어오도록 배율/이동을 맞춘다. 카드가 하나도 없으면
// 그냥 배율 1:1·원점으로 되돌린다.
function boardFitToNodes(){
  const viewport = document.getElementById('board-viewport');
  const rect = viewport.getBoundingClientRect();
  if(boardNodes.length === 0){
    boardState.x = 0; boardState.y = 0; boardState.scale = 1;
    applyBoardTransform();
    return;
  }
  const minX = Math.min(...boardNodes.map(n => n.x));
  const minY = Math.min(...boardNodes.map(n => n.y));
  const maxX = Math.max(...boardNodes.map(n => n.x + n.width));
  const maxY = Math.max(...boardNodes.map(n => n.y + n.height));
  const pad = 60;
  const scale = Math.min(BOARD_MAX_SCALE, Math.max(BOARD_MIN_SCALE,
    Math.min(rect.width / (maxX - minX + pad * 2), rect.height / (maxY - minY + pad * 2))));
  boardState.scale = scale;
  boardState.x = rect.width / 2 - (minX + maxX) / 2 * scale;
  boardState.y = rect.height / 2 - (minY + maxY) / 2 * scale;
  applyBoardTransform();
}

// ---- 프로젝트 보드 — 이미지/영상 카드를 놓을 때 "이 프로젝트 결과물 중 고르기" ----
let boardPickerKind = null;   // 'image' | 'video' | 'job'

async function openBoardAssetPicker(kind){
  if(typeof currentProjectId !== 'number') return;
  boardPickerKind = kind;
  document.getElementById('board-asset-picker-title').textContent =
    kind === 'image' ? '이미지 고르기' : kind === 'video' ? '영상 고르기' : kind === 'job' ? '작업 고르기' : '보드 프리셋 고르기';
  const grid = document.getElementById('board-asset-picker-grid');
  const empty = document.getElementById('board-asset-picker-empty');
  grid.innerHTML = '';
  grid.classList.toggle('jobs', kind === 'job' || kind === 'gen');
  empty.textContent = kind === 'job' ? '이 프로젝트에 작업이 없어요'
    : kind === 'gen' ? '저장한 보드 프리셋이 없어요 — 새 작업 창의 "보드 프리셋으로 저장"으로 만들어 주세요.'
    : '이 프로젝트에 결과물이 없어요';
  empty.style.display = 'none';
  document.getElementById('board-asset-picker-modal').style.display = 'flex';
  if(kind === 'job'){ fillBoardJobPicker(grid, empty); return; }
  if(kind === 'gen'){ fillBoardGenPicker(grid, empty); return; }
  let items = [];
  try{
    const res = await fetch(kind === 'image' ? '/api/output-images' : '/api/output-videos');
    const data = await res.json();
    items = ((kind === 'image' ? data.images : data.videos) || []).filter(it => it.project_id === currentProjectId);
    if(isNsfwHidden()) items = items.filter(it => !it.nsfw);   // 갤러리 "안 보기"와 같게 목록에서 뺀다
  }catch(e){ /* 아래에서 빈 상태로 처리 */ }
  if(items.length === 0){
    empty.style.display = 'block';
    return;
  }
  grid.innerHTML = items.map(it => {
    const name = it.name;
    if(it.nsfw && nsfwMode === 'blur'){
      // 갤러리 블러처럼 미리보기를 받지 않고 가린 채 고를 수 있게 둔다.
      return `<button type="button" class="board-picker-item" data-pick="${escapeHtml(name)}" title="NSFW 블러">`
        + `<div class="board-nsfw-cover">${ico('eye-off')}</div></button>`;
    }
    if(kind === 'image'){
      const url = window.__nightshiftMediaUrl(`/api/output-images/${encodeURIComponent(name)}/thumbnail?size=200&fit=cover`);
      return `<button type="button" class="board-picker-item" data-pick="${escapeHtml(name)}"><img src="${url}" loading="lazy" alt=""></button>`;
    }
    const url = window.__nightshiftMediaUrl(`/api/output-videos/${encodeURIComponent(name)}`);
    return `<button type="button" class="board-picker-item" data-pick="${escapeHtml(name)}"><video muted preload="metadata" src="${url}#t=0.1"></video></button>`;
  }).join('');
}
// 작업 고르기 — 이 프로젝트의 작업을 최근 것부터 한 줄씩(상태·템플릿·프롬프트 앞부분·등록 시각).
async function fillBoardJobPicker(grid, empty){
  const projectId = currentProjectId;
  let items = [];
  try{
    const res = await fetch(`/api/jobs?project_id=${projectId}`);
    if(res.ok) items = (await res.json()).jobs || [];
  }catch(e){ /* 아래에서 빈 상태로 처리 */ }
  if(projectId !== currentProjectId || boardPickerKind !== 'job') return;
  if(items.length === 0){ empty.style.display = 'block'; return; }
  grid.innerHTML = items.map(j => {
    const prompt = (j.options && j.options.main_prompt) || '';
    const when = j.queued_at ? new Date(j.queued_at).toLocaleString() : '';
    return `<button type="button" class="board-picker-job" data-pick="${escapeHtml(j.id)}">`
      + `<span class="badge ${escapeHtml(j.status)}"><span class="bdot"></span>${STATUS_LABEL[j.status] || escapeHtml(j.status)}</span>`
      + `<span class="board-picker-job-text"><b>${escapeHtml(j.template_label || j.template_id || '')}</b>`
      + `<span>${escapeHtml(prompt.slice(0, 120)) || '&nbsp;'}</span></span>`
      + `<span class="board-picker-job-when">${escapeHtml(when)}</span></button>`;
  }).join('');
}
// 보드 프리셋 고르기 — 이름·템플릿·꺼낸 옵션 수를 한 줄씩. 고르면 생성 카드를 놓는다.
async function fillBoardGenPicker(grid, empty){
  let items = [];
  try{
    const res = await fetch('/api/board-presets');
    if(res.ok) items = (await res.json()).presets || [];
  }catch(e){ /* 아래에서 빈 상태로 처리 */ }
  if(boardPickerKind !== 'gen') return;
  if(items.length === 0){ empty.style.display = 'block'; return; }
  grid.innerHTML = items.map(p => `<button type="button" class="board-picker-job" data-pick="${p.id}">`
    + `<span class="board-picker-job-text"><b>${escapeHtml(p.name)}</b><span>${escapeHtml(p.template_label || p.template_id)}</span></span>`
    + (p.template_missing ? '<span class="board-picker-job-when">템플릿 없음</span>' : '')
    + `</button>`).join('');
}

function closeBoardAssetPicker(){ document.getElementById('board-asset-picker-modal').style.display = 'none'; }
document.getElementById('board-asset-picker-close').addEventListener('click', closeBoardAssetPicker);
document.getElementById('board-asset-picker-modal').addEventListener('click', (e) => {
  if(e.target === document.getElementById('board-asset-picker-modal')) closeBoardAssetPicker();
});
document.getElementById('board-asset-picker-grid').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-pick]');
  if(!btn) return;
  closeBoardAssetPicker();
  createBoardNode(boardPickerKind, btn.dataset.pick, '');
});

