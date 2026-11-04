import { View, div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { appHome, readTextFile, exec, theme, run } from "herdr";

export default class TeamOperations extends View {
  init(_props, cx) {
    this.data = null; this.error = null; this.busy = false; this.config = null;
    cx.spawn(async cx => {
      try {
        const raw = await readTextFile(appHome() + "/mods/team-operations/local.json");
        this.config = JSON.parse(raw);
        await this.refresh(cx);
      } catch (error) { this.error = String(error); cx.notify(); }
    });
    cx.timer.every(30000, cx => cx.spawn(async cx => this.refresh(cx)));
  }

  async refresh(cx) {
    if (!this.config || this.busy) return;
    this.busy = true;
    try {
      const c = this.config;
      const result = await exec(c.python, [c.script, "--config", c.config, "--state", c.state, "--bindings", c.bindings, "knowledge-hub"]);
      if (result.code !== 0) throw new Error(result.stderr || "현황 조회 실패");
      this.data = JSON.parse(result.stdout); this.error = null;
    } catch (error) { this.error = String(error); }
    finally { this.busy = false; cx.notify(); }
  }

  render(cx) {
    const t = theme(), roster = this.data?.roster, teams = roster?.teams ?? [], knowledge = this.data?.knowledge ?? [];
    const title = text => div().font_semibold().text_size(14).child(text);
    const line = text => div().min_w(0).whitespace_normal().text_size(12).text_color(t.muted).child(text);
    const roleLabels = {leader:"팀장",implementer:"구현 담당",reviewer:"검토 담당"};
    const teamLabels = Object.fromEntries(teams.map(team => [team.id,team.label]));
    return v_flex().id("team-operations").size_full().min_h(0).p(14).gap(10).bg(t.background).text_color(t.foreground)
      .child(div().font_semibold().text_size(16).child("운영본부 · 지식 허브"))
      .when(Boolean(this.error), el => el.child(div().text_color(t.blocked).child(`갱신 실패 · 이전 자료 유지: ${this.error}`)))
      .when(!this.data && !this.error, el => el.child(line("지식과 직원명부를 읽는 중입니다.")))
      .child(h_flex().flex_none().flex_wrap().gap(8).children(teams.map(team =>
        v_flex().min_w(180).gap(4).p(9).rounded(8).bg(t.surface)
          .child(title(team.label))
          .child(line(`${team.live ? (team.state ?? "상태 미확인") : "세션 미확인"} · ${team.identity}`))
          .when(team.live && Boolean(team.terminal), el => el.child(div().id(`focus-${team.id}`).cursor_pointer().px(10).py(6).rounded(6).bg(t.raised)
            .on_click(() => run("agent.focus", {terminal:team.terminal})).child("스페이스 열기"))))))
      .child(h_flex().flex_1().min_h(0).min_w(0).gap(10)
        .child(v_flex().flex_1().min_w(0).min_h(0).gap(8).overflow_y_scrollbar()
          .child(title("팀별 주의사항"))
          .children(knowledge.filter(item => item.team !== "common").map(item =>
            v_flex().gap(5).p(10).rounded(8).bg(t.surface).child(title(item.title))
              .child(line(teamLabels[item.team] ?? item.team)).child(line(item.body.split("\n").slice(2).join("\n"))))))
        .child(v_flex().flex_1().min_w(0).min_h(0).gap(8).overflow_y_scrollbar()
          .child(title("전사 Wiki 색인"))
          .children(knowledge.map(item => line(`${item.title} · ${item.source}`)))
          .child(title("직원명부"))
          .children(teams.map(team => v_flex().gap(3).p(8).rounded(8).bg(t.surface)
            .child(title(team.label))
            .child(line(team.employees.length ? team.employees.map(person => `${person.label} · ${roleLabels[person.role] ?? "담당 직원"} · ${person.state}`).join(" / ") : "확인된 직원 신원 없음"))
            .child(line(team.identity))))
          .child(title("설정된 프로필"))
          .children((roster?.profiles ?? []).map(profile => line(`${profile.label} · ${profile.provider}/${profile.account}`)))));
  }
}
