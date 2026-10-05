// ---- Danbooru 프롬프트 조립 탭 ----
// 카테고리/태그 풀/규칙은 전부 코드가 아니라 아래 선언적 데이터(DANBOORU_*)로
// 관리한다 — 새 규칙이나 태그를 추가할 때 이 데이터만 늘리면 되고, 검증·랜덤
// 로직(뒤쪽 함수들)은 건드릴 필요가 없다.
// ============================================================

// ---- 2.4 태그 그룹 — 규칙과 랜덤 로직이 참조하는 논리적 묶음 ----
const DANBOORU_TAG_GROUPS = {
  bgFlat: ['simple background', 'white background', 'grey background', 'gradient background', 'transparent background', 'two-tone background'],
  indoorLoc: ['classroom', 'bedroom', 'kitchen', 'bathroom', 'library', 'cafe', 'office', 'train interior', 'atrium', 'lobby'],
  outdoorLoc: ['rooftop', 'street', 'alley', 'park', 'beach', 'forest', 'mountain', 'city', 'cityscape', 'ruins', 'shrine'],
  time: ['day', 'morning', 'sunrise', 'sunset', 'evening', 'twilight', 'blue hour', 'night'],
  weather: ['rain', 'snow', 'fog', 'cloudy', 'storm', 'clear sky', 'wind'],
  lightDir: ['backlighting', 'rim light', 'side lighting', 'underlighting', 'overhead lighting'],
  lightQual: ['soft lighting', 'harsh lighting', 'dim lighting', 'dramatic lighting', 'cinematic lighting', 'chiaroscuro'],
  daylight: ['sunlight', 'sunbeam', 'god rays', 'dappled sunlight'],
  artificial: ['neon lights', 'city lights', 'lamp', 'candlelight', 'firelight', 'screen light', 'spotlight'],
  lightAccent: ['light particles', 'cast shadow', 'silhouette', 'caustics', 'lens flare', 'bloom'],
  crowd: ['2girls', '2boys', '3girls', 'multiple girls', 'multiple boys'],
  counts: ['1girl', '1boy', '1other'],
  mono: ['monochrome', 'greyscale'],
};
DANBOORU_TAG_GROUPS.location = [...DANBOORU_TAG_GROUPS.indoorLoc, ...DANBOORU_TAG_GROUPS.outdoorLoc];

const DANBOORU_PLACE_POOL = [
  ...DANBOORU_TAG_GROUPS.bgFlat,
  'scenery', 'indoors', 'outdoors', 'window',
  ...DANBOORU_TAG_GROUPS.location,
  ...DANBOORU_TAG_GROUPS.time,
  ...DANBOORU_TAG_GROUPS.weather,
];
const DANBOORU_LIGHT_POOL = [
  ...DANBOORU_TAG_GROUPS.lightDir,
  ...DANBOORU_TAG_GROUPS.lightQual,
  ...DANBOORU_TAG_GROUPS.daylight,
  ...DANBOORU_TAG_GROUPS.artificial,
  ...DANBOORU_TAG_GROUPS.lightAccent,
  'moonlight', 'light rays', 'backlit',
];

// ---- 2.2/2.3 카테고리 목록 (조립 순서 = order) ----
const DANBOORU_CATEGORIES = [
  { key: 'quality', label: '품질·미학', kind: 'tags', order: 1, lockedByDefault: true,
    randomStrategy: { type: 'range', min: 0, max: 0 },
    tags: ['masterpiece', 'best quality', 'amazing quality', 'absurdres', 'highres', 'very awa', 'newest', 'recent', 'mid', 'early', 'old'] },
  { key: 'artist', label: '아티스트 스타일', kind: 'tags', order: 2,
    randomStrategy: { type: 'range', min: 0, max: 1 },
    tags: [] },
  { key: 'era', label: '시대·매체', kind: 'tags', order: 3,
    randomStrategy: { type: 'range', min: 0, max: 2 },
    tags: ['retro artstyle', '1980s (style)', '1990s (style)', '2000s (style)', 'nostalgic', 'anime screencap', 'official art', 'game cg'] },
  { key: 'technique', label: '기법', kind: 'tags', order: 4,
    randomStrategy: { type: 'range', min: 0, max: 1 },
    tags: ['sketch', 'lineart', 'monochrome', 'greyscale', 'watercolor (medium)', 'traditional media', 'oekaki', 'pixel art', 'flat color', 'cel shading', 'chibi'] },
  { key: 'texture', label: '질감·후처리', kind: 'tags', order: 5,
    randomStrategy: { type: 'range', min: 0, max: 2 },
    tags: ['film grain', 'chromatic aberration', 'halftone', 'screentone', 'dithering', 'vhs artifacts'] },
  { key: 'char_composition', label: '인원 구성', kind: 'tags', order: 6,
    randomStrategy: { type: 'combo' },
    tags: ['1girl', '1boy', '1other', '2girls', '2boys', '3girls', 'multiple girls', 'multiple boys', 'solo', 'solo focus', 'hetero', 'yuri', 'yaoi'] },
  { key: 'char1', label: '캐릭터 1 (외형·복장·표정)', kind: 'text', order: 7,
    placeholder: '캐릭터 1의 외형·복장·표정 등을 자유롭게 입력하세요' },
  { key: 'char2', label: '캐릭터 2', kind: 'text', order: 8,
    placeholder: '캐릭터 2의 외형·복장·표정 등을 자유롭게 입력하세요' },
  { key: 'framing', label: '프레이밍', kind: 'tags', order: 9, single: true,
    randomStrategy: { type: 'range', min: 1, max: 1 },
    tags: ['close-up', 'portrait', 'upper body', 'cowboy shot', 'full body', 'wide shot'] },
  { key: 'view', label: '시점', kind: 'tags', order: 10,
    randomStrategy: { type: 'range', min: 0, max: 2 },
    tags: ['from above', 'from below', 'from side', 'from behind', 'straight-on', 'dutch angle', 'pov'] },
  { key: 'pose', label: '포즈', kind: 'tags', order: 11,
    randomStrategy: { type: 'range', min: 0, max: 2 },
    tags: ['standing', 'sitting', 'looking up', 'looking back', 'arms up', 'leaning forward'] },
  // NSFW — 기본 풀을 일부러 비워둔다(artist와 같은 방식). 구체적인 태그는 사용자가
  // 아래 "태그 풀 편집"에서 직접 채워 넣는다. 다른 카테고리와 똑같이 메인 카테고리
  // 목록 안에 그려진다(예전엔 별도 접힌 패널이었으나 요청에 따라 통합).
  { key: 'nsfw', label: 'NSFW', kind: 'tags', order: 12,
    randomStrategy: { type: 'range', min: 0, max: 2 },
    tags: [] },
  { key: 'place', label: '배경·장소·시간대·날씨', kind: 'tags', order: 13,
    randomStrategy: { type: 'place' },
    tags: DANBOORU_PLACE_POOL },
  { key: 'light', label: '조명', kind: 'tags', order: 14,
    randomStrategy: { type: 'light' },
    tags: DANBOORU_LIGHT_POOL },
  // 자유 추가 — 예전엔 자유 입력 textarea였으나, 입력한 내용이 재사용(다음 세션에도
  // 다시 선택)되도록 다른 카테고리와 같은 "태그 풀 편집" 방식(kind: 'tags')으로
  // 바꿨다. 기본 풀은 비워두고 사용자가 원하는 걸 자유롭게 추가해서 쓴다.
  { key: 'extra', label: '자유 추가', kind: 'tags', order: 15,
    randomStrategy: { type: 'range', min: 0, max: 2 },
    tags: [] },
];
const DANBOORU_CATEGORY_BY_KEY = Object.fromEntries(DANBOORU_CATEGORIES.map(c => [c.key, c]));

// ---- 4.2 char_composition 전용 유효 조합 (가중치를 위해 일부 중복 배치) ----
const DANBOORU_COMBO_PRESETS = [
  ['1girl', 'solo'], ['1girl', 'solo'], ['1girl', 'solo'],
  ['1boy', 'solo'],
  ['1other', 'solo'],
  ['1girl', '1boy', 'hetero'],
  ['2girls', 'yuri'],
  ['2girls'],
  ['2boys', 'yaoi'],
  ['3girls'],
  ['1girl', 'multiple boys', 'solo focus'],
];

// ---- 3.2 규칙 목록 (선언적 데이터) ----
const DANBOORU_RULES = [
  { id: 'time-one', type: 'atMostOne', set: DANBOORU_TAG_GROUPS.time, message: '시간대는 하나만' },
  { id: 'indoor-outdoor-one', type: 'atMostOne', set: ['indoors', 'outdoors'], message: '실내와 실외는 함께 쓸 수 없음' },
  { id: 'bgflat-one', type: 'atMostOne', set: DANBOORU_TAG_GROUPS.bgFlat, message: '단색 배경은 하나만' },
  { id: 'relationship-one', type: 'atMostOne', set: ['hetero', 'yuri', 'yaoi'], message: '관계 태그는 하나만' },
  { id: 'lightdir-one', type: 'atMostOne', set: DANBOORU_TAG_GROUPS.lightDir, message: '광원 방향은 하나만' },
  { id: 'lightqual-one', type: 'atMostOne', set: DANBOORU_TAG_GROUPS.lightQual, message: '광질은 하나만' },
  { id: 'mono-one', type: 'atMostOne', set: DANBOORU_TAG_GROUPS.mono, message: 'monochrome과 greyscale은 중복' },

  { id: 'bgflat-vs-place', type: 'forbid', a: DANBOORU_TAG_GROUPS.bgFlat, b: [...DANBOORU_TAG_GROUPS.location, 'scenery', ...DANBOORU_TAG_GROUPS.weather], message: '단색 배경에 장소·날씨 태그 불가' },
  { id: 'indoors-vs-outdoorloc', type: 'forbid', a: ['indoors'], b: DANBOORU_TAG_GROUPS.outdoorLoc, message: '실내인데 실외 장소' },
  { id: 'outdoors-vs-indoorloc', type: 'forbid', a: ['outdoors'], b: DANBOORU_TAG_GROUPS.indoorLoc, message: '실외인데 실내 장소' },
  { id: 'indoorloc-vs-outdoorloc', type: 'forbid', a: DANBOORU_TAG_GROUPS.indoorLoc, b: DANBOORU_TAG_GROUPS.outdoorLoc, message: '실내 장소와 실외 장소 혼재' },
  { id: 'indoors-vs-weather', type: 'forbid', a: ['indoors'], b: DANBOORU_TAG_GROUPS.weather, message: '실내에 날씨 태그는 window가 있을 때만 유의미' },
  { id: 'night-vs-daylight', type: 'forbid', a: ['night'], b: DANBOORU_TAG_GROUPS.daylight, message: '밤에 햇빛 계열 불가' },
  { id: 'moonlight-vs-day', type: 'forbid', a: ['moonlight'], b: ['day', 'morning'], message: '낮에 달빛' },
  { id: 'solo-vs-crowd', type: 'forbid', a: ['solo'], b: DANBOORU_TAG_GROUPS.crowd, message: 'solo는 인물 한 명일 때만' },
  { id: 'solo-vs-solofocus', type: 'forbid', a: ['solo'], b: ['solo focus'], message: '동시 사용 불가' },
  { id: 'pixelart-vs-filmtexture', type: 'forbid', a: ['pixel art'], b: ['film grain', 'chromatic aberration'], message: '픽셀 아트와 필름 질감은 상쇄' },
  { id: 'mono-vs-color', type: 'forbid', a: DANBOORU_TAG_GROUPS.mono, b: ['neon lights', 'flat color'], message: '흑백인데 색 관련 태그' },

  { id: 'yuri-needs-multigirls', type: 'require', a: ['yuri'], b: ['2girls', 'multiple girls', '3girls'], message: '여성 복수 태그 필요' },
  { id: 'yaoi-needs-multiboys', type: 'require', a: ['yaoi'], b: ['2boys', 'multiple boys'], message: '남성 복수 태그 필요' },
  { id: 'hetero-needs-boy', type: 'require', a: ['hetero'], b: ['1boy', '2boys', 'multiple boys'], message: '남성 태그 필요' },
  { id: 'godrays-needs-path', type: 'require', a: ['god rays', 'dappled sunlight'], b: ['outdoors', 'window', 'forest'], message: '빛줄기에는 광원 경로 필요' },
];

// ---- 상태 ----
const danbooruState = {
  selections: {},      // {categoryKey: string[]} — kind:"tags"만
  texts: {},           // {categoryKey: string}   — kind:"text"만
  locked: {},          // {categoryKey: boolean}  — 세션 상태, 저장 대상 아님(5장)
  nsfwMode: 'nsfw',    // 'sfw' | 'nsfw' — 무작위 조합에서 nsfw 카테고리를 뽑을지. locked와 같은 세션 상태(저장 대상 아님)
  tagEdits: {},        // {categoryKey: {added:[], removed:[]}} — 서버에 영구 저장(6장)
  history: [],         // 서버에 영구 저장(8장)
  lastViolations: [],
};
for(const cat of DANBOORU_CATEGORIES){
  if(cat.kind === 'tags'){
    danbooruState.selections[cat.key] = [];
    danbooruState.locked[cat.key] = !!cat.lockedByDefault;
  }else{
    danbooruState.texts[cat.key] = '';
  }
}

// ---- 유틸 ----
function danbooruPickOne(arr){ return arr[Math.floor(Math.random() * arr.length)]; }
function danbooruRandomInt(min, max){ return Math.floor(Math.random() * (max - min + 1)) + min; }
function danbooruShuffle(arr){
  const a = arr.slice();
  for(let i = a.length - 1; i > 0; i--){
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}
function danbooruDedupe(arr){ return [...new Set(arr)]; }
function danbooruFilterToPool(candidates, pool){ return candidates.filter(t => pool.includes(t)); }

// 6장 — 카테고리의 "지금 실제로 고를 수 있는" 태그 풀 = 기본 풀에서 삭제분을 뺀 뒤
// 추가분을 더한 것. 랜덤 로직도 전부 이 풀을 기준으로 후보를 고른다.
function danbooruEffectivePool(key){
  const cat = DANBOORU_CATEGORY_BY_KEY[key];
  if(!cat || cat.kind !== 'tags') return [];
  const edits = danbooruState.tagEdits[key] || {};
  const removed = new Set(edits.removed || []);
  const added = edits.added || [];
  const kept = cat.tags.filter(t => !removed.has(t));
  for(const t of added) if(!kept.includes(t)) kept.push(t);
  return kept;
}

// ---- 3.4 검증 — 전체 카테고리의 선택 태그를 하나의 집합으로 합쳐 검사한다 ----
function danbooruSelectedTagSet(){
  const set = new Set();
  for(const cat of DANBOORU_CATEGORIES){
    if(cat.kind !== 'tags') continue;
    for(const t of (danbooruState.selections[cat.key] || [])) set.add(t);
  }
  return set;
}
function danbooruSelectedTagSetExcluding(excludeKey){
  const set = new Set();
  for(const cat of DANBOORU_CATEGORIES){
    if(cat.kind !== 'tags' || cat.key === excludeKey) continue;
    for(const t of (danbooruState.selections[cat.key] || [])) set.add(t);
  }
  return set;
}

function danbooruValidate(tagSet){
  const violations = [];
  for(const rule of DANBOORU_RULES){
    if(rule.type === 'atMostOne'){
      const hits = rule.set.filter(t => tagSet.has(t));
      if(hits.length >= 2) violations.push({ ruleId: rule.id, message: rule.message, offendingTags: hits });
    }else if(rule.type === 'forbid'){
      const aHits = rule.a.filter(t => tagSet.has(t));
      const bHits = rule.b.filter(t => tagSet.has(t));
      if(aHits.length > 0 && bHits.length > 0){
        violations.push({ ruleId: rule.id, message: rule.message, offendingTags: danbooruDedupe([...aHits, ...bHits]) });
      }
    }else if(rule.type === 'require'){
      const aHits = rule.a.filter(t => tagSet.has(t));
      const bHits = rule.b.filter(t => tagSet.has(t));
      if(aHits.length > 0 && bHits.length === 0){
        violations.push({ ruleId: rule.id, message: rule.message, offendingTags: aHits });
      }
    }
  }
  // 3.3 특수 규칙 — soloCount: solo가 있는데 counts 그룹에서 2개 이상 선택된 경우
  if(tagSet.has('solo')){
    const countHits = DANBOORU_TAG_GROUPS.counts.filter(t => tagSet.has(t));
    if(countHits.length >= 2){
      violations.push({ ruleId: 'soloCount', message: '인원 수 태그는 solo와 함께 하나만 선택해야 함', offendingTags: danbooruDedupe(['solo', ...countHits]) });
    }
  }
  return violations;
}

function danbooruOffendingTagSet(){
  const set = new Set();
  for(const v of danbooruState.lastViolations) for(const t of v.offendingTags) set.add(t);
  return set;
}

// ---- 4장 — 랜덤 조합 ----
// range(min,max): 개수를 정하고 풀을 섞어 앞에서부터, 넣었을 때 위반 개수가
// 늘지 않는 후보만 채택한다. 대부분의 tags 카테고리가 이 기본 전략을 쓴다.
function danbooruDrawRange(pool, min, max, contextSet){
  if(pool.length === 0) return [];
  const n = danbooruRandomInt(min, max);
  if(n <= 0) return [];
  const shuffled = danbooruShuffle(pool);
  const picked = [];
  let currentSet = new Set(contextSet);
  let beforeCount = danbooruValidate(currentSet).length;
  for(const candidate of shuffled){
    if(picked.length >= n) break;
    const trialSet = new Set(currentSet);
    trialSet.add(candidate);
    const afterCount = danbooruValidate(trialSet).length;
    if(afterCount <= beforeCount){
      picked.push(candidate);
      currentSet = trialSet;
      beforeCount = afterCount;
    }
  }
  return picked;
}

// combo: 조각을 조합하면 모순이 나기 쉬운 인원 구성 전용 — 미리 정의된 유효
// 조합에서 통째로 고른다. 풀 편집으로 조합의 태그 일부가 사라졌으면 그 조합은
// 후보에서 제외한다.
function danbooruDrawCombo(){
  const pool = danbooruEffectivePool('char_composition');
  const valid = DANBOORU_COMBO_PRESETS.filter(combo => combo.every(t => pool.includes(t)));
  if(valid.length === 0) return [];
  return danbooruPickOne(valid).slice();
}

// place: 배경 전용 확률 트리 (4.2)
function danbooruDrawPlace(){
  const pool = danbooruEffectivePool('place');
  const bgFlatAvail = danbooruFilterToPool(DANBOORU_TAG_GROUPS.bgFlat, pool);
  if(bgFlatAvail.length > 0 && Math.random() < 0.18){
    return [danbooruPickOne(bgFlatAvail)];
  }
  const isIndoor = Math.random() < 0.45;
  const result = [];
  const inOutTag = isIndoor ? 'indoors' : 'outdoors';
  if(pool.includes(inOutTag)) result.push(inOutTag);
  const locGroup = danbooruFilterToPool(isIndoor ? DANBOORU_TAG_GROUPS.indoorLoc : DANBOORU_TAG_GROUPS.outdoorLoc, pool);
  if(locGroup.length > 0) result.push(danbooruPickOne(locGroup));
  if(Math.random() < 0.85){
    const timeAvail = danbooruFilterToPool(DANBOORU_TAG_GROUPS.time, pool);
    if(timeAvail.length > 0) result.push(danbooruPickOne(timeAvail));
  }
  if(isIndoor){
    if(Math.random() < 0.40 && pool.includes('window')) result.push('window');
  }else{
    if(Math.random() < 0.40){
      const weatherAvail = danbooruFilterToPool(DANBOORU_TAG_GROUPS.weather, pool);
      if(weatherAvail.length > 0) result.push(danbooruPickOne(weatherAvail));
    }
  }
  if(Math.random() < 0.35 && pool.includes('scenery')) result.push('scenery');
  return danbooruDedupe(result);
}

// light: place가 확정된 이후에 실행 — 시간대(밤/저녁 여부)를 참조한다 (4.2)
function danbooruDrawLight(contextSet){
  const pool = danbooruEffectivePool('light');
  const result = [];
  const dirAvail = danbooruFilterToPool(DANBOORU_TAG_GROUPS.lightDir, pool);
  if(dirAvail.length > 0) result.push(danbooruPickOne(dirAvail));
  if(Math.random() < 0.80){
    const qualAvail = danbooruFilterToPool(DANBOORU_TAG_GROUPS.lightQual, pool);
    if(qualAvail.length > 0) result.push(danbooruPickOne(qualAvail));
  }
  const isNightish = contextSet.has('night') || contextSet.has('evening');
  if(Math.random() < 0.75){
    const sourceAvail = isNightish
      ? danbooruFilterToPool([...DANBOORU_TAG_GROUPS.artificial, 'moonlight'], pool)
      : danbooruFilterToPool([...DANBOORU_TAG_GROUPS.daylight, 'lens flare', 'bloom'], pool);
    if(sourceAvail.length > 0) result.push(danbooruPickOne(sourceAvail));
  }
  if(Math.random() < 0.30){
    const accentAvail = danbooruFilterToPool(DANBOORU_TAG_GROUPS.lightAccent, pool);
    if(accentAvail.length > 0) result.push(danbooruPickOne(accentAvail));
  }
  return danbooruDedupe(result);
}

function danbooruDrawCategory(key, contextSet){
  // SFW 모드에서는 nsfw 카테고리를 아예 뽑지 않는다 — 태그 풀에 뭐가 들어있든
  // 무작위 조합(danbooruRandomizeAll/danbooruRerollCategory/danbooruGenerateSeedPrompts,
  // 셋 다 이 함수를 거침)에서는 배제한다. 수동으로 칩을 클릭해 선택하는 건 이 함수를
  // 안 거치므로 영향받지 않는다.
  if(key === 'nsfw' && danbooruState.nsfwMode === 'sfw') return [];
  const cat = DANBOORU_CATEGORY_BY_KEY[key];
  const strategy = cat.randomStrategy;
  if(strategy.type === 'range') return danbooruDrawRange(danbooruEffectivePool(key), strategy.min, strategy.max, contextSet);
  if(strategy.type === 'combo') return danbooruDrawCombo();
  if(strategy.type === 'place') return danbooruDrawPlace();
  if(strategy.type === 'light') return danbooruDrawLight(contextSet);
  return [];
}

// 고정 안 된 카테고리만 새로 뽑아 "선택 스냅샷"을 돌려주는 순수 버전 — 화면
// 상태(danbooruState.selections)를 건드리지 않는다. 고정된 카테고리는 지금
// 선택을 그대로 복사해서 쓰고, 나머지는 일단 비운 뒤(잔여값이 검증에 끼어들지
// 않게) order 순서대로 채운다 — place → light 순서라 4.3의 "light는 place
// 이후" 조건이 자연히 충족된다. danbooruRandomizeAll(화면용)과 시드 배치의
// "시드마다 다른 프롬프트"(danbooruGenerateSeedPrompts, 여러 번 독립적으로
// 호출해야 해서 전역 상태를 바꾸면 안 됨) 둘 다 이 함수를 재사용한다 — 검증/
// 모순 방지 로직(danbooruDrawCategory 안의 danbooruDrawRange 등)이 한 곳에만
// 있으니 두 기능이 항상 같은 규칙을 따른다.
function danbooruComputeRandomSelections(){
  const selections = {};
  for(const cat of DANBOORU_CATEGORIES){
    if(cat.kind !== 'tags') continue;
    selections[cat.key] = danbooruState.locked[cat.key]
      ? (danbooruState.selections[cat.key] || []).slice()
      : [];
  }
  for(const cat of DANBOORU_CATEGORIES){
    if(cat.kind !== 'tags' || danbooruState.locked[cat.key]) continue;
    const context = new Set();
    for(const c of DANBOORU_CATEGORIES){
      if(c.kind !== 'tags' || c.key === cat.key) continue;
      for(const t of selections[c.key]) context.add(t);
    }
    selections[cat.key] = danbooruDrawCategory(cat.key, context);
  }
  return selections;
}

// 화면(Danbooru 탭)에서 쓰는 전체 무작위 조합 — 위 순수 함수의 결과를 실제
// danbooruState.selections에 반영하고 다시 그린다.
function danbooruRandomizeAll(){
  Object.assign(danbooruState.selections, danbooruComputeRandomSelections());
  danbooruUpdateAll();
}

// 4.4 부분 뽑기 — 이 카테고리만 다시 뽑는다. "나머지 전부를 임시로 고정한 상태에서
// 랜덤 실행"과 동일하므로, 다른 카테고리의 현재 선택을 그대로 컨텍스트로 써서
// 이 카테고리 하나만 새로 그린다(잠금 상태와 무관하게 다른 카테고리는 안 건드림).
function danbooruRerollCategory(key){
  const context = danbooruSelectedTagSetExcluding(key);
  danbooruState.selections[key] = danbooruDrawCategory(key, context);
  danbooruUpdateAll();
}

// ---- 7장 — 프롬프트 조립 ----
function danbooruEscapeTag(tag){
  return tag.replace(/\(/g, '\\(').replace(/\)/g, '\\)');
}
// selections/texts를 인자로 받는 순수 버전 — 화면에 보이는 조립(danbooruAssemblePrompt)과
// 시드 배치의 "시드마다 다른 프롬프트"(danbooruGenerateSeedPrompts) 둘 다 이 함수
// 하나로 조립해서, 조립 규칙(순서/이스케이프/빈 값 건너뛰기)이 항상 똑같이 적용된다.
function danbooruAssembleFromSelections(selections, texts){
  const parts = [];
  for(const cat of DANBOORU_CATEGORIES){
    if(cat.kind === 'tags'){
      const tags = selections[cat.key] || [];
      if(tags.length === 0) continue;
      parts.push(tags.map(danbooruEscapeTag).join(', '));
    }else{
      const text = (texts[cat.key] || '').trim();
      if(!text) continue;
      parts.push(text);
    }
  }
  return parts.join(', ');
}
function danbooruAssemblePrompt(){
  return danbooruAssembleFromSelections(danbooruState.selections, danbooruState.texts);
}

// 시드 배치 전용 — n개를 서로 독립적으로 뽑아 조립된 프롬프트 문자열 배열로
// 돌려준다. danbooruComputeRandomSelections가 화면 상태를 안 건드리는 순수
// 함수라, 이 함수를 여러 번 불러도 Danbooru 탭에 지금 보이는 조합은 그대로다.
// 고정된 카테고리는 매번 같은 값, 나머지는 매번 새로 뽑히며, 모순 방지 검증도
// danbooruDrawCategory를 그대로 재사용하므로 화면의 "무작위 조합"과 동일하게 적용된다.
function danbooruGenerateSeedPrompts(n){
  const prompts = [];
  for(let i = 0; i < n; i++){
    const selections = danbooruComputeRandomSelections();
    prompts.push(danbooruAssembleFromSelections(selections, danbooruState.texts));
  }
  return prompts;
}

// ---- 렌더링 ----
function danbooruRenderCategoryChips(key){
  const container = document.querySelector(`.danbooru-chips[data-key="${key}"]`);
  if(!container) return;
  const pool = danbooruEffectivePool(key);
  const selected = new Set(danbooruState.selections[key] || []);
  const offending = danbooruOffendingTagSet();
  if(pool.length === 0){
    container.innerHTML = `<div class="danbooru-empty-pool">${key === 'artist' ? '태그가 없어요 — 아래 편집(<svg class="ico"><use href="#i-pencil"/></svg>)에서 "by artistname" 형식으로 추가하세요' : '태그가 없어요 — 아래 편집(<svg class="ico"><use href="#i-pencil"/></svg>)에서 추가하세요'}</div>`;
    return;
  }
  container.innerHTML = pool.map(tag => {
    const isSelected = selected.has(tag);
    const isConflict = isSelected && offending.has(tag);
    return `<button type="button" class="dtag${isSelected ? ' selected' : ''}${isConflict ? ' conflict' : ''}" data-tag="${escapeHtml(tag)}">${escapeHtml(tag)}</button>`;
  }).join('');
  container.querySelectorAll('.dtag').forEach(btn => {
    btn.addEventListener('click', () => danbooruToggleTag(key, btn.dataset.tag));
  });
}

function danbooruRenderAllChips(){
  for(const cat of DANBOORU_CATEGORIES){
    if(cat.kind === 'tags') danbooruRenderCategoryChips(cat.key);
  }
}

function danbooruRenderConflicts(){
  const violations = danbooruValidate(danbooruSelectedTagSet());
  danbooruState.lastViolations = violations;
  const el = document.getElementById('danbooru-conflicts');
  if(violations.length === 0){
    el.innerHTML = '';
    el.hidden = true;
    return;
  }
  el.hidden = false;
  el.innerHTML = violations.map(v => `
    <div class="danbooru-conflict">
      <span class="danbooru-conflict-msg"><svg class="ico"><use href="#i-triangle-alert"/></svg> ${escapeHtml(v.message)}</span>
      <span class="danbooru-conflict-tags">${v.offendingTags.map(escapeHtml).join(', ')}</span>
    </div>
  `).join('');
}

// 선택이 바뀔 때마다(칩 클릭/랜덤/텍스트 입력/풀 편집 등) 호출 — 프롬프트와
// 충돌 목록을 다시 계산하고, 충돌에 관여한 칩 표시를 갱신하려고 칩도 다시 그린다
// (카테고리 경계를 넘는 충돌이 있어 다른 카테고리의 칩도 영향받을 수 있음, 3.4).
function danbooruUpdateAll(){
  document.getElementById('danbooru-prompt-text').textContent = danbooruAssemblePrompt() || '(비어 있음)';
  danbooruRenderConflicts();
  danbooruRenderAllChips();
}

function danbooruToggleTag(key, tag){
  const cat = DANBOORU_CATEGORY_BY_KEY[key];
  const current = danbooruState.selections[key] || [];
  let next;
  if(cat.single){
    next = (current.length === 1 && current[0] === tag) ? [] : [tag];
  }else if(current.includes(tag)){
    next = current.filter(t => t !== tag);
  }else{
    next = [...current, tag];
  }
  danbooruState.selections[key] = next;
  danbooruUpdateAll();
}

function danbooruToggleLock(key){
  danbooruState.locked[key] = !danbooruState.locked[key];
  const btn = document.querySelector(`.danbooru-lock-btn[data-key="${key}"]`);
  if(btn) btn.classList.toggle('active', danbooruState.locked[key]);
}

function danbooruTogglePoolEditor(key){
  const editor = document.querySelector(`.danbooru-pool-editor[data-key="${key}"]`);
  const btn = document.querySelector(`.danbooru-edit-toggle-btn[data-key="${key}"]`);
  if(!editor) return;
  const willShow = editor.hidden;
  editor.hidden = !willShow;
  if(btn) btn.classList.toggle('active', willShow);
  if(willShow) danbooruRenderPoolEditor(key);
}

// ---- 6장 — 태그 풀 편집 ----
function danbooruRenderPoolEditor(key){
  const container = document.querySelector(`.danbooru-pool-chips[data-key="${key}"]`);
  if(!container) return;
  const pool = danbooruEffectivePool(key);
  container.innerHTML = pool.map(tag => `
    <span class="dtag-pool-item">${escapeHtml(tag)}<button type="button" class="dtag-pool-remove" data-tag="${escapeHtml(tag)}" aria-label="삭제">&times;</button></span>
  `).join('');
  container.querySelectorAll('.dtag-pool-remove').forEach(btn => {
    btn.addEventListener('click', () => danbooruRemovePoolTag(key, btn.dataset.tag));
  });
}

async function danbooruPersistTagEdits(){
  try{
    await fetch('/api/danbooru/tag-edits', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(danbooruState.tagEdits),
    });
  }catch(e){
    // 저장 실패해도 이번 세션 안에서는 메모리 상태로 계속 동작한다 — 새로고침하면
    // 이번 편집이 유실될 수 있음을 감수한다.
  }
}

function danbooruAddPoolTags(key, rawInput){
  const newTags = rawInput.split(',').map(t => t.trim()).filter(Boolean);
  if(newTags.length === 0) return;
  if(!danbooruState.tagEdits[key]) danbooruState.tagEdits[key] = { added: [], removed: [] };
  const edits = danbooruState.tagEdits[key];
  const pool = danbooruEffectivePool(key);
  for(const tag of newTags){
    const removedIdx = edits.removed.indexOf(tag);
    if(removedIdx !== -1){
      edits.removed.splice(removedIdx, 1); // 예전에 지운 기본 태그를 되살림
    }else if(!pool.includes(tag)){
      edits.added.push(tag);
    }
  }
  danbooruPersistTagEdits();
  danbooruRenderCategoryChips(key);
  danbooruRenderPoolEditor(key);
}

function danbooruRemovePoolTag(key, tag){
  const cat = DANBOORU_CATEGORY_BY_KEY[key];
  if(!danbooruState.tagEdits[key]) danbooruState.tagEdits[key] = { added: [], removed: [] };
  const edits = danbooruState.tagEdits[key];
  const addedIdx = edits.added.indexOf(tag);
  if(addedIdx !== -1){
    edits.added.splice(addedIdx, 1);
  }else if(cat.tags.includes(tag) && !edits.removed.includes(tag)){
    edits.removed.push(tag);
  }
  const sel = danbooruState.selections[key];
  if(sel){
    const i = sel.indexOf(tag);
    if(i !== -1) sel.splice(i, 1);
  }
  danbooruPersistTagEdits();
  danbooruRenderPoolEditor(key);
  danbooruUpdateAll();
}

// ---- 8장 — 기록 ----
function danbooruRenderHistoryList(){
  const list = document.getElementById('danbooru-history-list');
  const empty = document.getElementById('danbooru-history-empty');
  if(danbooruState.history.length === 0){
    list.innerHTML = '';
    empty.style.display = 'block';
    return;
  }
  empty.style.display = 'none';
  list.innerHTML = danbooruState.history.map(entry => {
    const when = new Date(entry.timestamp);
    const whenText = `${pad2(when.getMonth() + 1)}/${pad2(when.getDate())} ${pad2(when.getHours())}:${pad2(when.getMinutes())}`;
    return `
      <div class="danbooru-history-item" data-id="${entry.id}">
        <div class="danbooru-history-info">
          <div class="danbooru-history-name">${escapeHtml(entry.name || '(이름 없음)')}</div>
          <div class="danbooru-history-time">${whenText}</div>
        </div>
        <div class="danbooru-history-actions">
          <button type="button" class="load-btn danbooru-history-load-btn" title="불러오기"><svg class="ico"><use href="#i-folder-open"/></svg> 불러오기</button>
          <button type="button" class="del-btn danbooru-history-delete-btn" title="삭제"><svg class="ico"><use href="#i-trash-2"/></svg> 삭제</button>
        </div>
      </div>
    `;
  }).join('');
  list.querySelectorAll('.danbooru-history-item').forEach(el => {
    const id = el.dataset.id;
    el.querySelector('.danbooru-history-load-btn').addEventListener('click', () => danbooruLoadHistoryEntry(id));
    el.querySelector('.danbooru-history-delete-btn').addEventListener('click', () => danbooruDeleteHistoryEntry(id));
  });
}

async function danbooruLoadHistory(){
  try{
    const res = await fetch('/api/danbooru/history');
    const data = await res.json();
    danbooruState.history = data.history || [];
  }catch(e){
    danbooruState.history = [];
  }
  danbooruRenderHistoryList();
}

async function danbooruSaveHistory(){
  const nameInput = document.getElementById('danbooru-history-name');
  let name = nameInput.value.trim();
  if(!name) name = danbooruAssemblePrompt().slice(0, 34);
  const selection = {};
  const texts = {};
  for(const cat of DANBOORU_CATEGORIES){
    if(cat.kind === 'tags') selection[cat.key] = danbooruState.selections[cat.key] || [];
    else texts[cat.key] = danbooruState.texts[cat.key] || '';
  }
  try{
    const res = await fetch('/api/danbooru/history', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, selection, texts }),
    });
    if(res.ok){
      const entry = await res.json();
      danbooruState.history.unshift(entry);
      danbooruState.history = danbooruState.history.slice(0, 40);
      nameInput.value = '';
      danbooruRenderHistoryList();
    }
  }catch(e){
    // 저장 실패해도 현재 조합(선택 상태)에는 영향 없다.
  }
}

// 불러오기 — 선택과 텍스트를 모두 복원한다. 명세에 없는 카테고리 키는(과거
// 명세로 저장된 기록 등) 무시하고, 누락된 키는 빈 값으로 채운다. 잠금 상태는
// 세션 상태라 건드리지 않는다(5장).
function danbooruLoadHistoryEntry(id){
  const entry = danbooruState.history.find(h => h.id === id);
  if(!entry) return;
  for(const cat of DANBOORU_CATEGORIES){
    if(cat.kind === 'tags'){
      const value = entry.selection ? entry.selection[cat.key] : null;
      danbooruState.selections[cat.key] = Array.isArray(value) ? value.slice() : [];
    }else{
      const value = entry.texts ? entry.texts[cat.key] : null;
      const text = typeof value === 'string' ? value : '';
      danbooruState.texts[cat.key] = text;
      const input = document.querySelector(`.danbooru-text-input[data-key="${cat.key}"]`);
      if(input) input.value = text;
    }
  }
  danbooruUpdateAll();
}

async function danbooruDeleteHistoryEntry(id){
  try{
    await fetch(`/api/danbooru/history/${encodeURIComponent(id)}`, { method: 'DELETE' });
  }catch(e){
    // 무시 — 아래에서 로컬 목록은 어차피 지운다.
  }
  danbooruState.history = danbooruState.history.filter(h => h.id !== id);
  danbooruRenderHistoryList();
}

// ---- Enhance 실험(NS-32) ----
// 조립된 프롬프트를 /api/enhance-prompt(mode=danbooru)로 바로 돌려 탭 안에 보여 준다.
// 작업 큐를 거치지 않으므로 jobs 목록에 작업이 생기지 않는다. 버튼과 결과 칸은
// index.html을 건드리지 않고 여기서 조립된 프롬프트 상자에 덧붙인다.
function danbooruBuildEnhanceDOM(){
  const actions = document.querySelector('.danbooru-prompt-row-actions');
  const sendStatus = document.getElementById('danbooru-send-status');
  if(!actions || !sendStatus) return;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'load-btn';
  btn.id = 'danbooru-enhance-btn';
  btn.title = '작업 대기열 없이 바로 Prompt Enhance를 돌려 봐요';
  btn.innerHTML = ico('sparkles') + ' Enhance 실험';
  actions.insertBefore(btn, actions.firstChild);

  const status = document.createElement('div');
  status.className = 'enhance-status';
  const box = document.createElement('div');
  box.className = 'danbooru-enhance-box';
  box.hidden = true;
  box.innerHTML = `<div class="danbooru-prompt-row">
      <div class="danbooru-prompt-label">Enhance 결과</div>
      <div class="danbooru-prompt-row-actions"></div>
    </div>
    <div class="danbooru-prompt-text" id="danbooru-enhance-text"></div>`;
  const text = box.querySelector('#danbooru-enhance-text');
  box.querySelector('.danbooru-prompt-row-actions').appendChild(createCopyButton(() => text.textContent));
  sendStatus.after(status, box);

  btn.addEventListener('click', async () => {
    const prompt = danbooruAssemblePrompt().trim();
    status.className = 'enhance-status';
    if(!prompt){
      status.textContent = '먼저 태그를 골라 주세요.';
      status.classList.add('error');
      return;
    }
    btn.disabled = true;
    btn.innerHTML = ico('loader-circle', true) + ' 개선하는 중…';
    status.textContent = 'ComfyUI로 요청 중…';
    try{
      const res = await fetch('/api/enhance-prompt', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt, mode: 'danbooru' }),
      });
      const data = await res.json().catch(() => ({}));
      if(!res.ok){
        status.textContent = data.detail || '프롬프트 개선에 실패했어요.';
        status.classList.add('error');
        return;
      }
      text.textContent = data.enhanced || '';
      box.hidden = false;
      status.textContent = '완료 — 조립된 프롬프트는 그대로 두었어요.';
      status.classList.add('success');
    }catch(e){
      status.textContent = '프롬프트 개선에 실패했어요.';
      status.classList.add('error');
    }finally{
      btn.disabled = false;
      btn.innerHTML = ico('sparkles') + ' Enhance 실험';
    }
  });
}
danbooruBuildEnhanceDOM();

