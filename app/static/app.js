"use strict";
/* MyDrive web app — no build step, no dependencies. */

const $ = (s, el = document) => el.querySelector(s);
const state = { me: null, path: "", items: [], sel: new Set(), view: "files", search: null };

// ------------------------------------------------------------------ helpers
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v; // only used with constant markup
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) el.append(c instanceof Node ? c : String(c));
  return el;
}

async function api(method, url, body, raw) {
  const opt = { method, headers: { "X-Requested-With": "mydrive" } };
  if (body !== undefined) {
    if (raw) opt.body = body;
    else { opt.body = JSON.stringify(body); opt.headers["Content-Type"] = "application/json"; }
  }
  const r = await fetch(url, opt);
  if (r.status === 401 && !url.endsWith("/login")) { showLogin(); throw new Error("Please sign in"); }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.detail) ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : `Error ${r.status}`);
  return data;
}
const q = (o) => new URLSearchParams(o).toString();
const join = (a, b) => (a && b ? `${a}/${b}` : a || b);
const parentOf = (p) => (p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "");
const baseName = (p) => p.slice(p.lastIndexOf("/") + 1);

function fmtSize(n) {
  if (n == null) return "";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(n < 10 ? 1 : 0) : n) + " " + u[i];
}
function fmtDate(t) {
  if (!t) return "—";
  const d = new Date(t * 1000), now = new Date();
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString([], { year: d.getFullYear() === now.getFullYear() ? undefined : "numeric", month: "short", day: "numeric" });
}
function toast(msg, err) {
  const t = h("div", { class: "toast" + (err ? " err" : "") }, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), err ? 6000 : 3000);
}
const fail = (e) => toast(e.message || String(e), true);
async function copyText(s) {
  try { await navigator.clipboard.writeText(s); toast("Copied to clipboard"); }
  catch { prompt("Copy this:", s); }
}

// ------------------------------------------------------------------ icons
const ICONS = {
  folder: '<svg viewBox="0 0 24 24"><path fill="#5f9cf6" d="M3 6.5A2.5 2.5 0 0 1 5.5 4h4l2 2h7A2.5 2.5 0 0 1 21 8.5v9a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5z"/></svg>',
  shared: '<svg viewBox="0 0 24 24"><path fill="#12b76a" d="M3 6.5A2.5 2.5 0 0 1 5.5 4h4l2 2h7A2.5 2.5 0 0 1 21 8.5v9a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5z"/><circle cx="10" cy="12.5" r="2" fill="#fff"/><circle cx="15" cy="12.5" r="2" fill="#fff"/><path fill="#fff" d="M6.5 17.5c.3-1.6 1.8-2.6 3.5-2.6s3.2 1 3.5 2.6zm6 0c.3-1.6 1-2.6 2.5-2.6s2.7 1 3 2.6z"/></svg>',
};
const FT = { image: "#f79009", video: "#e31b54", audio: "#7a5af8", pdf: "#d92d20", archive: "#667085", code: "#0ba5ec", doc: "#2563eb", sheet: "#12b76a", file: "#98a2b3" };
function kind(it) {
  const m = it.mime || "", ext = (it.name.split(".").pop() || "").toLowerCase();
  if (m.startsWith("image/")) return "image";
  if (m.startsWith("video/")) return "video";
  if (m.startsWith("audio/")) return "audio";
  if (m === "application/pdf") return "pdf";
  if (["zip", "rar", "7z", "gz", "tar", "bz2", "xz", "tgz"].includes(ext)) return "archive";
  if (["xls", "xlsx", "csv", "ods", "tsv"].includes(ext)) return "sheet";
  if (["doc", "docx", "odt", "rtf", "ppt", "pptx", "key", "pages"].includes(ext)) return "doc";
  if (["js", "ts", "py", "json", "html", "css", "sh", "go", "rs", "java", "c", "cpp", "yml", "yaml", "md", "txt", "xml", "sql", "toml", "log"].includes(ext)) return "code";
  return "file";
}
function icon(it) {
  const span = h("span", { class: "ficon" });
  if (it.is_dir) span.innerHTML = it.shared ? ICONS.shared : ICONS.folder;
  else {
    const c = FT[kind(it)];
    span.innerHTML = `<svg viewBox="0 0 24 24"><path fill="${c}" d="M6 2.5h8l5 5v13a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 5 20.5V4A1.5 1.5 0 0 1 6.5 2.5z" opacity=".9"/><path fill="#fff" opacity=".55" d="M14 2.5v5h5z"/></svg>`;
  }
  return span;
}

// ------------------------------------------------------------------ modal / menu
const modal = $("#modal");
function openModal({ title, body, actions = [], wide = false, onClose }) {
  modal.className = "modal" + (wide ? " wide" : "");
  modal.replaceChildren(
    h("div", { class: "modal-head" }, h("h2", {}, title), h("button", { class: "icon-btn", "aria-label": "Close", onclick: closeModal }, "✕")),
    h("div", { class: "modal-body" }, body),
    actions.length ? h("div", { class: "modal-foot" }, actions.map((a) =>
      h("button", { class: "btn " + (a.primary ? "primary" : a.danger ? "danger" : ""), type: a.submit ? "submit" : "button", onclick: a.onClick }, a.label))) : null,
  );
  modal._onClose = onClose;
  if (!modal.open) modal.showModal();
  const inp = modal.querySelector("input:not([type=checkbox])");
  if (inp) setTimeout(() => { inp.focus(); inp.select && inp.select(); }, 30);
}
function closeModal() { if (modal.open) modal.close(); }
modal.addEventListener("close", () => { modal.querySelectorAll("video,audio").forEach((m) => m.pause()); modal._onClose && modal._onClose(); modal.replaceChildren(); });
modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });

function ask(title, label, value = "", okLabel = "OK") {
  return new Promise((resolve) => {
    let done = false;
    const inp = h("input", { value });
    const submit = () => { done = true; closeModal(); resolve(inp.value.trim()); };
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } });
    openModal({ title, body: h("label", {}, label, inp), actions: [{ label: "Cancel", onClick: closeModal }, { label: okLabel, primary: true, onClick: submit }],
      onClose: () => { if (!done) resolve(null); } });
  });
}
function confirmBox(title, text, okLabel = "Delete", danger = true) {
  return new Promise((resolve) => {
    let done = false;
    openModal({ title, body: h("p", {}, text), actions: [{ label: "Cancel", onClick: closeModal },
      { label: okLabel, primary: !danger, danger, onClick: () => { done = true; closeModal(); resolve(true); } }],
      onClose: () => { if (!done) resolve(false); } });
  });
}

const menu = $("#menu");
function openMenu(x, y, entries) {
  menu.replaceChildren(...entries.map((e) => e === "-" ? h("hr") :
    h("button", { class: e.danger ? "danger" : "", onclick: () => { hideMenu(); e.fn(); } }, e.label)));
  menu.hidden = false;
  const r = menu.getBoundingClientRect();
  menu.style.left = Math.min(x, innerWidth - r.width - 8) + "px";
  menu.style.top = Math.min(y, innerHeight - r.height - 8) + "px";
}
function hideMenu() { menu.hidden = true; }
document.addEventListener("click", (e) => { if (!menu.contains(e.target)) hideMenu(); }, true);
addEventListener("resize", hideMenu);

// ------------------------------------------------------------------ auth
function showLogin() { $("#app").hidden = true; $("#login").hidden = false; closeModal(); }
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  $("#login-error").textContent = "";
  try {
    await api("POST", "/api/login", { username: f.get("username"), password: f.get("password") });
    e.target.reset();
    await boot();
  } catch (err) { $("#login-error").textContent = err.message; }
});
$("#logout").addEventListener("click", async () => { await api("POST", "/api/logout").catch(() => {}); showLogin(); });

async function refreshMe() {
  state.me = await api("GET", "/api/me");
  const m = state.me, used = m.usage_bytes;
  $("#whoami").textContent = m.username;
  $("#nav-users").hidden = !m.is_admin;
  const meter = $("#meter");
  if (m.quota_bytes) {
    const pct = Math.min(100, (used / m.quota_bytes) * 100);
    meter.replaceChildren(h("div", { class: "bar" }, h("div", { style: `width:${pct}%` })), `${fmtSize(used)} of ${fmtSize(m.quota_bytes)} used`);
  } else {
    const pct = Math.min(100, (m.disk.used / m.disk.total) * 100);
    meter.replaceChildren(h("div", {}, `You use ${fmtSize(used)}`), h("div", { class: "bar" }, h("div", { style: `width:${pct}%` })),
      `Disk: ${fmtSize(m.disk.free)} free of ${fmtSize(m.disk.total)}`);
  }
}

async function boot() {
  try { await refreshMe(); } catch { showLogin(); return; }
  $("#login").hidden = true; $("#app").hidden = false;
  route();
}

// ------------------------------------------------------------------ routing
addEventListener("hashchange", route);
function route() {
  const hsh = decodeURIComponent(location.hash.slice(1) || "/files/");
  closeSidebar();
  state.sel.clear();
  if (hsh.startsWith("/files")) {
    state.view = "files"; state.search = null;
    state.path = hsh.replace(/^\/files\/?/, "");
    $("#search").value = "";
    loadFiles();
  } else if (hsh.startsWith("/search/")) {
    state.view = "files"; state.search = hsh.slice(8); loadFiles();
  } else {
    state.view = hsh.slice(1);
    ({ trash: viewTrash, shares: viewShares, keys: viewKeys, account: viewAccount, users: viewUsers }[state.view] || (() => (location.hash = "#/files/")))();
  }
  const active = state.view === "files" ? (state.path === "Shared" || state.path.startsWith("Shared/") ? "shared" : (state.search ? "" : "files")) : state.view;
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === active));
}
function goPath(p) { location.hash = "#/files/" + p.split("/").map(encodeURIComponent).join("/"); }

$("#search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const v = $("#search").value.trim();
  location.hash = v ? "#/search/" + encodeURIComponent(v) : "#/files/";
});
$("#menu-btn").addEventListener("click", () => { $("#sidebar").classList.add("open"); $("#scrim").classList.add("open"); });
$("#scrim").addEventListener("click", closeSidebar);
function closeSidebar() { $("#sidebar").classList.remove("open"); $("#scrim").classList.remove("open"); }

// ------------------------------------------------------------------ files view
async function loadFiles() {
  const view = $("#view"), want = location.hash;
  try {
    if (state.search) state.items = (await api("GET", "/api/search?" + q({ q: state.search }))).items;
    else state.items = (await api("GET", "/api/files?" + q({ path: state.path }))).items;
  } catch (e) {
    view.replaceChildren(h("div", { class: "empty" }, h("div", { class: "big" }, "⚠️"), e.message, h("p", {}, h("a", { href: "#/files/" }, "Go to My files"))));
    return;
  }
  if (location.hash !== want || state.view !== "files") return;
  renderFiles();
}

function crumbs() {
  if (state.search) return h("div", { class: "crumbs" }, `Search results for “${state.search}”`);
  const el = h("div", { class: "crumbs" }, h("a", { href: "#/files/" }, "My files"));
  let acc = "";
  for (const part of state.path.split("/").filter(Boolean)) {
    acc = join(acc, part);
    const p = acc;
    el.append(h("span", { class: "sep" }, "›"), h("a", { href: "#", onclick: (e) => { e.preventDefault(); goPath(p); } }, part === "Shared" && p === "Shared" ? "Family shared" : part));
  }
  return el;
}

function renderFiles() {
  const view = $("#view");
  const sel = state.sel;
  const toolbar = h("div", { class: "toolbar" }, crumbs());
  if (sel.size) {
    const paths = [...sel];
    toolbar.append(h("div", { class: "selbar" }, `${sel.size} selected`,
      h("button", { class: "btn small", onclick: () => downloadMany(paths) }, "Download"),
      h("button", { class: "btn small", onclick: () => moveDialog(paths, false) }, "Move"),
      h("button", { class: "btn small", onclick: () => moveDialog(paths, true) }, "Copy"),
      h("button", { class: "btn small danger", onclick: () => deletePaths(paths) }, "Delete"),
      h("button", { class: "icon-btn", title: "Clear selection", onclick: () => { sel.clear(); renderFiles(); } }, "✕")));
  } else if (!state.search) {
    toolbar.append(
      h("button", { class: "btn small", onclick: () => $("#file-input").click() }, "Upload files"),
      h("button", { class: "btn small", onclick: () => $("#folder-input").click() }, "Upload folder"),
      h("button", { class: "btn small", onclick: newFolder }, "New folder"));
  }
  if (!state.items.length) {
    view.replaceChildren(toolbar, h("div", { class: "empty" }, h("div", { class: "big" }, state.search ? "🔍" : "📂"),
      state.search ? "Nothing found." : h("div", {}, "This folder is empty.", h("br"), "Drag files here or use ", h("b", {}, "New"), " to upload.")));
    return;
  }
  const allChk = h("input", { type: "checkbox", "aria-label": "Select all", onchange: (e) => {
    sel.clear(); if (e.target.checked) state.items.filter((i) => !i.shared).forEach((i) => sel.add(i.path)); renderFiles(); } });
  allChk.checked = sel.size && sel.size === state.items.filter((i) => !i.shared).length;
  const tbody = h("tbody");
  for (const it of state.items) {
    const chk = it.shared ? "" : h("input", { type: "checkbox", "aria-label": "Select " + it.name, onclick: (e) => e.stopPropagation(),
      onchange: (e) => { e.target.checked ? sel.add(it.path) : sel.delete(it.path); renderFiles(); } });
    if (chk) chk.checked = sel.has(it.path);
    const tr = h("tr", { class: "row" + (sel.has(it.path) ? " sel" : ""), onclick: () => openItem(it),
      oncontextmenu: (e) => { if (it.shared) return; e.preventDefault(); itemMenu(it, e.clientX, e.clientY); } },
      h("td", { class: "chk" }, chk),
      h("td", { class: "name" }, h("div", { class: "nm" }, icon(it), h("span", { title: it.path }, it.shared ? "Family shared" : it.name),
        it.shared ? h("span", { class: "tag" }, "everyone") : null,
        state.search ? h("span", { class: "muted", style: "font-size:12px" }, parentOf(it.path) || "/") : null)),
      h("td", { class: "size" }, it.is_dir ? "—" : fmtSize(it.size)),
      h("td", { class: "mod" }, fmtDate(it.modified)),
      h("td", { class: "act" }, it.shared ? "" : h("button", { class: "icon-btn", "aria-label": "Actions", onclick: (e) => {
        e.stopPropagation(); const r = e.currentTarget.getBoundingClientRect(); itemMenu(it, r.left - 150, r.bottom); } }, "⋮")));
    tbody.append(tr);
  }
  view.replaceChildren(toolbar, h("table", { class: "files" },
    h("colgroup", {}, h("col", { class: "c-chk" }), h("col"), h("col", { class: "c-size" }), h("col", { class: "c-mod" }), h("col", { class: "c-act" })),
    h("thead", {}, h("tr", {}, h("th", { class: "chk" }, allChk), h("th", {}, "Name"), h("th", {}, "Size"), h("th", { class: "mod" }, "Modified"), h("th"))),
    tbody));
}

function openItem(it) {
  if (it.is_dir) return goPath(it.path);
  preview(it);
}

function itemMenu(it, x, y) {
  openMenu(x, y, [
    !it.is_dir && { label: "Preview", fn: () => preview(it) },
    { label: it.is_dir ? "Download as .zip" : "Download", fn: () => download(it.path) },
    { label: "Share link…", fn: () => shareDialog(it.path) },
    "-",
    { label: "Rename…", fn: () => rename(it) },
    { label: "Move to…", fn: () => moveDialog([it.path], false) },
    { label: "Make a copy…", fn: () => moveDialog([it.path], true) },
    state.search && { label: "Show in folder", fn: () => goPath(parentOf(it.path)) },
    "-",
    { label: "Delete", danger: true, fn: () => deletePaths([it.path]) },
  ].filter(Boolean));
}

const dlUrl = (p, inline) => "/api/download?" + q(inline ? { path: p, inline: 1 } : { path: p });
function download(p) { location.href = dlUrl(p); }
function downloadMany(paths) {
  if (paths.length === 1) return download(paths[0]);
  const u = new URLSearchParams(); paths.forEach((p) => u.append("paths", p));
  location.href = "/api/zip?" + u;
}

async function preview(it) {
  const k = kind(it), src = dlUrl(it.path, true);
  let body;
  if (k === "image" && it.mime !== "image/svg+xml") body = h("img", { src, alt: it.name });
  else if (k === "video") body = h("video", { src, controls: true, autoplay: true, playsinline: true });
  else if (k === "audio") body = h("audio", { src, controls: true, autoplay: true });
  else if (k === "pdf") body = h("iframe", { src, title: it.name });
  else if ((k === "code" || (it.mime || "").startsWith("text/")) && it.size < 2e6) {
    body = h("pre", {}, "Loading…");
    fetch(src).then((r) => r.text()).then((t) => (body.textContent = t)).catch(() => (body.textContent = "Could not load."));
  } else body = h("div", { class: "empty" }, h("div", { class: "big" }, "📄"), "No preview for this file type.", h("br"), fmtSize(it.size));
  openModal({ title: it.name, wide: true, body: h("div", { class: "preview" }, body), actions: [
    { label: "Share", onClick: () => shareDialog(it.path) },
    { label: "Download", primary: true, onClick: () => download(it.path) }] });
}

async function newFolder() {
  const name = await ask("New folder", "Folder name", "Untitled folder", "Create");
  if (!name) return;
  try { await api("POST", "/api/mkdir", { path: join(state.path, name) }); loadFiles(); } catch (e) { fail(e); }
}

async function rename(it) {
  const name = await ask("Rename", "New name", it.name, "Rename");
  if (!name || name === it.name) return;
  try { await api("POST", "/api/move", { src: it.path, dst: join(parentOf(it.path), name) }); toast("Renamed"); loadFiles(); }
  catch (e) { fail(e); }
}

async function deletePaths(paths) {
  const label = paths.length === 1 ? `“${baseName(paths[0])}”` : `${paths.length} items`;
  if (!(await confirmBox("Move to trash?", `${label} will be moved to the trash. You can restore it for ${state.me.trash_days} days.`, "Move to trash"))) return;
  try { await api("POST", "/api/delete", { paths }); toast("Moved to trash"); state.sel.clear(); loadFiles(); refreshMe(); }
  catch (e) { fail(e); }
}

function folderPicker(start, onPick) {
  let cur = start;
  const list = h("div", { class: "picker" }), title = h("div", { class: "muted" });
  async function load() {
    title.textContent = "Destination: /" + cur;
    onPick(cur);
    list.replaceChildren(h("div", { class: "muted" }, "Loading…"));
    try {
      const items = (await api("GET", "/api/files?" + q({ path: cur }))).items.filter((i) => i.is_dir);
      list.replaceChildren(
        cur ? h("div", { onclick: () => { cur = parentOf(cur); load(); } }, "⬆ ..") : "",
        ...items.map((i) => h("div", { onclick: () => { cur = i.path; load(); } }, icon(i), i.shared ? "Family shared" : i.name)));
      if (!items.length && !cur) list.append(h("div", { class: "muted" }, "No folders"));
    } catch (e) { list.replaceChildren(h("div", { class: "error" }, e.message)); }
  }
  load();
  return h("div", {}, title, list);
}

function moveDialog(paths, isCopy) {
  let dest = parentOf(paths[0]);
  const picker = folderPicker(dest, (p) => (dest = p));
  openModal({ title: (isCopy ? "Copy " : "Move ") + (paths.length > 1 ? `${paths.length} items` : `“${baseName(paths[0])}”`), body: picker,
    actions: [{ label: "Cancel", onClick: closeModal }, { label: isCopy ? "Copy here" : "Move here", primary: true, onClick: async () => {
      closeModal();
      let ok = 0;
      for (const p of paths) {
        let dst = join(dest, baseName(p));
        if (isCopy && dst === p) dst = join(dest, "Copy of " + baseName(p));
        try { await api("POST", isCopy ? "/api/copy" : "/api/move", { src: p, dst }); ok++; } catch (e) { fail(`${baseName(p)}: ${e.message}`); }
      }
      if (ok) toast(`${isCopy ? "Copied" : "Moved"} ${ok} item${ok > 1 ? "s" : ""}`);
      state.sel.clear(); loadFiles(); refreshMe();
    } }] });
}

function shareDialog(path) {
  const sel = h("select", {}, h("option", { value: "1" }, "1 day"), h("option", { value: "7", selected: true }, "7 days"),
    h("option", { value: "30" }, "30 days"), h("option", { value: "0" }, "Never expires"));
  const out = h("div");
  openModal({ title: "Share “" + (baseName(path) || path) + "”", body: h("div", {},
    h("p", { class: "muted" }, "Anyone with the link can view and download (no sign-in). You can revoke it any time under Share links."),
    h("label", {}, "Link expires after", sel), out),
    actions: [{ label: "Close", onClick: closeModal }, { label: "Create link", primary: true, onClick: async (e) => {
      try {
        const s = await api("POST", "/api/shares", { path, expires_days: Number(sel.value) || null });
        e.target.disabled = true;
        out.replaceChildren(h("div", { class: "secret" }, h("code", {}, s.url), h("button", { class: "btn small", onclick: () => copyText(s.url) }, "Copy")));
        copyText(s.url);
      } catch (err) { fail(err); }
    } }] });
}

// ------------------------------------------------------------------ uploads
const uploadQ = [];
let uploading = 0;
const upBox = $("#uploads");
function upPanel() {
  if (!upBox.firstChild) {
    upBox.append(h("div", { class: "head" }, h("span", { id: "up-title" }, "Uploading"), h("button", { class: "icon-btn", onclick: () => { if (!uploading) { upBox.hidden = true; upBox.replaceChildren(); } } }, "✕")),
      h("div", { class: "list", id: "up-list" }));
  }
  upBox.hidden = false;
}
function enqueue(files, base) {
  if (!files.length) return;
  upPanel();
  for (const { file, rel } of files) {
    const bar = h("div", { style: "width:0%" }), stat = h("span", { class: "muted" }, "Waiting");
    const row = h("div", { class: "item" }, h("div", { class: "n" }, h("span", { title: rel }, rel), stat), h("div", { class: "bar" }, bar));
    $("#up-list").prepend(row);
    uploadQ.push({ file, path: join(base, rel), bar, stat });
  }
  pump();
}
function pump() {
  while (uploading < 2 && uploadQ.length) {
    const job = uploadQ.shift();
    uploading++;
    $("#up-title").textContent = `Uploading (${uploading + uploadQ.length} left)`;
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", "/api/upload?" + q({ path: job.path, conflict: "rename", mtime: Math.floor((job.file.lastModified || Date.now()) / 1000) }));
    xhr.setRequestHeader("X-Requested-With", "mydrive");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) { const p = (e.loaded / e.total) * 100; job.bar.style.width = p + "%"; job.stat.textContent = Math.floor(p) + "%"; } };
    xhr.onload = () => {
      if (xhr.status < 300) { job.stat.textContent = "✓"; job.bar.style.width = "100%"; }
      else { let m = "Failed"; try { m = JSON.parse(xhr.responseText).detail; } catch {} job.stat.textContent = "✕"; job.stat.title = m; job.bar.style.background = "var(--danger)"; fail(`${job.file.name}: ${m}`); }
      done();
    };
    xhr.onerror = () => { job.stat.textContent = "✕"; job.bar.style.background = "var(--danger)"; fail(`${job.file.name}: network error`); done(); };
    xhr.send(job.file);
  }
  function done() {
    uploading--;
    if (!uploading && !uploadQ.length) {
      $("#up-title").textContent = "Uploads complete"; if (state.view === "files") loadFiles(); refreshMe();
      const hadErr = upBox.querySelector('.bar > div[style*="danger"]');
      if (!hadErr) setTimeout(() => { if (!uploading && !uploadQ.length) { upBox.hidden = true; upBox.replaceChildren(); } }, 6000);
    }
    pump();
  }
}
$("#file-input").addEventListener("change", (e) => { enqueue([...e.target.files].map((f) => ({ file: f, rel: f.name })), state.path); e.target.value = ""; });
$("#folder-input").addEventListener("change", (e) => { enqueue([...e.target.files].map((f) => ({ file: f, rel: f.webkitRelativePath || f.name })), state.path); e.target.value = ""; });
$("#new-btn").addEventListener("click", (e) => {
  if (state.view !== "files" || state.search) { location.hash = "#/files/" + state.path; }
  const r = e.currentTarget.getBoundingClientRect();
  openMenu(r.left, r.bottom + 4, [{ label: "New folder", fn: newFolder }, "-",
    { label: "Upload files", fn: () => $("#file-input").click() }, { label: "Upload folder", fn: () => $("#folder-input").click() }]);
});

// drag & drop (files and folders)
let dragDepth = 0;
const canDrop = () => state.view === "files" && !state.search && !$("#app").hidden;
addEventListener("dragenter", (e) => { if (canDrop() && e.dataTransfer.types.includes("Files")) { dragDepth++; $("#dropzone").hidden = false; } });
addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("#dropzone").hidden = true; } });
addEventListener("dragover", (e) => { if (canDrop()) e.preventDefault(); });
addEventListener("drop", async (e) => {
  e.preventDefault(); dragDepth = 0; $("#dropzone").hidden = true;
  if (!canDrop()) return;
  const entries = [...e.dataTransfer.items].map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
  const out = [];
  async function walk(entry, prefix) {
    if (entry.isFile) await new Promise((res) => entry.file((f) => { out.push({ file: f, rel: prefix + f.name }); res(); }, res));
    else if (entry.isDirectory) {
      const reader = entry.createReader();
      let batch;
      do {
        batch = await new Promise((res) => reader.readEntries(res, () => res([])));
        for (const c of batch) await walk(c, prefix + entry.name + "/");
      } while (batch.length);
    }
  }
  if (entries.length) for (const en of entries) await walk(en, "");
  else for (const f of e.dataTransfer.files) out.push({ file: f, rel: f.name });
  enqueue(out, state.path);
});

// ------------------------------------------------------------------ trash
async function viewTrash() {
  const view = $("#view");
  let data;
  try { data = await api("GET", "/api/trash"); } catch (e) { return fail(e); }
  if (state.view !== "trash") return;
  const page = h("div", { class: "page" }, h("div", { class: "toolbar" }, h("div", { class: "crumbs" }, "Trash"),
    data.items.length ? h("button", { class: "btn small danger", onclick: async () => {
      if (!(await confirmBox("Empty trash?", "All items in the trash will be deleted forever.", "Empty trash"))) return;
      try { await api("DELETE", "/api/trash"); viewTrash(); refreshMe(); } catch (e) { fail(e); } } }, "Empty trash") : null),
    h("p", { class: "muted" }, `Items are deleted forever after ${data.days} days.`));
  if (!data.items.length) page.append(h("div", { class: "empty" }, h("div", { class: "big" }, "🗑️"), "Trash is empty."));
  else page.append(h("table", { class: "simple" }, h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", { class: "hide-sm" }, "Original location"), h("th", { class: "hide-sm" }, "Deleted"), h("th", {}, "Size"), h("th"))),
    h("tbody", {}, data.items.map((t) => h("tr", {},
      h("td", {}, (t.is_dir ? "📁 " : "📄 ") + baseName(t.vpath)), h("td", { class: "muted hide-sm" }, "/" + parentOf(t.vpath)),
      h("td", { class: "muted hide-sm" }, fmtDate(t.deleted_at)), h("td", { class: "muted" }, fmtSize(t.size)),
      h("td", {}, h("div", { class: "row-actions" },
        h("button", { class: "btn small", onclick: async () => { try { const r = await api("POST", `/api/trash/${t.id}/restore`); toast("Restored to /" + r.path); viewTrash(); refreshMe(); } catch (e) { fail(e); } } }, "Restore"),
        h("button", { class: "btn small danger", onclick: async () => {
          if (!(await confirmBox("Delete forever?", `“${baseName(t.vpath)}” will be permanently deleted.`))) return;
          try { await api("DELETE", `/api/trash/${t.id}`); viewTrash(); refreshMe(); } catch (e) { fail(e); } } }, "Delete forever"))))))));
  view.replaceChildren(page);
}

// ------------------------------------------------------------------ share links
async function viewShares() {
  let data;
  try { data = await api("GET", "/api/shares"); } catch (e) { return fail(e); }
  if (state.view !== "shares") return;
  const page = h("div", { class: "page" }, h("h1", {}, "Share links"),
    h("p", { class: "muted" }, "Public links you created. Anyone with a link can download that item. Use ⋮ → Share link on any file or folder to create one."));
  if (!data.items.length) page.append(h("div", { class: "empty" }, h("div", { class: "big" }, "🔗"), "No share links yet."));
  else page.append(h("table", { class: "simple" }, h("thead", {}, h("tr", {}, h("th", {}, "Item"), h("th", { class: "hide-sm" }, "Expires"), h("th", { class: "hide-sm" }, "Downloads"), h("th"))),
    h("tbody", {}, data.items.map((s) => h("tr", {},
      h("td", {}, (s.is_dir ? "📁 " : "📄 ") + s.vpath, s.expired ? h("span", { class: "tag grey" }, "expired") : null),
      h("td", { class: "muted hide-sm" }, s.expires_at ? fmtDate(s.expires_at) : "Never"),
      h("td", { class: "muted hide-sm" }, s.downloads),
      h("td", {}, h("div", { class: "row-actions" },
        h("button", { class: "btn small", onclick: () => copyText(s.url) }, "Copy link"),
        h("button", { class: "btn small danger", onclick: async () => { try { await api("DELETE", `/api/shares/${s.id}`); viewShares(); } catch (e) { fail(e); } } }, "Revoke"))))))));
  $("#view").replaceChildren(page);
}

// ------------------------------------------------------------------ API keys / connections
function snippets(key, username, scope) {
  const B = state.me.base_url;
  return [
    ["Claude.ai / Desktop", `In Claude: Settings → Connectors → Add custom connector\n\nName:  MyDrive${scope ? " (" + scope + ")" : ""}\nURL:   ${B}/mcp/${key}\n\n(The key is part of the URL, so treat this URL like a password.)`],
    ["Claude Code", `claude mcp add --transport http mydrive ${B}/mcp --header "Authorization: Bearer ${key}"`],
    ["curl / REST", `# list a folder\ncurl -H "Authorization: Bearer ${key}" "${B}/api/files?path="\n\n# upload a file\ncurl -H "Authorization: Bearer ${key}" -T report.pdf "${B}/api/upload?path=report.pdf&conflict=overwrite"\n\n# download a file\ncurl -H "Authorization: Bearer ${key}" -o report.pdf "${B}/api/download?path=report.pdf"\n\n# full API reference: ${B}/api/docs`],
    ["Python", `import requests\n\nDRIVE = "${B}"\nH = {"Authorization": "Bearer ${key}"}\n\n# list\nprint(requests.get(f"{DRIVE}/api/files", params={"path": ""}, headers=H).json())\n\n# upload\nwith open("data.csv", "rb") as f:\n    requests.put(f"{DRIVE}/api/upload", params={"path": "data.csv", "conflict": "overwrite"}, data=f, headers=H).raise_for_status()\n\n# download\nr = requests.get(f"{DRIVE}/api/download", params={"path": "data.csv"}, headers=H)\nopen("data.csv", "wb").write(r.content)`],
    ["WebDAV / rclone", `WebDAV URL:  ${B}/dav/\nUsername:    ${username}\nPassword:    ${key}\n\n# rclone (sync, backup, mount)\nrclone config create mydrive webdav url=${B}/dav vendor=other user=${username} pass=$(rclone obscure ${key})\nrclone ls mydrive:`],
  ];
}
function snippetTabs(key, username, scope) {
  const sn = snippets(key, username, scope);
  const pre = h("pre"), copyBtn = h("button", { class: "btn small", style: "margin-top:8px" }, "Copy");
  const tabs = h("div", { class: "tabs" });
  const show = (i) => { pre.textContent = sn[i][1]; copyBtn.onclick = () => copyText(sn[i][1]); tabs.querySelectorAll("button").forEach((b, j) => b.classList.toggle("on", i === j)); };
  sn.forEach(([name], i) => tabs.append(h("button", { onclick: () => show(i) }, name)));
  show(0);
  return h("div", {}, tabs, pre, copyBtn);
}

async function viewKeys() {
  let data;
  try { data = await api("GET", "/api/keys"); } catch (e) { return fail(e); }
  if (state.view !== "keys") return;
  const me = state.me;
  const name = h("input", { placeholder: "e.g. Claude, Blog project, Backup script" });
  let scope = "";
  const scopeLabel = h("code", {}, "/ (everything, incl. Family shared)");
  const access = h("select", {}, h("option", { value: "rw" }, "Read & write"), h("option", { value: "ro" }, "Read only"));
  const pickScope = () => {
    let dest = scope;
    openModal({ title: "Limit key to a folder", body: h("div", {},
      h("p", { class: "muted" }, "The app will only see this folder (as its root). Pick “/” for full access."),
      folderPicker(scope, (p) => (dest = p))),
      actions: [{ label: "New folder here…", onClick: async () => {
        const n = await ask("New folder", "Folder name", "projects/my-app", "Create");
        if (n) { scope = join(dest, n); scopeLabel.textContent = "/" + scope + " (will be created)"; }
      } }, { label: "Use this folder", primary: true, onClick: () => { scope = dest; scopeLabel.textContent = "/" + scope + (scope ? "" : " (everything, incl. Family shared)"); closeModal(); } }] });
  };
  const create = async () => {
    try {
      const k = await api("POST", "/api/keys", { name: name.value || "Untitled", scope, can_write: access.value === "rw" });
      openModal({ title: "Key created: " + k.name, wide: true, body: h("div", {},
        h("p", { class: "warn" }, "Copy this key now — it will not be shown again."),
        h("div", { class: "secret" }, h("code", {}, k.key), h("button", { class: "btn small", onclick: () => copyText(k.key) }, "Copy")),
        h("p", { class: "muted" }, `Access: ${k.can_write ? "read & write" : "read only"} · Folder: /${k.scope}`),
        h("h3", {}, "Connect it"), snippetTabs(k.key, me.username, k.scope)),
        actions: [{ label: "Done", primary: true, onClick: closeModal }], onClose: viewKeys });
    } catch (e) { fail(e); }
  };
  const page = h("div", { class: "page" }, h("h1", {}, "Apps & Claude"),
    h("p", { class: "muted" }, "Create an API key for each app or Claude connection. Keys can be limited to one folder and made read-only, and revoked any time."),
    h("div", { class: "card" }, h("h3", {}, "New API key"),
      h("div", { class: "grid2" }, h("label", {}, "Name", name), h("label", {}, "Access", access)),
      h("label", {}, "Folder", h("div", { class: "secret", style: "margin-top:5px" }, scopeLabel, h("button", { class: "btn small", onclick: pickScope }, "Choose…"))),
      h("button", { class: "btn primary", onclick: create }, "Create key")));
  page.append(h("h3", {}, "Your keys"));
  if (!data.items.length) page.append(h("p", { class: "muted" }, "No keys yet."));
  else page.append(h("table", { class: "simple" }, h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Folder"), h("th", { class: "hide-sm" }, "Access"), h("th", { class: "hide-sm" }, "Last used"), h("th"))),
    h("tbody", {}, data.items.map((k) => h("tr", {},
      h("td", {}, k.name, h("div", { class: "muted", style: "font-size:12px" }, k.prefix + "…")),
      h("td", {}, h("code", {}, "/" + k.scope)),
      h("td", { class: "muted hide-sm" }, k.can_write ? "read & write" : "read only"),
      h("td", { class: "muted hide-sm" }, k.last_used_at ? fmtDate(k.last_used_at) : "never"),
      h("td", {}, h("button", { class: "btn small danger", onclick: async () => {
        if (!(await confirmBox("Revoke key?", `Apps using “${k.name}” will stop working immediately.`, "Revoke"))) return;
        try { await api("DELETE", `/api/keys/${k.id}`); viewKeys(); } catch (e) { fail(e); } } }, "Revoke")))))));
  page.append(h("div", { class: "card" }, h("h3", {}, "How the connections work"),
    h("p", { class: "muted" }, "Claude uses the MCP endpoint to list, read, write, search and share files. Other projects use the REST API (docs at ",
      h("a", { href: "/api/docs", target: "_blank" }, "/api/docs"), ") or WebDAV. Deletes from any app go to your Trash.")));
  $("#view").replaceChildren(page);
}

// ------------------------------------------------------------------ account
function viewAccount() {
  const me = state.me;
  const cur = h("input", { type: "password", autocomplete: "current-password" });
  const nw = h("input", { type: "password", autocomplete: "new-password" });
  const nw2 = h("input", { type: "password", autocomplete: "new-password" });
  const B = me.base_url;
  $("#view").replaceChildren(h("div", { class: "page" }, h("h1", {}, "Account"),
    h("div", { class: "card" }, h("h3", {}, "Storage"),
      h("p", {}, `Signed in as `, h("b", {}, me.username), me.is_admin ? h("span", { class: "tag" }, "admin") : null),
      h("p", { class: "muted" }, `You use ${fmtSize(me.usage_bytes)}${me.quota_bytes ? " of your " + fmtSize(me.quota_bytes) + " quota" : ""} (trash included; Family shared is not counted). Server disk: ${fmtSize(me.disk.free)} free of ${fmtSize(me.disk.total)}.`)),
    h("div", { class: "card" }, h("h3", {}, "Change password"),
      h("div", { class: "grid2" }, h("label", {}, "Current password", cur), h("span"), h("label", {}, "New password", nw), h("label", {}, "Repeat new password", nw2)),
      h("button", { class: "btn primary", onclick: async () => {
        if (nw.value !== nw2.value) return fail(new Error("New passwords do not match"));
        try { await api("POST", "/api/me/password", { current: cur.value, new: nw.value }); toast("Password changed"); cur.value = nw.value = nw2.value = ""; }
        catch (e) { fail(e); } } }, "Change password")),
    h("div", { class: "card" }, h("h3", {}, "Use as a network drive (WebDAV)"),
      h("p", { class: "muted" }, "Sign in with your username and password, or with an API key as the password to limit access to one folder."),
      h("pre", {}, `Address:   ${B}/dav/\nUsername:  ${me.username}\n\nWindows:   File Explorer → This PC → Map network drive → ${B}/dav/\nmacOS:     Finder → Go → Connect to Server (⌘K) → ${B}/dav/\niPhone:    Files app → ⋯ → Connect to Server → ${B}/dav/\nAndroid:   use an app with WebDAV support (e.g. Solid Explorer, CX File Explorer)`)),
    h("div", { class: "card" }, h("h3", {}, "Install as an app"),
      h("p", { class: "muted" }, "On your phone, open this site in the browser and choose “Add to Home Screen”."))));
}

// ------------------------------------------------------------------ users (admin)
async function viewUsers() {
  let data;
  try { data = await api("GET", "/api/admin/users"); } catch (e) { return fail(e); }
  if (state.view !== "users") return;
  const un = h("input", { autocapitalize: "none", placeholder: "lowercase, e.g. mom" });
  const pw = h("input", { type: "text", placeholder: "min 8 characters" });
  const quota = h("input", { type: "number", min: "0", step: "1", placeholder: "no limit" });
  const adm = h("input", { type: "checkbox" });
  const patch = async (u, body, msg) => { try { await api("PATCH", `/api/admin/users/${u.id}`, body); toast(msg); viewUsers(); } catch (e) { fail(e); } };
  const page = h("div", { class: "page" }, h("h1", {}, "Users"),
    h("p", { class: "muted" }, "Each person gets a private space plus access to the Family shared folder."),
    h("div", { class: "card" }, h("h3", {}, "Add a family member"),
      h("div", { class: "grid2" }, h("label", {}, "Username", un), h("label", {}, "Initial password", pw),
        h("label", {}, "Quota (GB)", quota), h("label", { style: "display:flex;gap:8px;align-items:center;margin-top:24px" }, adm, "Administrator")),
      h("button", { class: "btn primary", onclick: async () => {
        try { await api("POST", "/api/admin/users", { username: un.value, password: pw.value, is_admin: adm.checked, quota_gb: quota.value ? Number(quota.value) : null });
          toast("User created"); viewUsers(); } catch (e) { fail(e); } } }, "Add user")),
    h("table", { class: "simple" }, h("thead", {}, h("tr", {}, h("th", {}, "User"), h("th", {}, "Used"), h("th", { class: "hide-sm" }, "Quota"), h("th"))),
      h("tbody", {}, data.items.map((u) => h("tr", {},
        h("td", {}, u.username, u.is_admin ? h("span", { class: "tag" }, "admin") : null, u.disabled ? h("span", { class: "tag grey" }, "disabled") : null),
        h("td", { class: "muted" }, fmtSize(u.usage_bytes)),
        h("td", { class: "muted hide-sm" }, u.quota_bytes ? fmtSize(u.quota_bytes) : "no limit"),
        h("td", {}, h("button", { class: "icon-btn", "aria-label": "User actions", onclick: (e) => {
          const r = e.currentTarget.getBoundingClientRect();
          openMenu(r.left - 160, r.bottom, [
            { label: "Reset password…", fn: async () => { const p = await ask("Reset password for " + u.username, "New password (min 8 characters)", "", "Set password"); if (p) patch(u, { password: p }, "Password reset"); } },
            { label: "Set quota…", fn: async () => { const g = await ask("Quota for " + u.username, "Quota in GB (empty = no limit)", u.quota_bytes ? String(Math.round(u.quota_bytes / 1024 ** 3)) : "", "Save");
              if (g !== null) patch(u, g === "" ? { clear_quota: true } : { quota_gb: Number(g) }, "Quota updated"); } },
            { label: u.is_admin ? "Remove admin" : "Make admin", fn: () => patch(u, { is_admin: !u.is_admin }, "Updated") },
            { label: u.disabled ? "Enable account" : "Disable account", fn: () => patch(u, { disabled: !u.disabled }, "Updated") },
            "-",
            { label: "Delete user…", danger: true, fn: async () => {
              if (!(await confirmBox("Delete " + u.username + "?", "The account, its keys and share links are removed. Their files stay on the server disk (an admin can recover them over SSH)."))) return;
              try { await api("DELETE", `/api/admin/users/${u.id}`); toast("User deleted"); viewUsers(); } catch (e) { fail(e); } } },
          ]); } }, "⋮")))))));
  $("#view").replaceChildren(page);
}

boot();
