"use strict";
// Design space view: checked configurations as points over three key figures,
// rotatable in 3D or as 2D projections, with the Pareto front of the admissible ones.

const BATCH = 50;          // configurations per verify run
const WORKERS = 2;         // parallel requests (the server runs at most two verify processes)
const ALL_LIMIT = 50000;   // offer an exhaustive pass up to this many combinations
const PARETO_ROWS = 100;
const VIEW_3D = { yaw: -0.7, pitch: 0.42, zoom: 1 };

const ex = {
  points: [], byKey: new Map(), total: 0, units: {},
  axes: [], dirs: {}, axesTouched: false,
  view: "3d", showInvalid: true, ...VIEW_3D,
  plan: null, offset: 0, started: false,
  pareto: new Set(), hover: null, selected: null,
  screen: [], drag: null,
};

/** Same canonical text as `selection_key` in explorer.py. */
const selectionKey = (sel) => Object.entries(sel).filter(([, v]) => v).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
  .map(([k, v]) => `${k}.${v}`).join(",");
const fmtNum = (v, digits = 2) => v.toLocaleString("de-DE", { maximumFractionDigits: digits });
const figText = (name, v) => (v == null ? "–" : `${fmtNum(v)}${ex.units[name] ? " " + ex.units[name] : ""}`);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// ------------------------------------------------------------------ lifecycle

function resetExplore() {
  if (ex.plan) ex.plan.stop = true;
  Object.assign(ex, { points: [], byKey: new Map(), units: {}, axes: [], dirs: {}, axesTouched: false,
    plan: null, offset: 0, started: false, pareto: new Set(), hover: null, selected: null, total: pl() ? pl().combinations : 0 });
  if (!$("exploreView").classList.contains("hidden")) exploreShown();
}

function exploreShown() {
  if (!pl()) return;
  if (!ex.started) {
    ex.started = true;
    const given = [...pl().configurations.map((c) => c.selection), state.selection];
    runPlan([{ mode: "given", count: given.length, selections: given },
      { mode: "random", count: 300 }, { mode: "neighbors", count: 300 }]);
  }
  renderAxisPickers();
  renderExplore();
}

// ------------------------------------------------------------------ sampling

async function runPlan(steps) {
  if (ex.plan) return;
  const plan = ex.plan = { stop: false, done: 0, target: steps.reduce((n, s) => n + s.count, 0) };
  updateSampleControls();
  try {
    for (const step of steps) {
      if (plan.stop) break;
      if (step.mode === "given") {
        addPoints(await api("/api/explore", { productLine: state.pl, mode: "given", count: step.count, selections: step.selections }), plan);
        plan.done += step.count;
        continue;
      }
      let remaining = step.count;
      const worker = async () => {
        while (!plan.stop && remaining > 0) {
          const n = Math.min(BATCH, remaining);
          remaining -= n;
          const body = { productLine: state.pl, mode: step.mode, count: n, seed: Math.floor(Math.random() * 2 ** 31),
            known: [...ex.byKey.keys()] };
          if (step.mode === "all") {
            if (ex.offset >= ex.total) { remaining = 0; break; }
            body.offset = ex.offset;
            ex.offset = Math.min(ex.total, ex.offset + n);
          }
          if (step.mode === "neighbors") body.around = neighborBases();
          const res = await api("/api/explore", body);
          plan.done += n;
          addPoints(res, plan);
        }
      };
      await Promise.all(Array.from({ length: WORKERS }, worker));
    }
  } catch (err) {
    showError(err);
  } finally {
    if (ex.plan === plan) ex.plan = null;
    updateSampleControls();
  }
}

/** Admissible configurations to vary: the Pareto front first, then others at random. */
function neighborBases() {
  const valid = ex.points.filter((p) => p.status === "valid");
  if (!valid.length) return [];
  const front = valid.filter((p) => ex.pareto.has(p.key));
  const rest = valid.filter((p) => !ex.pareto.has(p.key)).sort(() => Math.random() - 0.5);
  return [...front.slice(0, 20), ...rest].slice(0, 40).map((p) => p.selection);
}

function addPoints(res, plan) {
  if (plan !== ex.plan) return; // reset meanwhile
  ex.total = res.total;
  Object.assign(ex.units, res.units);
  for (const p of res.points) {
    if (ex.byKey.has(p.key)) continue;
    p.source = res.mode;
    ex.byKey.set(p.key, p);
    ex.points.push(p);
  }
  if (!ex.axesTouched) autoAxes();
  computePareto();
  scheduleRender();
}

let renderQueued = false;
function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => { renderQueued = false; renderExplore(); });
}

// ------------------------------------------------------------------ axes & Pareto front

/** Key figures with numbers; `derived`: computed from other key figures (e.g. a price/performance ratio). */
function numericFigures() {
  const names = pl().keyFigures.map((k) => k.name);
  return pl().keyFigures.map((k) => {
    const values = new Set(ex.points.map((p) => p.figures[k.name]).filter(Number.isFinite));
    const derived = names.some((n) => n !== k.name && new RegExp(`\\b${n}\\b`).test(k.expression || ""));
    return { name: k.name, distinct: values.size, derived };
  }).filter((f) => f.distinct > 0);
}

function defaultDirection(name) {
  return /(preis|kosten|price|cost)$/i.test(name) ? -1 : 1;
}

function autoAxes() {
  const figs = numericFigures();
  if (!figs.length) return;
  // varied before constant, measured before derived; otherwise model order
  const varied = figs.filter((f) => f.distinct > 1).sort((a, b) => a.derived - b.derived);
  const pool = varied.length >= Math.min(3, figs.length) ? varied : [...varied, ...figs.filter((f) => f.distinct <= 1)];
  const price = pool.find((f) => defaultDirection(f.name) < 0);
  const order = price ? [price, ...pool.filter((f) => f !== price)] : pool;
  const axes = order.slice(0, 3).map((f) => f.name);
  if (axes.join() === ex.axes.join()) return;
  ex.axes = axes;
  for (const a of axes) if (!(a in ex.dirs)) ex.dirs[a] = defaultDirection(a);
  if (ex.axes.length < 3 && ex.view !== "xy") ex.view = "xy";
  renderAxisPickers();
}

function computePareto() {
  const dims = ex.axes;
  const cand = ex.points.filter((p) => p.status === "valid" && dims.length && dims.every((d) => Number.isFinite(p.figures[d])))
    .map((p) => ({ p, v: dims.map((d) => ex.dirs[d] * p.figures[d]) }))
    .sort((a, b) => b.v[0] - a.v[0] || b.v[1] - a.v[1] || (b.v[2] ?? 0) - (a.v[2] ?? 0));
  const front = [];
  const dominates = (a, b) => a.every((x, i) => x >= b[i]) && a.some((x, i) => x > b[i]);
  for (const c of cand) if (!front.some((f) => dominates(f.v, c.v))) front.push(c);
  ex.pareto = new Set(front.map((f) => f.p.key));
}

// ------------------------------------------------------------------ controls

function renderAxisPickers() {
  const host = $("axisPickers");
  if (!pl()) { host.innerHTML = ""; return; }
  const figs = numericFigures();
  const names = figs.length ? figs.map((f) => f.name) : pl().keyFigures.map((k) => k.name);
  const constant = new Set(figs.filter((f) => f.distinct === 1).map((f) => f.name));
  host.innerHTML = ex.axes.map((axis, i) => `
    <div class="axis-picker">
      <span class="axis-letter">${"XYZ"[i]}</span>
      <select data-axis="${i}" aria-label="Kennwert auf Achse ${"XYZ"[i]}">
        ${names.map((n) => `<option value="${esc(n)}" ${n === axis ? "selected" : ""}>${esc(n)}${constant.has(n) ? " (konstant)" : ""}</option>`).join("")}
      </select>
      <button type="button" class="dir" data-dir="${esc(axis)}" title="Für die Pareto-Front: ${ex.dirs[axis] > 0 ? "größer ist besser" : "kleiner ist besser"}">
        ${ex.dirs[axis] > 0 ? "↑ max" : "↓ min"}</button>
    </div>`).join("");
  host.querySelectorAll("select").forEach((sel) => sel.addEventListener("change", () => {
    ex.axesTouched = true;
    ex.axes[Number(sel.dataset.axis)] = sel.value;
    if (!(sel.value in ex.dirs)) ex.dirs[sel.value] = defaultDirection(sel.value);
    computePareto();
    renderAxisPickers();
    renderExplore();
  }));
  host.querySelectorAll("[data-dir]").forEach((btn) => btn.addEventListener("click", () => {
    ex.dirs[btn.dataset.dir] *= -1;
    computePareto();
    renderAxisPickers();
    renderExplore();
  }));
  document.querySelectorAll("#viewButtons button").forEach((b) => {
    b.disabled = b.dataset.plot !== "xy" && ex.axes.length < 3;
    b.classList.toggle("active", b.dataset.plot === ex.view);
    const [i, j] = { xy: [0, 1], xz: [0, 2], yz: [1, 2] }[b.dataset.plot] || [];
    b.title = i === undefined ? "Drehbare 3D-Ansicht" : `${ex.axes[i] || ""} gegen ${ex.axes[j] || ""}`;
  });
}

function updateSampleControls() {
  const running = !!ex.plan;
  ["sampleRandom", "sampleNeighbors", "sampleAll"].forEach((id) => { $(id).disabled = running; });
  $("sampleStop").classList.toggle("hidden", !running);
  $("progress").classList.toggle("hidden", !running);
  if (running) $("progressBar").style.width = `${Math.min(100, (100 * ex.plan.done) / Math.max(1, ex.plan.target))}%`;
  const total = ex.total || (pl() ? pl().combinations : 0);
  const left = Math.max(0, total - ex.offset);
  $("sampleAll").classList.toggle("hidden", !total || total > ALL_LIMIT || left === 0);
  $("sampleAll").textContent = ex.offset ? `Rest prüfen (${left.toLocaleString("de-DE")})` : `Alle ${total.toLocaleString("de-DE")} prüfen`;
  $("sampleAll").title = `Prüft jede Kombination, ca. ${Math.ceil(left / 40 / 60)} min`;
  $("sampleNeighbors").disabled = running || !ex.points.some((p) => p.status === "valid");
}

function bindExploreEvents() {
  $("sampleRandom").addEventListener("click", () => runPlan([{ mode: "random", count: 500 }]));
  $("sampleNeighbors").addEventListener("click", () => runPlan([{ mode: "neighbors", count: 500 }]));
  $("sampleAll").addEventListener("click", () => runPlan([{ mode: "all", count: Math.max(0, ex.total - ex.offset) }]));
  $("sampleStop").addEventListener("click", () => { if (ex.plan) ex.plan.stop = true; });
  $("showInvalid").addEventListener("change", (e) => { ex.showInvalid = e.target.checked; renderExplore(); });
  $("csvButton").addEventListener("click", downloadCsv);
  document.querySelectorAll("#viewButtons button").forEach((b) => b.addEventListener("click", () => {
    ex.view = b.dataset.plot;
    ex.hover = null;
    renderAxisPickers();
    renderExplore();
  }));

  const canvas = $("plot");
  canvas.addEventListener("pointerdown", (e) => {
    ex.drag = { x: e.clientX, y: e.clientY, moved: false };
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener("pointermove", (e) => {
    if (ex.drag && ex.view === "3d") {
      const dx = e.clientX - ex.drag.x, dy = e.clientY - ex.drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) ex.drag.moved = true;
      if (ex.drag.moved) {
        ex.yaw += dx * 0.008;
        ex.pitch = Math.max(-0.15, Math.min(1.55, ex.pitch + dy * 0.008));
        ex.drag.x = e.clientX; ex.drag.y = e.clientY;
        hideTooltip();
        drawPlot();
        return;
      }
    }
    updateHover(e);
  });
  canvas.addEventListener("pointerup", (e) => {
    const click = ex.drag && !ex.drag.moved;
    ex.drag = null;
    if (click) {
      const hit = hitTest(e);
      ex.selected = hit ? hit.key : null;
      renderPointDetails();
      drawPlot();
    }
  });
  canvas.addEventListener("pointerleave", () => { ex.hover = null; hideTooltip(); drawPlot(); });
  canvas.addEventListener("wheel", (e) => {
    if (ex.view !== "3d") return;
    e.preventDefault();
    ex.zoom = Math.max(0.5, Math.min(3, ex.zoom * Math.exp(-e.deltaY * 0.0015)));
    drawPlot();
  }, { passive: false });
  canvas.addEventListener("dblclick", () => { Object.assign(ex, VIEW_3D); drawPlot(); });
  new ResizeObserver(() => { if (!$("exploreView").classList.contains("hidden")) drawPlot(); }).observe($("plotWrap"));
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => drawPlot());
}

// ------------------------------------------------------------------ rendering

function renderExplore() {
  if ($("exploreView").classList.contains("hidden") || !pl()) return;
  renderStats();
  updateSampleControls();
  drawPlot();
  renderPointDetails();
  renderParetoTable();
}

function renderStats() {
  const n = ex.points.length;
  const valid = ex.points.filter((p) => p.status === "valid").length;
  const total = ex.total || pl().combinations;
  const share = (x, of) => `${fmtNum((100 * x) / of, x / of < 0.01 ? 3 : 1)} %`;
  // only the uniform draws estimate the admissible share; neighbours of admissible ones are biased towards it
  const uniform = ex.points.filter((p) => p.source === "random" || p.source === "all");
  const uniformValid = uniform.filter((p) => p.status === "valid").length;
  const estimate = uniform.length >= 20
    ? `Zulässiger Anteil laut ${uniform.length.toLocaleString("de-DE")} ${uniform.length === total ? "geprüften" : "zufällig gezogenen"} Kombinationen: <b>${share(uniformValid, uniform.length)}</b>.`
    : "";
  $("sampleStats").innerHTML = `
    <div><span class="num">${n.toLocaleString("de-DE")}</span><span class="lbl">geprüft</span></div>
    <div><span class="num">${valid.toLocaleString("de-DE")}</span><span class="lbl">zulässig</span></div>
    <div><span class="num">${ex.pareto.size.toLocaleString("de-DE")}</span><span class="lbl">Pareto-optimal</span></div>
    <p class="note">${share(n, total)} von ${total.toLocaleString("de-DE")} möglichen Kombinationen geprüft. ${estimate}</p>`;
}

/** 0, 0.2, 0.4 … : about `count` round steps covering [min, max]. */
function niceTicks(min, max, count = 5) {
  if (min === max) { const d = Math.abs(min) * 0.1 || 1; min -= d; max += d; }
  const raw = (max - min) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let t = lo; t <= hi + step / 2; t += step) ticks.push(Math.abs(t) < step / 1e6 ? 0 : t);
  const digits = Math.max(0, -Math.floor(Math.log10(step) + 1e-9)) + (step / mag === 2.5 ? 1 : 0);
  return { lo, hi, ticks, fmt: (v) => v.toLocaleString("de-DE", { maximumFractionDigits: digits, minimumFractionDigits: digits }) };
}

function visiblePoints() {
  return ex.points.filter((p) => (ex.showInvalid || p.status === "valid") && ex.axes.every((a) => Number.isFinite(p.figures[a])));
}

function axisTitle(i) {
  const name = ex.axes[i];
  return `${name}${ex.units[name] ? ` [${ex.units[name]}]` : ""} (${ex.dirs[name] > 0 ? "max" : "min"})`;
}

function setupCanvas() {
  const wrap = $("plotWrap"), canvas = $("plot");
  const w = wrap.clientWidth;
  const h = Math.round(ex.view === "3d" ? Math.max(360, Math.min(700, w * 0.72)) : Math.max(320, Math.min(620, w * 0.62)));
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
  canvas.style.width = `${w}px`; canvas.style.height = `${h}px`;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

const FONT = '12px system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
const FONT_TITLE = '600 12.5px system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';

/** Places text only where it does not collide with text already placed (titles first, then ticks).
 *  Drawing waits for `flush`, so labels end up above the points. */
class LabelLayer {
  constructor(ctx, w, h) { Object.assign(this, { ctx, w, h, boxes: [], queue: [] }); }
  box(text, x, y, align, baseline, font) {
    this.ctx.font = font;
    const tw = this.ctx.measureText(text).width, th = 14;
    const left = align === "left" ? x : align === "right" ? x - tw : x - tw / 2;
    const top = baseline === "top" ? y : baseline === "bottom" ? y - th : y - th / 2;
    return { left, top, right: left + tw, bottom: top + th };
  }
  free(b, pad = 3) {
    return b.left >= 2 && b.top >= 2 && b.right <= this.w - 2 && b.bottom <= this.h - 2 &&
      !this.boxes.some((o) => b.left < o.right + pad && b.right > o.left - pad && b.top < o.bottom + pad && b.bottom > o.top - pad);
  }
  /** `force`: keep the label, shifted into the canvas (axis titles). */
  draw(text, x, y, { align = "center", baseline = "middle", font = FONT, color, force = false } = {}) {
    let b = this.box(text, x, y, align, baseline, font);
    if (force) {
      const dx = Math.max(0, 4 - b.left) - Math.max(0, b.right - (this.w - 4));
      const dy = Math.max(0, 4 - b.top) - Math.max(0, b.bottom - (this.h - 4));
      x += dx; y += dy; b = this.box(text, x, y, align, baseline, font);
    } else if (!this.free(b)) return false;
    this.boxes.push(b);
    this.queue.push({ text, x, y, b, style: { font, fillStyle: color, textAlign: align, textBaseline: baseline } });
    return true;
  }
  flush(surface) {
    for (const { text, x, y, b, style } of this.queue) {
      this.ctx.fillStyle = surface; // knock out the points behind the text
      this.ctx.globalAlpha = 0.85;
      this.ctx.fillRect(b.left - 2, b.top, b.right - b.left + 4, b.bottom - b.top);
      this.ctx.globalAlpha = 1;
      Object.assign(this.ctx, style);
      this.ctx.fillText(text, x, y);
    }
    this.queue = [];
  }
}

function drawPlot() {
  if ($("exploreView").classList.contains("hidden")) return;
  const { ctx, w, h } = setupCanvas();
  const pts = visiblePoints();
  ex.screen = [];
  const empty = $("plotEmpty");
  if (!ex.axes.length || (!pts.length && !ex.plan)) {
    empty.textContent = !pl().keyFigures.length
      ? "Das Modell hat keine Kennwerte (Attribute mit Wert im System). Ohne sie gibt es keine Achsen."
      : ex.points.length ? "Keine Punkte mit Werten für alle drei Achsen." : "Noch keine Punkte. Starte eine Stichprobe.";
    empty.classList.remove("hidden");
  } else empty.classList.add("hidden");
  if (!ex.axes.length) return;

  const colors = { grid: css("--border"), axis: css("--border-strong"), text: css("--text-2"), title: css("--text"),
    valid: css("--ok"), invalid: css("--text-3"), pareto: css("--accent"), surface: css("--surface"), current: css("--text") };
  const scales = ex.axes.map((a) => {
    const vals = pts.map((p) => p.figures[a]);
    return vals.length ? niceTicks(Math.min(...vals), Math.max(...vals)) : niceTicks(0, 1);
  });
  const norm = (i, v) => (scales[i].hi === scales[i].lo ? 0 : ((v - scales[i].lo) / (scales[i].hi - scales[i].lo)) * 2 - 1);
  const labels = new LabelLayer(ctx, w, h);

  // painter's order: invalid, valid, Pareto, current/selected on top (3D re-sorts by depth)
  const rank = (p) => (p.status !== "valid" ? 0 : ex.pareto.has(p.key) ? 2 : 1);
  const placed = [];
  if (ex.view === "3d") draw3d(ctx, w, h, pts, norm, scales, colors, labels, placed);
  else draw2d(ctx, w, h, pts, norm, scales, colors, labels, placed);
  placed.sort((a, b) => (ex.view === "3d" ? b.depth - a.depth : 0) || rank(a.p) - rank(b.p));
  const currentKey = selectionKey(state.selection);
  for (const s of placed) {
    const { p } = s;
    const kind = p.status !== "valid" ? "invalid" : ex.pareto.has(p.key) ? "pareto" : "valid";
    const r = (kind === "pareto" ? 5 : kind === "valid" ? 4 : 3) * s.scale;
    ctx.globalAlpha = kind === "invalid" ? 0.45 : 1;
    ctx.beginPath(); ctx.arc(s.x, s.y, r + (kind === "invalid" ? 1 : 2), 0, 2 * Math.PI);
    ctx.fillStyle = colors.surface; ctx.fill();
    ctx.beginPath(); ctx.arc(s.x, s.y, r, 0, 2 * Math.PI);
    ctx.fillStyle = colors[kind]; ctx.fill();
    ctx.globalAlpha = 1;
    s.r = r;
    ex.screen.push(s);
  }
  labels.flush(colors.surface);
  for (const s of ex.screen) { // rings on top of everything
    const rings = [];
    if (s.p.key === currentKey) rings.push([colors.current, 4, 1.5]);
    if (s.p.key === ex.selected) rings.push([colors.pareto, 6, 2]);
    if (s.p.key === ex.hover) rings.push([colors.title, 4, 1]);
    for (const [color, gap, width] of rings) {
      ctx.beginPath(); ctx.arc(s.x, s.y, s.r + gap, 0, 2 * Math.PI);
      ctx.strokeStyle = color; ctx.lineWidth = width; ctx.stroke();
    }
  }
  $("plotHint").classList.toggle("hidden", ex.view !== "3d");
  $("paretoLegend").textContent = ex.view === "3d" || ex.axes.length < 3 ? "Pareto-optimal" : `Pareto-optimal (über ${ex.axes.join(", ")})`;
}

function draw2d(ctx, w, h, pts, norm, scales, colors, labels, placed) {
  const [i, j] = { xy: [0, 1], xz: [0, 2], yz: [1, 2] }[ex.view];
  const sx = scales[i], sy = scales[j];
  ctx.font = FONT;
  const yTickWidth = Math.max(...sy.ticks.map((t) => ctx.measureText(sy.fmt(t)).width));
  const m = { left: yTickWidth + 40, right: 24, top: 18, bottom: 54 };
  const X = (v) => m.left + ((norm(i, v) + 1) / 2) * (w - m.left - m.right);
  const Y = (v) => h - m.bottom - ((norm(j, v) + 1) / 2) * (h - m.top - m.bottom);

  ctx.lineWidth = 1;
  ctx.strokeStyle = colors.grid;
  for (const t of sx.ticks) { ctx.beginPath(); ctx.moveTo(Math.round(X(t)) + 0.5, m.top); ctx.lineTo(Math.round(X(t)) + 0.5, h - m.bottom); ctx.stroke(); }
  for (const t of sy.ticks) { ctx.beginPath(); ctx.moveTo(m.left, Math.round(Y(t)) + 0.5); ctx.lineTo(w - m.right, Math.round(Y(t)) + 0.5); ctx.stroke(); }
  ctx.strokeStyle = colors.axis;
  ctx.strokeRect(m.left + 0.5, m.top + 0.5, w - m.left - m.right - 1, h - m.top - m.bottom - 1);

  labels.draw(axisTitle(i), (m.left + w - m.right) / 2, h - 8, { baseline: "bottom", font: FONT_TITLE, color: colors.title, force: true });
  // the rotated y title is drawn directly; reserve its strip so no tick lands there
  labels.boxes.push({ left: 0, top: m.top, right: 22, bottom: h - m.bottom });
  ctx.save();
  ctx.translate(14, (m.top + h - m.bottom) / 2);
  ctx.rotate(-Math.PI / 2);
  Object.assign(ctx, { font: FONT_TITLE, fillStyle: colors.title, textAlign: "center", textBaseline: "middle" });
  ctx.fillText(axisTitle(j), 0, 0);
  ctx.restore();
  for (const t of sx.ticks) labels.draw(sx.fmt(t), X(t), h - m.bottom + 8, { baseline: "top", color: colors.text });
  for (const t of sy.ticks) labels.draw(sy.fmt(t), m.left - 8, Y(t), { align: "right", color: colors.text });

  for (const p of pts) placed.push({ p, x: X(p.figures[ex.axes[i]]), y: Y(p.figures[ex.axes[j]]), scale: 1, depth: 0 });
}

function draw3d(ctx, w, h, pts, norm, scales, colors, labels, placed) {
  // scale from a bound that holds for every turn around the vertical axis, so the cube neither clips nor
  // changes size while spinning: horizontally the floor diagonal (√2), vertically cos(pitch) + √2·sin(pitch),
  // both enlarged by the perspective
  const D = 8, fMax = D / (D - Math.sqrt(3));
  const halfH = (Math.cos(ex.pitch) + Math.SQRT2 * Math.abs(Math.sin(ex.pitch))) * fMax;
  const cx = w / 2, cy = h / 2;
  const sidePad = Math.min(150, w * 0.17); // room for tick labels and axis titles
  const S = Math.max(20, Math.min((w - 2 * sidePad) / (2 * Math.SQRT2 * fMax), (h - 70) / (2 * halfH)) * ex.zoom);
  const cyaw = Math.cos(ex.yaw), syaw = Math.sin(ex.yaw), cp = Math.cos(ex.pitch), sp = Math.sin(ex.pitch);
  // X and Y lie in the floor, Z points up; the camera looks along +y after the yaw, from `pitch` above
  const project = ([x, y, z]) => {
    const x1 = x * cyaw - y * syaw, y1 = x * syaw + y * cyaw;
    const up = z * cp + y1 * sp, depth = y1 * cp - z * sp;
    const f = D / (D + depth);
    return { x: cx + S * f * x1, y: cy - S * f * up, depth, f };
  };
  const line = (a, b, color) => {
    const p = project(a), q = project(b);
    ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(q.x, q.y); ctx.strokeStyle = color; ctx.stroke();
  };
  const vec = (axis, value, others) => { const v = [...others]; v.splice(axis, 0, value); return v; };
  const tn = scales.map((s, i) => s.ticks.map((t) => ({ t, n: norm(i, t) })));

  // grids on the three back faces
  ctx.lineWidth = 1;
  for (let a = 0; a < 3; a++) {
    const side = project(vec(a, 1, [0, 0])).depth > project(vec(a, -1, [0, 0])).depth ? 1 : -1;
    const [b, c] = [0, 1, 2].filter((k) => k !== a);
    for (const { n } of tn[b]) {
      const p = [0, 0, 0]; p[a] = side; p[b] = n; p[c] = -1;
      const q = [...p]; q[c] = 1;
      line(p, q, colors.grid);
    }
    for (const { n } of tn[c]) {
      const p = [0, 0, 0]; p[a] = side; p[c] = n; p[b] = -1;
      const q = [...p]; q[b] = 1;
      line(p, q, colors.grid);
    }
  }
  // cube edges
  for (let a = 0; a < 3; a++) {
    for (const u of [-1, 1]) for (const v of [-1, 1]) line(vec(a, -1, [u, v]), vec(a, 1, [u, v]), colors.axis);
  }

  // axis labels: X and Y on their lowest edge, Z on its leftmost one; text points away from the cube
  const centre = project([0, 0, 0]);
  const plans = [0, 1, 2].map((a) => {
    let best = null;
    for (const u of [-1, 1]) for (const v of [-1, 1]) {
      const mid = project(vec(a, 0, [u, v]));
      const score = a === 2 ? -mid.x : mid.y;
      if (!best || score > best.score) best = { score, others: [u, v], mid };
    }
    let dx = best.mid.x - centre.x, dy = best.mid.y - centre.y;
    const len = Math.hypot(dx, dy) || 1;
    dx /= len; dy /= len;
    return { a, ...best, dx, dy };
  });
  ctx.font = FONT;
  for (const pl3 of plans) {
    const align = pl3.dx > 0.35 ? "left" : pl3.dx < -0.35 ? "right" : "center";
    const baseline = pl3.dy > 0.35 ? "top" : pl3.dy < -0.35 ? "bottom" : "middle";
    const widest = Math.max(...scales[pl3.a].ticks.map((t) => ctx.measureText(scales[pl3.a].fmt(t)).width));
    // past the tick labels: their width when they sit beside the edge, their height when below/above it
    const reach = 12 + (align === "center" ? 20 : widest + 12);
    pl3.tick = { align, baseline };
    labels.draw(axisTitle(pl3.a), pl3.mid.x + pl3.dx * reach, pl3.mid.y + pl3.dy * reach,
      { align, baseline, font: FONT_TITLE, color: colors.title, force: true });
  }
  for (const pl3 of plans) {
    for (const { t, n } of tn[pl3.a]) {
      const p = project(vec(pl3.a, n, pl3.others));
      labels.draw(scales[pl3.a].fmt(t), p.x + pl3.dx * 12, p.y + pl3.dy * 12, { ...pl3.tick, color: colors.text });
    }
  }

  for (const p of pts) {
    const q = project(ex.axes.map((a, i) => norm(i, p.figures[a])));
    placed.push({ p, x: q.x, y: q.y, depth: q.depth, scale: Math.max(0.75, Math.min(1.3, q.f)) });
  }
}

// ------------------------------------------------------------------ hover, details, table

function hitTest(e) {
  const rect = $("plot").getBoundingClientRect();
  const x = e.clientX - rect.left, y = e.clientY - rect.top;
  let best = null, bestD = Infinity;
  for (let k = ex.screen.length - 1; k >= 0; k--) { // topmost first
    const s = ex.screen[k];
    const d = Math.hypot(s.x - x, s.y - y);
    if (d < Math.max(12, s.r + 6) && d < bestD - 0.5) { best = s; bestD = d; }
  }
  return best ? best.p : null;
}

function updateHover(e) {
  const p = hitTest(e);
  const key = p ? p.key : null;
  if (key !== ex.hover) { ex.hover = key; drawPlot(); }
  if (!p) { hideTooltip(); return; }
  const tip = $("tooltip");
  tip.innerHTML = `
    <div class="tip-head"><span class="pill ${p.status === "valid" ? "satisfied" : p.status === "invalid" ? "violated" : "undecided"}">${statusText(p)}</span>
      ${ex.pareto.has(p.key) ? `<span class="pill pareto">Pareto-optimal</span>` : ""}</div>
    <dl>${ex.axes.map((a) => `<dt>${esc(a)}</dt><dd>${esc(figText(a, p.figures[a]))}</dd>`).join("")}</dl>
    <div class="tip-sel">${esc(selectionSummary(p.selection))}</div>
    ${p.violated.length ? `<div class="tip-why">verletzt: ${esc(p.violated.slice(0, 3).join(", "))}${p.violated.length > 3 ? ` +${p.violated.length - 3}` : ""}</div>` : ""}
    <div class="tip-hint">Klicken für Details</div>`;
  tip.classList.remove("hidden");
  const wrap = $("plotWrap").getBoundingClientRect();
  let x = e.clientX - wrap.left + 14, y = e.clientY - wrap.top + 14;
  if (x + tip.offsetWidth > wrap.width - 4) x = e.clientX - wrap.left - tip.offsetWidth - 14;
  if (y + tip.offsetHeight > wrap.height - 4) y = e.clientY - wrap.top - tip.offsetHeight - 14;
  tip.style.left = `${Math.max(4, x)}px`;
  tip.style.top = `${Math.max(4, y)}px`;
}

function hideTooltip() { $("tooltip").classList.add("hidden"); }

const statusText = (p) => ({ valid: "zulässig", invalid: "nicht zulässig" }[p.status] || "offen");

function selectionSummary(sel) {
  return pl().variationPoints.filter((vp) => sel[vp.name]).map((vp) => sel[vp.name]).join(" · ");
}

function renderPointDetails() {
  const host = $("pointDetails");
  const p = ex.selected && ex.byKey.get(ex.selected);
  if (!p) {
    host.innerHTML = `<p class="muted">Klicke einen Punkt oder eine Zeile der Tabelle an, um die Konfiguration zu sehen.</p>`;
    return;
  }
  host.innerHTML = `
    <div class="tip-head"><span class="pill ${p.status === "valid" ? "satisfied" : p.status === "invalid" ? "violated" : "undecided"}">${statusText(p)}</span>
      ${ex.pareto.has(p.key) ? `<span class="pill pareto">Pareto-optimal</span>` : ""}</div>
    <dl class="point-figures">${pl().keyFigures.map((k) => `<dt>${esc(k.name)}</dt><dd>${esc(figText(k.name, p.figures[k.name]))}</dd>`).join("")}</dl>
    <dl class="point-selection">${pl().variationPoints.filter((vp) => p.selection[vp.name])
      .map((vp) => `<dt>${esc(vp.parent ? featurePath(vp) : vp.name)}</dt><dd>${esc(p.selection[vp.name])}</dd>`).join("")}</dl>
    ${p.violated.length ? `<p class="tip-why">verletzt: ${esc(p.violated.join(", "))}</p>` : ""}
    <button type="button" class="btn" id="openPoint">Im Konfigurator öffnen</button>`;
  $("openPoint").addEventListener("click", () => openInConfigurator(p));
}

function openInConfigurator(p) {
  state.selection = completeSelection({ ...p.selection });
  $("presetSelect").value = matchingPreset();
  renderVariationPoints();
  document.querySelector('.tab[data-view="configure"]').click();
  runCheck();
  window.scrollTo(0, 0);
}

function renderParetoTable() {
  const front = ex.points.filter((p) => ex.pareto.has(p.key));
  const [a0] = ex.axes;
  front.sort((p, q) => ex.dirs[a0] * (q.figures[a0] - p.figures[a0]));
  $("paretoCaption").textContent = front.length
    ? `${front.length} von ${ex.points.filter((p) => p.status === "valid").length} zulässigen · bezogen auf ${ex.axes.join(", ")}${front.length > PARETO_ROWS ? ` · erste ${PARETO_ROWS} gezeigt` : ""}`
    : "noch keine zulässigen Punkte";
  const head = `<thead><tr>${ex.axes.map((a) => `<th class="num">${esc(a)} ${ex.dirs[a] > 0 ? "↑" : "↓"}</th>`).join("")}<th>Auswahl</th><th></th></tr></thead>`;
  const rows = front.slice(0, PARETO_ROWS).map((p) => `
    <tr class="${p.key === ex.selected ? "selected-row" : ""}" data-key="${esc(p.key)}">
      ${ex.axes.map((a) => `<td class="num">${esc(figText(a, p.figures[a]))}</td>`).join("")}
      <td class="sel-cell">${esc(selectionSummary(p.selection))}</td>
      <td class="num"><button type="button" class="btn small" data-open="${esc(p.key)}">öffnen</button></td>
    </tr>`).join("");
  const table = $("paretoTable");
  table.innerHTML = head + `<tbody>${rows || `<tr><td class="muted" colspan="${ex.axes.length + 2}">keine</td></tr>`}</tbody>`;
  table.querySelectorAll("tr[data-key]").forEach((tr) => tr.addEventListener("click", (e) => {
    const p = ex.byKey.get(tr.dataset.key);
    if (e.target.closest("[data-open]")) { openInConfigurator(p); return; }
    ex.selected = p.key;
    renderPointDetails();
    renderParetoTable();
    drawPlot();
  }));
}

function downloadCsv() {
  const vps = pl().variationPoints, figs = pl().keyFigures.map((k) => k.name);
  const q = (s) => `"${String(s ?? "").replace(/"/g, '""')}"`;
  const lines = [[...vps.map((vp) => vp.name), "status", "paretoOptimal", ...figs, "verletzteRegeln"].map(q).join(";")];
  for (const p of ex.points) {
    lines.push([...vps.map((vp) => q(p.selection[vp.name])), q(p.status), q(ex.pareto.has(p.key) ? "ja" : "nein"),
      ...figs.map((f) => (Number.isFinite(p.figures[f]) ? String(p.figures[f]).replace(".", ",") : "")), q(p.violated.join(", "))].join(";"));
  }
  const url = URL.createObjectURL(new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: `loesungsraum_${pl().name}.csv` });
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

bindExploreEvents();
