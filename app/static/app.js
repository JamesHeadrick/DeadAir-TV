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

function showNotice(el, msg, isError = false) {
  el.textContent = msg;
  el.classList.toggle("error", isError);
  el.hidden = !msg;
}

function showView(name) {
  $("channels-view").hidden = name !== "channels";
  $("pick-view").hidden = name !== "pick";
  $("back").hidden = name !== "pick";
}

async function loadChannels() {
  const grid = $("channels");
  try {
    const data = await api("api/channels");
    adbEnabled = data.adb_enabled;
    grid.replaceChildren(...data.channels.map(channelButton));
    showNotice($("notice"), data.sync_error ? `Sync issue: ${data.sync_error}` : "", true);
  } catch (e) {
    showNotice($("notice"), `Couldn't load channels: ${e.message}`, true);
  }
}

function channelButton(ch) {
  const btn = document.createElement("button");
  btn.className = "channel-btn";
  const name = document.createElement("span");
  name.className = "name";
  name.textContent = ch.name;
  const meta = document.createElement("span");
  meta.className = "meta";
  meta.textContent = ch.shows.map((s) => s.show_name).join(" · ");
  btn.append(name, meta);

  const gone = ch.shows.filter((s) => s.available === false);
  if (gone.length) {
    const warn = document.createElement("span");
    warn.className = "warn";
    warn.textContent = `⚠ ${gone.length} moved`;
    warn.title = gone.map((s) => `${s.show_name} is no longer on ${s.service}`).join("\n");
    btn.append(warn);
  }
  btn.addEventListener("click", () => pick(ch.name));
  return btn;
}

async function pick(channel) {
  currentChannel = channel;
  showView("pick");
  showNotice($("play-status"), "");
  document.body.classList.add("loading");
  $("reroll").disabled = true;
  try {
    render(await api(`api/pick?channel=${encodeURIComponent(channel)}`));
  } catch (e) {
    current = null;
    render(null, e.message);
  } finally {
    document.body.classList.remove("loading");
    $("reroll").disabled = false;
  }
}

function render(ep, error) {
  current = ep;
  $("p-channel").textContent = currentChannel;
  if (!ep) {
    $("p-show").textContent = "Nothing to show";
    $("p-code").textContent = "";
    $("p-title").textContent = "";
    $("p-overview").textContent = error || "";
    $("p-flag").hidden = true;
    $("still").removeAttribute("src");
    $("open").hidden = true;
    $("play").hidden = true;
    return;
  }
  $("p-show").textContent = ep.show_name;
  $("p-code").textContent = ep.code;
  $("p-title").textContent = ep.title;
  $("p-overview").textContent = ep.overview || "No synopsis available.";
  const still = $("still");
  if (ep.still_url) {
    still.src = ep.still_url;
    still.alt = `${ep.show_name} ${ep.code}`;
  } else {
    still.removeAttribute("src");
  }

  const flag = $("p-flag");
  if (ep.available === false) {
    const now = ep.providers.length ? ` Now on: ${ep.providers.join(", ")}.` : "";
    flag.textContent = `No longer on ${ep.service}.${now}`;
    flag.hidden = false;
  } else {
    flag.hidden = true;
  }

  const open = $("open");
  open.href = ep.show_url;
  open.textContent = `Open in ${ep.service}`;
  open.hidden = false;
  $("play").hidden = !adbEnabled;
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
      body: JSON.stringify({ channel: currentChannel, tmdb_id: current.tmdb_id }),
    });
    showNotice($("play-status"), "Launched on TV.");
  } catch (e) {
    showNotice($("play-status"), `TV: ${e.message}`, true);
  } finally {
    btn.disabled = false;
  }
}

$("reroll").addEventListener("click", () => currentChannel && pick(currentChannel));
$("play").addEventListener("click", playOnTv);
$("back").addEventListener("click", () => {
  showView("channels");
  loadChannels();
});

showView("channels");
loadChannels();
