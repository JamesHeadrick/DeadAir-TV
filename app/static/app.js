"use strict";

const $ = (id) => document.getElementById(id);

let adbEnabled = false;
let currentChannel = null;
// Per channel visit: shows you've skipped and episodes you've already been shown.
let skippedShows = new Set();
let seenEpisodes = [];
let current = null;

async function api(path, opts) {
  const res = await fetch(path, opts);
  const body = await res.json().catch(() => ({}));
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

function showView(name) {
  for (const v of ["channels", "pick", "shows", "settings"]) $(`${v}-view`).hidden = v !== name;
  $("back").hidden = name === "channels";
  $("shows-btn").hidden = name === "shows" || name === "settings";
  $("settings-btn").hidden = name === "settings";
}

// --- channels ---------------------------------------------------------------

async function loadChannels() {
  try {
    const data = await api("api/channels");
    adbEnabled = data.adb_enabled;
    $("channels").replaceChildren(...data.channels.map(channelButton));
    if (!data.channels.length) {
      $("channels").replaceChildren(el("p", { className: "notice", textContent: "No channels yet. Add some shows in Settings (⚙)." }));
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
  const btn = el("button", { className: "channel-btn" },
    el("span", { className: "name", textContent: ch.name }),
    el("span", { className: "meta", textContent: ch.shows.join(" · ") }),
  );
  if (ch.unwatchable.length) {
    btn.append(el("span", {
      className: "warn",
      textContent: `⚠ ${ch.unwatchable.length} unavailable`,
      title: `Not on your services, skipped: ${ch.unwatchable.join(", ")}`,
    }));
  }
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
  $("p-channel").textContent = currentChannel;
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
    a.append(i === 0 ? `Open in ${o.provider_name}` : o.provider_name);
    return a;
  });
  $("watch-options").replaceChildren(...opts);
  $("play").hidden = !adbEnabled || !access.options.length;
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
  const list = $("show-list");
  list.replaceChildren(el("li", { className: "notice", textContent: "Loading…" }));
  try {
    const [{ shows }, { services }] = await Promise.all([api("api/shows"), api("api/channels")]);
    $("my-services").textContent = services.length
      ? `Your services: ${services.join(", ")}`
      : "No services listed in config.yaml";
    list.replaceChildren(...shows.map(showItem));
  } catch (e) {
    list.replaceChildren(el("li", { className: "notice error", textContent: e.message }));
  }
}

function showItem(s) {
  const a = s.access;
  const chips = el("div", { className: "chips" });
  if (!a.checked) {
    chips.append(el("span", { className: "chip", textContent: "not checked yet" }));
  } else if (!a.tier) {
    chips.append(el("span", { className: "chip meh", textContent: "not on your services · skipped" }));
  } else {
    const cls = a.tier === "subscription" ? "good" : "meh";
    for (const o of a.options) chips.append(el("span", { className: `chip ${cls}`, textContent: o.provider_name }));
  }
  const lines = [el("p", { textContent: `${a.tier_label} · ${s.channels.join(", ")}` })];
  if (a.other_subscriptions.length) {
    lines.push(el("p", { textContent: `Also on: ${a.other_subscriptions.join(", ")}` }));
  }
  return el("li", { className: "show-item" },
    s.poster_url ? el("img", { className: "poster", src: s.poster_url, alt: "" }) : el("div", { className: "poster" }),
    el("div", {}, el("h3", { textContent: s.show_name }), ...lines, chips),
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
  if (!settingsCanLeave()) return;
  showView("channels");
  loadChannels();
});

showView("channels");
loadChannels();
