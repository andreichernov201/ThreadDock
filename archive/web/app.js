import { COPY, fill, translations } from "./texts.js";
import { createDemoApi } from "./demo.js";

const tg = window.Telegram?.WebApp;
const demo = new URLSearchParams(location.search).get("demo") === "1";

const icons = {
  filter: '<path d="M3 4h18l-7 8v7l-4-2v-5z"/>',
  feed:
    '<path d="M5 4h14l3 10v5a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-5z"/><path d="M2 14h6l2 3h4l2-3h6"/>',
  chats:
    '<path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8A8.5 8.5 0 0 1 12.5 20a8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8A8.5 8.5 0 0 1 8.7 3.9a8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5z"/>',
  gallery:
    '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1.5"/><path d="m21 15-6-6L3 21"/>',
  stats: '<path d="M4 20h16M7 16V9M12 16V4M17 16v-5"/>',
  settings:
    '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="15" cy="17" r="3"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  edit: '<path d="m16 3 5 5L9 20H4v-5zM13 6l5 5"/>',
  trash: '<path d="M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7M14 10v7"/>',
  star:
    '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9z"/>',
  lock:
    '<rect x="5" y="10" width="14" height="11" rx="3"/><path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v4h16v-4"/>',
  arrow: '<path d="m9 5 7 7-7 7"/>',
  back: '<path d="m14 5-7 7 7 7"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  send: '<path d="m22 2-7 20-4-9-9-4zM22 2 11 13"/>',
  bell: '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/>',
  file:
    '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M8 13h8M8 17h5"/>',
  audio:
    '<path d="M9 18V5l12-2v13M9 5l12-2"/><ellipse cx="6" cy="18" rx="3" ry="3"/><ellipse cx="18" cy="16" rx="3" ry="3"/>',
  video:
    '<rect x="3" y="5" width="14" height="14" rx="3"/><path d="m17 10 5-3v10l-5-3"/>',
  shield: '<path d="m12 2 9 4v6c0 5-9 10-9 10S3 17 3 12V6zM8 12l3 3 5-6"/>',
  key: '<circle cx="8" cy="15" r="5"/><path d="m12 11 9-9m-3 3 3 3m-6 0 3 3"/>',
  more:
    '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
};
const icon = (name) =>
  `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${
    icons[name] || icons.file
  }</svg>`;
const e = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[c]),
  );
const S = {
  view: "feed",
  chat: 0,
  mode: "all",
  query: "",
  items: [],
  cursor: null,
  chats: [],
  stats: null,
  meta: null,
  owner: null,
  lang: "ru",
  locked: false,
  token: "",
  csrf: "",
  user: null,
  urls: new Map(),
  lastActivity: Date.now(),
  activityPing: 0,
  busy: false,
  newEvents: 0,
  epoch: 0,
  statsDays: "30",
  statsStart: "",
  statsEnd: "",
  showSearch: false,
  showChatFilter: false,
  counts: {},
};
const t = (value) => S.lang === "en" ? (translations[value] || value) : value;
const fmt = (number) =>
  new Intl.NumberFormat(S.lang === "en" ? "en-US" : "ru-RU").format(
    number || 0,
  );
const time = (stamp, full = false) =>
  stamp
    ? new Intl.DateTimeFormat(
      S.lang === "en" ? "en-GB" : "ru-RU",
      full
        ? { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }
        : { hour: "2-digit", minute: "2-digit" },
    ).format(new Date(stamp * 1000))
    : "";
const date = (stamp) =>
  new Intl.DateTimeFormat(S.lang === "en" ? "en-GB" : "ru-RU", {
    day: "numeric",
    month: "long",
  }).format(new Date(stamp * 1000));
const initials = (name) =>
  String(name || "?").trim().split(/\s+/).slice(0, 2).map((word) => word[0])
    .join("").toUpperCase();
const size = (bytes) =>
  bytes >= 1e6
    ? `${(bytes / 1e6).toFixed(1)} MB`
    : `${Math.ceil(bytes / 1000)} KB`;
const root = document.querySelector("#app");
const modal = document.querySelector("#modal");
let toastTimer, searchTimer;

class Client {
  constructor() {
    this.demo = demo ? createDemoApi() : null;
  }
  async request(path, options = {}) {
    const epoch = S.epoch;
    if (this.demo) return this.demo.request(path, options);
    const headers = {
      Authorization: `Bearer ${S.token}`,
      "X-CSRF-Token": S.csrf,
      ...options.headers,
    };
    let body = options.body;
    if (body && !(body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(body);
    }
    const response = await fetch(path, {
      method: options.method || "GET",
      headers,
      body,
      credentials: "omit",
      cache: "no-store",
    });
    const data = await response.json();
    if (epoch !== S.epoch) {
      throw new DOMException("Stale response", "AbortError");
    }
    if (!response.ok) {
      if (response.status === 423) showLock();
      if (response.status === 401) {
        clearPrivate();
        showLogin();
      }
      const detail = data.operation_id
        ? " · " + fill(t(COPY.OPERATION_ID), data.operation_id)
        : "";
      throw new Error((data.error || "Request failed") + detail);
    }
    return data;
  }
  async blob(media) {
    const epoch = S.epoch;
    if (demo) {
      return new Blob([
        '<svg xmlns="http://www.w3.org/2000/svg" width="900" height="600"><rect width="900" height="600" fill="#e3e2fb"/><circle cx="680" cy="160" r="75" fill="#fff0c4"/><path d="M0 500 250 240 480 470 700 300 900 510V600H0" fill="#aaa5df"/><path d="m0 540 380-180 360 240H0" fill="#7d79b8"/></svg>',
      ], { type: "image/svg+xml" });
    }
    const response = await fetch(`/api/media/${media.id}/file`, {
      headers: { Authorization: `Bearer ${S.token}` },
      cache: "no-store",
      credentials: "omit",
    });
    if (!response.ok) {
      const result = await response.json();
      if (response.status === 423) showLock();
      throw new Error(result.error);
    }
    const blob = await response.blob();
    if (epoch !== S.epoch) {
      throw new DOMException("Stale response", "AbortError");
    }
    return blob;
  }
}
const api = new Client();
function toast(text) {
  const box = document.querySelector("#toast");
  box.textContent = t(text);
  box.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => box.classList.remove("show"), 4000);
}
function theme() {
  document.documentElement.dataset.theme = "dark";
  tg?.setHeaderColor?.("#151517");
  tg?.setBackgroundColor?.("#151517");
}
function clearPrivate() {
  S.epoch++;
  for (const url of S.urls.values()) URL.revokeObjectURL(url);
  S.urls.clear();
  S.items = [];
  S.chats = [];
  S.stats = null;
  S.openMessage = null;
  S.owner = null;
  S.meta = null;
  if (modal.open) modal.close();
  document.querySelector("#modal-content").textContent = "";
}
function showLogin(
  message = COPY.OPEN_THIS_APP_USING_THE_BUTTON_IN_THREADDOCK,
) {
  root.innerHTML =
    `<div class="boot"><div class="lock-card panel"><div class="brand-symbol">t<span>·</span></div><h1>ThreadDock</h1><p>${
      e(t(message))
    }</p><p class="subtitle">${
      e(t(COPY.SECURE_TELEGRAM_SIGN_IN))
    }</p><a class="button subtle" href="?demo=1">${
      e(t(COPY.VIEW_DEMO))
    }</a></div></div>`;
}
function showLock() {
  S.locked = true;
  clearPrivate();
  root.innerHTML =
    `<div class="boot"><form id="unlock-form" class="lock-card panel"><div class="empty-icon">${
      icon("lock")
    }</div><h1>${e(t(COPY.YOUR_ARCHIVE_IS_LOCKED))}</h1><p class="subtitle">${
      e(t(COPY.ENTER_YOUR_PIN_TO_CONTINUE))
    }</p><div class="field"><label for="unlock-pin">PIN</label><input id="unlock-pin" name="pin" type="password" inputmode="numeric" autocomplete="off" pattern="[0-9]{6,12}" minlength="6" maxlength="12" required></div><button class="button primary" type="submit">${
      e(t(COPY.UNLOCK))
    }</button>${
      tg?.BiometricManager
        ? `<button type="button" class="button subtle" data-action="unlock-bio">${
          e(t(COPY.USE_BIOMETRICS))
        }</button>`
        : ""
    }</form></div>`;
}
async function bootstrap() {
  S.meta = await api.request("/api/session");
  if (S.meta.locked) {
    showLock();
    return;
  }
  S.locked = false;
  S.owner = S.meta.owner;
  S.lang = S.owner.language;
  S.lastActivity = Date.now();
  S.chats = (await api.request("/api/chats")).items;
  await load(false);
  if (S.pendingMessage) {
    const id = S.pendingMessage;
    S.pendingMessage = 0;
    await openMessage(id).catch(fail);
  }
}
async function boot() {
  theme();
  tg?.ready();
  tg?.expand();
  try {
    const auth = await api.request("/api/auth", {
      method: "POST",
      body: { init_data: tg?.initData || "" },
    });
    S.token = auth.token;
    S.csrf = auth.csrf;
    S.user = auth.user;
    S.lang = auth.language || "ru";
    S.locked = auth.locked;
    const initial = Number(new URLSearchParams(location.search).get("chat"));
    if (Number.isSafeInteger(initial) && initial > 0) {
      S.chat = initial;
      S.view = "feed";
    }
    const focused = Number(new URLSearchParams(location.search).get("message"));
    S.pendingMessage = Number.isSafeInteger(focused) && focused > 0
      ? focused
      : 0;
    await bootstrap();
  } catch (error) {
    showLogin(error.message);
  }
}
function navButtons() {
  return [["feed", COPY.ACTIVITY], [
    "gallery",
    COPY.MEDIA,
  ], [
    "stats",
    COPY.STATISTICS,
  ], ["settings", COPY.SETTINGS]].map(([key, label]) =>
    `<button data-action="nav" data-view="${key}" class="${
      S.view === key ? "active" : ""
    }">${icon(key)}<span>${e(t(label))}</span></button>`
  ).join("");
}
function shell(content) {
  if (S.locked || !S.owner) return;
  const title = {
    feed: COPY.ACTIVITY,
    chats: COPY.ARCHIVE,
    gallery: COPY.MEDIA,
    stats: COPY.STATISTICS,
    settings: COPY.SETTINGS,
  }[S.view] || COPY.ARCHIVE;
  root.innerHTML = `<div class="shell"><aside class="sidebar">
    <div class="brand">${icon("feed")}<span>ThreadDock</span></div>
    <nav class="nav">${navButtons()}</nav>
    <button class="side-lock text-button" data-action="lock">${icon("lock")}${
    e(t(COPY.LOCK_APP))
  }</button>
    </aside><main class="main">${demo ? COPY.DEMO_BANNER : ""}
    <header class="topbar"><h1>${e(t(title))}</h1><div class="top-actions">${
    S.view === "feed"
      ? `<button class="icon-button round ${
        S.showChatFilter ? "on" : ""
      }" data-action="feed-filters" aria-label="${
        e(t(COPY.FILTERS))
      }" aria-expanded="${S.showChatFilter}">${
        icon("filter")
      }</button><button class="icon-button round ${
        S.showSearch ? "on" : ""
      }" data-action="feed-search" aria-label="${
        e(t(COPY.SEARCH))
      }" aria-expanded="${S.showSearch}">${icon("search")}</button>`
      : `<button class="icon-button" data-action="refresh" aria-label="${
        e(t(COPY.REFRESH))
      }">${
        icon("feed")
      }</button><button class="icon-button" data-action="export" aria-label="${
        e(t(COPY.EXPORT))
      }">${icon("download")}</button>`
  }</div></header>
    ${S.view !== "settings" ? statusBanner() : ""}${content}</main>
    <nav class="bottom-nav">${navButtons()}</nav></div>`;
  const sentinel = document.querySelector("#more-sentinel");
  if (sentinel && S.cursor) {
    new IntersectionObserver((entries, observer) => {
      if (entries.some((entry) => entry.isIntersecting) && !S.busy) {
        observer.disconnect();
        load(true).catch(fail);
      }
    }, { rootMargin: "100px" }).observe(sentinel);
  }
  if (tg?.BackButton) S.chat ? tg.BackButton.show() : tg.BackButton.hide();
  if (S.view === "gallery") loadThumbnails();
}
function statusBanner() {
  if (S.owner.enabled && !S.owner.paused) return "";
  const paused = S.owner.paused;
  return `<div class="connection-banner">${icon("shield")}<div><strong>${
    e(t(paused ? COPY.ARCHIVE_PAUSED : COPY.CONNECT_YOUR_HISTORY))
  }</strong><p>${
    e(
      paused ? COPY.PAUSED_DESCRIPTION : COPY.ENABLE_DESCRIPTION,
    )
  }</p></div><button class="button primary" data-action="nav" data-view="settings">${
    e(t(COPY.SET_UP))
  }</button></div>`;
}
function statCards() {
  const a = S.stats?.totals || {};
  return `<div class="stats-row">${
    [[COPY.MESSAGES, a.messages, "feed"], [COPY.DELETED, a.deleted, "trash"], [
      COPY.EDITED,
      a.edited,
      "edit",
    ], [COPY.FAVORITES, a.favorites, "star"]].map(([label, value, name]) =>
      `<div class="stat"><div class="stat-head">${e(t(label))}${
        icon(name)
      }</div><div class="stat-value">${
        fmt(value)
      }</div><div class="stat-foot">${
        e(
          S.view === "stats" && S.stats?.period
            ? `${date(S.stats.period.start)} — ${date(S.stats.period.end)}`
            : t(COPY.IN_THE_LAST_30_DAYS),
        )
      }</div></div>`
    ).join("")
  }</div>`;
}

function filterBar() {
  const labels = [
    ["all", COPY.ALL_EVENTS],
    ["deleted", COPY.DELETIONS],
    ["edited", COPY.EDITS],
    ["reply_media", COPY.DISAPPEARING],
    ["protected_media", COPY.PROTECTED_ATTACHMENTS],
    ["favorites", COPY.FAVORITES],
  ];
  return `<div class="filters">${
    labels.map(([key, label]) =>
      `<button class="filter ${
        S.mode === key ? "active" : ""
      }" data-action="filter" data-mode="${key}">${e(t(label))}${
        S.counts[key] ? `<span>${fmt(S.counts[key])}</span>` : ""
      }</button>`
    ).join("")
  }</div>
  <div class="archive-search" ${
    !S.showSearch && !S.showChatFilter && !S.chat ? "hidden" : ""
  }>
    <div class="search-wrap search-global" ${!S.showSearch ? "hidden" : ""}>${
    icon("search")
  }<input id="global-search" value="${e(S.query)}" placeholder="${
    e(t(COPY.SEARCH_YOUR_ARCHIVE))
  }" aria-label="${e(t(COPY.SEARCH_YOUR_ARCHIVE))}"></div>
    <select id="archive-chat" ${
    !S.showChatFilter && !S.chat ? "hidden" : ""
  } aria-label="${e(t(COPY.CONVERSATION))}"><option value="0">${
    e(t(COPY.ALL_CHATS))
  }</option>${
    S.chats.map((chat) =>
      `<option value="${chat.id}" ${S.chat === chat.id ? "selected" : ""}>${
        e(chat.title)
      }</option>`
    ).join("")
  }</select>
    ${
    S.chat
      ? `<button class="icon-button" data-action="chat-settings" aria-label="${
        e(t(COPY.SETTINGS))
      }">${icon("settings")}</button>`
      : ""
  }
  </div>`;
}
function empty(
  title = COPY.NOTHING_HERE_YET,
  description =
    COPY.EVENTS_APPEAR_AFTER_THE_BOT_RECEIVES_MESSAGES_FROM_CONNECTED_CHATS,
) {
  return `<div class="empty"><div class="empty-icon">${icon("feed")}</div><h2>${
    e(t(title))
  }</h2><p>${e(t(description))}</p></div>`;
}
function more() {
  return S.cursor
    ? `<div class="load-more" id="more-sentinel"><button class="button subtle" data-action="more">${
      e(t(COPY.LOAD_MORE))
    }</button></div>`
    : "";
}
function eventFeed() {
  let last = "", opened = false;
  const result = S.items.map((item) => {
    const stamp = item.occurred_at || item.sent_at, d = date(stamp);
    let divider = "";
    if (d !== last) {
      const label = d === date(Date.now() / 1000)
        ? t(COPY.TODAY)
        : d === date(Date.now() / 1000 - 86400)
        ? t(COPY.YESTERDAY)
        : d;
      divider = `${opened ? "</div>" : ""}<div class="day-divider">${
        e(label)
      }</div><div class="feed-day">`;
      opened = true;
      last = d;
    }
    const kind = item.kind || "search";
    const label = {
      edited: COPY.MESSAGE_EDITED,
      deleted: COPY.MESSAGE_DELETED,
      unknown_deleted: COPY.MESSAGE_DELETED,
      profile: COPY.PROFILE_CHANGED,
      reply_media: COPY.REPLY_MEDIA,
      protected_media: COPY.PROTECTED_ATTACHMENT,
      search: COPY.SEARCH_RESULTS,
    }[kind] || COPY.SERVICE_MESSAGE;
    const glyph = {
      edited: "edit",
      deleted: "trash",
      unknown_deleted: "trash",
      profile: "settings",
      reply_media: "file",
      protected_media: "file",
    }[kind] || "chats";
    const name = item.chat_title || S.chats.find((c) =>
      c.id === item.chat_id
    )?.title || COPY.CONVERSATION;
    const message = item.message_id || kind === "search";
    const id = message ? (item.message_id || item.id) : item.chat_id;
    const body = item.match_text || item.body ||
      (kind === "unknown_deleted"
        ? COPY.UNKNOWN_MESSAGE
        : kind === "profile"
        ? COPY.PROFILE_UPDATE
        : COPY.SERVICE_MESSAGE);
    return `${divider}<button class="event-row" data-action="${
      message ? "message" : "chat"
    }" data-id="${id}" aria-label="${e(name)}: ${e(t(label))}">
      <span class="event-avatar avatar c${(item.chat_id || 0) % 5}">${
      e(initials(name))
    }<span class="event-mark ${e(kind)}" title="${e(t(label))}">${
      icon(glyph)
    }</span></span>
      <span class="event-main"><span class="event-line"><strong>${
      e(name)
    }</strong><time>${time(stamp)}</time></span><span class="event-text">${
      e(body)
    }</span></span>
      ${item.favorite ? `<span class="event-star">${icon("star")}</span>` : ""}
    </button>`;
  }).join("");
  return result + (opened ? "</div>" : "");
}
function renderFeed() {
  shell(
    `${filterBar()}<section class="archive-feed">${
      S.newEvents
        ? `<button class="new-events" data-action="refresh">${
          e(t(COPY.REFRESH))
        } · ${fmt(S.newEvents)}</button>`
        : ""
    }
    <div class="feed">${
      S.items.length ? eventFeed() : empty(
        S.query ? COPY.NO_RESULTS : COPY.NOTHING_HERE_YET,
        S.query ? COPY.TRY_ANOTHER_WORD_OR_FILTER : undefined,
      )
    }</div>${more()}</section>`,
  );
}
function renderGallery() {
  shell(
    fill(
      COPY.RENDERGALLERY_TEMPLATE,
      e(t(COPY.MEDIA)),
      S.items.map((media) =>
        `<article class="media-card"><button class="media-preview" data-action="media" data-id="${media.id}" aria-label="${
          e(media.filename)
        }" id="preview-${media.id}">${
          icon(
            media.kind === "photo"
              ? "gallery"
              : ["voice", "audio"].includes(media.kind)
              ? "audio"
              : media.kind === "video"
              ? "video"
              : "file",
          )
        }</button><div class="media-info"><strong>${
          e(media.filename)
        }</strong><small>${e(media.chat_title)} · ${
          size(media.size)
        }</small><span class="badge ${
          media.status === "ready" ? "green" : ""
        }">${
          e(t(media.status === "ready" ? COPY.SAVED : COPY.IN_TELEGRAM))
        }</span>${
          media.protected
            ? ""
            : `<button class="text-button" data-action="send-media" data-id="${media.id}">${
              e(t(COPY.SEND_TO_TELEGRAM))
            }</button>`
        }</div></article>`
      ).join(""),
      S.items.length ? "" : empty(
        COPY.NOTHING_HERE_YET,
        COPY.EMPTY_MEDIA,
      ),
      more(),
    ),
  );
}
function activityChart() {
  const items = S.stats.activity || [];
  if (!items.length) return empty();
  const max = Math.max(...items.map((v) => v.messages), 1);
  const points = items.map((v, i) =>
    `${25 + i * 550 / Math.max(1, items.length - 1)},${
      145 - v.messages / max * 125
    }`
  ).join(" ");
  return fill(
    COPY.ACTIVITYCHART_TEMPLATE,
    points,
    e(items[0].day),
    e(items.at(-1).day),
  );
}
function renderStats() {
  const data = S.stats;
  const heat = data.heatmap ||
    Array.from({ length: 7 }, () => Array(24).fill(0));
  const max = Math.max(...heat.flat(), 1);
  const days = S.lang === "en"
    ? ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    : [
      COPY.MONDAY,
      COPY.TUESDAY,
      COPY.WEDNESDAY,
      COPY.THURSDAY,
      COPY.FRIDAY,
      COPY.SATURDAY,
      COPY.SUNDAY,
    ];
  shell(
    fill(
      COPY.RENDERSTATS_TEMPLATE,
      statCards(),
      e(t(COPY.PERIOD)),
      e(t(COPY.REFRESH)),
      e(t(COPY.DAILY_ACTIVITY)),
      fmt(data.totals.messages),
      activityChart(),
      e(t(COPY.TIME_BEFORE_DELETION)),
      [
        COPY.DELAY_MINUTE,
        COPY.DELAY_HOUR,
        COPY.DELAY_DAY,
        COPY.DELAY_WEEK,
        COPY.DELAY_LONGER,
      ].map((
        label,
        i,
      ) =>
        `<div class="bar-row"><div class="bar-label"><span>${
          e(label)
        }</span><span>${
          data.deletion_delays[i]
        }</span></div><meter min="0" max="${
          Math.max(...data.deletion_delays, 1)
        }" value="${data.deletion_delays[i]}"></meter></div>`
      ).join(""),
      e(t(COPY.ACTIVITY_HEATMAP)),
      Array.from(
        { length: 24 },
        (_, h) => `<span class="heat-day">${h % 3 === 0 ? h : ""}</span>`,
      ).join(""),
      heat.map((row, d) =>
        `<span class="heat-day">${days[d]}</span>${
          row.map((value, h) =>
            `<span class="heat-cell l${
              value ? Math.max(1, Math.ceil(value / max * 5)) : 0
            }" title="${days[d]} ${h}:00 · ${value}"></span>`
          ).join("")
        }`
      ).join(""),
      e(t(COPY.MEDIAN_REPLY_TIME)),
      data.reply_median_seconds
        ? Math.round(data.reply_median_seconds / 60) + COPY.MINUTE_UNIT
        : "—",
      fmt(data.reply_sample),
      data.sample_is_partial
        ? COPY.SAMPLE_NOTICE + fmt(data.sampled_messages) + COPY.MESSAGE_UNIT
        : "",
      e(t(COPY.FREQUENT_WORDS_IN_DELETED_MESSAGES)),
      (data.frequent_deleted_words || []).map((item) =>
        `<span class="word-chip">${e(item.word)}<small>${
          fmt(item.count)
        }</small></span>`
      ).join("") || COPY.INSUFFICIENT_DATA,
      [[COPY.INCOMING, data.totals.messages - data.totals.outgoing], [
        COPY.OUTGOING,
        data.totals.outgoing,
      ]].map(([label, value]) =>
        `<div class="bar-row"><div class="bar-label"><span>${
          e(t(label))
        }</span><strong>${fmt(value)}</strong></div><meter min="0" max="${
          data.totals.messages || 1
        }" value="${value}"></meter></div>`
      ).join(""),
    ),
  );
  document.querySelector(".main").insertAdjacentHTML(
    "beforeend",
    extendedStats(data),
  );
  document.querySelector("#stats-days").insertAdjacentHTML(
    "beforeend",
    `<option value="3660">${e(t(COPY.INSIGHT_TEN_YEARS))}</option>`,
  );
  document.querySelector("#stats-days").closest(".top-actions")
    .insertAdjacentHTML(
      "afterbegin",
      `<select id="stats-chat" aria-label="${
        e(t(COPY.CONVERSATION))
      }"><option value="0">${e(t(COPY.ALL_CHATS))}</option>${
        S.chats.map((chat) =>
          `<option value="${chat.id}" ${S.chat === chat.id ? "selected" : ""}>${
            e(chat.title)
          }</option>`
        ).join("")
      }</select>`,
    );
  document.querySelector("#stats-days").value = S.statsDays;
  document.querySelector("#stats-start").value = S.statsStart;
  document.querySelector("#stats-end").value = S.statsEnd;
}

function extendedStats(data) {
  const a = data.insights;
  if (!a) return "";
  const card = (title, body) =>
    `<section class="panel"><div class="panel-header"><h2>${
      e(t(title))
    }</h2></div><div class="chart-wrap">${body}</div></section>`;
  const bars = (items, label, value) => {
    const max = Math.max(...items.map(value), 1);
    return `<div class="bar-list">${
      items.map((item) =>
        `<div class="bar-row"><div class="bar-label"><span>${
          e(label(item))
        }</span><strong>${
          fmt(value(item))
        }</strong></div><meter min="0" max="${max}" value="${
          value(item)
        }"></meter></div>`
      ).join("")
    }</div>`;
  };
  const labels = COPY.INSIGHT_WEEKDAYS;
  const c = a.content;
  const duration = (value) =>
    value == null ? "—" : `${Math.round(value / 60)} ${t(COPY.MINUTE_UNIT)}`;
  const metrics = [
    [COPY.INSIGHT_ACTIVE_DAYS, c.active_days],
    [COPY.INSIGHT_STREAK, a.longest_active_streak],
    [COPY.INSIGHT_CHARACTERS, c.characters],
    [COPY.INSIGHT_MEAN_LENGTH, Math.round(c.mean_characters)],
    [COPY.INSIGHT_LINKS, c.messages_with_links],
    [COPY.INSIGHT_REPLIES, c.replies],
    [COPY.INSIGHT_EDIT_EVENTS, a.edit_events],
    [COPY.INSIGHT_LONGEST, c.longest_characters],
  ];
  return `<div class="insight-note">${
    e(t(COPY.INSIGHT_SCOPE))
  }</div><div class="insight-metrics">${
    metrics.map(([label, value]) =>
      `<div class="panel insight-metric"><small>${e(t(label))}</small><strong>${
        fmt(value)
      }</strong></div>`
    ).join("")
  }</div><div class="analytics-grid">${
    card(
      COPY.INSIGHT_HOURS,
      bars(
        Array.from(
          { length: 24 },
          (_, hour) => ({
            hour,
            messages: a.hourly.find((x) => x.hour === hour)?.messages || 0,
          }),
        ),
        (x) => `${x.hour}:00`,
        (x) => x.messages,
      ),
    ) +
    card(
      COPY.INSIGHT_DAYS,
      bars(
        Array.from(
          { length: 7 },
          (_, weekday) => ({
            weekday,
            messages: a.weekdays.find((x) => x.weekday === weekday)?.messages ||
              0,
          }),
        ),
        (x) => labels[x.weekday],
        (x) => x.messages,
      ),
    ) +
    card(
      COPY.INSIGHT_CHATS,
      bars(a.top_chats, (x) => x.title, (x) => x.messages),
    ) +
    card(
      COPY.INSIGHT_MEDIA,
      bars(
        a.media_types,
        (x) => COPY.INSIGHT_MEDIA_NAMES[x.kind] || x.kind,
        (x) => x.files,
      ),
    ) +
    card(
      COPY.INSIGHT_EVENTS,
      bars(
        a.event_counts,
        (x) => COPY.INSIGHT_EVENT_NAMES[x.kind] || x.kind,
        (x) => x.count,
      ),
    ) +
    card(
      COPY.INSIGHT_RESPONSE,
      `<p class="muted">${
        e(t(COPY.INSIGHT_RESPONSE_METHOD))
      }</p><dl class="record-details">${
        [["outgoing", COPY.OUTGOING], ["incoming", COPY.INCOMING]].map((
          [key, label],
        ) =>
          `<div><dt>${e(t(label))}</dt><dd>${
            duration(a.responses[key].median_seconds)
          } · P90 ${duration(a.responses[key].p90_seconds)}<br><small>n=${
            fmt(a.responses[key].count)
          }</small></dd></div>`
        ).join("")
      }</dl>${
        data.sample_is_partial
          ? `<p>${e(t(COPY.SAMPLE_NOTICE))}${fmt(data.sampled_messages)}</p>`
          : ""
      }`,
    )
  }</div><p class="insight-note">${e(t(COPY.INSIGHT_COMPARISON))}: ${
    fmt(data.previous.messages)
  } → ${fmt(data.totals.messages)}</p>`;
}
const checked = (value) => value ? "checked" : "";
function toggle(name, label, note = "") {
  return `<label class="setting-row"><span>${e(t(label))}${
    note ? `<small>${e(note)}</small>` : ""
  }</span><input type="checkbox" name="${name}" ${
    checked(S.owner[name])
  }></label>`;
}
function renderSettings() {
  const o = S.owner;
  const connected = S.meta.connections.some((item) => item.enabled);
  const deletion = S.meta.deletion;
  shell(
    `<section class="panel trigger-help"><h2>${
      e(t(COPY.TRIGGERS_HELP))
    }</h2><p>${e(t(COPY.TRIGGERS_DESCRIPTION))}</p><p>${
      e(t(COPY.REPLY_MEDIA_HELP))
    }</p><p>${e(t(COPY.MEDIA_POLICY))}</p></section>` + fill(
      COPY.RENDERSETTINGS_TEMPLATE_2,
      e(t(COPY.ARCHIVING)),
      connected ? "" : "off",
      connected ? COPY.CONNECTION_ACTIVE : COPY.CONNECTION_MISSING,
      toggle("enabled", COPY.ENABLE_ARCHIVE),
      toggle("paused", COPY.PAUSE),
      o.enabled ? "checked" : "",
      e(t(COPY.RETENTION)),
      [[0, COPY.FOREVER], [30, COPY["30_DAYS"]], [90, COPY["90_DAYS"]], [
        365,
        COPY.ONE_YEAR,
      ]].map((
        [v, label],
      ) =>
        `<option value="${v}" ${o.retention_days === v ? "selected" : ""}>${
          e(t(label))
        }</option>`
      ).join(""),
      toggle(
        "auto_delete_disconnected",
        COPY.DELETE_AFTER_DISCONNECT,
        COPY.DISCONNECT_RETENTION,
      ),
      e(t(COPY.MARK_MESSAGES_READ)),
      [["none", COPY.DO_NOT_MARK_READ], ["all", COPY.ALL_CHATS], [
        "selected",
        COPY.SELECTED_CHATS,
      ]].map(([v, label]) =>
        `<option value="${v}" ${o.read_mode === v ? "selected" : ""}>${
          e(t(label))
        }</option>`
      ).join(""),
      e(t(COPY.SAVE)),
      e(t(COPY.NOTIFICATIONS)),
      toggle("notify_enabled", COPY.NOTIFICATIONS),
      toggle("notify_deletes", COPY.DELETIONS),
      toggle("notify_edits", COPY.EDITS),
      toggle("notify_keywords", COPY.KEYWORDS),
      toggle("notify_profiles", COPY.PROFILE_CHANGES),
      toggle("hide_content", COPY.HIDE_MESSAGE_TEXT_IN_NOTIFICATIONS),
      o.quiet_start >= 0
        ? `${String(Math.floor(o.quiet_start / 60)).padStart(2, "0")}:${
          String(o.quiet_start % 60).padStart(2, "0")
        }`
        : "",
      o.quiet_end >= 0
        ? `${String(Math.floor(o.quiet_end / 60)).padStart(2, "0")}:${
          String(o.quiet_end % 60).padStart(2, "0")
        }`
        : "",
      o.utc_offset,
      e(t(COPY.SAVE)),
      e(t(COPY.KEYWORDS)),
      e(o.keywords.join(", ")),
      e(o.exclusions.join(", ")),
      toggle("whole_words", COPY.WHOLE_WORDS),
      e(t(COPY.SAVE)),
      e(t(COPY.PRIVACY_AND_SECURITY)),
      e(t(COPY.LANGUAGE)),
      S.lang === "ru" ? "selected" : "",
      S.lang === "en" ? "selected" : "",
      e(t(COPY.THEME)),
      [["dark", COPY.DARK]].map((
        [v, label],
      ) =>
        `<option value="${v}" ${v === "dark" ? "selected" : ""}>${
          e(t(label))
        }</option>`
      ).join(""),
      e(t(COPY.AUTO_LOCK)),
      [60, 300, 900, 1800].map((v) =>
        fill(
          COPY.RENDERSETTINGS_TEMPLATE,
          v,
          o.auto_lock === v ? "selected" : "",
          v / 60,
        )
      ).join(""),
      icon("lock"),
      e(t(o.has_pin ? COPY.CHANGE_PIN : COPY.SET_PIN)),
      e(t(COPY.BIOMETRIC_UNLOCK)),
      e(t(COPY.ARCHIVE_GROUP)),
      o.vault_chat_id ? "" : "off",
      o.vault_chat_id
        ? COPY.VAULT_CONNECTED + e(o.vault_chat_id)
        : COPY.VAULT_MISSING,
      e(t(COPY.CHOOSE_A_GROUP)),
      e(t(COPY.YOUR_DATA)),
      e(t(COPY.EXPORT)),
      e(t(COPY.DELETE_ALL_ARCHIVE_DATA)),
      fill(
        COPY.STATISTICS_MANAGEMENT,
        e(t(COPY.STATISTICS)),
        e(t(COPY.STATISTICS_MANAGEMENT_HINT)),
        e(t(COPY.REBUILD_STATISTICS)),
        e(t(COPY.RESET_STATISTICS)),
      ) + (deletion?.vault_pending || deletion?.vault_manual
        ? `<section class="panel setting-card"><h2>${
          e(t(COPY.OPERATOR_CLEANUP))
        }</h2><p>${
          e(fill(
            t(COPY.VAULT_DELETION_STATUS),
            fmt(deletion.vault_pending),
            fmt(deletion.vault_manual),
          ))
        }</p></section>`
        : "") +
        (S.meta.operator ? COPY.OPERATOR_CARD : ""),
      e(t(COPY.CLEAR_ARCHIVE_HISTORY)),
    ),
  );
}
function render() {
  if (S.locked) return;
  document.documentElement.lang = S.lang;
  if (S.view === "settings") renderSettings();
  else if (S.view === "stats") renderStats();
  else if (S.view === "gallery") renderGallery();
  else renderFeed();
}
async function load(append = false) {
  if (S.busy) {
    if (!append) S.reloadPending = true;
    return;
  }
  S.busy = true;
  try {
    let path;
    if (S.view === "settings") {
      const session = await api.request("/api/session");
      S.meta.deletion = session.deletion;
      render();
      return;
    }
    if (S.view === "stats") {
      S.stats = await api.request(statsPath());
      render();
      return;
    }
    if (S.query) {
      path = `/api/search?q=${encodeURIComponent(S.query)}&chat=${
        S.chat || 0
      }&kind=${S.mode}`;
    } else if (S.view === "gallery") path = `/api/gallery?chat=${S.chat || 0}`;
    else path = `/api/events?kind=${S.mode}&chat=${S.chat || 0}`;
    if (append && S.cursor) path += `&cursor=${encodeURIComponent(S.cursor)}`;
    const data = await api.request(path);
    S.items = append ? [...S.items, ...data.items] : data.items;
    S.counts = data.counts || {};
    S.cursor = data.next_cursor;
    S.newEvents = 0;
    render();
  } finally {
    S.busy = false;
    if (S.reloadPending && !S.locked) {
      S.reloadPending = false;
      load(false).catch(fail);
    }
  }
}
async function navigate(view) {
  S.view = view;
  S.chat = 0;
  S.mode = "all";
  S.query = "";
  S.cursor = null;
  S.items = [];
  await load();
}

function statsPath() {
  if (Boolean(S.statsStart) !== Boolean(S.statsEnd)) {
    throw new Error(COPY.INSIGHT_DATE_PAIR);
  }
  const offset = (S.owner?.utc_offset || 0) * 60;
  const period = S.statsStart && S.statsEnd
    ? `start=${Date.parse(S.statsStart + "T00:00:00Z") / 1000 - offset}&end=${
      Date.parse(S.statsEnd + "T00:00:00Z") / 1000 - offset + 86400
    }`
    : `days=${S.statsDays}`;
  return `/api/stats?${period}&chat=${S.chat || 0}`;
}
function openModal(title, body, actions = "") {
  if (S.locked) return;
  modal.dataset.view = "";
  document.querySelector("#modal-content").innerHTML =
    `<div class="modal-head"><h2>${
      e(t(title))
    }</h2><button class="icon-button" data-action="close-modal" aria-label="${
      e(t(COPY.CLOSE))
    }">${icon("close")}</button></div><div class="modal-body">${body}</div>${
      actions ? `<div class="modal-actions">${actions}</div>` : ""
    }`;
  if (!modal.open) modal.showModal();
}
async function openMessage(id) {
  const message = await api.request(`/api/messages/${id}`);
  S.openMessage = message;
  const kind = message.kind;
  const label = {
    deleted: COPY.MESSAGE_DELETED,
    unknown_deleted: COPY.MESSAGE_DELETED,
    edited: COPY.MESSAGE_EDITED,
    reply_media: COPY.REPLY_MEDIA,
    protected_media: COPY.PROTECTED_ATTACHMENT,
  }[kind] || COPY.SERVICE_MESSAGE;
  const changes = (message.versions || []).filter((version) => version.edited);
  const history = changes.map((version, index) =>
    `<section class="version"><small>${e(t(COPY.VERSION_PREFIX))}${
      index + 1
    } · ${time(version.changed_at, true)}</small>${
      version.has_previous
        ? ""
        : `<p class="muted">${e(t(COPY.PREVIOUS_UNAVAILABLE))}</p>`
    }<pre>${
      version.has_previous && version.diff?.length
        ? version.diff.map((part) => {
          const tag = part.kind === "added"
            ? "ins"
            : part.kind === "removed"
            ? "del"
            : "span";
          return `<${tag}>${e(part.text)}</${tag}>`;
        }).join("")
        : e(version.body)
    }</pre>${
      version.attachments?.length
        ? `<small>${e(version.attachments.join(", "))}</small>`
        : ""
    }</section>`
  ).join("");
  const attachments = (message.media || []).map((media) =>
    `<button class="media-attachment" data-action="media" data-id="${media.id}">${
      icon(
        media.kind === "photo"
          ? "gallery"
          : media.kind === "video"
          ? "video"
          : "file",
      )
    }<span>${e(media.filename)}<small>${
      e(t(
        media.status === "ready"
          ? COPY.SAVED
          : media.protected
          ? COPY.PROTECTED_FILE
          : COPY.IN_TELEGRAM,
      ))
    }</small></span>${icon("arrow")}</button>`
  ).join("");
  document.querySelector("#modal-content").innerHTML =
    `<div class="record-toolbar"><button class="icon-button round" data-action="close-modal" aria-label="${
      e(t(COPY.CLOSE))
    }">${icon("back")}</button><button class="icon-button round favorite ${
      message.favorite ? "on" : ""
    }" data-action="favorite" data-id="${id}" data-value="${
      message.favorite ? "0" : "1"
    }" aria-label="${e(t(COPY.FAVORITES))}">${icon("star")}</button></div>
  <div class="record-header"><div class="avatar c${message.chat_id % 5}">${
      e(initials(message.chat_title))
    }</div><div><h2>${e(message.chat_title)}</h2><p>${
      time(message.sent_at, true)
    }</p></div></div>
  <span class="record-state ${kind}">${
      icon(
        { deleted: "trash", unknown_deleted: "trash", edited: "edit" }[kind] ||
          "file",
      )
    }${e(t(label))}</span>
  <div class="record-content">${
      message.body
        ? `<pre>${e(message.body)}</pre>`
        : attachments
        ? ""
        : `<p class="muted">${e(t(COPY.UNKNOWN_MESSAGE))}</p>`
    }${attachments}</div>
  ${
      history
        ? `<h3 class="section-label">${
          e(t(COPY.EDIT_HISTORY))
        } · ${changes.length}</h3>${history}<p class="diff-legend"><del>${
          e(t(COPY.BEFORE))
        }</del> → <ins>${e(t(COPY.AFTER))}</ins></p>`
        : ""
    }
  <h3 class="section-label">${
      e(t(COPY.DETAILS))
    }</h3><dl class="record-details"><div><dt>${e(t(COPY.RECEIVED))}</dt><dd>${
      time(message.sent_at, true)
    }</dd></div>${
      message.edited_at
        ? `<div><dt>${e(t(COPY.LAST_EDIT))}</dt><dd>${
          time(message.edited_at, true)
        }</dd></div>`
        : ""
    }${
      message.deleted_at
        ? `<div><dt>${e(t(COPY.DELETED_AT))}</dt><dd>${
          time(message.deleted_at, true)
        }</dd></div>`
        : ""
    }</dl>
  <div class="record-actions"><button data-action="chat" data-id="${message.chat_id}">${
      icon("chats")
    }${e(t(COPY.OPEN_CHAT))}${icon("arrow")}</button></div>
  <div class="record-actions"><button class="danger" data-action="delete-message" data-id="${id}">${
      icon("trash")
    }${e(t(COPY.DELETE_RECORD))}</button></div><p class="record-note">${
      e(t(COPY.ARCHIVE_COPY_NOTE))
    }</p>`;
  modal.dataset.view = "record";
  modal.setAttribute("aria-label", message.chat_title);
  if (!modal.open) modal.showModal();
}
function exportModal() {
  openModal(
    COPY.EXPORT,
    fill(
      COPY.EXPORTMODAL_TEMPLATE,
      e(t(COPY.THE_FILE_WILL_ARRIVE_IN_YOUR_PRIVATE_BOT_CHAT)),
      e(t(COPY.FORMAT)),
      e(t(COPY.CONVERSATION)),
      S.chats.map((chat) =>
        `<option value="${chat.id}" ${S.chat === chat.id ? "selected" : ""}>${
          e(chat.title)
        }</option>`
      ).join(""),
      e(t(COPY.EXPORT)),
    ),
  );
}
function pinModal() {
  openModal(
    S.owner.has_pin ? COPY.CHANGE_PIN : COPY.SET_PIN,
    fill(
      COPY.PINMODAL_TEMPLATE,
      S.owner.has_pin ? COPY.CURRENT_PIN_FIELD : "",
      S.owner.has_pin ? "" : "required",
      S.owner.has_pin ? COPY.REMOVE_PIN_HINT : "",
      e(t(COPY.SAVE)),
    ),
  );
}
function deleteModal(kind, id = 0) {
  const hints = {
    all: COPY.DELETE_ACCOUNT_HINT,
    history: COPY.CLEAR_HISTORY_HINT,
    stats: COPY.RESET_STATISTICS_HINT,
    message: COPY.DELETE_MESSAGE_HINT,
    chat: COPY.DELETE_CHAT_HINT,
  };
  openModal(
    COPY.PERMANENT_DELETION,
    fill(
      COPY.DELETEMODAL_TEMPLATE,
      kind,
      id,
      e(t(hints[kind])),
      kind === "chat" ? COPY.DELETE_CHAT_OPTIONS : "",
      e(t(COPY.CONTINUE)),
      kind === "stats" ? "" : `<p>${e(t(COPY.VAULT_DELETION_HINT))}</p>`,
    ),
  );
}
function chatSettings() {
  const c = S.chats.find((v) => v.id === S.chat);
  openModal(
    COPY.SETTINGS,
    fill(
      COPY.CHATSETTINGS_TEMPLATE,
      e(c.title),
      checked(c.muted),
      checked(c.mark_read),
      c.excluded ? COPY.REINCLUDE_CHAT : "",
      e(t(COPY.SAVE)),
    ),
    fill(COPY.CHATSETTINGS_TEMPLATE_2),
  );
}
async function previewMedia(id) {
  let media = S.items.find((v) => v.id === id) ||
    S.items.flatMap((item) => item.media || []).find((v) => v.id === id) ||
    (S.openMessage?.media || []).find((v) => v.id === id);
  if (!media) {
    toast(COPY.MEDIA_UNAVAILABLE);
    return;
  }
  try {
    let url = S.urls.get(id);
    if (!url) {
      const blob = await api.blob(media);
      url = URL.createObjectURL(blob);
      S.urls.set(id, url);
    }
    const type = media.mime_type || "";
    let element = (media.kind === "photo" || demo)
      ? `<img class="full-media" src="${url}" alt="${e(media.filename)}">`
      : type.startsWith("video/")
      ? `<video class="full-media" src="${url}" controls playsinline></video>`
      : type.startsWith("audio/")
      ? `<audio class="full-media" src="${url}" controls></audio>`
      : COPY.REMOTE_FILE;
    openModal(
      media.filename,
      element,
      media.protected
        ? ""
        : `<button class="button primary" data-action="send-media" data-id="${id}">${
          e(t(COPY.SEND_TO_TELEGRAM))
        }</button>`,
    );
  } catch (error) {
    if (S.locked || error.name === "AbortError") return;
    openModal(
      media.filename,
      `<p>${e(error.message)}</p>`,
      media.protected
        ? ""
        : fill(COPY.PREVIEWMEDIA_TEMPLATE, id, e(t(COPY.SEND_TO_TELEGRAM)), id),
    );
  }
}
async function loadThumbnails() {
  for (
    const media of S.items.filter((item) =>
      item.kind === "photo" && item.status === "ready"
    ).slice(0, 30)
  ) {
    try {
      let url = S.urls.get(media.id);
      if (!url) {
        url = URL.createObjectURL(await api.blob(media));
        S.urls.set(media.id, url);
      }
      const container = document.querySelector(`#preview-${media.id}`);
      if (container) {
        container.textContent = "";
        const image = document.createElement("img");
        image.src = url;
        image.alt = media.filename;
        image.loading = "lazy";
        container.append(image);
      }
    } catch { /* A remote-only or expired preview remains a file card. */ }
  }
}
async function trackTask(id) {
  if (!id) return;
  for (let index = 0; index < 60 && !S.locked; index++) {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    try {
      const job = await api.request(`/api/tasks/${id}`);
      if (job.status === "cancelled") return;
      if (job.status === "done") {
        toast(COPY.DONE);
        return;
      }
      if (["failed", "uncertain"].includes(job.status)) {
        toast(
          job.status === "uncertain"
            ? COPY.DELIVERY_IS_UNCERTAIN_CHECK_THE_CHAT_BEFORE_RETRYING
            : COPY.TASK_FAILED,
        );
        return;
      }
    } catch {
      return;
    }
  }
}
async function queue(path, body = {}, wait = false) {
  const result = await api.request(path, { method: "POST", body });
  toast(COPY.ADDED_TO_THE_QUEUE);
  if (wait) await trackTask(result.task_id);
  else trackTask(result.task_id);
  return result;
}
function fail(error) {
  if (error.name === "AbortError") return;
  toast(error.message || COPY.ACTION_FAILED);
}
function biometricManager() {
  return new Promise((resolve, reject) => {
    if (!tg?.BiometricManager) {
      reject(
        new Error(
          COPY.BIOMETRIC_CLIENT_REQUIRED,
        ),
      );
      return;
    }
    tg.BiometricManager.init(() => resolve(tg.BiometricManager));
  });
}
async function enrollBiometric() {
  if (!S.owner.has_pin) {
    pinModal();
    return;
  }
  const manager = await biometricManager();
  if (!manager.isBiometricAvailable) {
    throw new Error(COPY.BIOMETRIC_UNAVAILABLE);
  }
  const credential = await api.request("/api/biometric", {
    method: "POST",
    body: { device_id: manager.deviceId },
  });
  manager.requestAccess(
    { reason: COPY.BIOMETRIC_ENROLL_REASON },
    (granted) => {
      if (!granted) {
        toast(COPY.BIOMETRIC_PERMISSION_DENIED);
        return;
      }
      manager.updateBiometricToken(credential.token, (success) =>
        toast(
          success ? COPY.BIOMETRIC_ENROLLED : COPY.BIOMETRIC_STORE_FAILED,
        ));
    },
  );
}
async function unlockBiometric() {
  const manager = await biometricManager();
  manager.authenticate(
    { reason: COPY.BIOMETRIC_UNLOCK_REASON },
    async (success, token) => {
      if (!success || !token) {
        toast(COPY.BIOMETRIC_FAILED);
        return;
      }
      try {
        await api.request("/api/unlock-biometric", {
          method: "POST",
          body: { device_id: manager.deviceId, token },
        });
        await bootstrap();
      } catch (error) {
        fail(error);
      }
    },
  );
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  try {
    if (action === "nav") await navigate(button.dataset.view);
    else if (action === "feed-search") {
      S.showSearch = !S.showSearch;
      render();
      if (S.showSearch) document.querySelector("#global-search")?.focus();
    } else if (action === "feed-filters") {
      S.showChatFilter = !S.showChatFilter;
      render();
    } else if (action === "chat") {
      if (modal.open) modal.close();
      S.chat = Number(button.dataset.id);
      S.view = "feed";
      S.query = "";
      S.mode = "all";
      S.cursor = null;
      await load();
    } else if (action === "filter") {
      S.mode = button.dataset.mode;
      S.cursor = null;
      await load();
    } else if (action === "more") await load(true);
    else if (action === "refresh") {
      S.chats = (await api.request("/api/chats")).items;
      await load();
    } else if (action === "message") {
      await openMessage(Number(button.dataset.id));
    } else if (action === "favorite") {
      await api.request(`/api/messages/${button.dataset.id}/favorite`, {
        method: "POST",
        body: { value: button.dataset.value === "1" },
      });
      if (modal.open) modal.close();
      await load();
    } else if (action === "close-modal") modal.close();
    else if (action === "export") exportModal();
    else if (action === "pin") pinModal();
    else if (action === "enroll-bio") await enrollBiometric();
    else if (action === "unlock-bio") await unlockBiometric();
    else if (action === "chat-settings") chatSettings();
    else if (action === "delete-all") deleteModal("all");
    else if (action === "delete-history") deleteModal("history");
    else if (action === "delete-stats") deleteModal("stats");
    else if (action === "rebuild-stats") {
      button.disabled = true;
      try {
        await queue("/api/stats/rebuild", {}, true);
        if (S.view === "stats" && !S.locked) await load();
      } finally {
        button.disabled = false;
      }
    } else if (action === "delete-chat") deleteModal("chat");
    else if (action === "delete-message") {
      deleteModal("message", Number(button.dataset.id));
    } else if (action === "media") await previewMedia(button.dataset.id);
    else if (action === "send-media") {
      button.disabled = true;
      try {
        await queue(`/api/media/${button.dataset.id}/send`, {}, true);
      } finally {
        button.disabled = false;
      }
    } else if (action === "retry-media") {
      button.disabled = true;
      try {
        await queue(`/api/media/${button.dataset.id}/retry`, {}, true);
      } finally {
        button.disabled = false;
      }
    } else if (action === "vault-request") {
      await queue("/api/vault/request");
      if (S.meta.bot_username) {
        tg?.openTelegramLink(`https://t.me/${S.meta.bot_username}`);
      }
    } else if (action === "vault-id") {
      openModal(
        COPY.ARCHIVE_GROUP,
        COPY.VAULT_FORM,
      );
    } else if (action === "lock") {
      if (S.owner.has_pin) {
        await api.request("/api/lock", { method: "POST", body: {} });
        showLock();
      } else pinModal();
    } else if (action === "stats-period") {
      const start = document.querySelector("#stats-start").value,
        end = document.querySelector("#stats-end").value,
        days = document.querySelector("#stats-days").value;
      S.statsDays = days;
      S.statsStart = start;
      S.statsEnd = end;
      S.chat = Number(document.querySelector("#stats-chat").value);
      S.stats = await api.request(statsPath());
      renderStats();
      document.querySelector("#stats-days").value = days;
      document.querySelector("#stats-start").value = start;
      document.querySelector("#stats-end").value = end;
    } else if (action === "operator") {
      const data = await api.request("/api/operator");
      const rows = (items, fields) =>
        items.map((row) =>
          `<tr>${
            fields.map((key) => `<td>${e(String(row[key] ?? ""))}</td>`).join(
              "",
            )
          }</tr>`
        ).join("");
      const table = (title, headings, body) =>
        `<h3>${e(t(title))}</h3><table class="data-table"><thead><tr>${
          headings.map((label) => `<th>${e(t(label))}</th>`).join("")
        }</tr></thead><tbody>${body}</tbody></table>`;
      openModal(
        COPY.OPERATOR,
        fill(
          COPY.TEXT_TEMPLATE,
          fmt(data.owners),
          fmt(data.messages),
          rows(data.errors, ["code", "count"]),
        ) +
          table(
            COPY.OPERATOR_QUEUE,
            COPY.OPERATOR_TASK_HEADERS,
            rows(data.tasks, [
              "kind",
              "status",
              "count",
              "max_attempts",
              "oldest_seconds",
            ]),
          ) +
          table(
            COPY.OPERATOR_FILES,
            COPY.OPERATOR_MEDIA_HEADERS,
            rows(data.media, ["status", "count", "bytes"]),
          ) +
          table(
            COPY.OPERATOR_CLEANUP,
            COPY.OPERATOR_CLEANUP_HEADERS,
            rows(data.vault_cleanup, ["state", "count"]),
          ),
      );
    }
  } catch (error) {
    fail(error);
  }
});

document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  event.preventDefault();
  const data = new FormData(form);
  const button = form.querySelector("button[type=submit],button:not([type])");
  if (button) button.disabled = true;
  try {
    if (form.id === "unlock-form") {
      await api.request("/api/unlock", {
        method: "POST",
        body: { pin: data.get("pin") },
      });
      await bootstrap();
    } else if (form.id === "export-form") {
      await queue("/api/export", {
        format: data.get("format"),
        chat_id: Number(data.get("chat_id")),
      });
      modal.close();
    } else if (form.id === "pin-form") {
      if (data.get("new_pin") !== data.get("confirm_pin")) {
        throw new Error(COPY.PIN_MISMATCH);
      }
      await api.request("/api/pin", {
        method: "POST",
        body: {
          current_pin: data.get("current_pin") || "",
          new_pin: data.get("new_pin") || "",
        },
      });
      modal.close();
      await bootstrap();
      toast(COPY.SETTINGS_SAVED);
    } else if (form.id === "storage-form") {
      const body = {
        enabled: data.has("enabled"),
        paused: data.has("paused"),
        retention_days: Number(data.get("retention_days")),
        read_mode: data.get("read_mode"),
        auto_delete_disconnected: data.has("auto_delete_disconnected"),
      };
      if (body.enabled && !S.owner.enabled) {
        if (!data.has("consent")) {
          throw new Error(COPY.CONSENT_REQUIRED);
        }
        body.consent = "archive-permitted-chats";
      }
      S.owner = await api.request("/api/settings", { method: "PATCH", body });
      renderSettings();
      toast(COPY.SETTINGS_SAVED);
    } else if (form.id === "notification-form") {
      const minutes = (value) =>
        value
          ? Number(value.split(":")[0]) * 60 + Number(value.split(":")[1])
          : -1;
      const body = Object.fromEntries(
        [
          "notify_enabled",
          "notify_deletes",
          "notify_edits",
          "notify_keywords",
          "notify_profiles",
          "hide_content",
        ].map((key) => [key, data.has(key)]),
      );
      Object.assign(body, {
        quiet_start: minutes(data.get("quiet_start")),
        quiet_end: minutes(data.get("quiet_end")),
        utc_offset: Number(data.get("utc_offset")),
      });
      S.owner = await api.request("/api/settings", { method: "PATCH", body });
      toast(COPY.SETTINGS_SAVED);
    } else if (form.id === "keyword-form") {
      S.owner = await api.request("/api/settings", {
        method: "PATCH",
        body: {
          keywords: String(data.get("keywords")).split(",").map((v) => v.trim())
            .filter(Boolean),
          exclusions: String(data.get("exclusions")).split(",").map((v) =>
            v.trim()
          ).filter(Boolean),
          whole_words: data.has("whole_words"),
        },
      });
      toast(COPY.SETTINGS_SAVED);
    } else if (form.id === "chat-settings-form") {
      const body = {
        muted: data.has("muted"),
        mark_read: data.has("mark_read"),
      };
      if (data.has("include")) body.excluded = false;
      await api.request(`/api/chats/${S.chat}`, { method: "PATCH", body });
      modal.close();
      S.chats = (await api.request("/api/chats")).items;
      renderFeed();
    } else if (form.id === "vault-form") {
      await api.request("/api/vault", {
        method: "POST",
        body: { chat_id: Number(data.get("chat_id")) },
      });
      modal.close();
      await bootstrap();
      toast(COPY.SETTINGS_SAVED);
    } else if (form.id === "delete-form") {
      const kind = form.dataset.kind;
      const path = {
        all: "/api/delete-me",
        history: "/api/history/delete",
        stats: "/api/stats/delete",
        message: `/api/messages/${Number(form.dataset.message)}/delete`,
        chat: `/api/chats/${S.chat}/delete`,
      }[kind];
      const result = await api.request(path, {
        method: "POST",
        body: {
          confirmation: data.get("confirmation"),
          days: Number(data.get("days") || 0),
          exclude: data.has("exclude"),
        },
      });
      modal.close();
      if (kind === "stats") {
        S.stats = null;
        await load();
        toast(COPY.STATISTICS_RESET_DONE);
        return;
      }
      const status = fill(
        COPY.VAULT_DELETION_STATUS,
        fmt(result.vault_pending || 0),
        fmt(result.vault_manual || 0),
      );
      if (kind === "all") {
        S.token = "";
        S.csrf = "";
        clearPrivate();
        showLogin(COPY.ARCHIVE_DELETED);
        toast(status);
      } else {
        S.chats = (await api.request("/api/chats")).items;
        await load();
        toast(status);
      }
    }
  } catch (error) {
    fail(error);
  } finally {
    if (button) button.disabled = false;
  }
});

document.addEventListener("input", (event) => {
  if (event.target.id === "global-search") {
    clearTimeout(searchTimer);
    const value = event.target.value;
    searchTimer = setTimeout(async () => {
      S.query = value;
      S.cursor = null;
      try {
        await load();
        const input = document.querySelector("#global-search");
        input?.focus();
        input?.setSelectionRange(value.length, value.length);
      } catch (error) {
        fail(error);
      }
    }, 350);
  }
});
document.addEventListener("change", async (event) => {
  if (event.target.id === "archive-chat") {
    S.chat = Number(event.target.value);
    S.cursor = null;
    await load().catch(fail);
    return;
  }

  try {
    if (event.target.id === "theme") {
      localStorage.setItem("threaddock-theme", event.target.value);
      theme();
    } else if (event.target.id === "language") {
      S.owner = await api.request("/api/settings", {
        method: "PATCH",
        body: { language: event.target.value },
      });
      S.lang = S.owner.language;
      render();
    } else if (event.target.id === "auto-lock") {
      S.owner = await api.request("/api/settings", {
        method: "PATCH",
        body: { auto_lock: Number(event.target.value) },
      });
      toast(COPY.SETTINGS_SAVED);
    }
  } catch (error) {
    fail(error);
  }
});
for (const name of ["pointerdown", "keydown"]) {
  document.addEventListener(name, () => {
    S.lastActivity = Date.now();
    if (
      S.owner?.has_pin && !S.locked && S.lastActivity - S.activityPing > 30000
    ) {
      S.activityPing = S.lastActivity;
      api.request("/api/activity", { method: "POST", body: {} }).catch(
        () => {},
      );
    }
  });
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden && S.owner?.has_pin && !S.locked) {
    api.request("/api/lock", { method: "POST", body: {} }).catch(() => {});
    showLock();
  }
});
document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "k") {
    event.preventDefault();
    document.querySelector("#global-search")?.focus();
  }
});
setInterval(async () => {
  if (document.hidden || !S.owner || S.locked) return;
  if (
    S.owner.has_pin && Date.now() - S.lastActivity > S.owner.auto_lock * 1000
  ) {
    await api.request("/api/lock", { method: "POST", body: {} }).catch(
      () => {},
    );
    showLock();
    return;
  }
  if (S.view === "feed" && !S.query) {
    try {
      const result = await api.request(
        `/api/events?kind=${S.mode}&chat=${S.chat || 0}`,
      );
      if (result.items[0]?.id && result.items[0].id !== S.items[0]?.id) {
        S.newEvents = result.items.filter((item) =>
          item.id > (S.items[0]?.id || 0)
        ).length;
        render();
      }
    } catch {}
  }
}, 10000);
tg?.onEvent("themeChanged", theme);
tg?.BackButton?.onClick(() => navigate("feed").catch(fail));
boot();

document.title = COPY.PAGE_TITLE;
document.querySelector("#modal").setAttribute("aria-label", COPY.DETAILS);
