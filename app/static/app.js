"use strict";

const $ = (id) => document.getElementById(id);

let adbEnabled = false;
let currentChannel = null;
// Per channel visit: shows you've skipped and episodes you've already been shown.
let skippedShows = new Set();
let seenEpisodes = [];
let current = null;
let me = null; // { id, username, is_admin }

async function api(path, opts) {
  const res = await fetch(path, opts);
  const body = await res.json().catch(() => ({}));
  if (res.status === 401 && !path.startsWith("api/auth/")) {
    showLogin(false); // session expired or logged out elsewhere
  }
  if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
  return body;
}

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children.filter((c) => c != null));
  return node;
}

function showNotice(node, msg, isError = false) {
  node.textContent = msg;
  node.classList.toggle("error", isError);
  node.hidden = !msg;
}

let currentView = null;
let viewBeforeCredits = null;

function showView(name) {
  currentView = name;
  for (const v of ["login", "channels", "pick", "shows", "settings", "credits"]) $(`${v}-view`).hidden = v !== name;
  $("back").hidden = name === "channels" || name === "login";
  $("shows-btn").hidden = ["shows", "settings", "login", "credits"].includes(name) || !me;
  $("settings-btn").hidden = name === "settings" || name === "login" || !me;
  updateSaveBar();
}

function openCredits(e) {
  e.preventDefault();
  if (currentView === "credits") return;
  if (EDIT_VIEWS.includes(currentView) && !settingsCanLeave()) return;
  viewBeforeCredits = currentView;
  showView("credits");
  window.scrollTo(0, 0);
}

// --- footer: version, and (for admins) whether a newer one is out -------------

const REPO_URL = "https://github.com/JamesHeadrick/DeadAir-TV";

async function loadVersion() {
  try {
    const v = await api("api/version");
    const parts = [`DeadAir ${v.version}`];
    if (v.commit) {
      parts.push(el("a", { href: `${REPO_URL}/commit/${v.commit_full}`, target: "_blank", rel: "noopener", textContent: v.commit }));
    }
    $("build-info").replaceChildren(...parts.flatMap((p, i) => (i ? [" · ", p] : [p])));
    $("build-info").title = v.built ? `Built ${v.built.slice(0, 10)}` : "";
  } catch {
    $("build-info").replaceChildren();
  }
}

async function checkForUpdate() {
  const node = $("update-info");
  node.hidden = true;
  if (!me?.is_admin) return;
  try {
    const { update } = await api("api/updates");
    if (!update?.available) return;
    const text = update.behind != null
      ? `${update.behind} new commit${update.behind === 1 ? "" : "s"} on main`
      : `Update available: ${update.latest}`;
    node.replaceChildren(el("a", { href: update.url, target: "_blank", rel: "noopener", textContent: `⬆ ${text}` }));
    node.hidden = false;
  } catch { /* no notice if GitHub or the check is unavailable */ }
}

// --- login ------------------------------------------------------------------

let setupMode = false;

function showLogin(needsSetup) {
  setupMode = needsSetup;
  me = null;
  $("update-info").hidden = true;
  showView("login");
  $("login-title").textContent = needsSetup ? "Welcome! Create the admin account" : "Log in";
  $("login-hint").textContent = needsSetup
    ? "This account can change settings and add other users."
    : "";
  $("login-hint").hidden = !needsSetup;
  $("login-confirm-wrap").hidden = !needsSetup;
  $("login-pass2").required = needsSetup;
  $("login-pass").autocomplete = needsSetup ? "new-password" : "current-password";
  $("login-submit").textContent = needsSetup ? "Create account" : "Log in";
  showNotice($("login-error"), "");
  $("login-user").focus();
}

function setMe(user) {
  me = user;
  document.body.classList.toggle("viewer", !user.is_admin);
  checkForUpdate();
}

async function submitLogin(e) {
  e.preventDefault();
  const username = $("login-user").value.trim();
  const password = $("login-pass").value;
  if (setupMode && password !== $("login-pass2").value) {
    return showNotice($("login-error"), "Passwords don't match.", true);
  }
  $("login-submit").disabled = true;
  try {
    const { user } = await api(setupMode ? "api/auth/setup" : "api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    $("login-form").reset();
    setMe(user);
    showView("channels");
    loadChannels();
  } catch (err) {
    showNotice($("login-error"), err.message, true);
  } finally {
    $("login-submit").disabled = false;
  }
}

async function boot() {
  loadVersion();
  try {
    const status = await api("api/auth/status");
    if (!status.user) return showLogin(status.needs_setup);
    setMe(status.user);
  } catch (e) {
    showView("channels");
    return showNotice($("notice"), `Couldn't reach the server: ${e.message}`, true);
  }
  showView("channels");
  loadChannels();
}

// --- channels ---------------------------------------------------------------

async function loadChannels() {
  try {
    const data = await api("api/channels");
    adbEnabled = data.adb_enabled;
    $("channels").replaceChildren(...data.channels.map(channelButton));
    if (!data.channels.length) {
      $("channels").replaceChildren(el("p", {
        className: "notice",
        textContent: me?.is_admin ? "No channels yet. Add some shows under All shows." : "No channels yet. Ask an admin to add some shows.",
      }));
    }
    const problems = [
      data.config_error && `config.yaml has an error, using the last good version: ${data.config_error}`,
      data.sync_error && `Sync issue: ${data.sync_error}`,
    ].filter(Boolean);
    showNotice($("notice"), problems.join(" · "), true);
  } catch (e) {
    showNotice($("notice"), `Couldn't load channels: ${e.message}`, true);
  }
}

function channelButton(ch) {
  const text = el("span", { className: "text" },
    el("span", { className: "name" },
      ch.emoji ? el("span", { className: "emoji", textContent: ch.emoji, ariaHidden: "true" }) : null,
      el("span", { textContent: ch.name })),
    el("span", { className: "meta", textContent: ch.shows.join(" · ") }),
  );
  if (ch.unwatchable.length) {
    text.append(el("span", {
      className: "warn",
      textContent: `⚠ ${ch.unwatchable.length} not on your services`,
      title: `Skipped when picking: ${ch.unwatchable.join(", ")}`,
    }));
  }
  const posters = el("span", { className: "posters", ariaHidden: "true" },
    ...ch.posters.map((src) => {
      const img = el("img", { src, alt: "", loading: "lazy" });
      img.addEventListener("error", () => img.remove()); // no broken-image icons if TMDB's CDN hiccups
      return img;
    }));
  const btn = el("button", { className: "channel-btn" }, text, ch.posters.length ? posters : null);
  btn.addEventListener("click", () => {
    current = null;
    enterChannel(ch.name);
  });
  return btn;
}

// --- pick -------------------------------------------------------------------

function enterChannel(channel) {
  currentChannel = channel;
  skippedShows = new Set();
  seenEpisodes = [];
  pick();
}

// mode: "any" (full reroll), "other-show" (skip this show), "same-show" (another episode of it)
async function pick(mode = "any") {
  const params = new URLSearchParams({ channel: currentChannel });
  if (current && mode === "other-show") skippedShows.add(current.tmdb_id);
  if (current && mode === "same-show") params.set("show", current.tmdb_id);
  skippedShows.forEach((id) => params.append("skip_show", id));
  seenEpisodes.forEach((k) => params.append("skip_ep", k));

  showView("pick");
  showNotice($("play-status"), "");
  document.body.classList.add("loading");
  const buttons = ["reroll", "other-show", "same-show", "skip", "watched"].map($);
  buttons.forEach((b) => (b.disabled = true));
  try {
    const ep = await api(`api/pick?${params}`);
    seenEpisodes.push(`${ep.tmdb_id}:${ep.season}:${ep.episode}`);
    seenEpisodes = seenEpisodes.slice(-200);
    render(ep);
  } catch (e) {
    render(null, e.message);
  } finally {
    document.body.classList.remove("loading");
    buttons.forEach((b) => (b.disabled = false));
  }
}

function render(ep, error) {
  current = ep;
  renderChannelLabel(ep);
  $("other-show").hidden = !ep || ep.other_shows === 0;
  $("same-show").hidden = !ep;
  $("watched").hidden = !ep;
  $("skip").hidden = !ep;
  renderHistory(ep);
  const still = $("still");
  if (!ep) {
    $("p-show").textContent = "Nothing to show";
    $("p-code").textContent = "";
    $("p-title").textContent = "";
    $("p-overview").textContent = error || "";
    $("p-tier").textContent = "";
    $("watch-options").replaceChildren();
    still.removeAttribute("src");
    $("play").hidden = true;
    return;
  }
  $("p-show").textContent = ep.show_name;
  $("p-code").textContent = ep.code;
  $("p-title").textContent = ep.title;
  $("p-overview").textContent = ep.overview || "No synopsis available.";
  if (ep.still_url) {
    still.src = ep.still_url;
    still.alt = `${ep.show_name} ${ep.code}`;
  } else {
    still.removeAttribute("src");
  }

  const access = ep.access;
  const tier = $("p-tier");
  tier.className = "tier" + (access.tier === "rent_buy" ? " rent" : "");
  tier.replaceChildren(el("strong", { textContent: access.checked ? access.tier_label : "Checking where to watch…" }));

  const opts = access.options.map((o, i) => {
    const a = el("a", {
      className: i === 0 ? "btn primary" : "btn secondary-option",
      href: o.url,
      target: "_blank",
      rel: "noopener",
    });
    if (o.logo_url) a.append(el("img", { className: "logo", src: o.logo_url, alt: "" }));
    a.append(el("span", { textContent: watchLabel(o) }));
    return a;
  });
  $("watch-options").replaceChildren(...opts);
  $("play").hidden = !adbEnabled || !access.options.length;
}

// Say what the button will do: show pages open the series, fallbacks only search.
function watchLabel(o) {
  const name = o.provider_name;
  if (o.source === "manual" || o.source === "auto") return `Open series in ${name}`;
  if (o.source === "tmdb") return `Find ${name} on TMDB`;
  return `Search in ${name}`;
}

// "COMEDY · scifi · short": the channel you picked from, then the show's other channels, dimmed.
function renderChannelLabel(ep) {
  const others = (ep?.channels || [])
    .filter((c) => c !== currentChannel)
    .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
  const emoji = ep?.channel_emoji?.[currentChannel];
  $("p-channel").replaceChildren(
    emoji ? el("span", { className: "emoji", textContent: emoji, ariaHidden: "true" }) : "",
    el("span", { textContent: currentChannel }),
    ...others.map((c) => {
      const link = el("button", {
        className: "other", textContent: c, title: `Roll on ${c} instead`,
        ariaLabel: `${ep.show_name} is also on ${c}. Roll on ${c}`,
      });
      link.addEventListener("click", () => enterChannel(c)); // a fresh visit to that channel
      return link;
    }),
  );
}

function ago(ts) {
  const days = Math.floor((Date.now() / 1000 - ts) / 86400);
  if (days < 1) return "today";
  if (days === 1) return "yesterday";
  if (days < 14) return `${days} days ago`;
  if (days < 60) return `${Math.round(days / 7)} weeks ago`;
  if (days < 730) return `${Math.round(days / 30)} months ago`;
  return `${Math.round(days / 365)} years ago`;
}

function renderHistory(ep) {
  const h = ep?.history;
  const watchedNow = !!(h && h.kind === "watched" && h.cooling_down);
  const btn = $("watched");
  btn.setAttribute("aria-pressed", String(watchedNow));
  btn.textContent = watchedNow ? "✓ Watched" : "Mark watched";
  btn.title = watchedNow ? "Tap to undo" : "Mark as watched: it won't come up again for a while";
  const note = $("p-history");
  // Only mention history from before this pick; a mark you just made is shown by the button.
  if (watchedNow) {
    note.textContent = "Marked watched. It'll sit out the cooldown (tap ✓ Watched to undo).";
    note.hidden = false;
  } else if (h && !(h.kind === "skipped" && h.cooling_down)) {
    note.textContent = `You ${h.kind === "watched" ? "watched" : "skipped"} this ${ago(h.at)}.`;
    note.hidden = false;
  } else {
    note.hidden = true;
  }
}

const epRef = (ep) => ({ tmdb_id: ep.tmdb_id, season: ep.season, episode: ep.episode });

async function toggleWatched() {
  if (!current) return;
  const btn = $("watched");
  btn.disabled = true;
  const undo = btn.getAttribute("aria-pressed") === "true";
  try {
    const res = await api(undo ? "api/history/unwatch" : "api/history", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(undo ? epRef(current) : { ...epRef(current), kind: "watched" }),
    });
    current.history = res.history;
    renderHistory(current);
  } catch (e) {
    showNotice($("play-status"), `Couldn't save: ${e.message}`, true);
  } finally {
    btn.disabled = false;
  }
}

async function skipEpisode() {
  if (!current) return;
  try {
    await api("api/history", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...epRef(current), kind: "skipped" }),
    });
  } catch (e) {
    return showNotice($("play-status"), `Couldn't save: ${e.message}`, true);
  }
  pick("any");
}

async function playOnTv() {
  if (!current) return;
  const btn = $("play");
  btn.disabled = true;
  showNotice($("play-status"), "Sending to TV…");
  try {
    await api("api/play", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tmdb_id: current.tmdb_id }),
    });
    showNotice($("play-status"), "Launched on TV.");
  } catch (e) {
    showNotice($("play-status"), `TV: ${e.message}`, true);
  } finally {
    btn.disabled = false;
  }
}

// --- all shows --------------------------------------------------------------

async function loadShows() {
  showView("shows");
  showNotice($("shows-error"), "");
  const list = $("show-list");
  list.replaceChildren(el("li", { className: "notice", textContent: "Loading…" }));
  try {
    const [{ shows }, { services }] = await Promise.all([api("api/shows"), api("api/channels")]);
    $("my-services").textContent = services.length
      ? `Your services: ${services.join(", ")}`
      : "No services set up yet (Settings → Your services)";
    if (me.is_admin) await openShowsEditor(shows); // settings.js: same list, editable
    else list.replaceChildren(...shows.map(showItem));
  } catch (e) {
    list.replaceChildren(el("li", { className: "notice error", textContent: e.message }));
  }
}

// Where-to-watch lines for a show: tier and channels, provider chips, other services.
function accessBits(a, channels) {
  const chips = el("div", { className: "chips" });
  if (!a.checked) {
    chips.append(el("span", { className: "chip", textContent: "not checked yet" }));
  } else if (!a.tier) {
    chips.append(el("span", { className: "chip meh", textContent: "not on your services · skipped" }));
  } else {
    const cls = a.tier === "subscription" ? "good" : "meh";
    for (const o of a.options) chips.append(el("span", { className: `chip ${cls}`, textContent: o.provider_name }));
  }
  const tier = channels ? `${a.tier_label} · ${channels.join(", ")}` : a.tier_label;
  const lines = [el("p", { textContent: tier })];
  if (a.other_subscriptions.length) {
    lines.push(el("p", { textContent: `Also on: ${a.other_subscriptions.join(", ")}` }));
  }
  return [...lines, chips];
}

function showItem(s) {
  return el("li", { className: "show-item" },
    s.poster_url ? el("img", { className: "poster", src: s.poster_url, alt: "" }) : el("div", { className: "poster" }),
    el("div", {}, el("h3", { textContent: s.show_name }), ...accessBits(s.access, s.channels)),
  );
}

// --- wiring -----------------------------------------------------------------

$("reroll").addEventListener("click", () => pick("any"));
$("watched").addEventListener("click", toggleWatched);
$("skip").addEventListener("click", skipEpisode);
$("other-show").addEventListener("click", () => pick("other-show"));
$("same-show").addEventListener("click", () => pick("same-show"));
$("play").addEventListener("click", playOnTv);
$("shows-btn").addEventListener("click", loadShows);
$("back").addEventListener("click", () => {
  if (currentView === "credits") {
    // Return to where Credits was opened from (e.g. the login screen or a pick).
    const back = me ? viewBeforeCredits : "login";
    if (back === "login" || !me) return showLogin(setupMode);
    if (back === "shows") return loadShows();
    if (back && back !== "settings") return showView(back);
  }
  if (!settingsCanLeave()) return;
  showView("channels");
  loadChannels();
});
$("credits-link").addEventListener("click", openCredits);

$("login-form").addEventListener("submit", submitLogin);

// After settings.js has loaded too: views use its save bar and editors.
document.addEventListener("DOMContentLoaded", boot);
