/* Gizmo UI.
   One router, seven views, no framework. Every number on the page comes from
   the service's own API — the UI holds no state it cannot rebuild from /api. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = { view: "home", libraryKind: "movie", live: null, results: [] };

/* --------------------------------------------------------------------- utils */

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (response.status === 401) {
    throw new Error("Not signed in — reload and sign in again.");
  }
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try { detail = (await response.json()).detail || detail; } catch { /* not JSON */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function toast(message, kind = "") {
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  node.textContent = message;
  $("#toasts").append(node);
  setTimeout(() => node.remove(), 4200);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

const bytes = (n) => {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i > 2 ? 1 : 0)} ${units[i]}`;
};

const eta = (ms) => {
  if (!ms || ms < 0 || ms > 86400 * 7) return "—";
  const h = Math.floor(ms / 3600);
  const m = Math.floor((ms % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
};

function initials(text) {
  return String(text || "?").trim().slice(0, 1).toUpperCase();
}

function ratingPill(quality) {
  return quality && quality !== "unknown"
    ? `<span class="pill brand">${escapeHtml(quality)}</span>`
    : `<span class="pill">quality unknown</span>`;
}

/* --------------------------------------------------------------- chrome */

function setActive(view) {
  $$("[data-view]").forEach((link) => {
    if (link.dataset.view === view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
}

function loading(on) {
  $("#loading").hidden = !on;
}

async function refreshHealth() {
  try {
    const health = await api("/api/health");
    $("#health-dot").className = "dot ok";
    $("#health-text").textContent =
      `${health.stats.items} items · ${health.stats.wanted} wanted`;
  } catch {
    $("#health-dot").className = "dot bad";
    $("#health-text").textContent = "service unreachable";
  }
}

/* ------------------------------------------------------------------- views */

const views = {};

views.home = async () => {
  const [stats, recent, downloads] = await Promise.all([
    api("/api/stats"),
    api("/api/library?limit=12"),
    api("/api/downloads").catch(() => ({ status: {}, torrents: [] })),
  ]);

  const active = downloads.torrents.filter((t) => !t.complete).slice(0, 4);
  return `
    <div class="view-head">
      <div>
        <p class="eyebrow">Gizmo</p>
        <h1>Everything, in one place.</h1>
        <p>Library, requests, the download queue and live TV on one page — the eight hostnames this platform used to answer on all land here now.</p>
      </div>
      <div class="spacer"></div>
      <button class="btn" id="scan-now" type="button">Rescan library</button>
    </div>

    <div class="grid wide" style="margin-bottom:22px">
      <div class="card stat"><span>In the library</span><b>${stats.items}</b>
        <p>${stats.movies} films · ${stats.episodes} episodes · ${stats.tracks} tracks · ${bytes(stats.bytes)}</p></div>
      <div class="card stat"><span>Waiting on</span><b>${stats.wanted}</b>
        <p>${stats.grabbed} downloading · ${stats.imported} imported · ${stats.failed} failed</p></div>
      <div class="card stat"><span>Downloader</span><b>${downloads.status?.reachable ? bytes(downloads.status.rate) + "/s" : "offline"}</b>
        <p>${downloads.status?.count ?? 0} torrents · ${downloads.status?.downloading ?? 0} active</p></div>
      <div class="card stat"><span>Indexers</span><b>${stats.indexers}</b>
        <p>${stats.livetv ? "live TV configured" : "no live TV playlist yet"}</p></div>
    </div>

    ${active.length ? `
      <h2 style="margin:6px 0 14px;font-size:19px">Downloading now</h2>
      <div class="table-wrap" style="margin-bottom:24px"><table>
        <thead><tr><th>Release</th><th>Progress</th><th>Rate</th><th>Left</th></tr></thead>
        <tbody>${active.map((t) => `
          <tr>
            <td>${escapeHtml(t.name)}</td>
            <td><div class="progress"><i style="width:${t.percent}%"></i></div></td>
            <td>${bytes(t.rate)}/s</td>
            <td>${eta(t.eta)}</td>
          </tr>`).join("")}</tbody>
      </table></div>` : ""}

    <h2 style="margin:6px 0 14px;font-size:19px">Recently added</h2>
    ${recent.items.length ? `<div class="grid">
      ${recent.items.map((item) => `
        <article class="card">
          <div class="poster">${initials(item.title)}</div>
          <h3>${escapeHtml(item.title)}</h3>
          <p>${item.year ? item.year + " · " : ""}${item.kind === "episode" && item.season != null
            ? `S${String(item.season).padStart(2, "0")}E${String(item.episode ?? 0).padStart(2, "0")} · ` : ""}${bytes(item.size)}</p>
          <div class="meta">${ratingPill(item.quality)}</div>
        </article>`).join("")}
    </div>` : `<div class="empty">The library is empty. Add an indexer, then request something — or hit rescan if the files are already on disk.</div>`}
  `;
};

views.library = async () => {
  const data = await api(`/api/library?kind=${state.libraryKind}&limit=120`);
  const label = { movie: "Films", episode: "Episodes", track: "Music" }[state.libraryKind];
  return `
    <div class="view-head">
      <div><h1>Library</h1><p>${data.total} ${label.toLowerCase()} catalogued, each row deduplicated on title, year, episode and quality.</p></div>
      <div class="spacer"></div>
      <div class="segmented" id="kind-filter" role="group" aria-label="Filter by kind">
        ${["movie", "episode", "track"].map((kind) => `
          <button type="button" data-kind="${kind}" aria-pressed="${state.libraryKind === kind}">
            ${{ movie: "Films", episode: "TV", track: "Music" }[kind]}
          </button>`).join("")}
      </div>
    </div>
    ${data.items.length ? `<div class="grid">
      ${data.items.map((item) => `
        <article class="card">
          <div class="poster">${initials(item.title)}</div>
          <h3>${escapeHtml(item.title)}</h3>
          <p>${item.year ? item.year + " · " : ""}${bytes(item.size)}</p>
          <div class="meta">${ratingPill(item.quality)}</div>
        </article>`).join("")}
    </div>` : `<div class="empty">Nothing catalogued under ${label.toLowerCase()} yet.</div>`}
  `;
};

views.search = async (params) => {
  const query = params.get("q") || "";
  if (!query) {
    return `
      <div class="view-head"><div><h1>Search</h1>
        <p>Type above to search every indexer at once. Results are scored against your profile — the number is the reason, not a black box.</p></div></div>
      <div class="empty">Try a title, for example <b>Dune</b>. The indexers are queried live; nothing is downloaded until you ask.</div>`;
  }
  const kind = params.get("kind") || "movie";
  const data = await api(`/api/search?q=${encodeURIComponent(query)}&kind=${kind}`);
  if (data.reason) return `<div class="error">${escapeHtml(data.reason)}</div>`;
  state.results = data.results;
  return `
    <div class="view-head"><div><h1>Results for “${escapeHtml(query)}”</h1>
      <p>${data.results.length} unique releases after cross-indexer dedupe${data.current_quality ? ` · already held at ${escapeHtml(data.current_quality)}` : ""}.</p></div>
      <div class="spacer"></div>
      <div class="segmented" id="kind-filter" role="group" aria-label="Media kind">
        ${["movie", "episode", "track"].map((k) => `<button type="button" data-kind="${k}" aria-pressed="${kind === k}">${{ movie: "Film", episode: "TV", track: "Music" }[k]}</button>`).join("")}
      </div>
    </div>
    ${data.results.length ? `<div class="table-wrap"><table>
      <thead><tr><th>Release</th><th>Score</th><th>Seeds</th><th>Size</th><th></th></tr></thead>
      <tbody>${data.results.map((row, index) => `
        <tr>
          <td>
            <div style="font-weight:600">${escapeHtml(row.title)}</div>
            <div style="color:var(--muted);font-size:12.5px">${escapeHtml(row.indexer)} · ${row.verdict.reasons.slice(0, 3).join(" · ") || "no notes"}</div>
          </td>
          <td>${row.verdict.accepted ? `<span class="pill ok">${row.verdict.score}</span>` : `<span class="pill bad">rejected</span>`}</td>
          <td>${row.seeders}</td>
          <td>${bytes(row.size)}</td>
          <td><button class="btn ghost" type="button" data-grab="${index}" title="Send this release to the downloader">Grab</button></td>
        </tr>`).join("")}</tbody>
    </table></div>` : `<div class="empty">No indexer returned a match. Check the indexers in Settings.</div>`}
  `;
};

views.requests = async () => {
  const [wanted, profile] = await Promise.all([api("/api/wanted"), api("/api/profile")]);
  return `
    <div class="view-head">
      <div><h1>Requests</h1><p>What the platform should fetch. Each request searches every indexer on the next pass and grabs the best candidate that beats what you already have.</p></div>
    </div>
    <div class="row" style="align-items:flex-start">
      <div class="card" style="flex:1 1 320px">
        <h3>Add a request</h3>
        <form id="request-form">
          <div class="field"><label for="w-title">Title</label><input id="w-title" name="title" required placeholder="Dune: Part Two"></div>
          <div class="row">
            <div class="field"><label for="w-kind">Kind</label>
              <select id="w-kind" name="kind">
                <option value="movie">Film</option>
                <option value="episode">TV episode</option>
                <option value="track">Music</option>
              </select></div>
            <div class="field"><label for="w-year">Year</label><input id="w-year" name="year" inputmode="numeric" placeholder="2024"></div>
          </div>
          <div class="row" id="episode-fields" hidden>
            <div class="field"><label for="w-season">Season</label><input id="w-season" name="season" inputmode="numeric" placeholder="1"></div>
            <div class="field"><label for="w-episode">Episode</label><input id="w-episode" name="episode" inputmode="numeric" placeholder="1"></div>
          </div>
          <button class="btn primary" type="submit">Add request</button>
        </form>
      </div>
      <div class="card" style="flex:1 1 320px">
        <h3>Profile in force</h3>
        <p style="margin:8px 0 0">Resolutions, best first: <b>${profile.resolutions.join(", ")}</b></p>
        <p>Sources: <b>${profile.sources.join(", ")}</b></p>
        <p>Codecs: <b>${profile.codecs.join(", ")}</b></p>
        <p>Size window: <b>${profile.min_size_gb}–${profile.max_size_gb} GB</b>${profile.upgrade_allowed ? ", upgrades allowed" : ", no upgrades"}</p>
        <div class="meta"><span class="pill">cutoff ${escapeHtml(profile.cutoff)}</span></div>
      </div>
    </div>
    <h2 style="margin:24px 0 12px;font-size:19px">Queue</h2>
    ${wanted.items.length ? `<div class="table-wrap"><table>
      <thead><tr><th>Title</th><th>Kind</th><th>Status</th><th>Attempts</th><th></th></tr></thead>
      <tbody>${wanted.items.map((row) => `
        <tr>
          <td>${escapeHtml(row.title)}${row.year ? ` <span style="color:var(--muted)">(${row.year})</span>` : ""}
            ${row.season != null ? `<span style="color:var(--muted)"> · S${String(row.season).padStart(2, "0")}E${String(row.episode ?? 0).padStart(2, "0")}</span>` : ""}</td>
          <td>${escapeHtml(row.kind)}</td>
          <td><span class="pill ${row.status === "imported" ? "ok" : row.status === "failed" ? "bad" : ""}">${escapeHtml(row.status)}</span></td>
          <td>${row.attempts}</td>
          <td><button class="btn ghost danger" type="button" data-delete="${row.id}">Remove</button></td>
        </tr>`).join("")}</tbody>
    </table></div>` : `<div class="empty">No requests yet.</div>`}
  `;
};

views.downloads = async () => {
  const data = await api("/api/downloads");
  return `
    <div class="view-head">
      <div><h1>Downloads</h1><p>Transmission is the only download client left — the queue, the rate and the history in one view.</p></div>
      <div class="spacer"></div>
      <span class="pill ${data.status.reachable ? "ok" : "bad"}">
        ${data.status.reachable ? `${data.status.count} torrents · ${bytes(data.status.rate)}/s` : "downloader unreachable"}
      </span>
    </div>
    ${data.torrents.length ? `<div class="table-wrap"><table>
      <thead><tr><th>Release</th><th>State</th><th>Progress</th><th>Rate</th><th>Left</th></tr></thead>
      <tbody>${data.torrents.map((t) => `
        <tr>
          <td>${escapeHtml(t.name)}${t.error ? `<div style="color:var(--bad);font-size:12.5px">${escapeHtml(t.error)}</div>` : ""}</td>
          <td><span class="pill ${t.complete ? "ok" : ""}">${escapeHtml(t.status)}</span></td>
          <td><div class="progress"><i style="width:${t.percent}%"></i></div><div style="color:var(--muted);font-size:12px">${t.percent}% · ${bytes(t.size)}</div></td>
          <td>${bytes(t.rate)}/s</td>
          <td>${eta(t.eta)}</td>
        </tr>`).join("")}</tbody>
    </table></div>` : `<div class="empty">Nothing downloading.</div>`}
    <h2 style="margin:24px 0 12px;font-size:19px">Grab history</h2>
    ${data.grabs.length ? `<div class="table-wrap"><table>
      <thead><tr><th>Release</th><th>Indexer</th><th>Score</th><th>State</th><th>When</th></tr></thead>
      <tbody>${data.grabs.map((g) => `
        <tr>
          <td>${escapeHtml(g.release)}<div style="color:var(--muted);font-size:12.5px">${escapeHtml(g.quality || "")}</div></td>
          <td>${escapeHtml(g.indexer)}</td>
          <td>${g.score}</td>
          <td><span class="pill ${g.state === "imported" ? "ok" : g.state === "failed" ? "bad" : ""}">${escapeHtml(g.state)}</span></td>
          <td>${escapeHtml((g.grabbed_at || "").replace("T", " ").replace("+00:00", ""))}</td>
        </tr>`).join("")}</tbody>
    </table></div>` : `<div class="empty">No grabs recorded yet.</div>`}
  `;
};

views.live = async () => {
  const data = await api("/api/live/channels");
  if (!data.channels.length) {
    return `<div class="view-head"><div><h1>Live TV</h1>
      <p>Point <code>LIVETV_M3U_URL</code> and <code>LIVETV_XMLTV_URL</code> at a playlist and guide and the channel list appears here, numbered deterministically — the tuner is built in, so there is no backend to run.</p></div></div>
      <div class="empty">No playlist configured.</div>`;
  }
  return `
    <div class="view-head"><div><h1>Live TV</h1>
      <p>${data.channels.length} channels, numbered by group and name so a number always means the same channel.</p></div></div>
    <div class="table-wrap"><table>
      <thead><tr><th>#</th><th>Channel</th><th>Now</th><th>Next</th></tr></thead>
      <tbody>${data.channels.map((c) => `
        <tr>
          <td>${c.number}</td>
          <td><div style="font-weight:600">${escapeHtml(c.name)}</div><div style="color:var(--muted);font-size:12.5px">${escapeHtml(c.group)}</div></td>
          <td>${c.now ? `${escapeHtml(c.now.title)} <span class="pill">${c.progress}%</span>` : `<span style="color:var(--muted)">—</span>`}</td>
          <td>${c.next ? escapeHtml(c.next.title) : `<span style="color:var(--muted)">—</span>`}</td>
        </tr>`).join("")}</tbody>
    </table></div>`;
};

views.settings = async () => {
  const [indexers, scheduler, subs, jelly] = await Promise.all([
    api("/api/indexers"),
    api("/api/scheduler"),
    api("/api/subtitles").catch(() => null),
    api("/api/jellyfin").catch(() => ({ configured: false, reachable: false })),
  ]);
  return `
    <div class="view-head"><div><h1>Settings</h1>
      <p>Indexers, the scheduler and the dedupe audit. Keys are stored server-side and never sent back to the browser.</p></div></div>

    <div class="row" style="align-items:flex-start">
      <div class="card" style="flex:1 1 360px">
        <h3>Add or update an indexer</h3>
        <form id="indexer-form">
          <div class="field"><label for="i-name">Name</label><input id="i-name" name="name" required placeholder="Tracker A"></div>
          <div class="field"><label for="i-url">Torznab URL</label><input id="i-url" name="url" required placeholder="https://indexer.example"></div>
          <div class="field"><label for="i-key">API key</label><input id="i-key" name="api_key" placeholder="optional"></div>
          <div class="row">
            <div class="field"><label for="i-kind">Kind</label>
              <select id="i-kind" name="kind"><option>torznab</option><option>newznab</option></select></div>
            <div class="field"><label for="i-cats">Categories</label><input id="i-cats" name="cats" placeholder="2000,5000"></div>
          </div>
          <button class="btn primary" type="submit">Save indexer</button>
        </form>
      </div>
      <div class="card" style="flex:1 1 360px">
        <h3>Scheduler</h3>
        <p>Searching every ${Math.round(scheduler.interval / 60)} minutes, ${scheduler.batch} requests per pass; finished downloads are imported every minute.</p>
        <div class="meta">
          <span class="pill">last run ${scheduler.last?.at ? escapeHtml(scheduler.last.at.replace("T", " ").replace("+00:00", "")) : "not yet"}</span>
        </div>
        <div class="meta" style="margin-top:14px">
          <button class="btn" id="tick" type="button">Run a pass now</button>
          <button class="btn" id="dedupe" type="button">Audit duplicates</button>
        </div>
        <div id="dedupe-out" style="margin-top:14px"></div>
      </div>
    </div>

    <div class="row" style="align-items:flex-start;margin-top:18px">
      <div class="card" style="flex:1 1 360px">
        <h3>Subtitles</h3>
        ${subs && subs.enabled ? `
          <p>${subs.ok} files have a subtitle, ${subs.missing} have none yet, ${subs.failed} failed. Languages: <b>${subs.languages.join(", ")}</b> via <b>${escapeHtml(subs.provider)}</b>.</p>
          <div class="meta" style="margin-top:12px">
            <button class="btn" id="subs-fetch" type="button">Fetch missing subtitles</button>
          </div>
          <div id="subs-out" style="margin-top:12px;color:var(--muted);font-size:13px"></div>
        ` : `<p>No subtitle provider configured. Set <code>OPENSUBTITLES_API_KEY</code> and <code>CORE_SUBTITLE_LANGS</code>, then restart core — missing subtitles are then fetched for every import and on a slow background pass.</p>`}
      </div>
      <div class="card" style="flex:1 1 360px">
        <h3>Jellyfin</h3>
        ${jelly.configured ? (jelly.reachable
          ? `<p>${escapeHtml(jelly.server || "Jellyfin")} ${escapeHtml(jelly.version || "")} — core tells it about every import, so a new file appears without waiting for a scheduled scan.</p>`
          : `<p>Configured, but not reachable: ${escapeHtml(jelly.error || "no answer")}.</p>`)
          : `<p>Set <code>JELLYFIN_URL</code> and <code>JELLYFIN_API_KEY</code> so every import refreshes the library.</p>`}
        <div class="meta" style="margin-top:12px">
          <button class="btn" id="jelly-refresh" type="button" ${jelly.configured ? "" : "disabled"}>Refresh Jellyfin library</button>
        </div>
      </div>
    </div>

    <h2 style="margin:24px 0 12px;font-size:19px">Indexers</h2>
    ${indexers.indexers.length ? `<div class="table-wrap"><table>
      <thead><tr><th>Name</th><th>URL</th><th>Kind</th><th>Key</th><th></th></tr></thead>
      <tbody>${indexers.indexers.map((i) => `
        <tr>
          <td>${escapeHtml(i.name)}</td><td>${escapeHtml(i.url)}</td><td>${escapeHtml(i.kind)}</td>
          <td>${i.has_key ? `<span class="pill ok">set</span>` : `<span class="pill warn">none</span>`}</td>
          <td><button class="btn ghost danger" type="button" data-indexer="${escapeHtml(i.name)}">Remove</button></td>
        </tr>`).join("")}</tbody>
    </table></div>` : `<div class="empty">No indexers yet — add one above and search starts working.</div>`}
  `;
};

/* ---------------------------------------------------------------- rendering */

async function render() {
  const [route, queryString] = location.hash.replace(/^#\/?/, "").split("?");
  const params = new URLSearchParams(queryString || "");
  const view = views[route] ? (route || "home") : "home";
  state.view = view;
  setActive(view);
  loading(true);
  try {
    $("#view").innerHTML = await views[state.view](params);
  } catch (error) {
    $("#view").innerHTML = `<div class="error"><h3>Could not load this view</h3><p>${escapeHtml(error.message)}</p></div>`;
  } finally {
    loading(false);
  }
  wire();
}

function wire() {
  const sidebar = $(".sidebar");
  sidebar.dataset.open = "false";

  $$("[data-kind]").forEach((button) => button.addEventListener("click", () => {
    const route = location.hash.replace(/^#\/?/, "").split("?")[0] || "home";
    if (route === "search") {
      const params = new URLSearchParams(location.hash.split("?")[1] || "");
      params.set("kind", button.dataset.kind);
      location.hash = `#/search?${params}`;
    } else {
      state.libraryKind = button.dataset.kind;
      render();
    }
  }));

  $("#scan-now")?.addEventListener("click", async (event) => {
    event.target.disabled = true;
    event.target.textContent = "Scanning…";
    try {
      const result = await api("/api/scan", { method: "POST" });
      toast(`Scanned ${result.scanned} files, ${result.added} new`, "ok");
      render();
    } catch (error) {
      toast(error.message, "bad");
    } finally {
      event.target.disabled = false;
      event.target.textContent = "Rescan library";
    }
  });

  $("#request-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    const payload = {
      title: form.get("title"),
      kind: form.get("kind"),
      year: form.get("year") ? Number(form.get("year")) : null,
      season: form.get("season") ? Number(form.get("season")) : null,
      episode: form.get("episode") ? Number(form.get("episode")) : null,
    };
    try {
      await api("/api/wanted", { method: "POST", body: JSON.stringify(payload) });
      toast(`Requested ${payload.title}`, "ok");
      render();
    } catch (error) {
      toast(error.message, "bad");
    }
  });

  $("#w-kind")?.addEventListener("change", (event) => {
    $("#episode-fields").hidden = event.target.value !== "episode";
  });

  $$("[data-delete]").forEach((button) => button.addEventListener("click", async () => {
    try {
      await api(`/api/wanted/${button.dataset.delete}`, { method: "DELETE" });
      toast("Request removed");
      render();
    } catch (error) { toast(error.message, "bad"); }
  }));

  $("#indexer-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    const payload = {
      name: form.get("name"), url: form.get("url"),
      api_key: form.get("api_key") || "", kind: form.get("kind") || "torznab",
      cats: form.get("cats") || "", enabled: true,
    };
    try {
      await api("/api/indexers", { method: "POST", body: JSON.stringify(payload) });
      toast(`Saved ${payload.name}`, "ok");
      render();
    } catch (error) { toast(error.message, "bad"); }
  });

  $$("[data-indexer]").forEach((button) => button.addEventListener("click", async () => {
    try {
      await api(`/api/indexers/${encodeURIComponent(button.dataset.indexer)}`, { method: "DELETE" });
      toast("Indexer removed");
      render();
    } catch (error) { toast(error.message, "bad"); }
  }));

  $("#tick")?.addEventListener("click", async (event) => {
    event.target.disabled = true;
    try {
      const result = await api("/api/scheduler/tick", { method: "POST" });
      toast(`Pass done: ${result.grabbed?.length ?? 0} grabbed, ${result.imported?.length ?? 0} imported`, "ok");
      render();
    } catch (error) { toast(error.message, "bad"); }
    finally { event.target.disabled = false; }
  });

  $$("[data-grab]").forEach((button) => button.addEventListener("click", async () => {
    const row = state.results[Number(button.dataset.grab)];
    if (!row) return;
    button.disabled = true;
    button.textContent = "Grabbing…";
    try {
      const kind = new URLSearchParams(location.hash.split("?")[1] || "").get("kind") || "movie";
      const result = await api("/api/grab", {
        method: "POST",
        body: JSON.stringify({
          title: row.title, kind, download_url: row.download_url,
          infohash: row.infohash, indexer: row.indexer, guid: row.guid,
          size: row.size || 0,
        }),
      });
      if (result.ok) {
        toast(result.duplicate ? "Already grabbed — nothing sent twice" : "Grabbed — watch it in Downloads", "ok");
        button.textContent = result.duplicate ? "Already grabbed" : "Grabbed";
      } else {
        toast(result.error || "Grab failed", "bad");
        button.disabled = false;
        button.textContent = "Grab";
      }
    } catch (error) {
      toast(error.message, "bad");
      button.disabled = false;
      button.textContent = "Grab";
    }
  }));

  $("#subs-fetch")?.addEventListener("click", async (event) => {
    event.target.disabled = true;
    event.target.textContent = "Fetching…";
    try {
      const result = await api("/api/subtitles/fetch", { method: "POST" });
      $("#subs-out").textContent = result.count
        ? `${result.ok} of ${result.count} subtitles written; the rest were recorded as missing.`
        : "Nothing to fetch — every file in the library already has one, or recent misses are being held back.";
      toast(`${result.ok || 0} subtitles saved`, "ok");
    } catch (error) { toast(error.message, "bad"); }
    finally { event.target.disabled = false; event.target.textContent = "Fetch missing subtitles"; }
  });

  $("#jelly-refresh")?.addEventListener("click", async (event) => {
    event.target.disabled = true;
    try {
      const result = await api("/api/jellyfin/refresh", { method: "POST" });
      toast(result.refreshed ? "Jellyfin is rescanning" : "Jellyfin did not acknowledge", result.refreshed ? "ok" : "bad");
    } catch (error) { toast(error.message, "bad"); }
    finally { event.target.disabled = false; }
  });

  $("#dedupe")?.addEventListener("click", async (event) => {
    event.target.disabled = true;
    try {
      const report = await api("/api/dedupe");
      const target = $("#dedupe-out");
      target.innerHTML = report.count
        ? `<p><b>${report.count}</b> duplicated file groups, <b>${bytes(report.wasted_bytes)}</b> recoverable.</p>
           <ul style="margin:8px 0 0;padding-left:18px;color:var(--muted);font-size:13px">
             ${report.groups.slice(0, 5).map((g) => `<li>${escapeHtml(g.keep.split("/").pop())} — ${g.remove.length} identical copy(ies)</li>`).join("")}
           </ul>`
        : `<p>No duplicate files found — the library is already deduplicated.</p>`;
    } catch (error) { toast(error.message, "bad"); }
    finally { event.target.disabled = false; }
  });
}

/* ------------------------------------------------------------------- boot */

$("#menu")?.addEventListener("click", () => {
  const sidebar = $(".sidebar");
  const open = sidebar.dataset.open !== "true";
  sidebar.dataset.open = String(open);
  $("#menu").setAttribute("aria-expanded", String(open));
});

$("#theme-toggle")?.addEventListener("click", () => {
  const root = document.documentElement;
  const next = root.dataset.theme === "dark" ? "light" : "dark";
  root.dataset.theme = next;
  localStorage.setItem("gizmo.theme", next);
});

$("#quick-search")?.addEventListener("submit", (event) => {
  event.preventDefault();
  const query = $("#q").value.trim();
  if (query.length < 2) return;
  location.hash = `#/search?q=${encodeURIComponent(query)}`;
});

document.addEventListener("keydown", (event) => {
  if (event.key === "/" && !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) {
    event.preventDefault();
    $("#q").focus();
  }
});

window.addEventListener("hashchange", render);

const saved = localStorage.getItem("gizmo.theme") || localStorage.getItem("monarch.theme");
if (saved) document.documentElement.dataset.theme = saved;

refreshHealth();
setInterval(refreshHealth, 60000);
render();
