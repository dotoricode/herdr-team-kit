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
const cx={stop_propagation(){},notify(){},spawn(fn){const task=fn(cx);tasks.push(task);return task},timer:{every(){},after(){}}};
function view(role='list'){
  const v=new context.TeamQueue();
  Object.assign(v,{role,options:{item},data,filter:'active',selected:item,revealed:true,
    config:{python:'python',cli:'cli',config:'config',state:'state'}});return v;
}
function nodes(x){return x?.value?[x,...x.value.kids.flatMap(nodes)]:[]}
function text(x){return x?.value?x.value.kids.map(text).join(' '):typeof x==='string'?x:''}
function id(x,key){return nodes(x).find(x=>x.value.props.id?.[0]===key)}
(async()=>{
  const capacityView=view('operations');
  capacityView.data={...data,capacity:{global_limit:null,available:null,used:4}};
  assert(text(capacityView.render(cx)).includes('상한 없음'),'explicit null global cap renders unlimited');
  capacityView.data={...data,capacity:{available:null,reason:'observation unavailable'}};
  assert(!text(capacityView.render(cx)).includes('상한 없음'),'observation error stays unknown');
  const v=view('detail');let tree=v.render(cx);
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
  console.log('PASS queue controls: CAS, direct selection, asynchronous sync, double-click fence, unknown-outcome polling, archive/restore, truthful manual state. Native pixels/Esc/outside not exercised.');
})().catch(error=>{console.error(error);process.exitCode=1});
