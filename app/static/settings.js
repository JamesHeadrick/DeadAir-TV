"use strict";
// Settings screen: edits config.yaml through GET/PUT api/config.
// Uses $, el, api, showView from app.js.

let draft = null;        // working copy of the config
let savedJson = "";      // last saved state, for dirty tracking
let providers = null;    // TMDB provider list (name, logo) for your region
let expanded = new Set(); // tmdb_ids whose editor is open

const isDirty = () => draft && JSON.stringify(stripped(draft)) !== savedJson;

function stripped(d) {
  // Only the fields PUT cares about, so display-only fields don't count as edits.
  return {
    services: d.services,
    include_free: d.include_free,
    include_rent_buy: d.include_rent_buy,
    cooldown_days: d.cooldown_days,
    search_urls: d.search_urls,
    shows: d.shows.map(({ tmdb_id, channels, weight, name, title, links }) =>
      ({ tmdb_id, channels, weight, name, title, links })),
  };
}

function settingsCanLeave() {
  return !isDirty() || confirm("Discard unsaved settings changes?");
}

async function openSettings() {
  showView("settings");
  showNotice($("settings-error"), "");
  $("me-name").textContent = me.username;
  $("me-role").textContent = me.is_admin ? " (admin)" : "";
  draft = null;
  $("save-bar").hidden = true;
  if (!me.is_admin) {
    api("api/channels").then((d) => {
      $("cooldown-text").textContent = `for ${d.cooldown_days} days`;
    }).catch(() => {});
    return;
  }
  loadUsers();
  try {
    draft = await api("api/config");
    savedJson = JSON.stringify(stripped(draft));
    if (draft.config_error) {
      showNotice($("settings-error"),
        `config.yaml has an error (${draft.config_error}). Showing the last good version; saving will overwrite the file.`, true);
    }
  } catch (e) {
    showNotice($("settings-error"), `Couldn't load settings: ${e.message}`, true);
    return;
  }
  expanded = new Set();
  renderSettings();
  loadProviders();
}

function changed() {
  if (!draft) return;
  if ($("settings-error").classList.contains("error") && !draft.config_error) showNotice($("settings-error"), "");
  $("save-bar").hidden = !isDirty();
  $("save-status").textContent = "Unsaved changes";
}

function renderSettings() {
  renderServices();
  $("include-free").checked = draft.include_free;
  $("include-rent-buy").checked = draft.include_rent_buy;
  $("cooldown-days").value = draft.cooldown_days;
  $("cooldown-text").textContent = "for the number of days below";
  renderShows();
  renderSearchUrls();
  changed();
}

// --- services ---------------------------------------------------------------

async function loadProviders() {
  if (providers) return renderProviderList();
  try {
    providers = (await api("api/tmdb/providers")).providers;
  } catch (e) {
    providers = [];
    $("provider-list").replaceChildren(el("p", { className: "hint", textContent: `Provider list unavailable (${e.message}). Type a name and press Enter.` }));
    return;
  }
  renderServices();
  renderProviderList();
}

const logoFor = (name) => providers?.find((p) => p.provider_name === name)?.logo_url;

function renderServices() {
  $("my-service-chips").replaceChildren(...draft.services.map((name, i) => {
    const logo = logoFor(name);
    const chip = el("button", { className: "chip-btn" + (logo ? "" : " plain"), title: "Remove" },
      logo ? el("img", { src: logo, alt: "" }) : null, `${i + 1}. ${name} ✕`);
    chip.addEventListener("click", () => {
      draft.services.splice(i, 1);
      renderServices();
      renderProviderList();
      changed();
    });
    return chip;
  }));
  if (!draft.services.length) {
    $("my-service-chips").append(el("span", { className: "hint", textContent: "None yet." }));
  }
}

function renderProviderList() {
  const q = $("provider-filter").value.trim().toLowerCase();
  const list = $("provider-list");
  if (!q || !providers) return list.replaceChildren();
  const matches = providers
    .filter((p) => p.provider_name.toLowerCase().includes(q) && !draft.services.includes(p.provider_name))
    .slice(0, 12);
  list.replaceChildren(...matches.map((p) => {
    const row = el("button", { className: "pick-row" },
      p.logo_url ? el("img", { src: p.logo_url, alt: "" }) : el("span", { className: "ph" }),
      el("span", { className: "grow", textContent: p.provider_name }),
      el("span", { className: "tag", textContent: "Add" }));
    row.addEventListener("click", () => addService(p.provider_name));
    return row;
  }));
}

function addService(name) {
  if (name && !draft.services.includes(name)) draft.services.push(name);
  $("provider-filter").value = "";
  renderServices();
  renderProviderList();
  changed();
}

// --- shows ------------------------------------------------------------------

const allChannels = () => [...new Set(draft.shows.flatMap((s) => s.channels))];
const showTitle = (s) => s.name || s.title || `TMDB #${s.tmdb_id}`;

let searchTimer = null;
let searchSeq = 0;

function onSearchInput() {
  clearTimeout(searchTimer);
  const q = $("show-search").value.trim();
  if (!q) return $("search-results").replaceChildren();
  searchTimer = setTimeout(() => runSearch(q), 300);
}

async function runSearch(q) {
  const seq = ++searchSeq;
  let results;
  try {
    results = (await api(`api/tmdb/search?q=${encodeURIComponent(q)}`)).results;
  } catch (e) {
    $("search-results").replaceChildren(el("p", { className: "hint", textContent: e.message }));
    return;
  }
  if (seq !== searchSeq) return; // a newer search is in flight
  renderSearchResults(results.slice(0, 10));
}

function renderSearchResults(results) {
  const box = $("search-results");
  if (!results.length) return box.replaceChildren(el("p", { className: "hint", textContent: "No matches." }));
  box.replaceChildren(...results.map((r) => {
    const have = draft.shows.some((s) => s.tmdb_id === r.tmdb_id);
    const row = el("button", { className: "pick-row result", disabled: have },
      r.poster_url ? el("img", { src: r.poster_url, alt: "" }) : el("span", { className: "ph" }),
      el("span", { className: "grow" }, r.title, el("small", { textContent: [r.year, r.overview].filter(Boolean).join(" · ").slice(0, 90) })),
      el("span", { className: "tag", textContent: have ? "Added" : "Add" }));
    row.addEventListener("click", () => {
      draft.shows.unshift({
        tmdb_id: r.tmdb_id, title: r.title, channels: [], weight: 1, name: null, links: {}, poster_url: r.poster_url,
      });
      expanded.add(r.tmdb_id);
      $("show-search").value = "";
      box.replaceChildren();
      renderShows();
      changed();
    });
    return row;
  }));
}

function renderShows() {
  const list = $("settings-shows");
  if (!draft.shows.length) {
    return list.replaceChildren(el("li", { className: "hint", textContent: "No shows yet. Search above to add one." }));
  }
  list.replaceChildren(...draft.shows.map(showRow));
}

function showRow(show) {
  const open = expanded.has(show.tmdb_id);
  const head = el("button", { className: "head", ariaExpanded: String(open) },
    show.poster_url ? el("img", { className: "poster", src: show.poster_url, alt: "" }) : el("div", { className: "poster" }),
    el("div", { className: "grow" },
      el("h3", { textContent: showTitle(show) }),
      show.channels.length
        ? el("p", { textContent: show.channels.join(" · ") + (show.weight !== 1 ? ` · weight ${show.weight}` : "") })
        : el("p", { className: "warn-text", textContent: "Pick at least one channel" })),
    el("span", { className: "tag", textContent: open ? "▲" : "▼" }));
  head.addEventListener("click", () => {
    open ? expanded.delete(show.tmdb_id) : expanded.add(show.tmdb_id);
    renderShows();
  });
  const li = el("li", { className: "show-item settings-show" }, head);
  if (open) li.append(showEditor(show));
  return li;
}

function showEditor(show) {
  const rerender = () => { renderShows(); changed(); };

  // Channels: current tags, then suggestions from other shows, then free text.
  const current = el("div", { className: "chips big" }, ...show.channels.map((ch) => {
    const b = el("button", { className: "chip-btn plain", textContent: `${ch} ✕` });
    b.addEventListener("click", () => { show.channels = show.channels.filter((c) => c !== ch); rerender(); });
    return b;
  }));
  const suggestions = allChannels().filter((c) => !show.channels.includes(c));
  const suggest = el("div", { className: "chips big" }, ...suggestions.map((ch) => {
    const b = el("button", { className: "chip-btn plain", textContent: `+ ${ch}` });
    b.addEventListener("click", () => { show.channels.push(ch); rerender(); });
    return b;
  }));
  const newCh = el("input", { className: "field", placeholder: "New channel, e.g. scifi (Enter)", enterKeyHint: "done" });
  newCh.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const v = newCh.value.trim();
    if (v && !show.channels.includes(v)) show.channels.push(v);
    rerender();
  });

  const weight = el("input", { className: "field", type: "number", min: "0.1", step: "0.5", value: show.weight });
  weight.addEventListener("input", () => {
    const w = parseFloat(weight.value);
    weight.classList.toggle("invalid", !(w > 0));
    if (w > 0) { show.weight = w; changed(); }
  });
  const name = el("input", { className: "field", placeholder: show.title || "", value: show.name || "" });
  name.addEventListener("input", () => { show.name = name.value.trim() || null; changed(); });

  const links = el("div", {}, ...Object.entries(show.links).map(([svc, url]) => kvRow(svc, url, (k, v) => {
    const entries = Object.entries(show.links).map(([a, b]) => (a === svc ? [k, v] : [a, b]));
    show.links = Object.fromEntries(entries.filter(([a]) => a !== null));
    svc = k;
    changed();
  }, () => { delete show.links[svc]; rerender(); }, draft.services)));
  const addLink = el("button", { className: "btn small", textContent: "Add deep link" });
  addLink.addEventListener("click", () => {
    const svc = draft.services.find((s) => !(s in show.links)) || "Service";
    show.links[svc] = "";
    rerender();
  });

  const remove = el("button", { className: "btn small danger", textContent: "Remove show" });
  remove.addEventListener("click", () => {
    if (!confirm(`Remove ${showTitle(show)}?`)) return;
    draft.shows = draft.shows.filter((s) => s !== show);
    rerender();
  });

  return el("div", { className: "editor" },
    el("div", {}, el("label", { textContent: "Channels" }), current, suggest, newCh),
    el("div", { className: "row2" },
      el("div", {}, el("label", { textContent: "Weight (1 = normal)" }), weight),
      el("div", {}, el("label", { textContent: "Display name" }), name)),
    el("div", {},
      el("label", { textContent: "Deep links (optional, used instead of search)" }), links, addLink),
    remove);
}

// A service/value pair editor row. onChange(key, value); onRemove().
function kvRow(key, value, onChange, onRemove, suggestions = []) {
  const listId = `dl-${Math.random().toString(36).slice(2)}`;
  const k = el("input", { className: "field", value: key, placeholder: "Service", autocomplete: "off" });
  k.setAttribute("list", listId);
  const v = el("input", { className: "field", value, placeholder: "https://…", type: "url", autocomplete: "off" });
  const update = () => onChange(k.value.trim(), v.value.trim());
  k.addEventListener("change", update);
  v.addEventListener("input", update);
  const x = el("button", { className: "btn small x", textContent: "✕", ariaLabel: "Remove" });
  x.addEventListener("click", onRemove);
  return el("div", { className: "kv-row" }, k, v, x,
    el("datalist", { id: listId }, ...suggestions.map((s) => el("option", { value: s }))));
}

// --- search URLs --------------------------------------------------------------

function renderSearchUrls() {
  const rows = Object.entries(draft.search_urls).map(([svc, tpl]) => {
    const row = kvRow(svc, tpl, (k, v) => {
      const entries = Object.entries(draft.search_urls).map(([a, b]) => (a === svc ? [k, v] : [a, b]));
      draft.search_urls = Object.fromEntries(entries);
      svc = k;
      changed();
    }, () => { delete draft.search_urls[svc]; renderSearchUrls(); changed(); }, draft.services);
    row.children[1].placeholder = "https://example.com/search?q={q}";
    row.children[1].type = "text";
    return row;
  });
  $("search-url-rows").replaceChildren(...rows);
}

// --- save ---------------------------------------------------------------------

function validate() {
  const missing = draft.shows.filter((s) => !s.channels.length);
  if (missing.length) {
    missing.forEach((s) => expanded.add(s.tmdb_id));
    renderShows();
    return `Give these shows a channel: ${missing.map(showTitle).join(", ")}`;
  }
  const emptyLinks = draft.shows.filter((s) => Object.entries(s.links).some(([k, v]) => !k || !v));
  if (emptyLinks.length) return `Fill in or remove the empty deep links on: ${emptyLinks.map(showTitle).join(", ")}`;
  if (Object.entries(draft.search_urls).some(([k, v]) => !k || !v.includes("{q}"))) {
    return "Each search link needs a service name and a URL containing {q}.";
  }
  return null;
}

async function save() {
  const problem = validate();
  if (problem) return showNotice($("settings-error"), problem, true);
  showNotice($("settings-error"), "");
  $("save").disabled = true;
  $("save-status").textContent = "Saving…";
  try {
    const res = await api("api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version: draft.version, ...stripped(draft) }),
    });
    draft.version = res.version;
    savedJson = JSON.stringify(stripped(draft));
    changed();
    showNotice($("settings-error"), "Saved. New shows will appear once their episodes are fetched (a few seconds each).");
  } catch (e) {
    $("save-status").textContent = "Unsaved changes";
    showNotice($("settings-error"), `Save failed: ${e.message}`, true);
  } finally {
    $("save").disabled = false;
  }
}

// --- account & users ------------------------------------------------------------

async function changePassword(e) {
  e.preventDefault();
  try {
    await api("api/auth/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current_password: $("pw-current").value, new_password: $("pw-new").value }),
    });
    $("password-form").reset();
    showNotice($("settings-error"), "Password changed. Other devices have been logged out.");
  } catch (err) {
    showNotice($("settings-error"), `Couldn't change password: ${err.message}`, true);
  }
}

async function logout() {
  if (!settingsCanLeave()) return;
  draft = null;
  await api("api/auth/logout", { method: "POST" }).catch(() => {});
  showLogin(false);
}

async function loadUsers() {
  try {
    const { users } = await api("api/users");
    $("user-list").replaceChildren(...users.map(userRow));
  } catch (e) {
    $("user-list").replaceChildren(el("li", { className: "hint", textContent: e.message }));
  }
}

async function userAction(fn, okMsg) {
  try {
    await fn();
    if (okMsg) showNotice($("settings-error"), okMsg);
  } catch (e) {
    showNotice($("settings-error"), e.message, true);
  }
  loadUsers();
}

const jsonReq = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function userRow(u) {
  const self = u.id === me.id;
  const role = el("button", { className: "btn small", textContent: u.is_admin ? "Make viewer" : "Make admin" });
  role.addEventListener("click", () => userAction(
    () => api(`api/users/${u.id}`, jsonReq("PATCH", { is_admin: !u.is_admin })),
    `${u.username} is now ${u.is_admin ? "a viewer" : "an admin"}.`,
  ).then(() => { if (self) location.reload(); }));
  const reset = el("button", { className: "btn small", textContent: "Reset password" });
  reset.addEventListener("click", () => {
    const pw = prompt(`New password for ${u.username} (8+ characters):`);
    if (pw) userAction(() => api(`api/users/${u.id}`, jsonReq("PATCH", { password: pw })), `Password for ${u.username} changed.`);
  });
  const del = el("button", { className: "btn small danger", textContent: "Delete" });
  del.addEventListener("click", () => {
    if (confirm(`Delete ${u.username} and their watch history?`)) {
      userAction(() => api(`api/users/${u.id}`, { method: "DELETE" }), `Deleted ${u.username}.`);
    }
  });
  return el("li", {},
    el("span", { className: "grow" }, u.username, " ", el("small", { textContent: (u.is_admin ? "admin" : "viewer") + (self ? " · you" : "") })),
    role, self ? null : reset, self ? null : del);
}

async function addUser(e) {
  e.preventDefault();
  const username = $("new-user").value.trim();
  await userAction(async () => {
    await api("api/users", jsonReq("POST", {
      username, password: $("new-pass").value, is_admin: $("new-admin").checked,
    }));
    $("add-user-form").reset();
  }, `Added ${username}.`);
}

// --- wiring -----------------------------------------------------------------

$("settings-btn").addEventListener("click", () => settingsCanLeave() && openSettings());
$("provider-filter").addEventListener("input", renderProviderList);
$("provider-filter").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  const first = $("provider-list").querySelector(".pick-row");
  if (first) first.click();
  else addService($("provider-filter").value.trim()); // manual entry
});
$("include-free").addEventListener("change", (e) => { draft.include_free = e.target.checked; changed(); });
$("include-rent-buy").addEventListener("change", (e) => { draft.include_rent_buy = e.target.checked; changed(); });
$("show-search").addEventListener("input", onSearchInput);
$("cooldown-days").addEventListener("input", (e) => {
  const d = parseFloat(e.target.value);
  e.target.classList.toggle("invalid", !(d >= 0));
  if (d >= 0) { draft.cooldown_days = d; changed(); }
});
$("clear-history").addEventListener("click", async () => {
  if (!confirm("Forget every episode you've marked watched or skipped?")) return;
  try {
    const { deleted } = await api("api/history", { method: "DELETE" });
    showNotice($("settings-error"), `Cleared ${deleted} history entr${deleted === 1 ? "y" : "ies"}.`);
  } catch (e) {
    showNotice($("settings-error"), `Couldn't clear history: ${e.message}`, true);
  }
});
$("add-search-url").addEventListener("click", () => {
  draft.search_urls[draft.services.find((s) => !(s in draft.search_urls)) || ""] = "";
  renderSearchUrls();
  changed();
});
$("save").addEventListener("click", save);
$("password-form").addEventListener("submit", changePassword);
$("logout").addEventListener("click", logout);
$("add-user-form").addEventListener("submit", addUser);
$("discard").addEventListener("click", () => {
  if (confirm("Discard unsaved changes?")) openSettings();
});
window.addEventListener("beforeunload", (e) => {
  if (isDirty()) e.preventDefault();
});
