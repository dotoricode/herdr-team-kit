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
  console.log('PASS queue controls: CAS, double-click fence, unknown-outcome reconciliation, archive/restore, truthful manual state. Native pixels/Esc/outside not exercised.');
})().catch(error=>{console.error(error);process.exitCode=1});
