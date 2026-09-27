"use strict";

const $ = (id) => document.getElementById(id);

let adbEnabled = false;
let currentChannel = null;
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
  for (const v of ["channels", "pick", "shows"]) $(`${v}-view`).hidden = v !== name;
  $("back").hidden = name === "channels";
  $("shows-btn").hidden = name === "shows";
}

// --- channels ---------------------------------------------------------------

async function loadChannels() {
  try {
    const data = await api("api/channels");
    adbEnabled = data.adb_enabled;
    $("channels").replaceChildren(...data.channels.map(channelButton));
    showNotice($("notice"), data.sync_error ? `Sync issue: ${data.sync_error}` : "", true);
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
  btn.addEventListener("click", () => pick(ch.name));
  return btn;
}

// --- pick -------------------------------------------------------------------

async function pick(channel) {
  currentChannel = channel;
  showView("pick");
  showNotice($("play-status"), "");
  document.body.classList.add("loading");
  $("reroll").disabled = true;
  try {
    render(await api(`api/pick?channel=${encodeURIComponent(channel)}`));
  } catch (e) {
    render(null, e.message);
  } finally {
    document.body.classList.remove("loading");
    $("reroll").disabled = false;
  }
}

function render(ep, error) {
  current = ep;
  $("p-channel").textContent = currentChannel;
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

$("reroll").addEventListener("click", () => currentChannel && pick(currentChannel));
$("play").addEventListener("click", playOnTv);
$("shows-btn").addEventListener("click", loadShows);
$("back").addEventListener("click", () => {
  showView("channels");
  loadChannels();
});

showView("channels");
loadChannels();
