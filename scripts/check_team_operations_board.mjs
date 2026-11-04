import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../app-mods/team-operations/main.js', import.meta.url), 'utf8');
const bridge = readFileSync(new URL('../teamkit/app_bridge.py', import.meta.url), 'utf8');
const journal = readFileSync(new URL('../teamkit/app_board.py', import.meta.url), 'utf8');
const match = source.match(/const kstDayStart = nowSeconds => \{[\s\S]*?\n\};/);
assert.ok(match, 'KST midnight helper is present');
const kstDayStart = vm.runInNewContext(`${match[0]}; kstDayStart`);
assert.equal(kstDayStart(Date.UTC(2026, 10, 3, 15) / 1000), Date.UTC(2026, 10, 3, 15) / 1000);
assert.equal(kstDayStart(Date.UTC(2026, 10, 4, 4, 30) / 1000), Date.UTC(2026, 10, 3, 15) / 1000);
for (const contract of [
  'Asia/Seoul', 'Math.hypot(e.position.x-d.at.x,e.position.y-d.at.y)<=6',
  'this.justDroppedUntil=Date.now()+250', 'this.drag.target={column:"archived",beforeId:null}',
  'rank=(previousRank+nextRank)/2', 'if(gap<1e-6)rank=(index+1)*1000',
  'async persistNormalizedRanks(entries, target, action, cx)',
  'manual_status:action==="waiting"?"waiting":action==="progress"?"progress":action==="done"?"complete"',
  'const restoreAction={waiting:"waiting",progress:"progress",complete:"done"}[oldStatus]??"restore"',
  '.transition("height",motion)', '.border_dashed()',
  'this.openDetail(card.kind,card.raw,true)',
  '.min_w(260)', '오늘 · 활성', '팀별 스윔레인', 'keystroke==="escape"',
]) assert.ok(source.includes(contract), `frontend contract present: ${contract}`);
const gate = source.indexOf('card.p.decision_required && (target.column !== card.category || target.column === "archived")');
const archive = source.indexOf('if(target.column==="archived")');
assert.ok(gate >= 0 && archive > gate, 'decision-required gate precedes archive handling');
for (const contract of ["'reorder'", 'rank=None', "math.isfinite(rank)"])
  assert.ok(journal.includes(contract), `journal contract present: ${contract}`);
for (const contract of ['resource_lanes', "'tickets':tickets"])
  assert.ok(bridge.includes(contract), `bridge contract present: ${contract}`);
console.log('team-operations board source contracts: OK');
const dropMethod = source.match(/  async dropBoardCard\(cx\) \{[\s\S]*?\n  \}\n\n\n  finishChoice/)[0].replace(/\n\n\n  finishChoice$/, '');
const Board = vm.runInNewContext(`(class { ${dropMethod} })`);
const instance = new Board();
const card = {id:'A',kind:'work',category:'waiting',raw:{id:'A',team:'alpha',board:{revision:'a'}},p:{}};
const peer = {id:'B',kind:'work',category:'progress',raw:{id:'B',team:'beta',board:{rank:2000}},p:{}};
instance.boardMode='status';instance.boardCards=[card,peer];
instance.drag={moved:true,card,target:{column:'progress',lane:'beta',beforeId:'B'}};
let saved;
instance.organizeCard=async (...args)=>{saved=args;return true;};
await instance.dropBoardCard({notify(){}});
assert.equal(card.category,'progress');
assert.equal(saved[2],'progress');
assert.equal(saved[4],1000);
const cardSection=source.slice(source.indexOf('    const boardCard = card => {'),source.indexOf('    const column ='));
const startBody=cardSection.match(/const startDrag=\(e,cx\)=>\{(.*?cx\.notify\(\);)\};/s)?.[1];
const cardMove=cardSection.match(/\.on_mouse_move\(\(e,cx\)=>\{(.*?)\}\)\s*\.on_click/s)?.[1];
const boardMove=source.match(/\.on_mouse_move\(\(e,cx\)=>\{(const d=this\.drag;if\(!d\)return;.*?)\}\)/s)?.[1];
const boardDown=source.match(/\.on_mouse_down\("left",\(e,cx\)=>\{(if\(this\.drag\).*?)\}\)/s)?.[1];
assert.ok(startBody && cardMove && boardMove && boardDown);
const harness={card,boardMode:'status',boardCards:[card,peer],boardFocus:{focus(){}},cardHeights:{'work-A':90}};
const bind=(body,withCard=false)=>vm.runInNewContext(`(function(){${withCard?'const card=this.card;':''}return (e,cx)=>{${body}}})`).call(harness);
const cx={notify(){},stop_propagation(){this.stopped=true;}};
bind(startBody,true)({position:{x:300,y:400},local_position:{x:15,y:85}},cx);
assert.ok(harness.drag,'bottom of card starts dragging');
bind(boardDown)({local_position:{x:200,y:300}},cx);
bind(cardMove,true)({position:{x:310,y:410},local_position:{x:2,y:5}},cx);
assert.equal(harness.drag.x,200);assert.equal(harness.drag.y,300);
bind(boardMove)({position:{x:320,y:420},local_position:{x:220,y:320}},cx);
assert.equal(harness.drag.x,220);assert.equal(harness.drag.y,320);
const buttonHandlers=[...cardSection.matchAll(/\.on_mouse_down\("left",(\(_e,cx\)=>cx\.stop_propagation\(\))\)/g)];
assert.equal(buttonHandlers.length,5);
const stop=vm.runInNewContext(buttonHandlers[0][1]);
cx.stopped=false;stop({},cx);assert.equal(cx.stopped,true);
