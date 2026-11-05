// 종합상황실: what every staff session is doing now, departments, the shared goal,
// proposals, bounties, coffee notes and the knowledge hub.
// The same mod draws the page (no role) and its dialogs (props().role), so dialogs are
// app overlays: Esc and a press beside them close them.
//
// Look: its own dark neutrals and four meaning colours only (not the app theme):
//   ACCENT selection, actions, information · OK working, healthy, done
//   WARN attention, waiting, possible stall  · BAD errors, failures

import { View, div } from "gpui-kit";
import { v_flex, h_flex, InputState, Input, TextareaState, Textarea } from "gpui-base";
import { appHome, readTextFile, exec, run, props, openOverlay, closeOverlay, shared, setShared, nextCommand } from "herdr";

const REV = "team-operations.rev";
const REFRESH_MS = 15000;
const TABS = ["progress", "floor", "goal", "bounty"];
const STALE_S = 45;

const C = {
  bg: "#0d1015", panel: "#151a21", raised: "#1c222b", line: "#29303b",
  text: "#eef1f5", sub: "#a7b0bd", faint: "#6c7684",
  ACCENT: "#4c8dff", OK: "#2fbf71", WARN: "#f0a020", BAD: "#ef4b4b",
};
const FS = { xs: 13, sm: 14, md: 16, lg: 19, xl: 24, xxl: 30 };

const floorOf = label => (String(label ?? "").match(/^(\d+)/)?.[1] ?? "");
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
const newRequestId = () => Array.from({ length: 16 }, () => Math.floor(Math.random() * 16).toString(16)).join("");

// What a knowledge card shows: the passage around a search match, otherwise the newest
// "###" note (notes are appended at the end, so the head of the file is the oldest part).
function excerpt(body, query) {
  const text = String(body ?? "");
  const parts = text.split(/^###\s+/m);
  const flat = s => s.replace(/\s+/g, " ").trim();
  if (query) {
    const at = text.toLowerCase().indexOf(query);
    if (at >= 0) return { heading: "", text: flat(text.slice(Math.max(0, at - 60), at + 200)) };
  }
  if (parts.length > 1) {
    const last = parts[parts.length - 1];
    const cut = last.indexOf("\n");
    return { heading: `최근: ${flat(cut < 0 ? last : last.slice(0, cut))}`, text: flat(cut < 0 ? "" : last.slice(cut)).slice(0, 220) };
  }
  return { heading: "", text: flat(text.split("\n").slice(1).join(" ")).slice(0, 220) };
}

// A staff member's state in one of the four colours.
function tone(e) {
  const p = e.progress ?? {};
  if (p.stalled) return { color: C.WARN, text: "멈춤 의심" };
  if (e.state === "working") return { color: C.OK, text: "작업 중" };
  if (e.state === "blocked") return { color: C.WARN, text: "확인 필요" };
  if (e.state === "done") return { color: C.ACCENT, text: "새 응답" };
  return { color: C.faint, text: "대기" };
}

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

function text(value, size = FS.md, color = C.text) {
  return div().text_size(size).text_color(color).child(String(value ?? ""));
}

function button(id, label, onClick, fill = null) {
  return div().id(id).px(14).py(7).rounded(8).cursor_pointer().flex_none().whitespace_nowrap()
    .text_size(FS.sm).font_semibold()
    .bg(fill ?? C.raised).text_color(fill ? "#ffffff" : C.text).border(1).border_color(fill ?? C.line)
    .hover(s => s.opacity(0.85))
    .on_click((_e, cx) => onClick(cx))
    .child(label);
}

function chip(id, label, active, onClick) {
  return div().id(id).px(12).py(6).rounded(8).cursor_pointer().flex_none().whitespace_nowrap()
    .text_size(FS.sm).font_semibold()
    .bg(active ? C.ACCENT : C.raised).text_color(active ? "#ffffff" : C.sub)
    .on_click((_e, cx) => onClick(cx))
    .child(label);
}

function pill(label, color) {
  return div().flex_none().px(8).py(2).rounded(999).border(1).border_color(color)
    .text_size(FS.xs).font_semibold().text_color(color).child(label);
}

function dot(color, size = 10) {
  return div().flex_none().size(size).rounded_full().bg(color);
}

function panel() {
  return v_flex().min_h(0).gap(12).p(16).rounded(12).bg(C.panel).border(1).border_color(C.line);
}

function panelTitle(title, note = "") {
  return h_flex().w_full().flex_none().items_center().justify_between()
    .child(div().font_bold().text_size(FS.lg).text_color(C.text).child(title))
    .child(text(note, FS.xs, C.faint));
}

function empty(message) {
  return div().p(12).text_size(FS.sm).text_color(C.faint).child(message);
}

function bar(done, total) {
  const pct = total ? Math.round((done / total) * 100) : 0;
  return div().w_full().h(6).rounded(999).bg(C.line)
    .child(div().h_full().rounded(999).bg(C.ACCENT).w(`${pct}%`));
}

// The checklist the way Claude's app draws it: done greyed and struck, current in accent.
function stepList(steps) {
  return v_flex().gap(6).children((steps?.items ?? []).map((s, i) => h_flex().id(`step-${i}`).gap(8).items_center()
    .child(dot(s.status === "completed" ? C.OK : s.status === "in_progress" ? C.ACCENT : C.line, 8))
    .child(s.status === "completed" ? div().text_size(FS.sm).text_color(C.faint).line_through().child(s.content)
      : s.status === "in_progress" ? div().text_size(FS.sm).font_semibold().text_color(C.ACCENT).child(s.active || s.content)
      : text(s.content, FS.sm, C.sub))));
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
    // One id per dialog: a retry after an unknown outcome reuses it, and the store keeps one copy.
    this.requestId = newRequestId();
    this.title = InputState.new({ placeholder: p.titleHint ?? "제목" });
    this.body = TextareaState.new({ placeholder: p.bodyHint ?? "내용", rows: 5 });
  }

  close() { closeOverlay(props().overlay); }

  async submit(cx, action) {
    if (this.busy) return;
    // Same inputs retry with the same id (stored once); edited inputs are a new request.
    const inputs = JSON.stringify([this.team, this.points, this.title.value(), this.body.value()]);
    if (inputs !== this.lastInputs) {
      if (this.lastInputs !== undefined) this.requestId = newRequestId();
      this.lastInputs = inputs;
    }
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

  teamPicker(withCommon) {
    const teams = (this.p.teams ?? []).filter(x => withCommon || x.id !== "common");
    return h_flex().flex_wrap().gap(6)
      .when(!withCommon, el => el.child(chip("team-none", "부서 없음", this.team === "", cx => { this.team = ""; cx.notify(); })))
      .children(teams.map(x => chip(`team-${x.id}`, x.label, this.team === x.id, cx => { this.team = x.id; cx.notify(); })));
  }

  field(label, element) {
    return v_flex().gap(6)
      .child(text(label, FS.sm, C.sub))
      .child(div().px(10).py(8).rounded(8).border(1).border_color(C.line).bg(C.raised)
        .child(element.w_full().text_size(FS.md).text_color(C.text)));
  }

  frame(heading, note, content, submitText, onSubmit) {
    return v_flex().size_full().p(20).gap(14).bg(C.panel).text_color(C.text)
      .child(h_flex().items_center().justify_between()
        .child(div().font_bold().text_size(FS.xl).child(heading))
        .child(button("dialog-close", "닫기", () => this.close())))
      .when(Boolean(note), el => el.child(text(note, FS.sm, C.sub)))
      .child(content)
      .when(Boolean(this.error), el => el.child(div().p(10).rounded(8).border(1).border_color(C.BAD)
        .text_size(FS.sm).text_color(C.BAD).child(this.error)))
      .child(h_flex().justify_end().gap(8)
        .child(button("dialog-cancel", "취소", () => this.close()))
        .child(button("dialog-submit", this.busy ? "저장 중..." : submitText, cx => cx.spawn(async cx => onSubmit(cx)), C.ACCENT)));
  }

  render(cx) {
    const p = this.p;
    if (p.role === "badge") return this.badge();
    if (p.role === "gotcha") return this.frame("새 주의사항", "부서 위키 문서 끝에 추가됩니다.",
      v_flex().gap(12).child(this.teamPicker(true)).child(this.field("제목", Input.new(this.title)))
        .child(this.field("내용", Textarea.new(this.body))),
      "위키에 저장", cx => this.submit(cx, config => bridge(config, ["knowledge-add", "--team", this.team || "common",
        "--title", this.title.value(), "--content", this.body.value(), "--request-id", this.requestId])));
    if (p.role === "bounty") return this.frame("현상금 걸기", "유휴 직원이 python3 -m teamkit.hq bounty-claim으로 맡습니다.",
      v_flex().gap(12).child(this.teamPicker(false)).child(this.field("난제", Input.new(this.title)))
        .child(this.field("재현 조건·완료 기준", Textarea.new(this.body)))
        .child(h_flex().gap(6).items_center().child(text("현상금", FS.sm, C.sub))
          .children([100, 300, 500, 1000].map(n => chip(`pts-${n}`, `${n}pt`, this.points === n, cx => { this.points = n; cx.notify(); })))),
      "현상금 걸기", cx => this.submit(cx, config => hqAction(config, { op: "bounty-add", title: this.title.value(),
        detail: this.body.value(), team: this.team, points: this.points, by: "사용자", request_id: this.requestId })));
    if (p.role === "coffee") return this.frame("커피타임 근황", "24시간 동안 커피타임에 보입니다.",
      v_flex().gap(12).child(this.teamPicker(false)).child(this.field("한 줄 근황", Input.new(this.title))),
      "남기기", cx => this.submit(cx, config => hqAction(config, { op: "coffee", text: this.title.value(), team: this.team, by: "사용자", request_id: this.requestId })));
    if (p.role === "resolve") return this.frame("현상금 해결 기록", p.item?.title ?? "",
      v_flex().gap(12).child(this.field("해결 내용과 근거", Textarea.new(this.body))),
      "해결로 기록", cx => this.submit(cx, config => hqAction(config, { op: "bounty-resolve", item_id: p.item.id,
        by: p.item.claimed_by || "사용자", note: this.body.value() })));
    if (p.role === "doc") return v_flex().size_full().min_h(0).p(20).gap(12).bg(C.panel).text_color(C.text)
      .child(h_flex().flex_none().items_center().justify_between()
        .child(div().font_bold().text_size(FS.xl).truncate().child(p.doc?.title ?? "문서"))
        .child(button("doc-close", "닫기", () => this.close())))
      .child(v_flex().flex_1().min_h(0).gap(8).overflow_y_scrollbar()
        .children(String(p.doc?.body ?? "").split("\n").map((line, i) => div().id(`doc-line-${i}`).text_size(FS.md)
          .text_color(line.startsWith("#") ? C.text : C.sub)
          .when(line.startsWith("#"), el => el.font_bold().pt(8))
          .child(line.replace(/^#+\s*/, "") || " "))));
    return empty(`알 수 없는 대화상자: ${p.role}`);
  }

  badge() {
    const e = this.p.employee ?? {};
    const pr = e.progress ?? {};
    const look = tone(e);
    const row = (label, value) => h_flex().justify_between().py(4)
      .child(text(label, FS.sm, C.sub))
      .child(div().text_size(FS.sm).font_semibold().text_color(C.text).child(value));
    return v_flex().size_full().min_h(0).p(20).gap(14).bg(C.panel).text_color(C.text)
      .child(h_flex().items_center().gap(14)
        .child(div().size(52).rounded_full().bg(C.raised).border(2).border_color(look.color).flex().items_center().justify_center()
          .child(div().font_bold().text_size(FS.xl).child(String(e.label ?? "?").slice(0, 1))))
        .child(v_flex().gap(4).flex_1().min_w(0)
          .child(div().font_bold().text_size(FS.xl).truncate().child(e.label ?? "직원"))
          .child(h_flex().gap(8).items_center().child(pill(look.text, look.color)).child(text(shortName(e.teamLabel), FS.sm, C.sub)))))
      .child(v_flex().gap(6).p(12).rounded(10).bg(C.raised)
        .child(text("지금 하는 일", FS.sm, C.sub))
        .child(pr.activity
          ? div().text_size(FS.md).font_semibold().child(`${pr.activity.label}${pr.activity.detail ? ` · ${pr.activity.detail}` : ""}`)
          : text(pr.linked ? "기록된 활동 없음" : "진행 기록 연결 안 됨", FS.md, C.faint))
        .when(Boolean(pr.activity?.at), el => el.child(text(ago(pr.activity.at), FS.xs, C.faint)))
        .when(Boolean(pr.steps), el => el.child(v_flex().gap(8).pt(6)
          .child(h_flex().justify_between().child(text(pr.steps.title || "단계", FS.sm, C.sub)).child(text(`${pr.steps.done}/${pr.steps.total}`, FS.sm, C.text)))
          .child(bar(pr.steps.done, pr.steps.total))
          .child(stepList(pr.steps)))))
      .child(v_flex().gap(2).px(4)
        .child(row("공급자", e.provider === "codex" ? "Codex" : "Claude Code"))
        .child(row("계정", e.account ?? "확인 안 됨"))
        .child(row("터미널", e.terminal ?? "미확인"))
        .child(row("신원", e.identity_verified ? "작업 소유권으로 확인됨" : "화면 표시 이름 기준")))
      .child(h_flex().justify_end().gap(8)
        .child(button("badge-close", "닫기", () => this.close()))
        .when(Boolean(e.terminal), el => el.child(button("badge-go", "터미널로 이동", () => {
          run("agent.focus", { terminal: e.terminal });
          this.close();
        }, C.ACCENT))));
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
    this.stale = false;
    this.lastOk = 0;
    this.tab = "progress";
    this.dept = "all";
    this.seenRev = shared(REV);
    this.search = InputState.new({ placeholder: "직원·부서·주의사항 검색" });
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
    // "team-operations.<tab>" (keys.json or `herdr-app do`) shows a tab; "refresh" reloads.
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
    const now = Date.now() / 1000;
    if (!error) this.lastOk = now;
    const stale = Boolean(this.lastOk) && now - this.lastOk > STALE_S;
    // Read times change on every call; only a new minute changes what is shown.
    const minute = new Set(["observed_at", "last_change", "at"]);
    const shown = d => JSON.stringify(d, (k, v) => minute.has(k) && typeof v === "number" ? Math.floor(v / 60) : v);
    const changed = this.loading || error !== this.error || stale !== this.stale || shown(data) !== shown(this.data);
    this.data = data;
    this.error = error;
    this.stale = stale;
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
    // Floors come from the space name ("01 · ..."), so renaming a space reorders the map.
    const floor = x => Number(floorOf(x.label) || 999);
    return (this.data?.roster?.teams ?? []).map(x => ({ id: x.id, label: x.label, space: x.space, employees: x.employees ?? [] }))
      .sort((a, b) => floor(a) - floor(b));
  }

  dialogTeams() {
    return [{ id: "common", label: "전사 공통" }, ...this.teams().map(x => ({ id: x.id, label: shortName(x.label) }))];
  }

  open(role, extra = {}, size = { width: 560, height: 500 }) {
    openOverlay({ mod: "team-operations", kind: "dialog", width: size.width, height: size.height,
      props: { role, teams: this.dialogTeams(), ...extra } });
  }

  openBadge(e, teamLabel) {
    this.open("badge", { employee: { ...e, teamLabel } }, { width: 520, height: 0.8 });
  }

  query() {
    return this.search.value().trim().toLowerCase();
  }

  matches(e, team) {
    const q = this.query();
    return !q || `${e.label} ${team.label} ${e.progress?.activity?.detail ?? ""}`.toLowerCase().includes(q);
  }

  render(cx) {
    if (this.dialog) return this.dialog.render(cx);
    const rev = shared(REV);
    if (rev !== this.seenRev) {
      this.seenRev = rev;
      cx.spawn(async cx => this.refresh(cx));
    }
    const teams = this.teams();
    const hq = this.data?.hq ?? {};
    const staff = teams.flatMap(x => x.employees);
    const working = staff.filter(e => e.state === "working").length;
    const stalled = staff.filter(e => e.progress?.stalled).length;
    const openBounties = (hq.bounties ?? []).filter(b => b.status !== "resolved").length;
    const pending = (hq.proposals ?? []).filter(x => x.status === "대기").length;
    const tabs = [["progress", "진행 현황"], ["floor", "부서"], ["goal", `목표·제안함${pending ? ` ${pending}` : ""}`],
      ["bounty", `현상금${openBounties ? ` ${openBounties}` : ""}`]];

    return v_flex().id("team-operations").size_full().min_h(0).p(20).gap(14).bg(C.bg).text_color(C.text)
      .child(h_flex().w_full().flex_none().items_center().justify_between()
        .child(h_flex().items_center().gap(14)
          .child(div().font_bold().text_size(FS.xxl).child("종합상황실"))
          .child(this.pulse()))
        .child(h_flex().gap(8)
          .child(button("open-gotcha", "+ 주의사항", () => this.open("gotcha", { titleHint: "예: SDK 변경 시 영향 점검" })))
          .child(button("refresh", "새로고침", cx => cx.spawn(async cx => this.refresh(cx))))))
      .when(Boolean(this.error), el => el.child(div().p(12).rounded(10).border(1).border_color(C.BAD)
        .text_size(FS.md).text_color(C.BAD).child(`조회 오류: ${this.error}`)))
      .child(h_flex().w_full().flex_none().gap(12)
        .child(this.kpi("작업 중", `${working}`, `전체 직원 ${staff.length}명`, working ? C.OK : C.text))
        .child(this.kpi("멈춤 의심", `${stalled}`, "작업 중인데 10분 넘게 기록 변화 없음", stalled ? C.WARN : C.text))
        .child(this.kpi("대기 제안", `${pending}`, `전체 ${(hq.proposals ?? []).length}건`, pending ? C.ACCENT : C.text))
        .child(this.kpi("열린 현상금", `${openBounties}`, `해결 ${(hq.bounties ?? []).length - openBounties}건`, openBounties ? C.ACCENT : C.text)))
      .child(this.coffee(hq))
      .child(h_flex().w_full().flex_none().items_center().justify_between().gap(12)
        .child(h_flex().gap(6).children(tabs.map(([id, label]) => chip(`tab-${id}`, label, this.tab === id, cx => { this.tab = id; cx.notify(); }))))
        .child(div().w(320).h(38).px(12).flex().items_center().rounded(8).border(1).border_color(C.line).bg(C.panel)
          .child(Input.new(this.search).w_full().text_size(FS.md).text_color(C.text))))
      // items_stretch: a row centres its children by default, which left the
      // columns content-high and their flex_1 lists at zero height.
      .child(h_flex().w_full().flex_1().min_h(0).items_stretch().gap(14)
        .child(v_flex().flex_1().min_w(0).h_full().min_h(0)
          .child(this.tab === "goal" ? this.goalTab(hq) : this.tab === "bounty" ? this.bountyTab(hq)
            : this.tab === "floor" ? this.floorTab(teams) : this.progressTab(teams)))
        .child(v_flex().w(440).flex_none().h_full().min_h(0)
          .child(this.knowledge(hq.tip))));
  }

  // Live means the last read succeeded within STALE_S; the dot pulses natively (no JS timer).
  pulse() {
    const state = this.loading ? "loading" : this.error ? "error" : this.stale ? "stale" : "live";
    const look = { live: [C.OK, "실시간"], stale: [C.WARN, "갱신 지연"], error: [C.BAD, "조회 오류"], loading: [C.faint, "불러오는 중"] }[state];
    const mark = div().id(`pulse-${state}`).size(12).rounded_full().bg(look[0]);
    return h_flex().items_center().gap(8).px(12).py(6).rounded(999).bg(C.panel).border(1).border_color(C.line)
      .child(state === "live" ? mark.loop("opacity", { from: 0.25, to: 1, period: 1600 }) : mark)
      .child(div().text_size(FS.md).font_semibold().text_color(look[0]).child(look[1]))
      .when(Boolean(this.data?.roster?.observed_at), el => el.child(text(`관측 ${clock(this.data.roster.observed_at)}`, FS.sm, C.sub)));
  }

  kpi(label, value, note, color) {
    return v_flex().flex_1().min_w(0).p(16).gap(4).rounded(12).bg(C.panel).border(1).border_color(C.line)
      .child(text(label, FS.sm, C.sub))
      .child(div().font_bold().text_size(FS.xxl).text_color(color).child(value))
      .child(div().text_size(FS.xs).text_color(C.faint).truncate().child(note));
  }

  // One line under the KPIs: the latest notes from the teams, like a status ticker.
  coffee(hq) {
    const notes = (hq.coffee ?? []).slice(0, 3);
    return h_flex().w_full().flex_none().items_center().gap(14).px(16).py(10).rounded(12).bg(C.panel).border(1).border_color(C.line)
      .child(div().flex_none().text_size(FS.md).font_bold().child("커피타임"))
      .child(h_flex().flex_1().min_w(0).gap(20).items_center()
        .when(!notes.length, el => el.child(text("최근 24시간 동안 올라온 근황이 없습니다.", FS.sm, C.faint)))
        .children(notes.map(n => h_flex().id(`coffee-${n.id}`).min_w(0).flex_shrink(1).gap(8).items_center()
          .child(div().min_w(0).text_size(FS.sm).truncate().child(`${n.by}: ${n.text}`))
          .child(text(ago(n.at), FS.xs, C.faint)))))
      .child(button("open-coffee", "+ 근황", () => this.open("coffee", { titleHint: "예: NDK 빌드 정리 끝, 오후엔 문서 작업" }, { width: 540, height: 340 })));
  }

  // Who is doing what right now: stalls first, then working, then the most recent.
  progressTab(teams) {
    const rows = teams.flatMap(team => team.employees.filter(e => this.matches(e, team)).map(e => ({ e, team })));
    const rank = ({ e }) => (e.progress?.stalled ? 0 : e.state === "working" ? 1 : e.state === "blocked" ? 2 : 3);
    const recent = ({ e }) => -(e.progress?.last_change ?? 0);
    // Recorded steps count even when the transcript is not linked (agent-progress works without it).
    const shown = r => r.e.progress?.linked || Boolean(r.e.progress?.steps);
    const linked = rows.filter(shown).sort((a, b) => rank(a) - rank(b) || recent(a) - recent(b));
    const unlinked = rows.filter(r => !shown(r));
    return panel().size_full()
      .child(panelTitle("진행 현황", "직원을 누르면 단계와 상세"))
      .child(v_flex().flex_1().min_h(0).gap(8).overflow_y_scrollbar()
        .when(!rows.length, el => el.child(empty(this.loading ? "불러오는 중..." : "표시할 직원이 없습니다.")))
        .children(linked.map(({ e, team }) => this.progressRow(e, team)))
        .when(unlinked.length > 0, el => el.child(v_flex().gap(4).pt(8)
          .child(text(`진행 기록 연결 안 됨 ${unlinked.length}명`, FS.sm, C.sub))
          .child(h_flex().flex_wrap().gap(6).children(unlinked.map(({ e, team }) => div().id(`unlinked-${e.terminal}`)
            .px(8).py(3).rounded(6).bg(C.raised).cursor_pointer().text_size(FS.xs).text_color(C.sub)
            .on_click(() => this.openBadge(e, team.label)).child(e.label)))))));
  }

  progressRow(e, team) {
    const pr = e.progress ?? {};
    const look = tone(e);
    const act = pr.activity;
    return v_flex().id(`row-${e.terminal}`).w_full().gap(8).p(14).rounded(10).bg(C.raised).cursor_pointer()
      .border(1).border_color(pr.stalled ? C.WARN : C.raised)
      .hover(s => s.opacity(0.9))
      .on_click(() => this.openBadge(e, team.label))
      .child(h_flex().w_full().items_center().gap(10)
        .child(e.state === "working" && !pr.stalled ? dot(look.color).loop("opacity", { from: 0.35, to: 1, period: 1400 }) : dot(look.color))
        .child(div().font_bold().text_size(FS.md).flex_none().child(e.label))
        .child(text(shortName(team.label), FS.sm, C.faint))
        .child(div().flex_1())
        .child(pill(look.text, look.color))
        .child(text(ago(pr.last_change), FS.sm, C.sub)))
      .child(div().text_size(FS.md).text_color(act ? C.text : C.faint).truncate()
        .child(act ? `${act.label}${act.detail ? ` · ${act.detail}` : ""}` : "기록된 활동 없음"))
      .when(Boolean(pr.steps), el => el.child(v_flex().gap(6)
        .child(h_flex().gap(10).items_center()
          .child(text(`${pr.steps.done}/${pr.steps.total}`, FS.sm, C.text))
          .child(div().flex_1().min_w(0).text_size(FS.sm).text_color(C.ACCENT).truncate().child(pr.steps.current ? `지금: ${pr.steps.current}` : "")))
        .child(bar(pr.steps.done, pr.steps.total))));
  }

  floorTab(teams) {
    const shown = teams.filter(x => this.dept === "all" || x.id === this.dept)
      .map(team => ({ ...team, employees: team.employees.filter(e => this.matches(e, team)) }))
      .filter(team => team.employees.length || !this.query());
    return panel().size_full()
      .child(panelTitle("부서", "직원을 누르면 사원증"))
      .child(h_flex().flex_none().flex_wrap().gap(6)
        .child(chip("dept-all", "전체", this.dept === "all", cx => { this.dept = "all"; cx.notify(); }))
        .children(teams.map(x => chip(`dept-${x.id}`, `${floorOf(x.label) || "-"} ${shortName(x.label)}`, this.dept === x.id, cx => { this.dept = x.id; cx.notify(); }))))
      .child(v_flex().flex_1().min_h(0).gap(10).overflow_y_scrollbar()
        .when(!shown.length, el => el.child(empty(this.loading ? "불러오는 중..." : "표시할 부서가 없습니다.")))
        .children(shown.map(team => this.deptCard(team))));
  }

  deptCard(team) {
    const busy = team.employees.filter(e => e.state === "working").length;
    const stalled = team.employees.filter(e => e.progress?.stalled).length;
    return v_flex().id(`dept-card-${team.id}`).w_full().p(14).gap(10).rounded(10).bg(C.raised)
      .child(h_flex().w_full().items_center().gap(12)
        .child(div().font_bold().text_size(FS.xl).text_color(C.sub).w(40).child(floorOf(team.label) || "-"))
        .child(v_flex().flex_1().min_w(0)
          .child(div().font_bold().text_size(FS.lg).truncate().child(shortName(team.label)))
          .child(h_flex().gap(8).items_center()
            .child(text(`${team.employees.length}명`, FS.sm, C.sub))
            .when(busy > 0, el => el.child(pill(`작업 중 ${busy}`, C.OK)))
            .when(stalled > 0, el => el.child(pill(`멈춤 의심 ${stalled}`, C.WARN)))))
        .when(Boolean(team.space), el => el.child(button(`enter-${team.id}`, "스페이스로 이동", () => run("space.select", { id: team.space })))))
      .child(v_flex().gap(4)
        .when(!team.employees.length, el => el.child(text("열린 세션 없음", FS.sm, C.faint)))
        .children(team.employees.map(e => {
          const look = tone(e);
          const act = e.progress?.activity;
          return h_flex().id(`emp-${e.terminal}`).w_full().items_center().gap(10).px(10).py(8).rounded(8).cursor_pointer()
            .hover(s => s.bg(C.panel))
            .on_click(() => this.openBadge(e, team.label))
            .child(dot(look.color, 8))
            .child(div().text_size(FS.md).font_semibold().flex_none().child(e.label))
            .child(div().flex_1().min_w(0).text_size(FS.sm).text_color(C.sub).truncate()
              .child(act ? `${act.label}${act.detail ? ` · ${act.detail}` : ""}` : look.text))
            .child(text(e.provider === "codex" ? "Codex" : "Claude", FS.xs, C.faint));
        })));
  }

  goalTab(hq) {
    const goal = hq.goal;
    const proposals = hq.proposals ?? [];
    const table = (rows, cols) => v_flex().gap(4)
      .child(h_flex().gap(12).pb(4).border_b(1).border_color(C.line)
        .children(cols.map(c => div().flex_1().min_w(0).text_size(FS.xs).text_color(C.faint).child(c))))
      .children(rows.map((r, i) => h_flex().id(`row-${i}-${cols[0]}`).gap(12).py(4)
        .children(cols.map(c => div().flex_1().min_w(0).text_size(FS.sm).text_color(String(r[c]) === "미확인" ? C.WARN : C.text)
          .child(String(r[c] ?? ""))))));
    return v_flex().size_full().min_h(0).gap(14)
      .child(panel().flex_none()
        .child(panelTitle(goal ? goal.name : "공통 목표", goal ? `정본 ${ago(goal.updated_at)} 수정` : ""))
        .when(!goal, el => el.child(empty("목표 브리프가 아직 없습니다. goal-setting 스킬로 합의해 지식 폴더의 GOAL-BRIEF.md로 저장하세요.")))
        .when(Boolean(goal), el => el
          .child(div().text_size(FS.lg).font_semibold().child(goal.sentence || "목표 문장 없음"))
          .when(goal.metrics.length > 0, el => el.child(text("성공 지표", FS.sm, C.sub)).child(table(goal.metrics, ["지표", "기준선", "목표", "기한"])))
          .when(goal.levers.length > 0, el => el.child(text("레버", FS.sm, C.sub)).child(table(goal.levers, ["입력 지표", "담당 (사람/에이전트)"])))
          .when(goal.guardrails.length > 0, el => el.child(text("가드레일", FS.sm, C.sub)).child(table(goal.guardrails, ["지표", "허용 한계", "넘으면 할 일"])))
          .when(goal.donts.length > 0, el => el.child(div().text_size(FS.sm).text_color(C.BAD).child(`하지 말 것: ${goal.donts.join(" · ")}`)))))
      .child(panel().flex_1()
        .child(panelTitle(`제안함 ${proposals.length}`, "에이전트: python3 -m teamkit.hq propose"))
        .child(v_flex().flex_1().min_h(0).gap(8).overflow_y_scrollbar()
          .when(!proposals.length, el => el.child(empty("아직 제안이 없습니다.")))
          .children(proposals.map(x => this.proposalCard(x)))));
  }

  proposalCard(x) {
    const color = x.status === "채택" ? C.OK : x.status === "보류" ? C.faint : C.ACCENT;
    return v_flex().id(`proposal-${x.id}`).w_full().p(14).gap(6).rounded(10).bg(C.raised)
      .child(h_flex().items_center().gap(10)
        .child(pill(x.status, color))
        .child(div().flex_1().min_w(0).font_bold().text_size(FS.md).truncate().child(x.title))
        .child(text(`${x.proposed_by} · ${ago(x.created_at)}`, FS.xs, C.faint)))
      .when(Boolean(x.metric), el => el.child(text(`기여 지표: ${x.metric}`, FS.sm, C.sub)))
      .child(text(x.content, FS.md, C.text))
      .when(Boolean(x.hypothesis), el => el.child(text(`가설: ${x.hypothesis}`, FS.sm, C.sub)))
      .when(x.status === "대기", el => el.child(h_flex().justify_end().gap(8)
        .child(button(`hold-${x.id}`, "보류", cx => cx.spawn(async cx => this.act(cx, { op: "proposal-decide", item_id: x.id, status: "보류" }))))
        .child(button(`adopt-${x.id}`, "채택", cx => cx.spawn(async cx => this.act(cx, { op: "proposal-decide", item_id: x.id, status: "채택" })), C.ACCENT))));
  }

  bountyTab(hq) {
    const all = hq.bounties ?? [];
    const open = all.filter(b => b.status !== "resolved");
    const done = all.filter(b => b.status === "resolved").sort((a, b) => (b.resolved_at ?? 0) - (a.resolved_at ?? 0));
    const hunters = {};
    for (const b of done) hunters[b.resolved_by] = (hunters[b.resolved_by] ?? 0) + (b.points ?? 0);
    const ranking = Object.entries(hunters).sort((a, b) => b[1] - a[1]).slice(0, 5);
    return h_flex().size_full().min_h(0).items_stretch().gap(14)
      .child(panel().flex_1().min_w(0)
        .child(h_flex().w_full().flex_none().items_center().justify_between()
          .child(div().font_bold().text_size(FS.lg).child(`열린 현상금 ${open.length}`))
          .child(button("open-bounty", "+ 현상금 걸기", () => this.open("bounty", { titleHint: "예: test-macbook 간헐 타임아웃 원인 규명" }), C.ACCENT)))
        .child(text("에이전트: python3 -m teamkit.hq bounty-claim <id> --by <이름>", FS.xs, C.faint))
        .child(v_flex().flex_1().min_h(0).gap(8).overflow_y_scrollbar()
          .when(!open.length, el => el.child(empty("열린 현상금이 없습니다.")))
          .children(open.map(b => this.bountyCard(b)))))
      .child(panel().w(280).flex_none()
        .child(panelTitle("명예의 전당"))
        .when(!ranking.length, el => el.child(empty("해결 기록이 아직 없습니다.")))
        .children(ranking.map(([name, pts], i) => h_flex().id(`hunter-${i}`).justify_between().py(4)
          .child(div().text_size(FS.md).font_semibold().child(`${i + 1}. ${name}`))
          .child(text(`${pts}pt`, FS.md, C.OK))))
        .child(text("최근 해결", FS.sm, C.sub))
        .child(v_flex().flex_1().min_h(0).gap(6).overflow_y_scrollbar()
          .children(done.slice(0, 10).map(b => v_flex().id(`done-${b.id}`).py(4)
            .child(div().text_size(FS.sm).font_semibold().truncate().child(b.title))
            .child(div().text_size(FS.xs).text_color(C.faint).truncate().child(`${b.resolved_by} · ${b.resolution}`))))));
  }

  bountyCard(b) {
    const claimed = b.status === "claimed";
    return v_flex().id(`bounty-${b.id}`).w_full().p(14).gap(6).rounded(10).bg(C.raised)
      .child(h_flex().items_center().gap(10)
        .child(pill(claimed ? "진행 중" : "대기", claimed ? C.WARN : C.ACCENT))
        .child(div().flex_1().min_w(0).font_bold().text_size(FS.md).truncate().child(b.title))
        .child(div().text_size(FS.md).font_bold().child(`${b.points}pt`)))
      .when(Boolean(b.detail), el => el.child(text(b.detail, FS.sm, C.sub)))
      .child(h_flex().items_center().justify_between()
        .child(text(claimed ? `담당 ${b.claimed_by} · ${ago(b.claimed_at)}` : `id ${b.id} · ${ago(b.created_at)}`, FS.xs, C.faint))
        .when(claimed, el => el.child(h_flex().gap(8)
          .child(button(`release-${b.id}`, "내려놓기", cx => cx.spawn(async cx => this.act(cx, { op: "bounty-release", item_id: b.id }))))
          .child(button(`resolve-${b.id}`, "해결 기록", () => this.open("resolve", { item: b, bodyHint: "원인, 수정, 확인한 근거" }), C.ACCENT)))));
  }

  knowledge(tip) {
    const q = this.query();
    const items = (this.data?.knowledge ?? []).filter(x => !q || `${x.team} ${x.title} ${x.body}`.toLowerCase().includes(q));
    return panel().size_full()
      .child(panelTitle(`지식 허브 ${items.length}`))
      .when(Boolean(tip), el => el.child(v_flex().flex_none().gap(4).p(12).rounded(10).bg(C.raised).border_l(3).border_color(C.ACCENT)
        .child(text(`오늘의 주의사항 · ${tip.team}`, FS.xs, C.sub))
        .child(div().text_size(FS.md).font_semibold().child(tip.title))
        .child(text(tip.body.slice(0, 200), FS.sm, C.sub))))
      .child(v_flex().flex_1().min_h(0).gap(8).overflow_y_scrollbar()
        .when(!items.length, el => el.child(empty(q ? "검색 결과가 없습니다." : "지식 문서가 없습니다.")))
        .children(items.map(x => {
          const shown = excerpt(x.body, q);
          return v_flex().id(`kn-${x.team}`).w_full().p(12).gap(4).rounded(10).bg(C.raised).cursor_pointer()
            .hover(s => s.opacity(0.9))
            .on_click(() => this.open("doc", { doc: { title: x.title, body: x.body } }, { width: 0.6, height: 0.75 }))
            .child(div().font_bold().text_size(FS.md).truncate().child(x.title))
            .when(Boolean(shown.heading), el => el.child(div().text_size(FS.sm).text_color(C.ACCENT).truncate().child(shown.heading)))
            .child(text(shown.text, FS.sm, C.sub));
        })));
  }
}
