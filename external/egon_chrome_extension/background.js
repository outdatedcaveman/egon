// Egon tabs reporter — service worker.
//
// Posts a JSON list of currently-open tabs to your local Egon/Panop
// instance every PUSH_INTERVAL_S seconds, and also immediately on any
// tab create/remove/update event. No data ever leaves 127.0.0.1.
//
// Endpoint: POST http://127.0.0.1:8000/api/v1/chrome_tabs/update
// Payload:  { "ts": <epoch_ms>, "count": N, "tabs": [{id,title,url,windowId,active,pinned}, ...] }

const ENDPOINT = "http://127.0.0.1:8000/api/v1/chrome_tabs/update";
const PUSH_INTERVAL_S = 30;        // baseline push, even if no tab events
const ALARM_NAME = "egon-tab-push";

async function pushTabs() {
  try {
    const tabs = await chrome.tabs.query({});
    const manifest = chrome.runtime.getManifest();
    const payload = {
      ts: Date.now(),
      count: tabs.length,
      // Extension self-report — lets Egon show what's actually loaded and
      // warn the user when an outdated version is running. Added 2026-05-20
      // after Bruno spent multiple sessions debugging stale v1.0 behaviour.
      extension: {
        version: manifest.version,
        name: manifest.name,
        host_permissions_count: (manifest.host_permissions || []).length,
      },
      tabs: tabs.map(t => ({
        id:        t.id,
        title:     t.title || "",
        url:       t.url || "",
        windowId:  t.windowId,
        active:    !!t.active,
        pinned:    !!t.pinned,
        audible:   !!t.audible,
        groupId:   t.groupId || -1,
      })),
    };
    await fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    await chrome.storage.local.set({
      last_push_ok: Date.now(),
      last_push_count: tabs.length,
      last_push_error: null,
    });
  } catch (e) {
    await chrome.storage.local.set({
      last_push_error: String(e).slice(0, 200),
      last_push_attempted: Date.now(),
    });
  }
}

// Periodic baseline push via Chrome alarms (service workers can't use setInterval)
const HARVEST_ALARM = "egon-auto-harvest";
const HARVEST_PERIOD_MIN = 60;     // re-harvest every 60min on each known tab

// ─── TV Time auth capture ───────────────────────────────────────────────────
// TV Time's web app is Flutter/CanvasKit (verified 2026-05-23): no DOM, and its
// network calls bypass page-context fetch/XHR hooks (Flutter grabs native refs
// before our content script loads). But webRequest sees headers at the NETWORK
// layer — nothing can hide them. We snapshot the x-api-key (and Authorization)
// the app sends to its sidecar proxy so the harvester can REPLAY those exact,
// known-good headers against every show endpoint. Captured live in Bruno's own
// browser, never leaves the machine, and self-heals if TV Time rotates the key.
// Bruno 2026-05-23 explicitly chose to crack it with the app key.
let _tvtimeAuth = null;
try {
  chrome.webRequest.onBeforeSendHeaders.addListener(
    (details) => {
      try {
        // Bruno 2026-05-29: capture the x-api-key from ANY tvtime request that
        // carries it — the app's sidecar proxy calls (app.tvtime.com/sidecar)
        // AND direct msapi.tvtime.com calls. The old filter required "/sidecar"
        // in the URL, so if the app hit msapi directly (or the sidecar call was
        // served from cache) the key was never seen and the harvest stayed
        // gated out (auth_captured:false → 0 shows). Broaden it + MERGE headers
        // so a partial capture on one request completes from another.
        const url = details.url || "";
        if (!/tvtime\.com/.test(url)) return;
        const want = (url.includes("/sidecar") || url.includes("msapi.tvtime.com"));
        if (!want) return;
        const prev = (_tvtimeAuth && _tvtimeAuth.headers) || {};
        const h = { ...prev };
        for (const x of (details.requestHeaders || [])) {
          const n = (x.name || "").toLowerCase();
          if (n === "x-api-key" || n === "authorization" ||
              n === "tvst-access-token" || n === "x-app-version") h[n] = x.value;
        }
        if (h["x-api-key"]) {
          _tvtimeAuth = { headers: h, ts: Date.now() };
          chrome.storage.local.set({ tvtime_auth: _tvtimeAuth });
        }
      } catch (e) {}
    },
    { urls: ["*://app.tvtime.com/*", "*://*.tvtime.com/*", "*://msapi.tvtime.com/*"] },
    ["requestHeaders", "extraHeaders"]
  );
} catch (e) { /* webRequest unavailable — harvester falls back to debug-only */ }

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(ALARM_NAME,    { periodInMinutes: PUSH_INTERVAL_S / 60 });
  chrome.alarms.create(HARVEST_ALARM, { periodInMinutes: HARVEST_PERIOD_MIN });
  pushTabs();
});
chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create(ALARM_NAME,    { periodInMinutes: PUSH_INTERVAL_S / 60 });
  chrome.alarms.create(HARVEST_ALARM, { periodInMinutes: HARVEST_PERIOD_MIN });
  pushTabs();
});
chrome.alarms.onAlarm.addListener(async (a) => {
  if (a.name === ALARM_NAME) pushTabs();
  if (a.name === HARVEST_ALARM) {
    // Auto-harvest: walk every open tab whose URL matches a harvester and
    // run the extractor against it. No user action needed — Bruno's main
    // complaint about Kindle/Paperpile was "it's too manual". This makes
    // the data stay fresh as long as he keeps the relevant tab open
    // somewhere (background tabs work too).
    try {
      const tabs = await chrome.tabs.query({});
      for (const t of tabs) {
        if (!t || !t.url) continue;
        if (HARVESTERS.some(h => h.test(t.url))) {
          await harvestIfMatch(t).catch(() => {});
        }
      }
    } catch (e) {}
  }
});

// Event-driven pushes — react immediately to tab changes
chrome.tabs.onCreated.addListener(pushTabs);
chrome.tabs.onRemoved.addListener(pushTabs);
chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  // only push on URL/title/status changes, not on every keystroke
  if (info.url || info.title || info.status === "complete") {
    pushTabs();
    // When the page finishes loading on a known harvest URL, run the harvester.
    if (info.status === "complete" && tab && tab.url) harvestIfMatch(tab);
  }
});
chrome.tabs.onMoved.addListener(pushTabs);
chrome.tabs.onActivated.addListener(async (info) => {
  pushTabs();
  // SPAs (Paperpile) navigate without triggering tabs.onUpdated 'complete'.
  // Re-run the harvester whenever the user switches to a known tab.
  try {
    const tab = await chrome.tabs.get(info.tabId);
    if (tab) harvestIfMatch(tab);
  } catch (e) {}
});

// Manual triggers from the popup or other extension UI surfaces.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.kind === "force_harvest_active") {
    (async () => {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (tab) await harvestIfMatch(tab);
      sendResponse({ ok: true });
    })();
    return true;
  }
  if (msg && msg.kind === "run_full_sync") {
    (async () => {
      const result = await runFullSync({ reason: "popup_click" });
      sendResponse(result);
    })();
    return true;
  }
  // Egon Connect (connect.js floating panel): POST on-screen text to the local
  // mind Connection Engine and return ranked archive/mind connections. Routed
  // through the worker so the page's CSP can't block the local fetch. 2026-06-06.
  if (msg && msg.type === "egon_connect") {
    (async () => {
      try {
        const r = await fetch("http://127.0.0.1:8000/api/v1/mind/connect", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: String(msg.text || ""), limit: 18 }),
        });
        sendResponse(await r.json());
      } catch (e) {
        sendResponse({ status: "error", error: String(e).slice(0, 200) });
      }
    })();
    return true;
  }
});


// ─── FULL AUTO-SYNC ─────────────────────────────────────────────────────────
// Opens each harvester's default_url in a NEW BACKGROUND TAB, waits for the
// harvester to fire on `tabs.onUpdated → complete`, then closes the tab.
// Bruno's request: zero manual steps. The extension owns the loop entirely.
//
// Triggers:
//   1. Chrome alarm `egon-full-sync` every 6 h
//   2. Periodic poll of Panop's /api/v1/sync/request — Egon can ask anytime
//   3. Popup "Sync all libraries now" button
const FULL_SYNC_ALARM = "egon-full-sync";
const FULL_SYNC_INTERVAL_MIN = 6 * 60;        // safety net: every 6 hours
const DAILY_SYNC_ALARM = "egon-daily-6am";    // Bruno's request: 06:00 daily
const SYNC_REQUEST_POLL_MIN  = 0.5;           // poll Egon every 30 s
const SYNC_REQUEST_POLL_ALARM = "egon-sync-poll";
const SYNC_REQUEST_ENDPOINT = "http://127.0.0.1:8000/api/v1/sync/request";

// Compute the epoch-ms of the next local 06:00. Chrome alarms fire on
// wall-clock time, so this gives us a true daily-at-6AM trigger that then
// repeats every 1440 minutes. If the machine is asleep at 6AM, Chrome fires
// the alarm as soon as it wakes — so the morning refresh still happens.
function _next6amMs() {
  const now = new Date();
  const next = new Date(now);
  next.setHours(6, 0, 0, 0);
  if (next.getTime() <= now.getTime()) next.setDate(next.getDate() + 1);
  return next.getTime();
}

// Where we'd find each harvester's URL — collected once at startup.
function _harvesterUrls() {
  const seen = new Set();
  const urls = [];
  for (const h of HARVESTERS) {
    const u = h.default_url || "";
    if (!u || seen.has(u)) continue;
    seen.add(u);
    urls.push({ url: u, endpoint: h.endpoint });
  }
  return urls;
}

async function runFullSync(opts = {}) {
  const reason = opts.reason || "scheduled";
  
  let dynamicKindleUrl = null;
  try {
    const configResp = await fetch("http://127.0.0.1:8000/api/v1/kindle/config");
    if (configResp.ok) {
      const configData = await configResp.json();
      if (configData.pdocs_url) {
        dynamicKindleUrl = configData.pdocs_url;
      }
    }
  } catch (e) {}

  const urls = _harvesterUrls();
  const out = { reason, started_at: Date.now(), per_url: [] };
  for (let { url, endpoint } of urls) {
    if (endpoint.includes("kindle/library") && dynamicKindleUrl) {
      url = dynamicKindleUrl;
    }
    const perUrl = { url, opened: false, harvested: false, count_before: 0,
                     count_after: 0, error: null, ms: 0 };
    const t0 = Date.now();
    try {
      // Snapshot the current harvest count for this endpoint so we can detect
      // a successful harvest by count delta.
      try {
        const r = await fetch(endpoint, { method: "GET" });
        if (r.ok) {
          const d = await r.json();
          perUrl.count_before = d && d.count ? d.count : 0;
        }
      } catch (e) {}

      // Open in a background tab (active:false keeps your foreground)
      // SPAs (YouTube, TV Time, Instapaper, Paperpile) DON'T render or fire
      // their data fetches in a background tab — Chrome throttles them. So we
      // open the harvest tab in the FOREGROUND, let it render+harvest, then
      // close it. At the 6 AM daily sync the user is asleep so the brief tab
      // flashes don't matter; on-demand it's a few seconds per source.
      // Bruno 2026-05-22: this is why background harvest returned 1 / no_data.
      const tab = await chrome.tabs.create({ url, active: true });
      perUrl.opened = true;

      // Wait for `complete` status with a hard timeout, then run the
      // harvester and wait for endpoint count to change.
      const COMPLETE_TIMEOUT_MS = 60_000;
      await new Promise((resolve) => {
        const deadline = Date.now() + COMPLETE_TIMEOUT_MS;
        const listener = (tabId, info) => {
          if (tabId !== tab.id) return;
          if (info.status === "complete") {
            chrome.tabs.onUpdated.removeListener(listener);
            resolve();
          }
        };
        chrome.tabs.onUpdated.addListener(listener);
        // Safety: even if `complete` never fires, give up after the deadline
        setTimeout(() => {
          chrome.tabs.onUpdated.removeListener(listener);
          resolve();
        }, COMPLETE_TIMEOUT_MS);
      });

      // Run harvest explicitly (it would also auto-fire on the complete
      // event, but doing it ourselves lets us await the result here).
      try {
        await harvestIfMatch(tab);
        perUrl.harvested = true;
      } catch (e) {
        perUrl.error = `harvest: ${String(e).slice(0, 200)}`;
      }

      // Poll the endpoint up to 90 s for count to advance
      const POLL_MS = 90_000;
      const pollDeadline = Date.now() + POLL_MS;
      while (Date.now() < pollDeadline) {
        try {
          const r = await fetch(endpoint, { method: "GET" });
          if (r.ok) {
            const d = await r.json();
            const count = d && d.count ? d.count : 0;
            if (count !== perUrl.count_before) {
              perUrl.count_after = count;
              break;
            }
          }
        } catch (e) {}
        await new Promise(r => setTimeout(r, 1500));
      }

      // Close the background tab whether we got data or not
      try { await chrome.tabs.remove(tab.id); } catch (e) {}
    } catch (e) {
      perUrl.error = String(e).slice(0, 200);
    }
    perUrl.ms = Date.now() - t0;
    out.per_url.push(perUrl);
  }
  out.finished_at = Date.now();
  // Tell Egon we did the work
  try {
    await fetch("http://127.0.0.1:8000/api/v1/sync/ack", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(out),
    });
  } catch (e) {}
  await chrome.storage.local.set({ last_full_sync: out });
  return { ok: true, result: out };
}

// Periodically poll Egon for an on-demand sync request.
async function pollSyncRequest() {
  try {
    const r = await fetch(SYNC_REQUEST_ENDPOINT, { method: "GET" });
    if (!r.ok) return;
    const d = await r.json();
    if (d.status !== "ok" || !d.ts) return;
    const local = await chrome.storage.local.get("last_processed_sync_ts");
    const prev = local.last_processed_sync_ts || 0;
    if (d.ts > prev) {
      await chrome.storage.local.set({ last_processed_sync_ts: d.ts });
      await runFullSync({ reason: "egon_signal" });
    }
  } catch (e) {}
}

// Wire up alarms at install + startup. We add the new alarms next to the
// existing ones rather than replacing the handler.
function _createSyncAlarms() {
  chrome.alarms.create(FULL_SYNC_ALARM,         { periodInMinutes: FULL_SYNC_INTERVAL_MIN });
  chrome.alarms.create(SYNC_REQUEST_POLL_ALARM, { periodInMinutes: SYNC_REQUEST_POLL_MIN });
  // Daily at 06:00 local, repeating every 24 h thereafter.
  chrome.alarms.create(DAILY_SYNC_ALARM, { when: _next6amMs(), periodInMinutes: 1440 });
}
chrome.runtime.onInstalled.addListener(_createSyncAlarms);
chrome.runtime.onStartup.addListener(_createSyncAlarms);
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === FULL_SYNC_ALARM)         runFullSync({ reason: "scheduled_6h" });
  if (a.name === DAILY_SYNC_ALARM)        runFullSync({ reason: "daily_6am" });
  if (a.name === SYNC_REQUEST_POLL_ALARM) pollSyncRequest();
});


// ───────────────────────────────────────────────────────────────────────────
// CONTENT HARVESTERS
// ───────────────────────────────────────────────────────────────────────────
//
// When you load a known page in YOUR real Chrome (where you're signed in),
// we extract the relevant content via chrome.scripting.executeScript and
// POST it to Panop. This gets us past every anti-bot defence that blocks
// Egon's own Playwright — because there's no Playwright involved.
//
// Add a new site here in three places:
//   1. host_permissions in manifest.json
//   2. HARVESTERS table below — URL pattern + extractor function
//   3. an endpoint on Panop that accepts the POST (mirror the existing
//      /api/v1/chrome_tabs/update shape, e.g. /api/v1/kindle/library)

// Each harvester needs a default_url so the auto-sync routine can open the
// right page in a background tab without Bruno doing it by hand.
const HARVESTERS = [
  {
    // YouTube WATCH HISTORY — not in the Data API (Google killed it in 2016).
    // The reliable source is `ytInitialData`, the JSON blob YouTube embeds in
    // every page describing the rendered content. We walk it for video
    // renderers + page through the continuation by scrolling. Bruno 2026-05-22.
    default_url: "https://www.youtube.com/feed/history",
    test: (url) => /youtube\.com\/feed\/history/.test(url),
    endpoint: "http://127.0.0.1:8000/api/v1/youtube/history",
    extract: async () => {
      const seen = new Map();
      const out = [];
      function pushVideo(id, title, channel, thumb, watchedDate) {
        if (!id) return;
        if (seen.has(id)) {
          const existing = seen.get(id);
          if (watchedDate && !existing.watched) {
            existing.watched = watchedDate;
          }
          return;
        }
        const item = { id, title, channel,
                   url: `https://www.youtube.com/watch?v=${id}`,
                   thumbnail: thumb || "", watched: watchedDate || "" };
        seen.set(id, item);
        out.push(item);
      }
      // Recursively find videos. YouTube has TWO structures now:
      //   (a) legacy videoRenderer / compactVideoRenderer / gridVideoRenderer
      //   (b) 2024+ lockupViewModel (this is why history returned 1 — the old
      //       walker only knew (a)). Bruno 2026-05-22.
      function walk(obj, depth, sectionHeader = "") {
        if (depth > 16 || !obj || typeof obj !== "object") return;
        if (Array.isArray(obj)) { for (const x of obj) walk(x, depth + 1, sectionHeader); return; }
        
        let currentHeader = sectionHeader;
        if (obj.itemSectionRenderer) {
          try {
            const header = obj.itemSectionRenderer.header;
            const headerRenderer = header && (header.itemSectionHeaderRenderer || header.sectionHeaderRenderer);
            if (headerRenderer) {
              const runs = headerRenderer.title && headerRenderer.title.runs;
              if (runs && runs.length) {
                currentHeader = runs.map(r => r.text).join("");
              } else if (headerRenderer.title && headerRenderer.title.simpleText) {
                currentHeader = headerRenderer.title.simpleText;
              }
            }
          } catch (e) {}
        }

        // (a) classic renderers
        const vr = obj.videoRenderer || obj.compactVideoRenderer || obj.gridVideoRenderer;
        if (vr && vr.videoId) {
          const title = (vr.title && (vr.title.simpleText ||
                        (vr.title.runs || []).map(r => r.text).join(""))) || "";
          const channel = (vr.longBylineText && (vr.longBylineText.runs || [])
                          .map(r => r.text).join("")) ||
                          (vr.ownerText && (vr.ownerText.runs || [])
                          .map(r => r.text).join("")) || "";
          const thumbs = (vr.thumbnail && vr.thumbnail.thumbnails) || [];
          pushVideo(vr.videoId, title, channel,
                    thumbs.length ? thumbs[thumbs.length - 1].url : "", currentHeader);
        }
        // (b) new lockupViewModel
        const lv = obj.lockupViewModel;
        if (lv && lv.contentId && (lv.contentType === "LOCKUP_CONTENT_TYPE_VIDEO" ||
                                   /VIDEO/.test(lv.contentType || ""))) {
          let title = "", channel = "", thumb = "";
          try {
            const meta = lv.metadata && lv.metadata.lockupMetadataViewModel;
            title = (meta && meta.title && (meta.title.content ||
                    (meta.title.runs || []).map(r => r.text).join(""))) || "";
            const rows = meta && meta.metadata &&
                         meta.metadata.contentMetadataViewModel &&
                         meta.metadata.contentMetadataViewModel.metadataRows;
            if (rows && rows[0] && rows[0].metadataParts && rows[0].metadataParts[0]) {
              channel = rows[0].metadataParts[0].text &&
                        rows[0].metadataParts[0].text.content || "";
            }
            const imgs = lv.contentImage &&
                         lv.contentImage.thumbnailViewModel &&
                         lv.contentImage.thumbnailViewModel.image &&
                         lv.contentImage.thumbnailViewModel.image.sources;
            if (imgs && imgs.length) thumb = imgs[imgs.length - 1].url;
          } catch (e) {}
          pushVideo(lv.contentId, title, channel, thumb, currentHeader);
        }
        for (const k in obj) { try { walk(obj[k], depth + 1, currentHeader); } catch (e) {} }
      }
      
      function harvestFromDOM() {
        try {
          const sections = document.querySelectorAll("ytd-item-section-renderer");
          for (const sec of sections) {
            const headerEl = sec.querySelector("#title-text") || sec.querySelector("h2") || sec.querySelector("#title");
            const dateStr = headerEl ? headerEl.textContent.trim() : "";
            const vids = sec.querySelectorAll("ytd-video-renderer, ytd-compact-video-renderer, ytd-grid-video-renderer, yt-lockup-view-model");
            for (const v of vids) {
              let id = "", title = "", channel = "", thumb = "";
              const anchor = v.querySelector("a#thumbnail") || v.querySelector("a[href*=\'/watch?v=\']") || v.querySelector("a");
              if (anchor) {
                const href = anchor.getAttribute("href") || "";
                const m = href.match(/[?&]v=([^&#]+)/);
                if (m) id = m[1];
              }
              if (!id) continue;
              
              const titleEl = v.querySelector("#video-title") || v.querySelector("yt-formatted-string") || v.querySelector("h3");
              if (titleEl) title = titleEl.textContent.trim();
              
              const channelEl = v.querySelector("#channel-name") || v.querySelector("#byline") || v.querySelector("ytd-channel-name");
              if (channelEl) channel = channelEl.textContent.trim();
              
              const img = v.querySelector("img");
              if (img) thumb = img.getAttribute("src") || "";
              
              pushVideo(id, title, channel, thumb, dateStr);
            }
          }
        } catch (e) {}
      }

      function harvestData() {
        try { if (window.ytInitialData) walk(window.ytInitialData, 0, ""); } catch (e) {}
        // captured continuation responses (infinite scroll) hold more history
        for (const c of (window.__egonCaptured || [])) {
          try { walk(c.json, 0, ""); } catch (e) {}
        }
        harvestFromDOM();
      }
      harvestData();
      // Scroll to pull continuations (YouTube lazy-loads history as you scroll)
      const scroller = document.scrollingElement || document.documentElement;
      let prev = -1;
      for (let i = 0; i < 1000 && out.length !== prev; i++) {
        prev = out.length;
        scroller.scrollTop = scroller.scrollHeight;
        window.dispatchEvent(new Event("scroll"));
        await new Promise(r => setTimeout(r, 600));
        harvestData();
      }
      return { ts: Date.now(), url: location.href, count: out.length,
               items: out, strategy: "ytInitialData+scroll+DOM" };
    },
  },
  {
    // TV Time — verified live 2026-05-23 via Chrome MCP. The web app is a
    // FLUTTER / CanvasKit PWA: the UI is painted on a <canvas>, so there is NO
    // HTML DOM to scrape (this is why every DOM attempt returned 0), and the
    // app's network calls bypass page-context fetch/XHR hooks. Data flows
    // through a SAME-ORIGIN proxy: app.tvtime.com/sidecar?o_b64=<base64 target>
    // authenticated with x-api-key + Bearer JWT. We capture those headers at
    // the network layer (chrome.webRequest, see top of file), then REPLAY them
    // here to call every show endpoint directly — robust by construction since
    // we replay a request that already works. `auth` is handed in from the
    // background (MAIN world has no chrome.* access). Bruno 2026-05-23.
    default_url: "https://app.tvtime.com/to-watch",
    test: (url) => /tvtime\.com\//.test(url),
    endpoint: "http://127.0.0.1:8000/api/v1/tvtime/library",
    reload_before: true,        // cache-bypass reload so the app re-fetches (→ auth capture)
    pre_delay_ms: 16000,        // let Flutter re-boot + fire calls so we capture auth
    needs_tvtime_auth: true,
    extract: async (auth) => {
      const seen = new Set();
      const out = [];
      const posterOf = (o) => {
        const p = o.poster || o.image || o.thumb || o.poster_path ||
                  o.artwork || o.image_url || (o.images && (o.images.poster ||
                  o.images.thumb)) || "";
        if (!p) return "";
        if (/^https?:/.test(p)) return p;
        if (p.startsWith("/")) return "https://artworks.thetvdb.com" + p;
        return p;
      };
      const looksLikeEntity = (o) =>
        o && typeof o === "object" && (o.name || o.title) &&
        (o.id || o.uuid || o.show_id || o.entity_id) &&
        (o.poster || o.image || o.thumb || o.poster_path || o.seasons ||
         o.entity_type || o.first_aired || o.overview || o.status ||
         o.last_seen || o.aired_episodes || o.number_of_seasons);
      const add = (o, kind) => {
        const id = String(o.id || o.uuid || o.show_id || o.entity_id);
        const key = (kind || "") + ":" + id;
        if (seen.has(key)) return;
        seen.add(key);
        out.push({
          id,
          title: String(o.name || o.title || "").slice(0, 200),
          image: posterOf(o),
          status: o.status || o.watch_status || "",
          entity_type: o.entity_type || kind || (o.seasons || o.number_of_seasons ? "series" : ""),
          year: String(o.first_aired || o.year || o.release_date || "").slice(0, 4),
          rating: o.rating || o.user_rating || "",
          url: `https://app.tvtime.com/show/${id}`,
        });
      };
      const walk = (obj, depth) => {
        if (depth > 14 || !obj || typeof obj !== "object") return;
        if (Array.isArray(obj)) { for (const x of obj) walk(x, depth + 1); return; }
        const nested = obj.entity || obj.show || obj.series || obj.object;
        if (looksLikeEntity(obj)) add(obj);
        else if (looksLikeEntity(nested)) add(nested, obj.entity_type);
        for (const k in obj) { try { walk(obj[k], depth + 1); } catch (e) {} }
      };

      // ── auth: live JWT from localStorage + captured x-api-key ──
      const jwt = localStorage.getItem("flutter.jwtToken") || "";
      const capHeaders = (auth && auth.headers) || window.__egonTvTimeAuth || {};
      const apiKey = capHeaders["x-api-key"] || "";
      const authz = jwt ? ("Bearer " + jwt) : (capHeaders["authorization"] || "");
      let uid = "";
      try { uid = String(JSON.parse(atob(jwt.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"))).id || ""); } catch (e) {}

      const debug = {
        has_canvas: !!document.querySelector("canvas, flt-glass-pane, flutter-view"),
        logged_in: localStorage.getItem("flutter.isLoggedIn") || null,
        auth_captured: !!apiKey, auth_age_s: auth ? Math.round((Date.now() - (auth.ts || 0)) / 1000) : null,
        uid_found: !!uid, endpoints: [],
      };

      if (apiKey && jwt && uid) {
        const b64 = (s) => btoa(s).replace(/=+$/, "");
        const H = { "Accept": "application/json", "x-api-key": apiKey, "Authorization": authz };
        const EP = [
          { label: "followed_series", base: `https://msapi.tvtime.com/prod/v1/tracking/cgw/follows/user/${uid}`, qs: "&entity_type=series&filter=only_followed_series" },
          { label: "followed_movies", base: `https://msapi.tvtime.com/prod/v1/tracking/cgw/follows/user/${uid}`, qs: "&entity_type=movie&sort=watched_date,desc" },
          { label: "watched_movies",  base: `https://msapi.tvtime.com/prod/v1/tracking/watches/user/${uid}`, qs: "&entity_type=movie" },
          // WATCHED EPISODES — the actual viewing history (Bruno 2026-06-13).
          // This is what a show tracker is FOR; the harvester previously only
          // captured followed shows + watched movies, never the episodes.
          // Endpoint guesses, paginated; harmless if a label 404s.
          { label: "watched_episodes", base: `https://msapi.tvtime.com/prod/v1/tracking/watches/user/${uid}`, qs: "&entity_type=episode&sort=watched_date,desc", paged: true },
          { label: "episode_history",  base: `https://msapi.tvtime.com/prod/v1/tracking/cgw/history/user/${uid}`, qs: "&entity_type=episode", paged: true },
          { label: "fav_series", base: `https://msapi.tvtime.com/prod/v2/lists/user/${uid}/lists/favorite-series`, qs: "&expand=all" },
          { label: "fav_movies", base: `https://msapi.tvtime.com/prod/v2/lists/user/${uid}/lists/favorite-movies`, qs: "&expand=all" },
        ];
        for (const ep of EP) {
          const before = out.length;
          let status = "err";
          try {
            const pages = ep.paged ? 40 : 1;     // walk paginated histories
            for (let pg = 0; pg < pages; pg++) {
              const pgQs = ep.paged ? `${ep.qs}&page=${pg}&limit=100&offset=${pg*100}` : ep.qs;
              const r = await fetch(`/sidecar?o_b64=${b64(ep.base)}${pgQs}`,
                                    { credentials: "include", headers: H });
              status = r.status;
              if (!r.ok) break;
              const j = await r.json();
              const data = j.data !== undefined ? j.data : j;
              const grew = out.length;
              walk(data, 0);
              // stop a paged endpoint once a page adds nothing new
              if (ep.paged && out.length === grew) break;
              if (ep.paged) await new Promise(r => setTimeout(r, 150));
            }
          } catch (e) { status = "err"; }
          debug.endpoints.push({ label: ep.label, status, added: out.length - before });
          await new Promise(r => setTimeout(r, 200));
        }
      }
      return {
        ts: Date.now(), url: location.href, count: out.length, items: out,
        strategy: "sidecar_replay", _debug: debug,
      };
    },
  },
  {
    // Instapaper — Simple API has no list endpoint and Full API needs OAuth.
    // Bruno's signed in to www.instapaper.com, so we harvest the rendered
    // reading list there. Structure verified live 2026-05-23 via Chrome MCP:
    //   • rows are <article class="js_article_item article_item ..."> — the
    //     OLD selector "div.article_item" never matched (it's an ARTICLE), so
    //     every harvest returned 0. THIS was the bug.
    //   • id           → data-article-id attr
    //   • title        → a.article_title (innerText) / aria-label fallback
    //   • REAL url     → a.js_domain_linkout (the source site, not the
    //                    instapaper.com/read/<id> proxy)
    //   • host         → .host
    //   • per_page is IGNORED (always 40/page); pagination is /<folder>/<n>.
    // We walk all three folders so the harvest is exhaustive: Home/unread
    // (/u), Archive (/archive), Liked (/starred).
    default_url: "https://www.instapaper.com/u",
    test: (url) => /(?:www\.)?instapaper\.com\//.test(url),
    endpoint: "http://127.0.0.1:8000/api/v1/instapaper/library",
    extract: async () => {
      const origin = location.origin || "https://www.instapaper.com";
      const seen = new Set();
      const out = [];
      const text = (el) => (el && el.innerText ? el.innerText : "").trim();
      const folders = [
        { path: "/u",       name: "unread"  },
        { path: "/archive", name: "archive" },
        { path: "/starred", name: "liked"   },
      ];
      const debug = { folders: {}, capture_installed: !!window.__egonCaptureInstalled };
      for (const folder of folders) {
        let folderAdded = 0, pages = 0;
        for (let p = 1; p <= 80; p++) {
          const url = p === 1 ? `${origin}${folder.path}`
                              : `${origin}${folder.path}/${p}`;
          let html;
          try {
            // Reuse the already-rendered live DOM for the page we landed on;
            // fetch every other page (same-origin, cookie-auth — verified ok).
            if (p === 1 && location.pathname.replace(/\/$/, "") === folder.path) {
              html = document.documentElement.outerHTML;
            } else {
              const r = await fetch(url, { credentials: "include",
                                           headers: { "Accept": "text/html" } });
              if (!r.ok) break;
              html = await r.text();
            }
          } catch (e) { break; }
          const doc = new DOMParser().parseFromString(html, "text/html");
          const rows = doc.querySelectorAll("article.article_item, .js_article_item");
          if (!rows.length) break;
          pages = p;
          let added = 0;
          rows.forEach((row) => {
            const id = row.getAttribute("data-article-id")
                    || (row.id || "").replace(/^article_/, "");
            const titleEl = row.querySelector("a.article_title, .article_title");
            const title = text(titleEl) || row.getAttribute("aria-label") || "";
            if (!title) return;
            const key = id || title;
            if (seen.has(key)) return;
            seen.add(key);
            const linkout = row.querySelector("a.js_domain_linkout, a[href^='http']:not(.article_title):not(.star_toggle)");
            const realUrl = (linkout && linkout.href) || "";
            const hostVal = text(row.querySelector(".host, .article_host, .js_domain"));
            out.push({
              id: String(id || ""),
              title: title.slice(0, 400),
              url: realUrl,
              read_url: (titleEl && titleEl.href) || "",
              host: hostVal,
              time: (() => {
                const timeEl = row.querySelector("time, .metadata, .meta, .meta_data, .article_date, .date, [class*='timestamp']");
                let rawTime = text(timeEl);
                if (rawTime && hostVal) {
                  const escapedHost = hostVal.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
                  const rx = new RegExp("^\\s*" + escapedHost + "\\s*([•·|\\-\\s]+)?", "i");
                  rawTime = rawTime.replace(rx, "").trim();
                }
                return rawTime;
              })(),
              description: text(row.querySelector(".article_preview, .article_summary")).slice(0, 300),
              folder: folder.name,
              source: "dom_paginated",
            });
            added++; folderAdded++;
          });
          if (!added) break;             // page had only duplicates — stop folder
          await new Promise(r => setTimeout(r, 200));
        }
        debug.folders[folder.name] = { added: folderAdded, pages };
      }
      return {
        ts: Date.now(),
        url: location.href,
        count: out.length,
        items: out,
        strategy: "paginated_dom_v2",
        _debug: debug,
      };
    },
  },
  {
    // Amazon Kindle library — opens the digital-console root; JSON API
    // walker covers every category from there. Bruno's region is com.br
    // but we let the user override via storage if they're elsewhere.
    default_url: "https://www.amazon.com.br/hz/mycd/digital-console/contentlist/allcontent/dateDsc?pageNumber=1",
    // includes BOTH ebooks AND sideloaded documents (epub/pdf categorised
    // as 'Documents'). Bruno 2026-05-20.
    //
    // Also matches the legacy /myx page + the reader pages, just in case.
    test: (url) => (
      /amazon\.[a-z.]+\/hz\/mycd\/digital-console/.test(url) ||
      /amazon\.[a-z.]+\/hz\/mycd\/myx/.test(url) ||
      /amazon\.[a-z.]+\/hz\/mycd/.test(url) ||
      /(?:ler|read)\.amazon\.[a-z.]+\/kindle-library/.test(url) ||
      /(?:ler|read)\.amazon\.[a-z.]+\/notebook/.test(url)
    ),
    endpoint: "http://127.0.0.1:8000/api/v1/kindle/library",
    extract: async () => {
      // Pagination walker: when we're on the /digital-console/contentlist
      // page, fetch every pageNumber=1..N from the same origin (the user's
      // cookies are carried automatically). Parse each response's HTML and
      // accumulate items. Stops on the first empty page.
      //
      // For non-paginated pages (reader/notebook) we just scrape the
      // current DOM.
      const PATTERNS = [
        // digital-console contentlist — owned items (ebooks AND documents)
        { row: "div.ListItem-module_row__3orql, div[data-asin], li[data-asin]",
          asin: (el) => el.getAttribute("data-asin") || (el.querySelector("[data-asin]") || {}).getAttribute?.("data-asin") || "",
          title: ".digital_entity_title, [class*='title']",
          author: ".digital_entity_author_label, [class*='author']",
          type: "[class*='content-type'], [class*='item-type']" },
        // Legacy /myx table layout
        { row: "div.contentTableList_myx tr, table.contentTableList_myx tr",
          asin: (el) => el.getAttribute("data-asin") || "",
          title: ".title, .digital_entity_title",
          author: ".author, .digital_entity_author" },
        // read.amazon.* /kindle-library + ler.amazon.com.br /kindle-library
        { row: "a.kp-notebook-library-each-book",
          asin: (el) => el.getAttribute("id") || "",
          title: "h2.kp-notebook-searchable",
          author: "p.kp-notebook-searchable" },
        // Generic data-test-id rows
        { row: "[data-test-id='content-row']",
          asin: (el) => el.getAttribute("data-asin") || "",
          title: ".title, [data-test-id='title']",
          author: ".author, [data-test-id='author']" },
      ];

      function parseDoc(doc) {
        const found = [];
        for (const p of PATTERNS) {
          doc.querySelectorAll(p.row).forEach((row) => {
            const t = row.querySelector(p.title);
            const title = (t ? (t.innerText || t.textContent) : "").trim();
            if (!title) return;
            const a = row.querySelector(p.author);
            const ty = p.type ? row.querySelector(p.type) : null;
            const asin = p.asin(row) || "";
            found.push({
              asin: asin,
              title: title.slice(0, 400),
              author: (a ? (a.innerText || a.textContent) : "").trim().slice(0, 200),
              kind: ty ? (ty.innerText || ty.textContent).trim() : "",
              source: `dom:${p.row}`,
            });
          });
        }
        return found;
      }

      const seen = new Set();
      const out = [];
      function merge(items) {
        for (const it of items) {
          const key = it.asin || `${it.title}|${it.author}`;
          if (seen.has(key)) continue;
          seen.add(key);
          out.push(it);
        }
      }

      // Strategy 0: walk every content type via JSON API.
      //
      // The /digital-console/ URL Bruno was on is just a CATEGORY OVERVIEW
      // page (eBooks 6, Documents 199, Collections 373 across 8 collections).
      // The ACTUAL items live under per-category contentlists. Rather than
      // make Bruno navigate each one, we hit the JSON endpoint for every
      // known content type and accumulate.
      //
      // Bruno 2026-05-20.
      const CONTENT_TYPES = [
        "Ebook", "KindleEBook", "Books", "booksAll",
        "PersonalDocument", "PersonalDocuments", "yourDocuments",
        "Audiobook", "Audible",
        "Magazine", "Subscription", "Periodical", "newsstand",
        "Comic",
      ];
      async function tryJsonApi() {
        const origin = location.origin;
        // Endpoint shapes Amazon has used (vary by year / TLD)
        const baseShapes = [
          `${origin}/hz/mycd/ajax/api/contentlist`,
          `${origin}/hz/mycd/api/contentlist`,
        ];
        // Pick the first base that returns JSON for at least one contentType
        let workingBase = null;
        for (const base of baseShapes) {
          try {
            const probe = `${base}/Ebook/dateDsc?pageNumber=1`;
            const r = await fetch(probe, {
              credentials: "include",
              headers: { "Accept": "application/json" },
            });
            const ct = (r.headers.get("content-type") || "").toLowerCase();
            if (r.ok && ct.includes("json")) {
              workingBase = base;
              break;
            }
          } catch (_) {}
        }
        if (!workingBase) return false;
        function findItems(obj, depth=0) {
          if (depth > 6 || !obj || typeof obj !== "object") return [];
          if (Array.isArray(obj)) {
            const sample = obj[0];
            if (sample && typeof sample === "object" &&
                (sample.asin || sample.ASIN || sample.title || sample.productName)) {
              return obj;
            }
            let acc = [];
            for (const x of obj) acc = acc.concat(findItems(x, depth+1));
            return acc;
          }
          let acc = [];
          for (const k of Object.keys(obj)) acc = acc.concat(findItems(obj[k], depth+1));
          return acc;
        }
        // Iterate every content type, every page
        for (const ct of CONTENT_TYPES) {
          for (let p = 1; p <= 200; p++) {
            const url = `${workingBase}/${ct}/dateDsc?pageNumber=${p}&surfaceType=Desktop`;
            let r;
            try {
              r = await fetch(url, {
                credentials: "include",
                headers: { "Accept": "application/json" },
              });
            } catch (e) { break; }
            if (!r.ok) break;
            const cth = (r.headers.get("content-type") || "").toLowerCase();
            if (!cth.includes("json")) break;
            let body;
            try { body = await r.json(); } catch (e) { break; }
            const found = findItems(body);
            if (!found.length) break;
            const before = out.length;
            for (const it of found) {
              out.push({
                asin:   String(it.asin || it.ASIN || ""),
                title:  String(it.title || it.productName || "").slice(0, 400),
                author: String(it.authors || it.author || "").slice(0, 200),
                kind:   String(it.contentType || it.itemType || it.kindleProductType || ct),
                source: `json_api:${ct}`,
              });
            }
            // If a page returned >0 rows but added nothing new, we've wrapped
            if (out.length === before) break;
            await new Promise(r => setTimeout(r, 200));
          }
        }
        return out.length > 0;
      }

      // ─── Strategy 0: CSRF-token POST to /hz/mycd/ajax (Amazon's REAL API) ──
      // The "Manage Your Content and Devices" page fetches its list via a
      // POST to /hz/mycd/ajax carrying a csrfToken + a JSON `data` param. This
      // is the actual endpoint (the GET shapes below were guesses that 404).
      // Works headless in a background tab because the user's cookies + the
      // page's csrfToken are both present. Bruno 2026-05-22.
      const _kindleDebug = { csrf: false, post_status: null, content_types_hit: [] };

      function _findCsrf() {
        // Token shows up in several places across Amazon's MYCD versions.
        const html = document.documentElement.innerHTML;
        let m =
          html.match(/csrfToken["'\s:=]+([A-Za-z0-9+/=_-]{16,})/) ||
          html.match(/"csrf"\s*:\s*"([^"]+)"/) ||
          html.match(/name=["']csrfToken["'][^>]*value=["']([^"']+)["']/);
        if (m) return m[1];
        const inp = document.querySelector("input[name='csrfToken'], #csrfToken");
        if (inp && inp.value) return inp.value;
        return null;
      }

      async function tryCsrfPostApi() {
        const csrf = _findCsrf();
        _kindleDebug.csrf = !!csrf;
        if (!csrf) return false;
        const origin = location.origin;
        // Amazon's contentType values. "Ebook" works; documents proved
        // elusive (Doc/PersonalDocument → 0). The captured items use
        // contentCategoryType="KindleEBook", so we try those category-style
        // names too, plus "allcontent" (the all-items filter from the
        // contentlist URL) and a NO-FILTER pass that returns everything.
        // Bruno 2026-05-22.
        // The MYCD contentlist URL slugs (discovered via contentlist_links)
        // are: booksAll, pdocs, freebies. The OwnershipData API contentType
        // for personal documents is "PDOC" (matches the /pdocs/ slug) — this
        // is the value that was missing. Bruno 2026-05-23.
        const TYPES = ["Ebook", "PDOC", "pdocs", "pdoc", "KindlePDoc", "KindleEBook",
                       "PrintReplica", "Freebie",
                       "KindlePersonalDocuments", "PersonalDocument", "Doc",
                       "Audiobook", "Audible", "Magazine", "Subscription",
                       "Periodical", "Comic", "allcontent", ""];
        const ORIGINS = ["Purchase", "Pottermore", "Prime", "KindleUnlimited",
                         "Sample", "Comixology", "PersonalSideLoad", "EBP", "Personal"];
        let anyHit = false;
        for (const ct of TYPES) {
          let startIndex = 0;
          for (let page = 0; page < 100; page++) {     // 100×100 = 10k ceiling
            const od = {
              sortOrder: "DESCENDING", sortIndex: "DATE",
              startIndex: startIndex, batchSize: 100,
              itemStatus: ["Active"],
              originType: ORIGINS,
            };
            if (ct) {
              od.contentType = ct;
            }
            const dataParam = JSON.stringify({ param: { OwnershipData: od }});
            let resp;
            try {
              resp = await fetch(`${origin}/hz/mycd/ajax`, {
                method: "POST",
                credentials: "include",
                headers: { "Content-Type": "application/x-www-form-urlencoded",
                           "Accept": "application/json" },
                body: `csrfToken=${encodeURIComponent(csrf)}&data=${encodeURIComponent(dataParam)}`,
              });
            } catch (e) { break; }
            _kindleDebug.post_status = resp.status;
            if (!resp.ok) break;
            let body;
            try { body = await resp.json(); } catch (e) { break; }
            const odResp = (body && (body.OwnershipData || body.GetItems || body)) || {};
            const list = odResp.items || odResp.Items || [];
            if (!list.length) break;
            anyHit = true;
            const label = ct || "(all)";
            if (!_kindleDebug.content_types_hit.includes(label))
              _kindleDebug.content_types_hit.push(`${label}:${list.length}`);
            for (const it of list) {
              const a = it.sortableAuthors || it.authors || it.author || "";
              out.push({
                asin:   String(it.asin || it.ASIN || ""),
                title:  String(it.title || it.productName || "(untitled)").slice(0, 400),
                author: String(Array.isArray(a) ? a.join(", ") : a).slice(0, 200),
                kind:   String(it.contentCategoryType || it.contentType || ct || "csrf"),
                acquired: String(it.acquiredDate || it.acquiredTime || ""),
                cover:  String(it.productImage || ""),
                source: `csrf_post:${label}`,
              });
            }
            if (list.length < 100) break;
            startIndex += 100;
            await new Promise(r => setTimeout(r, 200));
          }
        }
        return anyHit;
      }

      // Strategy 0: captured API responses. capture.js stashed every JSON
      // Amazon fetched (including the document categories the page loaded),
      // so we get the real items + the real contentType without guessing.
      const _capKindleDbg = { sample_item_keys: [], arrays_seen: [] };
      function fromCapturedKindle() {
        const caps = window.__egonCaptured || [];
        const seenSig = new Set();
        // An Amazon content item has a title-ish field. asin is common but
        // NOT guaranteed for sideloaded docs — so we accept title alone and
        // synthesise a key. We also record sample keys + array sizes so we
        // can see the real structure if extraction still misses.
        function titleOf(o) {
          return o.title || o.productName || o.contentName || o.displayName ||
                 (o.resource && o.resource.title) || "";
        }
        // Bruno 2026-05-22: do NOT require a title — some sideloaded docs
        // barely have one. Identify a library item by its METADATA instead
        // (asin + Amazon's real category/acquired fields, learned from the
        // captured sample: contentCategoryType, sortableAuthors, acquiredDate).
        function looksLikeContentItem(o) {
          if (!o || typeof o !== "object" || Array.isArray(o)) return false;
          const t = titleOf(o);
          const hasAsin = !!(o.asin || o.ASIN || o.contentId || o.id || o.key || t);
          const hasCat  = !!(o.contentCategoryType || o.contentType || o.category || o.udlCategory);
          const hasAcq  = !!(o.acquiredDate || o.acquiredTime);
          // an item needs an ASIN/ID/Title plus at least one library-ish signal
          return hasAsin && (hasCat || hasAcq || "sortableAuthors" in o || "readStatus" in o);
        }
        function authorOf(o) {
          const a = o.sortableAuthors || o.authors || o.author || "";
          if (Array.isArray(a)) return a.map(x => (typeof x === "string" ? x : (x.name || ""))).filter(Boolean).join(", ");
          return String(a);
        }
        function walk(obj, depth) {
          if (depth > 10 || !obj || typeof obj !== "object") return;
          if (Array.isArray(obj)) {
            if (obj.length && looksLikeContentItem(obj[0])) {
              _capKindleDbg.arrays_seen.push(obj.length);
              if (!_capKindleDbg.sample_item_keys.length)
                _capKindleDbg.sample_item_keys = Object.keys(obj[0]).slice(0, 25);
            }
            for (const x of obj) walk(x, depth + 1);
            return;
          }
          if (looksLikeContentItem(obj)) {
            const rawAsin = obj.asin || obj.ASIN || obj.contentId || obj.id || "";
            const t = titleOf(obj) || "(untitled)";
            const asin = rawAsin || "pdoc_" + t.toLowerCase().replace(/[^a-z0-9]/g, "").slice(0, 24);
            const k = asin || t.slice(0, 80);
            if (!seenSig.has(k)) {
              seenSig.add(k);
              out.push({
                asin:   String(asin),
                title:  String(t).slice(0, 400),
                author: authorOf(obj).slice(0, 200),
                kind:   String(obj.contentCategoryType || obj.contentType ||
                               obj.category || obj.udlCategory || "captured"),
                acquired: String(obj.acquiredDate || obj.acquiredTime || ""),
                cover:  String(obj.productImage || (obj.productDetail && obj.productDetail.productImage) || ""),
                source: "captured_api",
              });
            }
            return;
          }
          for (const k in obj) { try { walk(obj[k], depth + 1); } catch (e) {} }
        }
        for (const c of caps) walk(c.json, 0);
        return out.length;
      }

      // Try captured API first, then CSRF POST API, then paginated HTML walk, then DOM scrape
      try { fromCapturedKindle(); } catch (e) {}
      
      const onMycd = /\/hz\/mycd\//.test(location.href);
      if (onMycd) {
        try { await tryCsrfPostApi(); } catch (e) {}
        if (out.length === 0) {
          try { await tryJsonApi(); } catch (e) {}
        }
      }

      // Strategy A: paginated contentlist walk (HTML fallback)
      const onContentList = /\/hz\/mycd\/digital-console\/contentlist\//.test(location.href);
      if (onContentList) {
        // Strip any existing pageNumber so we can append our own
        const base = location.href.replace(/[?&]pageNumber=\d+/, "");
        const sep = base.includes("?") ? "&" : "?";
        for (let p = 1; p <= 200; p++) {  // sanity cap: 200 pages × 25 ~= 5000 items
          let html;
          try {
            const url = `${base}${sep}pageNumber=${p}`;
            // First page: prefer the already-rendered DOM (faster + identical)
            if (p === 1) {
              html = document.documentElement.outerHTML;
            } else {
              const r = await fetch(url, {
                credentials: "include",
                headers: { "Accept": "text/html,application/xhtml+xml" },
              });
              if (!r.ok) break;
              html = await r.text();
            }
          } catch (e) { break; }
          const doc = new DOMParser().parseFromString(html, "text/html");
          const parsedItems = parseDoc(doc);
          if (!parsedItems.length) break;
          const before = out.length;
          merge(parsedItems);
          // If the page parsed >0 rows but added 0 new items, we've wrapped.
          if (out.length === before) break;
          // Be polite to Amazon
          if (p > 1) await new Promise(r => setTimeout(r, 250));
        }
      } else if (!onMycd) {
        // Strategy B: single-page DOM scrape (notebook / kindle-library / etc.)
        try { merge(parseDoc(document)); } catch (e) {}
      }

      // Deduplicate and merge metadata
      const dedup = new Map();
      for (const it of out) {
        if (!it || !it.title) continue;
        const k = it.asin || `${it.title.trim().toLowerCase()}|${(it.author || "").trim().toLowerCase()}`;
        if (!dedup.has(k)) {
          dedup.set(k, it);
        } else {
          const existing = dedup.get(k);
          if (!existing.cover && it.cover) existing.cover = it.cover;
          if (!existing.acquired && it.acquired) existing.acquired = it.acquired;
          if (!existing.kind && it.kind) existing.kind = it.kind;
          if (!existing.asin && it.asin) existing.asin = it.asin;
        }
      }

      const finalItems = Array.from(dedup.values());
      return {
        ts: Date.now(),
        url: location.href,
        count: finalItems.length,
        items: finalItems,
        strategy: onContentList ? "api+dom_walk" : "api_only",
        _debug: {
          ...(_kindleDebug || {}),
          onMycd,
          onContentList,
          total_raw: out.length
        }
      };
    },
  },
  {
    // Paperpile library — matches ANY paperpile.com page.
    default_url: "https://app.paperpile.com/my-library/all",
    test: (url) => /paperpile\.com\//.test(url),
    endpoint: "http://127.0.0.1:8000/api/v1/paperpile/library",
    extract: async () => {
      // ─── Strategy 1: IndexedDB scan, with POLL + nested unwrap ────────────
      // The critical fix (Bruno 2026-05-22): when this runs in a freshly-opened
      // BACKGROUND tab, Paperpile's app is still loading data into IndexedDB
      // asynchronously — so a one-shot read finds an empty DB. We POLL the DB
      // for up to ~40s, re-reading until the record count stabilises.
      //
      // We also UNWRAP nested records: Paperpile may store the reference under
      // a wrapper key (doc / value / data / item / paper / reference), so we
      // probe those before giving up on an object.
      const _idbDebug = { stores: [], sample_keys: [] };

      function _unwrap(o) {
        if (!o || typeof o !== "object") return null;
        // direct hit?
        if (o.title || o.Title || o.titleText) return o;
        // common wrapper keys
        for (const k of ["doc", "value", "data", "item", "paper", "reference", "ref", "_doc"]) {
          if (o[k] && typeof o[k] === "object" &&
              (o[k].title || o[k].Title || o[k].titleText)) {
            return o[k];
          }
        }
        return null;
      }

      function _toRecord(raw, dbName, storeName) {
        const o = _unwrap(raw);
        if (!o) return null;
        const t = o.title || o.Title || o.titleText;
        if (!t || typeof t !== "string" || t.length < 4) return null;
        if (["collection", "folder", "tag", "label"].includes(o.kind)) return null;
        return {
          id:      String(o._id || o.id || o.guid || ""),
          title:   String(t).slice(0, 400),
          authors: String(
            Array.isArray(o.authors)
              ? o.authors.map(a => (typeof a === "string" ? a : (a.last || a.family || a.name || "")))
                  .filter(Boolean).join(", ")
              : (o.authors || o.author || o.authorsText || "")
          ).slice(0, 300),
          year:    String(o.year || o.publishedYear ||
                          (o.published && o.published.year) || "").slice(0, 6),
          doi:     String(o.doi || o.DOI || ""),
          journal: String(o.journal || o.journalfull || o.journal_abbrev || ""),
          source:  `idb:${dbName}/${storeName}`,
        };
      }

      async function _readAllStores() {
        const out = [];
        _idbDebug.stores = [];
        const dbs = (await indexedDB.databases?.()) || [];
        for (const meta of dbs) {
          let db;
          try {
            const req = indexedDB.open(meta.name);
            db = await new Promise((res, rej) => {
              req.onsuccess = () => res(req.result);
              req.onerror   = () => rej(req.error);
            });
          } catch (e) { continue; }
          for (const storeName of Array.from(db.objectStoreNames)) {
            try {
              const tx = db.transaction(storeName, "readonly");
              const store = tx.objectStore(storeName);
              const all = await new Promise((res) => {
                const r = store.getAll();
                r.onsuccess = () => res(r.result || []);
                r.onerror = () => res([]);
              });
              _idbDebug.stores.push(`${meta.name}/${storeName}=${all.length}`);
              if (all.length && _idbDebug.sample_keys.length < 20 && all[0] && typeof all[0] === "object") {
                _idbDebug.sample_keys = Object.keys(all[0]).slice(0, 20);
              }
              for (const raw of all) {
                const rec = _toRecord(raw, meta.name, storeName);
                if (rec) out.push(rec);
              }
            } catch (_) { /* skip stores we can't read */ }
          }
          try { db.close(); } catch (_) {}
        }
        return out;
      }

      async function fromIndexedDB() {
        // Poll: re-read until the count stops growing for 2 consecutive reads,
        // or 40s elapses. This rides out Paperpile's async data load.
        let best = [];
        let stable = 0;
        for (let i = 0; i < 20; i++) {        // 20 × 2s = 40s ceiling
          const cur = await _readAllStores();
          if (cur.length > best.length) {
            best = cur;
            stable = 0;
          } else if (best.length > 0) {
            stable += 1;
            if (stable >= 2) break;           // count settled → done
          }
          if (best.length === 0) {
            await new Promise(r => setTimeout(r, 2000));  // nothing yet — wait
          } else {
            await new Promise(r => setTimeout(r, 1500));
          }
        }
        return best;
      }

      // ─── Strategy 2: DOM scrape, also more aggressive ────────────────────
      function fromDOM() {
        const SELECTORS = [
          // Paperpile current UI patterns
          "paperpile-ref", "paperpile-row", "paperpile-paper",
          "[data-paper-id]", "[data-ref-id]", "[data-id]",
          // Angular Material rows
          "mat-row[data-id], mat-row[data-paper-id], mat-row",
          // Generic row patterns
          "[data-test-id*='paper'], [data-test-id*='ref']",
          "[data-testid*='paper'], [data-testid*='ref']",
          "div[role='row']", "div[role='listitem']",
          // Versions where the row is just an <a> to /paper/<id>
          "a[href*='/paper/']", "a[href*='/ref/']",
          // Class-name patterns we've seen across versions
          ".ref-row", ".reference-row", ".paper-row", ".paperRow",
          ".ref-item", ".reference-item",
          // Fallback: tablebody rows in the main library view
          ".tablebody tr, .tablebody-row, .tbl-row",
        ];
        const found = new Map();   // key → row obj (de-dup)
        for (const sel of SELECTORS) {
          let nodes;
          try { nodes = document.querySelectorAll(sel); }
          catch (_) { continue; }
          nodes.forEach((row) => {
            // Try every plausible attribute name for the row's ID
            const id = row.getAttribute?.("data-id")
                    || row.getAttribute?.("data-paper-id")
                    || row.getAttribute?.("data-ref-id")
                    || row.getAttribute?.("data-testid")
                    || (row.href ? row.href.match(/\/(?:paper|ref)\/([\w-]+)/)?.[1] : "")
                    || "";
            // Find the title — broad probe + fallback to .innerText longest line
            let titleEl = row.querySelector(
              ".title, .ref-title, .paper-title, [class*='title' i], h1, h2, h3"
            );
            let title = (titleEl ? titleEl.innerText : "").trim();
            if (!title) {
              // Heuristic: the longest text-line in the row that looks like a sentence.
              const lines = (row.innerText || "").split("\n")
                .map(s => s.trim()).filter(s => s.length > 15 && /[A-Za-z]{3}/.test(s));
              lines.sort((a, b) => b.length - a.length);
              title = lines[0] || "";
            }
            if (!title) return;
            const key = id || title.slice(0, 80);
            if (found.has(key)) return;
            const authorsEl = row.querySelector(
              ".author, .authors, .ref-authors, [class*='author' i]"
            );
            const yearEl = row.querySelector(
              ".year, .date, .ref-year, [class*='year' i], [class*='date' i]"
            );
            const doiEl = row.querySelector("a[href*='doi.org']");
            let added = "";
            try {
              const rowText = row.innerText || "";
              const dateMatch = rowText.match(/\b\d{1,2}\s+(?:de\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|fev|abr|mai|ago|set|out|dez)[a-z]*\b/i)
                             || rowText.match(/\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|fev|abr|mai|ago|set|out|dez)[a-z]*\s+\d{1,2}\b/i)
                             || rowText.match(/\b(?:today|yesterday|hoje|ontem)\b/i);
              if (dateMatch) added = dateMatch[0];
            } catch (e) {}

            found.set(key, {
              id: id,
              title: title.slice(0, 400),
              authors: (authorsEl ? authorsEl.innerText : "").trim().slice(0, 300),
              year:    (yearEl ? yearEl.innerText : "").trim().slice(0, 6),
              doi:     doiEl ? doiEl.href : "",
              added:   added,
              source:  `dom:${sel}`,
            });
          });
          // Cap at 20k so a runaway selector can't OOM us
          if (found.size > 20000) break;
        }
        return Array.from(found.values());
      }

      // ─── Strategy 3: scroll + innerText regex ────────────────────────────
      // Paperpile virtualises rows — only ~30 in the DOM at a time. Scroll
      // through the scroller, collecting innerText snapshots, then regex-
      // parse the combined text. Each ref renders as:
      //     <Title> (long line)
      //     <icons / whitespace>
      //     <Author> (short "Surname F" or "Surname F, Other O")
      //     <URL> (line starting with www. or http)
      //     · <Year (4 digits)?> · <Type>
      //     <Format e.g. PDF / HTML>
      async function fromScrollAndParse() {
        // Find the REAL virtual-scroll container. Bruno 2026-05-20: my
        // first attempt fixed-selectored a likely candidate and missed
        // Paperpile's actual container (which has anonymised classnames).
        // Better strategy: scan every element, pick the tallest one whose
        // computed overflow is scroll/auto AND whose scrollHeight >
        // clientHeight by a wide margin. That's almost certainly the list.
        function findScroller() {
          let best = null;
          let bestRatio = 1.2;  // require at least 20% more content than viewport
          const els = document.querySelectorAll("*");
          for (const el of els) {
            try {
              const cs = getComputedStyle(el);
              const ov = (cs.overflow || "") + (cs.overflowY || "");
              if (!/auto|scroll/.test(ov)) continue;
              if (el.clientHeight < 200) continue;   // too small to be the list
              const ratio = el.scrollHeight / Math.max(el.clientHeight, 1);
              if (ratio > bestRatio) {
                bestRatio = ratio;
                best = el;
              }
            } catch (_) {}
          }
          return best || document.scrollingElement || document.documentElement;
        }
        const scrollEl = findScroller();
        const seenTitles = new Set();
        const out = [];
        const URL_RE = /^(?:www\.|https?:\/\/)\S+/;
        const YEAR_RE = /^\d{4}$/;
        const FORMAT_RE = /^(PDF|HTML|EPUB|DOCX)$/i;
        // A real title/author line has plenty of letters; Paperpile's
        // icon/whitespace separator lines are mostly non-letters and must be
        // skipped. Bruno 2026-05-22: the old parser grabbed those as titles.
        function letterRatio(s) {
          const letters = (s.match(/[A-Za-zÀ-ÿ]/g) || []).length;
          return letters / Math.max(s.length, 1);
        }
        function looksLikeText(s) {
          return s.length >= 3 && letterRatio(s) > 0.55;
        }
        function harvestCurrentText() {
          const text = document.body.innerText || "";
          const lines = text.split(/\n+/).map(s => s.trim()).filter(Boolean);
          for (let i = 0; i < lines.length; i++) {
            if (!URL_RE.test(lines[i])) continue;
            const url = lines[i];
            // Collect the candidate text lines above the URL (skipping junk/
            // icon lines), nearest-first. The author is the SHORT one; the
            // title is the LONG one.
            const above = [];
            for (let j = i - 1; j >= Math.max(0, i - 6) && above.length < 4; j--) {
              const cand = lines[j];
              if (!cand || URL_RE.test(cand)) break;
              if (!looksLikeText(cand)) continue;   // skip icon/whitespace junk
              above.push(cand);
            }
            // above[0] is nearest the URL. Author = first short line; title =
            // first line ≥ 20 chars.
            let author = "", title = "";
            for (const cand of above) {
              if (!author && cand.length < 80 && /[A-ZÀ-Ý]/.test(cand[0] || "")
                  && cand.length < 60) {
                author = cand;
              }
            }
            for (const cand of above) {
              if (cand !== author && cand.length >= 20) { title = cand; break; }
            }
            if (!title) {
              // fall back to the longest above-line
              const sorted = [...above].sort((a, b) => b.length - a.length);
              title = sorted[0] || "";
            }
            if (!title || title.length < 12) continue;
            const key = title.slice(0, 80);
            if (seenTitles.has(key)) continue;
            seenTitles.add(key);
            // Walk forward up to 4 lines for year + type
            let year = "", type = "", format = "", added = "";
            for (let j = i + 1; j <= Math.min(lines.length - 1, i + 6); j++) {
              const cand = lines[j];
              if (cand === "·") continue;
              if (YEAR_RE.test(cand)) { year = cand; continue; }
              if (FORMAT_RE.test(cand)) { format = cand; continue; }
              
              const dateMatch = cand.match(/\b\d{1,2}\s+(?:de\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|fev|abr|mai|ago|set|out|dez)[a-z]*\b/i)
                             || cand.match(/\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|fev|abr|mai|ago|set|out|dez)[a-z]*\s+\d{1,2}\b/i)
                             || cand.match(/\b(?:today|yesterday|hoje|ontem)\b/i);
              if (dateMatch && !added) {
                added = dateMatch[0];
              }
              
              if (cand.length < 40 && !URL_RE.test(cand) && !dateMatch) {
                if (!type) type = cand;
              }
            }
            out.push({
              id: "", title: title.slice(0, 400),
              authors: author.slice(0, 300),
              year: year, doi: "", journal: type,
              url: url, format: format,
              added: added,
              source: "innertext_regex",
            });
          }
        }
        // Scroll through the list. Paperpile's row height ≈ 70px, viewport
        // shows ~12 rows; jumping a viewport-height each pass + waiting
        // 300 ms for new rows to render.
        harvestCurrentText();
        const startScroll = scrollEl.scrollTop;
        const step = Math.max(400, (scrollEl.clientHeight || 600) - 100);
        let prevCount = out.length;
        let stillCount = 0;
        for (let s = 0; s < 1000; s++) {  // sanity cap: 1000 pages of scrolling
          scrollEl.scrollTop = scrollEl.scrollTop + step;
          window.dispatchEvent(new Event("scroll"));
          await new Promise(r => setTimeout(r, 280));
          harvestCurrentText();
          if (out.length === prevCount) {
            stillCount += 1;
            if (stillCount > 4) break;   // 4 consecutive empty steps = done
          } else {
            stillCount = 0;
            prevCount = out.length;
          }
          // Stop if we've reached the bottom AND nothing new came in
          if (Math.abs(scrollEl.scrollTop + scrollEl.clientHeight - scrollEl.scrollHeight) < 4) {
            // bottom — wait once more for tail to render then break
            await new Promise(r => setTimeout(r, 400));
            harvestCurrentText();
            break;
          }
        }
        // Restore original scroll position
        try { scrollEl.scrollTop = startScroll; } catch (e) {}
        return out;
      }

      // ─── Strategy 0 (PREFERRED): captured API responses ──────────────────
      // capture.js wraps fetch/XHR at document_start, so Paperpile's own
      // library fetches are stashed on window.__egonCaptured as clean JSON.
      // We scroll to trigger Paperpile to fetch every batch, then harvest
      // the captures — full fidelity, no DOM/innerText guessing.
      function fromCaptured() {
        const caps = window.__egonCaptured || [];
        const out = [];
        const seen = new Set();
        function isRef(o) {
          const t = o && (o.title || o.Title || o.titleText);
          if (!t || typeof t !== "string" || t.length < 4) return false;
          // a reference has at least one of these companions
          return !!(o.authors || o.author || o.year || o.published ||
                    o.doi || o.DOI || o.journal || o.journalfull || o._id);
        }
        function walk(obj, depth) {
          if (depth > 9 || !obj || typeof obj !== "object") return;
          if (Array.isArray(obj)) { for (const x of obj) walk(x, depth + 1); return; }
          if (isRef(obj)) {
            const t = obj.title || obj.Title || obj.titleText;
            const key = (obj._id || obj.id || t).toString().slice(0, 90);
            if (!seen.has(key)) {
              seen.add(key);
              out.push({
                id:      String(obj._id || obj.id || ""),
                title:   String(t).slice(0, 400),
                authors: String(
                  Array.isArray(obj.authors)
                    ? obj.authors.map(a => (typeof a === "string" ? a : (a.last || a.family || a.name || ""))).filter(Boolean).join(", ")
                    : (obj.authors || obj.author || "")
                ).slice(0, 300),
                year:    String(obj.year || (obj.published && obj.published.year) || "").slice(0, 6),
                doi:     String(obj.doi || obj.DOI || ""),
                journal: String(obj.journal || obj.journalfull || ""),
                source:  "captured_api",
              });
            }
            return;  // don't descend into a ref's own fields
          }
          for (const k in obj) { try { walk(obj[k], depth + 1); } catch (e) {} }
        }
        for (const c of caps) walk(c.json, 0);
        return out;
      }

      // Trigger Paperpile to load every batch by scrolling, capturing as we go.
      async function captureViaScroll() {
        const scrollEl =
          document.querySelector(".tablebody, [class*='scroll']") ||
          document.scrollingElement || document.documentElement;
        let best = fromCaptured();
        let stable = 0;
        for (let s = 0; s < 400; s++) {
          try { scrollEl.scrollTop += Math.max(500, (scrollEl.clientHeight || 600)); } catch (e) {}
          window.dispatchEvent(new Event("scroll"));
          await new Promise(r => setTimeout(r, 350));
          const cur = fromCaptured();
          if (cur.length > best.length) { best = cur; stable = 0; }
          else { stable += 1; if (stable >= 6) break; }
          try {
            if (Math.abs(scrollEl.scrollTop + scrollEl.clientHeight - scrollEl.scrollHeight) < 4 && stable >= 3) break;
          } catch (e) {}
        }
        return best;
      }

      let items = [];
      let strategyUsed = "";
      // 0. captured API (best) — scroll to pull every batch
      try {
        items = await captureViaScroll();
        if (items.length) strategyUsed = "captured_api";
      } catch (e) {}
      // 1. IndexedDB (empty for Paperpile, but cheap to try)
      if (!items.length) {
        try {
          items = await fromIndexedDB();
          if (items.length) strategyUsed = "indexeddb";
        } catch (e) {}
      }
      // 2. DOM scrape
      if (!items.length) {
        items = fromDOM();
        if (items.length) strategyUsed = "dom";
      }
      // 3. innerText scroll-parse (last resort)
      if (items.length < 100) {
        try {
          const scrolled = await fromScrollAndParse();
          if (scrolled.length > items.length) {
            items = scrolled;
            strategyUsed = "scroll_innertext";
          }
        } catch (e) {}
      }

      // ALWAYS include diagnostic info — even on success — so we can iterate
      // on selectors if Paperpile changes their UI in the future. Bruno 2026-05-20.
      const idbNames = (await indexedDB.databases?.())?.map(d => d.name) || [];
      const knownTags = new Set();
      document.querySelectorAll("[data-id], [data-paper-id], [data-ref-id], mat-row, paperpile-ref, paperpile-row")
        .forEach(el => knownTags.add(el.tagName.toLowerCase() + (el.className ? "." + el.className.split(" ")[0] : "")));
      return {
        ts: Date.now(),
        url: location.href,
        count: items.length,
        items: items,
        strategy: strategyUsed || "none",
        _debug: {
          capture_installed: !!window.__egonCaptureInstalled,
          captured_count: (window.__egonCaptured || []).length,
          captured_urls: (window.__egonCaptured || []).map(c => c.url).slice(0, 40),
          idb_names: idbNames,
          idb_stores: _idbDebug.stores,          // "<db>/<store>=<count>" per store
          idb_sample_keys: _idbDebug.sample_keys, // keys of a sample record
          dom_row_candidates: Array.from(knownTags).slice(0, 30),
          body_sample: (document.body.innerText || "").slice(0, 1000),
          h1_h2_h3_count: document.querySelectorAll("h1,h2,h3").length,
          link_to_paper_count: document.querySelectorAll("a[href*='/paper/']").length,
        },
      };
    },
  },
];


// Per-source guards so a harvest can't re-trigger itself. Bruno 2026-05-29:
// reload_before reloads the tab, which fires tabs.onUpdated 'complete', which
// called harvestIfMatch again → reload → loop (TV Time "kept reloading").
const _harvestInFlight = new Set();   // endpoint → currently running
const _harvestLastMs = {};            // endpoint → last completion ms
const _HARVEST_COOLDOWN_MS = 5 * 60 * 1000;

async function harvestIfMatch(tab) {
  if (!tab || !tab.id || !tab.url) return;
  const h = HARVESTERS.find(x => x.test(tab.url));
  if (!h) return;
  const key = h.endpoint;
  // Already harvesting this source (e.g. the reload's own onUpdated re-entry)
  // OR harvested it very recently → bail. THIS breaks the reload loop.
  if (_harvestInFlight.has(key)) return;
  if (Date.now() - (_harvestLastMs[key] || 0) < _HARVEST_COOLDOWN_MS) return;
  _harvestInFlight.add(key);
  try {
    // Bruno 2026-05-29: TV Time's Flutter app serves cached data on a normal
    // visit, so it often makes NO /sidecar call — and the x-api-key is only
    // observable on a live call, so the harvest stayed gated out (0 shows).
    // A cache-bypassing reload makes the app re-boot and re-fetch so we can
    // snapshot the auth. But ONLY do this until we've captured the auth once —
    // after that, reloading every visit is pointless and just churns the tab.
    if (h.reload_before) {
      let haveAuth = false;
      try {
        const s = await chrome.storage.local.get("tvtime_auth");
        haveAuth = !!(s && s.tvtime_auth && s.tvtime_auth.headers &&
                      s.tvtime_auth.headers["x-api-key"]);
      } catch (e) {}
      if (!haveAuth) {
        try { await chrome.tabs.reload(tab.id, { bypassCache: true }); } catch (e) {}
      }
    }
    // Some harvesters (TV Time / Flutter) need the app to fully boot first, so
    // it fires its authenticated API calls and our webRequest listener can
    // snapshot the auth headers before we extract. Honour an optional delay.
    if (h.pre_delay_ms) await new Promise(r => setTimeout(r, h.pre_delay_ms));
    // Pass captured auth into the extractor when the harvester needs it. The
    // extractor runs in MAIN world (no chrome.* access), so we hand it the auth
    // we captured in the background here. Bruno 2026-05-23.
    let extraArg = null;
    if (h.needs_tvtime_auth) {
      try {
        const s = await chrome.storage.local.get("tvtime_auth");
        extraArg = (s && s.tvtime_auth) || _tvtimeAuth || null;
      } catch (e) { extraArg = _tvtimeAuth || null; }
    }
    // Run the extractor in the page's main world so it can see the
    // rendered DOM that Vue/React produced (and use the page's cookies/JWT).
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: h.extract,
      world: "MAIN",
      args: [extraArg],
    });
    const data = results && results[0] && results[0].result;
    if (!data) return;
    // POST even when items is EMPTY. Previously we returned early on an empty
    // harvest, which meant the diagnostic `_debug` payload (only populated when
    // count===0) never reached the server — so every failed harvest was
    // invisible and I was diagnosing blind. The server keeps the prior library
    // when the incoming items are empty, so this can't clobber good data; it
    // only lets us SEE why a harvest came back empty. Bruno 2026-05-23.
    await fetch(h.endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    await chrome.storage.local.set({
      [`harvest_${h.endpoint}`]: {
        ts: Date.now(),
        count: data.count,
        url: data.url,
      },
    });
  } catch (e) {
    await chrome.storage.local.set({
      last_harvest_error: `${tab.url}: ${String(e).slice(0, 200)}`,
    });
  } finally {
    // Release the in-flight lock + stamp the cooldown so the reload's own
    // onUpdated 'complete' (and rapid tab events) can't re-trigger a harvest.
    _harvestLastMs[key] = Date.now();
    _harvestInFlight.delete(key);
  }
}
