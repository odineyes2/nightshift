// ---- 프로젝트 보드 (무한 캔버스) ----
// 카드(이미지/영상/텍스트)와 연결선(svg)은 board-world 안에 절대 위치로 얹고, 팬/줌은
// board-world 하나에 거는 변환(translate+scale)으로만 한다. 카드·선 좌표는 "world
// 좌표"(변환 전 좌표)로 다뤄서 배율과 무관하게 저장/복원된다.
//
// 마우스 휠/트랙패드로 줌, 빈 캔버스를 드래그하면 팬 — 둘 다 이 앱이 이미 쓰는
// Pointer Events 관례(setupItemLongPressDelete 등)를 그대로 따른다. 점 배경은
// board-world와 같은 변환을 받도록 팬/줌 때마다 background-position/-size를
// 함께 갱신해서 "무한 캔버스"처럼 보이게 한다.
const boardState = { x: 0, y: 0, scale: 1 };
const BOARD_MIN_SCALE = 0.2, BOARD_MAX_SCALE = 3;
const BOARD_GRID_PX = 24;
let boardCanvasInitialized = false;

function applyBoardTransform(){
  const world = document.getElementById('board-world');
  const viewport = document.getElementById('board-viewport');
  if(!world || !viewport) return;
  world.style.transform = `translate(${boardState.x}px, ${boardState.y}px) scale(${boardState.scale})`;
  viewport.style.backgroundPosition = `${boardState.x}px ${boardState.y}px`;
  const gridSize = BOARD_GRID_PX * boardState.scale;
  viewport.style.backgroundSize = `${gridSize}px ${gridSize}px`;
  document.getElementById('board-zoom-label').textContent = Math.round(boardState.scale * 100) + '%';
  // 끊기 버튼은 배율과 무관하게 화면에서 늘 같은 크기로 보이게 역배율을 건다.
  const edgeDel = document.getElementById('board-edge-del');
  if(edgeDel) edgeDel.style.transform = `scale(${1 / boardState.scale})`;
}

// 헤더/내비/프로젝트 바 높이는 화면 폭·모바일 줄바꿈에 따라 달라질 수 있어 CSS
// 고정값 대신 실측한다 — 보드 탭에 들어올 때/창 크기가 바뀔 때 다시 잰다.
function resizeBoardViewport(){
  if(currentTab !== 'prboard') return;
  const viewport = document.getElementById('board-viewport');
  if(!viewport) return;
  const top = viewport.getBoundingClientRect().top;
  viewport.style.height = Math.max(200, window.innerHeight - top) + 'px';
}
window.addEventListener('resize', resizeBoardViewport);

// (clientX, clientY) 화면 좌표 아래에 있던 월드 지점이 줌 전후로 같은 화면 위치를
// 계속 가리키도록 translate를 보정 — "커서 아래를 기준으로 확대/축소"의 표준 공식.
function boardZoomAt(clientX, clientY, factor){
  const viewport = document.getElementById('board-viewport');
  const rect = viewport.getBoundingClientRect();
  const px = clientX - rect.left, py = clientY - rect.top;
  const newScale = Math.min(BOARD_MAX_SCALE, Math.max(BOARD_MIN_SCALE, boardState.scale * factor));
  if(newScale === boardState.scale) return;
  boardState.x = px - (px - boardState.x) * (newScale / boardState.scale);
  boardState.y = py - (py - boardState.y) * (newScale / boardState.scale);
  boardState.scale = newScale;
  applyBoardTransform();
}

function initBoardCanvas(){
  if(boardCanvasInitialized) return;
  boardCanvasInitialized = true;
  const viewport = document.getElementById('board-viewport');

  viewport.addEventListener('wheel', (e) => {
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.1 : 1 / 1.1;
    boardZoomAt(e.clientX, e.clientY, factor);
  }, { passive: false });

  // 빈 캔버스(카드가 아닌 배경)를 한 손가락/마우스로 끌면 팬, 두 손가락이면 핀치
  // 줌(+두 손가락 가운데를 따라 팬). 카드 위의 pointerdown은 아래 카드 드래그가
  // 맡으므로 여기서는 배경만 본다. 배경에 닿아 있는 포인터를 전부 기억해 두고,
  // 두 개가 되면 두 점 사이 거리 변화로 배율을, 가운데 점 이동으로 위치를 바꾼다.
  // Shift를 누른 채(또는 선택 모드에서) 배경을 끌면 팬 대신 사각형으로 카드를 고른다.
  const BOARD_DRAG_THRESHOLD_PX = 5;   // 이보다 덜 움직이면 끌기가 아니라 "톡"으로 본다
  const bgPointers = new Map();   // pointerId -> { x, y }
  let dragNode = null;   // 카드 드래그 상태 — 아래 카드 드래그 코드 참고
  let marquee = null;    // 사각형 고르기 상태 — { pointerId, x0, y0, x, y, before, base, additive, el, moved }
  let bgTap = null;      // 배경을 한 손가락으로 "톡" 눌렀는지(끌면 null) — 떼면 카드 선택 해제
  let pinch = null;               // { dist, midX, midY } — 직전 두 손가락 상태
  const measurePinch = () => {
    const [a, b] = [...bgPointers.values()];
    return { dist: Math.hypot(a.x - b.x, a.y - b.y) || 1, midX: (a.x + b.x) / 2, midY: (a.y + b.y) / 2 };
  };
  viewport.addEventListener('pointerdown', (e) => {
    if(e.pointerType === 'mouse' && e.button !== 0) return;
    // 이미 한 손가락이 캔버스를 잡고 있으면 두 번째 손가락은 어디에 닿든(선, 선택 막대,
    // 끊기 버튼 위라도) 핀치로 본다 — 예: 첫 손가락이 카드를 고르는 순간 아래쪽에 선택
    // 막대가 떠서, 같이 내려온 두 번째 손가락이 그 위에 닿는다. 카드 위는 아래 카드
    // 드래그 쪽에서 같은 규칙으로 처리한다.
    if(e.pointerType === 'touch' && (dragNode || marquee || bgPointers.size) && !e.target.closest('.board-node')){
      if(dragNode) pinchFromNodeDrag(e);
      else if(marquee) pinchFromMarquee(e);
      else addBoardPanPointer(e);
      return;
    }
    if(e.target.closest('.board-node, .board-edge-del, .board-selection-bar')) return;
    // 선(투명한 굵은 선) 위를 누르면 팬 대신 그 선을 고른다. 여기서 바로 처리하는
    // 이유: 팬이 viewport에 포인터 캡처를 걸면 click 이벤트가 선이 아니라
    // viewport로 가서, click으로는 어느 선인지 알 수 없다.
    const hit = e.target.closest('.board-edge-hit');
    if(hit){ selectBoardEdge(Number(hit.dataset.edgeId)); return; }
    if(boardSelectedEdgeId !== null) selectBoardEdge(null);   // 빈 배경을 누르면 선 선택 해제
    if(dragNode){ pinchFromNodeDrag(e); return; }   // 한 손가락은 카드를 잡고 있었다 → 핀치로
    if(marquee){ pinchFromMarquee(e); return; }     // 한 손가락은 사각형을 그리던 중 → 핀치로
    if(bgPointers.size === 0 && (e.shiftKey || boardSelectMode)){ startMarquee(e); return; }
    addBoardPanPointer(e);
    bgTap = bgPointers.size === 1 ? { pointerId: e.pointerId, x: e.clientX, y: e.clientY } : null;
  });
  const addBoardPanPointer = (e) => {
    if(bgPointers.size >= 2) return;   // 세 번째 손가락부터는 무시
    bgPointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    viewport.setPointerCapture(e.pointerId);
    viewport.classList.add('panning');
    pinch = bgPointers.size === 2 ? measurePinch() : null;
    if(bgPointers.size === 2) bgTap = null;
  };
  // 확대해 두면 카드가 화면을 거의 덮어서, 핀치하는 손가락이 카드 위에 닿기 쉽다.
  // 카드를 잡은 손가락이 있는데 두 번째 손가락이 닿으면 카드 끌기를 취소하고(제자리로)
  // 두 손가락 핀치로 바꾼다 — 사람은 "카드 두 손가락으로 끌기"가 아니라 "확대/축소"를
  // 하려는 것이다.
  const pinchFromNodeDrag = (e) => {
    if(e.pointerType !== 'touch' || dragNode.pointerType !== 'touch') return;
    for(const g of dragNode.group){
      g.node.x = g.startX; g.node.y = g.startY;
      g.el.style.left = g.node.x + 'px';
      g.el.style.top = g.node.y + 'px';
      g.el.classList.remove('dragging');
    }
    updateBoardEdgePaths(new Set(dragNode.group.map(g => g.node.id)));
    setBoardSelection(dragNode.selBefore);   // 고르려던 게 아니라 핀치였다 — 선택도 되돌린다
    bgPointers.set(dragNode.pointerId, { x: dragNode.lastX, y: dragNode.lastY });
    dragNode = null;
    addBoardPanPointer(e);
  };
  viewport.addEventListener('pointermove', (e) => {
    const prev = bgPointers.get(e.pointerId);
    if(!prev) return;
    const dx = e.clientX - prev.x, dy = e.clientY - prev.y;
    prev.x = e.clientX; prev.y = e.clientY;
    if(bgTap && bgTap.pointerId === e.pointerId
       && Math.hypot(e.clientX - bgTap.x, e.clientY - bgTap.y) >= BOARD_DRAG_THRESHOLD_PX) bgTap = null;
    if(bgPointers.size === 2 && pinch){
      const now = measurePinch();
      boardState.x += now.midX - pinch.midX;
      boardState.y += now.midY - pinch.midY;
      boardZoomAt(now.midX, now.midY, now.dist / pinch.dist);
      applyBoardTransform();   // 배율이 한계라 boardZoomAt이 그냥 돌아와도 팬은 반영
      pinch = now;
      return;
    }
    boardState.x += dx;
    boardState.y += dy;
    applyBoardTransform();
  });
  const endBoardPan = (e) => {
    if(!bgPointers.delete(e.pointerId)) return;
    // 배경을 끌지 않고 톡 눌렀다 떼면 고른 카드를 푼다(끌어서 화면만 옮길 때는 유지).
    if(bgTap && bgTap.pointerId === e.pointerId){
      if(e.type === 'pointerup') setBoardSelection(new Set());
      bgTap = null;
    }
    // 두 손가락 중 하나를 떼면 남은 손가락으로 그대로 팬을 이어간다.
    pinch = bgPointers.size === 2 ? measurePinch() : null;
    if(bgPointers.size === 0) viewport.classList.remove('panning');
  };
  for(const type of ['pointerup', 'pointercancel', 'pointerleave']) viewport.addEventListener(type, endBoardPan);

  // 사각형으로 고르기 — 화면 좌표로 틀을 그리고, 틀을 월드 좌표로 바꿔 겹치는 카드를
  // 고른다. Shift로 시작했으면 이미 고른 카드에 더한다.
  const startMarquee = (e) => {
    // before: 시작 전 선택 — 두 번째 손가락이 닿아 핀치로 바뀌면 이걸로 되돌린다(사각형을
    // 그리려던 게 아니었으니 고른 카드를 잃으면 안 된다).
    marquee = { pointerId: e.pointerId, x0: e.clientX, y0: e.clientY, x: e.clientX, y: e.clientY,
                before: new Set(boardSelectedIds), base: e.shiftKey ? new Set(boardSelectedIds) : new Set(),
                additive: e.shiftKey, el: null, moved: false };
    viewport.setPointerCapture(e.pointerId);
  };
  const pinchFromMarquee = (e) => {
    if(e.pointerType !== 'touch') return;
    if(marquee.el) marquee.el.remove();
    setBoardSelection(marquee.before);
    bgPointers.set(marquee.pointerId, { x: marquee.x, y: marquee.y });
    marquee = null;
    addBoardPanPointer(e);
  };
  viewport.addEventListener('pointermove', (e) => {
    if(!marquee || e.pointerId !== marquee.pointerId) return;
    marquee.x = e.clientX; marquee.y = e.clientY;
    if(!marquee.moved){
      if(Math.hypot(e.clientX - marquee.x0, e.clientY - marquee.y0) < BOARD_DRAG_THRESHOLD_PX) return;
      marquee.moved = true;
      marquee.el = document.createElement('div');
      marquee.el.className = 'board-marquee';
      viewport.appendChild(marquee.el);
    }
    const vr = viewport.getBoundingClientRect();
    const left = Math.min(marquee.x0, e.clientX) - vr.left, top = Math.min(marquee.y0, e.clientY) - vr.top;
    const w = Math.abs(e.clientX - marquee.x0), h = Math.abs(e.clientY - marquee.y0);
    Object.assign(marquee.el.style, { left: left + 'px', top: top + 'px', width: w + 'px', height: h + 'px' });
    const wx0 = (left - boardState.x) / boardState.scale, wy0 = (top - boardState.y) / boardState.scale;
    const wx1 = wx0 + w / boardState.scale, wy1 = wy0 + h / boardState.scale;
    const sel = new Set(marquee.base);
    for(const n of boardNodes){
      // 틀은 완전히 감쌌을 때만 — 큰 틀 안에서 카드 몇 장을 고르려는데 틀까지 딸려 오지 않게.
      if(n.kind === 'frame'){
        if(n.x >= wx0 && n.y >= wy0 && n.x + n.width <= wx1 && n.y + n.height <= wy1) sel.add(n.id);
      }else if(n.x < wx1 && n.x + n.width > wx0 && n.y < wy1 && n.y + n.height > wy0){
        sel.add(n.id);
      }
    }
    setBoardSelection(sel);
  });
  const endMarquee = (e) => {
    if(!marquee || e.pointerId !== marquee.pointerId) return;
    if(marquee.el) marquee.el.remove();
    // 끌지 않고 톡 누른 거면(선택 모드에서 빈 곳 톡) 선택 해제 — Shift 톡은 그대로 둔다.
    if(!marquee.moved && e.type === 'pointerup' && !marquee.additive) setBoardSelection(new Set());
    marquee = null;
  };
  for(const type of ['pointerup', 'pointercancel']) viewport.addEventListener(type, endMarquee);

  document.getElementById('board-zoom-in-btn').addEventListener('click', () => {
    const rect = viewport.getBoundingClientRect();
    boardZoomAt(rect.left + rect.width / 2, rect.top + rect.height / 2, 1.25);
  });
  document.getElementById('board-zoom-out-btn').addEventListener('click', () => {
    const rect = viewport.getBoundingClientRect();
    boardZoomAt(rect.left + rect.width / 2, rect.top + rect.height / 2, 1 / 1.25);
  });
  document.getElementById('board-fit-btn').addEventListener('click', boardFitToNodes);

  // 카드 드래그 — 배경 팬(위)과 같은 viewport에 델리게이트하지만, 팬 쪽
  // pointerdown이 이미 `.board-node` 위면 그냥 return하므로 서로 안 부딪힌다.
  // 누른 카드가 고른 카드 중 하나면 고른 카드 전부를 같이 옮기고, 안 고른 카드면
  // 그 카드만 골라서 옮긴다. Shift/Ctrl/⌘를 누른 채 누르면 고르기에 더하거나 뺀다.
  // 텍스트 카드도 글쓰기 중(.editing)이 아니면 글칸이 포인터를 안 받아서(CSS) 카드
  // 어디를 잡아도 끌린다 — 모바일에서 위쪽 손잡이 띠만 노리기 어려웠다. 대신 끌지
  // 않고 톡 누르면(손을 뗄 때) 글쓰기로 들어간다.
  // dragNode = { id, el, pointerId, pointerType, additive, active, onGrip, selBefore,
  //              startClientX, startClientY, lastX, lastY, group: [{ node, el, startX, startY }] }
  viewport.addEventListener('pointerdown', (e) => {
    const nodeEl = e.target.closest('.board-node');
    if(!nodeEl) return;
    if(e.pointerType === 'mouse' && e.button !== 0) return;
    // 두 번째 손가락 → 핀치로(카드의 휴지통·연결 점·글칸 위에 닿았더라도)
    if(e.pointerType === 'touch'){
      if(dragNode){ pinchFromNodeDrag(e); return; }
      if(marquee){ pinchFromMarquee(e); return; }
      if(bgPointers.size){ addBoardPanPointer(e); return; }
    }
    if(dragNode || marquee) return;
    if(e.target.closest('.board-node-del, .board-node-handle, .board-frame-resize, .board-node-resize, .board-node-textarea, .board-gen-input, .board-gen-btn')) return;   // 삭제 버튼/연결 점/틀 크기 손잡이/글쓰기 중인 글칸/생성 카드 입력칸은 드래그 시작 안 함
    const id = Number(nodeEl.dataset.nodeId);
    if(!boardNodes.some(n => n.id === id)) return;
    const selBefore = new Set(boardSelectedIds);   // 핀치로 바뀌면 이걸로 되돌린다
    const additive = e.shiftKey || e.ctrlKey || e.metaKey;
    if(additive){
      const sel = new Set(boardSelectedIds);
      if(sel.has(id)) sel.delete(id); else sel.add(id);
      setBoardSelection(sel);
      if(!sel.has(id)) return;   // 방금 뺀 카드는 끌지 않는다
    }else if(!boardSelectedIds.has(id)){
      setBoardSelection(new Set([id]));
    }
    const world = document.getElementById('board-world');
    // 고른 것 중 묶음 틀이 있으면, 끌기를 시작하는 이 순간 그 틀 안에 완전히 든 카드(안쪽 틀
    // 포함)도 같이 옮긴다. 소속을 따로 저장하지 않고 위치로만 정한다 — 카드를 틀 밖으로
    // 끌어내면 그걸로 빠진다.
    const moveIds = new Set(boardSelectedIds);
    for(const f of boardNodes.filter(n => n.kind === 'frame' && boardSelectedIds.has(n.id))){
      for(const n of boardNodes) if(n.id !== f.id && boardNodeInside(n, f)) moveIds.add(n.id);
    }
    const group = boardNodes.filter(n => moveIds.has(n.id)).map(n => ({
      node: n, el: world.querySelector(`.board-node[data-node-id="${n.id}"]`), startX: n.x, startY: n.y,
    })).filter(g => g.el);
    dragNode = { id, el: nodeEl, pointerId: e.pointerId, pointerType: e.pointerType, additive, active: false,
                 onGrip: !!e.target.closest('.board-node-grip'), selBefore,
                 wasSole: selBefore.size === 1 && selBefore.has(id),
                 startClientX: e.clientX, startClientY: e.clientY, lastX: e.clientX, lastY: e.clientY, group };
    nodeEl.setPointerCapture(e.pointerId);
  });
  viewport.addEventListener('pointermove', (e) => {
    if(!dragNode || e.pointerId !== dragNode.pointerId) return;
    dragNode.lastX = e.clientX; dragNode.lastY = e.clientY;
    if(!dragNode.active){
      if(Math.hypot(e.clientX - dragNode.startClientX, e.clientY - dragNode.startClientY) < BOARD_DRAG_THRESHOLD_PX) return;
      dragNode.active = true;
      for(const g of dragNode.group) g.el.classList.add('dragging');
    }
    // 화면 픽셀 이동량을 지금 배율로 나눠 월드 좌표 이동량으로 바꾼다 — 확대돼
    // 있을 때 마우스를 조금만 움직여도 카드가 그만큼(화면상) 정확히 따라오게.
    const dx = (e.clientX - dragNode.startClientX) / boardState.scale;
    const dy = (e.clientY - dragNode.startClientY) / boardState.scale;
    for(const g of dragNode.group){
      g.node.x = g.startX + dx;
      g.node.y = g.startY + dy;
      g.el.style.left = g.node.x + 'px';
      g.el.style.top = g.node.y + 'px';
    }
    // 옮기는 카드에 붙은 선만 따라 움직인다(전체를 다시 그리지 않음)
    updateBoardEdgePaths(new Set(dragNode.group.map(g => g.node.id)));
  });
  const endBoardNodeDrag = (e) => {
    if(!dragNode || e.pointerId !== dragNode.pointerId) return;
    const d = dragNode;
    dragNode = null;
    for(const g of d.group) g.el.classList.remove('dragging');
    const moved = d.group.filter(g => g.node.x !== g.startX || g.node.y !== g.startY);
    if(e.type === 'pointerup' && !d.active && !d.additive){
      // 끌지 않고 톡 누름 — 여러 장 골라 둔 상태였으면 이 카드만 남긴다.
      if(boardSelectedIds.size > 1) setBoardSelection(new Set([d.id]));
      if(d.el.classList.contains('board-node-job')) openBoardJobDetail(d.id);   // 작업 카드 → 상세 창
      // 텍스트 카드 본문이면 글쓰기로(위쪽 손잡이 띠는 고르기만 — 글쓰기로 안 들어가야
      // 텍스트 카드도 골라서 Delete로 지울 수 있다). click이 아니라 여기서 한다 — 터치로
      // 뭔가를 끈 직후엔 브라우저가 click을 안 만들어 주는 경우가 있어서다. 터치의
      // pointerup은 사용자 동작으로 쳐 주므로 iOS에서도 여기서 준 포커스로 키보드가 뜬다.
      // 묶음 틀은 이미 이것 하나만 고른 상태에서 한 번 더 누르면 제목 고치기(처음 누름은 고르기만).
      if(d.el.classList.contains('board-frame') && d.wasSole && !d.el.classList.contains('editing')){
        d.el.classList.add('editing');
        const title = d.el.querySelector('.board-frame-title');
        title.focus();
        title.select();
      }
      if(!d.onGrip && d.el.classList.contains('board-node-text') && !d.el.classList.contains('editing')){
        d.el.classList.add('editing');
        const ta = d.el.querySelector('.board-node-textarea');
        ta.focus();
        ta.setSelectionRange(ta.value.length, ta.value.length);
      }
    }
    // 드래그가 끝났을 때 딱 한 번만(여러 장이어도 요청 하나로) 저장한다 — 실제로
    // 옮겼을 때만. 되돌리기 기록도 이 한 번의 이동을 한 칸으로 남긴다.
    if(moved.length){
      const items = moved.map(g => ({ id: g.node.id, from: { x: g.startX, y: g.startY }, to: { x: g.node.x, y: g.node.y } }));
      pushBoardHistory({ type: 'move', items });
      saveBoardPositions(items.map(it => ({ id: it.id, x: it.to.x, y: it.to.y })));
    }
  };
  for(const type of ['pointerup', 'pointercancel', 'pointerleave']) viewport.addEventListener(type, endBoardNodeDrag);

  // 크기 바꾸기(묶음 틀 · 이미지 · 영상 · 텍스트 · 생성 카드) — 오른쪽 아래 손잡이를 끈다. 끄는 동안 붙은 선이
  // 따라오고, 끝났을 때 한 번만 저장하고 되돌리기 기록을 남긴다. 최소 크기는 종류마다(BOARD_MIN_SIZE).
  let resize = null;   // { id, el, node, pointerId, startClientX, startClientY, startW, startH }
  viewport.addEventListener('pointerdown', (e) => {
    const handle = e.target.closest('.board-frame-resize, .board-node-resize');
    if(!handle) return;
    if(e.pointerType === 'mouse' && e.button !== 0) return;
    if(dragNode || marquee || bgPointers.size || resize) return;
    const el = handle.closest('.board-node');
    const node = boardNodes.find(n => n.id === Number(el.dataset.nodeId));
    if(!node) return;
    e.preventDefault();
    resize = { id: node.id, el, node, pointerId: e.pointerId, startClientX: e.clientX, startClientY: e.clientY,
               startW: node.width, startH: node.height };
    handle.setPointerCapture(e.pointerId);
  });
  viewport.addEventListener('pointermove', (e) => {
    if(!resize || e.pointerId !== resize.pointerId) return;
    const min = resize.node.kind === 'gen' ? boardGenMinSize(resize.node) : (BOARD_MIN_SIZE[resize.node.kind] || BOARD_MIN_SIZE.image);
    resize.node.width = Math.max(min.w, resize.startW + (e.clientX - resize.startClientX) / boardState.scale);
    resize.node.height = Math.max(min.h, resize.startH + (e.clientY - resize.startClientY) / boardState.scale);
    resize.el.style.width = resize.node.width + 'px';
    resize.el.style.height = resize.node.height + 'px';
    if(resize.node.kind !== 'frame') updateBoardEdgePaths(resize.node.id);   // 틀은 선이 없다
  });
  const endBoardResize = (e) => {
    if(!resize || e.pointerId !== resize.pointerId) return;
    const r = resize;
    resize = null;
    if(r.node.width === r.startW && r.node.height === r.startH) return;
    const size = { width: r.node.width, height: r.node.height };
    pushBoardHistory({ type: 'resize', id: r.id, from: { width: r.startW, height: r.startH }, to: size });
    saveBoardNodeSize(r.id, size);
  };
  for(const type of ['pointerup', 'pointercancel']) viewport.addEventListener(type, endBoardResize);

  // 선 잇기 — 카드의 연결 점을 누르고 끌면 점선이 커서를 따라오고, 다른 카드 위에서
  // 놓으면 그 둘을 잇는다. 연결 점에 포인터 캡처를 걸어 두므로 놓은 곳의 카드는
  // elementFromPoint로 찾는다(캡처 중에는 이벤트 target이 늘 연결 점이라서).
  let connect = null;   // { fromId, pointerId, targetEl }
  viewport.addEventListener('pointerdown', (e) => {
    const handle = e.target.closest('.board-node-handle');
    if(!handle) return;
    if(e.pointerType === 'mouse' && e.button !== 0) return;
    if(dragNode || marquee || bgPointers.size) return;   // 이미 다른 손가락이 캔버스를 잡고 있으면 핀치 쪽 몫
    e.preventDefault();
    selectBoardEdge(null);
    connect = { fromId: Number(handle.closest('.board-node').dataset.nodeId), pointerId: e.pointerId, targetEl: null };
    handle.setPointerCapture(e.pointerId);
    viewport.classList.add('connecting');
    updateBoardTempEdge(connect.fromId, e.clientX, e.clientY);
  });
  const boardNodeUnder = (clientX, clientY) => {
    const el = document.elementFromPoint(clientX, clientY);
    const nodeEl = el && el.closest('.board-node');
    if(nodeEl && nodeEl.classList.contains('board-frame')) return null;   // 묶음 틀은 선으로 잇지 않는다
    return nodeEl && viewport.contains(nodeEl) && Number(nodeEl.dataset.nodeId) !== connect.fromId ? nodeEl : null;
  };
  // 생성 카드 위에서 놓을 때 어느 입구로 갈지 — 손가락/커서 아래 입구 줄, 없으면 첫 빈 입구(다 차 있으면 첫 입구).
  // 출발이 이미지 카드가 아니거나 입구가 없는 카드면 보통 선(null).
  const boardSlotFor = (targetEl, clientX, clientY) => {
    const target = targetEl && boardNodes.find(n => n.id === Number(targetEl.dataset.nodeId));
    const source = boardNodes.find(n => n.id === connect.fromId);
    const slots = (target && target.kind === 'gen' && target.data && target.data.slots) || [];
    if(!slots.length || !source || source.kind !== 'image') return null;
    const under = document.elementFromPoint(clientX, clientY);
    const row = under && under.closest('.board-gen-slot');
    if(row && targetEl.contains(row)) return row.dataset.genSlot;
    const filled = new Set(boardEdges.filter(e => e.to_node_id === target.id && e.to_slot).map(e => e.to_slot));
    return (slots.find(sl => !filled.has(sl.name)) || slots[0]).name;
  };
  viewport.addEventListener('pointermove', (e) => {
    if(!connect || e.pointerId !== connect.pointerId) return;
    updateBoardTempEdge(connect.fromId, e.clientX, e.clientY);
    const targetEl = boardNodeUnder(e.clientX, e.clientY);
    if(targetEl !== connect.targetEl){
      if(connect.targetEl) connect.targetEl.classList.remove('connect-target', 'connect-no-slot');
      if(targetEl){
        targetEl.classList.add('connect-target');
        // 입구가 있는 생성 카드에 이미지 아닌 카드를 끌어 오면 입구 대신 보통 선이 된다고 알린다
        const target = boardNodes.find(n => n.id === Number(targetEl.dataset.nodeId));
        const source = boardNodes.find(n => n.id === connect.fromId);
        if(target && target.kind === 'gen' && ((target.data && target.data.slots) || []).length && source && source.kind !== 'image'){
          targetEl.classList.add('connect-no-slot');
        }
      }
      connect.targetEl = targetEl;
    }
    const slot = boardSlotFor(targetEl, e.clientX, e.clientY);
    document.querySelectorAll('#board-world .board-gen-slot.connect-slot').forEach(r => {
      if(!(targetEl && r.closest('.board-node') === targetEl && r.dataset.genSlot === slot)) r.classList.remove('connect-slot');
    });
    if(targetEl && slot){
      const row = targetEl.querySelector(`.board-gen-slot[data-gen-slot="${CSS.escape(slot)}"]`);
      if(row) row.classList.add('connect-slot');
    }
  });
  const endBoardConnect = (e) => {
    if(!connect || e.pointerId !== connect.pointerId) return;
    const targetEl = e.type === 'pointerup' ? boardNodeUnder(e.clientX, e.clientY) : null;
    const slot = targetEl ? boardSlotFor(targetEl, e.clientX, e.clientY) : null;
    if(connect.targetEl) connect.targetEl.classList.remove('connect-target', 'connect-no-slot');
    document.querySelectorAll('#board-world .board-gen-slot.connect-slot').forEach(r => r.classList.remove('connect-slot'));
    const fromId = connect.fromId;
    connect = null;
    viewport.classList.remove('connecting');
    const temp = document.getElementById('board-edge-temp');
    if(temp) temp.setAttribute('d', '');
    if(targetEl) createBoardEdge(fromId, Number(targetEl.dataset.nodeId), slot);
  };
  for(const type of ['pointerup', 'pointercancel']) viewport.addEventListener(type, endBoardConnect);

  // 카드 삭제 · 선 끊기 · 텍스트 카드 저장(포커스 벗어날 때) — innerHTML로 카드를
  // 통째로 다시 그리므로 델리게이트로 한 번만 건다.
  viewport.addEventListener('click', (e) => {
    const delBtn = e.target.closest('.board-node-del');
    if(delBtn) deleteBoardNodes([Number(delBtn.dataset.boardDel)]);
    const edgeDelBtn = e.target.closest('.board-edge-del');
    if(edgeDelBtn) deleteBoardEdge(Number(edgeDelBtn.dataset.edgeDel));
    const runBtn = e.target.closest('[data-gen-run]');
    if(runBtn && !runBtn.disabled) runBoardGen(Number(runBtn.dataset.genRun));
    const detailBtn = e.target.closest('[data-gen-detail]');
    if(detailBtn) openBoardGenDetail(Number(detailBtn.dataset.genDetail));
  });
  // 보드 단축키 — 글을 쓰는 중이면(글칸·입력칸) 전부 그 칸의 기본 동작(글자 지우기,
  // 글자 되돌리기, 전체 선택)에 양보한다. 모달이 떠 있을 때도 건드리지 않는다.
  //   Ctrl/⌘+Z 되돌리기 · Ctrl/⌘+Shift+Z 또는 Ctrl+Y 다시 하기 · Ctrl/⌘+A 카드 전체 선택
  //   Delete/Backspace 고른 선(없으면 고른 카드) 지우기 · Esc 선택 해제
  document.addEventListener('keydown', (e) => {
    if(currentTab !== 'prboard') return;
    if(e.target.closest && e.target.closest('input, textarea, select, [contenteditable]')) return;
    if([...document.querySelectorAll('.modal-overlay')].some(m => m.style.display && m.style.display !== 'none')) return;
    const mod = e.ctrlKey || e.metaKey;
    const key = e.key.toLowerCase();
    if(mod && key === 'z'){ e.preventDefault(); boardUndoRedo(e.shiftKey ? 'redo' : 'undo'); return; }
    if(mod && key === 'y'){ e.preventDefault(); boardUndoRedo('redo'); return; }
    if(mod && key === 'a'){ e.preventDefault(); setBoardSelection(new Set(boardNodes.map(n => n.id))); return; }
    if(e.key === 'Delete' || e.key === 'Backspace'){
      if(boardSelectedEdgeId !== null){ e.preventDefault(); deleteBoardEdge(boardSelectedEdgeId); }
      else if(boardSelectedIds.size){ e.preventDefault(); deleteBoardNodes([...boardSelectedIds]); }
    }else if(e.key === 'Escape'){
      selectBoardEdge(null);
      setBoardSelection(new Set());
    }
  });
  document.getElementById('board-undo-btn').addEventListener('click', () => boardUndoRedo('undo'));
  document.getElementById('board-redo-btn').addEventListener('click', () => boardUndoRedo('redo'));
  document.getElementById('board-select-mode-btn').addEventListener('click', (e) => {
    boardSelectMode = !boardSelectMode;
    e.currentTarget.setAttribute('aria-pressed', String(boardSelectMode));
  });
  document.getElementById('board-delete-selected-btn').addEventListener('click', () => deleteBoardNodes([...boardSelectedIds]));
  document.getElementById('board-clear-selection-btn').addEventListener('click', () => setBoardSelection(new Set()));
  // 글은 쓰다가 잠깐 멈추면(디바운스) 저장하고, 글칸을 벗어날 때는 바로 저장한다 —
  // 포커스를 안 뺀 채 새로고침/앱 전환해도 글이 날아가지 않게.
  viewport.addEventListener('input', (e) => {
    const textarea = e.target.closest('.board-node-textarea');
    if(textarea) scheduleBoardTextSave(Number(textarea.dataset.boardText), textarea.value);
  });
  // 글쓰기 한 번(글칸에 들어가서 나올 때까지)을 되돌리기 기록 한 칸으로 남긴다 — 글자마다
  // 남기면 Ctrl+Z를 수십 번 눌러야 한다. 글칸 안에서의 글자 단위 되돌리기는 브라우저 몫.
  let editStart = null;   // { id, text }
  viewport.addEventListener('focusin', (e) => {
    const textarea = e.target.closest('.board-node-textarea');
    if(textarea) editStart = { id: Number(textarea.dataset.boardText), text: textarea.value };
  });
  viewport.addEventListener('focusout', (e) => {
    const textarea = e.target.closest('.board-node-textarea');
    if(!textarea) return;
    textarea.closest('.board-node').classList.remove('editing');
    const id = Number(textarea.dataset.boardText);
    if(editStart && editStart.id === id && editStart.text !== textarea.value){
      pushBoardHistory({ type: 'text', id, from: editStart.text, to: textarea.value });
    }
    editStart = null;
    flushBoardTextSave(id, textarea.value);
  });
  // 글쓰기 중 Esc는 글쓰기만 끝낸다(카드는 고른 채로) — 이어서 Delete로 카드를 지우거나
  // 방향을 바꿀 수 있게. 위 보드 단축키는 글칸 안의 키를 전부 양보하므로 여기서 따로 받는다.
  viewport.addEventListener('keydown', (e) => {
    if(e.key === 'Escape' && e.target.closest('.board-node-textarea')){ e.preventDefault(); e.target.blur(); }
    if(e.key === 'Enter' && e.target.closest('.board-frame-title')){ e.preventDefault(); e.target.blur(); }
  });
  // 생성 카드의 꺼낸 옵션 — input은 저장 예약, change(칸을 벗어나거나 고르기를 마침)는 바로 저장 +
  // 되돌리기 기록. focusin에서 고치기 전 값을 기억해 둔다.
  const genStart = new Map();   // "노드id:옵션이름" -> 고치기 전 값
  viewport.addEventListener('focusin', (e) => {
    const el = e.target.closest('.board-gen-input');
    if(el) genStart.set(`${el.dataset.genNode}:${el.dataset.genField}`, el.value);
  });
  viewport.addEventListener('input', (e) => {
    const el = e.target.closest('.board-gen-input');
    if(!el) return;
    const nodeId = Number(el.dataset.genNode);
    setBoardGenValueLocal(nodeId, el.dataset.genField, el.value);
    scheduleBoardGenSave(nodeId, el.dataset.genField, el.value);
  });
  viewport.addEventListener('change', (e) => {
    const el = e.target.closest('.board-gen-input');
    if(!el) return;
    const nodeId = Number(el.dataset.genNode), name = el.dataset.genField, key = `${nodeId}:${name}`;
    setBoardGenValueLocal(nodeId, name, el.value);
    scheduleBoardGenSave(nodeId, name, el.value);
    flushBoardGenSave(key);
    const from = genStart.has(key) ? genStart.get(key) : el.value;
    if(from !== el.value) pushBoardHistory({ type: 'gen-value', id: nodeId, name, from, to: el.value });
    genStart.set(key, el.value);
  });
  // 모바일은 앱을 전환하면 탭이 언제든 정리될 수 있다 — 화면이 가려지는 순간 쓰던 글을 보낸다.
  document.addEventListener('visibilitychange', () => {
    if(document.visibilityState === 'hidden'){ flushAllBoardTextSaves(true); flushAllBoardGenSaves(true); }
  });

  document.getElementById('board-add-text-btn').addEventListener('click', () => createBoardNode('text', null, ''));
  document.getElementById('board-add-image-btn').addEventListener('click', () => openBoardAssetPicker('image'));
  document.getElementById('board-add-video-btn').addEventListener('click', () => openBoardAssetPicker('video'));
  document.getElementById('board-add-job-btn').addEventListener('click', () => openBoardAssetPicker('job'));
  document.getElementById('board-add-gen-btn').addEventListener('click', () => openBoardAssetPicker('gen'));
  document.getElementById('board-add-frame-btn').addEventListener('click', createBoardFrame);

  applyBoardTransform();
}

// ---- 프로젝트 보드 — 카드(노드): 서버 저장/복원, 드래그 이동, 삭제 ----
let boardNodes = [];
let boardEdges = [];               // { id, from_node_id, to_node_id }
let boardSelectedEdgeId = null;    // 클릭해서 고른 선(끊기 대상)
let boardSelectedIds = new Set();  // 고른 카드들(여러 장 옮기기/지우기 대상)
let boardSelectMode = false;       // 툴바 "선택 모드" — 켜면 배경 끌기가 팬 대신 사각형 고르기

// 카드 고르기 — 카드마다 .selected를 칠하고, 캔버스 아래 선택 막대(개수·지우기·해제)를
// 보이거나 숨긴다. 보드를 다시 그리거나 카드를 붙인 뒤에도 불러서 표시를 맞춘다.
function setBoardSelection(ids){
  boardSelectedIds = ids;
  paintBoardSelection();
}
function paintBoardSelection(){
  const world = document.getElementById('board-world');
  if(!world) return;
  for(const el of world.querySelectorAll('.board-node')){
    el.classList.toggle('selected', boardSelectedIds.has(Number(el.dataset.nodeId)));
  }
  const bar = document.getElementById('board-selection-bar');
  bar.style.display = boardSelectedIds.size ? 'flex' : 'none';
  document.getElementById('board-selection-count').textContent = `${boardSelectedIds.size}개 선택`;
}

// 헤더/내비/프로젝트 바는 그대로 두고 main만 바뀌므로, showTab()이 prboard로 올
// 때마다(다른 탭 갔다 오거나 프로젝트를 바꿔도) 그 프로젝트의 보드를 다시 받는다.
let boardFetchSeq = 0;   // 프로젝트를 빠르게 바꿀 때 늦게 도착한 옛 응답을 버리는 용도

async function fetchBoard(){
  const seq = ++boardFetchSeq;
  const grid = document.getElementById('board-world');
  boardSelectedEdgeId = null;
  // 다시 받으면 화면이 서버 상태로 새로 맞춰지므로, 그 전 상태를 전제로 한 선택·되돌리기
  // 기록은 버린다(다른 프로젝트로 옮겼거나, 저장 실패로 다시 맞추는 경우 모두).
  setBoardSelection(new Set());
  resetBoardHistory();
  if(typeof currentProjectId !== 'number'){
    // "미분류"는 진짜 프로젝트 행이 아니라 board_nodes.project_id(FK, NOT NULL)를
    // 붙일 곳이 없다 — 보드 개념 자체가 성립하지 않는다.
    boardNodes = [];
    boardEdges = [];
    if(grid) grid.innerHTML = '<div class="empty board-empty-notice">보드는 프로젝트 안에서만 쓸 수 있어요 — "미분류"에는 없어요.</div>';
    return;
  }
  // 아직 안 보낸 글이 있으면 먼저 보낸다 — 안 그러면 다시 그린 글칸에 옛 글이 뜬다.
  await flushAllBoardTextSaves(false);
  let data = null;
  try{
    const res = await fetch(`/api/projects/${currentProjectId}/board`);
    if(res.ok) data = await res.json();
  }catch(e){ /* 아래에서 실패로 처리 */ }
  if(seq !== boardFetchSeq) return;
  if(!data){
    // 옛 카드를 그대로 두면 다른 프로젝트의 카드가 이 프로젝트 것처럼 보이고, 그걸
    // 고치면 엉뚱한 주소로 저장을 시도하게 된다 — 비우고 알린다.
    boardNodes = [];
    boardEdges = [];
    if(grid) grid.innerHTML = '<div class="empty board-empty-notice">보드를 불러오지 못했어요. 잠시 뒤 다시 열어 주세요.</div>';
    return;
  }
  boardNodes = data.nodes || [];
  boardEdges = data.edges || [];
  boardJobInfo.clear();
  renderBoard();
  fetchBoardJobs();
}

// ---- 프로젝트 보드 — 작업(Job) 카드 ----
// 카드는 작업 id만 들고 있고, 상태·진행률·최근 결과물은 GET .../board/jobs로 따로 받는다
// (boardJobInfo: 카드 id -> 그 응답). 대기·실행 중인 작업이 하나라도 있으면 그동안만
// BOARD_JOB_POLL_MS마다 다시 받고, 다 끝나면 멈춘다. 보드 탭을 떠나도 멈춘다.
const BOARD_JOB_POLL_MS = 4000;
const boardJobInfo = new Map();
let boardJobTimer = null;
let boardJobSeq = 0;

function boardJobBodyHtml(info){
  if(!info) return '<div class="board-job-note">불러오는 중…</div>';
  if(info.missing) return `<div class="board-job-note">${ico('trash-2')}<span>지워진 작업이에요</span></div>`;
  const head = `<div class="board-job-head"><span class="badge ${escapeHtml(info.status || '')}"><span class="bdot"></span>`
    + `${STATUS_LABEL[info.status] || escapeHtml(info.status || '')}</span>`
    + `<span class="board-job-label">${escapeHtml(info.template_label || '')}</span></div>`;
  const promptText = escapeHtml(info.prompt || '') || '<span class="board-job-dim">프롬프트 없음</span>';
  const prompt = `<div class="board-job-prompt">${promptText}</div>`;
  const pr = info.progress;
  const progress = pr && pr.total
    ? `<div class="board-job-progress"><div class="board-job-bar"><div style="width:${Math.min(100, Math.round(pr.done / pr.total * 100))}%"></div></div>`
      + `<span>${pr.done}/${pr.total}</span></div>`
    : '';
  // 결과 미리보기 — NSFW 설정은 갤러리/카드와 같게: 안 보기는 빼고, 블러는 가림막만.
  const results = (info.results || []).filter(r => !(r.nsfw && nsfwMode === 'hide'));
  const thumbs = results.map(r => {
    if(r.nsfw && nsfwMode === 'blur') return `<div class="board-job-thumb nsfw">${ico('eye-off')}</div>`;
    if(r.kind === 'video') return `<div class="board-job-thumb video">${ico('clapperboard')}</div>`;
    const url = window.__nightshiftMediaUrl(`/api/output-images/${encodeURIComponent(r.path)}/thumbnail?size=120&fit=cover`);
    return `<div class="board-job-thumb"><img src="${url}" loading="lazy" alt=""></div>`;
  }).join('');
  return head + prompt + progress + (thumbs ? `<div class="board-job-thumbs">${thumbs}</div>` : '');
}

// 받은 정보로 작업 카드 안쪽만 고친다 — 바뀐 게 없으면 손대지 않아 썸네일이 깜빡이지 않는다.
function paintBoardJobCards(){
  for(const el of document.querySelectorAll('#board-world .board-node-job')){
    const body = el.querySelector('.board-job-body');
    const html = boardJobBodyHtml(boardJobInfo.get(Number(el.dataset.nodeId)));
    if(body && body._html !== html){ body.innerHTML = html; body._html = html; }
  }
  for(const el of document.querySelectorAll('#board-world .board-node-gen')){
    const id = Number(el.dataset.nodeId);
    const status = el.querySelector('.board-gen-status');
    const html = boardGenStatusHtml(boardJobInfo.get(id), boardNodes.find(n => n.id === id));
    if(status && status._html !== html){ status.innerHTML = html; status._html = html; }
  }
  paintBoardGenSlots();   // 실행 버튼(대기·실행 중이면 막힘)도 상태에 맞춘다
}

async function fetchBoardJobs(){
  clearTimeout(boardJobTimer);
  boardJobTimer = null;
  const projectId = currentProjectId;
  if(typeof projectId !== 'number' || currentTab !== 'prboard') return;
  // 작업 카드, 또는 한 번이라도 실행한 생성 카드가 있을 때만
  if(!boardNodes.some(n => n.kind === 'job' || (n.kind === 'gen' && n.data && (n.data.runs || []).length))) return;
  const seq = ++boardJobSeq;
  let data = null;
  try{
    const res = await fetch(`/api/projects/${projectId}/board/jobs`);
    if(res.ok) data = await res.json();
  }catch(e){ /* 잠깐의 네트워크 문제 — 아래에서 다음 차례에 다시 받는다 */ }
  if(seq !== boardJobSeq || projectId !== currentProjectId || currentTab !== 'prboard') return;
  if(data){
    for(const [id, info] of Object.entries(data.cards || {})) boardJobInfo.set(Number(id), info);
    paintBoardJobCards();
    // 생성 카드에 아직 안 펼친 결과가 있으면 보드에 펼친다(자동).
    for(const [id, info] of Object.entries(data.cards || {})) if(info.pending_spread > 0) spreadBoardGen(Number(id));
  }
  // 대기·실행 중이거나, 막 끝나 결과가 늦게 넘어올 수 있거나, 펼칠 결과가 남은 동안은 계속 지켜본다.
  const anyActive = !data || [...boardJobInfo.values()].some(info => info.active || info.settling || info.pending_spread > 0);
  if(anyActive) boardJobTimer = setTimeout(fetchBoardJobs, BOARD_JOB_POLL_MS);
}

// 작업 카드를 톡 누르면 작업 목록 화면과 같은 상세 창을 연다. 그 창은 화면이 받아 둔 작업
// 목록(lastJobs)에서 작업을 찾으므로, 보드에서 열 때는 목록을 한 번 새로 받는다.
async function openBoardJobDetail(nodeId){
  const info = boardJobInfo.get(nodeId);
  if(!info || info.missing || !info.job_id){ flashNotice('지워진 작업이라 열 수 없어요.'); return; }
  await fetchJobs();
  if(!lastJobs.some(j => j.id === info.job_id)){ flashNotice('작업을 찾을 수 없어요.'); return; }
  openJobDetailModal(info.job_id);
}

