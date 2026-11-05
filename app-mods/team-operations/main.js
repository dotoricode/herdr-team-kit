// 종합상황실: departments, knowledge, goal and proposals, bounties and coffee notes.
// The same mod draws the page (no role) and its dialogs (props().role), so dialogs
// are app overlays: Esc and a press beside them close them.

import { View, div } from "gpui-kit";
import { v_flex, h_flex, InputState, Input, TextareaState, Textarea } from "gpui-base";
import { appHome, readTextFile, exec, theme, run, props, openOverlay, closeOverlay, shared, setShared, nextCommand } from "herdr";

const REV = "team-operations.rev";
const REFRESH_MS = 15000;
const TABS = ["floor", "goal", "bounty"];
const COLORS = { operations: "#06b6d4", eversafe: "#8b5cf6", fakefinder: "#10b981", server: "#f43f5e",
  codemix: "#0ea5e9", qa: "#f59e0b", docs: "#6366f1", infra: "#f97316" };
const color = id => COLORS[id] ?? "#64748b";
const floorOf = label => (String(label ?? "").match(/^(\d+)/)?.[1] ?? "") ;
const shortName = label => String(label ?? "").split("·").pop().trim() || String(label ?? "");
const clock = seconds => {
  if (!seconds) return "";
  const d = new Date(seconds * 1000);
  const pad = n => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
};
const ago = seconds => {
  if (!seconds) return "";
  const m = Math.max(0, Math.round((Date.now() / 1000 - seconds) / 60));
  return m < 1 ? "방금" : m < 60 ? `${m}분 전` : m < 1440 ? `${Math.round(m / 60)}시간 전` : `${Math.round(m / 1440)}일 전`;
};
const bump = () => setShared(REV, Date.now());

async function loadConfig() {
  return JSON.parse(await readTextFile(appHome() + "/mods/team-operations/local.json"));
}

async function bridge(config, args) {
  const result = await exec(config.python, [config.script, "--config", config.config, "--state", config.state,
    "--bindings", config.bindings, ...args]);
  if (result.code !== 0) {
    let message = result.stderr || "요청 실패";
    try { message = JSON.parse(result.stderr).error ?? message; } catch (_) {}
    throw new Error(message);
  }
  return JSON.parse(result.stdout);
}

const hqAction = (config, action) => bridge(config, ["hq-action", "--send-json", JSON.stringify(action)]);

function button(t, id, text, onClick, tone = null) {
  return div().id(id).px(10).py(5).rounded(6).cursor_pointer().flex_none().whitespace_nowrap()
    .text_size(12).font_semibold()
    .bg(tone ?? t.raised).text_color(tone ? "#ffffff" : t.foreground)
    .hover(s => s.opacity(0.85))
    .on_click((_e, cx) => onClick(cx))
    .child(text);
}

function chip(t, id, text, active, onClick, tone = null) {
  return div().id(id).px(8).py(3).rounded(4).cursor_pointer().flex_none().whitespace_nowrap()
    .text_size(11).font_semibold()
    .bg(active ? (tone ?? t.accent) : t.raised).text_color(active ? "#ffffff" : t.foreground)
    .on_click((_e, cx) => onClick(cx))
    .child(text);
}

function panel(t) {
  return v_flex().min_h(0).gap(8).p(10).rounded(8).bg(t.surface).border(1).border_color(t.border);
}

function panelTitle(t, title, note = "") {
  return h_flex().w_full().flex_none().items_center().justify_between().pb(4).border_b(1).border_color(t.border)
    .child(div().font_bold().text_size(13).child(title))
    .child(div().text_size(10).text_color(t.muted).child(note));
}

function empty(t, text) {
  return div().p(10).text_size(11).text_color(t.muted).child(text);
}

// ---------------------------------------------------------------- dialogs

class Dialog {
  constructor(view, p) {
    this.view = view;
    this.p = p;
    this.error = null;
    this.busy = false;
    this.team = p.team ?? "";
    this.points = 300;
    this.title = InputState.new({ placeholder: p.titleHint ?? "제목" });
    this.body = TextareaState.new({ placeholder: p.bodyHint ?? "내용", rows: 4 });
  }

  close() { closeOverlay(props().overlay); }

  async submit(cx, action) {
    if (this.busy) return;
    this.busy = true;
    this.error = null;
    cx.notify();
    try {
      const config = await loadConfig();
      await action(config);
      bump();
      this.close();
    } catch (error) {
      this.error = String(error.message ?? error);
      this.busy = false;
      cx.notify();
    }
  }

  teamPicker(t, withCommon) {
    const teams = (this.p.teams ?? []).filter(x => withCommon || x.id !== "common");
    return h_flex().flex_wrap().gap(4)
      .when(!withCommon, el => el.child(chip(t, "team-none", "부서 없음", this.team === "", cx => { this.team = ""; cx.notify(); })))
      .children(teams.map(x => chip(t, `team-${x.id}`, x.label, this.team === x.id, cx => { this.team = x.id; cx.notify(); }, color(x.id))));
  }

  field(t, label, element) {
    return v_flex().gap(4)
      .child(div().text_size(11).font_semibold().child(label))
      .child(div().px(8).py(6).rounded(6).border(1).border_color(t.border).bg(t.raised).child(element.w_full().text_size(12)));
  }

  frame(t, heading, note, content, submitText, onSubmit) {
    return v_flex().size_full().p(16).gap(12).bg(t.surface).text_color(t.foreground)
      .child(h_flex().items_center().justify_between()
        .child(div().font_bold().text_size(14).child(heading))
        .child(button(t, "dialog-close", "닫기", () => this.close())))
      .when(Boolean(note), el => el.child(div().text_size(11).text_color(t.muted).child(note)))
      .child(content)
      .when(Boolean(this.error), el => el.child(div().p(8).rounded(6).bg("#450a0a").text_size(12).text_color("#fca5a5").child(this.error)))
      .child(h_flex().justify_end().gap(8)
        .child(button(t, "dialog-cancel", "취소", () => this.close()))
        .child(button(t, "dialog-submit", this.busy ? "저장 중..." : submitText, cx => cx.spawn(async cx => onSubmit(cx)), "#10b981")));
  }

  render(cx) {
    const t = theme();
    const p = this.p;
    if (p.role === "badge") return this.badge(t);
    if (p.role === "gotcha") return this.frame(t, "새 주의사항", "부서 위키 문서 끝에 추가됩니다.",
      v_flex().gap(10).child(this.teamPicker(t, true)).child(this.field(t, "제목", Input.new(this.title)))
        .child(this.field(t, "내용", Textarea.new(this.body))),
      "위키에 저장", cx => this.submit(cx, config => bridge(config, ["knowledge-add", "--team", this.team || "common",
        "--title", this.title.value(), "--content", this.body.value()])));
    if (p.role === "bounty") return this.frame(t, "현상금 걸기", "유휴 직원이 `python3 -m teamkit.hq bounty-claim`으로 맡습니다.",
      v_flex().gap(10).child(this.teamPicker(t, false)).child(this.field(t, "난제", Input.new(this.title)))
        .child(this.field(t, "재현 조건·완료 기준", Textarea.new(this.body)))
        .child(h_flex().gap(4).items_center().child(div().text_size(11).font_semibold().mr(4).child("현상금"))
          .children([100, 300, 500, 1000].map(n => chip(t, `pts-${n}`, `${n}pt`, this.points === n, cx => { this.points = n; cx.notify(); })))),
      "현상금 걸기", cx => this.submit(cx, config => hqAction(config, { op: "bounty-add", title: this.title.value(),
        detail: this.body.value(), team: this.team, points: this.points, by: "사용자" })));
    if (p.role === "coffee") return this.frame(t, "커피타임 근황", "24시간 동안 커피타임에 보입니다.",
      v_flex().gap(10).child(this.teamPicker(t, false)).child(this.field(t, "한 줄 근황", Input.new(this.title))),
      "남기기", cx => this.submit(cx, config => hqAction(config, { op: "coffee", text: this.title.value(), team: this.team, by: "사용자" })));
    if (p.role === "resolve") return this.frame(t, "현상금 해결 기록", p.item?.title ?? "",
      v_flex().gap(10).child(this.field(t, "해결 내용과 근거", Textarea.new(this.body))),
      "해결로 기록", cx => this.submit(cx, config => hqAction(config, { op: "bounty-resolve", item_id: p.item.id,
        by: p.item.claimed_by || "사용자", note: this.body.value() })));
    return empty(t, `알 수 없는 대화상자: ${p.role}`);
  }

  badge(t) {
    const e = this.p.employee ?? {};
    const row = (label, value) => h_flex().justify_between().py(2)
      .child(div().text_size(11).text_color(t.muted).child(label))
      .child(div().text_size(11).font_semibold().child(value));
    return v_flex().size_full().p(16).gap(12).bg(t.surface).text_color(t.foreground)
      .child(h_flex().items_center().gap(12)
        .child(div().size(44).rounded_full().bg(e.provider === "codex" ? "#059669" : "#7c3aed").flex().items_center().justify_center()
          .child(div().font_bold().text_size(18).text_color("#ffffff").child(String(e.label ?? "?").slice(0, 1))))
        .child(v_flex().gap(2)
          .child(div().font_bold().text_size(15).child(e.label ?? "직원"))
          .child(div().text_size(11).text_color(t.muted).child(e.teamLabel ?? ""))))
      .child(v_flex().gap(4).p(8).rounded(6).border(1).border_color(t.border)
        .child(row("공급자", e.provider === "codex" ? "Codex" : "Claude Code"))
        .child(row("계정", e.account ?? "확인 안 됨"))
        .child(row("터미널", e.terminal ?? "미확인"))
        .child(row("상태", e.state === "working" ? "작업 중" : e.state === "idle" ? "대기" : String(e.state ?? "미확인")))
        .child(row("신원", e.identity_verified ? "작업 소유권으로 확인됨" : "화면 표시 이름 기준")))
      .child(h_flex().justify_end().gap(8)
        .child(button(t, "badge-close", "닫기", () => this.close()))
        .when(Boolean(e.terminal), el => el.child(button(t, "badge-go", "터미널로 이동", () => {
          run("agent.focus", { terminal: e.terminal });
          this.close();
        }, t.accent))));
  }
}

// ---------------------------------------------------------------- page

export default class TeamOperations extends View {
  init(_props, cx) {
    const p = props() ?? {};
    if (p.role) {
      this.dialog = new Dialog(this, p);
      return;
    }
    this.config = null;
    this.data = null;
    this.error = null;
    this.loading = true;
    this.inflight = false;
    this.tab = "floor";
    this.dept = "all";
    this.seenRev = shared(REV);
    this.search = InputState.new({ placeholder: "주의사항·직원 검색" });
    this.search.on("change", (_e, cx) => cx.notify());
    cx.spawn(async cx => {
      try {
        this.config = await loadConfig();
      } catch (error) {
        this.error = `local.json을 읽지 못했습니다: ${error}`;
      }
      await this.refresh(cx);
    });
    cx.timer.every(REFRESH_MS, cx => cx.spawn(async cx => this.refresh(cx)));
    // "team-operations.goal" (keys.json or `herdr-app do`) shows a tab; "refresh" reloads.
    cx.spawn(async cx => {
      for (;;) {
        const command = await nextCommand();
        if (TABS.includes(command)) { this.tab = command; cx.notify(); }
        else if (command === "refresh") await this.refresh(cx);
      }
    });
  }

  // Background refresh keeps what is shown and notifies only when data or error changed.
  async refresh(cx) {
    if (!this.config || this.inflight) {
      if (this.loading && !this.config) { this.loading = false; cx.notify(); }
      return;
    }
    this.inflight = true;
    let data = this.data;
    let error = null;
    try {
      data = await bridge(this.config, ["knowledge-hub"]);
    } catch (e) {
      error = String(e.message ?? e);
    }
    this.inflight = false;
    const changed = this.loading || error !== this.error || JSON.stringify(data) !== JSON.stringify(this.data);
    this.data = data;
    this.error = error;
    this.loading = false;
    if (changed) cx.notify();
  }

  async act(cx, action) {
    try {
      await hqAction(this.config, action);
      await this.refresh(cx);
    } catch (error) {
      this.error = String(error.message ?? error);
      cx.notify();
    }
  }

  teams() {
    return (this.data?.roster?.teams ?? []).map(x => ({ id: x.id, label: x.label, space: x.space, employees: x.employees ?? [] }));
  }

  dialogTeams() {
    return [{ id: "common", label: "전사 공통" }, ...this.teams().map(x => ({ id: x.id, label: shortName(x.label) }))];
  }

  open(role, extra = {}, size = { width: 520, height: 460 }) {
    openOverlay({ mod: "team-operations", kind: "dialog", width: size.width, height: size.height,
      props: { role, teams: this.dialogTeams(), ...extra } });
  }

  render(cx) {
    if (this.dialog) return this.dialog.render(cx);
    const rev = shared(REV);
    if (rev !== this.seenRev) {
      this.seenRev = rev;
      cx.spawn(async cx => this.refresh(cx));
    }
    const t = theme();
    const teams = this.teams();
    const hq = this.data?.hq ?? {};
    const staff = teams.flatMap(x => x.employees);
    const working = staff.filter(e => e.state === "working").length;
    const openBounties = (hq.bounties ?? []).filter(b => b.status !== "resolved").length;
    const pending = (hq.proposals ?? []).filter(x => x.status === "대기").length;
    const tabs = [["floor", "부서 현황"], ["goal", `목표·제안함${pending ? ` ${pending}` : ""}`], ["bounty", `현상금 보드${openBounties ? ` ${openBounties}` : ""}`]];

    return v_flex().id("team-operations").size_full().min_h(0).p(14).gap(10)
      .bg(t.background).text_color(t.foreground)
      .child(h_flex().w_full().flex_none().items_center().justify_between()
        .child(v_flex().gap(1)
          .child(div().font_bold().text_size(16).child("종합상황실"))
          .child(div().text_size(11).text_color(t.muted).child(this.loading ? "불러오는 중..." :
            `관측 ${clock(this.data?.roster?.observed_at)} · 15초마다 갱신`)))
        .child(h_flex().gap(6)
          .child(button(t, "open-gotcha", "+ 주의사항", () => this.open("gotcha", { titleHint: "예: SDK 변경 시 영향 점검" })))
          .child(button(t, "refresh", "새로고침", cx => cx.spawn(async cx => this.refresh(cx))))))
      .when(Boolean(this.error), el => el.child(div().p(8).rounded(6).bg("#450a0a").text_size(12).text_color("#fca5a5")
        .child(`조회 오류: ${this.error}`)))
      .child(h_flex().w_full().flex_none().gap(8)
        .child(this.kpi(t, "직원", `${staff.length}명`, `작업 중 ${working} · 대기 ${staff.length - working}`, "#3b82f6"))
        .child(this.kpi(t, "공통 목표", hq.goal ? hq.goal.name : "없음", hq.goal ? `지표 ${hq.goal.metrics.length}개` : "goal-setting으로 작성", "#10b981"))
        .child(this.kpi(t, "대기 제안", `${pending}건`, `전체 ${(hq.proposals ?? []).length}건`, "#8b5cf6"))
        .child(this.kpi(t, "열린 현상금", `${openBounties}건`, `해결 ${(hq.bounties ?? []).length - openBounties}건`, "#f59e0b")))
      .child(h_flex().w_full().flex_none().gap(4)
        .children(tabs.map(([id, label]) => chip(t, `tab-${id}`, label, this.tab === id, cx => { this.tab = id; cx.notify(); }))))
      .child(h_flex().w_full().flex_1().min_h(0).gap(12)
        .child(v_flex().flex_1().min_w(0).min_h(0)
          .child(this.tab === "goal" ? this.goalTab(t, hq) : this.tab === "bounty" ? this.bountyTab(t, hq) : this.floorTab(t, teams)))
        .child(v_flex().w(380).flex_none().min_h(0).gap(10)
          .child(this.coffee(t, hq))
          .child(this.knowledge(t))));
  }

  kpi(t, label, value, note, tone) {
    return v_flex().flex_1().min_w(0).p(8).gap(2).rounded(8).bg(t.surface).border(1).border_color(t.border)
      .child(div().text_size(11).text_color(t.muted).child(label))
      .child(div().font_bold().text_size(15).text_color(tone).truncate().child(value))
      .child(div().text_size(10).text_color(t.muted).truncate().child(note));
  }

  floorTab(t, teams) {
    const shown = teams.filter(x => this.dept === "all" || x.id === this.dept);
    return panel(t).size_full()
      .child(panelTitle(t, "층별 부서", "직원을 누르면 사원증"))
      .child(h_flex().flex_none().flex_wrap().gap(4)
        .child(chip(t, "dept-all", "전체", this.dept === "all", cx => { this.dept = "all"; cx.notify(); }))
        .children(teams.map(x => chip(t, `dept-${x.id}`, shortName(x.label), this.dept === x.id, cx => { this.dept = x.id; cx.notify(); }, color(x.id)))))
      .child(v_flex().flex_1().min_h(0).gap(8).overflow_y_scrollbar()
        .when(!shown.length, el => el.child(empty(t, this.loading ? "불러오는 중..." : "표시할 부서가 없습니다.")))
        .children(shown.map(team => this.deptCard(t, team))));
  }

  deptCard(t, team) {
    const busy = team.employees.filter(e => e.state === "working").length;
    return v_flex().id(`dept-card-${team.id}`).w_full().p(8).gap(6).rounded(6).bg(t.raised).border_l(3).border_color(color(team.id))
      .child(h_flex().w_full().items_center().justify_between()
        .child(h_flex().items_center().gap(6)
          .child(div().px(5).py(1).rounded(4).bg(color(team.id)).text_size(10).font_bold().text_color("#ffffff").child(`${floorOf(team.label) || "-"}F`))
          .child(div().font_bold().text_size(12).child(shortName(team.label)))
          .child(div().text_size(10).text_color(t.muted).child(`${team.employees.length}명${busy ? ` · 작업 중 ${busy}` : ""}`)))
        .when(Boolean(team.space), el => el.child(button(t, `enter-${team.id}`, "스페이스로 이동", () => run("space.select", { id: team.space })))))
      .child(h_flex().w_full().flex_wrap().gap(4)
        .when(!team.employees.length, el => el.child(div().text_size(11).text_color(t.muted).child("열린 세션 없음")))
        .children(team.employees.map(e => h_flex().id(`emp-${e.terminal}`).items_center().gap(4).px(6).py(3).rounded(4)
          .bg(t.surface).border(1).border_color(e.state === "working" ? "#10b981" : t.border).cursor_pointer()
          .hover(s => s.opacity(0.85))
          .on_click(() => this.open("badge", { employee: { ...e, teamLabel: team.label } }, { width: 380, height: 340 }))
          .child(div().size(6).rounded_full().bg(e.state === "working" ? "#10b981" : "#94a3b8"))
          .child(div().text_size(11).font_semibold().child(e.label))
          .child(div().text_size(9).text_color(t.muted).child(e.provider === "codex" ? "Codex" : "Claude")))));
  }

  goalTab(t, hq) {
    const goal = hq.goal;
    const proposals = hq.proposals ?? [];
    const table = (rows, cols) => v_flex().gap(2).children(rows.map((r, i) => h_flex().id(`row-${i}-${cols[0]}`).gap(8).py(2)
      .children(cols.map(c => div().flex_1().min_w(0).text_size(11).truncate().child(String(r[c] ?? "")))))) ;
    return v_flex().size_full().min_h(0).gap(10)
      .child(panel(t).flex_none()
        .child(panelTitle(t, goal ? `공통 목표 · ${goal.name}` : "공통 목표", goal ? `정본 ${ago(goal.updated_at)} 수정` : ""))
        .when(!goal, el => el.child(empty(t, "목표 브리프가 아직 없습니다. goal-setting 스킬로 사용자와 합의해 지식 폴더의 GOAL-BRIEF.md로 저장하세요.")))
        .when(Boolean(goal), el => el
          .child(div().text_size(12).font_semibold().child(goal.sentence || "목표 문장 없음"))
          .when(goal.metrics.length > 0, el => el.child(div().text_size(11).text_color(t.muted).child("성공 지표")).child(table(goal.metrics, ["지표", "기준선", "목표", "기한"])))
          .when(goal.guardrails.length > 0, el => el.child(div().text_size(11).text_color(t.muted).child("가드레일")).child(table(goal.guardrails, ["지표", "허용 한계", "넘으면 할 일"])))
          .when(goal.donts.length > 0, el => el.child(div().text_size(11).text_color("#fca5a5").child(`하지 말 것: ${goal.donts.join(" · ")}`)))))
      .child(panel(t).flex_1()
        .child(panelTitle(t, `제안함 (${proposals.length})`, "에이전트: python3 -m teamkit.hq propose"))
        .child(v_flex().flex_1().min_h(0).gap(6).overflow_y_scrollbar()
          .when(!proposals.length, el => el.child(empty(t, "아직 제안이 없습니다.")))
          .children(proposals.map(x => this.proposalCard(t, x)))));
  }

  proposalCard(t, x) {
    const tone = x.status === "채택" ? "#10b981" : x.status === "보류" ? "#64748b" : "#8b5cf6";
    return v_flex().id(`proposal-${x.id}`).w_full().p(8).gap(4).rounded(6).bg(t.raised)
      .child(h_flex().items_center().justify_between()
        .child(h_flex().gap(6).items_center().min_w(0)
          .child(div().px(5).py(1).rounded(4).bg(tone).text_size(10).font_bold().text_color("#ffffff").child(x.status))
          .child(div().font_bold().text_size(12).truncate().child(x.title)))
        .child(div().text_size(10).text_color(t.muted).child(`${x.proposed_by} · ${ago(x.created_at)}`)))
      .when(Boolean(x.metric), el => el.child(div().text_size(11).text_color(t.muted).child(`기여 지표: ${x.metric}`)))
      .child(div().text_size(11).child(x.content))
      .when(Boolean(x.hypothesis), el => el.child(div().text_size(11).text_color(t.muted).child(`가설: ${x.hypothesis}`)))
      .when(x.status === "대기", el => el.child(h_flex().justify_end().gap(6)
        .child(button(t, `hold-${x.id}`, "보류", cx => cx.spawn(async cx => this.act(cx, { op: "proposal-decide", item_id: x.id, status: "보류" }))))
        .child(button(t, `adopt-${x.id}`, "채택", cx => cx.spawn(async cx => this.act(cx, { op: "proposal-decide", item_id: x.id, status: "채택" })), "#10b981"))));
  }

  bountyTab(t, hq) {
    const all = hq.bounties ?? [];
    const open = all.filter(b => b.status !== "resolved");
    const done = all.filter(b => b.status === "resolved").sort((a, b) => (b.resolved_at ?? 0) - (a.resolved_at ?? 0));
    const hunters = {};
    for (const b of done) hunters[b.resolved_by] = (hunters[b.resolved_by] ?? 0) + (b.points ?? 0);
    const ranking = Object.entries(hunters).sort((a, b) => b[1] - a[1]).slice(0, 5);
    return h_flex().size_full().min_h(0).gap(10)
      .child(panel(t).flex_1().min_w(0)
        .child(h_flex().w_full().flex_none().items_center().justify_between().pb(4).border_b(1).border_color(t.border)
          .child(div().font_bold().text_size(13).child(`열린 현상금 (${open.length})`))
          .child(button(t, "open-bounty", "+ 현상금 걸기", () => this.open("bounty", { titleHint: "예: test-macbook 간헐 타임아웃 원인 규명" }), "#f59e0b")))
        .child(div().flex_none().text_size(10).text_color(t.muted).child("에이전트: python3 -m teamkit.hq bounty-claim <id> --by <이름>"))
        .child(v_flex().flex_1().min_h(0).gap(6).overflow_y_scrollbar()
          .when(!open.length, el => el.child(empty(t, "열린 현상금이 없습니다.")))
          .children(open.map(b => this.bountyCard(t, b)))))
      .child(panel(t).w(240).flex_none()
        .child(panelTitle(t, "명예의 전당"))
        .when(!ranking.length, el => el.child(empty(t, "해결 기록이 아직 없습니다.")))
        .children(ranking.map(([name, pts], i) => h_flex().id(`hunter-${i}`).justify_between().py(2)
          .child(div().text_size(12).font_semibold().child(`${i + 1}. ${name}`))
          .child(div().text_size(12).text_color("#f59e0b").child(`${pts}pt`))))
        .child(div().pt(6).text_size(11).text_color(t.muted).child("최근 해결"))
        .child(v_flex().flex_1().min_h(0).gap(4).overflow_y_scrollbar()
          .children(done.slice(0, 10).map(b => v_flex().id(`done-${b.id}`).py(2)
            .child(div().text_size(11).font_semibold().truncate().child(b.title))
            .child(div().text_size(10).text_color(t.muted).truncate().child(`${b.resolved_by} · ${b.resolution}`))))));
  }

  bountyCard(t, b) {
    const claimed = b.status === "claimed";
    return v_flex().id(`bounty-${b.id}`).w_full().p(8).gap(4).rounded(6).bg(t.raised).border_l(3).border_color(b.team ? color(b.team) : "#f59e0b")
      .child(h_flex().items_center().justify_between()
        .child(div().font_bold().text_size(12).truncate().child(b.title))
        .child(div().text_size(12).font_bold().text_color("#f59e0b").child(`${b.points}pt`)))
      .when(Boolean(b.detail), el => el.child(div().text_size(11).child(b.detail)))
      .child(h_flex().items_center().justify_between()
        .child(div().text_size(10).text_color(t.muted).child(claimed ? `담당 ${b.claimed_by} · ${ago(b.claimed_at)}` : `대기 · id ${b.id} · ${ago(b.created_at)}`))
        .when(claimed, el => el.child(h_flex().gap(6)
          .child(button(t, `release-${b.id}`, "내려놓기", cx => cx.spawn(async cx => this.act(cx, { op: "bounty-release", item_id: b.id }))))
          .child(button(t, `resolve-${b.id}`, "해결 기록", () => this.open("resolve", { item: b, bodyHint: "원인, 수정, 확인한 근거" }), "#10b981")))));
  }

  coffee(t, hq) {
    const notes = hq.coffee ?? [];
    const tip = hq.tip;
    return panel(t).flex_none()
      .child(h_flex().w_full().items_center().justify_between().pb(4).border_b(1).border_color(t.border)
        .child(div().font_bold().text_size(13).child("커피타임"))
        .child(button(t, "open-coffee", "+ 근황", () => this.open("coffee", { titleHint: "예: NDK 빌드 정리 끝, 오후엔 문서 작업" }, { width: 480, height: 300 }))))
      .when(Boolean(tip), el => el.child(v_flex().gap(2).p(6).rounded(6).bg(t.raised)
        .child(div().text_size(10).text_color(t.muted).child(`오늘의 주의사항 · ${tip.team}`))
        .child(div().text_size(11).font_semibold().child(tip.title))
        .child(div().text_size(11).text_color(t.muted).child(tip.body.slice(0, 160)))))
      .when(!notes.length, el => el.child(empty(t, "최근 24시간 근황이 없습니다.")))
      .children(notes.slice(0, 5).map(n => h_flex().id(`coffee-${n.id}`).gap(6).items_center()
        .child(div().size(6).rounded_full().flex_none().bg(n.team ? color(n.team) : "#94a3b8"))
        .child(div().flex_1().min_w(0).text_size(11).truncate().child(`${n.by}: ${n.text}`))
        .child(div().flex_none().text_size(10).text_color(t.muted).child(ago(n.at)))));
  }

  knowledge(t) {
    const q = this.search.value().trim().toLowerCase();
    const items = (this.data?.knowledge ?? []).filter(x => !q || `${x.team} ${x.title} ${x.body}`.toLowerCase().includes(q));
    return panel(t).flex_1()
      .child(panelTitle(t, `지식 허브 (${items.length})`))
      .child(div().flex_none().h(30).px(8).flex().items_center().rounded(6).border(1).border_color(t.border).bg(t.raised)
        .child(Input.new(this.search).w_full().text_size(12)))
      .child(v_flex().flex_1().min_h(0).gap(6).overflow_y_scrollbar()
        .when(!items.length, el => el.child(empty(t, q ? "검색 결과가 없습니다." : "지식 문서가 없습니다.")))
        .children(items.map(x => v_flex().id(`kn-${x.team}`).w_full().p(8).gap(2).rounded(6).bg(t.raised)
          .child(h_flex().gap(6).items_center()
            .child(div().size(6).rounded_full().bg(color(x.team)))
            .child(div().font_bold().text_size(11).truncate().child(x.title)))
          .child(div().text_size(11).text_color(t.muted).child(x.body.split("\n").slice(1).join(" ").trim().slice(0, 220))))));
  }
}
