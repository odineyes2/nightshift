// CSV 편집기 수식 계산 — 문자열만 평가하고 동적 코드 실행은 쓰지 않는다
function csvColumnLabel(index){
  let label = '';
  for(index++; index > 0; index = Math.floor((index - 1) / 26)) label = String.fromCharCode(65 + (index - 1) % 26) + label;
  return label;
}

function csvFormulaTokens(text){
  const tokens = [];
  for(let i = 0; i < text.length;){
    if(/\s/.test(text[i])){ i++; continue; }
    const match = /^("(?:[^"]|"")*"|\$?[A-Za-z]+\$?[1-9]\d*|[A-Za-z]+|[&(),:])/.exec(text.slice(i));
    if(!match) throw new Error('#오류');
    tokens.push(match[0]); i += match[0].length;
  }
  return tokens;
}

function csvFormulaReference(token){
  const match = /^\$?([A-Za-z]+)\$?([1-9]\d*)$/.exec(token || '');
  if(!match) return null;
  let col = 0;
  for(const char of match[1].toUpperCase()) col = col * 26 + char.charCodeAt(0) - 64;
  return {col: col - 1, row: Number(match[2]) - 2};
}

function evaluateCsvFormulas(schema, rows){
  const cache = new Map(), active = new Set(), errors = [];
  function cell(r, c){
    if(c < 0 || c >= schema.length || r < -1 || r >= rows.length) return '';
    if(r === -1) return schema[c].name;
    const key = `${r}:${c}`;
    if(active.has(key)) throw new Error('#순환');
    if(cache.has(key)) return cache.get(key);
    const raw = String(rows[r][schema[c].name] ?? '');
    if(!raw.startsWith('=')) return raw;
    active.add(key);
    try{
      const tokens = csvFormulaTokens(raw.slice(1)); let pos = 0;
      const take = expected => { if(tokens[pos++] !== expected) throw new Error('#오류'); };
      function scalar(value){ if(Array.isArray(value)) throw new Error('#오류'); return value; }
      function expression(){
        let value = atom();
        while(tokens[pos] === '&'){ pos++; value = scalar(value) + scalar(atom()); }
        return value;
      }
      function atom(){
        const token = tokens[pos++];
        if(!token) throw new Error('#오류');
        if(token.startsWith('"')) return token.slice(1, -1).replace(/""/g, '"');
        const ref = csvFormulaReference(token);
        if(ref){
          if(tokens[pos] !== ':') return cell(ref.row, ref.col);
          pos++; const end = csvFormulaReference(tokens[pos++]);
          if(!end) throw new Error('#오류');
          const values = [];
          // 범위 밖은 빈 값이므로 실제 표와 겹치는 부분만 순회한다.
          for(let rr = Math.max(-1, Math.min(ref.row, end.row)); rr <= Math.min(rows.length - 1, Math.max(ref.row, end.row)); rr++)
            for(let cc = Math.max(0, Math.min(ref.col, end.col)); cc <= Math.min(schema.length - 1, Math.max(ref.col, end.col)); cc++) values.push(cell(rr, cc));
          return values;
        }
        const name = token.toUpperCase();
        if(name !== 'CONCAT' && name !== 'TEXTJOIN') throw new Error('#오류');
        take('('); const args = [];
        if(tokens[pos] !== ')'){
          args.push(expression());
          while(tokens[pos] === ','){ pos++; args.push(expression()); }
        }
        take(')');
        if(name === 'CONCAT') return args.flat().join('');
        if(args.length < 2) throw new Error('#오류');
        const separator = scalar(args.shift());
        return args.flat().filter(value => value !== '').join(separator);
      }
      const value = scalar(expression());
      if(pos !== tokens.length) throw new Error('#오류');
      cache.set(key, value); return value;
    }finally{ active.delete(key); }
  }
  const values = rows.map((row, r) => Object.fromEntries(schema.map((col, c) => {
    try{ return [col.name, cell(r, c)]; }
    catch(error){ const value = error.message === '#순환' ? '#순환' : '#오류'; errors.push({address: csvColumnLabel(c) + (r + 2), value}); return [col.name, value]; }
  })));
  return {rows: values, errors};
}

function shiftFormulaRows(formula, delta){
  if(!String(formula).startsWith('=')) return formula;
  // 문자열 리터럴 안의 칸 이름과 $가 붙은 행 번호는 옮기지 않는다.
  return formula.replace(/"(?:[^"]|"")*"|\$?[A-Za-z]+(\$?)([1-9]\d*)/g, (token, fixed, row) => {
    if(token.startsWith('"') || fixed === '$') return token;
    return token.slice(0, -row.length) + Math.max(1, Number(row) + delta);
  });
}
