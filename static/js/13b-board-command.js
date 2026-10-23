// 퀵바 — 보드 탭의 `/` 명령창에서 카드를 만들고 입력 기록과 완성을 관리한다.
const BOARD_COMMANDS = [
  { cmd: 'text', aliases: ['t'], description: '메모 카드', example: '/text 내용' },
  { cmd: 'frame', aliases: ['f'], description: '묶음 틀', example: '/frame 이름' },
  { cmd: 'job', aliases: [], description: '작업 카드', example: '/job 작업ID' },
  { cmd: 'image', aliases: ['img', 'i'], description: '이미지 고르기', example: '/image 검색어' },
  { cmd: 'video', aliases: ['v'], description: '영상 고르기', example: '/video 검색어' },
  { cmd: 'help', aliases: ['?'], description: '명령 도움말', example: '/help' },
];
const BOARD_COMMAND_HISTORY_KEY = 'nightshift.boardCommands';
let boardCommandPoint = null;
let boardCommandNext = null;
let boardCommandBusy = false;
let boardCommandChoices = [];
let boardCommandChoice = 0;
let boardCommandHistory = [];
try{
  const saved = JSON.parse(localStorage.getItem(BOARD_COMMAND_HISTORY_KEY) || '[]');
  if(Array.isArray(saved)) boardCommandHistory = saved.filter(x => typeof x === 'string').slice(-50);
}catch(e){ /* 저장소를 읽지 못하면 이번 화면의 기록만 쓴다. */ }
let boardCommandHistoryIndex = boardCommandHistory.length;
let boardCommandDraft = '';

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
  boardCommandPaint();
}
function boardCommandFocus(){
  if(currentTab !== 'prboard') return;
  const input = document.getElementById('board-command-input');
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
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
  input.value = '/' + choice.cmd + ' ';
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
  boardCommandChoice = Math.min(boardCommandChoice, Math.max(0, boardCommandChoices.length - 1));
  menu.hidden = !boardCommandChoices.length || document.activeElement !== input;
  menu.innerHTML = boardCommandChoices.map((c, i) => `<button type="button" role="option" aria-selected="${i === boardCommandChoice}" data-command="${c.cmd}"><b>${c.example}</b><span>${c.description}</span></button>`).join('');
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 6 * 23) + 'px';
  boardCommandError('');
}

function boardCommandHelp(){
  const modal = document.getElementById('board-command-help');
  document.getElementById('board-command-help-list').innerHTML = BOARD_COMMANDS.map(c =>
    `<li><code>${escapeHtml(c.example)}</code> — ${escapeHtml(c.description)}${c.aliases.length ? ` (${c.aliases.map(a => escapeHtml(a === '?' ? '?' : '/' + a)).join(', ')})` : ''}</li>`
  ).join('');
  modal.style.display = 'flex';
  modal.querySelector('.modal-close').focus();
}

async function boardCommandRun(){
  if(boardCommandBusy) return;
  const input = document.getElementById('board-command-input');
  const value = input.value;
  const parsed = parseBoardCommand(value);
  if(parsed.error){ boardCommandError(parsed.error); return; }
  if((parsed.cmd === 'text' || parsed.cmd === 'job') && !parsed.args){ boardCommandError('내용을 입력해 주세요.'); return; }
  boardCommandBusy = true;
  let success = false;
  try{
    let node = null;
    if(parsed.cmd === 'help') { boardCommandHelp(); success = true; }
    else if(parsed.cmd === 'image' || parsed.cmd === 'video'){
      openBoardAssetPicker(parsed.cmd, null, boardCommandGeometry(parsed.cmd), parsed.args, value);
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
    if(node){ boardCommandAdvance(node); success = true; }
    if(!success) boardCommandError('카드를 만들지 못했어요. 다시 시도해 주세요.');
  }catch(e){ boardCommandError(e.message || '명령을 실행하지 못했어요.'); }
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
  if(!e.clipboardData || !e.clipboardData.files.length) return;
  e.preventDefault();
  boardCommandUpload(e.clipboardData.files);
});
const boardCommandBox = document.getElementById('board-command');
boardCommandBox.addEventListener('dragover', e => { if(e.dataTransfer && Array.from(e.dataTransfer.types).includes('Files')) e.preventDefault(); });
boardCommandBox.addEventListener('drop', e => {
  if(!e.dataTransfer || !Array.from(e.dataTransfer.types).includes('Files')) return;
  e.preventDefault();
  e.stopPropagation();
  boardCommandUpload(e.dataTransfer.files);
});
boardCommandInput.addEventListener('input', () => { boardCommandChoice = 0; boardCommandPaint(); });
boardCommandInput.addEventListener('focus', boardCommandPaint);
boardCommandInput.addEventListener('blur', () => { setTimeout(boardCommandPaint, 0); });
boardCommandInput.addEventListener('keydown', e => {
  if(e.isComposing || e.keyCode === 229) return;
  if(e.key === 'Escape'){ e.preventDefault(); boardCommandInput.blur(); boardCommandPaint(); return; }
  if(e.key === 'Tab' && boardCommandChoices.length){ e.preventDefault(); boardCommandComplete(); return; }
  if(e.key === 'Enter' && !e.shiftKey){
    e.preventDefault();
    if(boardCommandChoices.length && !boardCommandInput.value.endsWith(' ')
        && !/\/(?:image|img|i|video|v)$/i.test(boardCommandInput.value)) boardCommandComplete();
    else boardCommandRun();
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
