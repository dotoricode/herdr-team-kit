// Exercise the real mod callbacks with a native-element contract adapter.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
function element(){
  const state={kids:[],props:{},handlers:{}};
  const proxy=new Proxy(state,{get(t,k){
    if(k==='value')return state;
    if(k==='child')return x=>{t.kids.push(x);return proxy};
    if(k==='children')return xs=>{t.kids.push(...xs);return proxy};
    if(k==='when')return (ok,fn)=>ok?fn(proxy):proxy;
    if(k==='on_click')return fn=>{t.handlers.click=fn;return proxy};
    return (...args)=>{t.props[k]=args;return proxy};
  }});return proxy;
}
let calls=[],reply,resolve;
const theme=new Proxy({},{get:(_,k)=>String(k)});
const context={View:class{},div:element,h_flex:element,v_flex:element,theme:()=>theme,
  terminals:()=>[],openOverlay:()=>{},closeOverlay:()=>{},appHome:()=>'/example',readTextFile:async()=>null,
  exec:async(exe,args)=>{calls.push(args);return reply(args)},run:()=>{},modProps:()=>({})};
vm.createContext(context);
const target=process.argv[2]??'app-mods/team-queue/main.js';
vm.runInContext(fs.readFileSync(target,'utf8').replace(/^import .*;$/gm,'').replace('export default class TeamQueue','globalThis.TeamQueue=class TeamQueue'),context);
const item={id:'one',title:'Example task',team_label:'Example',category:'waiting',phase:'pending',
  state_label:'배정 대기',request:'ORIGINAL_REQUEST',board_state:{revision:'revision-one',manual_status:null}};
const archived={...item,id:'archived',category:'archived',board_state:{revision:'archive-revision',can_restore:true}};
const data={schema_version:1,jobs:[item,archived],counts:{},fingerprint:'initial'};
const tasks=[];
let notifies=0;
const cx={stop_propagation(){},notify(){notifies++},spawn(fn){const task=fn(cx);tasks.push(task);return task},timer:{every(){},after(){}}};
function view(role='list'){
  const v=new context.TeamQueue();
  Object.assign(v,{role,options:{item},data,filter:'active',selected:item,revealed:true,
    config:{python:'python',cli:'cli',config:'config',state:'state'}});return v;
}
function nodes(x){return x?.value?[x,...x.value.kids.flatMap(nodes)]:[]}
function text(x){return x?.value?x.value.kids.map(text).join(' '):typeof x==='string'?x:''}
function id(x,key){return nodes(x).find(x=>x.value.props.id?.[0]===key)}
// A click that changes view state must request a redraw (Herdr re-renders only on notify or live-data change).
function clickNotifies(x,key,label){const before=notifies;id(x,key).value.handlers.click({},cx);assert(notifies>before,label+' must call cx.notify()')}
(async()=>{
  const capacityView=view('operations');
  capacityView.data={...data,capacity:{global_limit:null,available:null,used:4}};
  assert(text(capacityView.render(cx)).includes('상한 없음'),'explicit null global cap renders unlimited');
  capacityView.data={...data,capacity:{available:null,reason:'observation unavailable'}};
  assert(!text(capacityView.render(cx)).includes('상한 없음'),'observation error stays unknown');
  capacityView.data={...data,split_worktrees:[{ticket:'T-ONE',path:'/fixture/split',phase:'ready',exists:true}]};
  assert(text(capacityView.render(cx)).includes('분리 작업 폴더 · 정리 후보'));
  assert(text(capacityView.render(cx)).includes('/fixture/split'));
  const v=view('detail');let tree=v.render(cx);
  const isolated=view('detail');isolated.selected={...item,split_worktree:'/fixture/isolated',result_location:'/fixture/isolated/report'};
  const isolatedText=text(isolated.render(cx));
  assert(isolatedText.includes('분리 작업 폴더')&&isolatedText.includes('/fixture/isolated'));
  assert(isolatedText.includes('결과 위치')&&isolatedText.includes('/fixture/isolated/report'));
  assert(id(tree,'queue-organize-done'),'detail must expose card state changes');
  assert(id(tree,'queue-organize-archive'));assert(text(tree).includes('ORIGINAL_REQUEST'));
  reply=async args=>args.includes('organize')?new Promise(r=>resolve=r):{code:0,stdout:JSON.stringify({...data,fingerprint:'saved'})};
  id(tree,'queue-organize-done').value.handlers.click({},cx);
  const operation=tasks[0];
  id(tree,'queue-organize-done').value.handlers.click({},cx);
  assert.equal(calls.length,1,'double click must not send twice');
  assert(calls[0].includes('--expected-revision'));assert(calls[0].includes('revision-one'));
  assert(calls[0].includes('done'));assert(calls[0].includes('--client-request-id'));
  resolve({code:0});await operation;assert.equal(v.pendingAction,null);
  assert.equal(v.selected.phase,'pending','card completion never changes work phase');
  const list=view();assert(!id(list.render(cx),'queue-card-archived'));
  list.filter='archived';assert(id(list.render(cx),'queue-card-archived'));assert(!id(list.render(cx),'queue-card-one'));
  v.selected=archived;tree=v.render(cx);assert(id(tree,'queue-organize-restore'));assert(!id(tree,'queue-organize-done'));
  v.selected={...item,board_state:{revision:'revision-one',manual_status:'complete'}};
  tree=v.render(cx);assert(text(tree).includes('완료 표시'));assert(text(tree).includes('배정 대기'));
  calls=[];reply=async()=>{throw new Error('transport disconnected')};
  await v.organize(cx,'archive');const request=v.pendingAction.request;
  assert(v.actionFailure.includes('결과 불명'));assert(id(v.render(cx),'queue-organize-reconcile'));
  await v.organize(cx,'done');assert.equal(calls.length,1,'unknown outcome blocks different actions');
  reply=async args=>args.includes('organize')?{code:0}:{code:0,stdout:JSON.stringify({...data,fingerprint:'reconciled'})};
  await v.organize(cx,null);assert.equal(calls[1][calls[1].indexOf('--client-request-id')+1],request);
  reply=async args=>args.includes('organize')?{code:1,stderr:'revision conflict'}:{code:0,stdout:JSON.stringify(data)};
  await v.organize(cx,'progress');assert(v.actionFailure.includes('revision conflict'));assert.equal(v.pendingAction,null);
  const sync=view();
  assert(id(sync.render(cx),'queue-sync'),'list exposes actual sync separately from refresh');
  const direct=view('detail');
  assert(id(direct.render(cx),'queue-send-candidates'),'detail exposes direct recipient selection');
  calls=[];reply=async args=>({code:0,stdout:JSON.stringify({send_allowed:true,expected_ticket:'ticket-proof',candidates:[
    {id:'available',alias:'Example worker',account:'personal',role:'implementation',eligible:true},
    {id:'busy',alias:'Busy worker',account:'team',role:'implementation',eligible:false,reason:'수신 확인 대기'}]})});
  await direct.loadCandidates(cx);
  assert(text(direct.render(cx)).includes('Example worker (개인) · implementation'));
  assert(text(direct.render(cx)).includes('수신 확인 대기'));
  assert(!id(direct.render(cx),'queue-send-busy'),'ineligible candidate never has send callback');
  reply=async()=>new Promise(r=>resolve=r);
  const sending=direct.executeAction(cx,'send',direct.candidates.candidates[0]);
  await direct.executeAction(cx,'send',direct.candidates.candidates[0]);
  const executionRequest=direct.pendingExecution.request;
  assert.equal(calls.filter(x=>x.includes('send')).length,1,'double click sends only once');
  assert(calls.at(-1).includes('ticket-proof'));
  resolve({code:0,stdout:JSON.stringify({id:executionRequest,kind:'send',status:'queued'})});await sending;
  assert(direct.pendingExecution,'queued acknowledgement is not completion');
  assert(!text(direct.render(cx)).includes('전송 완료'));
  calls=[];reply=async()=>{throw Error('connection lost')};
  await direct.pollAction(cx);
  assert(direct.pendingExecution);assert(direct.executionFailure.includes('결과 확인 필요'));
  await direct.executeAction(cx,'sync');assert.equal(calls.length,1,'unknown outcome fences other actions');
  reply=async()=>({code:0,stdout:JSON.stringify({id:executionRequest,status:'uncertain',reason:'수신 근거 확인 필요'})});
  await direct.pollAction(cx);assert.equal(direct.pendingExecution.request,executionRequest);
  assert(text(direct.render(cx)).includes('수신 근거 확인 필요'));
  reply=async args=>args.includes('action-status')?{code:0,stdout:JSON.stringify({id:executionRequest,status:'completed',reason:'담당자 수신 기록 확인'})}:{code:0,stdout:JSON.stringify({...data,fingerprint:'action-finished'})};
  await direct.pollAction(cx);assert.equal(direct.pendingExecution,null);
  assert(text(direct.render(cx)).includes('담당자 수신 기록 확인'));
  direct.candidates={send_allowed:false,reason:'승인 필요',expected_ticket:'ticket-proof',candidates:[{id:'available',eligible:true}]};
  calls=[];await direct.executeAction(cx,'send',direct.candidates.candidates[0]);assert.equal(calls.length,0);
  assert(text(direct.render(cx)).includes('승인 필요'));
  const syncing=view();
  reply=async()=>({code:0,stdout:JSON.stringify({id:syncing.pendingExecution.request,status:'queued'})});
  await syncing.executeAction(cx,'sync');
  assert(calls.at(-1).includes('sync'));assert(calls.at(-1).includes('--client-request-id'));
  syncing.acceptAction({id:syncing.pendingExecution.request,kind:'sync',status:'completed',reason:'동기화 확인',result:{observed_count:3,dispatch:{started:['one']},completed:['two']}});
  assert(text(syncing.render(cx)).includes('확인 업무 3 · 새 배정 1 · 검증 종결 1'));
  let tick;
  const initialized=new context.TeamQueue();
  const timerCx={...cx,timer:{every(ms,fn){assert.equal(ms,5000);tick=fn},after(){}}};
  initialized.init({},timerCx);await tasks.at(-1);
  initialized.pendingExecution={kind:'sync',request:'poll-existing'};initialized.config=syncing.config;
  calls=[];reply=async args=>args.includes('action-status')?{code:0,stdout:JSON.stringify({id:'poll-existing',status:'running'})}:{code:0,stdout:JSON.stringify(data)};
  tick(timerCx);await tasks.at(-1);
  assert(calls.some(x=>x.includes('action-status')),'background timer polls pending requests');
  assert(!calls.some(x=>x.includes('send')||x.includes('sync')),'poll never repeats a mutation');
  const denied=view('detail');denied.candidates={send_allowed:true,expected_ticket:'proof',candidates:[{id:'x',eligible:true}]};
  calls=[];reply=async()=>({code:1,stderr:'gate changed'});
  await denied.executeAction(cx,'send',denied.candidates.candidates[0]);
  assert(denied.pendingExecution,'process error without authoritative status remains uncertain');
  assert(denied.executionFailure.includes('gate changed'));
  // A designated account is only marked, never a reason or a gate.
  const mark=view('detail');
  reply=async()=>({code:0,stdout:JSON.stringify({send_allowed:true,expected_ticket:'ticket-mark',candidates:[
    {id:'designated',alias:'Designated',account:'team',role:'reviewer',eligible:true,preferred:true},
    {id:'other',alias:'Other',account:'personal',role:'reviewer',eligible:true,preferred:false}]})});
  await mark.loadCandidates(cx);
  const markText=text(mark.render(cx));
  assert.equal(markText.split('지정 계정').length-1,1,'only the designated candidate is marked');
  // Manual priority send: listed with Korean reasons, explicit confirmation, no bypass of delivery guards.
  const force=view('detail');
  calls=[];reply=async()=>({code:0,stdout:JSON.stringify({send_allowed:false,reason:'배정 가능한 직원 없음',expected_ticket:'ticket-force',candidates:[
    {id:'mismatch',alias:'Other team',account:'team',role:'leader',eligible:false,reason:'executor responsibility or role mismatch',reason_ko:'담당 제품·역할이 이 업무와 맞지 않습니다 / 계정 동시 실행 용량이 가득 찼습니다',reasons:[{code:'role',bypassable:true,reason:'executor responsibility or role mismatch',reason_ko:'담당 제품·역할이 이 업무와 맞지 않습니다'},{code:'capacity',bypassable:true,reason:'profile execution capacity is exhausted or unknown',reason_ko:'계정 동시 실행 용량이 가득 찼습니다'}],override_allowed:true,planned:false},
    {id:'planned',alias:'New worker',account:'team',role:'leader',eligible:false,reason:'profile execution capacity is exhausted or unknown',reason_ko:'계정 동시 실행 용량이 가득 찼습니다',override_allowed:false,planned:true}]})});
  await force.loadCandidates(cx);
  let ftree=force.render(cx);
  assert(text(ftree).includes('배정 불가 · 담당 제품·역할이 이 업무와 맞지 않습니다'),'Korean reason is shown for an unassignable candidate');
  assert(id(ftree,'queue-force-mismatch'),'unassignable candidate offers the force button');
  assert(!text(ftree).includes('지정 계정'),'no designated-account mark without preferred');
  assert(!id(ftree,'queue-send-mismatch'),'normal send stays unavailable');
  assert(!id(ftree,'queue-force-planned'),'a planned new worker cannot be forced');
  assert(text(ftree).includes('새로 만들 직원이라 강제 전송할 수 없습니다'));
  calls=[];await force.executeAction(cx,'send',force.candidates.candidates[0],true);
  assert.equal(calls.length,0,'force without the confirmation step sends nothing');
  clickNotifies(ftree,'queue-force-mismatch','그래도 전송');
  ftree=force.render(cx);
  assert(id(ftree,'queue-force-confirm-mismatch')&&id(ftree,'queue-force-cancel-mismatch'),'confirmation step has confirm and cancel');
  assert(text(ftree).includes('닫기·Esc·바깥 클릭은 전송하지 않고 닫습니다'));
  assert(text(ftree).includes('· 담당 제품·역할이 이 업무와 맞지 않습니다')&&text(ftree).includes('· 계정 동시 실행 용량이 가득 찼습니다'),'confirmation lists every reason the send will ignore');
  assert.equal(calls.length,0,'opening the confirmation sends nothing');
  clickNotifies(ftree,'queue-force-cancel-mismatch','취소');
  ftree=force.render(cx);assert(!id(ftree,'queue-force-confirm-mismatch'));assert(id(ftree,'queue-force-mismatch'));assert.equal(calls.length,0);
  clickNotifies(ftree,'queue-force-mismatch','그래도 전송');
  reply=async args=>({code:0,stdout:JSON.stringify({id:args[args.indexOf('--client-request-id')+1],kind:'send',status:'blocked',reason:'executor draft, dialog or incomplete screen'})});
  const forced=force.executeAction(cx,'send',force.candidates.candidates[0],true);
  await forced;
  const sendCall=calls.filter(x=>x.includes('send')).at(-1);
  assert(sendCall.includes('--override-eligibility')&&sendCall.includes('--reason')&&sendCall.includes('mismatch')&&sendCall.includes('ticket-force'));
  assert(sendCall[sendCall.indexOf('--reason')+1].length<=300);
  assert(text(force.render(cx)).includes('대상 화면에 입력 중인 내용이 있어 전송하지 않았습니다'),'delivery-time draft guard is explained, not hidden');
  assert.equal(force.pendingExecution,null);
  // Team roster: own team by default, other teams behind a toggle, terminal address shown, no doubled account label.
  const roster=view('detail');
  calls=[];reply=async()=>({code:0,stdout:JSON.stringify({send_allowed:true,expected_ticket:'ticket-roster',candidates:[
    {id:'own',alias:'박과장 (팀)',terminal:'t7v',account:'team',role:'leader',eligible:true,other_team:false,hidden_by_default:false},
    {id:'busy',alias:'이사원 (개인)',terminal:'t7t',account:'personal',role:'leader',eligible:false,reason_ko:'직원이 작업 중이거나 준비되지 않았습니다',override_allowed:true,other_team:false},
    {id:'foreign',alias:'정과장 (팀)',terminal:'t9z',account:'team',role:'leader',eligible:false,reason_ko:'담당 제품·역할이 이 업무와 맞지 않습니다',override_allowed:true,other_team:true,hidden_by_default:true},
    {id:'temp',alias:'우누리 (팀)',terminal:'t106',account:'team',role:'leader',eligible:false,reason_ko:'화면에 입력 중인 내용이나 확인 창이 있습니다',override_allowed:true,other_team:false,hidden_by_default:true}]})});
  await roster.loadCandidates(cx);
  let rtree=roster.render(cx);
  assert(id(rtree,'queue-send-own')&&id(rtree,'queue-force-busy'),'own-team staff are listed with send and force actions');
  assert(!id(rtree,'queue-force-foreign')&&!id(rtree,'queue-force-temp'),'other-team and temporary staff are hidden by default');
  assert(text(rtree).includes('다른 팀·임시 직원 보기 (2)'),'toggle shows how many other-team or temporary staff are hidden');
  assert(text(rtree).includes('t7v'),'terminal address is shown');
  assert(!text(rtree).includes('(팀) (팀)')&&!text(rtree).includes('(개인) (개인)'),'account label is not doubled');
  clickNotifies(rtree,'queue-other-team-toggle','다른 팀·임시 직원 보기');
  rtree=roster.render(cx);
  assert(id(rtree,'queue-force-foreign')&&id(rtree,'queue-force-temp'),'hidden staff can be force-sent after the toggle');
  const plainOnly=view('detail');plainOnly.candidates={send_allowed:false,expected_ticket:'t',candidates:[{id:'x',eligible:false,override_allowed:false}]};
  plainOnly.forceCandidate=plainOnly.candidates.candidates[0];calls=[];
  await plainOnly.executeAction(cx,'send',plainOnly.candidates.candidates[0],true);assert.equal(calls.length,0,'non-overridable candidate is never forced');
  const legacy=view('detail');legacy.selected={...item,execution_actions:{legacy:true,send_allowed:false}};calls=[];
  await legacy.loadCandidates(cx);assert.equal(calls.length,0,'legacy request never calls the ticket backend');
  assert(text(legacy.render(cx)).includes('구형 요청이라 이 창에서 전송할 수 없습니다'));
  const unknown=view('detail');calls=[];reply=async()=>({code:1,stderr:'unknown ticket'});
  await unknown.loadCandidates(cx);
  assert(!text(unknown.render(cx)).includes('구형 요청이라 이 창에서 전송할 수 없습니다'),'unknown ticket without a legacy marker is not called a legacy request');
  assert(text(unknown.render(cx)).includes('unknown ticket'),'the real backend error stays visible');
  // The strip keeps the FIFO head visible when the track is narrow and folds newer jobs.
  const strip=view('strip');
  const jobs=Array.from({length:5},(_,i)=>({...item,id:'lane-'+i,title:'Job '+i,created_at:i+1}));
  Object.assign(strip,{data:{...data,jobs},trackWidth:750,entering:new Set(),leaving:new Map()});
  let laneTree=strip.render(cx);
  assert(id(laneTree,'queue-lane-lane-0'),'oldest head must remain visible');
  assert(id(laneTree,'queue-lane-lane-1'),'next oldest card fits');
  assert(!id(laneTree,'queue-lane-lane-2'),'newer excess cards must be folded');
  assert(text(id(laneTree,'queue-lane-overflow')).includes('+3'),'overflow chip reports hidden cards');
  assert(id(laneTree,'queue-lane-exit-intake'),'exit includes an intake');
  assert(text(id(laneTree,'queue-lane-lane-0')).includes('다음'),'oldest card is labelled next');
  const pos0=id(laneTree,'queue-lane-lane-0').value.props.left[0];
  const pos1=id(laneTree,'queue-lane-lane-1').value.props.left[0];
  assert(pos1<pos0,'newer card sits to the left of the head');
  let timers=[];const laneCx={...cx,timer:{after(ms,fn){timers.push({ms,fn})},every(){}}};
  strip.trackLane(laneCx,strip.data,{...data,jobs:jobs.slice(1)});
  strip.data={...data,jobs:jobs.slice(1)};
  laneTree=strip.render(cx);
  const departing=id(laneTree,'queue-lane-lane-0');
  assert.equal(departing.value.props.left[0],pos0,'departing card begins at its previous position');
  timers.find(x=>x.ms===32).fn(laneCx);
  laneTree=strip.render(cx);
  const moving=id(laneTree,'queue-lane-lane-0');
  assert(moving.value.props.left[0]>pos0,'departing card moves toward the measured track end');
  assert.equal(moving.value.props.opacity[0],0,'departing card fades');
  assert.equal(moving.value.props.w[0],1,'departing card narrows');
  assert.equal(moving.value.props.h[0],1,'departing card shrinks vertically');
  assert(moving.value.props.top[0]>departing.value.props.top[0],'departure collapses toward the rail center');
  assert(timers.some(x=>x.ms>=500&&x.ms<=800),'departure persists for the animation duration');
  const narrow=view('strip');Object.assign(narrow,{data:{...data,jobs},trackWidth:750,entering:new Set(),leaving:new Map()});
  const narrowTree=narrow.render(cx), beforeResize=notifies;
  id(narrowTree,'queue-lane-track').value.props.on_size_change[0]({width:430,height:82},cx);
  assert(notifies>beforeResize,'track resize requests a redraw');
  const resized=narrow.render(cx);
  assert(id(resized,'queue-lane-lane-0')&&!id(resized,'queue-lane-lane-1'),'resize keeps only the oldest head');
  assert(text(id(resized,'queue-lane-overflow')).includes('+4'),'resize updates overflow count');
  narrow.data={...data,jobs:[jobs[0]]};narrow.trackWidth=200;
  const oneNarrow=narrow.render(cx);
  assert(id(oneNarrow,'queue-lane-lane-0'),'head fits a track narrower than one card');
  assert(!id(oneNarrow,'queue-lane-overflow'),'a single card never reserves an empty overflow chip');
  timers.find(x=>x.ms===700).fn(laneCx);
  assert(!id(strip.render(cx),'queue-lane-lane-0'),'departure is removed after its animation');
  console.log('PASS queue controls: CAS, direct selection, asynchronous sync, double-click fence, unknown-outcome polling, archive/restore, truthful manual state. Manual priority send confirmation and legacy notice covered. Native pixels/Esc/outside not exercised.');
})().catch(error=>{console.error(error);process.exitCode=1});
