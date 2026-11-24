/* Rumil library window. Plain JS, no build step. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// DOIs may contain '<', '>', ';' and '#' (10.1130/0091-7613(2001)029<0987:HSESAT>2.0.CO;2): encode each part.
const doiUrl = (doi) => "https://doi.org/" + String(doi).split("/").map(encodeURIComponent).join("/");

const STATUS = {
  verified: ["ok", "Verified"], verified_no_doi: ["ok", "Verified (no DOI)"], url_ok: ["ok", "Link works"],
  differs: ["warn", "Differs from registry"], mismatch: ["warn", "Different work?"], doi_found: ["warn", "DOI found"],
  unverified: ["warn", "Unverified"],
  url_dead: ["warn", "Dead link"], retracted: ["bad", "Retracted"], doi_invalid: ["bad", "DOI not found"],
  error: ["idle", "Check failed"], unchecked: ["idle", "Not checked"],
};
const SPECIALS = [
  { id: "all", label: "All references" },
  { id: "recent", label: "Recently added", special: "recent" },
  { id: "unfiled", label: "Unfiled", special: "unfiled" },
  { id: "attention", label: "Needs attention", status: "attention", dot: "warn" },
  { id: "problem", label: "Retracted / bad DOI", status: "problem", dot: "bad" },
  { id: "unchecked", label: "Not yet verified", status: "unchecked", dot: "idle" },
  { id: "duplicates", label: "Duplicates", special: "duplicates" },
  { id: "trash", label: "Trash", special: "trash" },
];
const TYPES = ["article-journal", "paper-conference", "chapter", "book", "report", "thesis", "article", "dataset",
  "software", "webpage", "manuscript", "speech", "map", "document", "article-magazine", "article-newspaper",
  "entry-encyclopedia", "patent", "post-weblog", "standard"];
const TYPE_LABEL = { "article-journal": "Journal article", "paper-conference": "Conference paper / abstract", chapter: "Book chapter",
  book: "Book", report: "Report", thesis: "Thesis", article: "Preprint", dataset: "Dataset", software: "Software",
  webpage: "Web page", manuscript: "Manuscript", speech: "Presentation", map: "Map", document: "Document",
  "article-magazine": "Magazine article", "article-newspaper": "Newspaper article", "entry-encyclopedia": "Encyclopedia entry",
  patent: "Patent", "post-weblog": "Blog post", standard: "Standard" };
const FIELDS = [
  ["title", "Title", "textarea"], ["container-title", "Publication"], ["collection-title", "Series"],
  ["volume", "Volume"], ["issue", "Issue"], ["page", "Pages"], ["issued", "Date", "date"],
  ["publisher", "Publisher"], ["publisher-place", "Place"], ["event-title", "Conference"], ["edition", "Edition"],
  ["number", "Number"], ["genre", "Genre"], ["DOI", "DOI"], ["URL", "URL"], ["ISBN", "ISBN"], ["ISSN", "ISSN"],
  ["language", "Language"], ["accessed", "Accessed", "date"], ["abstract", "Abstract", "textarea"], ["note", "Extra / note", "textarea"],
];

const S = {
  view: { id: "all", special: null, collection: null, tag: null, status: null, q: "" },
  sort: { key: "date_added", order: "desc" },
  items: [], total: 0, selected: new Set(), focusId: null, anchorId: null,
  state: null, styles: [], style: localStorage.getItem("rumil.style") || "", detail: null, dirty: false,
};

/* ------------------------------------------------------------------ api */
async function api(path, opts = {}) {
  const o = { method: "GET", headers: { "Content-Type": "application/json" }, ...opts };
  if (o.body !== undefined && typeof o.body !== "string") o.body = JSON.stringify(o.body);
  const r = await fetch(path, o);
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.error) || data || r.statusText);
  return data;
}
const post = (p, body = {}) => api(p, { method: "POST", body });
const patch = (p, body = {}) => api(p, { method: "PATCH", body });
const del = (p, body = {}) => api(p, { method: "DELETE", body });

/* ---------------------------------------------------------------- toast */
let toastTimer = null;
function toast(msg, ms = 3500) {
  const t = $("#toast");
  t.innerHTML = esc(msg);
  t.hidden = false;
  clearTimeout(toastTimer);
  if (ms) toastTimer = setTimeout(() => (t.hidden = true), ms);
}
function fail(e) { console.error(e); toast("Error: " + (e.message || e), 7000); }

async function watchJob(job, onDone) {
  const t = $("#toast");
  clearTimeout(toastTimer);
  t.hidden = false;
  const tick = async () => {
    let j;
    try { j = await api(`/api/jobs/${job.id}`); } catch (e) { return fail(e); }
    const pct = j.total ? Math.round((100 * j.done) / j.total) : 0;
    if (j.state === "running") {
      t.innerHTML = `<span>${esc(j.label)}: ${j.done}/${j.total || "?"}</span><div class="bar"><div style="width:${pct}%"></div></div><button id="job-cancel">Stop</button>`;
      $("#job-cancel").onclick = () => del(`/api/jobs/${j.id}`).catch(fail);
      setTimeout(tick, 1200);
    } else {
      const r = j.result || {};
      let msg = `${j.label}: ${j.state}`;
      if (r.by_status) msg += " — " + Object.entries(r.by_status).map(([k, v]) => `${v} ${(STATUS[k] || [0, k])[1].toLowerCase()}`).join(", ");
      if (r.summary) msg += " — " + r.summary;
      if (j.error) msg += " — " + j.error;
      toast(msg, 12000);
      onDone && onDone(j);
    }
  };
  tick();
}

/* ------------------------------------------------------------- sidebar */
async function loadState() {
  S.state = await api("/api/state");
  renderSidebar();
}

function renderSidebar() {
  const warn = $("#setup-warn");
  const ws = S.state.setup_warnings || [];
  warn.hidden = !ws.length;
  if (ws.length) {
    warn.innerHTML = ws.map((w) => `<p>${esc(w.split(". Fix:")[0].split(". Get a free token")[0])}.</p>`).join("") +
      `<button class="mini" id="warn-settings">Open settings</button>`;
    $("#warn-settings").onclick = settingsModal;
  }
  const st = S.state.stats;
  const bs = st.by_status || {};
  const sum = (keys) => keys.reduce((a, k) => a + (bs[k] || 0), 0);
  const counts = {
    all: st.items, trash: st.trash,
    attention: sum(["differs", "mismatch", "doi_found", "unverified", "url_dead"]),
    problem: sum(["retracted", "doi_invalid"]), unchecked: sum(["unchecked", "error"]),
  };
  $("#special-list").innerHTML = SPECIALS.map((s) => `
    <li data-id="${s.id}" class="${S.view.id === s.id ? "active" : ""}">
      ${s.dot ? `<span class="dot ${s.dot}"></span>` : ""}<span>${esc(s.label)}</span>
      <span class="count">${counts[s.id] ?? ""}</span></li>`).join("");
  $$("#special-list li").forEach((li) => (li.onclick = () => {
    const s = SPECIALS.find((x) => x.id === li.dataset.id);
    setView({ id: s.id, special: s.special || null, status: s.status || null, collection: null, tag: null }, s.label);
  }));
  const tree = $("#collection-tree");
  const open = JSON.parse(localStorage.getItem("rumil.open") || "{}");
  const node = (c) => `
    <li><div class="node ${S.view.collection == c.id ? "active" : ""}" data-cid="${c.id}" title="${esc(c.name)}">
      <span class="twisty" data-tw="${c.id}">${c.children.length ? (open[c.id] ? "▾" : "▸") : ""}</span>
      <span class="name">${esc(c.name)}</span><span class="count">${c.count || ""}</span></div>
      ${c.children.length && open[c.id] ? `<ul>${c.children.map(node).join("")}</ul>` : ""}</li>`;
  tree.innerHTML = S.state.collections.map(node).join("") || `<li class="muted" style="padding:4px 8px">No collections</li>`;
  $$(".twisty", tree).forEach((t) => (t.onclick = (ev) => {
    ev.stopPropagation();
    open[t.dataset.tw] = !open[t.dataset.tw];
    localStorage.setItem("rumil.open", JSON.stringify(open));
    renderSidebar();
  }));
  $$(".node", tree).forEach((n) => {
    n.onclick = () => setView({ id: "c" + n.dataset.cid, collection: n.dataset.cid, special: null, status: null, tag: null },
      n.querySelector(".name").textContent);
    n.oncontextmenu = (ev) => { ev.preventDefault(); collectionMenu(n.dataset.cid, n.querySelector(".name").textContent); };
    n.ondragover = (ev) => { ev.preventDefault(); n.classList.add("drop"); };
    n.ondragleave = () => n.classList.remove("drop");
    n.ondrop = async (ev) => {
      ev.preventDefault(); n.classList.remove("drop");
      const ids = JSON.parse(ev.dataTransfer.getData("text/rumil-ids") || "[]");
      if (!ids.length) return;
      try {
        const r = await post(`/api/collections/${n.dataset.cid}/items`, { ids });
        toast(`Added ${r.changed} to “${n.querySelector(".name").textContent}”`);
        loadState();
      } catch (e) { fail(e); }
    };
  });
  renderTags();
}

function renderTags() {
  const f = ($("#tag-filter").value || "").toLowerCase();
  const tags = (S.state.tags || []).filter((t) => !f || t.tag.toLowerCase().includes(f)).slice(0, 300);
  $("#tag-cloud").innerHTML = tags.map((t) => `<span class="tag ${S.view.tag === t.tag ? "active" : ""}" data-tag="${esc(t.tag)}">${esc(t.tag)}</span>`).join("");
  $$("#tag-cloud .tag").forEach((el) => (el.onclick = () => {
    const tag = el.dataset.tag;
    S.view.tag = S.view.tag === tag ? null : tag;
    renderTags(); loadItems(true);
  }));
}

function collectionMenu(cid, name) {
  modal(`<h3>Collection “${esc(name)}”</h3>
    <div class="row"><input id="m-name" value="${esc(name)}"><button id="m-rename">Rename</button></div>
    <div class="row"><button id="m-sub">New sub-collection</button><button id="m-exp">Export as BibTeX</button>
    <button id="m-ver">Verify all in collection</button></div>
    <div class="row"><button class="danger" id="m-del">Delete collection (references are kept)</button></div>
    <div class="foot"><button data-close>Close</button></div>`, (m) => {
    $("#m-rename", m).onclick = async () => { await patch(`/api/collections/${cid}`, { name: $("#m-name", m).value }).catch(fail); closeModal(); loadState(); };
    $("#m-sub", m).onclick = async () => {
      const n = prompt("Sub-collection name"); if (!n) return;
      await post("/api/collections", { name: n, parent: Number(cid) }).catch(fail); closeModal(); loadState();
    };
    $("#m-exp", m).onclick = () => (location.href = `/api/export?format=bibtex&collection=${cid}`);
    $("#m-ver", m).onclick = async () => { closeModal(); const j = await post("/api/verify", { collection: Number(cid) }).catch(fail); j && j.background_job && watchJob({ id: j.background_job }, () => { loadItems(true); loadState(); }); };
    $("#m-del", m).onclick = async () => {
      if (!confirm(`Delete the collection “${name}”? The references stay in your library.`)) return;
      await del(`/api/collections/${cid}`).catch(fail); closeModal(); setView({ id: "all" }, "All references"); loadState();
    };
  });
}

/* ---------------------------------------------------------------- items */
function setView(v, title) {
  S.view = { id: "all", special: null, collection: null, tag: null, status: null, q: S.view.q, ...v };
  $("#view-title").textContent = title || "All references";
  S.selected.clear(); S.focusId = null;
  renderSidebar();
  loadItems(true);
}

let loadSeq = 0;
async function loadItems(reset) {
  const seq = ++loadSeq;
  if (S.view.special === "duplicates") return loadDuplicates();
  $("#dup-view").hidden = true; $("#items-table").hidden = false;
  const offset = reset ? 0 : S.items.length;
  const p = new URLSearchParams({ limit: 300, offset });
  if (S.view.q) p.set("q", S.view.q);
  if (S.view.collection) p.set("collection", S.view.collection);
  if (S.view.tag) p.set("tag", S.view.tag);
  if (S.view.status) p.set("status", S.view.status);
  if (S.view.special) p.set("special", S.view.special);
  if (!(S.view.q && S.sort.key === "date_added" && !S.userSorted)) { p.set("sort", S.sort.key); p.set("order", S.sort.order); }
  try {
    const r = await api("/api/items?" + p);
    if (seq !== loadSeq) return;
    S.items = reset ? r.items : S.items.concat(r.items);
    S.total = r.total;
    renderTable();
  } catch (e) { fail(e); }
}

function statusDot(st) { const [cls, label] = STATUS[st] || ["idle", st]; return `<span class="dot ${cls}" title="${esc(label)}"></span>`; }

function renderTable() {
  $("#view-count").textContent = `${S.total.toLocaleString()} reference${S.total === 1 ? "" : "s"}` + (S.view.tag ? ` · tag: ${S.view.tag}` : "");
  $("#items-body").innerHTML = S.items.map((it) => `
    <tr data-id="${it.id}" draggable="true" class="${S.selected.has(it.id) ? "selected" : ""} ${S.focusId === it.id ? "focus" : ""}">
      <td class="c-status">${statusDot(it.status)}</td>
      <td class="c-title" title="${esc(it.title)}">${esc(it.title)}</td>
      <td class="c-creator">${esc(it.creator)}</td>
      <td class="c-year">${it.year || ""}</td>
      <td class="c-container" title="${esc(it.container)}">${esc(it.container)}</td>
      <td class="c-key">${esc(it.citekey)}</td></tr>`).join("");
  $("#btn-more").hidden = S.items.length >= S.total;
  $$("#items-table th").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.sort === S.sort.key);
    th.classList.toggle("asc", th.dataset.sort === S.sort.key && S.sort.order === "asc");
  });
  if (S.view.special === "trash" && S.items.length) {
    $("#view-count").innerHTML += ` <button class="mini danger" id="btn-empty-trash">Empty trash</button>`;
    $("#btn-empty-trash").onclick = async () => {
      if (!confirm("Permanently delete everything in the trash?")) return;
      const r = await post("/api/trash/empty").catch(fail); r && toast(`Deleted ${r.purged}`); loadItems(true); loadState();
    };
  }
}

$("#items-body").addEventListener("click", (ev) => {
  const tr = ev.target.closest("tr"); if (!tr) return;
  const id = Number(tr.dataset.id);
  if (ev.metaKey || ev.ctrlKey) { S.selected.has(id) ? S.selected.delete(id) : S.selected.add(id); }
  else if (ev.shiftKey && S.anchorId != null) {
    const ids = S.items.map((i) => i.id); const a = ids.indexOf(S.anchorId), b = ids.indexOf(id);
    S.selected = new Set(ids.slice(Math.min(a, b), Math.max(a, b) + 1));
  } else { S.selected = new Set([id]); S.anchorId = id; }
  S.focusId = id;
  renderTable();
  if (S.selected.size === 1) showDetail(id); else showMulti();
});
$("#items-body").addEventListener("dblclick", (ev) => {
  const tr = ev.target.closest("tr"); if (!tr) return;
  const it = S.items.find((i) => i.id === Number(tr.dataset.id));
  if (it && it.doi) window.open(doiUrl(it.doi), "_blank");
});
$("#items-body").addEventListener("dragstart", (ev) => {
  const tr = ev.target.closest("tr"); if (!tr) return;
  const id = Number(tr.dataset.id);
  if (!S.selected.has(id)) { S.selected = new Set([id]); renderTable(); }
  ev.dataTransfer.setData("text/rumil-ids", JSON.stringify([...S.selected]));
  ev.dataTransfer.effectAllowed = "copy";
});
$$("#items-table th").forEach((th) => (th.onclick = () => {
  const k = th.dataset.sort;
  S.sort = { key: k, order: S.sort.key === k && S.sort.order === "asc" ? "desc" : "asc" };
  S.userSorted = true;
  loadItems(true);
}));
$("#btn-more").onclick = () => loadItems(false);

/* --------------------------------------------------------------- detail */
async function showDetail(id) {
  if (S.dirty && !confirm("Discard unsaved edits?")) return;
  S.dirty = false;
  try {
    const it = await api(`/api/items/${id}`);
    S.detail = it;
    renderDetail(it);
  } catch (e) { fail(e); }
}

function showMulti() {
  const n = S.selected.size;
  $("#detail").innerHTML = `<div class="empty-detail"><p><b>${n} references selected</b></p>
    <div class="copy-row" style="justify-content:center">
      <button id="mm-verify">Verify</button><button id="mm-bib">Copy formatted references</button>
      <button id="mm-bibtex">Export BibTeX</button><button id="mm-cite">Copy [@keys]</button>
      <button id="mm-tag">Add tag</button><button class="danger" id="mm-trash">Move to trash</button></div></div>`;
  const ids = [...S.selected];
  $("#mm-verify").onclick = async () => { const r = await post("/api/verify", { ids }).catch(fail); r && r.background_job && watchJob({ id: r.background_job }, () => { loadItems(true); loadState(); }); };
  $("#mm-bib").onclick = async () => {
    const r = await post("/api/format", { ids, style: S.style }).catch(fail); if (!r) return;
    copy(r.bibliography.map((e) => e.text).join("\n"), `${r.bibliography.length} references copied (${r.style.title})`);
  };
  $("#mm-bibtex").onclick = () => (location.href = `/api/export?format=bibtex&ids=${ids.join(",")}`);
  $("#mm-cite").onclick = () => copy("[" + S.items.filter((i) => S.selected.has(i.id)).map((i) => "@" + i.citekey).join("; ") + "]");
  $("#mm-tag").onclick = async () => { const t = prompt("Tag to add"); if (!t) return; await post("/api/tags", { ids, tags: [t] }).catch(fail); loadState(); toast("Tagged"); };
  $("#mm-trash").onclick = async () => {
    if (!confirm(`Move ${n} references to the trash?`)) return;
    for (const id of ids) await del(`/api/items/${id}`).catch(fail);
    S.selected.clear(); loadItems(true); loadState();
  };
}

function nameStr(n) { return n.literal || [n.family, n.given].filter(Boolean).join(", "); }
function dateStr(d) {
  if (!d) return "";
  if (d.literal) return d.literal;
  const p = (d["date-parts"] || [[]])[0];
  return p.filter((x) => x).map((x, i) => (i ? String(x).padStart(2, "0") : x)).join("-");
}

function renderDetail(it) {
  const c = it.csl;
  const [cls, label] = STATUS[it.verify_status] || ["idle", it.verify_status];
  const vd = it.verify_details || {};
  const sugg = it.suggestions || [];
  const pv = it.preview || {};
  const authors = (c.author || []).map(nameStr);
  const el = $("#detail");
  el.classList.add("open");
  el.innerHTML = `
    <button class="icon d-close" title="Close (Esc)" aria-label="Close details">&times;</button>
    <div class="d-type">${esc(TYPE_LABEL[c.type] || c.type)}${it.deleted ? " · <b>in trash</b>" : ""}</div>
    <div class="d-title">${esc(c.title || "(untitled)")}</div>
    <div class="d-authors">${esc(authors.slice(0, 8).join("; "))}${authors.length > 8 ? ` … +${authors.length - 8}` : ""}</div>

    <div class="card status-card ${cls}">
      <h4>Verification <span>${it.verify_checked ? esc(new Date(it.verify_checked).toLocaleDateString()) : ""}</span></h4>
      <div class="status-line">${statusDot(it.verify_status)} ${esc(label)}</div>
      ${vd.summary ? `<div>${esc(vd.summary)}</div>` : `<div class="muted">${esc(it.status_meaning)}</div>`}
      ${(vd.issues || []).length ? `<ul class="issues">${vd.issues.map((x) => `<li>${linkify(x)}</li>`).join("")}</ul>` : ""}
      ${sugg.length ? `<div style="margin-top:8px">${sugg.map(suggRow).join("")}</div>` : ""}
      <div class="copy-row">
        <button id="d-verify">Verify now</button>
        ${sugg.some((s) => s.safe) ? `<button id="d-safe" class="primary">Apply safe fixes</button>` : ""}
        ${sugg.some((s) => s.correction) ? `<button id="d-corr" class="primary">Use registry values</button>` : ""}
      </div>
    </div>

    <div class="card cite-preview">
      <h4>Citation <span>${esc(pv.style || "")}</span></h4>
      ${pv.error ? `<div class="muted">${esc(pv.error)}</div>` : `
      <div>${esc(pv.parenthetical || "")} &nbsp;·&nbsp; ${esc(pv.narrative || "")}</div>
      <div class="bib">${sanitize(pv.html || "")}</div>`}
      <div class="copy-row">
        <button data-copy="${esc(it.citekey)}">Copy key</button>
        <button data-copy="\\citep{${esc(it.citekey)}}">\\citep</button>
        <button data-copy="[@${esc(it.citekey)}]">[@key]</button>
        <button id="d-copy-ref">Copy reference</button>
        <button id="d-copy-bibtex">Copy BibTeX</button>
        ${c.DOI ? `<a class="link" href="${esc(doiUrl(c.DOI))}" target="_blank" rel="noopener">Open DOI ↗</a>` : ""}
      </div>
    </div>

    <div class="card">
      <h4>Details <span><button id="d-save" class="primary" hidden>Save</button> <button id="d-revert" hidden>Revert</button></span></h4>
      <div class="fields">
        <label>Citekey</label><input id="f-citekey" class="mono" value="${esc(it.citekey)}">
        ${it.aliases && it.aliases.length ? `<label>Also</label><div class="muted mono">${esc(it.aliases.join(", "))}</div>` : ""}
        <label>Type</label><select id="f-type">${TYPES.map((t) => `<option value="${t}" ${t === c.type ? "selected" : ""}>${esc(TYPE_LABEL[t] || t)}</option>`).join("")}</select>
        <label>Authors</label><div class="authors-edit" id="f-authors"></div>
        <label>Editors</label><div class="authors-edit" id="f-editors"></div>
        ${FIELDS.map(([k, lab, kind]) => {
          const v = kind === "date" ? dateStr(c[k]) : (c[k] ?? "");
          return `<label>${esc(lab)}</label>` + (kind === "textarea"
            ? `<textarea data-f="${k}" rows="${k === "abstract" ? 4 : 2}">${esc(v)}</textarea>`
            : `<input data-f="${k}" data-kind="${kind || ""}" value="${esc(v)}">`);
        }).join("")}
      </div>
    </div>

    <div class="card">
      <h4>Collections &amp; tags</h4>
      <div class="chips" id="d-colls">${(it.collections || []).map((x) => `<span class="chip">${esc(x.path)} <button data-rmcoll="${x.id}" title="Remove from collection">×</button></span>`).join("") || `<span class="muted">Unfiled</span>`}</div>
      <div class="row" style="display:flex;gap:6px;margin:6px 0"><select id="d-addcoll"><option value="">Add to collection…</option>${collOptions()}</select></div>
      <div class="chips" id="d-tags">${(it.tags || []).map((t) => `<span class="chip">${esc(t)} <button data-rmtag="${esc(t)}">×</button></span>`).join("")}</div>
      <div style="display:flex;gap:6px;margin-top:6px"><input id="d-newtag" placeholder="add tag"><button id="d-addtag">Add</button></div>
    </div>

    <div class="card">
      <h4>Attachments</h4>
      ${(it.attachments || []).map((a) => `<div>${a.path ? `<a href="/api/attachments/${a.id}/file" target="_blank">${esc(a.title || "file")}</a>` : a.url ? `<a href="${esc(safeUrl(a.url))}" target="_blank" rel="noopener">${esc(a.title || a.url)}</a>` : esc(a.title)} <span class="muted">${esc(a.content_type || "")}</span></div>`).join("") || `<span class="muted">None</span>`}
    </div>

    <div class="card">
      <h4>Notes <button class="mini" id="d-addnote">+ Note</button></h4>
      <div id="d-notes">${(it.notes || []).map((n) => `<div class="note" data-nid="${n.id}">${sanitize(n.html)}</div>
        <div class="note-actions"><button class="link" data-editnote="${n.id}">edit</button> <button class="link danger" data-delnote="${n.id}">delete</button></div>`).join("") || `<span class="muted">No notes</span>`}</div>
    </div>

    <details class="card"><summary>History</summary><div class="hist">${(it.history || []).map((h) =>
      `<div>${esc(new Date(h.ts).toLocaleString())} · ${esc(h.actor || "")} · ${esc(h.action)}${h.detail && h.detail.reason ? " — " + esc(h.detail.reason) : ""}${h.detail && h.detail.changed ? " (" + esc(h.detail.changed.join(", ")) + ")" : ""}</div>`).join("")}</div></details>

    <div class="copy-row">${it.deleted ? `<button id="d-restore">Restore from trash</button>` : `<button class="danger" id="d-trash">Move to trash</button>`}</div>`;

  authorsEditor($("#f-authors"), c.author || []);
  authorsEditor($("#f-editors"), c.editor || []);
  const markDirty = () => { S.dirty = true; $("#d-save").hidden = false; $("#d-revert").hidden = false; };
  $$("#detail [data-f], #f-type, #f-citekey").forEach((x) => (x.oninput = markDirty));
  $("#detail").addEventListener("authors-changed", markDirty);
  $("#d-save").onclick = () => saveDetail(it);
  $("#d-revert").onclick = () => { S.dirty = false; renderDetail(it); };
  $("#d-verify").onclick = async () => {
    $("#d-verify").disabled = true; $("#d-verify").textContent = "Checking…";
    try { const r = await post(`/api/items/${it.id}/verify`); S.detail = r; renderDetail(r); refreshRow(r); loadState(); } catch (e) { fail(e); renderDetail(it); }
  };
  if ($("#d-safe")) $("#d-safe").onclick = () => applySugg(it, "safe");
  if ($("#d-corr")) $("#d-corr").onclick = () => applySugg(it, "corrections");
  $$("[data-keep]").forEach((b) => (b.onclick = async () => {
    try { const r = await post(`/api/items/${it.id}/keep`, { fields: [b.dataset.keep] }); S.detail = r; renderDetail(r); refreshRow(r); loadState(); toast("Kept the library value"); }
    catch (e) { fail(e); }
  }));
  $$("[data-apply]").forEach((b) => (b.onclick = () => applySugg(it, [b.dataset.apply])));
  $$("[data-copy]").forEach((b) => (b.onclick = () => copy(b.dataset.copy)));
  $("#d-copy-ref").onclick = () => copy(htmlText(pv.html || ""), "Reference copied");
  $("#d-copy-bibtex").onclick = async () => {
    const t = await api(`/api/export?format=bibtex&ids=${it.id}`).catch(fail);
    t && copy(String(t).replace(/^%.*\n/, "").trim(), "BibTeX copied");
  };
  $("#d-addcoll").onchange = async (ev) => {
    if (!ev.target.value) return;
    await post(`/api/collections/${ev.target.value}/items`, { ids: [it.id] }).catch(fail);
    showDetail(it.id); loadState();
  };
  $$("[data-rmcoll]").forEach((b) => (b.onclick = async () => {
    await del(`/api/collections/${b.dataset.rmcoll}/items`, { ids: [it.id] }).catch(fail); showDetail(it.id); loadState();
  }));
  const setTags = async (tags) => { const r = await patch(`/api/items/${it.id}`, { tags }).catch(fail); r && renderDetail(r); loadState(); };
  $("#d-addtag").onclick = () => { const t = $("#d-newtag").value.trim(); t && setTags([...(it.tags || []), t]); };
  $("#d-newtag").onkeydown = (e) => { if (e.key === "Enter") $("#d-addtag").click(); };
  $$("[data-rmtag]").forEach((b) => (b.onclick = () => setTags((it.tags || []).filter((t) => t !== b.dataset.rmtag))));
  $("#d-addnote").onclick = () => noteEditor(it, null, "");
  $$("[data-editnote]").forEach((b) => (b.onclick = () => {
    const n = it.notes.find((x) => x.id == b.dataset.editnote); noteEditor(it, n.id, htmlText(n.html));
  }));
  $$("[data-delnote]").forEach((b) => (b.onclick = async () => {
    if (!confirm("Delete this note?")) return; await del(`/api/notes/${b.dataset.delnote}`).catch(fail); showDetail(it.id);
  }));
  if ($("#d-trash")) $("#d-trash").onclick = async () => { await del(`/api/items/${it.id}`).catch(fail); toast("Moved to trash"); loadItems(true); loadState(); $("#detail").innerHTML = ""; };
  if ($("#d-restore")) $("#d-restore").onclick = async () => { await post(`/api/items/${it.id}/restore`).catch(fail); toast("Restored"); loadItems(true); loadState(); };
}

function suggRow(s) {
  const show = (v) => {
    if (v == null) return "<i>remove</i>";
    if (Array.isArray(v)) return esc(v.map((n) => (typeof n === "object" ? nameStr(n) : n)).slice(0, 6).join("; ") + (v.length > 6 ? ` … (${v.length})` : ""));
    if (typeof v === "object") return esc(v["date-parts"] ? dateStr(v) : Object.keys(v).join(", "));
    return esc(String(v).slice(0, 300));
  };
  const what = {
    set_field: `Set <b>${esc(s.field)}</b> to <span class="val">${show(s.proposed)}</span>`,
    clean_field: `Clean <b>${esc(s.field)}</b> → <span class="val">${show(s.proposed)}</span>`,
    add_doi: `Add DOI <span class="val">${show(s.proposed)}</span>`,
    replace_doi: `Replace DOI with <span class="val">${show(s.proposed)}</span>`,
    published_version: `Cite the published version <span class="val">${show(s.proposed)}</span>`,
    replace_metadata: `Replace core fields with the registry record`,
    merge_duplicate: `Merge duplicate <span class="val">@${show(s.proposed)}</span>`,
  }[s.kind] || esc(s.kind);
  return `<div class="sugg"><div class="what">${what}<div class="muted">${esc(s.reason || "")}</div></div>
    ${s.safe ? `<span class="safe">safe</span>` : s.correction ? `<span class="safe">registry</span>` : ""}
    <button class="mini" data-apply="${esc(s.id)}">Apply</button>
    ${(s.correction && s.keepable !== false) || s.keepable ? `<button class="mini" data-keep="${esc(s.field)}" title="The library value is intended; stop flagging it">Keep mine</button>` : ""}</div>`;
}

async function applySugg(it, ids) {
  try { const r = await post(`/api/items/${it.id}/apply`, { ids }); S.detail = r; renderDetail(r); refreshRow(r); loadState(); toast("Applied"); }
  catch (e) { fail(e); }
}

function refreshRow(it) {
  const row = S.items.find((x) => x.id === it.id);
  if (row) { row.status = it.verify_status; row.title = it.csl.title; row.citekey = it.citekey; row.doi = it.csl.DOI; renderTable(); }
}

function authorsEditor(root, names) {
  // Each row keeps its original name object so parts the editor doesn't show (suffix, particles) survive.
  const rows = names.map((n) => ({ family: n.family || n.literal || "", given: n.given || "", literal: !!n.literal, orig: n }));
  root._touched = false;
  root.addEventListener("authors-changed", () => (root._touched = true));
  const render = () => {
    root.innerHTML = rows.map((r, i) => `<div class="arow"><input data-i="${i}" data-k="family" placeholder="Family / organization" value="${esc(r.family)}">
      <input data-i="${i}" data-k="given" placeholder="Given" value="${esc(r.given)}"><button data-up="${i}" title="Move up">↑</button><button data-rm="${i}" title="Remove">×</button></div>`).join("")
      + `<button class="mini" data-add>+ add</button>`;
    $$("input", root).forEach((inp) => (inp.oninput = () => { rows[inp.dataset.i][inp.dataset.k] = inp.value; root.dispatchEvent(new CustomEvent("authors-changed", { bubbles: true })); }));
    $$("[data-rm]", root).forEach((b) => (b.onclick = () => { rows.splice(b.dataset.rm, 1); render(); root.dispatchEvent(new CustomEvent("authors-changed", { bubbles: true })); }));
    $$("[data-up]", root).forEach((b) => (b.onclick = () => { const i = Number(b.dataset.up); if (i > 0) { [rows[i - 1], rows[i]] = [rows[i], rows[i - 1]]; render(); root.dispatchEvent(new CustomEvent("authors-changed", { bubbles: true })); } }));
    $("[data-add]", root).onclick = () => { rows.push({ family: "", given: "" }); render(); };
  };
  root._rows = rows;
  render();
}

function namesFrom(root) {
  return (root._rows || []).filter((r) => r.family.trim() || r.given.trim()).map((r) => {
    const fam = r.family.trim(), giv = r.given.trim();
    if (r.literal && !giv) return { literal: fam };
    const n = { ...(r.orig && !r.orig.literal ? r.orig : {}) };
    n.family = fam;
    if (giv) n.given = giv; else delete n.given;
    return n;
  });
}

async function saveDetail(it) {
  const c = it.csl;
  const p = {};
  $$("#detail [data-f]").forEach((el) => {
    const k = el.dataset.f;
    const old = el.dataset.kind === "date" ? dateStr(c[k]) : (c[k] ?? "");
    const v = el.value.trim();
    if (String(old) !== v) p[k] = v || null;
  });
  const t = $("#f-type").value; if (t !== c.type) p.type = t;
  // Names are only sent when the name editor was actually used.
  if ($("#f-authors")._touched) { const au = namesFrom($("#f-authors")); p.author = au.length ? au : null; }
  if ($("#f-editors")._touched) { const ed = namesFrom($("#f-editors")); p.editor = ed.length ? ed : null; }
  const body = {};
  if (Object.keys(p).length) body.patch = p;
  const key = $("#f-citekey").value.trim(); if (key && key !== it.citekey) body.citekey = key;
  if (!Object.keys(body).length) { S.dirty = false; return renderDetail(it); }
  try { const r = await patch(`/api/items/${it.id}`, body); S.dirty = false; S.detail = r; renderDetail(r); refreshRow(r); toast("Saved"); }
  catch (e) { fail(e); }
}

function noteEditor(it, nid, text) {
  modal(`<h3>${nid ? "Edit" : "New"} note</h3><textarea id="m-note" rows="12">${esc(text)}</textarea>
    <div class="foot"><button data-close>Cancel</button><button class="primary" id="m-save">Save</button></div>`, (m) => {
    $("#m-note", m).focus();
    $("#m-save", m).onclick = async () => {
      const html = $("#m-note", m).value.split(/\n{2,}/).map((p) => `<p>${esc(p).replace(/\n/g, "<br>")}</p>`).join("");
      try { nid ? await patch(`/api/notes/${nid}`, { html }) : await post(`/api/items/${it.id}/notes`, { html }); closeModal(); showDetail(it.id); }
      catch (e) { fail(e); }
    };
  });
}

/* ----------------------------------------------------------- duplicates */
async function loadDuplicates() {
  $("#items-table").hidden = true; $("#btn-more").hidden = true;
  const box = $("#dup-view"); box.hidden = false; box.innerHTML = `<p class="muted">Looking for duplicates…</p>`;
  const r = await api("/api/duplicates").catch(fail); if (!r) return;
  $("#view-count").textContent = `${r.groups.length} groups`;
  const rank = (i) => (i.doi ? 2 : 0) + (["verified", "verified_no_doi"].includes(i.status) ? 1 : 0);
  box.innerHTML = r.groups.map((g, gi) => {
    const best = g.slice().sort((a, b) => rank(b) - rank(a) || a.id - b.id)[0];
    return `<div class="dup-group" data-g="${gi}">${g.map((i) => `<label><input type="radio" name="keep${gi}" value="${i.id}" ${i.id === best.id ? "checked" : ""}>
      ${statusDot(i.status)} <span>${esc(i.title)}</span> <span class="muted">${esc(i.creator)} ${i.year || ""}</span> <span class="k" title="${esc(i.doi || "")}">@${esc(i.citekey)}${i.doi ? "<br>" + esc(i.doi) : ""}</span></label>`).join("")}
      <div class="copy-row"><button class="mini primary" data-merge="${gi}">Merge into selected</button>
      <span class="muted">Other citekeys keep working as aliases.</span></div></div>`;
  }).join("") || `<p class="muted">No duplicates found.</p>`;
  $$("[data-merge]", box).forEach((b) => (b.onclick = async () => {
    const gi = Number(b.dataset.merge); const g = r.groups[gi];
    const keep = Number($(`input[name=keep${gi}]:checked`, box).value);
    try { await post("/api/merge", { keep, remove: g.filter((i) => i.id !== keep).map((i) => i.id) }); toast("Merged"); loadDuplicates(); loadState(); }
    catch (e) { fail(e); }
  }));
}

/* ---------------------------------------------------------------- modals */
function modal(html, init) {
  const root = $("#modal-root");
  root.innerHTML = `<div class="modal-bg"><div class="modal">${html}</div></div>`;
  const m = $(".modal", root);
  $(".modal-bg", root).onclick = (e) => { if (e.target.classList.contains("modal-bg")) closeModal(); };
  $$("[data-close]", m).forEach((b) => (b.onclick = closeModal));
  init && init(m);
}
function closeModal() { $("#modal-root").innerHTML = ""; }
function collOptions(sel) {
  const out = [];
  const walk = (cs, depth) => cs.forEach((c) => { out.push(`<option value="${c.id}" ${sel == c.id ? "selected" : ""}>${" ".repeat(depth)}${esc(c.name)}</option>`); walk(c.children, depth + 1); });
  walk(S.state ? S.state.collections : [], 0);
  return out.join("");
}

function addModal() {
  modal(`<h3>Add references</h3>
    <div class="tabs"><button class="active" data-tab="id">By identifier</button><button data-tab="manual">Without identifier</button></div>
    <div data-pane="id">
      <p class="muted">One per line: DOI, arXiv id, ISBN, PMID, ADS bibcode, or a URL. Metadata comes from the registry, never typed by hand.</p>
      <textarea id="m-ids" rows="5" placeholder="10.1130/G50341.1&#10;arXiv:2208.03533&#10;https://www.mdpi.com/2072-4292/15/2/314"></textarea>
    </div>
    <div data-pane="manual" hidden>
      <p class="muted">For works with no DOI (an LPSC abstract, a report). Give a link that shows the work; Rumil checks the title and first author appear there.</p>
      <div class="fields">
        <label>Type</label><select id="mm-type">${TYPES.map((t) => `<option value="${t}" ${t === "paper-conference" ? "selected" : ""}>${esc(TYPE_LABEL[t])}</option>`).join("")}</select>
        <label>Title</label><input id="mm-title">
        <label>Authors</label><textarea id="mm-authors" rows="3" placeholder="Smith, Jane A.&#10;García, Luis"></textarea>
        <label>Year</label><input id="mm-year">
        <label>Publication</label><input id="mm-cont" placeholder="53rd Lunar and Planetary Science Conference">
        <label>Pages / no.</label><input id="mm-page" placeholder="1234">
        <label>Evidence URL</label><input id="mm-url" placeholder="https://www.hou.usra.edu/meetings/lpsc2022/pdf/1234.pdf">
      </div>
    </div>
    <div class="row"><label>Collection</label><select id="m-coll"><option value="">(none)</option>${collOptions(S.view.collection)}</select></div>
    <div class="result" id="m-result"></div>
    <div class="foot"><button data-close>Close</button><button class="primary" id="m-go">Add</button></div>`, (m) => {
    let tab = "id";
    $$(".tabs button", m).forEach((b) => (b.onclick = () => {
      tab = b.dataset.tab; $$(".tabs button", m).forEach((x) => x.classList.toggle("active", x === b));
      $$("[data-pane]", m).forEach((p) => (p.hidden = p.dataset.pane !== tab));
    }));
    $("#m-ids", m).focus();
    $("#m-go", m).onclick = async () => {
      const coll = $("#m-coll", m).value;
      const collName = coll ? $("#m-coll", m).selectedOptions[0].textContent.trim() : null;
      const out = $("#m-result", m);
      $("#m-go", m).disabled = true;
      try {
        if (tab === "id") {
          const ids = $("#m-ids", m).value.split(/\n+/).map((s) => s.trim()).filter(Boolean);
          out.innerHTML = "";
          for (const ident of ids) {
            const line = document.createElement("div"); line.textContent = `… ${ident}`; out.appendChild(line);
            try {
              const r = await post("/api/items", { identifier: ident, collection: coll ? Number(coll) : null });
              line.innerHTML = `${statusDot(r.item.status)} <b>${esc(r.status)}</b> @${esc(r.item.citekey)} — ${esc(r.item.title)}`
                + (r.warnings && r.warnings.length ? `<div class="muted">${esc(r.warnings.join(" "))}</div>` : "")
                + (r.message ? `<div class="muted">${esc(r.message)}</div>` : "");
            } catch (e) { line.innerHTML = `<span class="dot bad"></span> ${esc(ident)}: ${esc(e.message)}`; }
          }
        } else {
          const csl = { type: $("#mm-type", m).value, title: $("#mm-title", m).value.trim(),
            author: $("#mm-authors", m).value.split(/\n+/).map((s) => s.trim()).filter(Boolean).map((s) => {
              const [f, g] = s.split(","); return g ? { family: f.trim(), given: g.trim() } : { family: f.trim() };
            }), issued: $("#mm-year", m).value.trim() || undefined, "container-title": $("#mm-cont", m).value.trim() || undefined,
            page: $("#mm-page", m).value.trim() || undefined };
          const r = await post("/api/items/manual", { csl, evidence_url: $("#mm-url", m).value.trim() || null, collection: collName });
          out.innerHTML = `${statusDot(r.item.status)} <b>${esc(r.status)}</b> @${esc(r.item.citekey)} — ${esc(r.item.verification || r.message || "")}`;
        }
        loadItems(true); loadState();
      } catch (e) { out.innerHTML = `<span class="dot bad"></span> ${esc(e.message)}`; }
      $("#m-go", m).disabled = false;
    };
  });
}

function importModal() {
  modal(`<h3>Import</h3>
    <p><b>Zotero.</b> Pull new and changed items from ${esc(S.state.settings.zotero_dir)}. Safe to repeat; your Rumil edits are kept.</p>
    <button id="m-zot" class="primary">Sync from Zotero</button>
    <hr style="border:none;border-top:1px solid var(--border);margin:14px 0">
    <p><b>File.</b> A .bib, .ris, or CSL-JSON file. Its citekeys are kept; works already in the library become aliases instead of duplicates.</p>
    <div class="row"><input type="file" id="m-file" accept=".bib,.bibtex,.ris,.json,.txt"></div>
    <div class="row"><label>Into collection</label><select id="m-coll"><option value="">(none)</option>${collOptions(S.view.collection)}</select></div>
    <div class="result" id="m-result"></div>
    <div class="foot"><button data-close>Close</button><button id="m-go">Import file</button></div>`, (m) => {
    $("#m-zot", m).onclick = async () => {
      const j = await post("/api/import/zotero").catch(fail); if (!j) return;
      closeModal(); watchJob(j, () => { loadItems(true); loadState(); });
    };
    $("#m-go", m).onclick = async () => {
      const f = $("#m-file", m).files[0]; if (!f) return toast("Choose a file first");
      const text = await f.text();
      const sel = $("#m-coll", m);
      try {
        const r = await post("/api/import/file", { filename: f.name, text, collection: sel.value ? sel.selectedOptions[0].textContent.trim() : null });
        $("#m-result", m).innerHTML = `<b>${esc(r.summary)}</b>` + (r.key_conflicts.length ? `<ul class="issues">${r.key_conflicts.map((k) => `<li>${esc(k.message)}</li>`).join("")}</ul>` : "");
        loadItems(true); loadState();
      } catch (e) { fail(e); }
    };
  });
}

function exportModal() {
  const n = S.selected.size;
  modal(`<h3>Export</h3>
    <div class="row"><label>Format</label><select id="m-fmt"><option value="bibtex">BibTeX (.bib)</option><option value="biblatex">BibLaTeX (.bib)</option>
      <option value="csljson">CSL-JSON (pandoc, Zotero)</option><option value="ris">RIS (EndNote, Word add-ins)</option></select></div>
    <div class="row"><label><input type="radio" name="scope" value="sel" ${n ? "checked" : "disabled"}> Selected (${n})</label>
      <label><input type="radio" name="scope" value="coll" ${!n && S.view.collection ? "checked" : ""} ${S.view.collection ? "" : "disabled"}> This collection</label>
      <label><input type="radio" name="scope" value="all" ${!n && !S.view.collection ? "checked" : ""}> Whole library</label></div>
    <p class="muted">Your whole library is also kept current at ${esc(S.state.settings.home)}/library.bib for LaTeX.</p>
    <div class="foot"><button data-close>Close</button><button id="m-copybib">Copy formatted references</button><button class="primary" id="m-go">Download</button></div>`, (m) => {
    const q = () => {
      const scope = $("input[name=scope]:checked", m).value;
      const p = new URLSearchParams({ format: $("#m-fmt", m).value });
      if (scope === "sel") p.set("ids", [...S.selected].join(","));
      if (scope === "coll") p.set("collection", S.view.collection);
      return p;
    };
    $("#m-go", m).onclick = () => { location.href = "/api/export?" + q(); };
    $("#m-copybib", m).onclick = async () => {
      const scope = $("input[name=scope]:checked", m).value;
      let ids = [...S.selected];
      if (scope !== "sel") { const r = await api("/api/items?limit=1000&" + (scope === "coll" ? `collection=${S.view.collection}` : "")); ids = r.items.map((i) => i.id); }
      const r = await post("/api/format", { ids, style: S.style }).catch(fail); if (!r) return;
      copy(r.bibliography.map((e) => e.text).join("\n"), `${r.bibliography.length} references copied (${r.style.title})`);
    };
  });
}

async function settingsModal() {
  const st = S.state.settings;
  modal(`<h3>Settings</h3>
    <div class="fields">
      <label>Default style</label><select id="s-style">${S.styles.map((s) => `<option value="${esc(s.id)}" ${s.id === S.state.default_style ? "selected" : ""}>${esc(s.title)}</option>`).join("")}</select>
      <label>Install style</label><div style="display:flex;gap:6px"><input id="s-install" placeholder="CSL id, e.g. journal-of-geophysical-research-planets"><button id="s-inst">Install</button></div>
      <label>Contact email</label><input id="s-email" value="${esc(st.contact_email || "")}" placeholder="for Crossref's faster 'polite' pool (optional)">
      <label>ADS token</label><div><input id="s-ads" type="password" placeholder="${st.ads_token_set ? "(set — leave blank to keep)" : "NASA ADS API token"}">
        <div class="muted" style="margin-top:3px">Free: sign in at <a href="https://ui.adsabs.harvard.edu/user/settings/token" target="_blank" rel="noopener">ui.adsabs.harvard.edu</a> → Settings → API Token. Lets Rumil check LPSC and AGU abstracts.</div></div>
      <label>Zotero folder</label><input id="s-zot" value="${esc(st.zotero_dir || "")}">
    </div>
    <p class="muted">Library folder: ${esc(st.home)}</p>
    <div class="foot"><button data-close>Cancel</button><button class="primary" id="s-save">Save</button></div>`, (m) => {
    $("#s-inst", m).onclick = async () => {
      const name = $("#s-install", m).value.trim(); if (!name) return;
      try { const r = await post("/api/styles", { name }); S.styles = r.styles; toast(`Installed ${r.installed}`); closeModal(); renderStyleSelect(); settingsModal(); }
      catch (e) { fail(e); }
    };
    $("#s-save", m).onclick = async () => {
      const body = { default_style: $("#s-style", m).value, contact_email: $("#s-email", m).value, zotero_dir: $("#s-zot", m).value };
      const ads = $("#s-ads", m).value.trim(); if (ads) body.ads_token = ads;
      try { await post("/api/settings", body); closeModal(); await loadState(); renderStyleSelect(); toast("Settings saved"); }
      catch (e) { fail(e); }
    };
  });
}

async function verifyAction() {
  if (S.selected.size) {
    const r = await post("/api/verify", { ids: [...S.selected] }).catch(fail);
    return r && r.background_job && watchJob({ id: r.background_job }, () => { loadItems(true); loadState(); S.focusId && showDetail(S.focusId); });
  }
  const scope = S.view.collection ? "this collection" : "your whole library";
  modal(`<h3>Verify ${esc(scope)}</h3>
    <p>Each reference is checked against Crossref, DataCite, arXiv, ISBN services${S.state.settings.ads_token_set ? ", and NASA ADS" : ""}: DOIs that don't exist, metadata that disagrees, retractions, missing DOIs, dead links.</p>
    <label><input type="checkbox" id="v-unchecked" checked> Only references not yet checked</label><br>
    <label><input type="checkbox" id="v-safe"> Apply safe fixes automatically (fill empty fields, remove junk; never overwrites)</label><br>
    <label><input type="checkbox" id="v-corr"> Also use the registry's title, authors, and pages where a confirmed reference differs</label>
    <div class="foot"><button data-close>Cancel</button><button class="primary" id="v-go">Start</button></div>`, (m) => {
    $("#v-go", m).onclick = async () => {
      const body = { only_unchecked: $("#v-unchecked", m).checked,
        apply_safe: $("#v-corr", m).checked ? "corrections" : $("#v-safe", m).checked };
      if (S.view.collection) body.collection = Number(S.view.collection);
      const r = await post("/api/verify", body).catch(fail); closeModal();
      r && r.background_job && watchJob({ id: r.background_job }, () => { loadItems(true); loadState(); });
    };
  });
}

/* --------------------------------------------------------------- helpers */
function copy(text, msg) {
  navigator.clipboard.writeText(text).then(() => toast(msg || `Copied ${text.length > 60 ? "" : text}`), () => {
    const ta = document.createElement("textarea"); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove(); toast(msg || "Copied");
  });
}
function htmlText(h) { const d = document.createElement("div"); d.innerHTML = sanitize(h); return d.innerText.trim(); }
function safeUrl(u) { return /^(https?:|mailto:)/i.test(u || "") ? u : "#"; }
function linkify(s) { return esc(s).replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>').replace(/@([\w:.\-]+)/g, '<a href="#" data-key="$1">@$1</a>'); }
const ALLOWED = new Set(["P", "BR", "B", "STRONG", "I", "EM", "U", "SUB", "SUP", "UL", "OL", "LI", "A", "H1", "H2", "H3", "H4", "BLOCKQUOTE", "PRE", "CODE", "SPAN", "DIV", "TABLE", "TR", "TD", "TH", "TBODY", "THEAD"]);
function sanitize(html) {
  const doc = new DOMParser().parseFromString(String(html || ""), "text/html");
  const walk = (node) => {
    for (const el of Array.from(node.children)) {
      if (!ALLOWED.has(el.tagName)) { el.replaceWith(doc.createTextNode(el.tagName === "SCRIPT" || el.tagName === "STYLE" ? "" : el.textContent)); continue; }
      for (const a of Array.from(el.attributes)) {
        if (!(el.tagName === "A" && a.name === "href" && /^(https?:|mailto:)/i.test(a.value))) el.removeAttribute(a.name);
      }
      if (el.tagName === "A") { el.setAttribute("target", "_blank"); el.setAttribute("rel", "noopener"); }
      walk(el);
    }
  };
  walk(doc.body);
  return doc.body.innerHTML;
}

async function loadStyles() {
  const r = await api("/api/styles").catch(() => ({ styles: [], default: "" }));
  S.styles = r.styles;
  if (!S.style) S.style = r.default;
  renderStyleSelect();
}
function renderStyleSelect() {
  const sel = $("#style-select");
  sel.innerHTML = S.styles.map((s) => `<option value="${esc(s.id)}" ${s.id === S.style ? "selected" : ""}>${esc(s.title)}</option>`).join("");
}
$("#style-select").onchange = async (e) => {
  S.style = e.target.value; localStorage.setItem("rumil.style", S.style);
  await post("/api/settings", { default_style: S.style }).catch(fail);
  if (S.focusId) showDetail(S.focusId);
};

/* ---------------------------------------------------------------- wiring */
let searchTimer = null;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { S.view.q = e.target.value.trim(); S.userSorted = false; loadItems(true); }, 220);
});
$("#tag-filter").addEventListener("input", renderTags);
$("#btn-add").onclick = addModal;
$("#btn-import").onclick = importModal;
$("#btn-export").onclick = exportModal;
$("#btn-settings").onclick = settingsModal;
$("#btn-verify").onclick = verifyAction;
$("#btn-new-coll").onclick = async () => {
  const name = prompt("New collection name"); if (!name) return;
  await post("/api/collections", { name }).catch(fail); loadState();
};
document.addEventListener("click", (e) => {
  const a = e.target.closest("a[data-key]"); if (!a) return;
  e.preventDefault(); $("#search").value = a.dataset.key; S.view.q = a.dataset.key; loadItems(true);
});
// In a narrow window the details slide over the list; the close button and Esc put them away again.
document.addEventListener("click", (e) => { if (e.target.closest(".d-close")) $("#detail").classList.remove("open"); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if ($("#modal-root").innerHTML) return closeModal();
    return $("#detail").classList.remove("open");
  }
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "f") { e.preventDefault(); $("#search").focus(); return; }
  if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName) || $("#modal-root").innerHTML) return;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    const ids = S.items.map((i) => i.id); let i = ids.indexOf(S.focusId);
    i = e.key === "ArrowDown" ? Math.min(ids.length - 1, i + 1) : Math.max(0, i - 1);
    if (ids[i] == null) return;
    S.focusId = ids[i]; S.anchorId = ids[i]; S.selected = new Set([ids[i]]); renderTable(); showDetail(ids[i]);
    const row = $(`tr[data-id="${ids[i]}"]`); row && row.scrollIntoView({ block: "nearest" });
  }
  if ((e.key === "Backspace" || e.key === "Delete") && S.selected.size && S.view.special !== "trash") {
    e.preventDefault();
    if (!confirm(`Move ${S.selected.size} reference(s) to the trash?`)) return;
    Promise.all([...S.selected].map((id) => del(`/api/items/${id}`))).then(() => { S.selected.clear(); loadItems(true); loadState(); }).catch(fail);
  }
});

(async function init() {
  try {
    await loadState();
    await loadStyles();
    await loadItems(true);
    const st = S.state.stats;
    if (!st.items) toast("Library is empty: use Import → Sync from Zotero to bring in your references.", 10000);
  } catch (e) { fail(e); }
})();
