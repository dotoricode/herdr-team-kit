import { View, div, image, svg } from "gpui-kit";
import { v_flex, h_flex, Popover, Scrollbar, Textarea, TextareaState, TextView, Editor, EditorState } from "gpui-base";
import { appHome, readTextFile, exec, agents, terminals, theme, settings, run, nextCommand, props as getModProps, openOverlay, closeOverlay, notify as showNotice, layout } from "herdr";

const LOCAL = "/mods/team-operations/local.json";
const DUMMY = "—";
const visibleSnapshot = value => JSON.stringify(value, (key, item) => key === "observed_at" ? undefined : key === "age" && typeof item === "number" ? Math.floor(item / 60) : item);
// Drafts live in module scope for the running app: closing, refreshing or a changed card never submits or discards them.
const DRAFTS = new Map();
const requestId = () => `gui-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
// A very long unbroken token (path, id) may wrap after its separators instead of running past the card.
const softBreaks = text => String(text).replace(/\S{28,}/g, token => token.replace(/([/._:\-])/g, "$1\u200b"));
const shortHash = text => { let h = 5381; for (const ch of String(text)) h = ((h * 33) ^ ch.codePointAt(0)) >>> 0; return h.toString(36); };
const BOARD_NARROW = 900;
const SEND_FINAL = new Set(["sent", "not_sent", "unknown"]);
function documentPreview(source) {
  return String(source ?? "");
}

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
const OVERVIEW = { height: 166, rail: 98, card: 94, cardMinWidth: 210, gap: 6, padding: 8 };
const overviewColumnsFor = width => width - 2 * OVERVIEW.padding >= 4 * OVERVIEW.cardMinWidth + 3 * OVERVIEW.gap ? 4
  : width - 2 * OVERVIEW.padding >= 2 * OVERVIEW.cardMinWidth + OVERVIEW.gap ? 2 : 1;
function findLayoutNode(node, id) {
  if (node?.id === id) return node;
  for (const child of node?.children ?? []) {
    const found = findLayoutNode(child, id);
    if (found) return found;
  }
  return null;
}
const STATUS_TEXT = { unknown: "미확인", working: "실행 중", running: "실행 중", blocked: "사용자 답변 필요", done: "응답 완료", idle: "대기 중", pending: "대기 중", failed: "실패", error: "오류", success: "성공", healthy: "정상", not_measured: "측정 전", not_available: "미제공", unavailable: "미제공", missing: "미확인", current: "최신 기록", stale: "지난 기록" };
function statusText(status) { return STATUS_TEXT[status] ?? (status == null ? "미확인" : display(status)); }
function statusColor(status, t) { return status === "working" ? t.working : status === "blocked" ? t.blocked : status === "done" ? t.done : status === "idle" ? t.idle : t.unknown; }
const PRESENCE_TITLES = {
  working: ["업무 중", "집중 모드 켰어요", "한창 몰입 중", "열일 중이에요", "착착 진행 중", "잠시만요, 집중 중"],
  blocked: ["확인 부탁드려요", "여기 확인 한 번만요", "답변을 기다리고 있어요", "확인해 주시면 이어갈게요"],
  done: ["답변을 남겼어요", "새 답변 도착했어요", "대화에 답변 두고 왔어요", "답변 확인해 주세요"],
  idle: ["다음 업무 대기", "다음 일 받을 준비됐어요", "다음 타자 주세요", "부르면 바로 여기요", "새 요청 기다리는 중", "준비됐어요, 말씀하세요"]
};
function teamPresence(status, taskState, teamId = "", observedAt = 0, handoff = null) {
  if (handoff) return { title: handoff.title, detail: handoff.detail };
  const titles = PRESENCE_TITLES[status];
  const offset = Array.from(String(teamId)).reduce((value, char) => (value * 31 + char.charCodeAt(0)) >>> 0, 0);
  const slot = Number.isFinite(observedAt) ? Math.floor(observedAt / 45) : 0;
  const title = titles?.[(offset + slot) % titles.length];
  if (status === "working") return {
    title,
    detail: ({ launching: "업무를 시작할 준비 중이에요", submitted: "전달된 요청을 확인하고 있어요", running: "맡은 업무를 진행하고 있어요" })[taskState] ?? "대화에서 응답을 준비하고 있어요"
  };
  if (status === "blocked") return { title, detail: "입력이나 승인을 기다리고 있어요" };
  if (status === "done") return { title, detail: "대화를 열어 확인해 주세요" };
  if (status === "idle") return {
    title,
    detail: taskState === "awaiting_review" ? "검토 의견을 기다리고 있어요" : taskState === "queued" ? "배정된 요청이 대기 중이에요" : "다음 요청을 기다리고 있어요"
  };
  return { title: "상태 확인 필요", detail: "지금은 상태를 확인할 수 없어요" };
}

export default class TeamOperations extends View {
  init(props, cx) {
    this.viewProps = props ?? getModProps() ?? {};
    this.role = this.viewProps.role ?? "full";
    this.data = null; this.error = null; this.busy = false; this.seq = 0;
    this.expanded = new Set(); this.doc = null; this.docBusy = false; this.docSource = false;
    this.confirm = null; this.actionBusy = false; this.message = null;
    this.showAccounts = false; this.showArchived = false; this.boardBusy = false; this.boardMessage = null; this.boardRequestIds = new Map(); this.boardStatusOpen = null; this.docMode = "brief"; this.showCompleted = false; this.coordinatorConfirm = false; this.coordinatorBusy = false; this.coordinatorMessage = null;
    this.usageBusy = false; this.usageMessage = null;
    this.statusOpen = null; this.statusRevealed = false; this.statusEpoch = 0;
    this.detailData = null; this.detailError = null; this.detailBusy = false; this.detailFetching = false; this.detailHeight = null;
    this.choiceIds = new Map(); this.choiceBusy = false; this.choiceStarted = false; this.choiceCompleted = false;
    this.infoOpen = false; this.historyOpen = false;
    this.cardData = null; this.cardError = null; this.cardBusy = false;
    this.answerChoice = null; this.answerBusy = false; this.answerMessage = null;
    this.closed = false; this.refreshPromise = null; this.activeSeq = 0; this.manual = null; this.manualCount = 0; this.lastSuccessAt = null; this.viewWidth = null; this.viewHeight = null;
    this.form = { key: null, option: null }; this.restoring = false; this.preservedKeys = [];
    this.sendBusy = false; this.sendRequestId = null; this.sendResult = null; this.sendMessage = null; this.techOpen = false;
    if (this.role === "detail") {
      const edited = (_e, cx) => { if (this.restoring) return; this.rememberDraft(); this.editedAfterSend(); cx.notify(); };
      this.answerInput = TextareaState.new({ placeholder: "확인한 내용이나 필요한 조치를 적어 주세요.", rows: 3 });
      this.answerInput.on("change", edited);
      this.otherInput = TextareaState.new({ placeholder: "직접 입력할 내용을 적어 주세요.", rows: 3 });
      this.otherInput.on("change", edited);
    }
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
      if (["detail", "choice"].includes(this.role)) {
        await Promise.all([this.loadCard(cx), ...(this.viewProps.kind === "work" || this.viewProps.linkedWorkId ? [this.loadDetail(cx)] : [])]);
        if (this.role === "choice" && this.choiceContext()?.intent === "approve") await this.document({ id: this.viewProps.workId }, cx);
        if (this.viewProps.readDocument && this.viewProps.kind === "work" && this.detailData) await this.document(this.detailData, cx);
      }
    });
    if (!["detail", "choice"].includes(this.role)) cx.spawn(async cx => {
      for (;;) {
        const command = await nextCommand();
        if (command === "refresh") cx.spawn(async cx => this.refresh(cx, true));
        else if (command.startsWith("status-") && ["blocked", "working", "done", "idle"].includes(command.slice(7))) {
          const status = command.slice(7);
          this.setStatusOpen(status, this.statusOpen !== status, cx);
        }
        else if (command === "close-status") this.setStatusOpen(this.statusOpen, false, cx);
        else if (command.startsWith("card-")) {
          const item = this.data?.inventory?.find(item => item.id === command.slice(5));
          if (item) this.openDetail("inventory", item);
        }
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
      const stale = Boolean(this.data && this.lastSuccessAt && Date.now() - this.lastSuccessAt > 15000);
      if (stale !== Boolean(this.stale)) { this.stale = stale; cx.notify(); }
      await this.refresh(cx);
      if (["detail", "choice"].includes(this.role)) await Promise.all([this.loadCard(cx), ...(this.viewProps.kind === "work" || this.viewProps.linkedWorkId ? [this.loadDetail(cx)] : [])]);
    }));
    // A pending/sending delivery is only looked up, never re-sent.
    if (["detail", "choice"].includes(this.role)) cx.timer.every(1500, cx => cx.spawn(async cx => this.pollSend(cx)));
  }

  update(props) { this.viewProps = props ?? getModProps() ?? {}; }

  async loadCard(cx) {
    if (this.cardBusy || !this.viewProps.workId) return;
    this.cardBusy = true;
    const previous = visibleSnapshot(this.cardData), previousError = this.cardError;
    if (!this.cardData && !this.cardError) cx.notify();
    try {
      const result = await this.args("card-detail", ["--kind", this.viewProps.kind === "inventory" ? "inventory" : "work", "--work", String(this.viewProps.workId)]);
      if (result.code !== 0) throw new Error(result.stderr || "카드 정보를 읽지 못했습니다.");
      const next = JSON.parse(result.stdout);
      if (String(next.id) !== String(this.viewProps.workId)) throw new Error("요청한 카드와 다른 정보를 받았습니다.");
      if (this.closed) return;
      this.cardData = next;
      this.syncForm();
      this.cardError = null;
    } catch (e) { this.cardError = String(e); }
    finally { this.cardBusy = false; if (!this.closed && (previous !== visibleSnapshot(this.cardData) || previousError !== this.cardError)) cx.notify(); }
  }

  // The form is keyed by kind/work/question id+revision/card revision/document digest. A changed key starts a fresh
  // form; the old draft stays under its original key and is never moved onto a newer question or an option number.
  formKeyFor(card) {
    if (!card) return null;
    const q = card.question;
    return [card.kind, card.id, q ? `${q.question_id}:${q.question_revision}` : "general", card.revision, card.document_digest ?? ""].join("|");
  }

  rememberDraft() {
    if (!this.form.key || !this.answerInput) return;
    DRAFTS.set(this.form.key, { option: this.form.option, choice: this.answerChoice, other: this.otherInput?.value() ?? "", comment: this.answerInput.value() });
  }

  syncForm() {
    const card = this.cardData, key = this.formKeyFor(card);
    if (!key || key === this.form.key) return;
    if (this.form.key) this.rememberDraft();
    const saved = DRAFTS.get(key);
    this.form = { key, option: saved?.option ?? null };
    this.answerChoice = saved?.choice ?? (saved ? null : card.answer?.choice ?? null);
    this.restoring = true;
    this.answerInput?.set_value(saved?.comment ?? (saved ? "" : card.answer?.text ?? ""));
    this.otherInput?.set_value(saved?.other ?? "");
    this.restoring = false;
    this.sendRequestId = null;
    const prefix = `${card.kind}|${card.id}|`;
    this.preservedKeys = [...DRAFTS.keys()].filter(k => k.startsWith(prefix) && k !== key && (DRAFTS.get(k).comment?.trim() || DRAFTS.get(k).other?.trim())).slice(-3);
  }

  editedAfterSend() {
    // New content is a new request; the earlier request keeps its own recorded result and is never altered.
    this.sendRequestId = null;
  }

  async saveAnswer(cx) {
    const card = this.cardData;
    if (this.answerBusy || this.cardBusy && !card || this.cardError || !card?.can_answer || !this.answerChoice) return;
    const text = this.answerInput?.value() ?? "";
    if (text.length > 4000 || (this.answerChoice !== "confirmed" && !text.trim())) {
      this.answerMessage = "수정 필요·보류 사유를 입력해 주세요. 답변은 4,000자까지 저장할 수 있습니다."; cx.notify(); return;
    }
    const submittedChoice = this.answerChoice, key = this.form.key;
    let succeeded = false;
    this.answerBusy = true; this.answerMessage = null; cx.notify();
    try {
      const result = await this.args("card-answer", ["--kind", card.kind, "--work", String(card.id), "--card-revision", card.revision, "--choice", submittedChoice, `--answer-text=${text}`]);
      if (result.code !== 0) throw new Error(result.stderr || "답변을 저장하지 못했습니다.");
      const response = JSON.parse(result.stdout); succeeded = true;
      this.answerMessage = this.form.key !== key ? "이전 카드 내용에 대한 답변은 저장됐습니다. 현재 내용을 확인하고 다시 답변해 주세요." : response.message ?? "답변을 저장했습니다. 총괄 확인 대기입니다.";
      await this.loadCard(cx);
    } catch (e) { this.answerMessage = `⚠ ${String(e)}`; }
    finally { this.answerBusy = false; if (!this.closed) cx.notify(); }
    return succeeded;
  }

  // One explicit click sends one request. The id is made at the click and kept until the content changes, so a
  // timeout, error or repeated click only looks the same request up; the server never resends it.
  async sendAnswer(cx, typed, selectedComment = null) {
    const card = this.cardData, q = card?.question;
    if (this.sendBusy || !card?.can_answer || this.cardError) return;
    const fields = { kind: card.kind, work_id: String(card.id), card_revision: card.revision, document_revision: card.document_revision, document_digest: card.document_digest };
    if (typed) {
      const option = this.form.option, other = this.otherInput?.value() ?? "";
      if (!q || !option) return;
      if (option === "other" && !other.trim()) { this.sendMessage = "직접 입력을 선택했다면 내용을 적어 주세요."; cx.notify(); return; }
      if (other.length > 4000) { this.sendMessage = "직접 입력은 4,000자까지 보낼 수 있습니다."; cx.notify(); return; }
      Object.assign(fields, { question_id: q.question_id, question_revision: q.question_revision, option_id: option, other_text: option === "other" ? other : "" });
    } else {
      const comment = selectedComment ?? this.answerInput?.value() ?? "";
      if (!["changes", "defer"].includes(this.answerChoice) || !comment.trim() || comment.length > 4000) {
        this.sendMessage = "수정 필요 또는 나중 확인을 고르고 4,000자 이내 내용을 적어 주세요."; cx.notify(); return;
      }
      Object.assign(fields, { choice: this.answerChoice, comment });
    }
    if (this.sendResult?.state === "not_sent") this.sendRequestId = null; // a resolved refusal may be tried again as a new request
    if (!this.sendRequestId) this.sendRequestId = requestId();
    fields.client_request_id = this.sendRequestId;
    this.sendBusy = true; this.sendMessage = null; cx.notify();
    try {
      const result = await this.args("card-send", ["--send-json", JSON.stringify(fields)]);
      if (result.code !== 0) throw new Error(result.stderr || "전송 요청을 처리하지 못했습니다.");
      const response = JSON.parse(result.stdout);
      this.sendResult = response.request; this.sendMessage = response.message ?? null;
    } catch (e) {
      // The server may have recorded it before the failure: look the same id up; never send a second time.
      this.sendMessage = `⚠ 전송 결과를 확인하지 못했습니다. 같은 요청 ID로 상태를 조회합니다. (${String(e).slice(0, 160)})`;
      await this.lookupSend(cx);
    } finally { this.sendBusy = false; if (!this.closed) { await this.loadCard(cx); cx.notify(); } }
  }

  async lookupSend(cx) {
    if (!this.sendRequestId) return;
    try {
      const result = await this.args("card-send-status", ["--request-id", this.sendRequestId]);
      if (result.code !== 0) throw new Error(result.stderr || "");
      this.sendResult = JSON.parse(result.stdout).request;
    } catch (_) { if (!this.sendResult) this.sendMessage = "⚠ 서버에 접수된 기록이 아직 없습니다. 확인 후 다시 누르면 같은 요청으로 안전하게 이어집니다."; }
    if (!this.closed) cx.notify();
  }

  async pollSend(cx) {
    if (this.closed || this.sendBusy || !this.sendResult || !["pending", "sending"].includes(this.sendResult.state)) return;
    const before = this.sendResult.state;
    await this.lookupSend(cx);
    if (this.sendResult?.state !== before) await this.loadCard(cx);
    if (this.role === "choice" && (this.sendResult?.state === "sent" || this.sendResult?.receipt)) this.finishChoice("요청을 전달했습니다");
  }

  async loadDetail(cx) {
    if (this.detailFetching || !this.viewProps.workId) return;
    this.detailFetching = true; this.detailBusy = !this.detailData && !this.detailError;
    const previous = visibleSnapshot(this.detailData), previousError = this.detailError;
    if (this.detailBusy) cx.notify();
    try {
      const workId = this.viewProps.linkedWorkId ?? this.viewProps.workId;
      const r = await this.args("work-detail", ["--work", String(workId)]);
      if (r.code !== 0) throw new Error(r.stderr || `작업 정보를 읽지 못했습니다 (종료 코드 ${r.code}).`);
      const detail = JSON.parse(r.stdout);
      if (String(detail.id) !== String(workId)) throw new Error("요청한 작업과 다른 정보를 받았습니다.");
      this.detailData = detail;
      this.detailError = null;
    } catch (e) { this.detailError = String(e); }
    finally { this.detailFetching = false; this.detailBusy = false; if (previous !== visibleSnapshot(this.detailData) || previousError !== this.detailError) cx.notify(); }
  }

  openDetail(kind, item, readDocument = false) {
    this.selectedWorkId = item.id;
    this.detailOverlay = openOverlay({ mod: "team-operations", props: { role: "detail", kind, workId: item.id, linkedWorkId: item.work_id, item, local: this.viewProps.local, readDocument }, kind: "dialog", modal: false, width: kind === "inventory" && !item.work_id ? 0.6 : 0.74, height: kind === "inventory" && !item.work_id ? (String(item.status ?? "").startsWith("완료") ? 0.38 : 0.52) : 0.76 });
  }

  setStatusOpen(status, open, cx) {
    if (!open && this.statusOpen !== status) return;
    this.statusOpen = open ? status : null;
    this.statusRevealed = false;
    const epoch = ++this.statusEpoch;
    cx.notify();
    if (open) cx.timer.after(32, cx => {
      if (this.statusOpen === status && this.statusEpoch === epoch) {
        this.statusRevealed = true;
        cx.notify();
      }
    });
  }

  args(action, fields = []) {
    const c = this.config;
    if (!c) throw new Error(this.configError || "로컬 backend 설정을 읽는 중입니다.");
    return exec(c.python, [c.script, "--config", c.config, "--state", c.state, "--bindings", c.bindings, action, ...fields]);
  }

  // Automatic polling and the manual button share one in-flight request. A manual click while an automatic
  // request runs joins it (no extra request) and finishes only with that same request's result.
  refresh(cx, manual = false) {
    if (manual) {
      if (this.manual?.phase === "running") return this.refreshPromise;
      this.manual = { id: ++this.manualCount, phase: "running", startedAt: Date.now(), joined: Boolean(this.refreshPromise), joinedSeq: this.refreshPromise ? this.activeSeq : null, completedAt: null, changed: null, error: null };
      cx.notify();
    }
    if (!this.refreshPromise) {
      const run = this.runRefresh(cx);
      this.refreshPromise = run;
      if (this.manual?.phase === "running" && this.manual.joinedSeq == null) this.manual.joinedSeq = this.activeSeq;
      run.finally(() => { if (this.refreshPromise === run) this.refreshPromise = null; });
    }
    const joined = this.refreshPromise;
    return joined.then(result => {
      const state = this.manual;
      if (!this.closed && state?.phase === "running" && state.joinedSeq === result.seq) {
        this.manual = result.ok
          ? { ...state, phase: "done", changed: result.changed, completedAt: this.lastSuccessAt }
          : { ...state, phase: "failed", error: result.error };
        cx.notify();
      }
      return result;
    });
  }

  async runRefresh(cx) {
    this.busy = !this.data && !this.error; const seq = ++this.seq; this.activeSeq = seq;
    const previous = visibleSnapshot(this.data), previousError = this.error, previousConfirm = this.confirm, previousStale = this.stale;
    let outcome = { seq, ok: false, changed: false, error: null };
    try {
      const result = await this.args("view");
      if (result.code !== 0) throw new Error(result.stderr || `현황 조회 명령이 실패했습니다 (종료 코드 ${result.code}).`);
      const next = JSON.parse(result.stdout);
      if (this.closed) return outcome;
      if (seq === this.seq) {
        this.data = next; this.error = null; this.lastSuccessAt = Date.now(); this.stale = false;
        outcome = { seq, ok: true, changed: previous !== visibleSnapshot(next), error: null };
        const latest = (next.works ?? []).find(work => work.id === this.confirm?.id);
        if (this.confirm && (!this.doc || this.doc.id !== this.confirm.id || this.doc.revision !== this.confirm.revision || this.doc.digest !== this.confirm.digest || this.doc.phase !== this.confirm.phase || latest?.revision !== this.confirm.revision || latest?.phase !== this.confirm.phase || (latest?.digest != null && latest.digest !== this.confirm.digest))) this.confirm = null;
      }
    } catch (e) {
      // A failed refresh keeps the shown data and the previous success time.
      outcome = { seq, ok: false, changed: false, error: String(e) };
      if (seq === this.seq && !this.closed) this.error = String(e);
    }
    finally { this.busy = false; if (!this.closed && (previous !== visibleSnapshot(this.data) || previousError !== this.error || previousConfirm !== this.confirm || previousStale !== this.stale)) cx.notify(); }
    return outcome;
  }

  async document(work, cx) {
    if (this.actionBusy || this.docBusy) return;
    this.selectedWorkId = work.id; this.docBusy = true; this.docSource = false; this.docMode = "brief"; this.confirm = null; this.message = null; this.doc = null;
    this.docEditor?.release(); this.docEditor = null;
    const id = work.id;
    try {
      const r = await this.args("document", ["--work", String(id)]);
      if (r.code !== 0) throw new Error(r.stderr || `설계 읽기 명령이 실패했습니다 (종료 코드 ${r.code}).`);
      const d = JSON.parse(r.stdout);
      if (d.id !== id) throw new Error("backend가 요청한 작업과 다른 문서를 반환했습니다.");
      this.doc = d;
      const t = theme();
      this.docEditor = EditorState.new({ language: "markdown", readonly: true, value: String(d.source_body ?? d.body ?? ""), font_family: this.config?.document_code_font || "D2Coding", font_size: 14, line_numbers: false, folding: false, soft_wrap: true,
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

  async close(_cx) { this.closed = true; this.rememberDraft(); this.answerInput?.release(); this.answerInput = null; this.otherInput?.release(); this.otherInput = null; this.docEditor?.release(); this.docEditor = null; this.confirm = null; }

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
    let succeeded = false;
    this.actionBusy = true; this.confirm = null; this.message = null; cx.notify();
    try {
      const args = pending.kind === "approve"
        ? ["--work", String(doc.id), "--revision", String(doc.revision), "--digest", String(doc.digest), "--confirmed"]
        : ["--task", String(pending.value.id), "--owner", String(pending.value.owner), "--attempt", String(pending.value.attempt), "--work", String(doc.id), "--revision", String(doc.revision), "--digest", String(doc.digest), "--confirmed"];
      const r = await this.args(pending.kind, args);
      if (r.code !== 0) throw new Error(r.stderr || `요청한 작업이 실패했습니다 (종료 코드 ${r.code}).`);
      const out = JSON.parse(r.stdout); succeeded = true; this.message = String(out.message ?? "처리가 끝났습니다.");
      if (pending.kind === "approve") this.doc = { ...doc, can_approve: false, phase: "approved" };
    } catch (e) { this.message = String(e); }
    finally { this.actionBusy = false; await this.refresh(cx); cx.notify(); }
    return succeeded;
  }

  openChoice(kind, id) {
    const card = this.cardData;
    if (!card?.can_answer || this.cardError || this.choiceBusy) return;
    const option = kind === "question" ? card.question?.options.find(o => o.option_id === id) : card.decision?.options.find(o => o.id === id);
    if (!option) return;
    const key = `${this.formKeyFor(card)}|${kind}|${id}`;
    if (!this.choiceIds.has(key)) this.choiceIds.set(key, requestId());
    openOverlay({ mod: "team-operations", kind: "dialog", modal: false, width: 0.48, height: 0.30,
      props: { role: "choice", kind: card.kind, workId: card.id, local: this.viewProps.local,
        parentOverlay: this.viewProps.overlay ?? getModProps()?.overlay,
        selection: { kind, id, cardRevision: card.revision, documentRevision: card.document_revision,
          documentDigest: card.document_digest, questionId: card.question?.question_id,
          questionRevision: card.question?.question_revision, clientRequestId: this.choiceIds.get(key) } } });
  }

  choiceContext() {
    const card = this.cardData, selected = this.viewProps.selection;
    if ((card?.presentation ? !card.presentation.decision_required : card?.interaction && !card.needs_decision) && !this.choiceStarted) return null;
    if (!card || !selected || card.revision !== selected.cardRevision
        || card.document_revision !== selected.documentRevision || card.document_digest !== selected.documentDigest) return null;
    let option;
    if (selected.kind === "question") {
      if (!card.question || card.question.question_id !== selected.questionId || card.question.question_revision !== selected.questionRevision) return null;
      const source = card.question.options.find(o => o.option_id === selected.id);
      if (source) option = { ...source, intent: "question" };
    } else if ((card.presentation?.decision_required ?? card.needs_decision) || this.choiceStarted) option = card.decision?.options.find(o => o.id === selected.id);
    if (!option || option.intent === "approve" && (card.kind !== "work" || card.status !== "awaiting_approval")) return null;
    const recipient = option.intent === "confirmed" ? "팀 총괄" : card.leader?.label ?? card.team_label ?? "담당 팀장";
    const unresolved = (card.deliveries ?? []).some(d => {
      if (d.receipt || !["pending", "sending", "unknown"].includes(d.effective_state ?? d.state)) return false;
      const b = d.binding;
      if (!b) return true;
      return b.card_revision === card.revision && b.document_revision === card.document_revision && b.document_digest === card.document_digest
        && (!card.question || b.question_id === card.question.question_id && b.question_revision === card.question.question_revision);
    });
    const next = option.intent === "approve" ? `설계 v${card.document_revision}의 진행 허가를 기록합니다. 다음 단계는 ${recipient}의 작업 배정이며, 이번 확인은 설계 승인까지입니다.`
      : option.intent === "confirmed" ? "확인 결과를 총괄에게 남깁니다. 추가 작업을 시작하지 않습니다."
        : `${recipient}에게 이 요청을 전달합니다. 전달 후 담당자의 처리 결과를 확인합니다.`;
    return { ...option, recipient, next, blocked: unresolved && ["question", "changes", "defer"].includes(option.intent)
      ? "이전 요청의 전달 결과를 확인 중입니다. 새 요청은 보내지 않습니다." : null };
  }

  async organizeCard(kind, card, action, cx) {
    if (this.boardBusy || this.error || !this.data?.actions_enabled || !card?.board?.revision) return;
    this.boardBusy = true; this.boardStatusOpen = null; this.boardMessage = null; cx.notify();
    const key = `${kind}|${card.id}|${action}|${card.board.revision}`;
    if (!this.boardRequestIds.has(key)) this.boardRequestIds.set(key, requestId());
    try {
      const r = await this.args("card-board-action", ["--kind", kind, "--work", card.id, "--board-action", action,
        "--board-revision", card.board.revision, "--request-id", this.boardRequestIds.get(key)]);
      if (r.code !== 0) throw new Error(r.stderr || "카드 정리 상태를 변경하지 못했습니다.");
      const result = JSON.parse(r.stdout);
      this.boardMessage = result.message;
      if (action === "done") this.showCompleted = true;
      const title = { waiting: "보드 상태를 대기로 바꿨습니다", progress: "보드 상태를 진행 중으로 바꿨습니다", done: "보드 상태를 완료로 바꿨습니다", archive: "카드를 보관했습니다", restore: "자동 상태로 되돌렸습니다" }[action];
      showNotice({ title, body: "실제 세션 실행과 승인 상태는 유지됩니다.", level: "success", timeout: 3000 });
      // A read started before the write cannot acknowledge the new board status.
      if (this.refreshPromise) await this.refreshPromise;
      await this.refresh(cx);
      if (this.role === "detail") await this.loadCard(cx);
    } catch (error) { this.boardMessage = String(error); }
    finally { this.boardBusy = false; if (!this.closed) cx.notify(); }
  }


  finishChoice(title) {
    if (this.choiceCompleted || this.closed) return;
    this.choiceCompleted = true;
    showNotice({ title, body: this.choiceContext()?.recipient, level: "success", timeout: 3500 });
    const overlay = this.viewProps.overlay ?? getModProps()?.overlay;
    if (overlay) closeOverlay(overlay);
    if (this.viewProps.parentOverlay) closeOverlay(this.viewProps.parentOverlay);
  }

  async confirmChoice(cx) {
    const choice = this.choiceContext();
    if (!choice || choice.blocked || !this.cardData?.can_answer || !this.data?.actions_enabled || this.stale
        || this.cardError || this.error || this.configError || this.choiceBusy || this.choiceStarted) return;
    if (choice.intent === "approve" && (!this.doc?.can_approve || this.doc.id !== this.cardData.id
        || this.doc.revision !== this.cardData.document_revision || this.doc.digest !== this.cardData.document_digest)) return;
    this.choiceBusy = true; this.choiceStarted = true; cx.notify();
    try {
      if (choice.intent === "approve") {
        this.begin("approve", null, cx);
        if (await this.executeConfirmed(cx)) this.finishChoice("설계를 승인했습니다");
      } else if (choice.intent === "confirmed") {
        this.answerChoice = "confirmed";
        if (await this.saveAnswer(cx)) this.finishChoice("확인 결과를 남겼습니다");
      } else {
        this.sendRequestId = this.viewProps.selection.clientRequestId;
        if (choice.intent === "question") this.form.option = this.viewProps.selection.id;
        else this.answerChoice = choice.intent;
        await this.sendAnswer(cx, choice.intent === "question", `${choice.label}\n${choice.description}`);
        if (this.sendResult?.state === "sent") this.finishChoice("요청을 전달했습니다");
      }
    } finally { this.choiceBusy = false; if (!this.closed) cx.notify(); }
  }

  syncOverviewHeight(width, height, cx) {
    const props = getModProps() ?? this.viewProps;
    if (props.role !== "overview" || props.overlay || props.pane || props.overview_layout_id !== "team-overview") return;
    const node = findLayoutNode(layout(), "team-overview");
    if (node?.content?.mod !== "team-overview" || node.content.props?.role !== "overview"
        || node.content.props?.overview_layout_id !== "team-overview") return;
    this.overviewResizeWanted = { width, height };
    if (node.size?.px === height) {
      this.overviewResizeFailureKey = null;
      if (this.overviewResizeError) { this.overviewResizeError = null; cx.notify(); }
      return;
    }
    const key = JSON.stringify([width, height, node.max ?? null]);
    if (this.overviewResizePending || this.overviewResizeFailureKey === key) return;
    if (Number.isFinite(node.max) && node.max < height) {
      this.overviewResizeFailureKey = key;
      this.overviewResizeError = "상단 보드 크기를 조정하지 못했습니다.";
      console.error("team-overview layout height exceeds max", { width, height, max: node.max });
      cx.notify(); return;
    }
    this.overviewResizePending = true;
    cx.spawn(async cx => {
      try {
        const result = await exec("herdr-app", ["do", "layout.resize", JSON.stringify({ id: "team-overview", px: height })]);
        if (result.code !== 0) throw new Error(result.stderr || `종료 코드 ${result.code}`);
        const reply = result.stdout.trim() ? JSON.parse(result.stdout) : null;
        if (reply?.error || reply?.ok === false) throw new Error(reply.error || "레이아웃 변경이 거절됐습니다.");
        this.overviewResizeFailureKey = null;
        this.overviewResizeError = null;
      } catch (error) {
        this.overviewResizeFailureKey = key;
        this.overviewResizeError = "상단 보드 크기를 조정하지 못했습니다.";
        console.error("team-overview layout resize failed", { width, height, max: node.max }, error);
      } finally {
        this.overviewResizePending = false;
        if (!this.closed) {
          cx.notify();
          const wanted = this.overviewResizeWanted;
          if (wanted.width !== width || wanted.height !== height) this.syncOverviewHeight(wanted.width, wanted.height, cx);
        }
      }
    });
  }

  render(cx) {
    const t = theme(), data = this.data;
    const button = (id, text, fn, disabled = false) => div().id(id).px(10).py(6).rounded(6).cursor_pointer().bg(disabled ? t.chrome : t.raised).text_color(disabled ? t.dim : t.foreground).on_click((_e, cx) => { cx.stop_propagation?.(); if (!disabled) fn(cx); }).child(text);
    const badge = (text, color = t.muted) => div().flex_none().whitespace_nowrap().h(20).line_height("20px").px(7).rounded(5).bg(t.raised).text_size(11).font_semibold().text_color(color).child(text);
    const statusBadge = (label, status) => badge(label, t[status]).bg(t[status]).text_color("#000000");
    // Flowing text: soft-wraps at spaces, and a very long path or id breaks after its separators.
    const words = value => v_flex().min_w(0).gap(2).children(display(value).replaceAll("장부", "작업 기록").split("\n").map(line =>
      div().min_w(0).max_w_full().whitespace_normal().child(line.trim() ? softBreaks(line) : "\u00a0")));
    const openLink = (url, cx) => {
      try {
        const target = new URL(url);
        if (target.protocol === "https:" || target.protocol === "http:") { cx.open_url(target.href); return; }
      } catch (_) {}
      this.message = "이 링크는 이 화면에서 열 수 없습니다."; cx.notify();
    };
    // Purpose / task goal read as one continuous Markdown body (headings, lists, code, links), same font as the design.
    const readable = (id, source) => TextView.markdown(`${id}-${shortHash(source)}`, documentPreview(display(source))).selectable(true).scrollable(false)
      .w_full().min_w(0).max_w(700).flex_none().font_family(this.config?.document_font || "Pretendard").font_normal().text_size(16).line_height("24px").on_link_click(openLink);
    const pair = (a, b, scroll = false) => h_flex().gap(6).items_start()
      .child(div().flex_none().whitespace_nowrap().text_color(t.muted).child(a))
      .child(scroll ? div().flex_1().min_w(0).overflow_x_scrollbar().whitespace_nowrap().child(display(b)) : words(b).flex_1().min_w(0));
    const boardStatus = (kind, card) => {
      const key = `${kind}-${card.id}`, current = card.board?.manual_status ?? "auto";
      const labels = { auto: "자동", waiting: "대기", progress: "진행 중", complete: "완료" };
      const choices = [["restore", "auto"], ["waiting", "waiting"], ["progress", "progress"], ["done", "complete"]];
      const disabled = this.boardBusy || Boolean(this.error) || !this.data?.actions_enabled;
      const panel = v_flex().id(`board-status-menu-${key}`).w(250).p(10).gap(6).rounded(8).bg(t.surface)
        .child(div().font_semibold().child("보드 상태 변경"))
        .child(words("카드 위치만 바꿉니다. 실제 실행·승인은 그대로입니다.").text_size(12).text_color(t.muted))
        .children(choices.map(([action, status]) => button(`board-status-${key}-${status}`, `${current === status ? "●" : "○"} ${labels[status]}`, cx => cx.spawn(async cx => this.organizeCard(kind, card, action, cx)), disabled)))
        .child(button(`board-status-close-${key}`, "닫기", cx => { this.boardStatusOpen = null; cx.notify(); }));
      return div().on_click((_e, cx) => cx.stop_propagation?.()).child(Popover.new(`board-status-popover-${key}`).open(this.boardStatusOpen === key).anchor("top_left").overlay_closable(true)
        .on_open_change((open, cx) => { if (!open && this.boardStatusOpen === key) this.boardStatusOpen = null; else if (open && !disabled) this.boardStatusOpen = key; cx.notify(); })
        .trigger(div().id(`board-status-trigger-${key}`).px(10).py(6).rounded(6).bg(t.raised).cursor_pointer().text_color(disabled ? t.dim : t.foreground).child(`상태: ${labels[current]} ▾`))
        .content(panel));
    };
    const liveAgents = agents(), liveTerminals = terminals(), teams = data?.teams ?? [], works = data?.works ?? [], inventory = data?.inventory ?? [], accounts = data?.accounts ?? [];
    const liveFor = team => {
      const agent = liveAgents.find(a => a.address === team.terminal || a.pane === team.terminal);
      const terminal = liveTerminals.find(x => x.address === team.terminal);
      // Core done is a turn result; agents().status also tracks whether this screen saw it.
      const raw = team.app_state ?? terminal?.status ?? null;
      const status = team.identity_verified === false ? "unknown" : team.context_handoff?.status ?? agent?.status ?? (raw === "done" ? "unknown" : raw);
      return { agent, terminal, status, present: Boolean(team.terminal && (agent || terminal)) };
    };
    const appStates = ["blocked", "working", "done", "idle"];
    const appCounts = teams.reduce((n, team) => { const status = liveFor(team).status; if (appStates.includes(status)) n[status]++; return n; }, { blocked: 0, working: 0, done: 0, idle: 0 });
    const unknownTeams = teams.filter(team => !appStates.includes(liveFor(team).status)).length;
    const statusLabels = { blocked: "Needs you", working: "Working", done: "Done", idle: "Idle" };
    const statusControl = status => {
      const matches = teams.filter(team => liveFor(team).status === status);
      const open = this.statusOpen === status;
      const panelHeight = matches.length ? Math.min(320, 44 + matches.length * 52) : 88;
      const panel = v_flex().id(`status-panel-${status}-${this.statusEpoch}`).w(320).h(open && this.statusRevealed ? panelHeight : 0)
        .opacity(open && this.statusRevealed ? 1 : 0).overflow_hidden()
        .transition("height", { duration: 220, easing: "ease-out" })
        .transition("opacity", { duration: 150, easing: "ease-out" })
        .rounded(8).border_1().border_color(t[status]).bg(t.surface).text_color(t.foreground)
        .font_family(settings().ui_font || "Geist").text_size(12)
        .child(h_flex().h(36).flex_none().px(10).items_center().justify_between()
          .child(div().font_semibold().text_color(t[status]).child(`${statusLabels[status]} · ${matches.length}`))
          .child(button(`close-status-${status}`, "닫기", cx => this.setStatusOpen(status, false, cx)).text_size(10)))
        .child(v_flex().flex_1().min_h(0).gap(4).px(6).pb(6).overflow_y_scrollbar()
          .children(matches.map(team => v_flex().id(`status-team-${team.id}`).h(48).flex_none().px(8).justify_center().rounded(6)
            .cursor_pointer().hover(el => el.bg(t.raised))
            .on_click((_e, cx) => {
              this.setStatusOpen(status, false, cx);
              run("agent.focus", { terminal: team.terminal });
            })
            .child(div().whitespace_nowrap().font_semibold().text_size(14).child(display(team.label)))
            .child(div().whitespace_nowrap().text_size(11).text_color(t.muted).child(teamPresence(status, team.ledger_state, team.id, data?.observed_at, team.context_handoff).title))))
          .when(matches.length === 0, el => el.child(div().p(10).text_size(12).text_color(t.muted).child("해당 상태의 팀장이 없습니다."))));
      return Popover.new(`status-popover-${status}`).open(open).anchor("top_left").overlay_closable(true)
        .on_open_change((next, cx) => this.setStatusOpen(status, next, cx))
        .trigger(statusBadge(`${statusLabels[status]} ${appCounts[status]}`, status).cursor_pointer())
        .content(panel);
    };
    const waiting = [...new Set([...teams.map(x => x.reason), ...works.map(x => x.reason)]
      .filter(Boolean).map(display))];
    const summary = `업무 중 ${appCounts.working} · 확인 필요 ${appCounts.blocked}`;
    const coordinator = data?.coordinator;
    const teamCard = team => {
      const live = liveFor(team), status = live.status, presence = teamPresence(status, team.ledger_state, team.id, data?.observed_at, team.context_handoff);
      const activate = cx => run("agent.focus", { terminal: team.context_handoff?.new_terminal ?? team.terminal });
      return v_flex().id(`rail-${team.id}`).flex_1().min_w(0).h(OVERVIEW.card).gap(3).p(8).rounded(7).hover(s => s.bg(t.raised))
        .when(live.present || Boolean(team.context_handoff?.new_terminal), el => el.cursor_pointer().on_click((_e, cx) => activate(cx)))
        .bg(t.surface)
        .child(h_flex().h(28).flex_none().gap(6).items_center()
          .child(div().h(28).line_height("28px").truncate().font_semibold().text_size(19).child(display(team.label))))
        .child(h_flex().h(20).flex_none().child(h_flex().h(20).flex_none().items_center().gap(6).px(7).rounded(5).bg(t.raised).text_size(11).font_semibold().text_color(statusColor(status, t))
          .child(div().whitespace_nowrap().child(presence.title))
          .when(status === "working" && live.present && !this.error && !data?.native_error, el => el.child(svg("mods/workspace-chrome/spinner.svg").size(12).flex_none().text_color(t.working).spin(900)))))
        .child(div().h(20).flex_none().line_height("20px").truncate().text_size(12).text_color(t.muted).tooltip(presence.detail).child(presence.detail));
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
    const overviewColumns = overviewColumnsFor(this.overviewWidth ?? 0);
    const overviewRows = [];
    for (let index = 0; index < teams.length; index += overviewColumns) overviewRows.push(teams.slice(index, index + overviewColumns));
    const overviewRailHeight = Math.max(1, overviewRows.length) * OVERVIEW.card + Math.max(0, overviewRows.length - 1) * OVERVIEW.gap + 4;
    if (overview) return v_flex().id("team-operations-overview").w_full().min_w(0).h_auto().min_h(overviewRailHeight + OVERVIEW.height - OVERVIEW.rail).flex_none().gap(4).px(OVERVIEW.padding).py(5).bg(t.background).text_color(t.foreground)
      .font_family(this.config?.overview_font || settings().font_family || "monospace").text_size(13)
      .on_size_change((size, cx) => {
        const width = Math.floor(size.width);
        if (this.overviewWidth !== width) { this.overviewWidth = width; cx.notify(); }
        const rows = Math.max(1, Math.ceil(teams.length / overviewColumnsFor(width)));
        const railHeight = rows * OVERVIEW.card + (rows - 1) * OVERVIEW.gap + 4;
        const overhead = Math.max(OVERVIEW.height - OVERVIEW.rail, Math.ceil(size.height) - overviewRailHeight);
        this.syncOverviewHeight(width, railHeight + overhead, cx);
      })
      .child(h_flex().min_h(32).flex_none().flex_wrap().items_center().gap(10)
        .child(div().font_semibold().text_size(16).child("팀 운영"))
        .child(div().flex_1().text_size(12).text_color(t.muted).child(summary))
        .when(Boolean(data?.context_handoff?.enabled), el => el.child(badge(data.context_handoff.label, data.context_handoff.healthy ? t.muted : t.blocked)))
        .child(coordinatorButton)
        .child(button("open-dashboard", "전체 현황", () => run("tab.select", { id: this.config?.dashboard_tab }), !this.config?.dashboard_tab).text_size(12)))
      .when(Boolean(this.coordinatorMessage), el => el.child(div().h(16).flex_none().line_height("16px").text_size(10).text_color(/⚠|실패|오류/i.test(this.coordinatorMessage) ? t.blocked : t.muted).truncate().child(this.coordinatorMessage)))
      .when(Boolean(this.overviewResizeError), el => el.child(div().h(16).flex_none().line_height("16px").truncate().text_size(10).text_color(t.blocked).tooltip(this.overviewResizeError).child(this.overviewResizeError)))
      .when(this.coordinatorConfirm, el => el.child(coordinatorConfirm))
      .when(!this.coordinatorConfirm, el => el.child(v_flex().id("team-operations-rail").w_full().h(overviewRailHeight).flex_none().min_w(0).gap(OVERVIEW.gap)
        .children(overviewRows.map((row, index) => h_flex().id(`team-operations-rail-row-${index}`).w_full().min_w(0).h(OVERVIEW.card).flex_none().gap(OVERVIEW.gap).children(row.map(teamCard))))));
    const overlay = this.viewProps.overlay ?? getModProps()?.overlay;
    if (this.role === "choice") {
      const choice = this.choiceContext();
      const approvalReady = choice?.intent !== "approve" || Boolean(this.doc?.can_approve && !this.docBusy);
      const ready = Boolean(choice && !choice.blocked && approvalReady && this.cardData?.can_answer && this.data?.actions_enabled
        && !this.cardError && !this.error && !this.configError && !this.stale && !this.choiceBusy && !this.choiceStarted);
      const message = this.cardError ?? this.error ?? this.configError ?? this.message ?? this.answerMessage ?? this.sendMessage;
      return v_flex().id("choice-preview").size_full().min_h(0).gap(14).p(20).bg(t.background).text_color(t.foreground)
        .font_family(settings().ui_font || "Geist").text_size(13).line_height("21px")
        .child(h_flex().items_center().justify_between().gap(8)
          .child(div().font_semibold().text_size(16).child(choice?.label ?? "선택 확인"))
          .child(button("choice-close", "닫기", () => closeOverlay(overlay))))
        .child(v_flex().id("choice-description").flex_1().min_h(0).gap(10).overflow_y_scroll()
          .when(Boolean(choice), el => el
            .child(pair(choice.intent === "approve" ? "다음 담당" : "받는 사람", choice.recipient))
            .child(words(choice.description))
            .child(words(choice.next).text_color(t.muted))
            .when(Boolean(choice.blocked), x => x.child(words(choice.blocked).text_color(t.blocked))))
          .when(!choice, el => el.child(words(this.cardBusy || !this.cardData ? "선택 내용을 확인하고 있습니다." : "카드나 선택지가 바뀌었습니다. 닫고 최신 카드에서 다시 선택해 주세요.").text_color(t.muted)))
          .when(this.docBusy, el => el.child(words("승인할 설계 버전을 확인하고 있습니다.").text_color(t.muted)))
          .when(Boolean(choice?.intent === "approve" && this.doc && !this.doc.can_approve), el => el.child(words("현재는 이 설계를 승인할 수 없습니다. 최신 상태를 확인해 주세요.").text_color(t.blocked)))
          .when(Boolean(message), el => el.child(words(message).text_size(12).text_color(t.blocked)))
          .when(Boolean(this.sendResult), el => el.child(words(this.sendResult.label).text_color(t.accent))))
        .child(h_flex().flex_none().gap(8)
          .child(button("choice-confirm", this.choiceBusy ? "처리 중" : choice?.intent === "approve" ? "이 설계 승인" : choice?.intent === "confirmed" ? "확인" : "이 요청 보내기", cx => cx.spawn(async cx => this.confirmChoice(cx)), !ready))
          .child(button("choice-cancel", "취소", () => closeOverlay(overlay))));
    }
    if (this.role === "detail") {
      const detail = this.detailData;
      const card = this.cardData;
      const detailWork = detail ?? card ?? this.viewProps.item ?? { id: this.viewProps.workId };
      const designReview = this.viewProps.kind === "work" && (card?.status ?? detail?.phase ?? detailWork.phase) === "awaiting_approval";
      const tasks = detail?.tasks ?? [];
      const leader = detail?.leader ?? card?.leader;
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
          .when(Boolean(task.goal), x => x.child(v_flex().min_w(0).gap(3).child(div().text_color(t.muted).child("목적")).child(readable(`goal-${task.id}`, task.goal))))
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
      const docBrief = doc?.brief;
      const briefPart = (id, title, content) => v_flex().min_w(0).gap(7).pt(12)
        .child(div().font_semibold().text_size(14).child(title))
        .child(readable(`brief-${id}-${doc?.id}`, Array.isArray(content) ? content.map(x => `- ${x}`).join("\n") : content));
      const documentSection = v_flex().id("detail-document").min_h(0).flex_none().gap(12).pt(12).font_family(this.config?.document_font || "Pretendard").font_normal()
        .child(h_flex().flex_none().flex_wrap().items_center().justify_between().gap(8)
          .child(div().font_semibold().text_size(14).child(doc ? `설계 v${doc.revision}` : "설계 설명"))
          .child(h_flex().flex_wrap().gap(6)
            .when(Boolean(doc), el => el.children([
              button("detail-reading-brief", "쉬운 설명", cx => { this.docMode = "brief"; this.docSource = false; cx.notify(); }),
              button("detail-reading-full", "전체 설계", cx => { this.docMode = "full"; this.docSource = false; cx.notify(); }),
              button("detail-reading-contract", "범위·검증 조건", cx => { this.docMode = "contract"; this.docSource = false; cx.notify(); }),
              button("detail-toggle-source", "원문", cx => { this.docMode = "source"; this.docSource = true; cx.notify(); }),
              button("detail-close-document", "문서 닫기", cx => this.closeDocument(cx))
            ]))))
        .when(Boolean(this.message), x => x.child(words(this.message).text_color(t.blocked)))
        .when(this.docBusy, x => x.child(words("현재 버전의 설계를 읽는 중입니다.").text_color(t.muted)))
        .when(Boolean(docBrief) && this.docMode === "brief", x => x.child(v_flex().id("reading-brief").min_w(0).max_w(700).gap(10)
          .when(docBrief.summary !== (card?.presentation?.summary ?? card?.brief?.summary), el => el.child(readable("brief-summary", docBrief.summary)))
          .child(briefPart("purpose", "왜 필요한가요?", docBrief.purpose))
          .child(briefPart("changes", "달라지는 점", docBrief.changes))
          .child(briefPart("checks", "어떻게 확인하나요?", docBrief.checks))
          .child(briefPart("impact", "영향과 범위", docBrief.impact))
          .child(briefPart("decision", "내가 결정할 내용", docBrief.decision_explanation))
          .when(doc.brief_state !== "current", el => el.child(words("현재 설계의 요구사항과 검증 조건으로 정리한 설명입니다.").text_size(12).text_color(t.muted)))))
        .when(Boolean(doc) && this.docMode === "full", x => x.child(readable(`design-preview-${doc.id}-${doc.revision}-${doc.digest}`, doc.body)))
        .when(Boolean(doc) && this.docMode === "contract", x => x.child(v_flex().min_w(0).max_w(700).gap(10)
          .child(briefPart("paths", "변경할 수 있는 범위", doc.contract?.allowed_paths ?? []))
          .child(briefPart("requirements", "지켜야 할 조건", doc.contract?.requirements ?? []))
          .child(briefPart("steps", "완료를 확인할 단계", (doc.contract?.steps ?? []).map(step => step.title)))))
        .when(this.docEditor != null && this.docMode === "source", x => x.child(Editor.new(this.docEditor).h(420).min_h(0)))
        .when(Boolean(doc) && !this.confirm && selectedActionAllowed, x => x.child(approvalButtons))
        .when(Boolean(this.confirm), x => x.child(confirmBox));
      const shownStatus = card?.status_label ?? phaseText(detail?.phase ?? detailWork.phase ?? detailWork.status);
      const detailCategory = h_flex().flex_wrap().gap(8).text_size(11).text_color(t.muted)
        .child(div().whitespace_nowrap().child(`${this.viewProps.kind === "inventory" ? "운영 환경 구축" : "제품 작업"} · ${display(detailWork.id)}`))
        .child(badge(shownStatus, /확인|승인 대기|검토|오류|실패/.test(shownStatus) ? t.blocked : /완료/.test(shownStatus) ? t.done : t.working));
      const titleBlock = v_flex().flex_1().min_w(0).gap(3)
        .child(div().min_w(0).whitespace_normal().text_size(15).line_height("22px").font_semibold().child(softBreaks(display(card?.presentation?.title ?? this.viewProps.item?.presentation?.title ?? (this.viewProps.kind === "inventory" ? detailWork.name ?? detailWork.title ?? detailWork.id : detail?.title ?? detailWork.title ?? detail?.name ?? detailWork.id)))));
      const detailHeader = h_flex().min_h(48).flex_none().items_center().justify_between().gap(10)
        .child(titleBlock)
        .child(button("detail-close", "닫기", () => closeOverlay(overlay)));
      const leaderIsLive = Boolean(!this.detailError && !this.error && leader?.terminal && (liveAgents.some(agent => agent.address === leader.terminal || agent.pane === leader.terminal)
        || liveTerminals.some(terminal => terminal.address === leader.terminal || terminal.name === leader.terminal)));
      const answer = card?.answer;
      const q = card?.question, qState = card?.question_state ?? { state: "missing", reason: "담당자가 결정 질문을 준비하지 않았습니다." };
      const canSend = Boolean(card?.can_answer) && !this.cardError;
      const current = this.sendResult && this.sendRequestId === this.sendResult.request_id ? this.sendResult : null;
      const sendLocked = current != null && ["pending", "sending", "sent", "unknown"].includes(current.state);
      const sendLabel = (idle) => this.sendBusy ? "보내는 중" : current?.state === "sent" ? "보냈습니다" : ["pending", "sending"].includes(current?.state) ? "전송 확인 중" : current?.state === "unknown" ? "결과 확인 필요" : idle;
      const sendColor = state => state === "sent" ? t.accent : state === "pending" || state === "sending" ? t.working : t.blocked;
      const optionButton = (id, number, label, description) => {
        const picked = this.form.option === id;
        return v_flex().id(`question-option-${id}`).min_w(0).gap(2).p(8).rounded(6).border_1().border_color(picked ? t.accent : t.border).bg(picked ? t.raised : t.chrome)
          .when(canSend && !this.sendBusy, el => el.cursor_pointer().on_click(() => this.openChoice("question", id)))
          .child(div().min_w(0).font_semibold().whitespace_normal().child(`${picked ? "●" : "○"} ${number}. ${label}`))
          .when(Boolean(description), el => el.child(words(description).text_size(11).text_color(t.muted)));
      };
      const questionSection = q && (card?.presentation ? card.presentation.decision_required : card?.interaction ? card.needs_decision : true)
        ? v_flex().id("decision-question").flex_none().min_w(0).gap(7).p(10).rounded(7).bg(t.surface).border_1().border_color(t.accent)
          .child(div().text_size(11).font_semibold().text_color(t.accent).child("결정 질문"))
          .child(words(q.prompt).text_size(14).font_semibold())
          .when(!canSend, el => el.child(words("완료되었거나 답변 기능이 꺼져 있어 보낼 수 없습니다. 질문과 이전 기록은 계속 볼 수 있습니다.").text_size(11).text_color(t.muted)))
          .child(v_flex().min_w(0).gap(6)
            .children(q.options.map((option, index) => optionButton(option.option_id, index + 1, option.label, option.description))))
        : null;
      const preserved = this.preservedKeys.filter(key => DRAFTS.has(key)).map(key => {
        const draft = DRAFTS.get(key);
        return v_flex().min_w(0).gap(3).p(8).rounded(6).bg(t.chrome)
          .child(div().text_size(11).font_semibold().text_color(t.muted).child("이전 질문·카드에 쓴 내용 · 보내지 않고 보존함"))
          .child(words([draft.other, draft.comment].filter(x => x?.trim()).join("\n")).text_size(11))
          .child(button(`discard-draft-${shortHash(key)}`, "이 내용 지우기", cx => { DRAFTS.delete(key); this.preservedKeys = this.preservedKeys.filter(k => k !== key); cx.notify(); }).text_size(10));
      });
      const needsDecision = card?.presentation ? Boolean(card.presentation.decision_required) : card?.interaction ? Boolean(card.needs_decision) : Boolean(q || card?.needs_decision);
      const summary = card?.presentation?.summary ?? (card?.prompt && card.prompt !== "무엇을 확인했고 어떤 조치가 필요한가요?" ? card.prompt : card?.reason ?? card?.condition);
      const summarySection = v_flex().id("decision-summary").flex_none().min_w(0).gap(6)
        .child(div().text_size(11).font_semibold().text_color(t.muted).child(needsDecision ? "무엇을 결정하나요?" : "어떤 작업인가요?"))
        .child(readable("card-summary", summary ?? "작업 내용을 확인하고 있습니다."));
      const fallbackSection = v_flex().id("decision-options").flex_none().min_w(0).gap(8)
        .child(words(card?.decision?.prompt ?? card?.decision_reason ?? (card?.status === "awaiting_approval" ? "아래 설계 설명과 변경 범위를 읽고 이 버전의 진행 여부를 결정해 주세요." : "담당자가 이 작업에 맞는 선택지를 준비해야 합니다.")).text_size(14).font_semibold())
        .children((card?.decision?.options ?? []).map(option => button(`answer-${option.id}`, option.label, () => this.openChoice("decision", option.id), !canSend || this.choiceBusy)
          .min_w(0).whitespace_normal().text_size(13)
          .child(words(option.description).text_size(11).text_color(t.muted))));
      const progressSection = v_flex().id("card-progress").flex_none().min_w(0).gap(7)
        .child(words(card?.presentation?.waiting_on || card?.interaction?.label || shownStatus).text_color(t.accent).font_semibold())
        .child(words(card?.interaction?.reason || (card?.status === "complete" || card?.status_label === "완료" ? "완료된 작업입니다. 결과와 이력은 아래에서 볼 수 있습니다."
          : card?.status === "approved" ? "승인된 작업입니다. 담당자 배정을 기다리고 있습니다."
            : card?.status === "planning" ? "설계를 준비하고 있습니다. 검토가 필요해지면 질문이 표시됩니다."
              : "현재 사용자 결정 요청은 없습니다. 아래에 담당자와 작업 상태를 표시합니다.")).text_color(t.muted));
      const deliveries = card?.deliveries ?? [];
      const shownDeliveries = current && !deliveries.some(d => d.request_id === current.request_id) ? [current, ...deliveries] : deliveries;
      const deliverySection = v_flex().id("delivery-status").flex_none().min_w(0).gap(4).p(9).rounded(7).bg(t.surface)
        .child(div().text_size(12).font_semibold().text_color(t.accent).child("전달 상태"))
        .children(shownDeliveries.slice(0, 3).map(d => v_flex().min_w(0).gap(1)
          .child(div().min_w(0).whitespace_normal().font_semibold().text_color(sendColor(d.effective_state ?? d.state)).child(`${display(d.label)} · ${clock(d.created_at)}`))
          .when(Boolean(d.reason), el => el.child(words(d.reason).text_size(11).text_color(t.muted)))
          .when(Boolean(d.receipt), el => el.child(words(`응답 확인 · ${d.receipt?.summary ?? ""}`).text_size(11).text_color(t.accent)))
          .when(d.destination != null, el => el.child(div().min_w(0).whitespace_normal().text_size(10).text_color(t.dim).child(softBreaks(`받는 곳 ${display(d.destination.team)} · ${display(d.destination.task)}`))))))
        .when(Boolean(this.sendMessage), el => el.child(words(this.sendMessage).text_size(11).text_color(this.sendMessage.startsWith("⚠") ? t.blocked : t.muted)))
        .child(div().text_size(10).text_color(t.dim).whitespace_normal().child("‘전송 확인됨’은 전달 통로를 통과했다는 뜻입니다. 팀장의 내용 수신·실행 시작·설계 승인·완료와는 별개로 확인합니다."));
      const technical = v_flex().flex_none().min_w(0).gap(4).p(8).rounded(6).bg(t.chrome)
        .child(button("toggle-technical", this.techOpen ? "기술 내역 접기" : "기술 내역 보기", cx => { this.techOpen = !this.techOpen; cx.notify(); }).text_size(10))
        .when(this.techOpen, el => el
          .child(pair("카드 버전", card?.revision ?? DUMMY)).child(pair("문서", `v${display(card?.document_revision)} · ${display(card?.document_digest)}`))
          .child(pair("질문", q ? `${q.question_id} · v${q.question_revision}` : display(qState.state)))
          .child(pair("요청 ID", this.sendRequestId ?? current?.request_id ?? DUMMY))
          .child(pair("보존 키", this.form.key ?? DUMMY))
          .child(div().text_size(10).text_color(t.dim).whitespace_normal().child("입력 중인 내용은 이 앱을 켜 둔 동안만 보존됩니다. 앱을 다시 시작하면 복원되지 않습니다.")));
      const extraButtons = h_flex().flex_none().flex_wrap().gap(6)
        .when(Boolean(card?.board) && card.board.disposition !== "archived", el => el.child(boardStatus(card.kind, card)))
        .when(this.viewProps.kind !== "inventory" || Boolean(this.viewProps.linkedWorkId), el => el.child(button("review-open-design", this.docBusy ? "읽는 중" : this.docEditor ? "설계 닫기" : designReview ? "설계·승인 보기" : "설계 보기", cx => {
          if (this.docEditor) this.closeDocument(cx);
          else cx.spawn(async cx => this.document(detailWork, cx));
        }, this.docBusy || this.actionBusy)))
        .child(button("detail-toggle-info", this.infoOpen ? "상세 정보 접기" : "상세 정보", cx => { this.infoOpen = !this.infoOpen; cx.notify(); }))
        .child(button("detail-toggle-history", this.historyOpen ? "기록 접기" : "기록", cx => { this.historyOpen = !this.historyOpen; cx.notify(); }))
        .when(Boolean(card?.board), el => el
          .when(card.board.disposition === "visible", x => x.children([
            button("detail-board-done", "보드에서 정리 완료", cx => cx.spawn(async cx => this.organizeCard(card.kind, card, "done", cx)), this.boardBusy),
            button("detail-board-archive", "보관", cx => cx.spawn(async cx => this.organizeCard(card.kind, card, "archive", cx)), this.boardBusy)]))
          .when(card.board.disposition !== "visible", x => x.child(button("detail-board-restore", "보드에 복원", cx => cx.spawn(async cx => this.organizeCard(card.kind, card, "restore", cx)), this.boardBusy))));
      const informationSection = v_flex().id("detail-information").flex_none().min_w(0).gap(8)
        .child(detailCategory)
        .child(v_flex().flex_none().gap(4)
          .child(div().text_size(12).font_semibold().child(this.viewProps.kind === "inventory" ? "확인할 내용" : "작업 범위"))
          .child(readable(`purpose-${detailWork.id}`, card?.reason ?? detail?.reason ?? detail?.goal ?? detail?.purpose ?? detail?.description ?? detailWork.reason ?? detailWork.condition ?? detailWork.title)))
        .when(Boolean(card?.prompt && card.prompt !== "무엇을 확인했고 어떤 조치가 필요한가요?"), el => el.child(words(card.prompt)))
        .when(!q, el => el.child(words(qState.reason).text_size(11).text_color(t.muted)))
        .when(Boolean(card?.previous_question), el => el.child(words(`이전 질문: ${card.previous_question.prompt}`).text_size(11).text_color(t.muted)))
        .child(pair("담당", card?.team_label ?? leader?.label ?? "담당 팀장 미지정"))
        .when(leaderIsLive, el => el.child(button("focus-leader", "팀장 이동", () => { closeOverlay(overlay); run("agent.focus", { terminal: leader.terminal }); })))
        .when(tasks.length > 0, el => el.child(v_flex().flex_none().gap(5)
          .child(div().font_semibold().text_size(12).text_color(t.accent).child(`연결된 세션 · ${tasks.length}`)).children(detailSessions)))
        .when(detail?.native_error, el => el.child(words(detail.native_error).text_size(11).text_color(t.blocked)))
        .child(technical);
      const historySection = v_flex().id("detail-history").flex_none().min_w(0).gap(8)
        .children(preserved)
        .when(shownDeliveries.length > 0, el => el.child(deliverySection))
        .when(Boolean(answer), el => el.child(v_flex().flex_none().gap(3)
          .child(div().font_semibold().text_color(t.accent).child(`저장된 답변 · ${(card?.choices ?? []).find(c => c.id === answer.choice)?.label ?? display(answer.choice)}`))
          .when(Boolean(answer.text), x => x.child(words(answer.text))).child(div().text_size(10).text_color(t.dim).child(clock(answer.saved_at)))))
        .when(!answer && Boolean(card?.previous_answer), el => el.child(v_flex().gap(3)
          .child(div().text_color(t.muted).child("이전 카드에 남긴 답변 · 현재 내용은 다시 확인해 주세요."))
          .child(words(card.previous_answer.text || (card.choices ?? []).find(c => c.id === card.previous_answer.choice)?.label || card.previous_answer.choice))))
        .when(!preserved.length && !shownDeliveries.length && !answer && !card?.previous_answer, el => el.child(div().text_color(t.muted).child("아직 기록이 없습니다.")));
      const body = v_flex().id("detail-body").size_full().min_h(0).gap(9).overflow_y_scroll()
        .when(this.detailBusy || this.cardBusy && !card && !this.cardError, x => x.child(div().p(8).text_color(t.dim).child("카드 정보를 읽는 중입니다.")))
        .when(Boolean(this.detailError || this.cardError), x => x.child(words(`정보를 읽지 못했습니다 · ${this.detailError ?? this.cardError}`).text_color(t.blocked)))
        .when(Boolean(card), el => el.child(summarySection).child(needsDecision ? (q ? questionSection : fallbackSection) : progressSection))
        .when(Boolean(this.sendMessage), el => el.child(words(this.sendMessage).text_size(11).text_color(this.sendMessage.startsWith("⚠") ? t.blocked : t.muted)))
        .when(Boolean(current), el => el.child(words(current.label ?? sendLabel("전달 상태")).text_size(11).text_color(sendColor(current.state))))
        .when(Boolean(this.answerMessage), el => el.child(words(this.answerMessage).text_size(11).text_color(this.answerMessage.startsWith("⚠") ? t.blocked : t.accent)))
        .when(Boolean(card?.presentation), el => el.child(v_flex().id("card-current-work").gap(6).py(10)
          .child(pair("담당", card.presentation.owner_label))
          .child(pair("지금", card.presentation.current_activity))
          .child(pair("작업 세션", (card.presentation.sessions ?? []).filter(s => s.terminal).map(s => `${s.role_label} · ${s.terminal}`).join(" / ") || "현재 설계에 연결된 세션 없음"))
          .children((card.presentation.sessions ?? []).filter(s => s.can_focus).map(s => button(`current-session-${s.task}`, `${s.role_label} 세션 열기`, () => run("agent.focus", {terminal:s.terminal}))))))
        .when(Boolean(this.boardMessage), el => el.child(words(this.boardMessage).text_size(12).text_color(t.muted)))
        .when(Boolean(card), el => el.child(extraButtons))
        .when((this.viewProps.kind !== "inventory" || Boolean(this.viewProps.linkedWorkId)) && (this.docBusy || this.docEditor != null || Boolean(this.message)), el => el.child(documentSection))
        .when(this.infoOpen, el => el.child(informationSection))
        .when(this.historyOpen, el => el.child(historySection));
      return v_flex().id("team-operations-detail").size_full().min_h(0).gap(8).p(14).bg(t.background).text_color(t.foreground)
        .font_family(settings().ui_font || "Geist").text_size(12).line_height("18px")
        .on_size_change((size, cx) => {
          const height = Math.floor(size.height);
          if (this.detailHeight !== height) { this.detailHeight = height; cx.notify(); }
        })
        .child(detailHeader)
        .child(v_flex().relative().h(Math.max(0, (this.detailHeight ?? 440) - 84)).flex_none().min_h(0)
          .child(body)
          .child(Scrollbar.vertical("detail-body").absolute().inset_0().mode("always")));
    }
    const narrow = (this.viewWidth ?? Infinity) < BOARD_NARROW;
    const boardColumns = [
      { id: "waiting", title: "담당자 대기", color: t.idle },
      { id: "progress", title: "진행 중", color: t.working },
      { id: "review", title: "내 결정 필요", color: t.blocked },
      { id: "complete", title: "완료·정리", color: t.done }
    ];
    const asCard = (kind, raw) => {
      const p = raw.presentation ?? {};
      const original = kind === "work" ? WORK_PHASES[raw.phase]?.column ?? "waiting" : String(raw.status ?? "").startsWith("완료") ? "complete" : "waiting";
      const disposition = raw.board?.disposition ?? "visible";
      const manual = raw.board?.manual_status;
      const category = manual || (disposition === "done" ? "complete" : p.column ?? original);
      const state = manual ? `직접 변경 · ${{waiting:"대기",progress:"진행 중",complete:"완료"}[manual]}` : disposition === "done" ? "사용자가 보드에서 정리 완료" : p.decision_required ? "사용자 결정 대기" : p.waiting_on || raw.interaction?.label || (kind === "work" ? phaseText(raw.phase) : statusText(raw.status));
      return {kind,id:raw.id,raw,p,disposition,category,state,title:p.title || raw.title || raw.name};
    };
    const workCards = works.map(x => asCard("work",x)), inventoryCards = inventory.map(x => asCard("inventory",x));
    const allCards = [...workCards, ...inventoryCards];
    const archived = allCards.filter(card => card.disposition === "archived");
    const cardsInView = this.showArchived ? archived : allCards.filter(card => card.disposition !== "archived");
    const completedCount = cardsInView.filter(card => card.category === "complete").length;
    const remaining = inventoryCards.filter(card => card.category !== "complete" && card.disposition !== "archived").length;
    const boardCard = card => {
      const sessions = (card.p.sessions ?? []).filter(s => s.terminal);
      const attention = card.p.attention ?? [];
      return v_flex().id(`board-${card.kind}-${card.id}`).flex_none().min_w(0).gap(10).p(12).rounded(8).bg(t.surface)
        .hover(el => el.bg(t.raised)).cursor_pointer().on_click(() => this.openDetail(card.kind,card.raw,card.category === "review" && card.kind === "work"))
        .child(div().min_w(0).whitespace_normal().font_semibold().text_size(14).line_height("22px").child(display(card.title)))
        .when(Boolean(card.p.summary) && card.p.summary !== card.title, el => el.child(words(card.p.summary.replaceAll("`", "")).text_size(13).line_height("21px").text_color(t.muted)))
        .child(div().min_w(0).whitespace_normal().text_size(12).text_color(card.category === "review" ? t.blocked : card.category === "complete" ? t.done : t.foreground).child(card.state))
        .when(Boolean(card.raw.board?.manual_status) && card.p.decision_required, el => el.child(words("사용자 결정은 아직 필요합니다. ‘결정하기’에서 확인하세요.").text_size(12).text_color(t.blocked)))
        .child(h_flex().flex_wrap().gap_x(14).gap_y(6).text_size(12)
          .child(pair("담당", card.p.owner_label || card.raw.team || "팀 총괄"))
          .child(pair("지금", (card.p.current_activity || "현재 실행 기록 확인 중").split(" · 작업 내용: ")[0]))
          .child(pair("세션", sessions.length ? sessions.map(s => `${s.role_label} · ${s.terminal}`).join(" / ") : "현재 설계에 연결된 세션 없음")))
        .when(attention.length > 0, el => el.child(words(`담당자 확인 · ${attention[0].reason}`).text_size(12).text_color(t.muted)))
        .when(card.disposition === "done", el => el.child(words("보드 정리 상태입니다. 실제 업무 완료·승인은 별도로 유지됩니다.").text_size(11).text_color(t.muted)))
        .child(h_flex().flex_wrap().gap(6)
          .when(Boolean(card.raw.board) && card.disposition !== "archived", el => el.child(boardStatus(card.kind, card.raw)))
          .child(button(`open-card-${card.kind}-${card.id}`, card.p.decision_required ? "결정하기" : "자세히", () => this.openDetail(card.kind,card.raw,card.p.decision_required && card.kind === "work")))
          .children(sessions.filter(s => s.can_focus).map(s => button(`open-session-${card.id}-${s.task}`, "세션 열기", () => run("agent.focus",{terminal:s.terminal}))))
          .when(Boolean(card.raw.board), el => el
            .when(card.disposition === "visible" && card.category !== "complete", x => x.child(button(`done-card-${card.kind}-${card.id}`, "정리 완료", cx => cx.spawn(async cx => this.organizeCard(card.kind,card.raw,"done",cx)), this.boardBusy)))
            .when(card.disposition !== "archived", x => x.child(button(`archive-card-${card.kind}-${card.id}`, "보관", cx => cx.spawn(async cx => this.organizeCard(card.kind,card.raw,"archive",cx)), this.boardBusy)))
            .when(card.disposition !== "visible", x => x.child(button(`restore-card-${card.kind}-${card.id}`, "복원", cx => cx.spawn(async cx => this.organizeCard(card.kind,card.raw,"restore",cx)), this.boardBusy)))));
    };
    const column = definition => {
      const cards = cardsInView.filter(card => card.category === definition.id);
      const visible = definition.id === "complete" && !this.showCompleted && !this.showArchived ? [] : cards;
      return v_flex().id(`kanban-${definition.id}`).min_w(0).gap(10).p(8).rounded(8).bg(t.chrome)
        .when(narrow, el => el.w_full().flex_none())
        .when(!narrow, el => el.h_full().min_h(0).when(visible.length === 0, x => x.w(165).flex_none()).when(visible.length > 0, x => x.flex_1()))
        .child(h_flex().flex_none().items_center().justify_between().gap(6)
          .child(h_flex().items_center().gap(6).child(div().w(7).h(7).rounded_full().bg(definition.color)).child(div().whitespace_nowrap().font_semibold().text_size(13).child(definition.title)).child(badge(String(cards.length),cards.length ? definition.color : t.muted)))
          .when(definition.id === "complete" && cards.length > 0, el => el.child(button("toggle-completed",this.showCompleted ? "접기" : "보기",cx=>{this.showCompleted=!this.showCompleted;cx.notify();}).text_size(11))))
        .child(v_flex().min_w(0).gap(10).when(!narrow, el => el.flex_1().min_h(0).overflow_y_scrollbar()).children(visible.map(boardCard)))
        .when(cards.length === 0, el => el.child(words(definition.id === "review" ? "지금 결정할 요청이 없습니다." : definition.id === "progress" ? "실행 중으로 확인된 작업이 없습니다." : "이 상태의 카드가 없습니다.").text_size(12).text_color(t.muted)))
        .when(cards.length > 0 && !visible.length, el => el.child(words(`${cards.length}개 완료 기록을 접었습니다.`).text_size(12).text_color(t.muted)));
    };
    const accountRow = account => {
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
      return v_flex().id(key).min_w(0).gap(2).py(2).px(8).rounded(6).bg(t.surface).cursor_pointer()
        .on_click((_e, cx) => { open ? this.expanded.delete(key) : this.expanded.add(key); cx.notify(); })
        .child(h_flex().min_h(24).flex_wrap().gap(8).items_center().text_size(11)
          .child(h_flex().w(114).flex_none().gap(6).items_center()
            .when(["codex", "claude"].includes(account.provider), el => el.child(image(`icons/${account.provider}.svg`).w(16).h(16).flex_none()))
            .child(div().flex_1().min_w(0).truncate().font_semibold().child(display(account.label))))
          .child(usageCell("5시간", account.five_hour, "five_hour"))
          .child(usageCell("주간", account.weekly, "weekly"))
          .child(div().flex_1().min_w(0).truncate().child(display(account.coverage ?? statusText(account.status))))
          .child(div().w(92).flex_none().truncate().text_color(t.dim).child(duration(account.age))))
        .when(open && Boolean(meta), el => el.child(div().text_size(10).text_color(t.muted).child(meta)))
        .when(Boolean(this.usageMessage), el => el.child(div().text_size(10).text_color(/⚠|실패|오류/i.test(this.usageMessage) ? t.blocked : t.muted).child(this.usageMessage)));
    };
    const accountSections = [
      { id: "team", title: "회사", rows: accounts.filter(account => account.account === "team") },
      { id: "personal", title: "개인", rows: accounts.filter(account => account.account === "personal") },
      { id: "other", title: "기타 계정", rows: accounts.filter(account => !["team", "personal"].includes(account.account)) }
    ].filter(section => section.rows.length > 0).map(section =>
      v_flex().id(`account-section-${section.id}`).w_full().flex_none().gap(3).p(6).rounded(6).border_1().border_color(t.border)
        .child(div().whitespace_nowrap().font_semibold().text_size(12).text_color(t.foreground).child(section.title))
        .children(section.rows.map(accountRow)));
    const actionRows = (data?.actions ?? []).map(action => {
      const failed = /실패|오류|fail|error/i.test(`${action.status ?? ""} ${action.message ?? ""}`);
      return h_flex().gap(8).p(6).rounded(6).bg(t.surface).text_size(11)
        .child(div().w(72).flex_none().truncate().whitespace_nowrap().font_semibold().child(display(action.id)))
        .child(words(action.status).w(160).flex_none().text_color(failed ? t.blocked : t.muted))
        .child(words(action.message).flex_1().min_w(0));
    });
    // No horizontal scroll: columns share the window width (min_w 0) or stack vertically when it is narrow.
    const board = (narrow ? v_flex().id("operations-kanban").w_full().flex_none().gap(8) : h_flex().id("operations-kanban").w_full().min_w(0).h(Math.max(220, (this.viewHeight ?? 700) - (this.headerHeight ?? 48) - (this.controlsHeight ?? 150) - (this.accountHeight ?? 40) - 28)).flex_none().min_h(0).items_stretch().gap(8))
      .children(boardColumns.map(column));
    const accountArea = v_flex().id("account-area").on_size_change((size,cx)=>{ const h=Math.ceil(size.height); if(this.accountHeight!==h){this.accountHeight=h;cx.notify();} }).w_full().h(this.showAccounts ? 250 : 40).flex_none().min_h(0).gap(6).overflow_y_scrollbar()
      .child(h_flex().flex_none().justify_between().items_center()
        .child(button("toggle-accounts", this.showAccounts ? "계정 사용량 접기" : "계정 사용량 보기", cx => { this.showAccounts = !this.showAccounts; cx.notify(); }))
        .child(h_flex().flex_1().min_w(0).gap(10).px(12).text_size(10)
          .child(div().text_color(t.ansi2).child("<70%"))
          .child(div().text_color(t.ansi3).child("70–<90%"))
          .child(div().text_color(t.ansi1).child("≥90%"))
          .child(div().text_color(t.dim).child("지난 기록")))
        .child(button("refresh-usage", this.usageBusy || data?.usage_refreshing ? "사용량 갱신 중" : "사용량 갱신", cx => cx.spawn(async cx => this.refreshUsage(cx)), this.usageBusy || data?.usage_refreshing).text_size(11)))
      .when(Boolean(this.usageMessage), x => x.child(div().flex_none().text_size(10).text_color(/⚠|실패|오류/i.test(this.usageMessage) ? t.blocked : t.muted).child(this.usageMessage)))
      .children(this.showAccounts ? accountSections : []);
    const boardControls = v_flex().id("board-controls").w_full().flex_none().gap(6)
      .on_size_change((size,cx)=>{const h=Math.ceil(size.height);if(this.controlsHeight!==h){this.controlsHeight=h;cx.notify();}})
        .child(v_flex().w_full().flex_none().gap(3)
          .child(h_flex().flex_wrap().gap(12).text_size(12).font_semibold()
            .child(statusControl("blocked"))
            .child(statusControl("working"))
            .child(statusControl("done"))
            .child(statusControl("idle"))
            .when(unknownTeams > 0, el => el.child(badge(`? 미확인 ${unknownTeams}`, t.unknown)))
            .child(badge(`총괄 · ${coordinatorStateText}`, coordinator?.state === "live" ? t.muted : coordinator?.state === "recovering" ? t.working : t.blocked)))
          .when(Boolean(coordinator?.reason) && !(coordinator?.state === "live" && ["GUI 체크포인트에서 새 총괄 대화를 열었습니다. 제품 업무는 자동 재개하지 않았습니다.", "GUI 체크포인트를 사용하는 총괄입니다."].includes(coordinator.reason)), el => el.child(words(`총괄 상태 · ${display(coordinator.reason)}`).text_size(11).text_color(t.muted))))
        .when(waiting.length > 0, el => el.child(v_flex().w_full().flex_none().gap(2)
          .child(div().whitespace_nowrap().text_size(11).font_semibold().text_color(t.muted).child("대기 이유"))
          .children(waiting.map((reason, index) => words(`⚠ ${index + 1} · ${reason}`).text_size(11).text_color(t.dim)))))
        .when(Boolean(this.coordinatorMessage) && (coordinator?.state !== "live" || /⚠|실패|오류/i.test(this.coordinatorMessage)), el => el.child(div().flex_none().text_size(11).text_color(/⚠|실패|오류/i.test(this.coordinatorMessage) ? t.blocked : t.muted).child(this.coordinatorMessage)))
        .when(actionRows.length > 0, el => el.child(v_flex().flex_none().gap(3).children(actionRows)))
        .child(v_flex().flex_none().gap(3)
          .child(div().whitespace_nowrap().text_size(11).font_semibold().text_color(t.muted).child("작업 목록"))
          .child(h_flex().flex_wrap().items_center().gap(10).text_size(12).text_color(t.muted)
            .child(div().child(`활성 ${allCards.filter(c => c.category !== "complete" && c.disposition !== "archived").length} · 완료 기록 ${completedCount}`))
            .child(button("toggle-archived",this.showArchived ? "활성 카드로 돌아가기" : `보관된 카드 ${archived.length}`,cx=>{this.showArchived=!this.showArchived;cx.notify();}))
            .child(div().child("정리 완료·보관은 실제 업무와 승인 상태를 바꾸지 않습니다."))))
        .when(Boolean(this.boardMessage),el=>el.child(words(this.boardMessage).text_size(12).text_color(t.muted)));
    const manual = this.manual;
    const manualText = manual?.phase === "running" ? (manual.joined ? "새로고침 중 · 진행 중이던 자동 갱신 결과를 기다립니다" : "새로고침 중")
      : manual?.phase === "done" ? `갱신 완료 · ${manual.changed ? "변경 있음" : "변경 없음"} · ${clock(manual.completedAt / 1000)}${manual.joined ? " · 자동 갱신 결과 사용" : ""}`
        : manual?.phase === "failed" ? `⚠ 갱신 실패 · 기존 자료 유지 · ${display(manual.error).slice(0, 120)}` : null;
    return v_flex().id("team-operations").size_full().min_h(0).min_w(0).overflow_hidden().bg(t.background).text_color(t.foreground).text_size(13)
      .font_family(settings().ui_font || "Pretendard")
      .on_size_change((size, cx) => {
        const width = Math.floor(size.width), height = Math.floor(size.height);
        if (this.viewWidth !== width || this.viewHeight !== height) { this.viewWidth = width; this.viewHeight = height; cx.notify(); }
      })
      .child(h_flex().min_h(48).flex_none().flex_wrap().px(14).py(6).gap(8).items_center().border_b(1).border_color(t.border)
        .on_size_change((size,cx)=>{const h=Math.ceil(size.height);if(this.headerHeight!==h){this.headerHeight=h;cx.notify();}})
        .child(div().font_semibold().text_size(16).child("팀 운영 현황")).child(div().flex_1())
        .child(v_flex().min_w(0).items_end().gap(1)
          .child(div().min_w(0).whitespace_normal().text_size(11).text_color(t.dim).child(`${this.error ? "갱신 실패 · 이전 자료" : this.stale ? "갱신 지연 · 이전 자료" : data?.synchronization?.state === "partial" ? "일부 자료 미확인" : "조회 성공"} · 관찰 ${clock(data?.observed_at)} · 수신 ${this.lastSuccessAt ? clock(this.lastSuccessAt / 1000) : DUMMY}`))
          .when(Boolean(manualText), el => el.child(div().id("manual-refresh-status").min_w(0).whitespace_normal().text_size(11).text_color(manual.phase === "failed" ? t.blocked : manual.phase === "running" ? t.working : t.muted).child(manualText))))
        .child(button("refresh", manual?.phase === "running" ? "새로고침 중" : this.busy ? "불러오는 중" : "새로고침", cx => { this.refresh(cx, true); }, this.busy || manual?.phase === "running")))
      .when(Boolean(this.error), el => el.child(div().px(12).py(7).text_color(t.blocked).child(`갱신에 실패했습니다. 기존 자료는 이전 관찰값입니다: ${this.error}`)))
      .when(Boolean(this.configError), el => el.child(div().px(12).py(7).text_color(t.blocked).child(`로컬 backend 설정을 읽을 수 없습니다: ${this.configError}`)))
      .when(this.busy && !data, el => el.child(div().p(14).text_color(t.dim).child("운영 현황을 읽는 중입니다.")))
      .child(v_flex().id("operations-content").w_full().min_w(0).flex_1().min_h(0).gap(6).p(8).when(narrow, el => el.overflow_y_scrollbar())
        .child(boardControls)
        .child(board)
        .child(accountArea));
  }
}
