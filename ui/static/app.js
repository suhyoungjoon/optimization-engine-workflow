// 워크플로우 화면. 빌드 없는 단일 페이지: 해시 라우팅 + 폴링. 모든 텍스트는 textContent로 넣는다 (LLM 출력 포함).

const app = document.getElementById("app");
const STAGE_KEYS = ["run", "analysis", "proposals", "validation", "apply"];
const ACTOR_LABEL = { ai: "AI + 코드", code: "코드", human: "사람 + 코드" };
const STATUS = {
  running: ["실행 중", "info"], analyzed: ["분석 완료", "info"], proposed: ["제안 완료", "info"],
  validated: ["검증 완료", "info"], awaiting_approval: ["승인 대기", "human"], applied: ["적용됨", "good"],
  rejected: ["반려됨", "outline"], failed: ["실패", "bad"],
};
const METRIC_LABEL = {
  assignment_rate: "할당성공률", desired_time_match_rate: "희망시간 일치율", avg_travel_min: "평균 이동시간(분)",
  worker_utilization: "작업자 활용률",
};
const SET_LABEL = { train: "학습용", holdout: "검증용" };
let META = null;
let pollTimer = null;

// --- DOM 도우미 ---

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  setAttrs(el, attrs);
  append(el, children);
  return el;
}
function s(tag, attrs, ...children) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  setAttrs(el, attrs);
  append(el, children);
  return el;
}
function setAttrs(el, attrs) {
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.setAttribute("class", v);
    else if (k === "style") el.style.cssText = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
}
function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || res.statusText);
  return body;
}
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });
const put = (path, body) => api(path, { method: "PUT", body: JSON.stringify(body || {}) });

function toast(text, bad = false) {
  const box = document.getElementById("toast");
  const msg = h("div", { class: "msg" + (bad ? " bad" : "") }, text);
  box.append(msg);
  setTimeout(() => msg.remove(), bad ? 6000 : 3000);
}

// --- 표시 형식 ---

const metricLabel = (m) => METRIC_LABEL[m] || (m.match(/^stage_(\d+)_share$/) ? `${RegExp.$1}단계 배정 비율` : m);
const isMinutes = (m) => m.endsWith("_min");
function fmtValue(m, v) {
  if (v == null || Number.isNaN(v)) return "–";
  return isMinutes(m) ? v.toFixed(2) : (v * 100).toFixed(1) + "%";
}
function fmtDelta(m, d) {
  if (d == null) return "–";
  const shown = isMinutes(m) ? Math.abs(d).toFixed(2) : (Math.abs(d) * 100).toFixed(1);
  const sign = Number(shown) === 0 ? "±" : d > 0 ? "+" : "−";
  return isMinutes(m) ? `${sign}${shown}분` : `${sign}${shown}%p`;
}
function deltaClass(m, d, higherIsBetter = !isMinutes(m)) {
  if (!d || fmtDelta(m, d).startsWith("±")) return "";
  return (d > 0) === higherIsBetter ? "delta-up" : "delta-down";
}
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString("ko-KR", { hour12: false }) : "–");
const shortDate = (iso) => (iso ? new Date(iso).toLocaleString("ko-KR", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }) : "");
const shortTime = (iso) => (iso ? new Date(iso).toLocaleTimeString("ko-KR", { hour12: false }) : "");
const fmtJSON = (v) => (typeof v === "string" ? v : JSON.stringify(v));
const statusBadge = (status) => {
  const [label, cls] = STATUS[status] || [status, "outline"];
  return h("span", { class: `badge ${cls}` }, h("span", { class: "dot" }), label);
};
const actorChip = (actor) => h("span", { class: `actor-chip actor-${actor}` }, ACTOR_LABEL[actor]);

function getPath(obj, path) {
  const parts = path.match(/[^.[\]]+/g) || [];
  return parts.reduce((o, p) => (o == null ? undefined : o[/^\d+$/.test(p) ? Number(p) : p]), obj);
}
function parseValue(text) {
  const t = text.trim();
  try { return JSON.parse(t); } catch { return t; }
}

// --- 라우팅 ---

function route() {
  clearInterval(pollTimer);
  pollTimer = null;
  const parts = (location.hash.replace(/^#\/?/, "") || "runs").split("/");
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === parts[0]));
  const view = { runs: parts[1] ? () => runPage(parts[1], parts[2]) : runsPage, models: () => modelsPage(parts[1]),
                 settings: settingsPage }[parts[0]] || runsPage;
  Promise.resolve(view()).catch((e) => app.replaceChildren(h("div", { class: "error-box" }, e.message)));
}

// --- 단계 상태 ---

function failedStage(run) {
  if (run.status !== "failed") return null;
  const st = run.error?.stage;
  if (st) return st === "setup" ? "run" : st;
  const reason = run.history.at(-1)?.reason || "";
  if (reason.startsWith("결과분석")) return "analysis";
  if (reason.startsWith("개선안 도출")) return "proposals";
  if (reason.startsWith("검증")) return "validation";
  return run.progress?.stage || "run";
}

function stageStates(run, stages = null) {
  const st = Object.fromEntries(STAGE_KEYS.map((k) => [k, "pending"]));
  const reached = { running: 0, analyzed: 2, proposed: 3, validated: 4, awaiting_approval: 4, applied: 5, rejected: 5 };
  const doneCount = run.status === "failed" ? STAGE_KEYS.indexOf(failedStage(run)) : reached[run.status] ?? 0;
  STAGE_KEYS.slice(0, doneCount).forEach((k) => (st[k] = "done"));
  if (run.status === "running" && stages?.run) st.run = "done";
  if (["running", "analyzed", "proposed"].includes(run.status)) {
    const cur = run.progress?.stage;
    const idx = Math.max(doneCount, STAGE_KEYS.indexOf(cur));
    if (idx >= 0 && idx < 4) st[STAGE_KEYS[idx]] = "active";
    STAGE_KEYS.slice(0, idx).forEach((k) => (st[k] = "done"));
  }
  if (run.status === "failed") st[failedStage(run)] = "failed";
  if (run.status === "awaiting_approval") st.apply = "waiting";
  if (run.status === "rejected") st.apply = "rejected";
  return st;
}

function miniPipe(run) {
  const st = stageStates(run);
  return h("span", { class: "mini-pipe", title: STAGE_KEYS.map((k) => `${k}: ${st[k]}`).join(", ") },
    META.stages.map((m) => h("span", { class: `${st[m.key]} actor-${m.actor}`, style: `--c: var(--actor-${m.actor})` })));
}

// --- 실행 목록 ---

async function runsPage() {
  clearInterval(pollTimer);
  pollTimer = null;
  const [list, scenarios] = await Promise.all([api("/api/runs"), api("/api/scenarios")]);
  const startBtn = h("button", { class: "primary", disabled: list.busy, onclick: startRun },
    list.busy ? "다른 실행이 진행 중" : "리허설 실행 시작");
  async function startRun() {
    startBtn.disabled = true;
    try {
      const { run_id } = await post("/api/runs", { engine: "rule", train: "train", holdout: "holdout" });
      location.hash = `#/runs/${run_id}`;
    } catch (e) { toast(e.message, true); startBtn.disabled = false; }
  }
  const setInfo = (name) => {
    const set = scenarios[name];
    const combos = [...new Set(set.cases.map((c) => c.faults.join("+") || "결함 없음"))];
    return h("div", { class: "stack", style: "gap:4px" },
      h("div", {}, h("b", {}, `${SET_LABEL[name]} ${set.cases.length}건`), " ",
        h("span", { class: "muted small" }, `seed ${set.cases.map((c) => c.seed).join(", ")}`)),
      h("div", { class: "chips" }, combos.map((c) => h("span", { class: "chip" }, c))));
  };
  const rows = list.runs.map((r) =>
    h("tr", { class: "clickable", onclick: () => (location.hash = `#/runs/${r.run_id}`) },
      h("td", {}, statusBadge(r.status)),
      h("td", {}, miniPipe(r)),
      h("td", { class: "mono" }, r.run_id),
      h("td", {}, r.model || "–"),
      h("td", { class: "small ink-2" }, r.history.at(-1)?.reason || (r.progress?.detail ?? "")),
      h("td", { class: "small muted" }, fmtTime(r.created_at))));

  app.replaceChildren(
    h("div", { class: "row", style: "margin-bottom:16px" }, h("h1", {}, "실행"), h("span", { class: "spacer" })),
    h("div", { class: "card", style: "margin-bottom:16px" },
      h("div", { class: "card-head" }, h("h2", {}, "새 실행"), h("span", { class: "badge warn" }, "리허설만 허용 (가짜 LLM, API 비용 없음)")),
      h("div", { class: "grid-2" }, setInfo("train"), setInfo("holdout")),
      h("div", { class: "row", style: "margin-top:14px" },
        h("span", { class: "small muted" }, "엔진 rule · 현재 챔피언으로 실행 · 시나리오 세트는 기준정보에서 바꾼다"),
        h("span", { class: "spacer" }), startBtn)),
    h("div", { class: "card" },
      rows.length
        ? h("div", { class: "table-wrap" }, h("table", {},
            h("thead", {}, h("tr", {}, ["상태", "단계", "실행 ID", "모델", "마지막 기록", "시작"].map((t) => h("th", {}, t)))),
            h("tbody", {}, rows)))
        : h("div", { class: "empty" }, "아직 실행이 없다. 위에서 리허설 실행을 시작한다.")));
  if (list.busy || list.runs.some((r) => ["running", "analyzed", "proposed", "validated"].includes(r.status))) {
    pollTimer = setInterval(() => { if (location.hash.replace(/^#\/?/, "") === "runs" || !location.hash) runsPage(); }, 1500);
  }
}

// --- 실행 상세 ---

async function runPage(runId, stageKey) {
  clearInterval(pollTimer);
  pollTimer = null;
  const detail = await api(`/api/runs/${runId}`);
  const { run, stages } = detail;
  const states = stageStates(run, stages);
  const selected = stageKey || defaultStage(states);

  const pipeline = h("div", { class: "pipeline" }, META.stages.map((m, i) => stageNode(m, i, states[m.key], run, stages, m.key === selected)));
  const panel = stagePanel(selected, detail, states);
  const scen = run.scenarios;
  app.replaceChildren(
    h("div", { class: "run-header" },
      h("div", {},
        h("div", { class: "row" }, h("a", { href: "#/runs", class: "small" }, "← 실행 목록")),
        h("div", { class: "row", style: "margin-top:4px" }, h("h1", { class: "mono" }, run.run_id), statusBadge(run.status)),
        h("div", { class: "meta" },
          h("span", {}, "모델 ", h("b", {}, run.model || "–")),
          h("span", {}, "LLM ", run.llm.model, run.llm.rehearsal ? " (리허설)" : ""),
          ["train", "holdout"].map((k) => h("span", { title: (scen[k].cases || []).map((c) => `seed ${c.seed}: ${c.faults.join("+") || "결함 없음"}`).join("\n") },
            `${SET_LABEL[k]} ${(scen[k].cases || []).length}건`)),
          h("span", { class: "muted" }, fmtTime(run.created_at)))),
      h("span", { class: "spacer" }),
      historyList(run)),
    pipeline, panel);

  if (!detail.final && run.status !== "awaiting_approval") {
    pollTimer = setInterval(async () => {
      if (!location.hash.startsWith(`#/runs/${runId}`)) return;
      const again = await api(`/api/runs/${runId}`);
      if (JSON.stringify(again.run) !== JSON.stringify(run)) runPage(runId, stageKey);
    }, 700);
  }
}

function defaultStage(states) {
  const order = ["failed", "active", "waiting"];
  for (const want of order) {
    const k = STAGE_KEYS.find((key) => states[key] === want);
    if (k) return k;
  }
  return [...STAGE_KEYS].reverse().find((k) => states[k] === "done" || states[k] === "rejected") || "run";
}

function historyList(run) {
  return h("details", { class: "card small", style: "padding:10px 14px; min-width:260px; max-width:420px" },
    h("summary", {}, `상태 이력 ${run.history.length}건`),
    h("ul", { class: "events", style: "margin-top:6px" }, run.history.map((e) =>
      h("li", {}, h("span", { class: "muted" }, shortTime(e.at)),
        h("span", {}, h("b", {}, STATUS[e.status]?.[0] || e.status), e.reason ? h("div", { class: "ink-2" }, e.reason) : null)))));
}

function stageSummary(key, run, stages) {
  const st = stages[key];
  if (key === "run" && st) return `학습용 ${st.summary.cases}건 · 평균 할당 ${fmtValue("assignment_rate", st.summary.metrics.assignment_rate)} · 위반 ${st.summary.violations}`;
  if (key === "analysis" && st) return `발견 ${st.findings.length}개${st.dropped.length ? ` · 근거 부족 제외 ${st.dropped.length}` : ""} · LLM ${st.usage.llm_calls}회`;
  if (key === "proposals" && st) {
    const n = (s) => st.proposals.filter((p) => p.state === s).length;
    return `개선안 ${st.proposals.length}개 · 검증 대상 ${n("valid")}${n("invalid") ? ` · 범위 밖 ${n("invalid")}` : ""}`;
  }
  if (key === "validation" && st) {
    const pass = st.results.filter((r) => r.verdict.pass).length;
    return `판정 통과 ${pass} · 불통과 ${st.results.length - pass}`;
  }
  if (key === "apply") {
    if (st?.decision === "approved") return `${st.model_before} → ${st.model_after}`;
    if (st?.decision === "rejected") return `반려: ${st.reason}`;
    if (run.status === "awaiting_approval") return "사람의 승인을 기다린다";
  }
  return "";
}

function stageNode(meta, i, state, run, stages, selected) {
  const icon = { done: ["✓", "state-done", "완료"], failed: ["✕", "state-failed", "실패"], waiting: ["●", "state-waiting", "승인 대기"],
                 rejected: ["–", "muted", "반려"], active: [null, "", "진행 중"], pending: ["", "muted", "대기"] }[state];
  const prog = state === "active" && run.progress?.stage === meta.key ? run.progress : null;
  const indeterminate = state === "active" && (!prog || prog.total <= 1);
  return h("button", {
    class: `stage actor-${meta.actor} ${state}${selected ? " selected" : ""}`, style: `--c: var(--actor-${meta.actor})`,
    "aria-pressed": selected ? "true" : "false", "aria-label": `${i + 1}단계 ${meta.label}: ${icon[2]}`,
    onclick: () => (location.hash = `#/runs/${run.run_id}/${meta.key}`),
  },
  h("div", { class: "stage-top" },
    h("span", { class: "stage-no" }, i + 1), h("span", { class: "stage-name" }, meta.label),
    h("span", { class: `stage-state ${icon[1]}`, title: icon[2] }, state === "active" ? h("span", { class: "spinner" }) : icon[0])),
  h("span", { class: "actor-label" }, ACTOR_LABEL[meta.actor]),
  h("span", { class: "stage-summary" }, state === "active" ? (prog?.detail || "진행 중") : stageSummary(meta.key, run, stages) || icon[2]),
  state === "active" ? h("div", { class: `progress${indeterminate ? " indeterminate" : ""}` },
    h("span", { style: `width:${prog && prog.total ? Math.round((100 * prog.done) / prog.total) : 0}%` })) : null,
  state === "active" && prog && prog.total > 1 ? h("span", { class: "small muted num" }, `${prog.done} / ${prog.total}`) : null);
}

function stagePanel(key, detail, states) {
  const meta = META.stages.find((m) => m.key === key);
  const body = { run: runStageView, analysis: analysisView, proposals: proposalsView, validation: validationView, apply: applyView }[key](detail, states);
  return h("section", { class: "card stage-panel", style: `--c: var(--actor-${meta.actor})` },
    h("div", { class: "panel-head" },
      h("h2", {}, `${STAGE_KEYS.indexOf(key) + 1}. ${meta.label}`), actorChip(meta.actor),
      h("span", { class: "spacer" }),
      states[key] === "failed" ? h("span", { class: "badge bad" }, "실패") : null),
    states[key] === "failed" ? h("div", { class: "error-box", style: "margin-bottom:12px" }, detail.run.history.at(-1)?.reason) : null,
    body);
}

const notYet = (text = "이 단계는 아직 결과가 없다.") => h("div", { class: "empty" }, text);

// 1. 실행
function runStageView({ stages }) {
  const st = stages.run;
  if (!st) return notYet();
  const metrics = Object.keys(st.summary.metrics);
  const tiles = ["assignment_rate", "desired_time_match_rate", "avg_travel_min", "worker_utilization"].filter((m) => m in st.summary.metrics);
  return h("div", { class: "stack" },
    h("p", { class: "ink-2", style: "margin:0" }, "현재 챔피언 params로 학습용 세트의 모든 케이스를 풀고, 도메인 팩의 validate()로 필수조건을 독립 검증했다. 첫 케이스는 분석·개선안 도출의 대표 케이스다."),
    h("div", { class: "row", style: "gap:12px" }, tiles.map((m) => h("div", { class: "card", style: "padding:10px 14px; min-width:150px; box-shadow:none" },
      h("div", { class: "small muted" }, `${metricLabel(m)} 평균`), h("div", { style: "font-size:22px; font-weight:650" }, fmtValue(m, st.summary.metrics[m])))),
      h("div", { class: "card", style: "padding:10px 14px; min-width:150px; box-shadow:none" },
        h("div", { class: "small muted" }, "필수조건 위반"),
        h("div", { style: "font-size:22px; font-weight:650" }, st.summary.violations, " ",
          h("span", { class: `badge ${st.summary.violations ? "bad" : "good"}` }, st.summary.violations ? "✕ 있음" : "✓ 없음")))),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "seed"), h("th", {}, "결함"), h("th", { class: "num" }, "항목"),
        metrics.map((m) => h("th", { class: "num" }, metricLabel(m))), h("th", { class: "num" }, "위반"))),
      h("tbody", {}, st.cases.map((c, i) => h("tr", {},
        h("td", {}, c.seed, i === 0 ? h("span", { class: "badge info", style: "margin-left:6px" }, "대표") : null),
        h("td", {}, h("span", { class: "chips" }, c.faults.length ? c.faults.map((f) => h("span", { class: "chip" }, f)) : h("span", { class: "muted" }, "없음"))),
        h("td", { class: "num" }, c.items),
        metrics.map((m) => h("td", { class: "num" }, fmtValue(m, c.metrics[m]))),
        h("td", { class: "num" }, c.violations)))))));
}

// 2. 결과분석
function analysisView({ stages }) {
  const st = stages.analysis;
  if (!st) return notYet("분석 agent가 아직 리포트를 내지 않았다.");
  const cited = (f) => (f.cited_calls || []).map((id) => st.calls[id]).filter(Boolean);
  return h("div", { class: "stack" },
    h("div", { class: "note-box" }, "AI가 집계 도구로 결정 레코드를 조회해 실패 패턴을 찾았다. 발견의 수치가 인용한 도구 결과에 없으면 코드가 제외한다 (근거 검사). 정답표는 보지 않는다."),
    st.summary ? h("p", { style: "margin:0" }, h("b", {}, "요약 "), st.summary) : null,
    st.findings.map((f) => h("div", { class: "finding" },
      h("div", { class: "row" }, h("span", { class: "pid" }, f.id), h("b", {}, f.title),
        (f.reason_codes || []).map((r) => h("span", { class: "badge outline" }, r)),
        f.metric ? h("span", { class: "badge outline" }, `${metricLabel(f.metric.name)} ${f.metric.direction === "low" ? "낮음" : "높음"}`) : null),
      h("div", { style: "margin-top:4px" }, f.description),
      f.slice ? h("div", { class: "row small", style: "margin-top:6px" }, h("span", { class: "muted" }, "구간"),
        Object.entries(f.slice).map(([k, v]) => h("span", { class: "chip" }, `${META.dimensions[k] || k}: ${v.join(", ")}`))) : null,
      f.hypothesis ? h("div", { class: "small ink-2", style: "margin-top:4px" }, "가설: ", f.hypothesis) : null,
      h("details", { style: "margin-top:6px" }, h("summary", {}, `근거 도구 호출 ${cited(f).length}건`),
        cited(f).map((c) => h("pre", {}, `${c.name}(${JSON.stringify(c.input)})\n→ ${JSON.stringify(c.output, null, 1)}`))))),
    st.dropped.length ? h("div", {}, h("div", { class: "section-title" }, "근거 부족으로 제외된 발견"),
      st.dropped.map((d) => h("div", { class: "finding" }, h("b", {}, d.finding.title), h("div", { class: "small delta-down" }, d.problems.join("; "))))) : null,
    h("div", { class: "small muted" }, `LLM 호출 ${st.usage.llm_calls}회 · 도구 호출 ${Object.keys(st.calls).length}회 · 종료 ${st.stop}`));
}

// 3. 개선안 도출
function changeRows(p, params) {
  const rows = [];
  for (const c of p.params_changes || []) rows.push(h("tr", {}, h("td", {}, h("span", { class: "badge outline" }, "전역")), h("td", { class: "mono" }, c.path),
    h("td", { class: "num" }, fmtJSON(getPath(params, c.path))), h("td", { class: "num" }, h("b", {}, fmtJSON(c.value)))));
  for (const r of p.override_rules || []) for (const [path, v] of Object.entries(r.set)) rows.push(h("tr", {},
    h("td", {}, h("span", { class: "badge outline" }, "구간"), " ",
      h("span", { class: "chips" }, Object.entries(r.when).map(([k, vs]) => h("span", { class: "chip" }, `${META.dimensions[k] || k}: ${[].concat(vs).join(", ")}`)))),
    h("td", { class: "mono" }, path), h("td", { class: "num" }, fmtJSON(getPath(params, path))), h("td", { class: "num" }, h("b", {}, fmtJSON(v)))));
  return rows.length ? h("table", { style: "margin-top:8px" },
    h("thead", {}, h("tr", {}, h("th", {}, "적용 범위"), h("th", {}, "경로"), h("th", { class: "num" }, "챔피언"), h("th", { class: "num" }, "제안"))),
    h("tbody", {}, rows)) : null;
}

function stateBadge(p) {
  const map = { valid: ["검증 대상", "info"], invalid: ["허용 범위 밖", "bad"], unsupported: ["검증 불가 (spec)", "outline"] };
  const [label, cls] = map[p.state] || [p.state, "outline"];
  return h("span", { class: `badge ${cls}` }, label);
}
const originBadge = (p) => (p.origin ? h("span", { class: "badge human", title: p.origin.note || "" }, `사람 수정 ← 안 ${p.origin.revised_from}`) : null);

function proposalsView({ stages }) {
  const st = stages.proposals;
  if (!st) return notYet("개선 agent가 아직 개선안을 내지 않았다.");
  const params = stages.run?.params || {};
  return h("div", { class: "stack" },
    h("div", { class: "note-box" }, "AI가 params 변경안을 제안했다 (simulate_params 도구로 미리 시험 가능). 코드가 제출안을 허용 범위(bounds)와 구조로 다시 검사해 검증 대상을 가른다."),
    st.proposals.map((item) => {
      const p = item.proposal;
      return h("div", { class: "proposal" },
        h("div", { class: "proposal-head" }, h("span", { class: "pid" }, `안 ${item.id}`), h("b", {}, p.title), stateBadge(item), originBadge(item),
          (p.target_findings || []).map((f) => h("span", { class: "chip" }, `대상 ${f}`))),
        p.rationale ? h("div", { class: "ink-2" }, p.rationale) : null,
        p.expected_effect ? h("div", { class: "small muted" }, "기대 효과: ", p.expected_effect) : null,
        p.kind === "params" ? changeRows(p, params) : (p.spec_edits || []).map((e) => h("pre", { class: "small", style: "margin-top:6px" }, `[${e.section}] ${e.text}`)),
        item.errors.length ? h("div", { class: "error-box", style: "margin-top:8px" }, item.errors.join("; ")) : null);
    }),
    h("div", { class: "small muted" }, `시험 시뮬레이션 ${st.trials}회 · LLM 호출 ${st.usage.llm_calls}회 · 종료 ${st.stop}`));
}

// 4. 검증(비교)
const readable = (text) => text.replace(/\b([a-z]+(?:_[a-z0-9]+)+)\b/g, (k) => metricLabel(k));

function criteriaText(j) {
  const parts = [`${metricLabel(j.target.metric)} 평균 Δ ≥ ${fmtDelta(j.target.metric, j.target.min_improvement)}`];
  for (const [m, lim] of Object.entries(j.guards || {})) {
    parts.push(lim.max_drop != null ? `${metricLabel(m)} 하락 ≤ ${fmtDelta(m, lim.max_drop).slice(1)}` : `${metricLabel(m)} 증가 ≤ ${fmtDelta(m, lim.max_increase).slice(1)}`);
  }
  return parts;
}

function checkCell(check, metric) {
  if (!check) return h("td", {}, "–");
  const v = check.name === "violations" ? `${check.value}건` : check.value == null ? "없음" : fmtDelta(metric, check.value);
  return h("td", { class: "num" }, h("span", { class: `check ${check.ok ? "ok" : "no"}` }, check.ok ? "✓ " : "✕ "), v);
}

function verdictBlock(r, judgment, stages, editable, runId) {
  const sets = r.verdict.sets;
  const checkNames = sets.train.checks.map((c) => c.name);
  const metricOf = (name) => (name === "target" ? judgment.target.metric : name.startsWith("guard:") ? name.slice(6) : null);
  const judged = [judgment.target.metric, ...Object.keys(judgment.guards || {})];
  const allMetrics = Object.keys(r.train.summary.metrics);
  const origin = r.origin || stages.proposals?.proposals.find((p) => p.id === r.id)?.origin;
  const box = h("div", { class: `proposal ${r.verdict.pass ? "pass" : "fail"}` },
    h("div", { class: "proposal-head" },
      h("span", { class: "pid" }, `안 ${r.id}`), h("b", {}, r.title),
      h("span", { class: `badge ${r.verdict.pass ? "good" : "bad"}` }, r.verdict.pass ? "✓ 판정 통과" : "✕ 판정 불통과"),
      r.verdict.overfit ? h("span", { class: "badge warn", title: "학습용에서는 목표를 채웠지만 검증용에서는 효과가 사라졌다" }, "⚠ 과적합") : null,
      origin ? h("span", { class: "badge human" }, `사람 수정 ← 안 ${origin.revised_from}`) : null,
      h("span", { class: "spacer" }),
      editable ? h("button", { class: "small", onclick: (e) => openEditor(e.currentTarget, r.id, stages, runId) }, "값 수정 후 재검증") : null),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "판정 기준"), h("th", { class: "num" }, `학습용 (${r.train.summary.cases}건)`), h("th", { class: "num" }, `검증용 (${r.holdout.summary.cases}건)`))),
      h("tbody", {}, checkNames.map((name, i) => {
        const m = metricOf(name);
        const label = name === "violations" ? "필수조건 위반 0건 (절대 기준)" : name === "target"
          ? `${metricLabel(m)} 평균 Δ ≥ ${fmtDelta(m, judgment.target.min_improvement)}`
          : (() => { const lim = judgment.guards[m]; return lim.max_drop != null ? `${metricLabel(m)} 하락 ≤ ${fmtDelta(m, lim.max_drop).slice(1)}` : `${metricLabel(m)} 증가 ≤ ${fmtDelta(m, lim.max_increase).slice(1)}`; })();
        return h("tr", {}, h("td", {}, label), checkCell(sets.train.checks[i], m), checkCell(sets.holdout.checks[i], m));
      })))),
    h("div", { class: "charts" }, judged.filter((m) => allMetrics.includes(m)).map((m) => stripPlot(r, m, judgment))),
    h("details", { style: "margin-top:10px" }, h("summary", {}, "전체 지표 (세트 평균, 챔피언 → 도전자)"),
      h("div", { class: "table-wrap" }, h("table", { style: "margin-top:6px" },
        h("thead", {}, h("tr", {}, h("th", {}, "지표"), ["train", "holdout"].map((k) => h("th", { class: "num" }, SET_LABEL[k])))),
        h("tbody", {}, allMetrics.map((m) => h("tr", {}, h("td", {}, metricLabel(m)), ["train", "holdout"].map((k) => {
          const x = r[k].summary.metrics[m];
          return h("td", { class: "num" }, `${fmtValue(m, x.before)} → ${fmtValue(m, x.after)} `, h("span", { class: deltaClass(m, x.delta_mean) }, `(${fmtDelta(m, x.delta_mean)})`));
        }))))))),
    r.verdict.reasons.length ? h("ul", { class: "small", style: "margin:10px 0 0; padding-left:18px" }, r.verdict.reasons.map((t) => h("li", { class: "delta-down" }, readable(t)))) : null);
  return box;
}

function validationView({ run, stages }) {
  const st = stages.validation;
  if (!st) return notYet("검증은 개선안 도출 뒤에 코드가 한다.");
  const editable = run.status === "awaiting_approval";
  return h("div", { class: "stack" },
    h("div", { class: "note-box" }, "코드가 검증 대상 안마다 챔피언과 도전자를 학습용·검증용의 모든 케이스에서 같은 인스턴스로 비교하고, 판정 기준을 적용했다. 두 세트 모두 통과해야 판정 통과다."),
    h("div", { class: "row small" }, h("span", { class: "muted" }, "판정 기준"), criteriaText(st.judgment).map((t) => h("span", { class: "chip" }, t)),
      h("a", { href: "#/settings", class: "small" }, "기준정보에서 바꾸기")),
    st.results.map((r) => verdictBlock(r, st.judgment, stages, editable, run.run_id)));
}

// 케이스별 Δ 점 그래프 (학습용 채운 원, 검증용 빈 원). 0선과 판정 기준선.
function stripPlot(r, metric, judgment) {
  const W = 320, H = 118, L = 12, R = 12, rowY = { train: 44, holdout: 80 };
  const pts = ["train", "holdout"].flatMap((k) => r[k].cases.map((c) => ({ set: k, seed: c.seed, faults: c.faults,
    d: c.after[metric] - c.before[metric], before: c.before[metric], after: c.after[metric] })));
  let limit = null;
  if (metric === judgment.target.metric) limit = { v: judgment.target.min_improvement, label: "최소 개선" };
  else if (judgment.guards?.[metric]) { const g = judgment.guards[metric]; limit = g.max_drop != null ? { v: -g.max_drop, label: "하락 한도" } : { v: g.max_increase, label: "증가 한도" }; }
  const vals = [0, ...pts.map((p) => p.d), limit?.v ?? 0];
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi - lo || Math.abs(hi) || 0.01) * 0.12;
  lo -= pad; hi += pad;
  const x = (v) => L + ((v - lo) / (hi - lo)) * (W - L - R);
  const means = { train: r.train.summary.metrics[metric].delta_mean, holdout: r.holdout.summary.metrics[metric].delta_mean };
  const tip = document.querySelector(".tooltip") || document.body.appendChild(h("div", { class: "tooltip", hidden: true }));
  const show = (e, text) => { tip.hidden = false; tip.textContent = text; tip.style.left = `${e.clientX + 12}px`; tip.style.top = `${e.clientY + 12}px`; };
  const hide = () => (tip.hidden = true);
  const graph = s("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", role: "img",
    "aria-label": `${metricLabel(metric)} 케이스별 Δ: 학습용 평균 ${fmtDelta(metric, means.train)}, 검증용 평균 ${fmtDelta(metric, means.holdout)}` },
    // 행 기준선
    ["train", "holdout"].map((k) => s("line", { x1: L, x2: W - R, y1: rowY[k], y2: rowY[k], stroke: "var(--grid)", "stroke-width": 1 })),
    // 0선
    s("line", { x1: x(0), x2: x(0), y1: 22, y2: 98, stroke: "var(--axis)", "stroke-width": 1.5 }),
    s("text", { x: x(0), y: 112, "text-anchor": "middle", "font-size": 10, fill: "var(--muted)" }, "0"),
    limit ? [s("line", { x1: x(limit.v), x2: x(limit.v), y1: 18, y2: 98, stroke: "var(--ink-2)", "stroke-width": 1, "stroke-dasharray": "3 3" }),
      (() => { const lx = x(limit.v), anchor = lx < 70 ? "start" : lx > W - 70 ? "end" : "middle";
        return s("text", { x: anchor === "start" ? Math.max(lx - 4, 2) : anchor === "end" ? Math.min(lx + 4, W - 2) : lx, y: 13,
          "text-anchor": anchor, "font-size": 10, fill: "var(--ink-2)" }, `${limit.label} ${fmtDelta(metric, limit.v)}`); })()] : null,
    // 평균 표시
    ["train", "holdout"].map((k) => s("line", { x1: x(means[k]), x2: x(means[k]), y1: rowY[k] - 11, y2: rowY[k] + 11,
      stroke: k === "train" ? "var(--series-1)" : "var(--series-2)", "stroke-width": 2, "stroke-linecap": "round" })),
    pts.map((p) => {
      const color = p.set === "train" ? "var(--series-1)" : "var(--series-2)";
      const text = `${SET_LABEL[p.set]} seed ${p.seed} (${p.faults.join("+") || "결함 없음"})\n${fmtValue(metric, p.before)} → ${fmtValue(metric, p.after)}  Δ ${fmtDelta(metric, p.d)}`;
      return s("g", { onmousemove: (e) => show(e, text), onmouseleave: hide },
        s("circle", { cx: x(p.d), cy: rowY[p.set], r: 11, fill: "transparent" }),
        s("circle", { cx: x(p.d), cy: rowY[p.set], r: 4.5, fill: p.set === "train" ? color : "var(--surface)", stroke: p.set === "train" ? "var(--surface)" : color, "stroke-width": 2 }));
    }));
  return h("div", { class: "chart-card" },
    h("div", { class: "row" }, h("h4", {}, `${metricLabel(metric)} Δ (케이스별)`), h("span", { class: "spacer" }),
      h("span", { class: "series-legend" },
        h("span", {}, h("span", { class: "sw", style: "background:var(--series-1)" }), `학습용 평균 ${fmtDelta(metric, means.train)}`),
        h("span", {}, h("span", { class: "sw", style: "background:var(--surface); box-shadow: inset 0 0 0 2px var(--series-2)" }), `검증용 ${fmtDelta(metric, means.holdout)}`))),
    graph);
}

// 값 수정 후 재검증
function openEditor(button, proposalId, stages, runId) {
  const box = button.closest(".proposal");
  if (box.querySelector(".editor")) return;
  const item = stages.proposals.proposals.find((p) => p.id === proposalId);
  const base = item.proposal;
  const params = stages.run.params;
  const inputs = [];
  const field = (label, current, value, onValue) => {
    const input = h("input", { value: fmtJSON(value), style: "width:140px" });
    inputs.push(() => onValue(parseValue(input.value)));
    return h("tr", {}, h("td", {}, label), h("td", { class: "num muted" }, fmtJSON(current)), h("td", {}, input));
  };
  const edited = { params_changes: structuredClone(base.params_changes || []), override_rules: structuredClone(base.override_rules || []) };
  const rows = [
    ...edited.params_changes.map((c) => field(h("span", { class: "mono" }, c.path), getPath(params, c.path), c.value, (v) => (c.value = v))),
    ...edited.override_rules.flatMap((r) => Object.keys(r.set).map((path) => field(
      h("span", {}, h("span", { class: "mono" }, path), " ", h("span", { class: "chips" }, Object.entries(r.when).map(([k, vs]) => h("span", { class: "chip" }, `${META.dimensions[k] || k}: ${[].concat(vs).join(", ")}`)))),
      getPath(params, path), r.set[path], (v) => (r.set[path] = v)))),
  ];
  const note = h("input", { placeholder: "수정 이유 (모델 카드에 남는다)", style: "flex:1; min-width:220px" });
  const err = h("div");
  const submit = h("button", { class: "primary", onclick: async () => {
    inputs.forEach((f) => f());
    submit.disabled = true; submit.replaceChildren(h("span", { class: "spinner", style: "--c: var(--actor-human-ink)" }), " 재검증 중");
    try {
      const out = await post(`/api/runs/${runId}/revise`, { proposal_id: proposalId, changes: edited, note: note.value || null });
      toast(`안 ${out.proposal.id} 추가: ${out.result.verdict.pass ? "판정 통과" : "판정 불통과"}`);
      runPage(runId, "validation");
    } catch (e) { err.replaceChildren(h("div", { class: "error-box" }, e.message)); submit.disabled = false; submit.textContent = "재검증"; }
  } }, "재검증");
  box.append(h("div", { class: "editor card", style: "margin-top:12px; box-shadow:none; background:var(--surface-2)" },
    h("div", { class: "card-head" }, h("h3", {}, `안 ${proposalId}의 값 수정`), h("span", { class: "badge human" }, "사람"),
      h("span", { class: "small muted" }, "경로와 구간 조건은 그대로, 값만 바꾼다. 새 안으로 추가되어 같은 기준으로 다시 검증된다.")),
    h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "변경"), h("th", { class: "num" }, "챔피언"), h("th", {}, "새 값"))), h("tbody", {}, rows)),
    h("div", { class: "row", style: "margin-top:10px" }, note, submit, h("button", { class: "ghost", onclick: (e) => e.currentTarget.closest(".editor").remove() }, "취소")),
    err));
}

// 5. 개선적용
function applyView({ run, stages }) {
  const st = stages.apply;
  if (st?.decision === "approved") {
    return h("div", { class: "stack" },
      h("div", { class: "row" }, h("span", { class: "badge good" }, "✓ 승인됨"), h("b", {}, `안 ${st.proposal_id}: ${st.title}`)),
      h("dl", { class: "kv" },
        h("dt", {}, "모델"), h("dd", {}, `${st.model_before} → `, h("a", { href: `#/models/${st.version_after}` }, st.model_after)),
        h("dt", {}, "승인 시각"), h("dd", {}, fmtTime(st.at)),
        st.note ? [h("dt", {}, "메모"), h("dd", {}, st.note)] : null,
        st.override_reason ? [h("dt", {}, "판정 무시 사유"), h("dd", {}, h("span", { class: "badge warn" }, "판정 무시"), " ", st.override_reason)] : null));
  }
  if (st?.decision === "rejected") {
    return h("div", { class: "row" }, h("span", { class: "badge outline" }, "반려됨"), st.reason, h("span", { class: "muted small" }, fmtTime(st.at)));
  }
  if (run.status !== "awaiting_approval") return notYet("검증이 끝나면 사람이 승인하거나 반려한다.");

  const results = stages.validation.results;
  const passing = results.filter((r) => r.verdict.pass);
  let chosen = passing.length === 1 ? passing[0].id : null;
  const overrideBox = h("div");
  const note = h("input", { placeholder: "메모 (선택)", style: "flex:1; min-width:220px" });
  const err = h("div");
  const approveBtn = h("button", { class: "primary", onclick: approve }, "승인: 새 버전으로 등록하고 챔피언 지정");
  const rejectReason = h("input", { placeholder: "반려 사유 (필수)", style: "flex:1; min-width:220px" });

  function renderOverride() {
    const r = results.find((x) => x.id === chosen);
    approveBtn.disabled = !r || r.violations > 0;
    if (!r) return overrideBox.replaceChildren(h("div", { class: "small muted" }, "승인할 안을 고른다."));
    if (r.violations > 0) return overrideBox.replaceChildren(h("div", { class: "error-box" }, `필수조건 위반 ${r.violations}건: 승인할 수 없다 (절대 기준).`));
    if (r.verdict.pass) return overrideBox.replaceChildren();
    overrideBox.replaceChildren(h("div", { class: "note-box", style: "background:var(--warning-wash); color:var(--ink)" },
      h("b", {}, "판정을 통과하지 못한 안이다. "), "그래도 승인하려면 판정을 무시하는 사유를 적는다 (모델 카드에 남는다).",
      h("ul", { class: "small", style: "margin:6px 0 8px; padding-left:18px" }, r.verdict.reasons.map((t) => h("li", {}, readable(t)))),
      h("input", { class: "override-reason", placeholder: "판정 무시 사유", style: "width:100%" })));
  }
  async function approve() {
    const r = results.find((x) => x.id === chosen);
    const reason = overrideBox.querySelector(".override-reason")?.value.trim();
    if (!r.verdict.pass && !reason) return err.replaceChildren(h("div", { class: "error-box" }, "판정 무시 사유를 적는다."));
    if (!confirm(`안 ${r.id}을(를) 승인해 새 모델 버전을 챔피언으로 지정할까?`)) return;
    try {
      await post(`/api/runs/${run.run_id}/approve`, { proposal_id: r.id, note: note.value || null, override_reason: r.verdict.pass ? null : reason });
      toast("승인했다: 새 챔피언이 등록되었다");
      runPage(run.run_id, "apply");
    } catch (e) { err.replaceChildren(h("div", { class: "error-box" }, e.message)); }
  }
  async function reject() {
    if (!rejectReason.value.trim()) return err.replaceChildren(h("div", { class: "error-box" }, "반려 사유를 적는다."));
    try { await post(`/api/runs/${run.run_id}/reject`, { reason: rejectReason.value }); toast("반려했다"); runPage(run.run_id, "apply"); }
    catch (e) { err.replaceChildren(h("div", { class: "error-box" }, e.message)); }
  }

  const options = results.map((r) => h("label", { class: "proposal row", style: "cursor:pointer" },
    h("input", { type: "radio", name: "pick", checked: r.id === chosen, onchange: () => { chosen = r.id; renderOverride(); } }),
    h("span", { class: "pid" }, `안 ${r.id}`), h("b", {}, r.title),
    h("span", { class: `badge ${r.verdict.pass ? "good" : "bad"}` }, r.verdict.pass ? "✓ 판정 통과" : "✕ 불통과"),
    r.verdict.overfit ? h("span", { class: "badge warn" }, "⚠ 과적합") : null,
    r.violations ? h("span", { class: "badge bad" }, `위반 ${r.violations}`) : null,
    h("span", { class: "spacer" }),
    h("span", { class: "small ink-2 num" }, `검증용 ${metricLabel(stages.validation.judgment.target.metric)} ${fmtDelta(stages.validation.judgment.target.metric, r.holdout.summary.metrics[stages.validation.judgment.target.metric]?.delta_mean)}`)));
  renderOverride();
  return h("div", { class: "stack" },
    h("div", { class: "note-box" }, "여기서 멈춘다. 사람이 승인해야 새 버전이 챔피언이 된다 (자동 승인 없음). 승인 전 챔피언이 바뀌었으면 이 실행은 승인할 수 없다."),
    h("div", {}, options), overrideBox,
    h("div", { class: "row" }, note, approveBtn),
    h("div", { class: "row" }, rejectReason, h("button", { class: "danger", onclick: reject }, "반려")),
    err);
}

// --- 모델 버전 ---

async function modelsPage(sel) {
  const data = await api("/api/models/rule");
  const versions = data.versions;
  const selected = Number(sel?.replace(/^v/, "")) || data.champion;
  const detail = await api(`/api/models/rule/versions/${selected}`);
  const parentDiff = detail.card.parent ? await api(`/api/models/rule/diff?a=${detail.card.parent}&b=${selected}`) : null;

  app.replaceChildren(
    h("div", { class: "row", style: "margin-bottom:16px" }, h("h1", {}, "모델 버전"),
      h("span", { class: "badge human" }, `챔피언 rule@v${data.champion}`), h("span", { class: "muted small" }, `버전 ${versions.length}개 · 버전 파일은 만든 뒤 바뀌지 않는다`)),
    h("div", { class: "models-layout" },
      h("div", { class: "stack" },
        h("div", { class: "card lineage" }, h("div", { class: "section-title" }, "버전 계보"), lineage(versions, data.champion, selected)),
        h("div", { class: "card" }, h("div", { class: "section-title" }, "챔피언 이력"),
          h("ul", { class: "events" }, [...data.history].reverse().map((e) => h("li", {},
            h("span", { class: "muted small" }, shortDate(e.at)),
            h("span", {}, h("b", {}, { init: "초기", approve: "승인", rollback: "되돌리기" }[e.action] || e.action), ` → v${e.version}`,
              e.previous ? h("span", { class: "muted" }, ` (이전 v${e.previous})`) : null,
              e.reason ? h("div", { class: "ink-2" }, e.reason) : null,
              e.run_id ? h("div", {}, h("a", { href: `#/runs/${e.run_id}` }, `실행 ${e.run_id}`), ` 안 ${e.proposal_id}`) : null)))))),
      versionDetail(detail, parentDiff, versions, data.champion)));
}

function lineage(versions, champion, selected) {
  const lane = {}, childSeen = {};
  let maxLane = 0;
  for (const v of versions) {
    const p = v.parent;
    if (p == null) lane[v.version] = 0;
    else if (!childSeen[p]) lane[v.version] = lane[p];
    else lane[v.version] = ++maxLane;
    if (p != null) childSeen[p] = true;
  }
  const rowH = 58, W = 320, x0 = 26, laneW = 26;
  const ordered = [...versions].reverse();   // 최신이 위
  const y = Object.fromEntries(ordered.map((v, i) => [v.version, 28 + i * rowH]));
  const x = (v) => x0 + lane[v] * laneW;
  const H = 28 + ordered.length * rowH - 20;
  return s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "모델 버전 계보" },
    versions.filter((v) => v.parent != null).map((v) => {
      const [x1, y1, x2, y2] = [x(v.parent), y[v.parent], x(v.version), y[v.version]];
      return s("path", { d: x1 === x2 ? `M${x1},${y1} L${x2},${y2}` : `M${x1},${y1} C${x1},${y1 - 30} ${x2},${y2 + 30} ${x2},${y2}`,
        fill: "none", stroke: "var(--axis)", "stroke-width": 2 });
    }),
    ordered.map((v) => {
      const isChamp = v.version === champion, isSel = v.version === selected;
      const how = v.override ? "판정 무시" : v.parent == null ? "초기" : v.verdict?.pass ? "판정 통과" : "";
      return s("g", { class: "node", tabindex: 0, role: "link", "aria-label": `v${v.version}`,
        onclick: () => (location.hash = `#/models/v${v.version}`), onkeydown: (e) => e.key === "Enter" && (location.hash = `#/models/v${v.version}`) },
        s("rect", { x: 0, y: y[v.version] - 24, width: W, height: 48, rx: 8, fill: isSel ? "var(--surface-2)" : "transparent" }),
        isChamp ? s("circle", { cx: x(v.version), cy: y[v.version], r: 12, fill: "none", stroke: "var(--actor-human)", "stroke-width": 2 }) : null,
        s("circle", { class: "body", cx: x(v.version), cy: y[v.version], r: 7, fill: isChamp ? "var(--actor-human)" : "var(--surface)", stroke: "var(--actor-human)", "stroke-width": 2 }),
        s("text", { x: x0 + (maxLane + 1) * laneW + 6, y: y[v.version] - 3, "font-size": 13, "font-weight": 650, fill: "var(--ink)" },
          `rule@v${v.version}`, isChamp ? s("tspan", { "font-size": 11, fill: "var(--ink-2)", "font-weight": 600 }, "  · 챔피언") : null),
        s("text", { x: x0 + (maxLane + 1) * laneW + 6, y: y[v.version] + 14, "font-size": 11.5, fill: "var(--muted)" },
          `${v.parent != null ? `부모 v${v.parent} · ` : ""}${how}${v.origin ? " · 사람 수정" : ""}`));
    }));
}

function versionDetail(detail, parentDiff, versions, champion) {
  const card = detail.card;
  const v = card.version;
  const judged = card.validation ? Object.keys(card.validation.train.metrics).filter((m) => ["assignment_rate", "desired_time_match_rate", "avg_travel_min"].includes(m)) : [];
  const err = h("div");
  const rollbackReason = h("input", { placeholder: "되돌리기 사유 (필수)", style: "flex:1; min-width:200px" });
  async function makeChampion() {
    if (!rollbackReason.value.trim()) return err.replaceChildren(h("div", { class: "error-box" }, "사유를 적는다."));
    if (!confirm(`챔피언을 rule@v${champion}에서 rule@v${v}로 바꿀까?`)) return;
    try { await post("/api/models/rule/rollback", { to: v, reason: rollbackReason.value }); toast(`챔피언: rule@v${v}`); modelsPage(`v${v}`); }
    catch (e) { err.replaceChildren(h("div", { class: "error-box" }, e.message)); }
  }
  const cmpA = h("select", {}, versions.map((x) => h("option", { value: x.version, selected: x.version === (card.parent ?? v) }, `v${x.version}`)));
  const cmpB = h("select", {}, versions.map((x) => h("option", { value: x.version, selected: x.version === v }, `v${x.version}`)));
  const cmpOut = h("div");
  async function compare() {
    const d = await api(`/api/models/rule/diff?a=${cmpA.value}&b=${cmpB.value}`);
    cmpOut.replaceChildren(diffTable(d.rows, `v${d.a}`, `v${d.b}`));
  }
  return h("div", { class: "stack" },
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, `rule@v${v}`),
        detail.champion ? h("span", { class: "badge human" }, "챔피언") : null,
        card.parent == null ? h("span", { class: "badge outline" }, "초기 버전") : card.override ? h("span", { class: "badge warn" }, "판정 무시 승인") : h("span", { class: "badge good" }, "✓ 판정 통과"),
        card.origin ? h("span", { class: "badge human" }, `사람 수정 ← 안 ${card.origin.revised_from}`) : null),
      h("dl", { class: "kv" },
        h("dt", {}, "부모"), h("dd", {}, card.parent != null ? h("a", { href: `#/models/v${card.parent}` }, `rule@v${card.parent}`) : "–"),
        h("dt", {}, "만든 시각"), h("dd", {}, fmtTime(card.created_at)),
        card.run_id ? [h("dt", {}, "만든 실행"), h("dd", {}, h("a", { href: `#/runs/${card.run_id}` }, card.run_id), ` · 안 ${card.proposal_id}`)] : null,
        card.proposal ? [h("dt", {}, "개선안"), h("dd", {}, h("b", {}, card.proposal.title), card.proposal.rationale ? h("div", { class: "ink-2 small" }, card.proposal.rationale) : null)] : null,
        card.origin ? [h("dt", {}, "사람 수정"), h("dd", {}, card.origin.note || "(메모 없음)")] : null,
        card.override ? [h("dt", {}, "판정 무시 사유"), h("dd", {}, card.override.reason)] : null,
        card.note ? [h("dt", {}, "메모"), h("dd", {}, card.note)] : null,
        card.source ? [h("dt", {}, "출처"), h("dd", {}, card.source, card.note ? "" : "")] : null),
      card.verdict?.reasons?.length ? h("ul", { class: "small", style: "margin:10px 0 0; padding-left:18px" }, card.verdict.reasons.map((t) => h("li", { class: "delta-down" }, readable(t)))) : null,
      !detail.champion ? h("div", { class: "row", style: "margin-top:14px" }, rollbackReason,
        h("button", { class: "primary", onclick: makeChampion }, `이 버전을 챔피언으로 (되돌리기)`)) : null,
      err),
    card.validation ? h("div", { class: "card" }, h("div", { class: "section-title" }, "승인 당시 검증 (세트 평균 Δ)"),
      h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "지표"), h("th", { class: "num" }, "학습용"), h("th", { class: "num" }, "검증용"))),
        h("tbody", {}, judged.map((m) => h("tr", {}, h("td", {}, metricLabel(m)),
          ["train", "holdout"].map((k) => { const d = card.validation[k].metrics[m].delta_mean; return h("td", { class: `num ${deltaClass(m, d)}` }, fmtDelta(m, d)); })))))) : null,
    parentDiff ? h("div", { class: "card" }, h("div", { class: "section-title" }, `부모(v${card.parent}) 대비 변경`), diffTable(parentDiff.rows, `v${card.parent}`, `v${v}`)) : null,
    h("div", { class: "card" }, h("div", { class: "section-title" }, "두 버전 비교"),
      h("div", { class: "row" }, cmpA, h("span", { class: "change-arrow" }, "→"), cmpB, h("button", { class: "small", onclick: compare }, "비교")), cmpOut),
    h("details", { class: "card" }, h("summary", {}, `params.yaml (v${v}, 읽기 전용)`), h("pre", { class: "mono", style: "margin-top:8px" }, detail.params_text)));
}

function diffTable(rows, a, b) {
  if (!rows.length) return h("div", { class: "small muted" }, "차이 없음");
  return h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "경로"), h("th", { class: "num" }, a), h("th", { class: "num" }, b))),
    h("tbody", {}, rows.map((r) => h("tr", {}, h("td", { class: "mono" }, r.path, r.kind !== "changed" ? h("span", { class: "badge outline", style: "margin-left:6px" }, r.kind === "added" ? "추가" : "삭제") : null),
      h("td", { class: "num muted" }, r.a === undefined || r.a === null ? "–" : fmtJSON(r.a)), h("td", { class: "num" }, h("b", {}, r.b === undefined || r.b === null ? "–" : fmtJSON(r.b)))))));
}

// --- 기준정보 ---

async function settingsPage() {
  const [settings, scenarios, models] = await Promise.all([api("/api/settings"), api("/api/scenarios"), api("/api/models/rule")]);
  const champ = await api(`/api/models/rule/versions/${models.champion}`);
  app.replaceChildren(
    h("div", { class: "row", style: "margin-bottom:6px" }, h("h1", {}, "기준정보")),
    h("p", { class: "ink-2", style: "margin:0 0 16px" }, "바꾼 값은 다음 실행부터 쓴다. 이미 끝난 실행은 당시 판정 기준과 시나리오를 함께 보관한다. 파일(settings/, scenarios/)의 git 커밋은 사람이 한다."),
    h("div", { class: "stack", style: "gap:16px" },
      judgmentCard(settings.values.judgment),
      limitsCard(settings.values),
      h("div", { class: "grid-2" }, scenarioCard("train", scenarios), scenarioCard("holdout", scenarios)),
      paramsCard(champ)));
}

function saveRow(onSave) {
  const err = h("div");
  const btn = h("button", { class: "primary", onclick: async () => {
    btn.disabled = true;
    try { await onSave(); err.replaceChildren(); toast("저장했다"); }
    catch (e) { err.replaceChildren(h("div", { class: "error-box" }, e.message)); }
    btn.disabled = false;
  } }, "저장");
  return [h("div", { class: "row", style: "margin-top:12px" }, h("span", { class: "spacer" }), btn), err];
}

function judgmentCard(j) {
  const metricSel = (value) => h("select", {}, META.metrics.map((m) => h("option", { value: m, selected: m === value }, metricLabel(m))));
  const target = metricSel(j.target.metric);
  const minImp = h("input", { type: "number", step: "0.001", value: j.target.min_improvement });
  const tbody = h("tbody");
  const addGuard = (metric, kind, value) => {
    const tr = h("tr", {}, h("td", {}, metricSel(metric)),
      h("td", {}, h("select", {}, h("option", { value: "max_drop", selected: kind === "max_drop" }, "하락 한도"), h("option", { value: "max_increase", selected: kind === "max_increase" }, "증가 한도"))),
      h("td", {}, h("input", { type: "number", step: "0.001", min: 0, value })),
      h("td", {}, h("button", { class: "ghost small", onclick: () => tr.remove(), "aria-label": "삭제" }, "삭제")));
    tbody.append(tr);
  };
  for (const [m, lim] of Object.entries(j.guards || {})) addGuard(m, "max_drop" in lim ? "max_drop" : "max_increase", Object.values(lim)[0]);
  return h("section", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "판정 기준"), actorChip("code"),
      h("span", { class: "small muted" }, "학습용·검증용 세트 평균 Δ에 적용. 두 세트 모두 통과해야 통과. 필수조건 위반 0건은 절대 기준이라 바꿀 수 없다.")),
    h("div", { class: "row", style: "gap:16px" },
      h("label", { class: "field" }, "목표 지표 (높을수록 좋은 지표)", target),
      h("label", { class: "field" }, "최소 개선 (평균 Δ, 비율 지표 0.01 = 1%p)", minImp)),
    h("div", { class: "section-title", style: "margin-top:14px" }, "부작용 한도"),
    h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "지표"), h("th", {}, "종류"), h("th", {}, "한도 (비율 0.05 = 5%p, 분)"), h("th", {}))), tbody),
    h("button", { class: "small", style: "margin-top:8px", onclick: () => addGuard(META.metrics[0], "max_drop", 0.05) }, "+ 한도 추가"),
    saveRow(async () => {
      const guards = {};
      for (const tr of tbody.children) {
        const [m, kind, value] = tr.querySelectorAll("select, input");
        guards[m.value] = { [kind.value]: Number(value.value) };
      }
      await put("/api/settings", { judgment: { target: { metric: target.value, min_improvement: Number(minImp.value) }, guards } });
    }));
}

function limitsCard(values) {
  const a = h("input", { type: "number", min: 1, step: 1, value: values.llm.analyze_max_calls });
  const p = h("input", { type: "number", min: 1, step: 1, value: values.llm.propose_max_calls });
  const sec = h("input", { type: "number", min: 1, step: 1, value: values.validation.max_seconds });
  return h("section", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "실행 상한")),
    h("div", { class: "row", style: "gap:16px" },
      h("label", { class: "field" }, h("span", {}, "결과분석 LLM 호출 상한 ", actorChip("ai")), a),
      h("label", { class: "field" }, h("span", {}, "개선안 도출 LLM 호출 상한 ", actorChip("ai")), p),
      h("label", { class: "field" }, h("span", {}, "검증 시간 예산(초) ", actorChip("code")), sec)),
    saveRow(() => put("/api/settings", { llm: { analyze_max_calls: Number(a.value), propose_max_calls: Number(p.value) },
                                         validation: { max_seconds: Number(sec.value) } })));
}

function scenarioCard(name, scenarios) {
  const set = scenarios[name];
  const tbody = h("tbody");
  const addRow = (seed, faults) => {
    const toggles = META.faults.map((f) => {
      const cb = h("input", { type: "checkbox", checked: faults.includes(f.id), value: f.id });
      const lab = h("label", { class: `fault-toggle${faults.includes(f.id) ? " on" : ""}`, title: f.name }, cb, f.id);
      cb.addEventListener("change", () => lab.classList.toggle("on", cb.checked));
      return lab;
    });
    const tr = h("tr", {}, h("td", {}, h("input", { type: "number", step: 1, value: seed, style: "width:90px" })),
      h("td", {}, h("span", { class: "chips" }, toggles)),
      h("td", {}, h("button", { class: "ghost small", onclick: () => tr.remove() }, "삭제")));
    tbody.append(tr);
  };
  set.cases.forEach((c) => addRow(c.seed, c.faults));
  const nextSeed = () => Math.max(0, ...[...tbody.querySelectorAll("input[type=number]")].map((i) => Number(i.value))) + 1;
  return h("section", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, `${SET_LABEL[name]} 시나리오 세트`), h("span", { class: "badge outline" }, `${set.cases.length}건`)),
    h("div", { class: "small muted", style: "margin-bottom:8px" },
      name === "train" ? "챔피언 실행과 분석·개선안 도출(첫 케이스), 검증에 쓴다." : "판정에만 쓴다. 학습용과 seed가 겹치면 안 된다. 학습용에 없는 결함 조합을 섞으면 과적합을 걸러낸다.",
      " 결함: ", META.faults.map((f) => `${f.id} ${f.name}`).join(", ")),
    h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "seed"), h("th", {}, "결함"), h("th", {}))), tbody),
    h("button", { class: "small", style: "margin-top:8px", onclick: () => addRow(nextSeed(), []) }, "+ 케이스 추가"),
    saveRow(async () => {
      const cases = [...tbody.children].map((tr) => ({ seed: Number(tr.querySelector("input[type=number]").value),
        faults: [...tr.querySelectorAll("input[type=checkbox]:checked")].map((c) => c.value) }));
      await put(`/api/scenarios/${name}`, { cases });
    }));
}

function paramsCard(champ) {
  const params = champ.params;
  const sections = Object.entries(params).filter(([k, v]) => k !== "version" && k !== "overrides" && v && typeof v === "object");
  return h("section", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, `챔피언 params (rule@v${champ.card.version}, 읽기 전용)`),
      h("span", { class: "small muted" }, "params는 개선안 승인으로만 바뀐다. 개선안은 허용 범위(bounds) 안에서만 값을 바꿀 수 있다."),
      h("a", { href: `#/models/v${champ.card.version}`, class: "small" }, "버전 보기")),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "섹션"), h("th", {}, "키"), h("th", {}, "값"), h("th", {}, "허용 범위"), h("th", {}, "설명"))),
      h("tbody", {}, sections.flatMap(([sec, body]) => Object.entries(body).filter(([k]) => k !== "bounds" && k !== "docs").map(([k, v]) =>
        h("tr", {}, h("td", { class: "muted" }, sec), h("td", { class: "mono" }, k), h("td", { class: "mono" }, fmtJSON(v)),
          h("td", { class: "mono muted" }, body.bounds?.[k] ? `[${body.bounds[k].join(", ")}]` : "–"),
          h("td", { class: "small ink-2" }, body.docs?.[k] || ""))))))),
    params.overrides?.rules?.length ? h("div", { style: "margin-top:10px" }, h("div", { class: "section-title" }, "구간 조건"),
      params.overrides.rules.map((r) => h("div", { class: "row small" }, h("span", { class: "chips" }, Object.entries(r.when).map(([k, vs]) => h("span", { class: "chip" }, `${META.dimensions[k] || k}: ${[].concat(vs).join(", ")}`))),
        h("span", { class: "change-arrow" }, "→"), h("span", { class: "mono" }, JSON.stringify(r.set))))) : null);
}

// --- 시작 ---

(async function init() {
  META = await api("/api/meta");
  window.addEventListener("hashchange", route);
  route();
})();
