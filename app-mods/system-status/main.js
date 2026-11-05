import { View, div } from "gpui-kit";
import { h_flex } from "gpui-base";
import { spaces, agents, theme, exec } from "herdr";

const SAMPLER = "$HOME/.config/herdr-app/mods/system-status/sample.sh";
const SAMPLE_MS = 3000;
const HOT = 90;

export default class SystemStatus extends View {
  init(_props, cx) {
    this.stats = null; this.width = 470;
    this.sampling = false;
    cx.timer.every(SAMPLE_MS, cx => this.sample(cx));
    this.sample(cx);
  }

  sample(cx) {
    if (this.sampling) return;
    this.sampling = true;
    cx.spawn(async cx => {
      try {
        const r = await exec("/bin/zsh", ["-c", SAMPLER]);
        if(r.code!==0)throw new Error("sample failed");
        const next=JSON.parse(r.stdout);
        if(![next.cpu,next.gpu,next.mem,next.total].every(value=>Number.isFinite(value)&&value>=0)||next.total<=0)throw new Error("invalid sample");
        this.stats = next; this.failure = false;
      } catch {
        this.failure = true;
      }
      this.sampling = false;
      cx.notify();
    });
  }

  meter(t, name, pct, text) {
    const color = pct >= HOT ? t.blocked : t.muted;
    return h_flex().flex_none().gap(5).items_center().child(div().child(name))
      .when(this.width >= 420, el => el.child(div().w(30).h(4).rounded(2).bg(t.raised)
        .child(div().h_full().rounded(2).bg(color).w(Math.max(1,Math.min(30,pct*0.3))))))
      .child(div().text_color(color).child(text));
  }

  render() {
    const t=theme(),s=this.stats,space=spaces().find(item=>item.active),list=agents();
    const counts=status=>list.filter(item=>item.status===status).length;
    const details=[space?.machine ?? "이 컴퓨터",`에이전트 ${list.length} · 작업 중 ${counts("working")} · 확인 필요 ${counts("blocked")}`,
      space?.machine ? "원격 시스템 수치 미수집" : s ? `CPU ${s.cpu}% · GPU ${s.gpu}% · 메모리 ${s.mem.toFixed(1)}/${s.total}GB` : "시스템 측정 대기",
      !space?.machine && s?.batt != null ? `${s.ac ? "전원 연결" : "배터리"} ${s.batt}%` : "",
      this.failure && !space?.machine ? (s ? "갱신 실패 · 이전 측정값" : "측정 실패") : ""].filter(Boolean).join("\n");
    return h_flex().size_full().min_w(0).px(14).gap(16).items_center().bg(t.chrome).border_t(1).border_color(t.border).text_size(11.8).text_color(t.muted)
      .on_size_change((size,cx)=>{if(this.width!==size.width){this.width=size.width;cx.notify();}})
      .when(this.width>=700,el=>el.child(div().flex_none().child(space?.machine ?? "This computer")).child(div().flex_none().child(`${list.length} agents`)))
      .child(h_flex().flex_1().min_w(0).gap(14).items_center().justify_end().overflow_x_scrollbar()
        .when(Boolean(s)&&!space?.machine,el=>el
          .child(this.meter(t,"CPU",s.cpu,`${s.cpu}%`))
          .child(this.meter(t,"GPU",s.gpu,`${s.gpu}%`))
          .child(this.meter(t,"메모리",s.mem/s.total*100,`${s.mem.toFixed(1)}/${s.total}GB`)))
        .when(!s||Boolean(space?.machine),el=>el.child(div().flex_none().child(space?.machine ?? "측정 중"))))
      .child(div().flex_none().px(4).text_color(this.failure&&!space?.machine?t.blocked:t.dim).child(this.failure&&!space?.machine?"⚠":"ⓘ").tooltip(details));
  }
}
