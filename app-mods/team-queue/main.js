import { View, div } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { appHome, readTextFile, exec, theme, terminals, run, props as modProps, openOverlay, closeOverlay, watchFolder } from "herdr";

const MOD = "team-queue";
const GROUPS = [["active", "현재"], ["records", "이전·보류"], ["archived", "보관함"], ["all", "전체"]];
const provider = x => ({ codex: "Codex", claude: "Claude" })[x] ?? "미확인";
const account = x => ({ personal: "개인", team: "회사" })[x] ?? "계정 미확인";
const clock = x => x == null ? "—" : new Date(x * 1000).toLocaleString();
const percent = x => x == null ? "미제공" : typeof x === "number" && Number.isFinite(x) ? String(x)+"%" : "미확인";
const periodUsage = (value,key) => value.not_applicable?.includes(key) ? "해당 없음" : percent(value[key]?.used);
const clean = x => String(x ?? "").replaceAll("장부", "작업 기록");
const manualState = item => ({waiting:"대기 표시",progress:"진행 표시",complete:"완료 표시"})[item.board_state?.manual_status] ?? null;
const categoryColor = (category, t) => ({ attention: t.blocked, waiting: t.idle, progress: t.working, history: t.muted })[category] ?? t.muted;
// Each word is its own unbroken native text item; long paths wrap only at separators.
const textTokens = value => clean(value).split(/\s+/).filter(Boolean).flatMap(word => word.length > 48 ? word.replace(/([/._:\-])/g, "$1\u200b").split("\u200b") : [word]);
const words = value => v_flex().min_w(0).gap(3).children(clean(value).split("\n").map(line =>
  h_flex().min_w(0).max_w_full().flex_wrap().gap_x(4).gap_y(2)
    .children(line.trim() ? textTokens(line).map(token => div().whitespace_nowrap().child(token)) : [div().child(" ")])));
const pixelCorners=(outside)=>[
  div().absolute().left(-2).top(-2).w(10).h(4).bg(outside),div().absolute().left(-2).top(-2).w(4).h(10).bg(outside),
  div().absolute().right(-2).top(-2).w(10).h(4).bg(outside),div().absolute().right(-2).top(-2).w(4).h(10).bg(outside),
  div().absolute().left(-2).bottom(-2).w(10).h(4).bg(outside),div().absolute().left(-2).bottom(-2).w(4).h(10).bg(outside),
  div().absolute().right(-2).bottom(-2).w(10).h(4).bg(outside),div().absolute().right(-2).bottom(-2).w(4).h(10).bg(outside)
];
// Shared by the list cards and the strip rows: native terminal state confirms "working".
const nativeStatus = (item, list) => { const x=list.find(x=>x.address===item.receiver_terminal); return x?.status?.state ?? x?.status; };
const verifiedWorking = (item, list) => item.phase==="running" && item.verified_working===true && !item.completion_intent && !item.work_completion_confirmed && item.receiver_matches_current===true &&
  nativeStatus(item, list)==="working";
const itemState = (item, list) => item.category==="archived" ? "보관됨" : manualState(item) ?? (item.work_completion_confirmed ? "업무 완료" : verifiedWorking(item, list) ? "진행 중" : item.phase==="running" ? "결과 제출 대기" : item.phase==="received" ? "전달됨 · 수신 대기" : item.state_label);
const itemColor = (item, list, t) => item.requires_user_decision ? t.blocked : verifiedWorking(item, list) ? t.working : item.phase==="received" ? t.muted : categoryColor(item.category,t);
const isActive = item => !["history","deferred","archived"].includes(item.category);
// --- lane (pure): which jobs wait in the FIFO strip, which are already taken ---
// BUSY_PHASES mirrors teamkit/queue_policy.py:14. A job in one of these phases was taken from the queue.
const BUSY_PHASES = new Set(["active","claimed","working","running","sending","dispatching","waiting_handoff","received","done","uncertain"]);
const ENDED_PHASES = new Set(["completed","cancelled","closed"]);
const isOpenJob = item => !["history","deferred","archived"].includes(item.category);
const isTaken = item => isOpenJob(item) && BUSY_PHASES.has(item.phase);
const isQueued = item => isOpenJob(item) && !BUSY_PHASES.has(item.phase) && !ENDED_PHASES.has(item.phase);
const arrival = item => item.created_at ?? item.updated_at ?? null;
const fifo = (a, b) => {
  const x = arrival(a) ?? Infinity, y = arrival(b) ?? Infinity;
  return x === y ? String(a.id).localeCompare(String(b.id)) : x < y ? -1 : 1;
};
const needsAttention = item => item.requires_user_decision === true || item.category === "attention";
const splitLane = jobs => ({
  queued: jobs.filter(isQueued).sort(fifo),
  taken: jobs.filter(isTaken),
});
// --- /lane ---
const shortTeam = label => String(label ?? "").replace(/^\s*\d+\s*·\s*/, "").replace(/\s*(개발팀|팀장|팀)$/, "") || "팀 미확인";
const elapsed = seconds => seconds < 3600 ? Math.max(1, Math.floor(seconds/60))+"m" : seconds < 172800 ? Math.floor(seconds/3600)+"h" : Math.floor(seconds/86400)+"d";
const badge = (label, color, t) => div().flex_none().whitespace_nowrap().rounded(0).border_1().border_color(color).px(6).py(2).bg(t.raised).text_color(color).text_size(11).font_semibold().child(label);
const button = (id, label, callback, t) => div().id(id).relative().flex_none().px(12).py(7).rounded(0).border_2().border_color(t.dim).bg(t.raised).children(pixelCorners(t.background)).text_color(t.foreground).text_size(13).cursor_pointer()
  .hover(el => el.bg(t.surface)).on_click((_event,cx) => { cx.stop_propagation(); callback(cx); }).child(label);

export default class TeamQueue extends View {
  init(props,cx) {
    this.options = props ?? modProps() ?? {};
    this.role = this.options.role ?? "list";
    this.data = null; this.failure = null; this.inFlight = false;
    this.actionFlight = false; this.actionFailure = null; this.pendingAction = null;
    this.executionFlight=false; this.executionFailure=null; this.executionResult=null; this.pendingExecution=null;
    this.candidates=null; this.candidateFlight=false; this.candidateFailure=null;
    this.filter = "active"; this.selected = this.options.item ?? null;
    this.revealed = this.role !== "detail";
    this.entering = new Set(); this.leaving = new Map(); this.minute = 0; this.again = false;
    this.requestOpen = false; this.takenFlash = false;
    cx.spawn(async cx => {
      try {
        const local = this.options.local ?? appHome() + "/mods/" + MOD + "/local.json";
        this.config = JSON.parse(await readTextFile(local) ?? "{}");
        await this.refresh(cx);
      } catch(error) { this.failure = String(error); cx.notify(); return; }
      if(this.role === "strip") this.watchRequests(cx);
    });
    cx.timer.every(5000,cx => { if(this.config) cx.spawn(async cx => { await this.refresh(cx); if(this.pendingExecution) await this.pollAction(cx); }); });
    if(this.role === "detail") cx.timer.after(32,cx => { this.revealed = true; cx.notify(); });
  }
  async refresh(cx) {
    if(!this.config) return;
    if(this.inFlight) { this.again = true; return; }
    this.inFlight = true;
    try {
      const c=this.config;
      const result=await exec(c.python,[c.cli,"--config",c.config,"--state",c.state,"queue","status","--board"]);
      if(result.code !== 0) throw new Error(result.stderr || "요청 큐를 읽을 수 없습니다");
      const incoming=JSON.parse(result.stdout);
      if(incoming.schema_version !== 1 || !Array.isArray(incoming.jobs) || !incoming.counts || !incoming.fingerprint) throw new Error("요청 큐 자료 형식을 확인해 주세요");
      const changed=this.failure || this.data?.fingerprint !== incoming.fingerprint;
      if(this.role === "strip" && changed) this.trackLane(cx, this.data, incoming);
      this.failure=null; this.data=incoming;
      const minute=Math.floor(Date.now()/60000);
      if(this.role === "strip" && minute !== this.minute) { this.minute=minute; cx.notify(); }
      if(changed) { if(this.role === "detail") this.selected=incoming.jobs.find(x => x.id === this.options.item?.id) ?? this.selected; cx.notify(); }
    } catch(error) {
      const message=String(error);
      if(this.failure !== message) { this.failure=message; cx.notify(); }
    } finally {
      this.inFlight=false;
      if(this.again) { this.again=false; await this.refresh(cx); }
    }
  }
  // Request files changing means a job came in or was taken: refresh now instead of waiting for the 5s poll.
  async watchRequests(cx) {
    const dir=String(this.config.state ?? "").replace(/\/[^/]*$/, "") + "/pool-requests";
    try { for(;;) { await watchFolder(dir); await this.refresh(cx); } }
    catch(_error) { /* no watch: the 5s poll keeps the strip current */ }
  }
  // New lane cards fade in; cards that left (taken) fade out before they are dropped.
  trackLane(cx, before, after) {
    if(!before) return;
    const was=splitLane(before.jobs).queued, now=splitLane(after.jobs).queued;
    const nowIds=new Set(now.map(x=>x.id)), wasIds=new Set(was.map(x=>x.id));
    const fresh=now.filter(x=>!wasIds.has(x.id)), gone=was.filter(x=>!nowIds.has(x.id));
    for(const x of fresh) { this.entering.add(x.id); this.leaving.delete(x.id); }
    for(const x of gone) this.leaving.set(x.id, x);
    if(fresh.length) cx.timer.after(32, cx => { this.entering.clear(); cx.notify(); });
    if(gone.length) cx.timer.after(420, cx => { for(const x of gone) this.leaving.delete(x.id); cx.notify(); });
    if(splitLane(before.jobs).taken.length !== splitLane(after.jobs).taken.length) {
      this.takenFlash=true;
      cx.timer.after(450, cx => { this.takenFlash=false; cx.notify(); });
    }
  }
  async organize(cx, action) {
    if(this.actionFlight || this.executionFlight || this.pendingExecution || !this.config || !this.selected?.board_state) return;
    if(this.pendingAction && action !== null) return;
    if(!this.pendingAction) this.pendingAction = {
      id:this.selected.id, action, revision:this.selected.board_state.revision,
      request:"queue-ui-"+Date.now().toString(36)+"-"+Math.random().toString(36).slice(2)
    };
    const pending=this.pendingAction, c=this.config;
    this.actionFlight=true; this.actionFailure=null; cx.notify();
    try {
      const result=await exec(c.python,[c.cli,"--config",c.config,"--state",c.state,"queue","organize",pending.id,
        "--board-action",pending.action,"--client-request-id",pending.request,"--expected-revision",pending.revision]);
      if(result.code !== 0) {
        this.pendingAction=null;
        throw new Error(result.stderr || "카드 변경을 저장하지 못했습니다. 최신 상태를 확인하세요");
      }
      this.pendingAction=null;
      await this.refresh(cx);
    } catch(error) {
      this.actionFailure=String(error)+(this.pendingAction ? " · 결과 불명: 재확인은 같은 요청 ID를 사용합니다" : "");
    } finally { this.actionFlight=false; cx.notify(); }
  }
  async queueCall(args) {
    const c=this.config;
    const result=await exec(c.python,[c.cli,"--config",c.config,"--state",c.state,"queue",...args]);
    if(result.code !== 0) throw new Error(result.stderr || "업무 동작의 결과를 확인할 수 없습니다");
    return JSON.parse(result.stdout);
  }
  async loadCandidates(cx) {
    if(this.candidateFlight || this.pendingExecution || !this.config || !this.selected) return;
    this.candidateFlight=true; this.candidateFailure=null; cx.notify();
    try {
      const result=await this.queueCall(["candidates",this.selected.id]);
      if(!Array.isArray(result.candidates)) throw new Error("담당자 후보 자료 형식을 확인하세요");
      this.candidates=result;
    } catch(error) { this.candidates=null; this.candidateFailure=String(error); }
    finally { this.candidateFlight=false; cx.notify(); }
  }
  async executeAction(cx,kind,candidate) {
    if(this.executionFlight || this.pendingExecution || this.actionFlight || this.pendingAction || !this.config) return;
    if(kind === "send" && (!this.selected || this.candidates?.send_allowed !== true ||
      !this.candidates.expected_ticket || candidate?.eligible !== true ||
      !this.candidates.candidates.some(x=>x.id===candidate.id && x.eligible===true))) return;
    const pending={kind,request:"queue-action-ui-"+Date.now().toString(36)+"-"+Math.random().toString(36).slice(2)};
    this.pendingExecution=pending; this.executionFlight=true; this.executionFailure=null; this.executionResult=null; cx.notify();
    try {
      const args=kind === "send" ? ["send",this.selected.id,"--candidate-id",candidate.id,
        "--expected-ticket",this.candidates.expected_ticket] : ["sync"];
      this.acceptAction(await this.queueCall([...args,"--client-request-id",pending.request]));
      if(!this.pendingExecution) await this.refresh(cx);
    } catch(error) { this.executionFailure=String(error)+" · 결과 확인 필요: 같은 요청 ID로 조회합니다"; }
    finally { this.executionFlight=false; cx.notify(); }
  }
  acceptAction(result) {
    if(result.id !== this.pendingExecution?.request || !["queued","running","completed","blocked","uncertain","failed"].includes(result.status))
      throw new Error("업무 동작 응답의 요청 ID·상태를 확인하세요");
    this.executionResult=result; this.executionFailure=null;
    if(["completed","blocked","failed"].includes(result.status)) { this.pendingExecution=null; this.candidates=null; }
  }
  async pollAction(cx) {
    if(this.executionFlight || !this.pendingExecution || !this.config) return;
    this.executionFlight=true;
    try {
      this.acceptAction(await this.queueCall(["action-status","--client-request-id",this.pendingExecution.request]));
      if(!this.pendingExecution) await this.refresh(cx);
    } catch(error) { this.executionFailure=String(error)+" · 결과 확인 필요: 새 전송 없이 조회만 합니다"; }
    finally { this.executionFlight=false; cx.notify(); }
  }
  actionStatus(t) {
    const result=this.executionResult;
    const sync=result?.kind === "sync" && result?.status === "completed" ? result.result : null;
    const label=({queued:"처리 접수 · 결과 확인 대기",running:"업무 상태 확인·처리 중",completed:"요청 처리 확인",blocked:"처리 불가",uncertain:"결과 불명 · 재전송 금지",failed:"처리 실패"})[result?.status];
    return v_flex().gap(5)
      .when(Boolean(this.pendingExecution)&&!result,el=>el.child(words("요청 결과 확인 중")))
      .when(Boolean(label),el=>el.child(words(label+(result.reason?" · "+result.reason:""))))
      .when(Boolean(sync),el=>el.child(words("확인 업무 "+String(sync.observed_count??"미확인")+" · 새 배정 "+String(sync.dispatch?.started?.length??"미확인")+" · 검증 종결 "+String(sync.completed?.length??"미확인"))))
      .when(Boolean(this.executionFailure),el=>el.child(words(this.executionFailure).text_color(t.blocked)))
      .when(Boolean(this.pendingExecution),el=>el.child(button("queue-action-recheck",this.executionFlight?"확인 중…":"동작 결과 재확인",cx=>cx.spawn(async cx=>this.pollAction(cx)),t)));
  }
  detail(item) {
    openOverlay({ mod:MOD, props:{ role:"detail", item, local:this.options.local }, kind:"dialog", modal:false, frame:"none", width:0.72, height:0.82 });
  }
  operations() {
    openOverlay({ mod:MOD, props:{ role:"operations", local:this.options.local }, kind:"dialog", modal:false, frame:"none", width:0.66, height:0.76 });
  }
  stripStatus() {
    const r=this.executionResult;
    const label=({queued:"처리 접수",running:"처리 중",completed:"처리 확인",blocked:"처리 불가",uncertain:"결과 불명",failed:"처리 실패"})[r?.status];
    if(this.executionFailure) return String(this.executionFailure);
    if(this.pendingExecution&&!r) return "요청 결과 확인 중";
    return label ? label+(r.reason?" · "+r.reason:"") : "";
  }
  renderStrip(cx,t,base,nativeTerminals) {
    const now=Date.now()/1000;
    const {queued,taken}=splitLane(this.data?.jobs??[]);
    const attention=taken.filter(needsAttention).length;
    const queuedIds=new Set(queued.map(x=>x.id));
    // lane: oldest (head) first, as splitLane defines it. Rendering reverses it: newest at the left entrance, head at the right exit.
    const lane=[...queued,...[...this.leaving.values()].filter(x=>!queuedIds.has(x.id))].sort(fifo);
    const status=this.stripStatus();
    const openList=()=>openOverlay({ mod:MOD, props:{ role:"list", local:this.options.local }, kind:"dialog", modal:false, frame:"none", width:0.72, height:0.82 });
    const linkBtn=(id,label,cb)=>div().id(id).flex_none().whitespace_nowrap().px(10).h(24).flex().items_center().border_1().border_color(t.dim).text_size(13).text_color(t.foreground).cursor_pointer()
      .hover(el=>el.bg(t.raised)).on_click((_e,c)=>{c.stop_propagation();cb(c);}).child(label);
    // Motion is native only: width/left/opacity transitions and loops run without this script.
    const CARD_W=320, RING=3, ARROW_W=26, SLOT_W=CARD_W+RING*2+ARROW_W, CARD_H=68;
    const card=(item,head)=>{
      const leaving=!queuedIds.has(item.id), entering=this.entering.has(item.id), hidden=leaving||entering;
      const alert=needsAttention(item), ts=arrival(item);
      const age=ts==null?"":elapsed(Math.max(0,now-ts))+" 대기";
      return h_flex().id("queue-lane-"+item.id).flex_none().items_center().overflow_hidden()
        .w(hidden?0:SLOT_W).opacity(hidden?0:1)
        .transition("width",{duration:340,easing:"ease-out"}).transition("opacity",{duration:260,easing:"ease-out"})
        .child(h_flex().relative().flex_none().items_center().w(SLOT_W).left(leaving?60:entering?-60:0)
          .transition("left",{duration:340,easing:"ease-out"})
          .child(v_flex().relative().flex_none().w(CARD_W+RING*2).h(CARD_H+RING*2).p(RING)
            .when(head&&!alert,el=>el.child(div().absolute().left(0).top(0).right(0).bottom(0).border_2().border_color(t.accent)
              .loop("opacity",{from:0.2,to:1,period:2200})))
            .child(v_flex().flex_none().w(CARD_W).h(CARD_H).px(12).py(8).gap(4).justify_center().border_1().border_color(alert?t.blocked:head?t.accent:t.border).bg(t.surface)
              .cursor_pointer().hover(el=>el.bg(t.raised)).on_click(()=>this.detail(item)).tooltip(item.title+(item.reason?" · "+item.reason:""))
              .accessibility_label((head?"다음 배정 · ":"")+item.team_label+" · "+item.title+" · "+age)
              .child(h_flex().min_w(0).items_center().gap(8).text_size(12)
                .when(head,el=>el.child(div().flex_none().whitespace_nowrap().px(6).bg(t.accent).text_color(t.accent_foreground).font_semibold()
                  .loop("opacity",{from:0.55,to:1,period:2200}).child("다음")))
                .child(div().flex_1().min_w(0).truncate().text_color(t.muted).child(shortTeam(item.team_label)))
                .child(div().flex_none().whitespace_nowrap().text_color(alert?t.blocked:t.muted).child(age)))
              .child(div().min_w(0).truncate().text_size(16).font_semibold().child(item.short_title ?? item.title))))
          .child(div().w(ARROW_W).flex_none().flex().justify_center().text_size(20).text_color(t.dim)
            .loop("opacity",{from:0.25,to:1,period:2400}).child("›")));
    };
    const live=lane.filter(x=>queuedIds.has(x.id));
    const body=!this.data ? words(this.failure ? "최신 조회 확인 필요 · "+this.failure : "업무를 읽고 있습니다").text_size(14).text_color(this.failure?t.blocked:t.muted)
      : !lane.length ? h_flex().flex_1().min_w(0).justify_center().child(h_flex().id("queue-lane-empty").flex_none().items_center().gap(10).h(CARD_H).px(16).border_1().border_color(t.border).bg(t.background).text_size(14).text_color(t.muted)
          .loop("opacity",{from:0.55,to:1,period:3200}).child(div().text_color(t.dim).child("—")).child("대기 중인 업무 없음 · 들어오면 자동 배정됩니다"))
      : // justify_end + overflow_hidden: when full, the newest (left) cards are clipped so the head at the right stays visible.
          h_flex().id("queue-lane-track").flex_1().min_w(0).items_center().justify_end().overflow_hidden().children([...lane].reverse().map(x=>card(x,x.id===live[0]?.id)));
    const end=(id,label,w)=>div().id(id).flex_none().whitespace_nowrap().text_size(14).text_color(t.muted).bg(t.background).px(4).child(label);
    // Rail: a 2px line at the lane's vertical centre behind entry, cards and exit (all opaque), with dots shifted rightwards by native text frames.
    const RAIL_UNIT=16, RAIL_LEN=60;
    const railUnit="•"+" ".repeat(RAIL_UNIT-1);
    const railFrames=Array.from({length:RAIL_UNIT},(_,k)=>(" ".repeat(k)+railUnit.repeat(RAIL_LEN)).slice(0,RAIL_UNIT*RAIL_LEN));
    const fill=()=>div().absolute().left(0).top(0).right(0).bottom(0).flex().items_center().overflow_hidden();
    const rail=fill().id("queue-lane-rail")
      .child(div().flex_1().h(2).bg(t.border))
      .child(fill().child(div().flex_none().whitespace_nowrap().text_size(16).text_color(t.muted).frames(railFrames,220)));
    return base().text_size(14)
      .child(h_flex().flex_none().min_w(0).h(36).items_center().gap(12).px(14)
        .child(div().flex_none().text_size(15).font_semibold().child("업무 큐 · 대기 "+queued.length))
        .child(div().flex_1().min_w(0).truncate().text_size(13).text_color(this.executionFailure?t.blocked:t.muted).child(status))
        .child(div().id("queue-strip-taken").relative().flex_none().whitespace_nowrap().px(8).py(2).text_size(13).text_color(attention?t.blocked:t.muted).cursor_pointer()
          .on_click(()=>openList())
          .child(div().absolute().left(0).top(0).right(0).bottom(0).bg(t.accent).opacity(this.takenFlash?0.35:0).transition("opacity",{duration:420,easing:"ease-out"}))
          .child("처리 중 "+taken.length+(attention?" · 확인 필요 "+attention:"")))
        .when(Boolean(this.options.overlay),el=>el.child(linkBtn("queue-strip-close","닫기",()=>closeOverlay(this.options.overlay))))
        .child(linkBtn("queue-strip-sync",this.executionFlight?"확인 중…":"동기화",cx=>cx.spawn(async cx=>this.executeAction(cx,"sync"))))
        .child(linkBtn("queue-strip-all","전체 ›",()=>openList())))
      .child(h_flex().relative().flex_1().min_h(0).min_w(0).items_center().gap(10).px(14)
        .child(rail)
        .child(end("queue-lane-entry","입구 ›").frames(["입구 ›","입구 ››","입구 ›››"],700))
        .child(body)
        .child(end("queue-lane-exit","배정 ▶")));
  }
  render(cx) {
    const t=theme();
    const base = () => v_flex().size_full().min_w(0).min_h(0).bg(t.background).text_color(t.foreground).font_family("D2Coding").text_size(13);
    const fail = () => words("최신 조회 확인 필요 · " + this.failure).text_size(12).text_color(t.blocked);
    if(this.role === "operations") {
      const data=this.data, capacity=data?.capacity, supervisor=data?.supervisor;
      const healthy=supervisor?.alive && supervisor?.heartbeat_fresh && supervisor?.source_matches && supervisor?.dispatch_healthy;
      const row=(label,value)=>h_flex().flex_wrap().gap(8).child(div().text_color(t.muted).child(label)).child(words(value));
      return base().border_2().border_color(t.dim)
        .child(h_flex().flex_none().p(16).items_center().justify_between().gap(10)
          .child(div().font_semibold().text_size(18).child("운영 상태"))
          .child(button("queue-operations-close","닫기",()=>closeOverlay(this.options.overlay),t)))
        .child(v_flex().id("queue-operations-body").flex_1().min_h(0).overflow_y_scrollbar().p(16).gap(16)
          .when(Boolean(this.failure),el=>el.child(fail()))
          .when(!data,el=>el.child(words("운영 정보를 읽고 있습니다")))
          .when(Boolean(data),el=>el
            .child(row("승인·의존성 통과",String(data.policy_ready_count??"미확인")))
            .child(row("가용 실행 슬롯",capacity?.global_limit===null ? "상한 없음" : String(capacity?.available??"미확인")))
            .child(row("사용 / 상한",String(capacity?.used??"미확인")+" / "+(capacity?.global_limit===null ? "상한 없음" : String(capacity?.global_limit??"미확인"))))
            .child(row("프로필 / 준비 후보",String(data.account_profiles??"미확인")+" / "+String(data.candidate_slots??"미확인")))
            .children(Object.entries(data.waiting??{}).map(([label,count])=>row(label,String(count)))))
          .when(Boolean(data),el=>el
            .child(row("소비기",healthy?"정상":"확인 필요"))
            .child(row("실행 소스",supervisor?.source_fingerprint??"미확인"))
            .child(row("관측 지연",supervisor?.heartbeat_age_seconds==null?"미확인":String(Math.round(supervisor.heartbeat_age_seconds))+"초"))
            .when(Boolean(supervisor?.dispatch?.last_error),x=>x.child(words("배정 소비기 오류 · "+String(supervisor.dispatch.last_error.reason??"관측 필요")).text_color(t.blocked))))
          .children(Object.entries(data?.profiles??{}).map(([key,value])=>{
            const [p,a]=key.split("/");
            return v_flex().gap(5).child(div().font_semibold().child(account(a)+" "+provider(p)))
              .child(words(value.eligible?"사용량 조건 통과":"사용량 확인·회복 대기"))
              .child(words("5h "+periodUsage(value,"five_hour")+" · 주간 "+periodUsage(value,"weekly")))
              .child(words("관측 "+clock(value.observed_at)).text_size(11).text_color(t.muted));
          }))
          .when(Boolean(data),el=>el
            .child(words("장부 밖 HC 실행 "+String(capacity?.outside_ledger??"미확인")+"개를 관측하여 합산합니다. 직접 시작·HC 밖 모델 실행은 통제 범위 밖입니다. 슬롯·프로필·준비 후보는 서로 다른 수치입니다.").text_size(11).text_color(t.muted))
            .child(words("HC 터미널의 foreground 상태 기준입니다. 내부 하위 에이전트·API 모델 호출은 별도 집계하지 못합니다.").text_size(11).text_color(t.muted)))
          .when(Boolean(data?.warnings?.length),el=>el.child(words(data.warnings.map(x=>x.source+": "+x.reason).join("\n")).text_color(t.blocked))));
    }
    if(this.role === "detail") {
      const item=this.selected;
      const shell=base().relative().border_2().border_color(t.dim).children(pixelCorners(t.background)).top(this.revealed ? 0 : 8).opacity(this.revealed ? 1 : 0)
        .transition("top",{duration:220,easing:"ease-out"}).transition("opacity",{duration:180,easing:"ease-out"});
      if(!item) return shell.p(18).gap(10).child(words("요청 정보를 찾을 수 없습니다")).child(button("queue-detail-close","닫기",()=>closeOverlay(this.options.overlay),t));
      const current=terminals().find(x => x.address === item.current_terminal);
      const nativeState=current?.status?.state ?? current?.status ?? null;
      const isWorking=item.phase==="running" && item.verified_working === true && item.receiver_matches_current === true && nativeState==="working";
      const displayState=isWorking ? "진행 중" : item.phase==="running" ? "결과 제출 대기" : item.phase==="received" ? "입력 전달 확인 · 실행 수신 대기" : item.state_label;
      const nativeLabel = typeof nativeState === "string" ? ({working:"응답 중",done:"새 응답 있음",idle:"대기",blocked:"입력 필요"}[nativeState] ?? nativeState) : "앱 상태 미확인";
      const linkedSession=item.current_terminal ? `${account(item.account)} ${provider(item.provider)} · ${nativeLabel}` : "현재 실행자 미배정";
      const pair=(label,value) => v_flex().min_w(0).gap(4).child(div().text_size(11).text_color(t.muted).child(label)).child(words(value).line_height("20px"));
      const section=(label,body) => v_flex().flex_none().min_w(0).gap(8).child(div().text_size(12).font_semibold().text_color(t.muted).child(label)).child(body);
      return shell
        .child(v_flex().flex_none().p(18).gap(10).border_b(1).border_color(t.border)
          .child(h_flex().min_w(0).items_start().gap(12)
            .child(div().id("queue-detail-title").flex_1().min_w(0).truncate().text_size(19).font_semibold().line_height("25px").tooltip(clean(item.title))
              .child(clean(item.short_title ?? item.title).split("\n")[0]))
            .child(button("queue-detail-close","닫기",()=>closeOverlay(this.options.overlay),t)))
          .child(h_flex().flex_wrap().items_center().gap(7).child(badge(item.team_label,t.foreground,t)).child(badge(displayState,categoryColor(item.category,t),t))
            .when(Boolean(manualState(item)),el=>el.child(badge(manualState(item),t.muted,t)))
            .when(item.category==="archived",el=>el.child(badge("보관됨",t.muted,t))))
          .when(Boolean(this.failure),el => el.child(fail()))
          .when(Boolean(item.board_state),el=>el
            .child(div().text_size(11).text_color(t.muted).child("카드 정리 · 실행·승인·검증 상태는 별도로 유지됩니다"))
            .child(h_flex().flex_wrap().gap(6)
              .children((item.category==="archived" ? [["restore","복원"]] : [["restore","자동"],["waiting","대기 표시"],["progress","진행 표시"],["done","완료 표시"],["archive","카드 제거 (보관)"]]).map(([action,label])=>
                button("queue-organize-"+action,this.actionFlight?"저장 중…":label,cx=>cx.spawn(async cx=>this.organize(cx,action)),t))))
            .when(Boolean(this.pendingAction)&&!this.actionFlight,x=>x.child(button("queue-organize-reconcile","변경 결과 재확인",cx=>cx.spawn(async cx=>this.organize(cx,null)),t)))
            .when(Boolean(this.actionFailure),x=>x.child(words(this.actionFailure).text_size(12).text_color(t.blocked)))))
        .child(v_flex().id("queue-detail-body").flex_1().min_h(0).min_w(0).overflow_y_scrollbar().p(18).gap(20)
          .when(Boolean(item.reason),el => el.child(section("진행 상태",words(item.reason).text_color(item.category === "attention" ? t.blocked : t.foreground).line_height("21px"))))
          .child(section("담당·세션",v_flex().gap(9).child(words(item.team_label+" · "+linkedSession))
            .when(Boolean(item.current_terminal),el => el.child(h_flex().flex_wrap().gap(8).items_center()
              .child(div().text_size(11).text_color(t.muted).child("현재 연결: "+item.current_terminal))
              .when(Boolean(current),x => x.child(button("queue-focus-session","담당 대화 열기",()=>{closeOverlay(this.options.overlay);run("agent.focus",{terminal:item.current_terminal});},t)))))
            .when(Boolean(item.receiver_terminal),el => el.child(words("원 요청의 수신 기록: "+item.receiver_terminal+(item.session_changed ? " · 현재 실행자와 다른 세션" : "")).text_size(11).text_color(t.muted)))
            .child(words("연결된 작업: "+(item.task_id ?? "미등록")+" · "+(item.task_state ?? "상태 미확인")).text_size(11).text_color(t.muted))))
          .child(section("담당자에게 전송",v_flex().gap(8)
            .child(button("queue-send-candidates",this.candidateFlight?"담당자 확인 중…":"담당자에게 전송",cx=>cx.spawn(async cx=>this.loadCandidates(cx)),t))
            .when(Boolean(this.candidateFailure),el=>el.child(words(this.candidateFailure).text_color(t.blocked)))
            .when(Boolean(this.candidates),el=>el
              .when(this.candidates?.send_allowed!==true,x=>x.child(words(this.candidates?.reason||"현재 전송 불가 · 승인·수신 상태 확인 필요").text_color(t.blocked)))
              .when(!this.candidates?.candidates.length,x=>x.child(words("선택 가능한 담당자가 없습니다")))
              .children((this.candidates?.candidates??[]).map(candidate=>v_flex().gap(4)
                .child(words((candidate.alias||"별명 미확인")+" ("+({personal:"개인",team:"팀"}[candidate.account]||"계정 미확인")+") · "+(candidate.role||"역할 미확인")))
                .when(candidate.eligible!==true,x=>x.child(words(candidate.reason||"현재 배정 불가").text_color(t.blocked)))
                .when(this.candidates?.send_allowed===true&&candidate.eligible===true&&!this.pendingExecution,x=>x.child(button("queue-send-"+candidate.id,"이 담당자에게 전송",cx=>cx.spawn(async cx=>this.executeAction(cx,"send",candidate)),t))))))
            .child(this.actionStatus(t))))
          .child(section("요청 내용",(()=>{
            const text=item.request || "저장된 요청 본문 없음";
            const lines=clean(text).split("\n");
            const long=lines.length>4 || text.length>240;
            const folded=long && !this.requestOpen;
            return v_flex().gap(8)
              .child(words(folded ? lines.slice(0,4).join("\n") : text).line_height("21px")
                .when(folded,el=>el.max_h(90).overflow_hidden()))
              .when(long,el=>el.child(div().id("queue-request-toggle").flex_none().self_start().whitespace_nowrap().px(10).h(24).flex().items_center().border_1().border_color(t.dim)
                .text_size(12).text_color(t.foreground).cursor_pointer().hover(x=>x.bg(t.raised))
                .on_click((_e,c)=>{c.stop_propagation();this.requestOpen=!this.requestOpen;c.notify();})
                .child(folded ? "전체 보기 ("+lines.length+"줄)" : "접기")));
          })()))
          .child(section("책임·연결",v_flex().gap(5).child(words("책임 조직: "+item.team_label+" · 실행 역할: "+(item.role??"기존 요청"))).child(words("원 지시: "+(item.origin_id??item.id)+" · 부모: "+(item.parent_id??"없음"))).child(words("다음 행동: "+(item.next_action??"담당자 확인")))))
          .when(Boolean(item.handoff),el => el.child(section("인계 상태",words((item.handoff?.phase ?? "미확인")+" · "+(item.handoff?.reason ?? "기록된 이유 없음")).text_size(12))))
          .child(section("기록",v_flex().gap(8).child(pair("등록",clock(item.created_at))).child(pair("최근 변경",clock(item.updated_at)))
            .child(pair("완료 확인",item.work_completion_confirmed ? "업무 완료 확인됨" : "업무 완료를 확인한 기록 없음"))
            .when(Boolean(item.work_title),el=>el.child(pair("연결된 업무",item.work_title+" · "+(item.work_phase ?? "미확인"))))
            .when(Boolean(item.evidence_file),el=>el.child(pair("수신 확인 근거",item.evidence_file)))
            .when(Boolean(item.superseded_by),el=>el.child(pair("이어지는 요청",item.superseded_by)))
            .when(Boolean(item.technical_reason) && item.technical_reason !== item.reason,el=>el.child(pair("원 기록의 대기 이유",item.technical_reason)))
            .when(Boolean(item.technical_next_action) && item.technical_next_action !== item.next_action,el=>el.child(pair("원 기록의 다음 행동",item.technical_next_action)))
            .child(pair("원 요청 기록",item.source_file)).child(pair("요청 ID",item.id)))));
    }
    const data=this.data;
    const nativeTerminals=terminals();
    if(this.role === "strip") return this.renderStrip(cx,t,base,nativeTerminals);
    const jobs=(data?.jobs??[]).filter(item=>this.filter==="all" ||
      (this.filter==="archived" ? item.category==="archived" : this.filter==="records" ? ["history","deferred"].includes(item.category) : isActive(item)));
    const card=item=>v_flex().id("queue-card-"+item.id).min_w(0).flex_none().p(12).gap(8)
      .border_1().border_color(verifiedWorking(item,nativeTerminals)?t.working:t.border).bg(t.surface)
      .cursor_pointer().hover(el=>el.bg(t.raised)).on_click(()=>this.detail(item))
      .accessibility_label(item.team_label+" · "+item.title+" · "+itemState(item,nativeTerminals)).tooltip(item.title)
      .child(h_flex().min_w(0).items_center().justify_between().gap(10)
        .child(div().flex_1().min_w(0).truncate().text_size(11).text_color(t.muted).child(item.team_label))
        .child(badge(itemState(item,nativeTerminals),itemColor(item,nativeTerminals,t),t)))
      .child(div().min_w(0).truncate().text_size(15).font_semibold().child(item.title));
    const healthy=data?.supervisor?.alive && data?.supervisor?.heartbeat_fresh && data?.supervisor?.source_matches && data?.supervisor?.dispatch_healthy;
    return base()
      .child(h_flex().flex_none().flex_wrap().items_center().justify_between().p(14).gap(10)
        .child(div().text_size(18).font_semibold().child("업무 큐"))
        .child(h_flex().flex_wrap().items_center().gap(6)
          .when(Boolean(this.options.overlay),el=>el.child(button("queue-list-close","닫기",()=>closeOverlay(this.options.overlay),t)))
          .children(GROUPS.map(([id,label])=>div().id("queue-filter-"+id).px(10).py(6)
            .bg(this.filter===id?t.raised:t.background).text_color(this.filter===id?t.foreground:t.muted)
            .cursor_pointer().on_click((_event,cx)=>{this.filter=id;cx.notify();}).child(label)))
          .child(button("queue-operations",data&&!healthy?"운영 확인":"운영 정보",()=>this.operations(),t))
          .child(button("queue-sync",this.executionFlight?"동기화 확인 중…":"업무 동기화",cx=>cx.spawn(async cx=>this.executeAction(cx,"sync")),t))
          .child(button("queue-refresh","새로고침",cx=>cx.spawn(async cx=>this.refresh(cx)),t))))
      .child(v_flex().id("queue-content").flex_1().min_h(0).min_w(0).overflow_y_scrollbar().p(14).pt(0).gap(8)
        .child(this.actionStatus(t))
        .when(Boolean(this.failure),el=>el.child(fail()))
        .when(Boolean(data?.warnings?.length),el=>el.child(words("일부 기록 확인 필요 · 운영 정보에서 확인하세요").text_size(11).text_color(t.blocked)))
        .when(!data&&!this.failure,el=>el.child(words("업무를 읽고 있습니다").text_color(t.muted)))
        .when(Boolean(data)&&!jobs.length,el=>el.child(words("해당하는 업무가 없습니다").text_color(t.muted)))
        .children(jobs.map(card)));
  }
}
