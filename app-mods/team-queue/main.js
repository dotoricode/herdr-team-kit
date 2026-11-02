import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { appHome, readTextFile, exec, theme, terminals, run, props as modProps, openOverlay, closeOverlay } from "herdr";

const MOD = "team-queue";
const GROUPS = [
  ["active", "현재 요청"], ["attention", "연결 대기"], ["waiting", "대기"],
  ["progress", "맡은 지시"], ["deferred", "보류"], ["history", "이전 기록"], ["all", "전체"]
];
const provider = x => ({ codex: "Codex", claude: "Claude" })[x] ?? "미확인";
const account = x => ({ personal: "개인", team: "회사" })[x] ?? "계정 미확인";
const clock = x => x == null ? "—" : new Date(x * 1000).toLocaleString();
const clean = x => String(x ?? "").replaceAll("장부", "작업 기록");
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
const pixelActivity=t=>h_flex().gap(2).items_center().children([900,1100,1300].map(period=>
  div().w(4).h(4).bg(t.working).loop("opacity",{from:0.28,to:1,period,easing:"linear"})));
const badge = (label, color, t) => div().flex_none().whitespace_nowrap().rounded(0).border_1().border_color(color).px(6).py(2).bg(t.raised).text_color(color).text_size(11).font_semibold().child(label);
const button = (id, label, callback, t) => div().id(id).relative().flex_none().px(12).py(7).rounded(0).border_2().border_color(t.dim).bg(t.raised).children(pixelCorners(t.background)).text_color(t.foreground).text_size(13).cursor_pointer()
  .hover(el => el.bg(t.surface)).on_click((_event,cx) => { cx.stop_propagation(); callback(cx); }).child(label);

export default class TeamQueue extends View {
  init(props,cx) {
    this.options = props ?? modProps() ?? {};
    this.role = this.options.role ?? "list";
    this.data = null; this.failure = null; this.inFlight = false;
    this.filter = "active"; this.mode = "flow"; this.showResults = false; this.selected = this.options.item ?? null;
    this.revealed = this.role !== "detail";
    cx.spawn(async cx => {
      try {
        const local = this.options.local ?? appHome() + "/mods/" + MOD + "/local.json";
        this.config = JSON.parse(await readTextFile(local) ?? "{}");
        await this.refresh(cx);
      } catch(error) { this.failure = String(error); cx.notify(); }
    });
    cx.timer.every(5000,cx => { if(this.config) cx.spawn(async cx => this.refresh(cx)); });
    if(this.role === "detail") cx.timer.after(32,cx => { this.revealed = true; cx.notify(); });
  }
  async refresh(cx) {
    if(this.inFlight || !this.config) return;
    this.inFlight = true;
    try {
      const c=this.config;
      const result=await exec(c.python,[c.cli,"--config",c.config,"--state",c.state,"queue","status","--board"]);
      if(result.code !== 0) throw new Error(result.stderr || "요청 큐를 읽을 수 없습니다");
      const incoming=JSON.parse(result.stdout);
      if(incoming.schema_version !== 1 || !Array.isArray(incoming.jobs) || !incoming.counts || !incoming.fingerprint) throw new Error("요청 큐 자료 형식을 확인해 주세요");
      const changed=this.failure || this.data?.fingerprint !== incoming.fingerprint;
      this.failure=null; this.data=incoming;
      if(changed) { if(this.role === "detail") this.selected=incoming.jobs.find(x => x.id === this.options.item?.id) ?? this.selected; cx.notify(); }
    } catch(error) {
      const message=String(error);
      if(this.failure !== message) { this.failure=message; cx.notify(); }
    } finally { this.inFlight=false; }
  }
  detail(item) {
    openOverlay({ mod:MOD, props:{ role:"detail", item, local:this.options.local }, kind:"dialog", modal:false, frame:"none", width:0.72, height:0.82 });
  }
  render(cx) {
    const t=theme();
    const base = () => v_flex().size_full().min_w(0).min_h(0).bg(t.background).text_color(t.foreground).font_family("D2Coding").text_size(13);
    const fail = () => words("최신 조회 확인 필요 · " + this.failure).text_size(12).text_color(t.blocked);
    if(this.role === "detail") {
      const item=this.selected;
      const shell=base().relative().border_2().border_color(t.dim).children(pixelCorners(t.background)).top(this.revealed ? 0 : 8).opacity(this.revealed ? 1 : 0)
        .transition("top",{duration:220,easing:"ease-out"}).transition("opacity",{duration:180,easing:"ease-out"});
      if(!item) return shell.p(18).gap(10).child(words("요청 정보를 찾을 수 없습니다")).child(button("queue-detail-close","닫기",()=>closeOverlay(this.options.overlay),t));
      const current=terminals().find(x => x.address === item.current_terminal);
      const nativeState=current?.status?.state ?? current?.status ?? null;
      const nativeLabel = typeof nativeState === "string" ? ({working:"응답 중",done:"새 응답 있음",idle:"대기",blocked:"입력 필요"}[nativeState] ?? nativeState) : "앱 상태 미확인";
      const linkedSession=item.current_terminal ? `${account(item.account)} ${provider(item.provider)} · ${nativeLabel}` : "현재 실행자 미배정";
      const pair=(label,value) => v_flex().min_w(0).gap(4).child(div().text_size(11).text_color(t.muted).child(label)).child(words(value).line_height("20px"));
      const section=(label,body) => v_flex().flex_none().min_w(0).gap(8).child(div().text_size(12).font_semibold().text_color(t.muted).child(label)).child(body);
      return shell
        .child(v_flex().flex_none().p(18).gap(10).border_b(1).border_color(t.border)
          .child(h_flex().min_w(0).items_start().gap(12)
            .child(words(item.title).flex_1().text_size(19).font_semibold().line_height("25px"))
            .child(button("queue-detail-close","닫기",()=>closeOverlay(this.options.overlay),t)))
          .child(h_flex().flex_wrap().items_center().gap(7).child(badge(item.team_label,t.foreground,t)).child(badge(item.state_label,categoryColor(item.category,t),t)))
          .when(Boolean(this.failure),el => el.child(fail())))
        .child(v_flex().id("queue-detail-body").flex_1().min_h(0).min_w(0).overflow_y_scrollbar().p(18).gap(20)
          .when(Boolean(item.reason),el => el.child(section("진행 상태",words(item.reason).text_color(item.category === "attention" ? t.blocked : t.foreground).line_height("21px"))))
          .child(section("담당·세션",v_flex().gap(9).child(words(item.team_label+" · "+linkedSession))
            .when(Boolean(item.current_terminal),el => el.child(h_flex().flex_wrap().gap(8).items_center()
              .child(div().text_size(11).text_color(t.muted).child("현재 연결: "+item.current_terminal))
              .when(Boolean(current),x => x.child(button("queue-focus-session","담당 대화 열기",()=>{closeOverlay(this.options.overlay);run("agent.focus",{terminal:item.current_terminal});},t)))))
            .when(Boolean(item.receiver_terminal),el => el.child(words("원 요청의 수신 기록: "+item.receiver_terminal+(item.session_changed ? " · 현재 실행자와 다른 세션" : "")).text_size(11).text_color(t.muted)))
            .child(words("연결된 작업: "+(item.task_id ?? "미등록")+" · "+(item.task_state ?? "상태 미확인")).text_size(11).text_color(t.muted))))
          .child(section("요청 내용",words(item.request || "저장된 요청 본문 없음").line_height("21px")))
          .child(section("책임·연결",v_flex().gap(5).child(words("책임 조직: "+item.team_label+" · 실행 역할: "+(item.role??"기존 요청"))).child(words("원 지시: "+(item.origin_id??item.id)+" · 부모: "+(item.parent_id??"없음"))).child(words("다음 행동: "+(item.next_action??"담당자 확인")))))
          .when(Boolean(item.handoff),el => el.child(section("인계 상태",words((item.handoff?.phase ?? "미확인")+" · "+(item.handoff?.reason ?? "기록된 이유 없음")).text_size(12))))
          .child(section("기록",v_flex().gap(8).child(pair("등록",clock(item.created_at))).child(pair("최근 변경",clock(item.updated_at)))
            .child(pair("완료 확인",item.work_completion_confirmed ? "업무 완료 확인됨" : "업무 완료를 확인한 기록 없음"))
            .when(Boolean(item.work_title),el=>el.child(pair("연결된 업무",item.work_title+" · "+(item.work_phase ?? "미확인"))))
            .when(Boolean(item.evidence_file),el=>el.child(pair("수신 확인 근거",item.evidence_file)))
            .when(Boolean(item.superseded_by),el=>el.child(pair("이어지는 요청",item.superseded_by)))
            .when(Boolean(item.technical_reason) && item.technical_reason !== item.reason,el=>el.child(pair("원 기록의 대기 이유",item.technical_reason)))
            .child(pair("원 요청 기록",item.source_file)).child(pair("요청 ID",item.id)))));
    }
    const data=this.data;
    const jobs=(data?.jobs ?? []).filter(x => this.filter === "all" || this.filter === "active" ? this.filter === "all" || !["history","deferred"].includes(x.category) : x.category === this.filter);
    const allJobs=data?.jobs ?? [];
    const nativeTerminals=terminals();
    const terminalFor=address=>nativeTerminals.find(x=>x.address===address);
    const nativeState=terminal=>terminal?.status?.state ?? terminal?.status ?? "unknown";
    const verifiedWorking=item=>["received","running"].includes(item.phase) && !item.completion_intent && !item.work_completion_confirmed && item.receiver_matches_current === true && nativeState(terminalFor(item.receiver_terminal))==="working";
    const itemState=item=>item.requires_user_decision ? "답변 대기" : item.work_completion_confirmed ? "업무 완료" : item.completion_intent ? "결과 확인 중" : verifiedWorking(item) ? "진행 중" : item.phase==="received" ? "맡은 지시" : ["uncertain","waiting_handoff"].includes(item.phase) ? "담당 연결 대기" : item.state_label;
    const itemColor=item=>item.requires_user_decision ? t.blocked : verifiedWorking(item) ? t.working : item.phase==="received" ? t.muted : item.category==="attention" ? t.ansi3 : categoryColor(item.category,t);
    const results=allJobs.filter(x=>x.work_completion_confirmed || x.history_kind==="deferred");
    const counts={next:allJobs.filter(x=>["queued","pending"].includes(x.phase)).length,
      assigned:allJobs.filter(x=>x.phase==="received" && !x.work_completion_confirmed).length,
      working:allJobs.filter(verifiedWorking).length,results:results.length};
    const card=(item,compact=false)=>v_flex().id("queue-card-"+item.id).relative().border_2().border_color(verifiedWorking(item)?t.working:t.dim).min_w(0).flex_none().p(compact?10:13).gap(7).rounded(0).bg(t.surface).cursor_pointer().hover(el=>el.bg(t.raised)).on_click(()=>this.detail(item))
      .accessibility_label(item.team_label+" · "+item.title+" · "+itemState(item)).tooltip(item.title).children(pixelCorners(compact?t.chrome:t.background))
      .child(h_flex().min_w(0).items_start().justify_between().gap(10)
        .child(words(item.title).flex_1().font_semibold().text_size(15).line_height("22px"))
        .child(h_flex().gap(6).items_center()
          .when(verifiedWorking(item),el=>el.child(pixelActivity(t)))
          .child(badge(itemState(item),itemColor(item),t))))
      .when(!compact,el=>el.child(h_flex().flex_wrap().gap(8).text_size(11).text_color(t.muted)
        .child(div().whitespace_nowrap().child(item.team_label))
        .child(div().whitespace_nowrap().child(item.current_terminal ? account(item.account)+" "+provider(item.provider) : "실행자 연결 대기"))
        .child(div().whitespace_nowrap().child(clock(item.created_at)))))
      .when(!compact && Boolean(item.reason),el=>el.child(words(item.reason).text_size(12).line_height("19px").text_color(t.muted)));
    const empty=(title,body)=>v_flex().min_w(0).p(10).gap(4).child(div().text_size(12).text_color(t.muted).child(title)).when(Boolean(body),el=>el.child(words(body).text_size(11).text_color(t.dim)));
    const lane=definition=>{
      const laneJobs=jobs.filter(x=>x.team===definition.team);
      const current=laneJobs.filter(x=>!["queued","pending"].includes(x.phase) && !["history","deferred"].includes(x.category));
      const next=laneJobs.filter(x=>["queued","pending"].includes(x.phase)).sort((a,b)=>(a.created_at??0)-(b.created_at??0));
      const native=terminalFor(definition.terminal),state=nativeState(native);
      const nativeLabel=state==="working" ? "담당 대화 응답 중" : state==="blocked" ? "담당 대화 입력 대기" : native ? "다음 지시 대기" : "담당 연결 대기";
      const region=(title,items,waiting)=>v_flex().min_w(0).max_w(title==="다음 지시"?520:560).flex_1().gap(9)
        .child(h_flex().gap(7).items_center().child(div().text_size(11).font_semibold().text_color(t.muted).child(title)).child(div().text_size(11).text_color(t.dim).child(String(items.length))))
        .when(items.length>0,el=>el.child(title==="다음 지시" ? h_flex().min_w(0).w_full().overflow_x_scrollbar().gap(7).children(items.map(item=>card(item,true).w(280))) : v_flex().gap(7).children(items.map(item=>card(item,true).max_w(560)))))
        .when(items.length===0,el=>el.child(empty(waiting,"")))
        .when(items.length>0,el=>el.child(h_flex().w_full().gap(7).pt(3).children(Array.from({length:18},(_,index)=>
          div().h(3).flex_1().bg(t.dim).opacity(0.38)
            .when(items.some(verifiedWorking),pixel=>pixel.loop("opacity",{from:0.25,to:0.75,period:800+index*40,easing:"linear"}))))));
      return h_flex().id("queue-lane-"+definition.team).relative().border_2().border_color(t.border).children(pixelCorners(t.background)).min_w(0).flex_none().p(13).gap(18).rounded(0).bg(t.chrome).items_start()
        .child(v_flex().w(176).min_w(0).flex_none().gap(7).pt(4)
          .child(h_flex().gap(9).items_center()
            .child(svg("mods/"+MOD+"/pixel-queue.svg").w(32).h(32).text_color(t.muted))
            .child(div().whitespace_nowrap().font_semibold().text_size(16).child(definition.label)))
          .child(words(definition.terminal ? account(definition.account)+" "+provider(definition.provider) : "담당 세션 미연결").text_size(11).text_color(t.muted))
          .child(words(nativeLabel).text_size(11).text_color(state==="working"?t.working:t.dim)))
        .child(div().flex_1().min_w(0).overflow_x_scrollbar()
          .child(h_flex().min_w(640).gap(14).items_start()
            .child(region("다음 지시",next,"대기 지시 없음"))
            .child(v_flex().w(32).flex_none().pt(40).child(svg("mods/"+MOD+"/flow-arrow.svg").w(32).h(20).text_color(t.dim)))
            .child(region("지금 맡은 지시",current,"맡은 지시 없음"))));
    };
    const laneDefinitions=data?.lanes ?? [];
    const flow=v_flex().id("queue-team-flow").w_full().min_w(0).flex_none().gap(9).children(laneDefinitions.map(lane));
    const modeButton=(id,label)=>div().id("queue-mode-"+id).relative().px(12).py(7).rounded(0).border_2().border_color(this.mode===id?t.foreground:t.dim).children(pixelCorners(t.background)).bg(this.mode===id?t.raised:t.background).text_color(this.mode===id?t.foreground:t.muted).text_size(12).cursor_pointer().on_click((_event,cx)=>{this.mode=id;cx.notify();}).child(label);
    return base()
      .child(v_flex().relative().flex_none().m(12).p(14).gap(12).border_2().border_color(t.dim).bg(t.chrome).children(pixelCorners(t.background))
        .child(h_flex().justify_between().items_center().gap(10)
          .child(h_flex().flex_wrap().gap(9).items_center().child(svg("mods/"+MOD+"/pixel-queue.svg").w(24).h(24).text_color(t.done)).child(div().text_size(21).font_semibold().child("업무배정실"))
            .when(Boolean(data),el => el.child(badge(`맡은 지시 ${counts.assigned} · 다음 지시 ${counts.next}`,t.muted,t)))
            .when(counts.working>0,el=>el.child(badge(`진행 중 ${counts.working}`,t.working,t))))
          .child(h_flex().gap(6).items_center().child(modeButton("flow","팀별 흐름")).child(modeButton("list","목록")).child(button("queue-refresh","새로고침",cx=>cx.spawn(async cx=>this.refresh(cx)),t))))
        .child(h_flex().flex_wrap().gap(6).children(GROUPS.map(([id,label])=>div().id("queue-filter-"+id).relative().px(10).py(6).rounded(0).border_2().border_color(this.filter===id?t.muted:t.border).children(pixelCorners(t.chrome)).bg(this.filter===id?t.raised:t.background).text_color(this.filter===id?t.foreground:t.muted).text_size(11).cursor_pointer().on_click((_e,cx)=>{this.filter=id;if(["history","deferred"].includes(id))this.mode="list";cx.notify();}).child(label+" "+(data?.counts?.[id] ?? 0))))))
      .when(Boolean(data),el=>el.child(v_flex().flex_none().mx(12).mb(6).gap(7)
        .child(h_flex().flex_wrap().gap(8)
          .child(badge("승인·의존성 통과 "+String(data.policy_ready_count??"미확인"),t.muted,t))
          .child(badge("가용 실행 슬롯 "+String(data.capacity?.available??"미확인"),t.foreground,t))
          .child(badge("프로필 "+String(data.account_profiles)+" · 준비 후보 "+String(data.candidate_slots),t.muted,t)))
        .child(h_flex().flex_wrap().gap(6).children(Object.entries(data.waiting??{}).map(([label,count])=>badge(label+" "+String(count),t.muted,t))))
        .child(words((data.supervisor?.alive && data.supervisor?.heartbeat_fresh && data.supervisor?.source_matches ? "소비기 정상" : "소비기 확인 필요")+
          " · 소스 "+String(data.supervisor?.source_fingerprint??"미확인").slice(0,12)+
          " · 관측 지연 "+String(Math.round(data.supervisor?.heartbeat_age_seconds??0))+"초").text_size(11).text_color(data.supervisor?.alive?t.muted:t.blocked))
        .child(words("HC 밖 모델 실행은 통제 범위 밖입니다. 슬롯·프로필·준비 후보는 서로 다른 수치입니다.").text_size(11).text_color(t.muted))
        .child(h_flex().flex_wrap().gap(8).children(Object.entries(data.profiles??{}).map(([key,value])=>{
          const [p,a]=key.split("/"); return v_flex().gap(3).px(8).py(5).border_1().border_color(t.border)
            .child(div().text_size(11).child(account(a)+" "+provider(p)+" · "+(value.eligible?"배정 가능":"사용량 확인·회복 대기")))
            .child(div().text_size(11).text_color(t.muted).child("5h "+String(value.five_hour?.used??"미제공")+" · 주간 "+String(value.weekly?.used??"미제공")))
            .child(div().text_size(10).text_color(t.dim).child("관측 "+clock(value.observed_at)));
        })))))
      .child(v_flex().id("queue-content").flex_1().min_h(0).min_w(0).overflow_y_scrollbar().p(16).gap(10)
        .when(Boolean(this.failure),el=>el.child(fail()))
        .when(Boolean(data?.warnings?.length),el=>el.child(words("일부 기록을 읽지 못했습니다 · "+data.warnings.map(x=>x.source+": "+x.reason).join(" / ")).text_size(11).text_color(t.muted)))
        .when(!data&&!this.failure,el=>el.child(div().p(12).text_color(t.muted).child("요청 기록을 읽고 있습니다")))
        .when(this.mode==="flow",el=>el.child(flow))
        .when(this.mode==="list"&&!jobs.length&&Boolean(data),el=>el.child(empty("해당하는 지시가 없습니다","다른 상태에서 기록을 확인할 수 있습니다.")))
        .when(this.mode==="list",el=>el.children(jobs.map(item=>card(item))))
        .when(Boolean(data)&&this.mode==="flow",el=>el.child(v_flex().min_w(0).flex_none().mt(4).gap(9)
          .child(h_flex().justify_between().items_center().gap(10)
            .child(div().font_semibold().text_size(12).text_color(t.muted).child(`결과·보류 기록 ${results.length}`))
            .child(button("queue-results-toggle",this.showResults?"접기":"펼치기",cx=>{this.showResults=!this.showResults;cx.notify();},t)))
          .when(this.showResults,el=>el.children(results.length ? results.map(item=>card(item)) : [empty("아직 마감된 결과가 없습니다","")])))));
  }
}
