// 프롬프트 입력 근처에서 옵션 명세를 읽어 보여주고 닫으면 입력 위치로 돌아간다.
function promptOptionsHelpButtonHtml(column){
  const data = column === undefined ? '' : ` data-prompt-help-column="${column}"`;
  return `<button type="button" class="prompt-options-help-button" title="옵션 사용법" aria-label="옵션 사용법"${data}><svg class="ico"><use href="#i-circle-help"/></svg></button>`;
}

function createPromptOptionsHelpButton(){
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'prompt-options-help-button';
  button.title = '옵션 사용법';
  button.setAttribute('aria-label', '옵션 사용법');
  button.innerHTML = '<svg class="ico"><use href="#i-circle-help"/></svg>';
  return button;
}

const promptHelpModal = document.getElementById('prompt-options-help-modal');
const promptHelpBody = document.getElementById('prompt-options-help-body');
let promptHelpSpec = null;
let promptHelpTarget = null;
let promptHelpSelection = null;
let promptHelpRequest = null;

function promptHelpExamples(){
  return [
    ['1girl, beach --ar 2:3 --res 1024 --seed 7 --r 4', '832×1216, 시드 7부터 4장'],
    ['portrait --no blurry --steps 28 --cfg 7.5', '네거티브에 blurry 추가, 28단계, CFG 7.5'],
    ['cat \\--seed 7 --lora detail:0.8', '--seed 7은 글자로 유지, LoRA detail 적용'],
  ];
}

function renderPromptHelp(spec){
  const rows = (spec.options || []).map(item => `<tr><th scope="row"><code>--${escapeHtml(item.name)}</code>${item.aliases.length ? `<small>${item.aliases.map(a => `<code>--${escapeHtml(a)}</code>`).join(' ')}</small>` : ''}</th><td>${escapeHtml(item.format)}</td><td><code>${escapeHtml(item.example)}</code></td><td>${escapeHtml(item.description)}</td></tr>`).join('');
  promptHelpBody.innerHTML = `<p>프롬프트 끝에 옵션과 값을 적어 주세요. 실행할 때 옵션은 프롬프트에서 빠져요.</p>
    <div class="prompt-help-table-wrap"><table class="prompt-help-table"><thead><tr><th>옵션 · 별칭</th><th>값 형식</th><th>예시</th><th>설명</th></tr></thead><tbody>${rows}</tbody></table></div>
    <h3>적용 규칙</h3><ul>
      <li>프롬프트 옵션 값이 화면에서 고른 값보다 우선해요.</li>
      <li><code>--no</code>는 기존 네거티브 프롬프트 뒤에 덧붙여요.</li>
      <li><code>--ar</code>와 숫자 <code>--res</code>를 함께 쓰면 기준 크기와 비율로 계산해 64의 배수로 맞춰요. <code>--res 768x1024</code>처럼 크기를 직접 적으면 그 크기를 사용해요.</li>
      <li><code>\\--</code>를 쓰면 옵션으로 해석하지 않고 글자 그대로 남겨요.</li>
      <li>CSV의 프롬프트 옵션은 각 행에 따로 적용해요.</li>
    </ul><h3>예시</h3><div class="prompt-help-examples">${promptHelpExamples().map(([input, result], i) => `<div class="prompt-help-example"><code>${escapeHtml(input)}</code><button type="button" class="prompt-help-copy" data-prompt-help-example="${i}">복사</button><span>→ ${escapeHtml(result)}</span></div>`).join('')}</div>`;
}

function closePromptOptionsHelp(){
  if(promptHelpModal.style.display === 'none') return;
  promptHelpModal.style.display = 'none';
  if(promptHelpTarget && promptHelpTarget.isConnected){
    promptHelpTarget.focus({preventScroll:true});
    if(promptHelpSelection && promptHelpTarget.setSelectionRange){
      promptHelpTarget.setSelectionRange(...promptHelpSelection);
    }
  }
  promptHelpTarget = null;
  promptHelpSelection = null;
}

function promptHelpInputFor(button){
  if(button.hasAttribute('data-prompt-help-column')){
    return document.querySelector(`#csv-editor-tbody input[data-ci="${button.dataset.promptHelpColumn}"]`);
  }
  const boardField = button.closest('.board-gen-field');
  if(boardField) return boardField.querySelector('textarea');
  return button.closest('.prompt-enhance-col')?.querySelector('.enhance-raw') || null;
}

async function openPromptOptionsHelp(button){
  promptHelpTarget = promptHelpInputFor(button);
  promptHelpSelection = promptHelpTarget && 'selectionStart' in promptHelpTarget
    ? [promptHelpTarget.selectionStart, promptHelpTarget.selectionEnd] : null;
  promptHelpModal.style.display = 'flex';
  document.getElementById('prompt-options-help-close').focus();
  if(promptHelpSpec){ renderPromptHelp(promptHelpSpec); return; }
  promptHelpBody.textContent = '옵션 사용법을 불러오는 중이에요.';
  try{
    if(!promptHelpRequest) promptHelpRequest = fetch('/api/prompt-options/spec').then(r => {
      if(!r.ok) throw new Error('명세 요청 실패');
      return r.json();
    });
    promptHelpSpec = await promptHelpRequest;
    if(promptHelpModal.style.display !== 'none') renderPromptHelp(promptHelpSpec);
  }catch(error){
    promptHelpRequest = null;
    if(promptHelpModal.style.display !== 'none') promptHelpBody.textContent = '옵션 사용법을 불러오지 못했어요. 닫고 다시 열어 주세요.';
  }
}

document.addEventListener('click', event => {
  const button = event.target.closest('.prompt-options-help-button');
  if(button){
    event.preventDefault();
    event.stopPropagation();
    openPromptOptionsHelp(button);
  }
});
document.getElementById('prompt-options-help-close').addEventListener('click', closePromptOptionsHelp);
promptHelpModal.addEventListener('click', event => {
  if(event.target === promptHelpModal) closePromptOptionsHelp();
  const copy = event.target.closest('[data-prompt-help-example]');
  if(copy){
    const example = promptHelpExamples()[Number(copy.dataset.promptHelpExample)];
    if(example) copyTextToClipboard(example[0]).then(() => { copy.textContent = '복사했어요'; }, () => { copy.textContent = '복사하지 못했어요'; });
  }
});
promptHelpModal.addEventListener('keydown', event => {
  if(event.key === 'Escape'){
    event.preventDefault(); event.stopPropagation(); closePromptOptionsHelp();
  }
  if(event.key === 'Tab'){
    const buttons = [...promptHelpModal.querySelectorAll('button')].filter(b => b.getClientRects().length);
    const first = buttons[0], last = buttons[buttons.length - 1];
    if(event.shiftKey && document.activeElement === first){ event.preventDefault(); last.focus(); }
    else if(!event.shiftKey && document.activeElement === last){ event.preventDefault(); first.focus(); }
  }
});
