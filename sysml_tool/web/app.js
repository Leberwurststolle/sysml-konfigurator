"use strict";

const state = {
  ds: null,            // design space from the extractor
  pl: 0,               // index of the product line
  selection: {},       // variation point name -> variant name
  result: null,        // /api/check for the current selection
  alternatives: null,  // /api/alternatives for the current selection
  seq: 0,              // guards against out-of-order responses
  expanded: new Set(), // variant cards showing all attributes
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const shortName = (qn) => (qn || "").split("::").pop();
const MARK = { satisfied: "✓", violated: "✗", undecided: "?", notApplicable: "–" };
const SCOPE = { component: "Bauteil", interface: "Schnittstelle" };

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) {
    const diags = (data.diagnostics || []).map((d) => `\n  ${d.file}:${d.line}:${d.col}  ${d.message}`).join("");
    throw new Error((data.error || res.statusText) + diags);
  }
  return data;
}

function showError(err) {
  const banner = $("errorBanner");
  if (!err) { banner.classList.add("hidden"); return; }
  banner.textContent = err.message || String(err);
  banner.classList.remove("hidden");
}

const pl = () => state.ds.productLines[state.pl];

// ------------------------------------------------------------------ helpers

function valueText(v) {
  return v ? v.display : "–";
}

/** Attributes of a variation point that rules, key figures or requirements refer to (`cpu.sockel`). */
function relevantAttributes(vp) {
  const p = pl();
  const exprs = [
    ...p.rules.map((r) => r.expression),
    ...p.keyFigures.map((k) => k.expression),
    ...p.requirements.flatMap((q) => q.constraints.map((c) => c.expression)),
  ].filter(Boolean);
  const re = new RegExp(`(?:^|[^\\w])${vp.name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\.(\\w+)`, "g");
  const found = [];
  for (const e of exprs) {
    for (const m of e.matchAll(re)) {
      if (vp.attributeNames.includes(m[1]) && !found.includes(m[1])) found.push(m[1]);
    }
  }
  return found.length ? vp.attributeNames.filter((a) => found.includes(a)) : vp.attributeNames.slice(0, 4);
}

/** "AMD · Ryzen 5 7600": the first two string attributes describe a variant well in most catalogs. */
function variantLabel(variant) {
  return Object.values(variant.attributes)
    .filter((v) => v && v.kind === "string" && v.value && v.value !== "-")
    .slice(0, 2).map((v) => v.value).join(" · ");
}

function selectedVariant(vpName) {
  const vp = pl().variationPoints.find((x) => x.name === vpName);
  return vp && vp.variants.find((v) => v.name === state.selection[vpName]);
}

/** Top-level variation points, plus nested ones whose variant is selected (parents come first). */
function activeVariationPoints(selection) {
  const active = [];
  const names = new Set();
  for (const vp of pl().variationPoints) {
    const p = vp.parent;
    if (!p || (names.has(p.variationPoint) && selection[p.variationPoint] === p.variant)) {
      active.push(vp);
      names.add(vp.name);
    }
  }
  return active;
}

/** Drop choices of inactive nested variation points; choose the first variant of newly active ones. */
function completeSelection(selection) {
  let out = { ...selection };
  for (;;) {
    const active = activeVariationPoints(out);
    const cleaned = Object.fromEntries(Object.entries(out).filter(([k]) => active.some((vp) => vp.name === k)));
    for (const vp of active) {
      if (vp.parent && !cleaned[vp.name] && vp.variants.length) cleaned[vp.name] = vp.variants[0].name;
    }
    if (JSON.stringify(cleaned) === JSON.stringify(out)) return out;
    out = cleaned;
  }
}

/** `gehaeuse.farbe` for the nested variation point `gehaeuse/midiTower/farbe`. */
function featurePath(vp) {
  if (!vp.parent) return vp.feature || vp.name;
  const parent = pl().variationPoints.find((x) => x.name === vp.parent.variationPoint);
  return `${featurePath(parent)}.${vp.feature}`;
}

function vpTitle(vp) {
  return vp.parent ? `${vp.feature} (bei ${featurePath(pl().variationPoints.find((x) => x.name === vp.parent.variationPoint))} = ${vp.parent.variant})` : vp.name;
}

/** Values of `vp.attr` references in an expression, read from the selected variants. */
function involvedValues(expression) {
  const out = [];
  if (!expression) return out;
  for (const vp of activeVariationPoints(state.selection)) {
    const variant = selectedVariant(vp.name);
    if (!variant) continue;
    const path = featurePath(vp);
    if (vp.parent && vp.feature in variant.attributes && expression.includes(path)) {
      out.push([path, valueText(variant.attributes[vp.feature])]);
      continue;
    }
    const re = new RegExp(`(?:^|[^\\w.])${path.replace(/\./g, "\\.")}\\.(\\w+)`, "g");
    for (const m of expression.matchAll(re)) {
      const key = `${path}.${m[1]}`;
      if (m[1] in variant.attributes && !out.some((o) => o[0] === key)) out.push([key, valueText(variant.attributes[m[1]])]);
    }
  }
  return out;
}

function formatDelta(diff, unit) {
  const rounded = Math.round(diff * 100) / 100;
  return `${rounded > 0 ? "+" : "−"}${Math.abs(rounded).toLocaleString("de-DE")}${unit ? " " + unit : ""}`;
}

// ------------------------------------------------------------------ init

async function init() {
  const initialHash = location.hash; // read before the first check rewrites it
  try {
    state.ds = await api("/api/design-space");
  } catch (err) {
    showError(err);
    return;
  }
  setupProductLines();
  bindStaticEvents();
  applyHash(initialHash);
}

// The URL hash keeps the view and the selection: #view=model&pl=0&cfg=cpu.ryzen5,mainboard.b650m
function applyHash(hash) {
  const params = new URLSearchParams(hash.slice(1));
  const view = params.get("view");
  if (view && view !== "configure") document.querySelector(`.tab[data-view="${CSS.escape(view)}"]`)?.click();
  const cfg = params.get("cfg");
  if (!cfg || !pl()) return;
  const plIndex = Number(params.get("pl") || 0);
  if (plIndex !== state.pl && state.ds.productLines[plIndex]) {
    state.pl = plIndex;
    $("productLineSelect").value = String(plIndex);
    startProductLine();
  }
  for (const pair of cfg.split(",")) {
    const dot = pair.lastIndexOf(".");
    const [vp, variant] = [pair.slice(0, dot), pair.slice(dot + 1)].map(decodeURIComponent);
    const point = pl().variationPoints.find((x) => x.name === vp);
    if (point && point.variants.some((v) => v.name === variant)) state.selection[vp] = variant;
  }
  state.selection = completeSelection(state.selection);
  $("presetSelect").value = matchingPreset();
  renderVariationPoints();
  runCheck();
}

function writeHash() {
  const view = document.querySelector(".tab.active")?.dataset.view || "configure";
  const cfg = Object.entries(state.selection).filter(([, v]) => v)
    .map(([k, v]) => `${encodeURIComponent(k)}.${encodeURIComponent(v)}`).join(",");
  history.replaceState(null, "", `#view=${view}&pl=${state.pl}&cfg=${cfg}`);
}

function setupProductLines() {
  const select = $("productLineSelect");
  const lines = state.ds.productLines;
  select.innerHTML = lines.map((p, i) => `<option value="${i}">${esc(p.qualifiedName)}</option>`).join("");
  select.classList.toggle("hidden", lines.length < 2);
  if (state.pl >= lines.length) state.pl = 0;
  select.value = String(state.pl);
  if (!lines.length) {
    $("subtitle").textContent = `${state.ds.files.length} Dateien · keine Variationspunkte gefunden`;
    $("variationPoints").innerHTML = `<div class="card">Das Modell enthält keine <code>variation</code>-Elemente. Lege eine Produktlinie mit Variationspunkten an, um sie hier zu konfigurieren.</div>`;
    renderModelView();
    return;
  }
  startProductLine();
}

function startProductLine() {
  const p = pl();
  const nested = p.variationPoints.filter((vp) => vp.parent).length;
  $("subtitle").textContent =
    `${p.qualifiedName} · ${p.variationPoints.length - nested} Variationspunkte${nested ? ` (+${nested} abhängige)` : ""} · ${p.combinations.toLocaleString("de-DE")} Kombinationen · ${state.ds.generator.toolkit}`;
  $("subtitle").title = $("subtitle").textContent; // full text when the top bar truncates it
  const presets = $("presetSelect");
  presets.innerHTML = `<option value="">— eigene Auswahl —</option>` +
    p.configurations.map((c, i) => `<option value="${i}">${esc(c.name)}</option>`).join("");
  if (p.configurations.length) {
    presets.value = "0";
    state.selection = completeSelection(p.configurations[0].selection);
  } else {
    state.selection = completeSelection(Object.fromEntries(
      p.variationPoints.filter((vp) => !vp.parent).map((vp) => [vp.name, vp.variants[0]?.name])));
  }
  renderVariationPoints();
  renderModelView();
  resetExplore();
  runCheck();
}

function bindStaticEvents() {
  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => {
      t.classList.toggle("active", t === tab);
      t.setAttribute("aria-selected", t === tab);
    });
    document.querySelectorAll(".tab").forEach((t) => $(`${t.dataset.view}View`).classList.toggle("hidden", t !== tab));
    writeHash();
    if (tab.dataset.view === "explore") exploreShown();
  }));
  $("productLineSelect").addEventListener("change", (e) => { state.pl = Number(e.target.value); startProductLine(); });
  $("presetSelect").addEventListener("change", (e) => {
    if (e.target.value === "") return;
    state.selection = completeSelection(pl().configurations[Number(e.target.value)].selection);
    renderVariationPoints();
    runCheck();
  });
  $("reloadButton").addEventListener("click", reloadModel);
  $("snippetName").addEventListener("input", renderSnippet);
  $("copyButton").addEventListener("click", async () => {
    const text = $("snippet").textContent;
    try {
      await navigator.clipboard.writeText(text);
      $("copyButton").textContent = "Kopiert ✓";
    } catch {
      const range = document.createRange();
      range.selectNodeContents($("snippet"));
      getSelection().removeAllRanges();
      getSelection().addRange(range);
      $("copyButton").textContent = "Markiert";
    }
    setTimeout(() => { $("copyButton").textContent = "Kopieren"; }, 1500);
  });
}

async function reloadModel() {
  const btn = $("reloadButton");
  btn.disabled = true;
  btn.textContent = "Lade …";
  try {
    state.ds = await api("/api/reload", {});
    showError(null);
    const keep = { ...state.selection };
    setupProductLines();
    // keep the user's selection where it still exists
    const p = pl();
    if (p) {
      for (const vp of p.variationPoints) {
        if (vp.variants.some((v) => v.name === keep[vp.name])) state.selection[vp.name] = keep[vp.name];
      }
      state.selection = completeSelection(state.selection);
      $("presetSelect").value = "";
      renderVariationPoints();
      runCheck();
    }
  } catch (err) {
    showError(err);
  } finally {
    btn.disabled = false;
    btn.textContent = "Modell neu laden";
  }
}

// ------------------------------------------------------------ configure view

function renderVariationPoints() {
  const container = $("variationPoints");
  container.innerHTML = activeVariationPoints(state.selection).map((vp) => {
    const relevant = relevantAttributes(vp);
    const cards = vp.variants.map((v) => {
      const id = `${vp.name}::${v.name}`;
      const shown = state.expanded.has(id) ? vp.attributeNames : relevant;
      const attrs = shown.filter((a) => a in v.attributes)
        .map((a) => `<dt>${esc(a)}</dt><dd>${esc(valueText(v.attributes[a]))}</dd>`).join("");
      const hiddenCount = vp.attributeNames.length - relevant.length;
      const toggle = hiddenCount > 0
        ? `<button type="button" class="more" data-expand="${esc(id)}">${state.expanded.has(id) ? "weniger" : `alle ${vp.attributeNames.length} Attribute`}</button>` : "";
      return `
        <label class="variant ${state.selection[vp.name] === v.name ? "selected" : ""}" data-vp="${esc(vp.name)}" data-variant="${esc(v.name)}" title="${esc(v.doc || "")}">
          <input type="radio" name="vp-${esc(vp.name)}" value="${esc(v.name)}" ${state.selection[vp.name] === v.name ? "checked" : ""}>
          <div class="variant-top">
            <div>
              <div class="variant-name">${esc(v.name)}</div>
              <div class="variant-type">${esc(shortName(v.type))}</div>
            </div>
            <span class="badge unknown" data-badge></span>
          </div>
          ${variantLabel(v) ? `<div class="variant-label">${esc(variantLabel(v))}</div>` : ""}
          <dl class="attrs">${attrs}</dl>
          <div class="variant-foot" data-foot></div>
          ${toggle}
        </label>`;
    }).join("");
    return `
      <section class="vp ${vp.parent ? "nested" : ""}" data-vp-section="${esc(vp.name)}">
        <div class="vp-head">
          <h3>${esc(vpTitle(vp))}</h3>
          <span class="vp-type">${esc(vp.type || "")}</span>
          <span class="vp-chosen">${vp.variants.length} Varianten</span>
          ${vp.doc ? `<div class="vp-doc">${esc(vp.doc)}</div>` : ""}
        </div>
        <div class="variants">${cards}</div>
      </section>`;
  }).join("");

  container.querySelectorAll("input[type=radio]").forEach((input) => input.addEventListener("change", () => {
    const card = input.closest(".variant");
    const before = activeVariationPoints(state.selection).map((vp) => vp.name).join();
    state.selection = completeSelection({ ...state.selection, [card.dataset.vp]: card.dataset.variant });
    $("presetSelect").value = matchingPreset();
    if (activeVariationPoints(state.selection).map((vp) => vp.name).join() !== before) {
      renderVariationPoints(); // nested variation points appeared or disappeared
    } else {
      container.querySelectorAll(`.variant[data-vp="${CSS.escape(card.dataset.vp)}"]`)
        .forEach((c) => c.classList.toggle("selected", c === card));
    }
    runCheck();
  }));
  container.querySelectorAll("[data-expand]").forEach((btn) => btn.addEventListener("click", (e) => {
    e.preventDefault();
    const id = btn.dataset.expand;
    state.expanded.has(id) ? state.expanded.delete(id) : state.expanded.add(id);
    renderVariationPoints();
    renderAlternatives();
  }));
}

function matchingPreset() {
  const i = pl().configurations.findIndex((c) => {
    const cfg = completeSelection(c.selection);
    return pl().variationPoints.every((vp) => cfg[vp.name] === state.selection[vp.name]);
  });
  return i < 0 ? "" : String(i);
}

async function runCheck() {
  const seq = ++state.seq;
  const body = { productLine: state.pl, selection: state.selection, name: $("snippetName").value };
  writeHash();
  setChecking(true);
  try {
    const result = await api("/api/check", body);
    if (seq !== state.seq) return;
    state.result = result;
    showError(null);
    renderResult();
    const alternatives = await api("/api/alternatives", body);
    if (seq !== state.seq) return;
    state.alternatives = alternatives;
    renderAlternatives();
  } catch (err) {
    if (seq === state.seq) showError(err);
  } finally {
    if (seq === state.seq) setChecking(false);
  }
}

function setChecking(on) {
  $("statusCard").classList.toggle("checking", on);
  if (on && !state.result) {
    $("statusTitle").textContent = "Wird geprüft";
  }
}

function renderResult() {
  const r = state.result;
  const p = pl();
  const applicable = r.rules.filter((x) => x.verdict !== "notApplicable");
  const violated = applicable.filter((x) => x.verdict === "violated");
  const undecided = applicable.filter((x) => x.verdict === "undecided");

  const card = $("statusCard");
  card.classList.remove("valid", "invalid", "unknown");
  card.classList.add(r.status);
  $("statusIcon").textContent = { valid: "✓", invalid: "✗", unknown: "?" }[r.status];
  $("statusTitle").textContent = { valid: "Zulässig", invalid: "Nicht zulässig", unknown: r.complete ? "Nicht entscheidbar" : "Unvollständig" }[r.status];
  $("statusSub").textContent =
    r.status === "valid" ? `Alle ${applicable.length} Regeln erfüllt` :
    r.status === "invalid" ? `${violated.length} von ${applicable.length} Regeln verletzt` :
    !r.complete ? "Nicht für jeden Variationspunkt ist eine Variante gewählt" :
    `${undecided.length} Regeln konnten nicht ausgewertet werden`;

  $("figures").innerHTML = p.keyFigures.map((k) =>
    `<dt title="${esc(k.expression || "")}">${esc(k.name)}</dt><dd>${esc(valueText(r.keyFigures[k.name]))}</dd>`).join("");

  // rules: violated first, then undecided, then satisfied
  const order = { violated: 0, undecided: 1, satisfied: 2 };
  const rules = [...applicable].sort((a, b) => order[a.verdict] - order[b.verdict] || a.index - b.index);
  $("rulesCount").textContent = `${applicable.length - violated.length - undecided.length}/${applicable.length}`;
  $("rulesList").innerHTML = rules.map((rule) => {
    const values = [...involvedValues(rule.expression)];
    for (const [k, v] of Object.entries(rule.bindings || {})) {
      if (!values.some((x) => x[0] === k)) values.push([k, v.display]);
    }
    const valuesHtml = values.length
      ? `<span class="check-values">${values.map(([k, v]) => `${esc(k)} = <b>${esc(v)}</b>`).join(" · ")}</span>` : "";
    const reason = rule.verdict === "undecided" && rule.detail ? `<span class="check-values">${esc(rule.detail)}</span>` : "";
    return `
      <li class="${rule.verdict}">
        <div class="check-row">
          <span class="mark ${rule.verdict}">${MARK[rule.verdict]}</span>
          <div>
            <span class="check-name">${esc(rule.label || rule.name || `Regel ${rule.index + 1}`)}</span>
            ${SCOPE[rule.scope] ? `<span class="scope-tag" title="${esc(rule.context || "")}">${SCOPE[rule.scope]} · ${esc(rule.context || "")}</span>` : ""}
            <code class="check-expr">${esc(rule.expression || "")}</code>
            ${rule.verdict !== "satisfied" ? valuesHtml + reason : ""}
          </div>
        </div>
      </li>`;
  }).join("");

  $("requirementsList").innerHTML = r.requirements.length ? r.requirements.map((q) => {
    const meta = p.requirements[q.index];
    const constraints = q.constraints.map((c) => {
      const verdict = c.verdict || "undecided";
      const values = Object.entries(c.bindings || {}).map(([k, v]) => `${esc(k)} = <b>${esc(v.display)}</b>`).join(" · ");
      return `<li><span class="mark ${verdict}">${MARK[verdict]}</span><div><code class="check-expr">${c.kind === "assumption" ? "assume " : ""}${esc(c.expression || "?")}</code>${values ? `<span class="check-values">${values}</span>` : ""}</div></li>`;
    }).join("");
    return `
      <li class="${q.verdict}">
        <div class="check-row">
          <span class="mark ${q.verdict}">${MARK[q.verdict]}</span>
          <div>
            <span class="check-name">${esc(q.name)}</span>
            ${meta.doc ? `<span class="check-values">${esc(meta.doc)}</span>` : ""}
          </div>
        </div>
        <ul class="sublist">${constraints}</ul>
      </li>`;
  }).join("") : `<li class="muted">Keine Anforderungen mit passendem Subjekt im Modell.</li>`;

  renderSnippet();
}

function renderSnippet() {
  if (!state.result) return;
  const name = $("snippetName").value.trim() || "meineKonfiguration";
  const safe = /^[A-Za-z_][A-Za-z0-9_]*$/.test(name) ? name : `'${name.replace(/'/g, "\\'")}'`;
  $("snippet").textContent = state.result.snippet.replace(/^(\S+ )(\S+)/, `$1${safe}`);
}

function renderAlternatives() {
  const alt = state.alternatives;
  const r = state.result;
  if (!alt || !r) return;
  const numericFigures = pl().keyFigures.filter((k) => r.keyFigures[k.name] && typeof r.keyFigures[k.name].value === "number");
  document.querySelectorAll(".variant").forEach((card) => {
    const info = alt[card.dataset.vp]?.[card.dataset.variant];
    const badge = card.querySelector("[data-badge]");
    const foot = card.querySelector("[data-foot]");
    if (!info) return;
    const isCurrent = state.selection[card.dataset.vp] === card.dataset.variant;
    badge.className = `badge ${info.status}`;
    badge.textContent = info.status === "valid" ? "passt" : info.status === "invalid" ? "Konflikt" : "offen";
    badge.title = info.violatedRules.length ? `Verletzt: ${info.violatedRules.join(", ")}` : "";

    const parts = [];
    if (!isCurrent) {
      for (const k of numericFigures) {
        const base = r.keyFigures[k.name];
        const other = info.keyFigures[k.name];
        if (!other || typeof other.value !== "number") continue;
        const diff = other.value - base.value;
        if (Math.abs(diff) < 1e-9) continue;
        parts.push(`<span class="delta ${diff > 0 ? "up" : "down"}"><span class="muted">${esc(k.name)}</span> ${esc(formatDelta(diff, base.unit))}</span>`);
      }
    }
    if (info.violatedRules.length) {
      parts.push(`<span class="why">${esc(info.violatedRules.join(", "))}</span>`);
    }
    foot.innerHTML = parts.join("");
  });
}

// ------------------------------------------------------------------ model view

function renderModelView() {
  const ds = state.ds;
  const p = ds.productLines[state.pl];
  const container = $("modelContent");
  if (!p) {
    container.innerHTML = filesSection(ds);
    return;
  }
  const variantCount = p.variationPoints.reduce((n, vp) => n + vp.variants.length, 0);
  const stats = [
    [p.variationPoints.length, "Variationspunkte"], [variantCount, "Varianten"],
    [p.combinations.toLocaleString("de-DE"), "Kombinationen"], [p.rules.length, "Regeln"],
    [p.requirements.length, "Anforderungen"], [p.configurations.length, "Konfigurationen im Modell"],
    [ds.elementCount.toLocaleString("de-DE"), "Modellelemente"],
  ];
  let html = `
    <div>
      <h2 class="section-title">${esc(p.qualifiedName)}</h2>
      <p class="section-doc">${esc(p.doc || "")} ${p.types.length ? `Typ: <code>${esc(p.types.join(", "))}</code>` : ""}</p>
    </div>
    <div class="stats">${stats.map(([n, l]) => `<div class="stat"><div class="num">${esc(n)}</div><div class="lbl">${esc(l)}</div></div>`).join("")}</div>`;

  for (const vp of p.variationPoints) {
    const head = ["Variante", "Typ", ...vp.attributeNames];
    const rows = vp.variants.map((v) => [
      { text: v.name, sort: v.name }, { text: shortName(v.type), sort: v.type, mono: true },
      ...vp.attributeNames.map((a) => {
        const val = v.attributes[a];
        return { text: valueText(val), sort: val && typeof val.value === "number" ? val.value : valueText(val), num: val && typeof val.value === "number" };
      }),
    ]);
    html += table(vpTitle(vp), vp.type, head, rows);
  }

  html += table("Regeln", "assert constraint im System, in den Bauteil-Definitionen und in den Schnittstellen",
    ["Name", "Art", "Ausdruck (geprüft)", "im Modell", "definiert in"],
    p.rules.map((r) => [{ text: r.label || r.name || "(anonym)" },
      { text: SCOPE[r.scope] ? `${SCOPE[r.scope]} · ${r.context}` : "System" },
      { text: r.expression || "?", mono: true },
      { text: r.sourceExpression && r.sourceExpression !== r.expression ? r.sourceExpression : "", mono: true },
      { text: r.owner + (r.appliesTo ? ` (nur ${Object.entries(r.appliesTo).map(([k, v]) => `${k} = ${v.join("/")}`).join(", ")})` : ""), mono: true }]));
  html += table("Kennwerte", "berechnete Attribute", ["Name", "Berechnung", "definiert in"],
    p.keyFigures.map((k) => [{ text: k.name }, { text: k.expression || "?", mono: true }, { text: k.owner, mono: true }]));
  html += table("Anforderungen", "Subjekt passt zur Produktlinie", ["Name", "Beschreibung", "Bedingungen"],
    p.requirements.map((q) => [{ text: q.name }, { text: q.doc || "" },
      { html: q.constraints.map((c) => `<code class="mono">${esc(c.expression || "?")}</code>`).join("<br>") }]));
  html += table("Konfigurationen im Modell", "", ["Name", "Beschreibung", ...p.variationPoints.map((vp) => vp.parent ? featurePath(vp) : vp.name)],
    p.configurations.map((c) => [{ text: c.name }, { text: c.doc || "" }, ...p.variationPoints.map((vp) => ({ text: c.selection[vp.name] || "–" }))]));

  const claims = ds.modelVerification.satisfyClaims;
  html += table("Prüfung des Modells", `sysmlv2 verify: ${ds.modelVerification.counts.satisfied} erfüllt, ${ds.modelVerification.counts.violated} verletzt, ${ds.modelVerification.counts.undecided} offen`,
    ["satisfy", "durch", "Ergebnis"],
    claims.map((c) => [{ text: shortName(c.requirement) }, { text: c.satisfiedBy }, { html: `<span class="pill ${c.verdict}">${esc(c.verdict)}</span>` }]));

  html += filesSection(ds);
  container.innerHTML = html;
  container.querySelectorAll("table[data-sortable]").forEach(makeSortable);
}

function filesSection(ds) {
  const warnings = [...ds.warnings, ...ds.diagnostics.map((d) => `${d.severity}: ${d.message} (${shortName((d.file || "").replace(/\\/g, "/").split("/").pop())}:${d.line})`)];
  return table("Dateien", `${ds.generator.toolkit} · ${ds.extractionSeconds} s`, ["Datei"], ds.files.map((f) => [{ text: f, mono: true }])) +
    (warnings.length ? table("Hinweise", "", ["Meldung"], warnings.map((w) => [{ text: w }])) : "");
}

function table(title, caption, head, rows) {
  // right-align a header over a column of numbers, like its cells
  const numCol = (i) => rows.length > 0 && rows.every((cells) => cells[i]?.num || cells[i]?.text === "–");
  const th = head.map((h, i) => `<th data-col="${i}" class="${numCol(i) ? "num" : ""}">${esc(h)}</th>`).join("");
  const body = rows.map((cells) => `<tr>${cells.map((c) =>
    `<td class="${c.num ? "num" : ""}" data-sort="${esc(c.sort ?? c.text ?? "")}">${c.html ?? (c.mono ? `<span class="mono">${esc(c.text)}</span>` : esc(c.text))}</td>`).join("")}</tr>`).join("");
  return `
    <div class="table-wrap">
      <div class="tbl-caption"><h3>${esc(title)}</h3><span class="muted">${esc(caption || "")}</span></div>
      <table data-sortable><thead><tr>${th}</tr></thead><tbody>${body || `<tr><td class="muted" colspan="${head.length}">keine</td></tr>`}</tbody></table>
    </div>`;
}

function makeSortable(tableEl) {
  tableEl.querySelectorAll("th").forEach((th) => th.addEventListener("click", () => {
    const col = Number(th.dataset.col);
    const asc = !(th.classList.contains("sorted") && th.classList.contains("asc"));
    tableEl.querySelectorAll("th").forEach((x) => x.classList.remove("sorted", "asc"));
    th.classList.add("sorted");
    if (asc) th.classList.add("asc");
    const rows = [...tableEl.tBodies[0].rows];
    rows.sort((a, b) => {
      const x = a.cells[col]?.dataset.sort ?? "", y = b.cells[col]?.dataset.sort ?? "";
      const nx = Number(x), ny = Number(y);
      const cmp = x !== "" && y !== "" && !isNaN(nx) && !isNaN(ny) ? nx - ny : x.localeCompare(y, "de");
      return asc ? cmp : -cmp;
    });
    rows.forEach((r) => tableEl.tBodies[0].appendChild(r));
  }));
}

init();
