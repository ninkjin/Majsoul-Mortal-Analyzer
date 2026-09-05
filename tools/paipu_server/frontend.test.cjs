const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { test } = require('node:test');

// Python supplies the exact served HTML, including Python string unescaping.
const html = JSON.parse(fs.readFileSync(0, 'utf8'));
const jobId = 'a'.repeat(32);
const response = (data, status = 200) => ({
  ok: status >= 200 && status < 300, status,
  json: async () => data,
  text: async () => typeof data === 'string' ? data : JSON.stringify(data),
});

function page(source, options = {}) {
  const nodes = new Map();
  const makeElement = () => ({
    textContent: '', innerHTML: '', value: '', hidden: false, disabled: false,
    type: 'password', children: [], attributes: {}, style: { setProperty(key, value) { this[key] = value; } },
    appendChild(child) { this.children.push(child); },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    insertAdjacentHTML(_position, value) { this.innerHTML += value; },
    setAttribute(key, value) { this.attributes[key] = String(value); },
    addEventListener() {}, querySelector() { return null; },
  });
  const element = id => {
    if (!nodes.has(id)) nodes.set(id, makeElement());
    return nodes.get(id);
  };
  element('fetch-mode').value = 'remote';
  element('model-name').value = 'mortal.pth';
  element('player').value = 'auto';
  element('resume').hidden = true;
  const controls = ['prevRound', 'nextRound', 'prevChoice', 'nextChoice', 'prevDiff', 'nextDiff', 'prevEvent', 'nextEvent', 'roundSelect'].map(element);
  const storage = options.storage || new Map();
  const timers = new Map();
  let timerId = 0;
  const location = { href: options.url || 'http://127.0.0.1:8765/paipu-analyzer.html', protocol: 'http:' };
  const requests = [];
  const context = vm.createContext({
    console, URL, location,
    document: {
      getElementById: element, createElement: makeElement,
      createTextNode: value => ({ textContent: value }),
      querySelectorAll: () => controls, addEventListener() {},
      documentElement: { style: { setProperty() {} } },
    },
    window: { location, addEventListener() {} },
    history: { replaceState(_state, _unused, url) { location.href = String(url); } },
    sessionStorage: options.sessionStorage || {
      getItem: key => storage.get(key) || null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: key => storage.delete(key),
    },
    fetch: async (url, init = {}) => {
      requests.push({ url, ...init });
      return options.fetch ? options.fetch(url, init) : response({});
    },
    setTimeout(callback) { timers.set(++timerId, callback); return timerId; },
    clearTimeout(id) { timers.delete(id); },
    alert() {}, confirm: () => false,
  });
  const script = source.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/\s+boot\((?:\d+)?\);\s*$/, '');
  vm.runInContext(script, context);
  return {
    element, storage, location, requests, timers, controls, context,
    run: script => vm.runInContext(script, context),
    get: expression => JSON.parse(vm.runInContext(`JSON.stringify(${expression})`, context)),
    flush: () => new Promise(resolve => setImmediate(resolve)),
    async tick() {
      const first = timers.entries().next().value;
      assert.ok(first, 'expected a pending retry');
      timers.delete(first[0]);
      await first[1]();
      await this.flush();
    },
  };
}

function viewer(options) { return page(html.viewer, options); }

test('maximized viewer keeps its original scale', () => {
  const p = viewer();
  assert.deepEqual(p.get('viewerGeometry(1920,1000,1920,1080,0,80)'), {width:1920,height:1000,scale:1});
});

test('windowing scales the entire reference layout without changing its dimensions', () => {
  const p = viewer();
  assert.deepEqual(p.get('viewerGeometry(960,500,1920,1080,0,80)'), {width:1920,height:1000,scale:0.5});
  assert.deepEqual(p.get('viewerGeometry(1200,300,1920,1080,0,80)'), {width:1920,height:1000,scale:0.3});
});

test('first load in a narrow window still uses the display reference', () => {
  const p = viewer();
  const geometry = p.get('viewerGeometry(640,480,1920,1080,16,88)');
  assert.equal(geometry.width,1904);
  assert.equal(geometry.height,992);
  assert.ok(geometry.width * geometry.scale <= 640);
  assert.ok(geometry.height * geometry.scale <= 480);
});

test('maximizing again restores scale one without accumulated shrinking', () => {
  const p = viewer();
  p.run('viewerGeometry(800,500,1920,1080,0,80)');
  assert.equal(p.get('viewerGeometry(1920,1000,1920,1080,0,80).scale'),1);
});

test('resizing an embedded viewport preserves the initial layout reference', () => {
  const p = viewer();
  p.run("window.screen={availWidth:1920,availHeight:1080};window.innerWidth=1280;window.innerHeight=720;window.outerHeight=800;document.documentElement.clientWidth=1280;document.documentElement.clientHeight=720;$('viewer-surface').scrollHeight=1000;resizeViewer()");
  assert.equal(p.element('viewer-surface').style.width,'1920px');
  assert.equal(p.element('viewer-surface').style['--viewer-height'],'1000px');
  p.run('window.innerWidth=960;window.innerHeight=500;document.documentElement.clientWidth=960;document.documentElement.clientHeight=500;resizeViewer()');
  assert.equal(p.element('viewer-surface').style.width,'1920px');
  assert.equal(p.element('viewer-surface').style['--viewer-height'],'1000px');
  assert.equal(p.element('viewer-surface').style.transform,'scale(0.5)');
});

test('settlement dragging converts scaled pointer coordinates to layout coordinates', () => {
  const p = viewer();
  const table = p.element('table');
  table.offsetWidth=800;table.clientWidth=798;table.clientHeight=598;
  table.getBoundingClientRect=()=>({left:100,top:50,width:400});
  p.context.dragBox={style:{},offsetWidth:100,offsetHeight:100,getBoundingClientRect:()=>({left:150,top:100})};
  p.run('startSettlementDrag({target:{closest:()=>dragBox},preventDefault(){},clientX:160,clientY:110});moveSettlementDrag({clientX:200,clientY:140})');
  assert.equal(p.context.dragBox.style.left,'180px');
  assert.equal(p.context.dragBox.style.top,'160px');
});

function boardPage(hand, drawn) {
  const p = viewer();
  p.run(`const board = initialBoard({info:{tehais:[${JSON.stringify(hand)},[],[],[]]}}); board.drawn[0]=${JSON.stringify(drawn)};`);
  return p;
}

test('discarding a red five keeps a newly drawn ordinary five', () => {
  const p = boardPage(['5mr', '1m'], '5m');
  p.run("applyEvent(board,{type:'dahai',actor:0,pai:'5mr',tsumogiri:false},1)");
  assert.deepEqual(p.get('board.hands[0]'), ['1m', '5m']);
  assert.equal(p.get('board.drawn[0]'), null);
});

test('discarding an ordinary five keeps a newly drawn red five', () => {
  const p = boardPage(['5m', '1m'], '5mr');
  p.run("applyEvent(board,{type:'dahai',actor:0,pai:'5m',tsumogiri:false},1)");
  assert.deepEqual(p.get('board.hands[0]'), ['1m', '5mr']);
});

test('tsumogiri preserves the concealed red five', () => {
  const p = boardPage(['5mr', '1m'], '5m');
  p.run("applyEvent(board,{type:'dahai',actor:0,pai:'5m',tsumogiri:true},1)");
  assert.deepEqual(p.get('board.hands[0]'), ['5mr', '1m']);
});

test('ankan consumes the drawn fourth tile and records all four tiles', () => {
  const p = boardPage(['5m', '5m', '5mr', '9p'], '5m');
  p.run("applyEvent(board,{type:'ankan',actor:0,consumed:['5m','5m','5m','5mr']},1)");
  assert.deepEqual(p.get('board.hands[0]'), ['9p']);
  assert.equal(p.get('board.drawn[0]'), null);
  assert.deepEqual(p.get('board.melds[0]'), [['ankan', '5m', '5m', '5m', '5mr']]);
});

test('ankan keeps an unrelated draw in the concealed hand', () => {
  const p = boardPage(['1m', '1m', '1m', '1m'], '9p');
  p.run("applyEvent(board,{type:'ankan',actor:0,consumed:['1m','1m','1m','1m']},1)");
  assert.deepEqual(p.get('board.hands[0]'), ['9p']);
});

test('kakan upgrades the existing pon, including a red fourth tile', () => {
  const p = boardPage(['9p'], '5mr');
  p.run("board.melds[0]=[['pon','5m','5m','5m']]; applyEvent(board,{type:'kakan',actor:0,pai:'5mr',consumed:['5m','5m','5m']},1)");
  assert.deepEqual(p.get('board.hands[0]'), ['9p']);
  assert.deepEqual(p.get('board.melds[0]'), [['kakan', '5m', '5m', '5m', '5mr']]);
  assert.equal(p.get('board.drawn[0]'), null);
});

test('kakan consumes a hand tile and preserves an unrelated draw', () => {
  const p = boardPage(['5m', '9p'], '2s');
  p.run("board.melds[0]=[['pon','5m','5m','5mr']]; applyEvent(board,{type:'kakan',actor:0,pai:'5m',consumed:['5m','5m','5mr']},1)");
  assert.deepEqual(p.get('board.hands[0]'), ['9p', '2s']);
  assert.equal(p.get('board.melds[0][0].length'), 5);
});

test('different chi combinations are differences and do not increase match rate', () => {
  const p = viewer();
  p.run("const a={type:'chi',pai:'3m',consumed:['1m','2m']}; const b={type:'chi',pai:'3m',consumed:['2m','4m']}; state.choices=[{output:a,actual:b,diff:!sameAction(a,b)}]");
  assert.equal(p.get('state.choices[0].diff'), true);
  assert.equal(p.get('wholeGameAiMatchStats().rate'), 0);
});

test('call comparison ignores consumed order but preserves red identity and multiplicity', () => {
  const p = viewer();
  assert.equal(p.get("sameAction({type:'chi',pai:'3m',consumed:['1m','2m']},{type:'chi',pai:'3m',consumed:['2m','1m']})"), true);
  assert.equal(p.get("sameAction({type:'pon',pai:'5m',consumed:['5m','5mr']},{type:'pon',pai:'5m',consumed:['5m','5m']})"), false);
  assert.equal(p.get("sameAction({type:'ankan',consumed:['1m','1m','1m','1m']},{type:'ankan',consumed:['2m','2m','2m','2m']})"), false);
});

test('call highlighting uses the same full-action comparison', () => {
  const p = viewer();
  p.run("PLAYER_ID=0;state.events=[{type:'start_kyoku'},{type:'dahai',actor:3,pai:'3m'},{type:'chi',actor:0,target:3,pai:'3m',consumed:['2m','4m']}];state.outputs=[{event_index:1,reaction:{type:'chi',actor:0,target:3,pai:'3m',consumed:['1m','2m']}}]");
  assert.equal(p.get("buildCallTargetMap({start:0},1)['3:1'].aiMatch"), false);
});

test('red-five recommendations and actual discards remain separately highlighted', () => {
  const p = viewer();
  p.run("PLAYER_ID=0; const c={output:{type:'dahai',pai:'5m'},actual:{type:'dahai',actor:0,pai:'5mr'}}");
  assert.deepEqual(p.get('choiceTileHighlights(c,true)'), { recommended: '5m', actual: '5mr' });
  assert.equal(p.get("tileText('5mr')"), '红5m');
});

test('next/previous difference selects the nearest strict neighbor and stops at boundaries', () => {
  const p = viewer();
  p.run('renderChoice=()=>{};state.choices=Array.from({length:8},(_,index)=>({index,diff:[1,5,6].includes(index)}));state.currentChoice=3;goDiff(1)');
  assert.equal(p.get('state.currentChoice'), 5);
  p.run('goDiff(1)'); assert.equal(p.get('state.currentChoice'), 6);
  p.run('goDiff(1)'); assert.equal(p.get('state.currentChoice'), 6);
  p.run('state.currentChoice=3;goDiff(-1)'); assert.equal(p.get('state.currentChoice'), 1);
  p.run('goDiff(-1)'); assert.equal(p.get('state.currentChoice'), 1);
  p.run('state.currentChoice=0;goDiff(1)'); assert.equal(p.get('state.currentChoice'), 1);
});

test('404 data produces an actionable empty state and disabled navigation', async () => {
  const p = viewer({ fetch: async () => response({ error: 'not found' }, 404) });
  await p.run('boot()');
  assert.equal(p.element('status').textContent, '暂无可用复盘');
  assert.match(p.element('table').innerHTML, /返回分析/);
  assert.ok(p.controls.every(control => control.disabled));
});

for (const [label, body, status] of [
  ['HTTP error', { error: 'forbidden' }, 403],
  ['error object with HTTP 200', { error: 'not found' }, 200],
  ['corrupt JSONL', '{broken', 200],
]) {
  test(`${label} is not reported as a loaded replay`, async () => {
    const p = viewer({ fetch: async url => response(body, status) });
    await p.run('boot()');
    assert.equal(p.element('status').textContent, '加载失败');
    assert.match(p.element('table').innerHTML, /返回分析/);
  });
}

test('empty successful data produces an empty state', async () => {
  const p = viewer({ fetch: async url => url.endsWith('.jsonl') || url.endsWith('/log.json') ? response('') : response({},404) });
  await p.run('boot()');
  assert.equal(p.element('status').textContent, '暂无可用复盘');
});

test('valid replay enables navigation after loading', async () => {
  const events = [{type:'start_game',names:['A','B','C','D']},{type:'start_kyoku',bakaze:'E',kyoku:1,oya:0,scores:[25000,25000,25000,25000],tehais:[[],[],[],[]]},{type:'tsumo',actor:0,pai:'1m'},{type:'dahai',actor:0,pai:'1m',tsumogiri:true},{type:'end_kyoku'}];
  const outputs = [{event_index:2,event:events[2],reaction:{type:'dahai',actor:0,pai:'1m',meta:{q_values:[0],mask_bits:1}}}];
  const p = viewer({fetch:async url => {
    if(url.endsWith('/log.json')) return response(events.map(JSON.stringify).join('\n'));
    if(url.endsWith('.jsonl')) return response(outputs.map(JSON.stringify).join('\n'));
    if(url.endsWith('config.json')) return response({player_id:0});
    return response({},404);
  }});
  await p.run('boot()');
  assert.match(p.element('status').textContent, /已加载 1 局/);
  assert.ok(p.controls.every(control => !control.disabled));
});

function analyzer(options = {}) {
  return page(html.analyzer, { ...options, fetch: async (url, init) => {
    if(url === '/api/models') return response({models:['mortal.pth'],default:'mortal.pth'});
    if(url.startsWith('/api/history')) return response({items:[]});
    return options.fetch ? options.fetch(url, init) : response({job_id:jobId,status:'running',progress:30,step:'运行中'});
  }});
}

test('submission persists only the job ID and prevents repeat submission', async () => {
  const p = analyzer();
  p.element('url').value = 'paipu=test';
  p.element('password').value = 'test-only-password';
  await p.element('start').onclick(); await p.flush();
  await p.element('start').onclick();
  assert.equal(p.storage.get('mortal:active-job'), jobId);
  assert.equal(new URL(p.location.href).searchParams.get('job'), jobId);
  assert.equal(p.requests.filter(r=>r.url==='/api/analyze').length,1);
  assert.equal(p.element('password').value,'');
  assert.equal(p.element('start').disabled,true);
});

test('refresh resumes the saved running job without POSTing another analysis', async () => {
  const p = analyzer({storage:new Map([['mortal:active-job',jobId]])});
  await p.flush();
  assert.equal(p.element('start').disabled,true);
  assert.match(p.element('status').textContent,/30%/);
  assert.ok(p.requests.some(r=>r.url.includes('/api/status?job_id=')));
  assert.ok(!p.requests.some(r=>r.url==='/api/analyze'));
});

test('URL recovery works when browser storage is unavailable', async () => {
  const fail=()=>{throw new Error('storage unavailable');};
  const p=analyzer({url:`http://127.0.0.1:8765/paipu-analyzer.html?job=${jobId}`,sessionStorage:{getItem:fail,setItem:fail,removeItem:fail}});
  await p.flush();
  assert.equal(p.element('start').disabled,true);
  assert.match(p.element('status').textContent,/30%/);
});

test('a cleared completed result does not leave the analyzer permanently busy', async () => {
  const p=analyzer({storage:new Map([['mortal:active-job',jobId]]),fetch:async url=>url==='/api/use-history'?response({error:'result removed'},400):response({status:'done',progress:100})});
  await p.flush();
  assert.equal(p.element('start').disabled,false);
  assert.equal(p.element('resume').hidden,true);
  assert.equal(p.storage.has('mortal:active-job'),false);
  assert.match(p.element('status').textContent,/重新分析/);
});

test('a transient history-download failure can be retried without resubmission', async () => {
  let disconnected=true;
  const p=analyzer({storage:new Map([['mortal:active-job',jobId]]),fetch:async url=>{
    if(url==='/api/use-history') {
      if(disconnected) throw new Error('offline');
      return response({viewer:'/mortal-output-viewer.html'});
    }
    return response({status:'done',progress:100});
  }});
  await p.flush();
  assert.equal(p.element('start').disabled,true);
  assert.equal(p.element('resume').hidden,false);
  disconnected=false;p.element('resume').onclick();await p.flush();
  assert.equal(p.location.href,'/mortal-output-viewer.html');
  assert.ok(!p.requests.some(r=>r.url==='/api/analyze'));
});

test('finished recovered task restores its own history before opening the viewer', async () => {
  const p=analyzer({storage:new Map([['mortal:active-job',jobId]]),fetch:async url=>url==='/api/use-history'?response({viewer:'/mortal-output-viewer.html'}):response({status:'done',progress:100})});
  await p.flush();
  assert.equal(p.location.href,'/mortal-output-viewer.html');
  assert.equal(p.storage.has('mortal:active-job'),false);
  assert.deepEqual(JSON.parse(p.requests.find(r=>r.url==='/api/use-history').body),{job_id:jobId});
});

test('failed task clears the saved ID and allows correction', async () => {
  const p=analyzer({storage:new Map([['mortal:active-job',jobId]]),fetch:async()=>response({status:'error',error:'test failure'})});
  await p.flush();
  assert.equal(p.storage.has('mortal:active-job'),false);
  assert.equal(p.element('start').disabled,false);
  assert.equal(p.element('status').textContent,'test failure');
});

test('missing task after a server restart clears stale recovery state', async () => {
  const p=analyzer({storage:new Map([['mortal:active-job',jobId]]),fetch:async()=>response({error:'job not found'},404)});
  await p.flush();
  assert.equal(p.storage.has('mortal:active-job'),false);
  assert.equal(new URL(p.location.href).searchParams.has('job'),false);
  assert.equal(p.element('start').disabled,false);
  assert.match(p.element('status').textContent,/历史记录/);
});

test('exhausted network retries retain the job and offer safe resumption', async () => {
  let disconnected=true;
  const p=analyzer({storage:new Map([['mortal:active-job',jobId]]),fetch:async()=>{
    if(disconnected) throw new Error('offline');
    return response({status:'running',progress:45});
  }});
  await p.flush();
  for(let i=0;i<5;i++) await p.tick();
  assert.equal(p.element('start').disabled,true);
  assert.equal(p.element('resume').hidden,false);
  assert.equal(p.storage.get('mortal:active-job'),jobId);
  disconnected=false;
  p.element('resume').onclick(); await p.flush();
  assert.match(p.element('status').textContent,/45%/);
  assert.ok(!p.requests.some(r=>r.url==='/api/analyze'));
});

test('credentials are shown only in tensoul mode', () => {
  const p=analyzer();
  assert.equal(p.element('credentials').hidden,true);
  p.element('fetch-mode').value='tensoul';p.element('fetch-mode').onchange();
  assert.equal(p.element('credentials').hidden,false);
  assert.equal(p.element('password').disabled,false);
});
