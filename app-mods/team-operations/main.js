import { View, div } from "gpui-kit";
import { v_flex, h_flex, Editor, EditorState } from "gpui-base";
import { appHome, readTextFile, exec, agents, terminals, theme, settings, run, nextCommand, props as getModProps, openOverlay, closeOverlay } from "herdr";

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
  return `[${"█".repeat(bars)}${" ".repeat(10 - bars)}] ${p}% · ${work.steps_done}/${work.steps_total} 단계`;
}
const WORK_PHASES = {
  planning: { text: "▶ 설계 중", column: "progress" },
  awaiting_approval: { text: "🟨 설계 확인 필요", column: "review" },
  approved: { text: "✓ 승인됨", column: "waiting" },
  in_progress: { text: "▶ 진행 중", column: "progress" },
  awaiting_review: { text: "검토 대기", column: "review" },
  blocked: { text: "⚠ 대기 중", column: "review" },
  inspect: { text: "실행 결과 확인 필요", column: "review" },
  quota: { text: "사용량 확인 필요", column: "review" },
  environment: { text: "실행 준비 확인 필요", column: "review" },
  complete: { text: "✓ 완료 보고됨", column: "complete" }
};
function phaseText(phase) { return WORK_PHASES[phase]?.text ?? display(phase); }
const TASK_STATES = { queued: "배정 대기", claimed: "담당자 지정", launching: "시작 준비", submitted: "수신 확인 중", running: "요청 수신됨", ready: "대기", uncertain: "결과 확인 필요", handed_off: "인계됨", awaiting_review: "검토 대기", done: "완료" };
function taskStateText(state) { return TASK_STATES[state] ?? display(state); }
const OVERVIEW = { height: 166, rail: 98, card: 94 };
const STATUS_TEXT = { unknown: "미확인", working: "실행 중", running: "실행 중", blocked: "사용자 답변 필요", done: "응답 완료", idle: "대기 중", pending: "대기 중", failed: "실패", error: "오류", success: "성공", healthy: "정상", not_measured: "측정 전", not_available: "미제공", unavailable: "미제공", missing: "미확인", current: "최신 기록", stale: "지난 기록" };
function statusText(status) { return STATUS_TEXT[status] ?? (status == null ? "미확인" : display(status)); }
function statusColor(status, t) { return status === "working" ? t.working : status === "blocked" ? t.blocked : status === "done" ? t.done : status === "idle" ? t.idle : t.unknown; }

export default class TeamOperations extends View {
  init(props, cx) {
    this.viewProps = props ?? getModProps() ?? {};
    this.role = this.viewProps.role ?? "full";
    this.data = null; this.error = null; this.busy = false; this.seq = 0;
    this.expanded = new Set(); this.doc = null; this.docBusy = false;
    this.confirm = null; this.actionBusy = false; this.message = null;
    this.showCompleted = false; this.coordinatorConfirm = false; this.coordinatorBusy = false; this.coordinatorMessage = null;
    this.usageBusy = false; this.usageMessage = null;
    this.detailData = null; this.detailError = null; this.detailBusy = false;
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
      if (this.role === "detail") {
        if (this.viewProps.kind === "work" || this.viewProps.linkedWorkId) await this.loadDetail(cx);
        if (this.viewProps.readDocument && this.viewProps.kind === "work" && this.detailData) await this.document(this.detailData, cx);
      }
    });
    if (this.role !== "detail") cx.spawn(async cx => {
      for (;;) {
        const command = await nextCommand();
        if (command === "refresh") cx.spawn(async cx => this.refresh(cx));
        else if (this.viewProps.role === "overview") continue;
        else if (command === "expand-all") {
          this.showCompleted = true;
          cx.notify();
        } else if (command === "read-first") {
          const work = this.data?.works?.[0];
          if (work) this.openDetail("work", work, true);
        } else if (command === "read-next") {
          const works = this.data?.works ?? [];
          const current = works.findIndex(work => String(work.id) === String(this.selectedWorkId));
          const work = works.length ? works[current < 0 || current + 1 >= works.length ? 0 : current + 1] : null;
          if (work) this.openDetail("work", work, true);
        } else if (command === "close-document") {
          if (this.detailOverlay) closeOverlay(this.detailOverlay);
          else this.closeDocument(cx);
        }
        else if (command === "begin-approve") this.begin("approve", null, cx);
        else if (command === "begin-resume") {
          const task = this.doc?.resumable?.[0];
          if (task) this.begin("resume", task, cx);
        }
        else if (command === "confirm-action") cx.spawn(async cx => this.executeConfirmed(cx));
        else if (command === "cancel-action") { this.confirm = null; cx.notify(); }
      }
    });
    cx.timer.every(5000, cx => cx.spawn(async cx => {
      await this.refresh(cx);
      if (this.role === "detail" && (this.viewProps.kind === "work" || this.viewProps.linkedWorkId)) await this.loadDetail(cx);
    }));
  }

  update(props) { this.viewProps = props ?? getModProps() ?? {}; }

  async loadDetail(cx) {
    if (this.detailBusy || !this.viewProps.workId) return;
    this.detailBusy = true; this.detailError = null; cx.notify();
    try {
      const workId = this.viewProps.linkedWorkId ?? this.viewProps.workId;
      const r = await this.args("work-detail", ["--work", String(workId)]);
      if (r.code !== 0) throw new Error(r.stderr || `작업 정보를 읽지 못했습니다 (종료 코드 ${r.code}).`);
      const detail = JSON.parse(r.stdout);
      if (String(detail.id) !== String(workId)) throw new Error("요청한 작업과 다른 정보를 받았습니다.");
      this.detailData = detail;
    } catch (e) { this.detailError = String(e); }
    finally { this.detailBusy = false; cx.notify(); }
  }

  openDetail(kind, item, readDocument = false) {
    this.selectedWorkId = item.id;
    this.detailOverlay = openOverlay({ mod: "team-operations", props: { role: "detail", kind, workId: item.id, linkedWorkId: item.work_id, item, local: this.viewProps.local, readDocument }, kind: "dialog", modal: true, width: kind === "inventory" && !item.work_id ? 0.7 : 0.82, height: kind === "inventory" && !item.work_id ? 0.38 : 0.84 });
  }

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

  async recoverCoordinator(cx) {
    if (this.coordinatorBusy || this.data?.coordinator?.state !== "missing" || !this.data?.coordinator?.can_recover) return;
    this.coordinatorBusy = true; this.coordinatorConfirm = false; this.coordinatorMessage = null; cx.notify();
    let accepted = false;
    try {
      const result = await this.args("recover-coordinator", ["--confirmed"]);
      if (result.code !== 0) throw new Error(result.stderr || `총괄 복구 요청이 실패했습니다 (종료 코드 ${result.code}).`);
      const response = JSON.parse(result.stdout);
      accepted = true;
      if (this.data) this.data = { ...this.data, coordinator: { ...this.data.coordinator, state: "recovering", terminal: null } };
      this.coordinatorMessage = String(response.message ?? "총괄 복구 요청을 마쳤습니다.");
    } catch (e) { this.coordinatorMessage = `⚠ ${String(e)}`; }
    finally { await this.refresh(cx); this.coordinatorBusy = false; if (accepted && this.data?.coordinator?.state === "missing") this.data = { ...this.data, coordinator: { ...this.data.coordinator, state: "recovering", terminal: null } }; cx.notify(); }
  }

  async refreshUsage(cx) {
    if (this.usageBusy) return;
    this.usageBusy = true; this.usageMessage = null; cx.notify();
    try {
      const result = await this.args("refresh-usage", []);
      if (result.code !== 0) throw new Error(result.stderr || `사용량 갱신 요청이 실패했습니다 (종료 코드 ${result.code}).`);
      const response = JSON.parse(result.stdout);
      this.usageMessage = String(response.message ?? "사용량 갱신 요청을 마쳤습니다.");
    } catch (e) { this.usageMessage = `⚠ ${String(e)}`; }
    finally { await this.refresh(cx); this.usageBusy = false; cx.notify(); }
  }

  async close(_cx) { this.docEditor?.release(); this.docEditor = null; this.confirm = null; }

  begin(kind, value, cx) {
    if (this.actionBusy || this.busy || this.docBusy || this.error || this.configError || !this.data || !this.data.actions_enabled || !this.doc) return;
    const latest = (this.data.works ?? []).find(work => String(work.id) === String(this.doc.id));
    if (!latest || latest.revision !== this.doc.revision || latest.phase !== this.doc.phase || (latest.digest != null && latest.digest !== this.doc.digest)) {
      this.confirm = null; this.message = "작업 설계가 바뀌었습니다. 최신 문서를 다시 읽어 주세요."; cx.notify(); return;
    }
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
    const words = value => v_flex().min_w(0).gap(2).children(display(value).replaceAll("장부", "작업 기록").split("\n").map(line =>
      h_flex().min_w(0).flex_wrap().items_start().gap_x(4).gap_y(2).children(line.split(/\s+/).filter(Boolean).map(word =>
        div().max_w_full().whitespace_nowrap().truncate().child(word)))));
    const pair = (a, b, scroll = false) => h_flex().gap(6).items_start()
      .child(div().flex_none().whitespace_nowrap().text_color(t.muted).child(a))
      .child(scroll ? div().flex_1().min_w(0).overflow_x_scrollbar().whitespace_nowrap().child(display(b)) : words(b).flex_1().min_w(0));
    const liveAgents = agents(), liveTerminals = terminals(), teams = data?.teams ?? [], works = data?.works ?? [], inventory = data?.inventory ?? [], accounts = data?.accounts ?? [];
    const liveFor = team => {
      const agent = liveAgents.find(a => a.address === team.terminal || a.pane === team.terminal);
      const terminal = liveTerminals.find(x => x.address === team.terminal);
      // Core done is a turn result; agents().status also tracks whether this screen saw it.
      const raw = team.app_state ?? terminal?.status ?? null;
      const status = agent?.status ?? (raw === "done" ? "unknown" : raw);
      return { agent, terminal, status, present: Boolean(team.terminal && (agent || terminal)) };
    };
    const appCounts = teams.reduce((n, team) => { const s = liveFor(team).status; if (s === "working") n.working++; if (s === "blocked") n.blocked++; return n; }, { working: 0, blocked: 0 });
    const unknownTeams = teams.filter(team => { const status = liveFor(team).status; return status === "unknown" || status === "missing" || !STATUS_TEXT[status]; }).length;
    const waiting = [...new Set([...teams.map(x => x.reason), ...works.map(x => x.reason)]
      .filter(Boolean).map(display))];
    const summary = `실행 ${appCounts.working} · 사용자 답변 필요 ${appCounts.blocked}`;
    const coordinator = data?.coordinator;
    const teamCard = team => {
      const live = liveFor(team), status = live.status;
      const activate = cx => run("agent.focus", { terminal: team.terminal });
      return v_flex().id(`rail-${team.id}`).flex_1().min_w(210).h(OVERVIEW.card).gap(3).p(8).rounded(7).hover(s => s.bg(t.raised))
        .when(live.present, el => el.cursor_pointer().on_click((_e, cx) => activate(cx)))
        .bg(t.surface)
        .child(h_flex().h(28).flex_none().gap(6).items_center()
          .child(div().w(10).line_height("28px").flex_none().text_color(statusColor(status, t)).when(status === "working" && live.present && !this.error && !data?.native_error, el => el.frames(["●", "◉", "●", "◉"], 180)).child(status === "working" ? "●" : "·"))
          .child(div().h(28).line_height("28px").truncate().font_semibold().text_size(19).child(display(team.label))))
        .child(div().h(20).flex_none().line_height("20px").truncate().text_size(12).text_color(statusColor(status, t)).child(status === "done" ? "새 응답 · 확인 전" : statusText(status)))
        .child(div().h(20).flex_none().line_height("20px").truncate().text_size(12).text_color(t.muted).child(`작업 상태 · ${taskStateText(team.ledger_state)}`));
    };
    const coordinatorStateText = { live: "연결됨", missing: "총괄 종료", recovering: "복구 중", unknown: "상태 미확인" }[coordinator?.state] ?? "상태 미확인";
    const coordinatorButton = coordinator?.state === "live" && coordinator.terminal
      ? button("open-coordinator", display(coordinator.label ?? "총괄"), () => run("agent.focus", { terminal: coordinator.terminal })).text_size(12)
      : coordinator?.state === "missing" && coordinator.can_recover
        ? button("open-coordinator-recovery", this.coordinatorBusy ? "총괄 복구 중" : "총괄 복구", cx => { if (!this.coordinatorBusy) { this.coordinatorConfirm = true; this.coordinatorMessage = null; cx.notify(); } }, this.coordinatorBusy).text_size(12)
        : div().text_size(12).text_color(coordinator?.state === "recovering" ? t.working : t.muted).child(`총괄 · ${display(coordinator?.reason ?? coordinatorStateText)}`);
    const coordinatorConfirm = v_flex().h(OVERVIEW.card).flex_none().justify_center().gap(6).px(10).rounded(7).bg(t.surface)
      .child(div().text_size(12).child("닫힌 총괄을 작업 기록에서 다시 엽니다. 제품 업무 자동 재개 없음"))
      .child(h_flex().gap(6).children([
        button("confirm-coordinator-recovery", this.coordinatorBusy ? "복구 중" : "복구", cx => cx.spawn(async cx => this.recoverCoordinator(cx)), this.coordinatorBusy || !coordinator?.can_recover).text_size(12),
        button("cancel-coordinator-recovery", "취소", cx => { this.coordinatorConfirm = false; cx.notify(); }, this.coordinatorBusy).text_size(12)
      ]));
    const overview = (getModProps()?.role ?? this.viewProps.role) === "overview";
    if (overview) return v_flex().id("team-operations-overview").w_full().h(OVERVIEW.height).flex_none().gap(4).px(8).py(5).bg(t.background).text_color(t.foreground)
      .font_family(this.config?.overview_font || settings().font_family || "monospace").text_size(13)
      .child(h_flex().h(32).flex_none().items_center().gap(10)
        .child(div().font_semibold().text_size(16).child("팀 운영"))
        .child(div().flex_1().text_size(12).text_color(t.muted).child(summary))
        .child(coordinatorButton)
        .child(button("open-dashboard", "전체 현황", () => run("tab.select", { id: this.config?.dashboard_tab }), !this.config?.dashboard_tab).text_size(12)))
      .when(Boolean(this.coordinatorMessage), el => el.child(div().h(16).flex_none().line_height("16px").text_size(10).text_color(/⚠|실패|오류/i.test(this.coordinatorMessage) ? t.blocked : t.muted).truncate().child(this.coordinatorMessage)))
      .when(this.coordinatorConfirm, el => el.child(coordinatorConfirm))
      .when(!this.coordinatorConfirm, el => el.child(h_flex().id("team-operations-rail").w_full().h(OVERVIEW.rail).flex_none().min_w(0).gap(6).overflow_x_scrollbar().children(teams.map(teamCard))));
    const overlay = this.viewProps.overlay ?? getModProps()?.overlay;
    if (this.role === "detail") {
      const detail = this.detailData;
      const detailWork = detail ?? this.viewProps.item ?? { id: this.viewProps.workId };
      const tasks = detail?.tasks ?? [];
      const leader = detail?.leader;
      const doc = this.doc;
      const detailSessions = tasks.map(task => {
        const verified = task.session_present === true && task.identity_verified === true;
        const canFocus = verified && !this.detailError && !this.error && Boolean(task.can_focus && task.terminal && task.current_owner);
        const sessionStatus = task.session_present == null ? "실행 상태 확인 불가 · 종료 여부 미확인"
          : task.session_present === false ? "현재 연결된 세션 없음"
            : verified ? "실행 신원 확인됨" : "세션은 있으나 실행 신원 미확인";
        return v_flex().gap(3).p(8).rounded(7).bg(t.surface)
          .child(h_flex().justify_between().items_center().gap(8)
            .child(div().flex_1().min_w(0).font_semibold().child(`${display(task.id)} · ${display(task.session_name ?? task.role ?? task.owner)}`))
            .when(canFocus, x => x.child(button(`focus-task-${task.id}`, "세션 열기", () => { run("agent.focus", { terminal: task.terminal }); closeOverlay(overlay); }))))
          .when(Boolean(task.goal), x => x.child(pair("목적", task.goal)))
          .child(pair("앱 상태", statusText(task.native_state)))
          .child(pair("작업 상태", taskStateText(task.state)))
          .child(pair("세션", task.session_present === true ? `${display(task.session_location)} · ${sessionStatus}` : sessionStatus))
          .when(Boolean(task.provider || task.account || task.model_requested || task.effort_requested), x => x.child(pair("요청 환경", [task.provider, task.account, task.model_requested, task.effort_requested].filter(Boolean).map(display).join(" · "))))
          .when(Boolean(task.root), x => x.child(pair("작업 위치", task.root, true)))
          .when(!canFocus, x => x.child(div().text_size(11).text_color(t.dim).child(sessionStatus)));
      });
      const selectedActionAllowed = Boolean(this.data?.actions_enabled && !this.busy && !this.actionBusy && !this.docBusy && !this.error && !this.configError);
      const actionText = this.confirm?.kind === "approve" ? `설계 v${this.confirm.revision}의 범위와 검증 기준을 확인했고 이 버전의 진행을 허락합니다` : `설계 v${this.confirm?.revision}의 범위와 검증 기준을 확인했고 선택한 작업의 재개를 허락합니다`;
      const confirmButtons = h_flex().gap(7).children([
        button("detail-confirm-action", this.actionBusy ? "처리 중" : "실행", cx => cx.spawn(async cx => this.executeConfirmed(cx)), !selectedActionAllowed),
        button("detail-cancel-action", "취소", cx => { this.confirm = null; cx.notify(); }, this.actionBusy)
      ]);
      const confirmBox = v_flex().flex_none().gap(6).p(8).rounded(6).bg(t.chrome)
        .child(div().text_size(12).child(actionText))
        .child(confirmButtons);
      const approvalButtons = h_flex().flex_none().gap(6)
        .when(Boolean(doc?.can_approve), el => el.child(button("detail-approve", "이 설계 승인", cx => this.begin("approve", null, cx), !doc.can_approve)))
        .children((doc?.resumable ?? []).map(task => button(`detail-resume-${task.id}`, `재개 · ${task.id}`, cx => this.begin("resume", task, cx))));
      const documentSection = v_flex().min_h(0).gap(6).pt(4)
        .when(this.docBusy || this.docEditor != null, x => x.flex_1())
        .when(!this.docBusy && this.docEditor == null, x => x.flex_none())
        .child(h_flex().flex_none().items_center().justify_between().gap(8)
          .child(div().whitespace_nowrap().font_semibold().text_color(t.accent).child(doc ? `${display(doc.title)} · v${display(doc.revision)}` : "설계 문서"))
          .child(h_flex().gap(6).children([
            button("detail-read-document", this.docBusy ? "읽는 중" : doc ? "설계 다시 읽기" : "설계 읽기", cx => cx.spawn(async cx => this.document(detailWork, cx)), this.docBusy || this.actionBusy),
            ...(doc ? [button("detail-close-document", "문서 닫기", cx => this.closeDocument(cx))] : [])
          ])))
        .when(Boolean(this.message), x => x.child(div().flex_none().text_color(/실패|오류|fail|error/i.test(this.message) ? t.blocked : t.muted).child(this.message)))
        .when(this.docBusy, x => x.child(div().flex_none().text_color(t.dim).child("설계를 읽는 중입니다.")))
        .when(this.docEditor != null, x => x.child(Editor.new(this.docEditor).flex_1().min_h(0)))
        .when(Boolean(doc) && !this.confirm && selectedActionAllowed, x => x.child(approvalButtons))
        .when(Boolean(this.confirm), x => x.child(confirmBox));
      const detailCategory = h_flex().gap(8).text_size(12).text_color(t.muted)
        .child(div().whitespace_nowrap().child(this.viewProps.kind === "inventory" ? display(detail?.category ?? "구축 항목") : "제품 작업"))
        .when(Boolean(detail?.phase ?? detailWork.phase ?? detailWork.status), x => x.child(div().whitespace_nowrap().child(phaseText(detail?.phase ?? detailWork.phase ?? detailWork.status))));
      const titleBlock = v_flex().flex_1().min_w(0).gap(3)
        .child(div().truncate().whitespace_nowrap().text_size(18).font_semibold().child(display(this.viewProps.kind === "inventory" ? detailWork.name ?? detailWork.title ?? detailWork.id : detail?.title ?? detailWork.title ?? detail?.name ?? detailWork.id)))
        .child(detailCategory);
      const detailHeader = h_flex().flex_none().items_center().justify_between().gap(10)
        .child(titleBlock)
        .child(button("detail-close", "닫기", () => closeOverlay(overlay)));
      const leaderIsLive = Boolean(!this.detailError && !this.error && leader?.terminal && (liveAgents.some(agent => agent.address === leader.terminal || agent.pane === leader.terminal)
        || liveTerminals.some(terminal => terminal.address === leader.terminal || terminal.name === leader.terminal)));
      return v_flex().id("team-operations-detail").size_full().min_h(0).gap(8).p(14).bg(t.background).text_color(t.foreground).font_family(this.config?.font_family || settings().font_family || "monospace")
        .child(detailHeader)
        .when(this.detailBusy, x => x.child(div().p(10).text_color(t.dim).child("작업 정보를 읽는 중입니다.")))
        .when(Boolean(this.detailError), x => x.child(div().p(8).text_color(t.blocked).child(`작업 정보를 읽지 못했습니다: ${this.detailError}`)))
        .when(Boolean(detail?.goal || detail?.purpose || detail?.description || detail?.reason || detailWork.reason || detailWork.condition), x => x.child(v_flex().gap(4).p(8).rounded(7).bg(t.surface)
          .child(div().font_semibold().child(this.viewProps.kind === "inventory" ? "구축 항목 조건" : "작업 목적"))
          .child(words(detail?.goal ?? detail?.purpose ?? detail?.description ?? detailWork.reason ?? detailWork.condition ?? detailWork.title).text_size(12))))
        .when(Boolean(leader), x => x.child(h_flex().gap(8).items_center().child(div().flex_1().whitespace_nowrap().child(`담당 팀장 · ${display(leader.label)}`)).when(leaderIsLive, x => x.child(button("focus-leader", "팀장 이동", () => run("agent.focus", { terminal: leader.terminal }))))))
        .when(tasks.length > 0, x => x.child(v_flex().min_h(0).flex_1().gap(5).overflow_y_scrollbar()
          .child(div().flex_none().font_semibold().text_color(t.accent).child(`연결된 세션 · ${tasks.length}`))
          .children(detailSessions)))
        .when(detail?.observed_at != null, x => x.child(div().flex_none().text_size(10).text_color(t.dim).child(`세션 마지막 확인 · ${clock(detail.observed_at)}`)))
        .when(tasks.length === 0 && !this.detailBusy && !this.detailError, x => x.child(div().flex_none().p(8).text_size(12).text_color(t.muted).child(this.viewProps.kind === "inventory" && !this.viewProps.linkedWorkId ? "구축 항목은 제품 작업이나 세션으로 자동 연결하지 않습니다." : "연결된 세션 정보가 없습니다.")))
        .when(this.viewProps.kind !== "inventory" || Boolean(this.viewProps.linkedWorkId), x => x.child(documentSection));
    }
    const categoryForWork = work => WORK_PHASES[work.phase]?.column ?? "review";
    const categoryForInventory = item => {
      const status = String(item.status ?? "");
      if (status.startsWith("완료")) return "complete";
      if (/진행|실행 중/.test(status)) return "progress";
      if (/확인|승인|검토/.test(status)) return "review";
      return "waiting";
    };
    const boardColumns = [
      { id: "waiting", title: "대기", color: t.idle },
      { id: "progress", title: "진행 중", color: t.working },
      { id: "review", title: "확인 필요", color: t.blocked },
      { id: "complete", title: "완료", color: t.done }
    ];
    const workCards = works.map(work => {
      const category = categoryForWork(work);
      const reason = work.reason || (category === "waiting" ? work.next : null);
      return { id: work.id, category, kind: "work", label: "제품", title: work.title, state: phaseText(work.phase), reason, reasonLabel: work.reason ? "대기 이유" : "다음", percent: percent(work), raw: work };
    });
    const inventoryCards = inventory.map(item => {
      const complete = categoryForInventory(item) === "complete";
      return { id: item.id, category: categoryForInventory(item), kind: "inventory", label: display(item.category ?? "구축"), title: item.name, state: statusText(item.status), reason: item.condition, reasonLabel: "완료 조건", percent: complete ? "|██████████| 100% 1/1 · 완료 조건 확인 · 전체 진척률 아님" : null, raw: item };
    });
    const allCards = [...workCards, ...inventoryCards];
    const completedCount = allCards.filter(card => card.category === "complete").length;
    const boardCard = card => v_flex().id(`board-${card.kind}-${card.id}`).flex_none().gap(5).p(9).rounded(7).bg(t.surface).hover(el => el.bg(t.raised)).cursor_pointer()
      .on_click((_e, _cx) => this.openDetail(card.kind, card.raw))
      .child(h_flex().items_center().justify_between().gap(8)
        .child(div().flex_none().px(5).py(2).rounded(4).bg(t.chrome).text_size(10).text_color(t.muted).whitespace_nowrap().child(card.label))
        .child(div().flex_1().min_w(0).truncate().whitespace_nowrap().font_semibold().child(`${display(card.id)} · ${display(card.title)}`)))
      .child(h_flex().justify_between().gap(6).child(div().flex_1().min_w(0).truncate().whitespace_nowrap().text_size(11).text_color(card.category === "complete" ? t.done : card.category === "review" ? t.blocked : card.category === "progress" ? t.working : t.muted).child(card.state)).when(card.kind === "work", x => x.child(div().flex_none().whitespace_nowrap().text_size(10).text_color(t.accent).child(card.percent))))
      .when(card.kind === "inventory" && card.percent != null, x => x.child(v_flex().gap(2).text_size(10)
        .child(div().whitespace_nowrap().text_color(t.accent).child("|██████████| 100% 1/1"))
        .child(div().whitespace_nowrap().text_color(t.dim).child("완료 조건 확인 · 전체 진척률 아님"))))
      .when(Boolean(card.reason), x => x.child(words(`${card.reasonLabel} · ${display(card.reason)}`).text_size(11).text_color(t.dim)));
    const remaining = inventoryCards.filter(card => card.category !== "complete").length;
    const column = definition => {
      const cards = allCards.filter(card => card.category === definition.id);
      const visible = definition.id === "complete" && !this.showCompleted ? [] : cards;
      const holds = definition.id === "waiting" ? (data?.holds ?? []).map(hold => v_flex().flex_none().gap(4).p(8).rounded(7).bg(t.surface)
        .child(div().font_semibold().child(`보류 · ${display(hold.id)}`))
        .child(words(hold.reason).text_size(11).text_color(t.blocked))) : [];
      return v_flex().id(`kanban-${definition.id}`).w(270).h_full().flex_none().min_h(0).gap(6).p(7).rounded(8).bg(t.chrome)
        .child(h_flex().flex_none().items_center().justify_between().gap(6)
          .child(h_flex().items_center().gap(6).child(div().w(8).h(8).rounded_full().bg(definition.color)).child(div().whitespace_nowrap().font_semibold().child(definition.title)).child(div().whitespace_nowrap().text_size(11).text_color(t.dim).child(String(cards.length))))
          .when(definition.id === "complete", x => x.child(button("toggle-completed", this.showCompleted ? "접기" : "펼치기", cx => { this.showCompleted = !this.showCompleted; cx.notify(); }).text_size(10))))
        .child(v_flex().flex_1().min_h(0).gap(6).overflow_y_scrollbar().children([...holds, ...visible.map(boardCard)]))
        .when(cards.length === 0 && holds.length === 0, x => x.child(div().flex_none().px(4).py(8).text_size(11).text_color(t.dim).child("항목 없음")))
        .when(definition.id === "complete" && cards.length > 0 && !this.showCompleted, x => x.child(div().flex_none().text_size(11).text_color(t.dim).child(`완료된 항목 ${cards.length}건 · 전체 진척률과 별도`)));
    };
    const accountRows = accounts.map(account => {
      const key = `account-${account.provider}-${account.account}`, open = this.expanded.has(key);
      const usage = (value, name) => value == null ? (account.windows?.[name]?.status === "not_provided" ? "미제공" : "확인 필요") : `${display(value)}%${account.windows?.[name]?.status === "stale" ? "*" : ""}`;
      const usageCell = (label, value, name) => {
        const measured = typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 100;
        const stale = account.windows?.[name]?.status === "stale";
        const color = stale ? t.dim : value >= 90 ? t.ansi1 : value >= 70 ? t.ansi3 : t.ansi2;
        return h_flex().w(210).flex_none().gap(6).items_center()
          .child(div().w(36).flex_none().text_color(t.muted).child(label))
          .when(measured, el => el.child(div().w(92).h(10).flex_none().rounded(2).bg(t.raised)
            .child(div().w(92 * value / 100).h(10).rounded(2).bg(color))))
          .child(div().flex_1().min_w(0).truncate().text_color(measured ? color : t.foreground).child(usage(value, name)));
      };
      const meta = [account.coverage == null ? null : `측정 범위 · ${display(account.coverage)}`, account.reason == null ? null : `사유 · ${display(account.reason)}`, account.action == null ? null : `요청 · ${display(account.action)}`].filter(Boolean).join(" · ");
      return v_flex().id(key).gap(2).py(2).px(8).rounded(6).bg(t.surface).cursor_pointer()
        .on_click((_e, cx) => { open ? this.expanded.delete(key) : this.expanded.add(key); cx.notify(); })
        .child(h_flex().h(24).flex_none().gap(8).items_center().text_size(11)
          .child(div().w(92).flex_none().truncate().font_semibold().child(display(account.label)))
          .child(usageCell("5시간", account.five_hour, "five_hour"))
          .child(usageCell("주간", account.weekly, "weekly"))
          .child(div().flex_1().min_w(0).truncate().child(display(account.coverage ?? statusText(account.status))))
          .child(div().w(92).flex_none().truncate().text_color(t.dim).child(duration(account.age))))
        .when(open && Boolean(meta), el => el.child(div().text_size(10).text_color(t.muted).child(meta)))
        .when(Boolean(this.usageMessage), el => el.child(div().text_size(10).text_color(/⚠|실패|오류/i.test(this.usageMessage) ? t.blocked : t.muted).child(this.usageMessage)));
    });
    const actionRows = (data?.actions ?? []).map(action => {
      const failed = /실패|오류|fail|error/i.test(`${action.status ?? ""} ${action.message ?? ""}`);
      return h_flex().gap(8).p(6).rounded(6).bg(t.surface).text_size(11)
        .child(div().w(72).flex_none().truncate().whitespace_nowrap().font_semibold().child(display(action.id)))
        .child(words(action.status).w(160).flex_none().text_color(failed ? t.blocked : t.muted))
        .child(words(action.message).flex_1().min_w(0));
    });
    const board = h_flex().id("operations-kanban").w_full().flex_1().min_h(0).items_stretch().gap(8).overflow_x_scrollbar()
      .children(boardColumns.map(column));
    const accountArea = v_flex().id("account-area").w_full().max_h(150).flex_none().min_h(0).gap(3).overflow_y_scrollbar()
      .child(h_flex().flex_none().justify_between().items_center()
        .child(div().font_semibold().text_size(12).text_color(t.accent).child("계정 · 보조 정보"))
        .child(h_flex().flex_1().min_w(0).gap(10).px(12).text_size(10)
          .child(div().text_color(t.ansi2).child("<70%"))
          .child(div().text_color(t.ansi3).child("70–<90%"))
          .child(div().text_color(t.ansi1).child("≥90%"))
          .child(div().text_color(t.dim).child("지난 기록")))
        .child(button("refresh-usage", this.usageBusy || data?.usage_refreshing ? "사용량 갱신 중" : "사용량 갱신", cx => cx.spawn(async cx => this.refreshUsage(cx)), this.usageBusy || data?.usage_refreshing).text_size(11)))
      .when(Boolean(this.usageMessage), x => x.child(div().flex_none().text_size(10).text_color(/⚠|실패|오류/i.test(this.usageMessage) ? t.blocked : t.muted).child(this.usageMessage)))
      .children(accountRows);
    return v_flex().id("team-operations").size_full().min_h(0).overflow_hidden().bg(t.background).text_color(t.foreground).text_size(13)
      .font_family(this.config?.font_family || settings().font_family || "monospace")
      .child(h_flex().h(48).flex_none().px(14).gap(8).items_center().border_b(1).border_color(t.border)
        .child(div().font_semibold().text_size(16).child("팀 운영 현황")).child(div().flex_1())
        .child(div().text_size(11).text_color(t.dim).child(`마지막 관찰: ${clock(data?.observed_at)}`))
        .child(button("refresh", this.busy ? "불러오는 중" : "새로고침", cx => cx.spawn(async cx => this.refresh(cx)), this.busy)))
      .when(Boolean(this.error), el => el.child(div().px(12).py(7).text_color(t.blocked).child(`갱신에 실패했습니다. 기존 자료는 이전 관찰값입니다: ${this.error}`)))
      .when(Boolean(this.configError), el => el.child(div().px(12).py(7).text_color(t.blocked).child(`로컬 backend 설정을 읽을 수 없습니다: ${this.configError}`)))
      .when(this.busy && !data, el => el.child(div().p(14).text_color(t.dim).child("운영 현황을 읽는 중입니다.")))
      .child(v_flex().id("operations-content").w_full().flex_1().min_h(0).gap(6).p(8)
        .child(v_flex().w_full().flex_none().gap(3)
          .child(h_flex().flex_wrap().gap(12).text_size(12).font_semibold()
            .child(div().whitespace_nowrap().child(`실행 ${appCounts.working}`))
            .child(div().whitespace_nowrap().child(`사용자 답변 필요 ${appCounts.blocked}`))
            .child(div().whitespace_nowrap().text_color(t.muted).child(`미확인 ${unknownTeams}개`))
            .child(div().whitespace_nowrap().text_color(coordinator?.state === "recovering" ? t.working : t.muted).child(`총괄 · ${coordinatorStateText}`)))
          .when(coordinator?.state !== "live" && Boolean(coordinator?.reason), el => el.child(words(`총괄 상태 · ${display(coordinator.reason)}`).text_size(11).text_color(t.muted))))
        .when(waiting.length > 0, el => el.child(v_flex().w_full().flex_none().gap(2)
          .child(div().whitespace_nowrap().text_size(11).font_semibold().text_color(t.muted).child("대기 이유"))
          .children(waiting.map((reason, index) => words(`⚠ ${index + 1} · ${reason}`).text_size(11).text_color(t.dim)))))
        .when(Boolean(this.coordinatorMessage) && (coordinator?.state !== "live" || /⚠|실패|오류/i.test(this.coordinatorMessage)), el => el.child(div().flex_none().text_size(11).text_color(/⚠|실패|오류/i.test(this.coordinatorMessage) ? t.blocked : t.muted).child(this.coordinatorMessage)))
        .when(actionRows.length > 0, el => el.child(v_flex().flex_none().gap(3).children(actionRows)))
        .child(v_flex().flex_none().gap(3)
          .child(div().whitespace_nowrap().text_size(11).font_semibold().text_color(t.muted).child("작업 목록"))
          .child(h_flex().flex_wrap().gap(12).text_size(11).text_color(t.muted)
            .child(div().whitespace_nowrap().child(`남은 구축 ${remaining}건`))
            .child(div().whitespace_nowrap().child(`완료 카드 ${completedCount}건`))))
        .child(board)
        .child(accountArea));
  }
}
