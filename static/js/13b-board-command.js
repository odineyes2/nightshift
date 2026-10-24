// 퀵바 — 보드 탭의 `/` 명령창에서 카드를 만들고 입력 기록과 완성을 관리한다.
const BOARD_COMMANDS = [
  { cmd: 'text', aliases: ['t'], description: '메모 카드', example: '/text 내용' },
  { cmd: 'frame', aliases: ['f'], description: '묶음 틀', example: '/frame 이름' },
  { cmd: 'job', aliases: [], description: '작업 카드', example: '/job 작업ID' },
  { cmd: 'gen', aliases: ['g'], description: '프리셋으로 생성 카드', example: '/gen 프리셋 프롬프트' },
  { cmd: 'image', aliases: ['img', 'i'], description: '빈 이미지 카드 · 검색어가 있으면 이미지 고르기', example: '/image [검색어]' },
  { cmd: 'video', aliases: ['v'], description: '빈 영상 카드 · 검색어가 있으면 영상 고르기', example: '/video [검색어]' },
  { cmd: 'run', aliases: ['퀵런'], description: '퀵런 열기', example: '/run [글]' },
  { cmd: 'exit', aliases: ['q'], description: '퀵바 닫기', example: '/exit' },
  { cmd: 'help', aliases: ['?'], description: '명령 도움말', example: '/help' },
];
const BOARD_COMMAND_HISTORY_KEY = 'nightshift.boardCommands';
const BOARD_COMMAND_OPEN_KEY = 'nightshift.quickbarOpen';
let boardCommandOpen = false;
try{ boardCommandOpen = localStorage.getItem(BOARD_COMMAND_OPEN_KEY) === 'true'; }catch(e){ /* 저장소가 없으면 기본값을 쓴다. */ }
let boardCommandPoint = null;
let boardCommandNext = null;
let boardCommandBusy = false;
let boardCommandChoices = [];
let boardCommandChoice = 0;
let boardGenPresets = null;
let boardPromptOptions = null;
let boardCommandHistory = [];
try{
  const saved = JSON.parse(localStorage.getItem(BOARD_COMMAND_HISTORY_KEY) || '[]');
  if(Array.isArray(saved)) boardCommandHistory = saved.filter(x => typeof x === 'string').slice(-50);
}catch(e){ /* 저장소를 읽지 못하면 이번 화면의 기록만 쓴다. */ }
let boardCommandHistoryIndex = boardCommandHistory.length;
let boardCommandDraft = '';
let boardCommandPaste = null;

function boardCommandPasteLines(value){
  return value.split(/\r\n|\r|\n/).map(line => line.trim().replace(/^(?:[-*]\s+|\d+\.\s+)/, '').trim()).filter(Boolean);
}

function boardCommandPasteChoice(){
  let choice = document.getElementById('board-command-paste-choice');
  if(!choice){
    choice = document.createElement('div');
    choice.id = 'board-command-paste-choice';
    choice.className = 'board-command-paste-choice';
    choice.setAttribute('role', 'group');
    choice.setAttribute('aria-label', '여러 줄 붙여넣기 방식');
    choice.innerHTML = '<button type="button" data-paste-choice="many"></button><button type="button" data-paste-choice="one">한 장으로 붙여넣기 (Esc)</button>';
    document.getElementById('board-command-input').before(choice);
  }
  choice.hidden = !boardCommandPaste;
  if(boardCommandPaste) choice.querySelector('[data-paste-choice="many"]').textContent = `줄마다 메모 ${boardCommandPaste.lines.length}장 만들기 (Enter)`;
}

function boardCommandClearPaste(){
  boardCommandPaste = null;
  boardCommandPasteChoice();
}

function parseBoardCommand(text){
  const raw = text.trim();
  if(!raw) return { error: '내용을 입력해 주세요.' };
  if(raw.startsWith('//')) return { cmd: 'text', args: raw.slice(1) };
  if(raw === '?') return { cmd: 'help', args: '' };
  if(!raw.startsWith('/')) return { cmd: 'text', args: raw };
  const match = /^\/([^\s]+)(?:\s+([\s\S]*))?$/.exec(raw);
  const name = match ? match[1].toLowerCase() : '';
  const command = BOARD_COMMANDS.find(c => c.cmd === name || c.aliases.includes(name));
  if(!command) return { error: `/${name}: 모르는 명령이에요. /help로 목록을 볼 수 있어요. /로 시작하는 메모는 //로 입력해요.` };
  return { cmd: command.cmd, args: (match[2] || '').trim() };
}

function boardCommandRememberPoint(clientX, clientY){
  const rect = document.getElementById('board-viewport').getBoundingClientRect();
  boardCommandPoint = {
    x: (clientX - rect.left - boardState.x) / boardState.scale,
    y: (clientY - rect.top - boardState.y) / boardState.scale,
  };
  boardCommandNext = null;
}

function boardCommandGeometry(kind){
  const width = kind === 'frame' ? 480 : kind === 'job' ? 260 : 220;
  const height = kind === 'frame' ? 320 : kind === 'text' ? 160 : 220;
  const center = boardCommandPoint || boardViewportCenterWorld();
  const position = boardCommandNext || { x: center.x - width / 2, y: center.y - height / 2 };
  return { x: position.x, y: position.y, width, height };
}

function boardCommandAdvance(node){
  const rect = document.getElementById('board-viewport').getBoundingClientRect();
  const right = (rect.width - boardState.x) / boardState.scale;
  const nextX = node.x + node.width + 24;
  boardCommandNext = nextX + 220 <= right
    ? { x: nextX, y: node.y }
    : { x: (boardCommandPoint || boardViewportCenterWorld()).x - 110, y: node.y + node.height + 24 };
}

function boardCommandError(message){ document.getElementById('board-command-error').textContent = message || ''; }
function boardCommandFinish(value){
  const input = document.getElementById('board-command-input');
  boardCommandHistory.push(value);
  boardCommandHistory = boardCommandHistory.slice(-50);
  try{ localStorage.setItem(BOARD_COMMAND_HISTORY_KEY, JSON.stringify(boardCommandHistory)); }catch(e){ /* 이번 화면 기록은 유지한다. */ }
  boardCommandHistoryIndex = boardCommandHistory.length;
  boardCommandDraft = '';
  input.value = '';
  boardCommandClearPaste();
  boardCommandPaint();
}
function boardCommandFocus(){
  if(currentTab !== 'prboard') return;
  boardCommandSetOpen(true);
  const input = document.getElementById('board-command-input');
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}
function boardCommandSyncVisibility(){
  const composerOpen = !document.getElementById('composer').hidden;
  document.getElementById('board-command').hidden = !boardCommandOpen || composerOpen;
  document.getElementById('board-command-open').hidden = boardCommandOpen || composerOpen;
}
function boardCommandSetOpen(open){
  if(open && !boardCommandOpen) boardGenPresets = null;
  boardCommandOpen = open;
  try{ localStorage.setItem(BOARD_COMMAND_OPEN_KEY, String(open)); }catch(e){ /* 이번 화면의 상태는 유지한다. */ }
  if(!open && document.getElementById('board-command').contains(document.activeElement)) document.activeElement.blur();
  boardCommandSyncVisibility();
  boardCommandPaint();
}

function boardCommandPlaceAboveKeyboard(){
  const viewport = document.getElementById('board-viewport');
  const command = document.getElementById('board-command');
  if(!window.visualViewport || !viewport || !command) return;
  const bottom = viewport.getBoundingClientRect().bottom;
  const keyboardTop = window.visualViewport.offsetTop + window.visualViewport.height;
  command.style.bottom = Math.max(8, bottom - keyboardTop + 8) + 'px';
}
if(window.visualViewport){
  window.visualViewport.addEventListener('resize', boardCommandPlaceAboveKeyboard);
  window.visualViewport.addEventListener('scroll', boardCommandPlaceAboveKeyboard);
}

function boardCommandComplete(){
  const input = document.getElementById('board-command-input');
  const choice = boardCommandChoices[boardCommandChoice];
  if(!choice) return false;
  if(choice.complete){
    input.value = choice.complete;
    input.focus();
    boardCommandPaint();
    return true;
  }
  const prefix = /^\/([^\s]*)$/.exec(input.value);
  const name = prefix && prefix[1] && choice.aliases.find(alias => alias.startsWith(prefix[1].toLowerCase())) || choice.cmd;
  input.value = '/' + name + ' ';
  input.focus();
  boardCommandPaint();
  return true;
}

function boardCommandPaint(){
  const input = document.getElementById('board-command-input');
  const menu = document.getElementById('board-command-menu');
  const value = input.value;
  const prefix = /^\/([^\s]*)$/.exec(value);
  boardCommandChoices = prefix ? BOARD_COMMANDS.filter(c => [c.cmd, ...c.aliases].some(name => name.startsWith(prefix[1].toLowerCase()))) : [];
  const gen = /^\/(?:gen|g)\s+([\s\S]*)$/i.exec(value);
  if(gen){
    if(!boardGenPresets) boardCommandLoadGenPresets();
    const option = /(^|\s)--([\w-]*)$/.exec(gen[1]);
    if(option){
      if(!boardPromptOptions) boardCommandLoadPromptOptions();
      boardCommandChoices = (boardPromptOptions || []).filter(o => [o.name, ...(o.aliases || [])].some(n => n.startsWith(option[2].toLowerCase())))
        .map(o => ({ cmd: o.name, example: o.example, description: o.description,
          complete: value.slice(0, value.length - option[2].length) + o.name + ' ' }));
    }else if(!/\s/.test(gen[1].trim()) && boardGenPresets){
      const term = gen[1].trim().replace(/^"/, '').toLowerCase();
      boardCommandChoices = boardGenPresets.filter(p => p.name.toLowerCase().startsWith(term)).map(p => ({
        cmd: String(p.id), example: p.name, description: p.template_label || p.template_id,
        complete: '/gen ' + (/\s/.test(p.name) ? `"${p.name}"` : p.name) + ' ',
      }));
    }else boardCommandChoices = [];
  }
  boardCommandChoice = Math.min(boardCommandChoice, Math.max(0, boardCommandChoices.length - 1));
  menu.hidden = !boardCommandChoices.length || document.activeElement !== input;
  menu.innerHTML = boardCommandChoices.map((c, i) => {
    const name = prefix && prefix[1] && c.aliases && c.aliases.find(alias => alias.startsWith(prefix[1].toLowerCase())) || c.cmd;
    return `<button type="button" role="option" aria-selected="${i === boardCommandChoice}" data-command="${escapeHtml(c.cmd)}"><b>${escapeHtml(name === c.cmd ? c.example : '/' + name)}</b><span>${escapeHtml(c.description || '')}</span></button>`;
  }).join('');
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 6 * 23) + 'px';
  boardCommandError('');
  if(boardCommandPaste && boardCommandPaste.lines.length > 50)
    boardCommandError(`50줄까지 한 번에 만들 수 있어요(지금 ${boardCommandPaste.lines.length}줄).`);
}

async function boardCommandRunPaste(){
  if(!boardCommandPaste || boardCommandBusy) return;
  const lines = boardCommandPaste.lines;
  if(lines.length > 50){ boardCommandPaint(); return; }
  const projectId = currentProjectId, boardId = currentBoardId;
  if(typeof projectId !== 'number') return;
  const value = document.getElementById('board-command-input').value;
  const geom = boardCommandGeometry('text');
  const nodes = [];
  boardCommandBusy = true;
  try{
    for(const [i, line] of lines.entries()){
      if(!boardScopeMatches(projectId, boardId)) break;
      const node = await createBoardNode('text', null, line, {
        x: geom.x + (i % 4) * 240, y: geom.y + Math.floor(i / 4) * 184,
        width: 220, height: 160,
      }, false);
      if(!node) break;
      nodes.push(node);
    }
  }catch(e){ /* 이미 만든 카드는 한 번에 되돌릴 수 있게 아래에서 기록한다. */ }
  finally{ boardCommandBusy = false; }
  if(!boardScopeMatches(projectId, boardId)) return;
  if(nodes.length){
    setBoardSelection(new Set(nodes.map(node => node.id)));
    pushBoardHistory({ type: 'paste', nodes: nodes.map(boardNodeSnapshot), edges: [] });
    boardCommandAdvance(nodes[nodes.length - 1]);
    boardCommandFinish(value);
  }
  if(nodes.length < lines.length) boardCommandError(`${lines.length}장 중 ${nodes.length}장을 만들었어요.`);
}

async function boardCommandLoadGenPresets(){
  boardGenPresets = [];
  try{
    const res = await fetch('/api/board-presets');
    if(res.ok) boardGenPresets = (await res.json()).presets || [];
  }catch(e){ /* 프리셋 조회 실패는 실행할 때 오류로 알린다. */ }
  boardCommandPaint();
}

async function boardCommandLoadPromptOptions(){
  boardPromptOptions = [];
  try{
    const res = await fetch('/api/prompt-options/spec');
    if(res.ok) boardPromptOptions = (await res.json()).options || [];
  }catch(e){ /* 옵션 목록을 읽지 못해도 원문 입력은 가능하다. */ }
  boardCommandPaint();
}

function boardCommandGenArgs(args){
  const match = /^(?:"([^"]+)"|(\S+))(?:\s+([\s\S]*))?$/.exec(args);
  return match ? { name: match[1] || match[2], prompt: match[3] || '' } : null;
}

function boardCommandFindGenPreset(presets, name){
  return presets.filter(p => p.name.toLowerCase().startsWith(name.toLowerCase()));
}

function boardCommandHelp(){
  const modal = document.getElementById('board-command-help');
  document.getElementById('board-command-help-list').innerHTML = BOARD_COMMANDS.map(c =>
    `<li><code>${escapeHtml(c.example)}</code> — ${escapeHtml(c.description)}${c.aliases.length ? ` (${c.aliases.map(a => escapeHtml(a === '?' ? '?' : '/' + a)).join(', ')})` : ''}</li>`
  ).join('');
  modal.style.display = 'flex';
  modal.querySelector('.modal-close').focus();
}

async function boardCommandRun(placeOnly = false){
  if(boardCommandBusy) return;
  const input = document.getElementById('board-command-input');
  const value = input.value;
  const parsed = parseBoardCommand(value);
  if(!value.trim()) return;
  if(parsed.error){ boardCommandError(parsed.error); return; }
  boardCommandBusy = true;
  let success = false;
  try{
    let node = null;
    if(parsed.cmd === 'help') { boardCommandHelp(); success = true; }
    else if(parsed.cmd === 'exit'){
      input.value = '';
      boardCommandSetOpen(false);
      return;
    }
    else if(parsed.cmd === 'gen'){
      if(!parsed.args){ openBoardAssetPicker('gen', null, boardCommandGeometry('gen'), '', value); return; }
      const args = boardCommandGenArgs(parsed.args);
      if(!args) throw new Error('프리셋 이름을 입력해 주세요.');
      const res = await fetch('/api/board-presets');
      if(!res.ok) throw new Error('프리셋 목록을 읽지 못했어요.');
      boardGenPresets = (await res.json()).presets || [];
      const matches = boardCommandFindGenPreset(boardGenPresets, args.name);
      if(!matches.length) throw new Error('없는 프리셋이에요.');
      if(matches.length > 1) throw new Error(`프리셋을 더 구체적으로 입력해 주세요: ${matches.map(p => p.name).join(', ')}`);
      const selectedImage = boardSelectedIds.size === 1 ? boardNodes.find(n => n.id === [...boardSelectedIds][0] && n.kind === 'image') : null;
      node = await createBoardNode('gen', matches[0].id, '', boardCommandGeometry('gen'));
      if(!node) throw new Error('카드를 만들지 못했어요.');
      const fields = (node.data && node.data.fields) || [];
      const promptField = fields.find(f => f.type === 'textarea' && /prompt/i.test(f.name)) || fields.find(f => f.type === 'textarea');
      if(args.prompt && !promptField){
        const removed = await boardApi(currentProjectId, 'POST', '/nodes/delete', { ids: [node.id] }, false, currentBoardId);
        if(!removed) throw new Error('프롬프트 칸이 없어요. 카드는 남아 있어요.');
        removeBoardNodesLocal(new Set([node.id]));
        boardUndoStack.pop();
        paintBoardHistoryButtons();
        throw new Error('이 프리셋에는 프롬프트 칸이 없어요.');
      }
      if(args.prompt){
        const saved = await boardApi(currentProjectId, 'PATCH', `/nodes/${node.id}`, { values: { [promptField.name]: args.prompt } }, false, currentBoardId);
        if(!saved) throw new Error('프롬프트를 저장하지 못했어요. 카드는 남아 있어요.');
        setBoardGenValueLocal(node.id, promptField.name, args.prompt);
      }
      if(selectedImage){
        const slot = ((node.data && node.data.slots) || [])[0];
        if(slot){
          const edge = await createBoardEdge(selectedImage.id, node.id, slot.name, false);
          if(!edge) throw new Error('이미지를 잇지 못했어요. 카드는 남아 있어요.');
        }else flashNotice('이 프리셋에는 이미지 입구가 없어요.');
      }
      boardCommandAdvance(node);
      success = true;
      if(!placeOnly && args.prompt){
        const result = await runBoardGen(node.id);
        if(!result || !result.ok) throw new Error((result && result.error) || '실행하지 못했어요. 카드는 남아 있어요.');
      }
    }
    else if(parsed.cmd === 'run'){
      input.value = '';
      boardCommandPaint();
      if(parsed.args) cmpInput.value += (cmpInput.value ? '\n' : '') + parsed.args;
      openComposer();
      cmpAutosize();
      cmpUpdate();
      return;
    }
    else if(parsed.cmd === 'image' || parsed.cmd === 'video'){
      if(parsed.args){
        openBoardAssetPicker(parsed.cmd, null, boardCommandGeometry(parsed.cmd), parsed.args, value);
        return;
      }
      node = await createBoardNode(parsed.cmd, null, '', boardCommandGeometry(parsed.cmd));
    }
    else if(parsed.cmd === 'job' && !parsed.args){
      openBoardAssetPicker('job', null, boardCommandGeometry('job'), '', value);
      return;
    }
    else if(parsed.cmd === 'job'){
      const response = await fetch('/api/jobs');
      if(!response.ok) throw new Error('작업 목록을 읽지 못했어요.');
      const jobs = (await response.json()).jobs || [];
      if(!jobs.some(j => String(j.id) === parsed.args)) throw new Error('해당 작업 ID를 찾지 못했어요.');
      node = await createBoardNode('job', parsed.args, '', boardCommandGeometry('job'));
    }else if(parsed.cmd === 'frame'){
      node = await createBoardFrame(parsed.args || '묶음', boardSelectedIds.size ? undefined : boardCommandGeometry('frame'));
    }else node = await createBoardNode('text', null, parsed.args, boardCommandGeometry('text'));
    if(node && parsed.cmd !== 'gen'){
      boardCommandAdvance(node);
      success = true;
      if(parsed.cmd === 'text' && !parsed.args){
        const textarea = document.querySelector(`#board-world .board-node-textarea[data-board-text="${node.id}"]`);
        if(textarea) textarea.focus();
      }
    }
    if(!success) boardCommandError('카드를 만들지 못했어요. 다시 시도해 주세요.');
  }catch(e){ success = false; boardCommandError(e.message || '명령을 실행하지 못했어요.'); }
  finally{ boardCommandBusy = false; }
  if(!success) return;
  boardCommandFinish(value);
}

async function boardCommandUpload(files){
  files = Array.from(files).filter(f => !f.type || /^(image|video)\//.test(f.type));
  if(!files.length){ boardCommandError('이미지나 영상 파일만 올릴 수 있어요.'); return; }
  if(boardCommandBusy || assetUploadXhr){ boardCommandError('다른 파일을 올리는 중이에요.'); return; }
  const projectId = currentProjectId;
  if(typeof projectId !== 'number') return;
  boardCommandBusy = true;
  try{
    const data = await uploadAssetFiles(files, projectId, '', r => flashNotice(r < 1 ? `올리는 중… ${Math.floor(r * 100)}%` : '저장하는 중…'));
    if(projectId !== currentProjectId) return;
    const geom = boardCommandGeometry('image');
    let last = null;
    for(const [i, path] of data.paths.entries()){
      const kind = /\.(mp4|webm|mov|m4v)$/i.test(path) ? 'video' : 'image';
      last = await createBoardNode(kind, path, '', { x: geom.x + (i % 4) * 240, y: geom.y + Math.floor(i / 4) * 240, width: 220, height: 220 });
    }
    if(last) boardCommandAdvance(last);
    if(data.errors && data.errors.length) boardCommandError(`올리지 못한 파일: ${data.errors.join(' / ')}`);
  }catch(e){ boardCommandError(e.message || '파일을 올리지 못했어요.'); }
  finally{ assetUploadXhr = null; boardCommandBusy = false; }
}

const boardCommandInput = document.getElementById('board-command-input');
boardCommandInput.addEventListener('paste', e => {
  if(!e.clipboardData) return;
  if(e.clipboardData.files.length){
    e.preventDefault();
    boardCommandUpload(e.clipboardData.files);
    return;
  }
  const pasted = e.clipboardData.getData('text/plain');
  if(!/(?:\r\n|\r|\n)/.test(pasted)) return;
  e.preventDefault();
  boardCommandInput.setRangeText(pasted, boardCommandInput.selectionStart, boardCommandInput.selectionEnd, 'end');
  const lines = boardCommandPasteLines(boardCommandInput.value);
  boardCommandPaste = lines.length > 1 ? { lines } : null;
  boardCommandPasteChoice();
  boardCommandPaint();
});
const boardCommandBox = document.getElementById('board-command');
boardCommandSyncVisibility();
document.getElementById('board-command-open').addEventListener('click', boardCommandFocus);
document.getElementById('board-command-close').addEventListener('click', () => boardCommandSetOpen(false));
boardCommandBox.addEventListener('dragover', e => { if(e.dataTransfer && Array.from(e.dataTransfer.types).includes('Files')) e.preventDefault(); });
boardCommandBox.addEventListener('drop', e => {
  if(!e.dataTransfer || !Array.from(e.dataTransfer.types).includes('Files')) return;
  e.preventDefault();
  e.stopPropagation();
  boardCommandUpload(e.dataTransfer.files);
});
boardCommandInput.addEventListener('input', () => { boardCommandClearPaste(); boardCommandChoice = 0; boardCommandPaint(); });
boardCommandInput.addEventListener('focus', boardCommandPaint);
boardCommandInput.addEventListener('blur', () => { setTimeout(boardCommandPaint, 0); });
boardCommandInput.addEventListener('keydown', e => {
  if(e.isComposing || e.keyCode === 229) return;
  if(e.key === 'Escape'){
    e.preventDefault();
    e.stopPropagation();
    if(boardCommandPaste){ boardCommandClearPaste(); boardCommandPaint(); return; }
    if(!document.getElementById('board-command-menu').hidden){
      document.getElementById('board-command-menu').hidden = true;
      return;
    }
    boardCommandInput.blur();
    boardCommandPaint();
    return;
  }
  if(e.key === 'Tab' && boardCommandChoices.length){ e.preventDefault(); boardCommandComplete(); return; }
  if(e.key === 'Enter' && !e.shiftKey){
    e.preventDefault();
    if(boardCommandPaste){ boardCommandRunPaste(); return; }
    if(boardCommandChoices.length && !/^\/(?:gen|g)(?:\s|$)/i.test(boardCommandInput.value) && !boardCommandInput.value.endsWith(' ')
        && !/\/(?:text|t|job|image|img|i|video|v|gen|g|exit|q|run|퀵런)$/i.test(boardCommandInput.value)) boardCommandComplete();
    else boardCommandRun(e.ctrlKey || e.metaKey);
    return;
  }
  if((e.key === 'ArrowUp' || e.key === 'ArrowDown') && boardCommandChoices.length){
    e.preventDefault();
    boardCommandChoice = (boardCommandChoice + (e.key === 'ArrowUp' ? -1 : 1) + boardCommandChoices.length) % boardCommandChoices.length;
    boardCommandPaint();
    return;
  }
  if(e.key === 'ArrowUp' && boardCommandInput.selectionStart === 0 && boardCommandHistoryIndex > 0){
    e.preventDefault();
    if(boardCommandHistoryIndex === boardCommandHistory.length) boardCommandDraft = boardCommandInput.value;
    boardCommandInput.value = boardCommandHistory[--boardCommandHistoryIndex];
    boardCommandPaint();
  }else if(e.key === 'ArrowDown' && boardCommandInput.selectionStart === boardCommandInput.value.length && boardCommandHistoryIndex < boardCommandHistory.length){
    e.preventDefault();
    boardCommandHistoryIndex++;
    boardCommandInput.value = boardCommandHistoryIndex === boardCommandHistory.length ? boardCommandDraft : boardCommandHistory[boardCommandHistoryIndex];
    boardCommandPaint();
  }
});
boardCommandBox.addEventListener('click', e => {
  const button = e.target.closest('[data-paste-choice]');
  if(!button) return;
  if(button.dataset.pasteChoice === 'many') boardCommandRunPaste();
  else{ boardCommandClearPaste(); boardCommandPaint(); boardCommandInput.focus(); }
});
document.addEventListener('keydown', e => {
  if(e.key !== 'Escape' || e.isComposing || currentTab !== 'prboard' || !boardCommandOpen
      || !document.getElementById('composer').hidden || document.activeElement === boardCommandInput
      || (e.target.closest && e.target.closest('input, textarea, select, [contenteditable]'))
      || [...document.querySelectorAll('.modal-overlay')].some(m => m.style.display && m.style.display !== 'none')) return;
  boardCommandSetOpen(false);
});
document.getElementById('board-command-menu').addEventListener('pointerdown', e => e.preventDefault());
document.getElementById('board-command-menu').addEventListener('click', e => {
  const button = e.target.closest('[data-command]');
  if(!button) return;
  boardCommandChoice = boardCommandChoices.findIndex(c => c.cmd === button.dataset.command);
  boardCommandComplete();
});
document.getElementById('board-command-help').addEventListener('click', e => {
  if(e.target.closest('.modal-close') || e.target.id === 'board-command-help'){
    e.currentTarget.style.display = 'none';
    boardCommandFocus();
  }
});
document.getElementById('board-command-help').addEventListener('keydown', e => {
  if(e.key === 'Escape'){
    e.preventDefault();
    e.currentTarget.style.display = 'none';
    boardCommandFocus();
  }
});
