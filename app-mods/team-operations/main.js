import { View, div } from "gpui-kit";
import { v_flex, h_flex, InputState, Input } from "gpui-base";
import { appHome, readTextFile, exec, theme, run } from "herdr";

const DEPT_METADATA = {
  operations: { code: "00", label: "00 · 종합상황실", color: "#06b6d4", floor: "00F" },
  eversafe:   { code: "10", label: "10 · Eversafe 개발팀", color: "#8b5cf6", floor: "10F" },
  fakefinder: { code: "11", label: "11 · FakeFinder 개발팀", color: "#10b981", floor: "11F" },
  server:     { code: "12", label: "12 · 서버 개발팀", color: "#f43f5e", floor: "12F" },
  codemix:    { code: "13", label: "13 · Codemix 개발팀", color: "#0ea5e9", floor: "13F" },
  qa:         { code: "20", label: "20 · 품질보증실", color: "#f59e0b", floor: "20F" },
  docs:       { code: "30", label: "30 · 기술문서실", color: "#6366f1", floor: "30F" },
  infra:      { code: "40", label: "40 · 개발기반실", color: "#f97316", floor: "40F" },
  common:     { code: "전사", label: "전사 공통", color: "#94a3b8", floor: "ALL" }
};

export default class TeamOperations extends View {
  init(_props, cx) {
    this.data = null;
    this.error = null;
    this.busy = false;
    this.config = null;
    this.pulse = true;
    this.filterDept = "all";
    this.selectedEmployee = null;
    this.addModalOpen = false;
    this.newGotchaDept = "common";
    this.nowString = this.formatTime();

    this.searchInput = InputState.new({ placeholder: "주의사항 · 위키 · 직원 실시간 검색..." });
    this.newTitleInput = InputState.new({ placeholder: "주의사항 제목 (예: SDK 변경 시 영향 점검)" });
    this.newContentInput = InputState.new({ placeholder: "상세 주의사항 및 원칙 본문을 입력하세요..." });

    this.searchInput.on("change", (_e, cx) => cx.notify());
    this.newTitleInput.on("change", (_e, cx) => cx.notify());
    this.newContentInput.on("change", (_e, cx) => cx.notify());

    cx.spawn(async cx => {
      try {
        const raw = await readTextFile(appHome() + "/mods/team-operations/local.json");
        this.config = JSON.parse(raw);
        await this.refresh(cx);
      } catch (error) {
        this.error = String(error);
        cx.notify();
      }
    });

    cx.timer.every(1000, cx => {
      this.pulse = !this.pulse;
      this.nowString = this.formatTime();
      cx.notify();
    });

    cx.timer.every(15000, cx => cx.spawn(async cx => this.refresh(cx)));
  }

  formatTime() {
    const d = new Date();
    const pad = n => String(n).padStart(2, "0");
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())} KST`;
  }

  async refresh(cx) {
    if (!this.config || this.busy) return;
    this.busy = true;
    try {
      const c = this.config;
      const result = await exec(c.python, [
        c.script,
        "--config", c.config,
        "--state", c.state,
        "--bindings", c.bindings,
        "knowledge-hub"
      ]);
      if (result.code !== 0) throw new Error(result.stderr || "종합상황실 데이터 조회 실패");
      this.data = JSON.parse(result.stdout);
      this.error = null;
    } catch (error) {
      this.error = String(error);
    } finally {
      this.busy = false;
      cx.notify();
    }
  }

  async submitNewGotcha(cx) {
    const title = this.newTitleInput.value().trim();
    const content = this.newContentInput.value().trim();
    if (!title) return;
    this.busy = true;
    try {
      const c = this.config;
      const result = await exec(c.python, [
        c.script,
        "knowledge-add",
        "--team", this.newGotchaDept,
        "--title", title,
        "--content", content
      ]);
      if (result.code !== 0) throw new Error(result.stderr || "주의사항 추가 실패");
      this.newTitleInput.set_value("");
      this.newContentInput.set_value("");
      this.addModalOpen = false;
      await this.refresh(cx);
    } catch (error) {
      this.error = String(error);
    } finally {
      this.busy = false;
      cx.notify();
    }
  }

  render(cx) {
    const t = theme();
    const roster = this.data?.roster;
    const teams = roster?.teams ?? [];
    const knowledge = this.data?.knowledge ?? [];

    const searchQuery = this.searchInput.value().trim().toLowerCase();

    let totalStaff = 0;
    let workingStaff = 0;
    let idleStaff = 0;
    for (const team of teams) {
      for (const emp of (team.employees ?? [])) {
        totalStaff += 1;
        if (emp.state === "working") workingStaff += 1;
        else idleStaff += 1;
      }
    }

    const filteredTeams = teams.filter(team => {
      if (this.filterDept !== "all" && team.id !== this.filterDept) return false;
      if (!searchQuery) return true;
      const matchName = String(team.label || "").toLowerCase().includes(searchQuery);
      const matchEmp = (team.employees ?? []).some(e => String(e.label || "").toLowerCase().includes(searchQuery));
      return matchName || matchEmp;
    });

    const filteredKnowledge = knowledge.filter(item => {
      if (this.filterDept !== "all" && item.team !== this.filterDept) return false;
      if (!searchQuery) return true;
      const matchTitle = String(item.title || "").toLowerCase().includes(searchQuery);
      const matchBody = String(item.body || "").toLowerCase().includes(searchQuery);
      const matchTeam = String(item.team || "").toLowerCase().includes(searchQuery);
      return matchTitle || matchBody || matchTeam;
    });

    const field = state => div().h(30).px(8).flex().items_center().rounded(6)
      .border(1).border_color(t.border).bg(t.input ?? t.raised)
      .child(Input.new(state).w_full().text_size(12).text_color(t.foreground));

    const btn = (id, text, onClick, active = false, customColor = null) =>
      div().id(id).px(10).py(5).rounded(6).cursor_pointer().flex_none().whitespace_nowrap()
        .text_size(12).font_semibold()
        .bg(active ? (customColor ?? t.accent) : t.raised)
        .text_color(active ? "#ffffff" : t.foreground)
        .hover(s => s.opacity(0.85))
        .on_click((_e, cx) => onClick(cx))
        .child(text);

    return v_flex().id("team-operations").size_full().min_h(0).p(14).gap(12)
      .bg(t.background).text_color(t.foreground).relative()
      // 1. Header Bar
      .child(h_flex().w_full().flex_none().items_center().justify_between().pb(4).border_b(1).border_color(t.border)
        .child(h_flex().items_center().gap(10)
          .child(div().text_size(20).child("🏢"))
          .child(v_flex().gap(1)
            .child(h_flex().items_center().gap(8)
              .child(div().font_bold().text_size(16).child("가상 사옥 종합상황실"))
              .child(div().px(6).py(1).rounded(4).bg(t.raised).text_size(10).text_color(t.muted).child("HQ v2.0")))
            .child(div().text_size(11).text_color(t.muted).child("Knowledge Hub & Virtual Office Operations"))))
        .child(h_flex().items_center().gap(12)
          .child(h_flex().items_center().gap(6).px(10).py(4).rounded(6).bg(t.surface).border(1).border_color(t.border)
            .child(div().size(8).rounded_full().bg(this.pulse ? "#10b981" : "#047857"))
            .child(div().text_size(11).font_semibold().text_color("#10b981").child("● LIVE PULSE 72bpm"))
            .child(div().text_size(11).text_color(t.muted).child(`· ${this.nowString}`)))
          .child(btn("btn-add-gotcha", "+ 새 주의사항", cx => { this.addModalOpen = true; cx.notify(); }, true, "#10b981"))
          .child(btn("btn-refresh", this.busy ? "조회 중..." : "새로고침 ↻", cx => cx.spawn(async cx => this.refresh(cx))))))
      // Error alert if any
      .when(Boolean(this.error), el => el.child(div().p(8).rounded(6).bg("#450a0a").border(1).border_color("#b91c1c").text_size(12).text_color("#fca5a5")
        .child(`조회 경고: ${this.error}`)))
      // 2. Executive KPI Strip
      .child(h_flex().w_full().flex_none().gap(8)
        .child(this.renderKpiCard("전사 임직원", `${totalStaff}명`, `가동 ${workingStaff}명 · 대기 ${idleStaff}명`, "#3b82f6", t))
        .child(this.renderKpiCard("부서 네트워크", `${teams.length}개 부서`, "00·10·11·12·13·20·30·40F 전원 상주", "#10b981", t))
        .child(this.renderKpiCard("통합 주의사항", `${knowledge.length}건`, "팀별 규정 및 과거 검증 교훈", "#f59e0b", t))
        .child(this.renderKpiCard("소프트웨어 공학", "SOLID · YAGNI", "Ponytail(간결화) & Pstack(증거검증)", "#8b5cf6", t)))
      // 3. Search and Department Filters
      .child(h_flex().w_full().flex_none().items_center().justify_between().gap(8)
        .child(h_flex().items_center().flex_wrap().gap(4)
          .child(btn("filter-all", "전체 부서", cx => { this.filterDept = "all"; cx.notify(); }, this.filterDept === "all"))
          .children(teams.map(team => {
            const meta = DEPT_METADATA[team.id] ?? { color: "#64748b", floor: "F" };
            return btn(`filter-${team.id}`, `${meta.floor} ${team.label.split("·")[1]?.trim() || team.label}`, cx => {
              this.filterDept = team.id;
              cx.notify();
            }, this.filterDept === team.id, meta.color);
          })))
        .child(div().w(260).child(field(this.searchInput))))
      // 4. Main Body: Floor Map (Left) + Gotchas & Knowledge Feed (Right)
      .child(h_flex().w_full().flex_1().min_h(0).gap(12)
        // Left Column: Virtual Office Floor Map
        .child(v_flex().flex_1().min_w(0).min_h(0).gap(8).p(10).rounded(8).bg(t.surface).border(1).border_color(t.border)
          .child(h_flex().items_center().justify_between().flex_none().pb(4).border_b(1).border_color(t.border)
            .child(h_flex().items_center().gap(6)
              .child(div().font_bold().text_size(13).child("가상 사옥 층별 부서 현황 (Virtual Floor Map)"))
              .child(div().text_size(11).text_color(t.muted).child(`(${filteredTeams.length}개 부서)`)))
            .child(div().text_size(10).text_color(t.muted).child("직원 칩 클릭: 사원증 | 사무실 입장: 해당 스페이스 이동")))
          .child(v_flex().flex_1().min_h(0).gap(8).overflow_y_scrollbar()
            .children(filteredTeams.map(team => this.renderDeptCard(team, cx, t)))))
        // Right Column: Live Knowledge & Gotchas Feed
        .child(v_flex().w(380).min_h(0).gap(8).p(10).rounded(8).bg(t.surface).border(1).border_color(t.border)
          .child(h_flex().items_center().justify_between().flex_none().pb(4).border_b(1).border_color(t.border)
            .child(h_flex().items_center().gap(6)
              .child(div().font_bold().text_size(13).child("전사 지식 허브 & 팀별 주의사항"))
              .child(div().text_size(11).text_color(t.muted).child(`(${filteredKnowledge.length}건)`)))
            .child(div().text_size(10).text_color(t.muted).child("원문 Wiki 연동")))
          .child(v_flex().flex_1().min_h(0).gap(8).overflow_y_scrollbar()
            .children(filteredKnowledge.map(item => this.renderGotchaCard(item, t))))))
      // 5. Add Gotcha Modal (Inline Overlay)
      .when(this.addModalOpen, el => el.child(this.renderAddModal(cx, t)))
      // 6. Employee ID Badge Card Modal (Inline Overlay)
      .when(Boolean(this.selectedEmployee), el => el.child(this.renderEmployeeModal(cx, t)));
  }

  renderKpiCard(label, mainVal, subVal, accentColor, t) {
    return v_flex().flex_1().p(8).gap(3).rounded(8).bg(t.surface).border(1).border_color(t.border)
      .child(h_flex().items_center().justify_between()
        .child(div().text_size(11).text_color(t.muted).child(label))
        .child(div().size(6).rounded_full().bg(accentColor)))
      .child(div().font_bold().text_size(16).text_color(accentColor).child(mainVal))
      .child(div().text_size(10).text_color(t.muted).child(subVal));
  }

  renderDeptCard(team, cx, t) {
    const meta = DEPT_METADATA[team.id] ?? { color: "#64748b", floor: "F", label: team.label };
    const employees = team.employees ?? [];
    const isLive = employees.length > 0;
    const workingCount = employees.filter(e => e.state === "working").length;

    return v_flex().key(team.id).w_full().p(8).gap(6).rounded(6).bg(t.raised).border(1).border_color(t.border)
      // Department Header
      .child(h_flex().w_full().items_center().justify_between()
        .child(h_flex().items_center().gap(6)
          .child(div().px(5).py(1).rounded(4).bg(meta.color).text_size(10).font_bold().text_color("#ffffff").child(meta.floor))
          .child(div().font_bold().text_size(12).text_color(t.foreground).child(team.label))
          .child(div().size(6).rounded_full().bg(workingCount > 0 ? "#10b981" : (isLive ? "#64748b" : "#ef4444")))
          .child(div().text_size(10).text_color(t.muted).child(`${employees.length}명 상주${workingCount > 0 ? ` (작업 중 ${workingCount})` : ""}`)))
        .child(h_flex().items_center().gap(4)
          .when(Boolean(team.space), el => el.child(div().px(8).py(3).rounded(4).bg(t.surface).border(1).border_color(t.border)
            .cursor_pointer().text_size(10).font_semibold().text_color(meta.color)
            .hover(s => s.opacity(0.8))
            .on_click(() => run("space.select", { id: team.space }))
            .child("사무실 입장 ↗")))))
      // Employees list
      .child(h_flex().w_full().flex_wrap().gap(4)
        .children(employees.map(emp => {
          const isWorking = emp.state === "working";
          const providerColor = emp.provider === "codex" ? "#059669" : "#7c3aed";
          return h_flex().key(emp.terminal).items_center().gap(4).px(6).py(3).rounded(4)
            .bg(t.surface).border(1).border_color(isWorking ? "#10b981" : t.border)
            .cursor_pointer()
            .hover(s => s.opacity(0.85))
            .on_click((_e, cx) => {
              this.selectedEmployee = { ...emp, teamLabel: team.label, teamId: team.id };
              cx.notify();
            })
            .child(div().size(5).rounded_full().bg(isWorking ? "#10b981" : "#94a3b8"))
            .child(div().text_size(11).font_semibold().text_color(t.foreground).child(emp.label))
            .child(div().px(3).py(0.5).rounded(3).bg(providerColor).text_size(8).text_color("#ffffff")
              .child(emp.provider === "codex" ? "Codex" : "Claude"));
        })));
  }

  renderGotchaCard(item, t) {
    const meta = DEPT_METADATA[item.team] ?? { color: "#94a3b8", floor: "전사", label: item.team };
    const dateStr = item.updated_at ? new Date(item.updated_at * 1000).toLocaleDateString("ko-KR") : "";

    return v_flex().key(item.title).w_full().p(8).gap(4).rounded(6).bg(t.raised).border(1).border_color(t.border)
      .child(h_flex().items_center().justify_between()
        .child(h_flex().items_center().gap(6)
          .child(div().px(4).py(1).rounded(3).bg(meta.color).text_size(9).font_bold().text_color("#ffffff").child(meta.floor))
          .child(div().font_bold().text_size(11).text_color(t.foreground).child(item.title)))
        .child(div().text_size(9).text_color(t.muted).child(dateStr)))
      .child(div().text_size(10).text_color(t.muted).truncate().child(`출처: ${item.source}`))
      .child(div().w_full().p(6).rounded(4).bg(t.surface).text_size(11).text_color(t.foreground).whitespace_normal()
        .child(item.body.split("\n\n").slice(1).join("\n\n").trim() || item.body));
  }

  renderAddModal(cx, t) {
    const depts = [
      { id: "common", label: "전사 공통" },
      { id: "operations", label: "00 종합상황실" },
      { id: "eversafe", label: "10 Eversafe" },
      { id: "fakefinder", label: "11 FakeFinder" },
      { id: "server", label: "12 서버" },
      { id: "codemix", label: "13 Codemix" },
      { id: "qa", label: "20 품질보증" },
      { id: "docs", label: "30 기술문서" },
      { id: "infra", label: "40 개발기반" }
    ];

    return div().absolute().size_full().top(0).left(0).bg("#000000bb").flex().items_center().justify_center()
      .child(v_flex().w(520).p(16).gap(12).rounded(10).bg(t.surface).border(1).border_color(t.border)
        .child(h_flex().items_center().justify_between()
          .child(div().font_bold().text_size(14).child("📝 새 부서별 주의사항 (Gotcha) 기록"))
          .child(div().cursor_pointer().px(6).py(2).rounded(4).bg(t.raised).text_size(11)
            .on_click((_e, cx) => { this.addModalOpen = false; cx.notify(); }).child("✕ 닫기")))
        .child(div().text_size(11).text_color(t.muted)
          .child("기록된 주의사항은 전사 Wiki 정본에 즉시 추가되며 모든 에이전트와 지식 허브에 반영됩니다."))
        // Department Selection Chips
        .child(v_flex().gap(4)
          .child(div().text_size(11).font_semibold().child("대상 부서 선택:"))
          .child(h_flex().flex_wrap().gap(4)
            .children(depts.map(d => {
              const active = this.newGotchaDept === d.id;
              return div().key(d.id).px(8).py(4).rounded(4).cursor_pointer()
                .bg(active ? t.accent : t.raised).text_color(active ? "#ffffff" : t.foreground)
                .text_size(11).font_semibold()
                .on_click((_e, cx) => { this.newGotchaDept = d.id; cx.notify(); })
                .child(d.label);
            }))))
        // Title Input
        .child(v_flex().gap(4)
          .child(div().text_size(11).font_semibold().child("주의사항 제목:"))
          .child(div().h(30).px(8).flex().items_center().rounded(6).border(1).border_color(t.border).bg(t.raised)
            .child(Input.new(this.newTitleInput).w_full().text_size(12).text_color(t.foreground))))
        // Content Input
        .child(v_flex().gap(4)
          .child(div().text_size(11).font_semibold().child("주의사항 내용 및 핵심 원칙:"))
          .child(div().h(80).p(8).rounded(6).border(1).border_color(t.border).bg(t.raised)
            .child(Input.new(this.newContentInput).w_full().h_full().text_size(12).text_color(t.foreground))))
        // Actions
        .child(h_flex().justify_end().gap(8).pt(4)
          .child(div().px(12).py(6).rounded(6).cursor_pointer().bg(t.raised).text_size(12).font_semibold()
            .on_click((_e, cx) => { this.addModalOpen = false; cx.notify(); }).child("취소"))
          .child(div().px(14).py(6).rounded(6).cursor_pointer().bg("#10b981").text_color("#ffffff").text_size(12).font_semibold()
            .on_click((_e, cx) => cx.spawn(async cx => this.submitNewGotcha(cx))).child("지식 허브에 저장"))));
  }

  renderEmployeeModal(cx, t) {
    const emp = this.selectedEmployee;
    if (!emp) return div();

    const providerColor = emp.provider === "codex" ? "#059669" : "#7c3aed";
    const isWorking = emp.state === "working";

    return div().absolute().size_full().top(0).left(0).bg("#000000bb").flex().items_center().justify_center()
      .child(v_flex().w(380).p(16).gap(12).rounded(10).bg(t.surface).border(1).border_color(t.border)
        // Card Header
        .child(h_flex().items_center().justify_between().pb(6).border_b(1).border_color(t.border)
          .child(h_flex().items_center().gap(6)
            .child(div().text_size(14).child("🪪"))
            .child(div().font_bold().text_size(13).child("가상 사옥 임직원 사원증 (ID Badge)")))
          .child(div().cursor_pointer().px(6).py(2).rounded(4).bg(t.raised).text_size(11)
            .on_click((_e, cx) => { this.selectedEmployee = null; cx.notify(); }).child("✕ 닫기")))
        // Employee Profile
        .child(h_flex().items_center().gap(12).p(10).rounded(8).bg(t.raised)
          .child(div().size(44).rounded_full().bg(providerColor).flex().items_center().justify_center()
            .child(div().font_bold().text_size(18).text_color("#ffffff").child(emp.label.slice(0, 1))))
          .child(v_flex().gap(2)
            .child(div().font_bold().text_size(15).text_color(t.foreground).child(emp.label))
            .child(div().text_size(11).text_color(t.muted).child(emp.teamLabel))))
        // Metadata Table
        .child(v_flex().gap(6).p(8).rounded(6).bg(t.surface).border(1).border_color(t.border)
          .child(this.renderDetailRow("AI 공급자 / 모델", emp.provider === "codex" ? "Codex (OpenAI)" : "Claude Code (Anthropic)", t))
          .child(this.renderDetailRow("계정 프로필", emp.account ? `${emp.account} 계정` : "로컬 터미널 바인딩", t))
          .child(this.renderDetailRow("터미널 주소", emp.terminal || "미확인", t))
          .child(this.renderDetailRow("현재 동작 상태", isWorking ? "🟢 작업 진행 중 (Working)" : "⚪ 대기 상태 (Idle)", t))
          .child(this.renderDetailRow("신원 확인 근거", emp.identity_verified ? "✅ 계정 및 태스크 소유권 검증됨" : "ℹ️ App 세션 및 화면 바인딩 기준", t)))
        // Action Button: Jump to Terminal
        .child(h_flex().justify_end().gap(8).pt(4)
          .child(div().px(12).py(6).rounded(6).cursor_pointer().bg(t.raised).text_size(12).font_semibold()
            .on_click((_e, cx) => { this.selectedEmployee = null; cx.notify(); }).child("닫기"))
          .when(Boolean(emp.terminal), el => el.child(div().px(14).py(6).rounded(6).cursor_pointer()
            .bg(t.accent).text_color("#ffffff").text_size(12).font_semibold()
            .on_click(() => {
              run("agent.focus", { terminal: emp.terminal });
              this.selectedEmployee = null;
              cx.notify();
            }).child("터미널 바로가기 ↗")))));
  }

  renderDetailRow(label, value, t) {
    return h_flex().items_center().justify_between().py(2)
      .child(div().text_size(11).text_color(t.muted).child(label))
      .child(div().text_size(11).font_semibold().text_color(t.foreground).child(value));
  }
}
