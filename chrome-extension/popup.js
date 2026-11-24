/* Save to Rumil: reads citation metadata from the current tab and sends it to the local Rumil app,
   which resolves the DOI/arXiv id against the registries before saving. */
"use strict";

const RUMIL = "http://127.0.0.1:23120";
const main = document.getElementById("main");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Runs inside the page: collect <meta> tags, JSON-LD, and the title.
function collectPageMetadata() {
  const meta = {};
  for (const m of document.querySelectorAll("meta[name], meta[property], meta[itemprop]")) {
    const k = (m.getAttribute("name") || m.getAttribute("property") || m.getAttribute("itemprop") || "").toLowerCase().trim();
    const v = (m.getAttribute("content") || "").trim();
    if (k && v) (meta[k] = meta[k] || []).push(v);
  }
  const jsonld = [];
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    try { const d = JSON.parse(s.textContent); (Array.isArray(d) ? d : (d["@graph"] || [d])).forEach((x) => jsonld.push(x)); } catch (e) { /* ignore */ }
  }
  // A DOI link on the page is a last resort for pages without citation tags. It may belong to a cited
  // reference instead, so Rumil only uses it if the DOI's registered title matches this page's title.
  if (!meta["citation_doi"] && !meta["dc.identifier"]) {
    const a = document.querySelector('a[href*="doi.org/10."]');
    if (a) meta["rumil_doi_link"] = [a.getAttribute("href")];
  }
  return { url: location.href, title: document.title, meta, jsonld: jsonld.slice(0, 5) };
}

async function rumil(path, body) {
  const r = await fetch(RUMIL + path, {
    method: body ? "POST" : "GET",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

function flatten(tree, depth = 0, out = []) {
  for (const c of tree || []) { out.push({ id: c.id, name: c.name, depth }); flatten(c.children, depth + 1, out); }
  return out;
}

async function run() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  let page = { url: tab.url, title: tab.title, meta: {}, jsonld: [] };
  try {
    const [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: collectPageMetadata });
    if (res && res.result) page = res.result;
  } catch (e) { /* PDF viewer or restricted page: the URL alone still works */ }

  let pv;
  try { pv = await rumil("/api/capture/preview", page); }
  catch (e) {
    const offline = /Failed to fetch|NetworkError/i.test(e.message);
    main.innerHTML = offline
      ? `<p><b>Rumil isn't running.</b></p><p class="muted">Start it in Terminal with <code>rumil serve</code>, or run <code>rumil install-service</code> once so it starts at login.</p>`
      : `<p><b>Couldn't read this page.</b></p><p class="muted">${esc(e.message)}</p>`;
    return;
  }
  const s = pv.summary;
  const cls = pv.verified ? "ok" : "warn";
  const label = pv.verified ? `Verified with ${esc(pv.source === "crossref" ? "Crossref" : pv.source === "datacite" ? "DataCite" : pv.source)}` : "Not in any registry: will be saved as unverified";
  const colls = flatten(pv.collections);
  main.innerHTML = `
    <div class="title">${esc(s.title || "(no title found)")}</div>
    <div class="meta">${esc([s.creator, s.year, s.container].filter(Boolean).join(" \u00b7 "))}${s.doi ? `<br><code>${esc(s.doi)}</code>` : ""}</div>
    <div class="badge ${cls}"><span class="dot"></span>${label}</div>
    ${pv.duplicate ? `<div class="note">Already in your library as <code>@${esc(pv.duplicate.citekey)}</code>. Saving will just add it to the collection/tags below.</div>` : ""}
    ${(pv.warnings || []).map((w) => `<div class="note">${esc(w)}</div>`).join("")}
    <label>Collection<select id="coll"><option value="">(none)</option>${colls.map((c) => `<option value="${c.id}">${"\u2003".repeat(c.depth)}${esc(c.name)}</option>`).join("")}</select></label>
    <label>Tags (comma separated)<input id="tags" placeholder="e.g. hollows, mercury"></label>
    <div class="row"><button class="primary" id="save">${pv.duplicate ? "Update" : "Save"}</button></div>`;
  const last = localStorage.getItem("rumil.lastCollection");
  if (last && colls.some((c) => String(c.id) === last)) document.getElementById("coll").value = last;
  document.getElementById("save").onclick = async () => {
    const coll = document.getElementById("coll").value;
    localStorage.setItem("rumil.lastCollection", coll);
    const tags = document.getElementById("tags").value.split(",").map((t) => t.trim()).filter(Boolean);
    const btn = document.getElementById("save");
    btn.disabled = true; btn.textContent = "Saving\u2026";
    try {
      const r = await rumil("/api/capture", { ...page, collection: coll ? Number(coll) : null, tags });
      const key = r.item.citekey;
      main.innerHTML = `<div class="badge ${r.item.status.startsWith("verified") || r.item.status === "url_ok" ? "ok" : "warn"}"><span class="dot"></span>${r.status === "exists" ? "Already saved" : "Saved"}</div>
        <div class="title">${esc(r.item.title)}</div>
        <p>Citekey <code>${esc(key)}</code></p>
        <div class="row"><button data-c="[@${esc(key)}]">Copy [@key]</button><button data-c="\\citep{${esc(key)}}">Copy \\citep</button>
        <button id="open">Open Rumil</button></div>`;
      document.querySelectorAll("[data-c]").forEach((b) => (b.onclick = () => navigator.clipboard.writeText(b.dataset.c).then(() => (b.textContent = "Copied"))));
      document.getElementById("open").onclick = () => chrome.tabs.create({ url: RUMIL + "/" });
    } catch (e) {
      btn.disabled = false; btn.textContent = "Save";
      main.insertAdjacentHTML("beforeend", `<div class="note">${esc(e.message)}</div>`);
    }
  };
}

run();
