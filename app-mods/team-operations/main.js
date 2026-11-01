import { View, div } from "gpui-kit";
import { v_flex, h_flex, Editor, EditorState } from "gpui-base";
import { appHome, readTextFile, exec, agents, terminals, theme, settings, run, nextCommand, props as getModProps } from "herdr";

const LOCAL = "/mods/team-operations/local.json";
const DUMMY = "—";
const display = value => value == null ? "미확인" : typeof value === "string" ? value : typeof value === "number" ? String(value) : Array.isArray(value) ? value.map(display).join(" / ") : typeof value === "boolean" ? (value ? "예" : "아니요") : JSON.stringify(value);
const clock = value => {
  if (value == null) return DUMMY;
  const seconds = Number(value);
  return Number.isFinite(seconds) ? new Date(seconds * 1000).toLocaleString() : display(value);
};
function duration(value) {
  if (value == null || !Number.isFinite(Number(value)) || Number(value) < 0) return DUMMY;
  let seconds = Math.floor(Number(value));
  const h = Math.floor(seconds / 3600); seconds %= 3600;
  const m = Math.floor(seconds / 60); seconds %= 60;
  return `${h}시간 ${m}분 ${seconds}초`;
}
function percent(work) {
  if (!work.approved || work.percent == null || !Number.isFinite(work.percent) || !Number.isFinite(work.steps_total) || !Number.isFinite(work.steps_done))
    return `미정${work.reason ? ` · ${work.reason}` : " · 승인된 진행률 없음"}`;
  const p = Math.max(0, Math.min(100, Math.round(work.percent)));
  const bars = Math.round(p / 10);
  return `${"█".repeat(bars)}${" ".repeat(10 - bars)} ${p}% · ${work.steps_done}/${work.steps_total} 단계`;
}
const PHASE_TEXT = { awaiting_approval: "설계 확인 필요", planning: "설계 중", approved: "승인됨", executing: "진행 중", blocked: "대기 중", complete: "완료 보고됨" };
function phaseText(phase) { return PHASE_TEXT[phase] ?? display(phase); }
const STATUS_TEXT = { working: "실행 중", blocked: "사용자 답변 필요", done: "완료 보고됨", idle: "대기 중" };
function statusText(status) { return STATUS_TEXT[status] ?? (status == null ? "미확인" : display(status)); }
function statusColor(status, t) { return status === "working" ? t.working : status === "blocked" ? t.blocked : status === "done" ? t.done : status === "idle" ? t.idle : t.unknown; }

export default class TeamOperations extends View {
  init(props, cx) {
    this.viewProps = props ?? getModProps() ?? {};
    this.data = null; this.error = null; this.busy = false; this.seq = 0;
    this.expanded = new Set(); this.doc = null; this.docBusy = false;
    this.confirm = null; this.actionBusy = false; this.message = null;
    this.config = null; this.configError = null;
    cx.spawn(async cx => {
      try {
        const localPath = typeof this.viewProps.local === "string" && this.viewProps.local ? this.viewProps.local : appHome() + LOCAL;
        const raw = await readTextFile(localPath);
        const c = raw == null ? null : JSON.parse(raw);
        if (!c || !["python", "script", "config", "state", "bindings"].every(k => typeof c[k] === "string" && c[k].length))
          throw new Error("local.json에 python, script, config, state, bindings 경로가 필요합니다.");
        this.config = c;
      } catch (e) { this.configError = String(e); }
      await this.refresh(cx);
    });
    cx.spawn(async cx => {
      for (;;) {
        const command = await nextCommand();
        if (command === "refresh") cx.spawn(async cx => this.refresh(cx));
        else if (this.viewProps.role === "overview") continue;
        else if (command === "expand-all") {
          for (const team of this.data?.teams ?? []) this.expanded.add(`team-${team.id}`);
          for (const work of this.data?.works ?? []) this.expanded.add(`work-${work.id}`);
          cx.notify();
        } else if (command === "read-first") {
          const work = this.data?.works?.[0];
          if (work) cx.spawn(async cx => this.document(work, cx));
        } else if (command === "read-next") {
          const works = this.data?.works ?? [];
          const current = works.findIndex(work => work.id === this.doc?.id);
          const work = works.length ? works[current < 0 || current + 1 >= works.length ? 0 : current + 1] : null;
          if (work) cx.spawn(async cx => this.document(work, cx));
        } else if (command === "close-document") this.closeDocument(cx);
        else if (command === "begin-approve") this.begin("approve", null, cx);
        else if (command === "begin-resume") {
          const task = this.doc?.resumable?.[0];
          if (task) this.begin("resume", task, cx);
        }
        else if (command === "confirm-action") cx.spawn(async cx => this.executeConfirmed(cx));
        else if (command === "cancel-action") { this.confirm = null; cx.notify(); }
      }
    });
    cx.timer.every(5000, cx => cx.spawn(async cx => this.refresh(cx)));
  }

  update(props) { this.viewProps = props ?? getModProps() ?? {}; }

  args(action, fields = []) {
    const c = this.config;
    if (!c) throw new Error(this.configError || "로컬 backend 설정을 읽는 중입니다.");
    return exec(c.python, [c.script, "--config", c.config, "--state", c.state, "--bindings", c.bindings, action, ...fields]);
  }

  async refresh(cx) {
    if (this.busy) return;
    this.busy = true; const seq = ++this.seq;
    try {
      const result = await this.args("view");
      if (result.code !== 0) throw new Error(result.stderr || `현황 조회 명령이 실패했습니다 (종료 코드 ${result.code}).`);
      const next = JSON.parse(result.stdout);
      if (seq === this.seq) {
        this.data = next; this.error = null;
        const latest = (next.works ?? []).find(work => work.id === this.confirm?.id);
        if (this.confirm && (!this.doc || this.doc.id !== this.confirm.id || this.doc.revision !== this.confirm.revision || this.doc.digest !== this.confirm.digest || this.doc.phase !== this.confirm.phase || latest?.revision !== this.confirm.revision || latest?.phase !== this.confirm.phase || (latest?.digest != null && latest.digest !== this.confirm.digest))) this.confirm = null;
      }
    } catch (e) { if (seq === this.seq) this.error = String(e); }
    finally { this.busy = false; cx.notify(); }
  }

  async document(work, cx) {
    if (this.actionBusy || this.docBusy) return;
    this.selectedWorkId = work.id; this.docBusy = true; this.confirm = null; this.message = null; this.doc = null;
    this.docEditor?.release(); this.docEditor = null;
    const id = work.id;
    try {
      const r = await this.args("document", ["--work", String(id)]);
      if (r.code !== 0) throw new Error(r.stderr || `설계 읽기 명령이 실패했습니다 (종료 코드 ${r.code}).`);
      const d = JSON.parse(r.stdout);
      if (d.id !== id) throw new Error("backend가 요청한 작업과 다른 문서를 반환했습니다.");
      this.doc = d;
      const t = theme();
      this.docEditor = EditorState.new({ language: "markdown", readonly: true, value: String(d.body ?? ""), font_family: settings().font_family,
        syntax: { keyword: t.ansi5, string: t.ansi2, comment: t.dim, function: t.ansi4, type: t.ansi3, number: t.ansi1, constant: t.ansi6 } });
    } catch (e) { this.message = String(e); }
    finally { this.docBusy = false; cx.notify(); }
  }

  closeDocument(cx) { this.doc = null; this.docEditor?.release(); this.docEditor = null; this.confirm = null; this.message = null; cx.notify(); }

  async close(_cx) { this.docEditor?.release(); this.docEditor = null; this.confirm = null; }

  begin(kind, value, cx) {
    if (this.actionBusy || this.busy || this.docBusy || this.error || this.configError || !this.data || !this.data.actions_enabled || !this.doc) return;
    this.confirm = { kind, value, id: this.doc.id, revision: this.doc.revision, digest: this.doc.digest, phase: this.doc.phase };
    cx.notify();
  }

  async executeConfirmed(cx) {
    const pending = this.confirm, doc = this.doc;
    if (!pending || !doc || this.actionBusy || this.busy || this.docBusy || this.error || this.configError || !this.data || !this.data.actions_enabled || pending.id !== doc.id || pending.revision !== doc.revision || pending.digest !== doc.digest || pending.phase !== doc.phase) {
      this.confirm = null; cx.notify(); return;
    }
    this.actionBusy = true; this.confirm = null; this.message = null; cx.notify();
    try {
      const args = pending.kind === "approve"
        ? ["--work", String(doc.id), "--revision", String(doc.revision), "--digest", String(doc.digest), "--confirmed"]
        : ["--task", String(pending.value.id), "--owner", String(pending.value.owner), "--attempt", String(pending.value.attempt), "--work", String(doc.id), "--revision", String(doc.revision), "--digest", String(doc.digest), "--confirmed"];
      const r = await this.args(pending.kind, args);
      if (r.code !== 0) throw new Error(r.stderr || `요청한 작업이 실패했습니다 (종료 코드 ${r.code}).`);
      const out = JSON.parse(r.stdout); this.message = String(out.message ?? "처리가 끝났습니다.");
      if (pending.kind === "approve") this.doc = { ...doc, can_approve: false, phase: "approved" };
    } catch (e) { this.message = String(e); }
    finally { this.actionBusy = false; await this.refresh(cx); cx.notify(); }
  }

  render(cx) {
    const t = theme(), data = this.data;
    const button = (id, text, fn, disabled = false) => div().id(id).px(10).py(6).rounded(6).cursor_pointer().bg(disabled ? t.chrome : t.raised).text_color(disabled ? t.dim : t.foreground).on_click((_e, cx) => { if (!disabled) fn(cx); }).child(text);
    const pair = (a, b) => h_flex().gap(6).items_center().child(div().text_color(t.muted).child(a)).child(div().flex_1().min_w(0).child(display(b)));
    const actionsAllowed = Boolean(data?.actions_enabled && !this.busy && !this.actionBusy && !this.docBusy && !this.error && !this.configError);
    const liveAgents = agents(), liveTerminals = terminals(), teams = data?.teams ?? [], works = data?.works ?? [], inventory = data?.inventory ?? [], accounts = data?.accounts ?? [];
    const liveFor = team => {
      const agent = liveAgents.find(a => a.address === team.terminal || a.pane === team.terminal || a.space === team.space);
      const terminal = liveTerminals.find(x => x.address === team.terminal || x.name === team.terminal);
      return { agent, terminal, status: team.app_state ?? agent?.status ?? terminal?.status ?? null, present: Boolean(team.terminal && (agent || terminal)) };
    };
    const appCounts = teams.reduce((n, team) => { const s = liveFor(team).status; if (s === "working") n.working++; if (s === "blocked") n.blocked++; return n; }, { working: 0, blocked: 0 });
    const unknownTeams = teams.filter(team => !STATUS_TEXT[liveFor(team).status]).length;
    const waiting = [...teams.map(x => x.reason), ...works.map(x => x.reason), ...inventory.filter(x => !String(x.status ?? "").startsWith("완료")).map(x => x.condition), ...(data?.holds ?? []).map(x => x.reason)]
      .filter(Boolean).map(display);
    const summary = `실행 ${appCounts.working} · 사용자 답변 필요 ${appCounts.blocked}`;
    const teamCard = team => {
      const live = liveFor(team), status = live.status;
      const activate = cx => run("agent.focus", { terminal: team.terminal });
      return v_flex().id(`rail-${team.id}`).flex_1().min_w(180).h(82).gap(4).p(8).rounded(7).hover(s => s.bg(t.raised))
        .when(live.present, el => el.cursor_pointer().on_click((_e, cx) => activate(cx)))
        .bg(t.surface)
        .child(div().truncate().font_semibold().child(display(team.label)))
        .child(div().truncate().text_size(12).text_color(statusColor(status, t)).child(statusText(status)))
        .child(div().truncate().text_size(11).text_color(t.muted).child(`장부 · ${display(team.ledger_state)}`));
    };
    const overview = (getModProps()?.role ?? this.viewProps.role) === "overview";
    if (overview) return v_flex().id("team-operations-overview").w_full().h(146).flex_none().gap(4).px(8).py(5).bg(t.background).text_color(t.foreground)
      .font_family(this.config?.overview_font || settings().font_family || "monospace").text_size(13)
      .child(h_flex().h(34).flex_none().items_center().gap(10)
        .child(div().font_semibold().child("팀 운영"))
        .child(div().flex_1().text_size(12).text_color(t.muted).child(summary))
        .child(button("open-coordinator", "총괄", () => run("agent.focus", { terminal: this.config?.coordinator_terminal }), !this.config?.coordinator_terminal).text_size(12))
        .child(button("open-dashboard", "전체 현황", () => run("tab.select", { id: this.config?.dashboard_tab }), !this.config?.dashboard_tab).text_size(12)))
      .child(h_flex().id("team-operations-rail").w_full().h(98).min_w(0).gap(6).overflow_x_scrollbar().children(teams.map(teamCard)));

    const teamCards = teams.map(team => {
      const live = liveFor(team), status = live.status;
      return h_flex().h(62).flex_1().min_w(0).gap(8).p(8).rounded(7).bg(t.surface)
        .when(live.present, el => el.cursor_pointer().hover(s => s.bg(t.raised)).on_click((_e, cx) => run("agent.focus", { terminal: team.terminal })))
        .child(v_flex().flex_1().min_w(0).gap(3).child(div().truncate().font_semibold().child(display(team.label)))
          .child(div().truncate().text_size(11).text_color(statusColor(status, t)).child(`앱 · ${statusText(status)}`))
          .child(div().truncate().text_size(11).text_color(t.muted).child(`장부 · ${display(team.ledger_state)}`)));
    });
    const workRows = works.map(work => {
      const id = `work-${work.id}`, open = this.expanded.has(id);
      return v_flex().gap(4).p(8).rounded(7).bg(t.surface)
        .child(h_flex().justify_between().gap(8).child(div().flex_1().min_w(0).font_semibold().child(`${display(work.id)} · ${display(work.title)}`)).child(div().text_color(t.dim).child(phaseText(work.phase))))
        .child(div().text_size(12).text_color(t.accent).child(percent(work)))
        .child(h_flex().gap(6).children([
          button(`read-${work.id}`, this.doc?.id === work.id ? "설계 표시 중" : "설계 읽기", cx => cx.spawn(async cx => this.document(work, cx)), this.actionBusy || this.docBusy),
          button(`expand-${work.id}`, open ? "세부 접기" : "세부 보기", cx => { open ? this.expanded.delete(id) : this.expanded.add(id); cx.notify(); })
        ]))
        .when(open, el => el.child(v_flex().gap(4).pt(5).child(pair("남은 단계", Array.isArray(work.remaining_steps) ? work.remaining_steps.map(display).join(" / ") : work.remaining_steps)).child(pair("다음", work.next)).child(pair("실행 상태", work.execution_state)).child(pair("앱 상태", work.native_state)).child(pair("수정본", work.revision)).child(pair("갱신", clock(work.updated_at))).when(Boolean(work.reason), x => x.child(pair("대기 이유", work.reason)))));
    });
    const inventoryRows = inventory.map(item => {
      const status = display(item.status), complete = typeof item.status === "string" && item.status.startsWith("완료");
      return v_flex().gap(2).p(7).rounded(7).bg(t.surface)
        .child(h_flex().gap(8).items_center().child(div().w(52).flex_none().font_semibold().child(display(item.id))).child(div().flex_1().min_w(0).truncate().child(display(item.name))).child(div().w(100).flex_none().text_color(t.muted).child(status)))
        .when(complete, el => el.child(h_flex().gap(8).items_center().text_size(11).child(div().text_color(t.accent).child("|██████████| 100% 1/1")).child(div().text_color(t.dim).child("완료 조건 확인 · 전체 진척률 아님"))))
        .when(!complete, el => el.child(div().text_size(11).text_color(t.dim).child(`대기 이유 · ${display(item.condition)}`)));
    });
    const remaining = inventory.filter(item => !(typeof item.status === "string" && item.status.startsWith("완료"))).length;
    const accountRows = accounts.map(account => h_flex().h(28).flex_none().gap(8).items_center().px(8).rounded(6).bg(t.surface).text_size(12)
      .child(div().w(100).flex_none().truncate().font_semibold().child(display(account.label)))
      .child(div().w(64).flex_none().truncate().child(account.five_hour == null ? "미확인" : `${display(account.five_hour)}%`))
      .child(div().w(64).flex_none().truncate().child(account.weekly == null ? "미확인" : `${display(account.weekly)}%`))
      .child(div().w(80).flex_none().truncate().child(statusText(account.status)))
      .child(div().flex_1().min_w(0).truncate().text_color(t.dim).child(duration(account.age))));
    const doc = this.doc;
    const confirmText = this.confirm?.kind === "approve" ? `설계 v${this.confirm.revision}의 범위와 검증 기준을 확인했고 이 버전의 진행을 허락합니다` : `설계 v${this.confirm?.revision}의 범위와 검증 기준을 확인했고 선택한 작업의 재개를 허락합니다`;
    const actionRows = (data?.actions ?? []).map(action => {
      const failed = /실패|오류|fail|error/i.test(`${action.status ?? ""} ${action.message ?? ""}`);
      return h_flex().gap(8).p(6).rounded(6).bg(t.surface).text_size(11)
        .child(div().w(72).flex_none().font_semibold().child(display(action.id)))
        .child(div().w(90).flex_none().text_color(failed ? t.blocked : t.muted).child(display(action.status)))
        .child(div().flex_1().min_w(0).child(display(action.message)));
    });
    const workPanel = v_flex().id("work-inventory").w(370).h_full().flex_none().min_h(0).gap(6).overflow_y_scrollbar()
      .child(div().flex_none().font_semibold().text_color(t.accent).child("제품 작업"))
      .when(workRows.length > 0, x => x.children(workRows))
      .when(workRows.length === 0, x => x.child(div().p(8).text_size(12).text_color(t.muted).child("등록된 GUI 제품 작업 없음 · TUI 업무는 자동으로 가져오지 않음")))
      .child(div().pt(4).flex_none().font_semibold().text_color(t.accent).child(`전환 목록 · 전체 진행률 미정 · 남은 작업 ${remaining}건`))
      .children(inventoryRows)
      .children((data?.holds ?? []).map(hold => v_flex().p(7).rounded(7).bg(t.surface).child(pair("보류", `${display(hold.id)} · ${display(hold.reason)}`))));
    const designPane = v_flex().id("design-pane").h_full().flex_1().min_w(0).min_h(0).gap(6).p(8).rounded(8).border_1().border_color(t.border).bg(t.surface)
      .child(h_flex().flex_none().items_center().justify_between().gap(8)
        .child(div().flex_1().min_w(0).truncate().font_semibold().child(doc ? `${display(doc.title)} · v${display(doc.revision)}` : "설계 문서"))
        .when(Boolean(doc), x => x.child(button("close-doc", "닫기", cx => this.closeDocument(cx)))))
      .when(!doc && !this.docBusy, x => x.child(div().p(14).text_color(t.muted).child("작업에서 설계 읽기를 누르면 여기에 표시됩니다.")))
      .when(this.docBusy, x => x.child(div().p(14).text_color(t.dim).child("설계를 읽는 중입니다.")))
      .when(Boolean(this.message), x => x.child(div().flex_none().text_color(/실패|오류|fail|error/i.test(this.message) ? t.blocked : t.muted).child(this.message)))
      .when(this.docEditor != null, x => x.child(Editor.new(this.docEditor).flex_1().min_h(0)))
      .when(Boolean(doc) && !this.confirm && actionsAllowed, x => x.child(v_flex().flex_none().gap(6)
        .child(button("approve", "이 설계 승인", cx => this.begin("approve", null, cx), !doc.can_approve))
        .children((doc.resumable ?? []).map(task => button(`resume-${task.id}`, `재개 · ${task.id} (${task.owner})`, cx => this.begin("resume", task, cx))))))
      .when(Boolean(this.confirm), x => x.child(v_flex().flex_none().gap(6).p(8).rounded(6).bg(t.chrome)
        .child(div().child(confirmText))
        .child(h_flex().gap(7).children([
          button("confirm-action", this.actionBusy ? "처리 중" : "실행", cx => cx.spawn(async cx => this.executeConfirmed(cx)), !actionsAllowed),
          button("cancel-action", "취소", cx => { this.confirm = null; cx.notify(); }, this.actionBusy)
        ]))));
    const mainPanels = h_flex().id("operations-main").w_full().flex_1().min_h(0).items_stretch().gap(8)
      .child(workPanel).child(designPane);
    const accountArea = v_flex().id("account-area").w_full().flex_none().gap(3)
      .child(div().font_semibold().text_size(12).text_color(t.accent).child("계정 · 보조 정보"))
      .when(accountRows.length > 0, x => x.child(h_flex().w_full().h(22).flex_none().gap(8).items_center().px(8).text_size(10).text_color(t.muted)
        .child(div().w(100).flex_none().child("계정")).child(div().w(64).flex_none().child("5시간"))
        .child(div().w(64).flex_none().child("주간")).child(div().w(80).flex_none().child("상태"))
        .child(div().flex_1().min_w(0).child("경과 시간"))))
      .children(accountRows);
    return v_flex().id("team-operations").size_full().min_h(0).overflow_hidden().bg(t.background).text_color(t.foreground).text_size(13)
      .child(h_flex().h(48).flex_none().px(14).gap(8).items_center().border_b(1).border_color(t.border)
        .child(div().font_semibold().text_size(15).child("GUI 운영 현황")).child(div().flex_1())
        .child(div().text_size(11).text_color(t.dim).child(`마지막 관찰: ${clock(data?.observed_at)}`))
        .child(button("refresh", this.busy ? "불러오는 중" : "새로고침", cx => cx.spawn(async cx => this.refresh(cx)), this.busy)))
      .when(Boolean(this.error), el => el.child(div().px(12).py(7).text_color(t.blocked).child(`갱신에 실패했습니다. 기존 자료는 이전 관찰값입니다: ${this.error}`)))
      .when(Boolean(this.configError), el => el.child(div().px(12).py(7).text_color(t.blocked).child(`로컬 backend 설정을 읽을 수 없습니다: ${this.configError}`)))
      .when(this.busy && !data, el => el.child(div().p(14).text_color(t.dim).child("운영 현황을 읽는 중입니다.")))
      .child(v_flex().id("operations-content").w_full().flex_1().min_h(0).gap(6).p(8)
        .child(h_flex().flex_none().justify_between().items_center().gap(8)
          .child(div().text_size(12).text_color(t.muted).child("GUI 전용 장부 · TUI 업무를 가져오지 않음"))
          .child(div().text_size(12).font_semibold().child(summary)))
        .child(h_flex().flex_none().gap(8).when(teamCards.length > 0, el => el.child(teamCards[0])).when(teamCards.length > 1, el => el.child(teamCards[1])))
        .child(h_flex().flex_none().gap(8).when(teamCards.length > 2, el => el.child(teamCards[2])).when(teamCards.length > 3, el => el.child(teamCards[3])))
        .when(unknownTeams > 0 || waiting.length > 0, el => el.child(v_flex().flex_none().gap(2).text_size(11)
          .when(unknownTeams > 0, x => x.child(div().text_color(t.unknown).child(`미확인 팀 상태 ${unknownTeams}개`)))
          .when(waiting.length > 0, x => x.child(div().text_color(t.muted).child(`대기 이유 · ${waiting.join(" · ")}`)))))
        .when(actionRows.length > 0, el => el.child(v_flex().flex_none().gap(3).children(actionRows)))
        .child(mainPanels)
        .child(accountArea));
  }
}
