// 스켈레톤 로딩(docs/DESIGN.md 8절) — 목록 자리표시 칸 만들기, 늦게 띄우기, 썸네일 도착 표시(is-loaded). 모양은 css/01b-skeleton.css
// 이미지는 load 이벤트가 위로 올라오지 않아 문서에서 "잡기 단계"로 받는다 — 칸을 innerHTML로 다시 그려도 따로 연결할 게 없다.
// 실패한 이미지도 is-loaded를 붙여 회색 면이 영영 깜빡이지 않게 한다.
document.addEventListener('load', e => { if(e.target.tagName === 'IMG') e.target.classList.add('is-loaded'); }, true);
document.addEventListener('error', e => { if(e.target.tagName === 'IMG') e.target.classList.add('is-loaded'); }, true);

// 캐시에서 바로 그려져 load가 이 문서 리스너보다 먼저 끝났을 수 있는 이미지까지 표시한다(다시 그린 뒤 부른다).
function markLoadedThumbs(root){
  if(!root) return;
  root.querySelectorAll('img:not(.is-loaded)').forEach(img => {
    if(img.complete && img.naturalWidth) img.classList.add('is-loaded');
  });
}

// 빠르게 끝나는 로딩에서 회색 칸이 번쩍이지 않게 delay(ms, DESIGN.md 8절 "기다림"의 0.3초) 뒤에야 깐다. 돌려준 함수를 부르면(받기가 끝나면) 아직 안 깔았을 때 취소한다.
// stillWaiting을 주면 깔기 직전에 한 번 더 묻는다(취소 함수를 부를 자리가 마땅치 않을 때).
function showSkeletonLater(el, html, delay = 300, stillWaiting = null){
  if(!el) return () => {};
  const timer = setTimeout(() => {
    if(stillWaiting && !stillWaiting()) return;
    el.innerHTML = html; el.dataset.skeleton = '1';
  }, delay);
  return () => clearTimeout(timer);
}
// 깔아 둔 자리표시만 걷어 낸다(받기에 실패해 그릴 게 없을 때). 진짜 내용이 있으면 건드리지 않는다.
function clearSkeleton(el){
  if(el && el.dataset.skeleton){ el.innerHTML = ''; delete el.dataset.skeleton; }
}

// 이미지·영상 갤러리 칸. details면 자세히 보기 줄 모양.
function gallerySkeletonHtml(itemClass, details, count){
  if(details){
    const row = `<div class="skel-row"><div class="skel-block skel-thumb"></div><div class="skel-block skel-line"></div><div class="skel-block skel-line short"></div><div class="skel-block skel-line short"></div></div>`;
    return row.repeat(count || 10);
  }
  return `<div class="${itemClass} skel-item skel-block" aria-hidden="true"></div>`.repeat(count || 24);
}

// 홈의 프로젝트 카드
function projectSkeletonHtml(count){
  return `
    <div class="project-card skel-item" aria-hidden="true">
      <div class="project-cover skel-block"></div>
      <div class="project-card-body">
        <div class="skel-block skel-line title"></div>
        <div class="skel-block skel-line desc"></div>
        <div class="skel-block skel-line stats"></div>
      </div>
    </div>`.repeat(count || 6);
}
