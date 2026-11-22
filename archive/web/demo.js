import { COPY } from "./texts.js";
// Explicit, browser-only demonstration. Never connected to the production database.
export function createDemoApi() {
  const now = Date.now() / 1000;
  const owner = {
    owner_id: 1,
    enabled: 1,
    paused: 0,
    language: "ru",
    retention_days: 0,
    notify_enabled: 1,
    notify_edits: 1,
    notify_deletes: 1,
    notify_keywords: 1,
    notify_profiles: 0,
    hide_content: 1,
    quiet_start: -1,
    quiet_end: -1,
    utc_offset: 180,
    keywords: [COPY.CREATEDEMOAPI_KEYWORDS, COPY.CREATEDEMOAPI_KEYWORDS_2],
    exclusions: [],
    whole_words: 0,
    read_mode: "none",
    vault_chat_id: null,
    media_types: [
      "photo",
      "video",
      "document",
      "audio",
      "voice",
      "video_note",
      "animation",
      "sticker",
    ],
    has_pin: false,
    auto_lock: 300,
    auto_delete_disconnected: 0,
  };
  const chats = [{
    id: 1,
    title: COPY.CREATEDEMOAPI_TITLE,
    username: "anna_demo",
    last_at: now - 240,
    preview: COPY.CREATEDEMOAPI_PREVIEW,
    message_count: 428,
    deleted_count: 14,
    muted: 0,
    excluded: 0,
  }, {
    id: 2,
    title: COPY.CREATEDEMOAPI_TITLE_2,
    username: "ilya_demo",
    last_at: now - 1800,
    preview: COPY.CREATEDEMOAPI_PREVIEW_2,
    message_count: 563,
    deleted_count: 18,
    muted: 0,
    excluded: 0,
  }, {
    id: 3,
    title: COPY.CREATEDEMOAPI_TITLE_3,
    username: "maria_demo",
    last_at: now - 7200,
    preview: COPY.CREATEDEMOAPI_PREVIEW_3,
    message_count: 293,
    deleted_count: 6,
    muted: 1,
    excluded: 0,
  }];
  const messages = [{
    id: 1,
    chat_id: 1,
    chat_title: COPY.CREATEDEMOAPI_TITLE,
    body: COPY.CREATEDEMOAPI_BODY,
    sent_at: now - 8000,
    outgoing: 0,
    deleted_at: null,
    edited_at: null,
    favorite: 0,
    media: [],
    versions: [{
      id: 1,
      changed_at: now - 8000,
      body: COPY.CREATEDEMOAPI_BODY,
      diff: [{
        kind: "same",
        text: COPY.CREATEDEMOAPI_BODY,
      }],
    }],
  }, {
    id: 2,
    chat_id: 1,
    chat_title: COPY.CREATEDEMOAPI_TITLE,
    body: COPY.CREATEDEMOAPI_BODY_2,
    sent_at: now - 7900,
    outgoing: 1,
    deleted_at: null,
    edited_at: null,
    favorite: 0,
    media: [],
    versions: [{
      id: 2,
      changed_at: now - 7900,
      body: COPY.CREATEDEMOAPI_BODY_2,
      diff: [],
    }],
  }, {
    id: 3,
    kind: "edited",
    chat_id: 1,
    chat_title: COPY.CREATEDEMOAPI_TITLE,
    body: COPY.CREATEDEMOAPI_PREVIEW,
    sent_at: now - 7800,
    outgoing: 0,
    deleted_at: null,
    edited_at: now - 240,
    favorite: 1,
    media: [],
    versions: [{
      id: 3,
      changed_at: now - 7800,
      body: COPY.CREATEDEMOAPI_VERSIONS_BODY,
      diff: [{ kind: "same", text: COPY.CREATEDEMOAPI_VERSIONS_BODY }],
    }, {
      id: 4,
      edited: true,
      has_previous: true,
      changed_at: now - 240,
      body: COPY.CREATEDEMOAPI_PREVIEW,
      diff: [{ kind: "same", text: COPY.CREATEDEMOAPI_VERSIONS_DIFF_TEXT }, {
        kind: "removed",
        text: "18:00?",
      }, { kind: "added", text: "18:30?" }],
    }],
  }, {
    id: 4,
    kind: "deleted",
    chat_id: 2,
    chat_title: COPY.CREATEDEMOAPI_TITLE_2,
    body: COPY.CREATEDEMOAPI_BODY_3,
    sent_at: now - 7200,
    outgoing: 0,
    deleted_at: now - 900,
    edited_at: null,
    favorite: 0,
    media: [],
    versions: [{
      id: 5,
      changed_at: now - 7200,
      body: COPY.CREATEDEMOAPI_BODY_3,
      diff: [{
        kind: "same",
        text: COPY.CREATEDEMOAPI_BODY_3,
      }],
    }],
  }, {
    id: 5,
    chat_id: 3,
    chat_title: COPY.CREATEDEMOAPI_TITLE_3,
    body: COPY.CREATEDEMOAPI_BODY_4,
    sent_at: now - 9000,
    outgoing: 0,
    deleted_at: null,
    edited_at: null,
    favorite: 1,
    media: [],
    versions: [],
  }];
  const events = [{
    id: 8,
    kind: "edited",
    chat_id: 1,
    chat_title: COPY.CREATEDEMOAPI_TITLE,
    message_id: 3,
    body: messages[2].body,
    favorite: 1,
    occurred_at: now - 240,
    payload: {},
  }, {
    id: 7,
    kind: "deleted",
    chat_id: 2,
    chat_title: COPY.CREATEDEMOAPI_TITLE_2,
    message_id: 4,
    body: messages[3].body,
    favorite: 0,
    occurred_at: now - 900,
    payload: {},
  }, {
    id: 6,
    kind: "keyword",
    chat_id: 2,
    chat_title: COPY.CREATEDEMOAPI_TITLE_2,
    message_id: 4,
    body: COPY.CREATEDEMOAPI_BODY_5,
    favorite: 0,
    occurred_at: now - 1800,
    payload: { matches: [COPY.CREATEDEMOAPI_KEYWORDS_2] },
  }, {
    id: 5,
    kind: "new",
    chat_id: 3,
    chat_title: COPY.CREATEDEMOAPI_TITLE_3,
    message_id: 5,
    body: messages[4].body,
    favorite: 1,
    occurred_at: now - 7200,
    payload: {},
  }, {
    id: 4,
    kind: "deleted",
    chat_id: 1,
    chat_title: COPY.CREATEDEMOAPI_TITLE,
    message_id: 1,
    body: COPY.CREATEDEMOAPI_BODY_6,
    favorite: 0,
    occurred_at: now - 8200,
    payload: {},
  }, {
    id: 3,
    kind: "edited",
    chat_id: 2,
    chat_title: COPY.CREATEDEMOAPI_TITLE_2,
    message_id: 4,
    body: COPY.CREATEDEMOAPI_BODY_7,
    favorite: 0,
    occurred_at: now - 11000,
    payload: {},
  }];
  const media = [{
    id: "a".repeat(32),
    kind: "photo",
    filename: COPY.CREATEDEMOAPI_FILENAME,
    mime_type: "image/jpeg",
    size: 2400000,
    status: "ready",
    chat_title: COPY.CREATEDEMOAPI_TITLE,
    message_id: 3,
    chat_id: 1,
    created_at: now - 1000,
  }, {
    id: "b".repeat(32),
    kind: "document",
    filename: COPY.CREATEDEMOAPI_BODY_7,
    mime_type: "application/pdf",
    size: 1200000,
    status: "remote",
    chat_title: COPY.CREATEDEMOAPI_TITLE_2,
    message_id: 4,
    chat_id: 2,
    created_at: now - 2000,
  }, {
    id: "c".repeat(32),
    kind: "voice",
    filename: COPY.CREATEDEMOAPI_FILENAME_2,
    mime_type: "audio/ogg",
    size: 740000,
    status: "ready",
    chat_title: COPY.CREATEDEMOAPI_TITLE_3,
    message_id: 5,
    chat_id: 3,
    created_at: now - 3000,
  }];
  return {
    async request(path, options = {}) {
      const url = new URL(path, "https://demo.invalid"),
        route = url.pathname,
        body = options.body || {};
      if (route === "/api/auth") {
        return {
          token: "demo-only",
          csrf: "demo-only",
          locked: false,
          user: { id: 1, first_name: COPY.YOUR_ARCHIVE },
          language: "ru",
        };
      }
      if (route === "/api/session") {
        return {
          locked: false,
          has_pin: false,
          auto_lock: 300,
          owner,
          connections: [{
            enabled: 1,
            rights: { can_reply: true, can_read_messages: true },
          }],
          bot_username: "ThreadDockBot",
          operator: false,
        };
      }
      if (route === "/api/chats") return { items: chats };
      if (route === "/api/events") {
        let items = events.filter((item) =>
          !["new", "keyword"].includes(item.kind) &&
          (!Number(url.searchParams.get("chat")) ||
            item.chat_id === Number(url.searchParams.get("chat")))
        );
        const kind = url.searchParams.get("kind");
        if (kind && kind !== "all") {
          items = items.filter((item) =>
            kind === "favorites" ? item.favorite : item.kind === kind
          );
        }
        const counts = {};
        for (
          const event of events.filter((item) =>
            !["new", "keyword"].includes(item.kind) &&
            (!Number(url.searchParams.get("chat")) ||
              item.chat_id === Number(url.searchParams.get("chat")))
          )
        ) {
          counts[event.kind] = (counts[event.kind] || 0) + 1;
        }
        return { items, next_cursor: null, counts };
      }
      if (route === "/api/gallery") {
        return {
          items: media.filter((item) =>
            events.some((event) =>
              event.message_id === item.message_id &&
              ["edited", "deleted", "reply_media"].includes(event.kind)
            ) &&
            (!Number(url.searchParams.get("chat")) ||
              item.chat_id === Number(url.searchParams.get("chat")))
          ),
          next_cursor: null,
        };
      }
      if (route === "/api/search") {
        const q = url.searchParams.get("q")?.toLowerCase() || "";
        return {
          items: messages.filter((item) =>
            item.body.toLowerCase().includes(q) &&
            (!Number(url.searchParams.get("chat")) ||
              item.chat_id === Number(url.searchParams.get("chat"))) &&
            events.some((event) => event.message_id === item.id) &&
            (url.searchParams.get("kind") !== "deleted" || item.deleted_at) &&
            (url.searchParams.get("kind") !== "edited" || item.edited_at) &&
            (url.searchParams.get("kind") !== "favorites" || item.favorite)
          )
            .map((item) => ({ ...item, match_text: item.body })),
          next_cursor: null,
        };
      }
      if (/^\/api\/chats\/\d+\/messages$/.test(route)) {
        return {
          items: messages.filter((item) =>
            item.chat_id === Number(route.split("/")[3])
          ).slice().reverse(),
          next_cursor: null,
        };
      }
      if (/^\/api\/messages\/\d+$/.test(route)) {
        return messages.find((item) =>
          item.id === Number(route.split("/")[3])
        ) || messages[0];
      }
      if (route === "/api/stats") {
        return {
          insights: {
            content: {
              active_days: 12,
              characters: 32040,
              mean_characters: 25,
              longest_characters: 640,
              messages_with_links: 38,
              replies: 92,
            },
            hourly: Array.from(
              { length: 24 },
              (_, hour) => ({
                hour,
                messages: hour > 8 && hour < 23 ? (hour * 17) % 120 : 0,
              }),
            ),
            weekdays: Array.from(
              { length: 7 },
              (_, weekday) => ({ weekday, messages: 120 + weekday * 23 }),
            ),
            top_chats: chats.filter((chat) =>
              !Number(url.searchParams.get("chat")) ||
              chat.id === Number(url.searchParams.get("chat"))
            ).map((chat) => ({
              title: chat.title,
              messages: chat.message_count,
            })),
            media_types: [{ kind: "photo", files: 42 }, {
              kind: "video",
              files: 18,
            }, { kind: "voice", files: 11 }],
            event_counts: [{ kind: "edited", count: 92 }, {
              kind: "deleted",
              count: 38,
            }],
            edit_events: 92,
            longest_active_streak: 6,
            responses: {
              incoming: { count: 65, median_seconds: 240, p90_seconds: 1080 },
              outgoing: { count: 55, median_seconds: 120, p90_seconds: 720 },
            },
          },
          totals: {
            messages: 1284,
            chats: 3,
            deleted: 38,
            edited: 92,
            outgoing: 592,
            favorites: 17,
          },
          previous: { messages: 1086, deleted: 26, edited: 76 },
          activity: Array.from(
            { length: 14 },
            (_, i) => ({
              day: new Date((now - (13 - i) * 86400) * 1000).toISOString()
                .slice(0, 10),
              messages:
                [32, 51, 28, 65, 42, 87, 61, 48, 94, 73, 58, 113, 84, 92][i],
              deleted: i % 4,
            }),
          ),
          heatmap: Array.from(
            { length: 7 },
            (_, d) =>
              Array.from(
                { length: 24 },
                (_, h) => h > 8 && h < 23 ? (h * 7 + d * 5) % 21 : 0,
              ),
          ),
          deletion_delays: [12, 15, 8, 2, 1],
          frequent_deleted_words: [
            { word: COPY.CREATEDEMOAPI_KEYWORDS, count: 18 },
            {
              word: COPY.CREATEDEMOAPI_REQUEST_FREQUENT_DELETED_WORDS_WORD,
              count: 12,
            },
            { word: COPY.CREATEDEMOAPI_KEYWORDS_2, count: 9 },
            {
              word: COPY.CREATEDEMOAPI_REQUEST_FREQUENT_DELETED_WORDS_WORD_2,
              count: 7,
            },
            {
              word: COPY.CREATEDEMOAPI_REQUEST_FREQUENT_DELETED_WORDS_WORD_3,
              count: 6,
            },
          ],
          reply_median_seconds: 184,
          reply_sample: 120,
          sampled_messages: 1284,
          sample_is_partial: false,
          bursts: [],
        };
      }
      if (route === "/api/settings") {
        Object.assign(owner, body);
        return owner;
      }
      if (route.endsWith("/favorite")) {
        const m = messages.find((item) =>
          item.id === Number(route.split("/")[3])
        );
        if (m) m.favorite = body.value;
        events.forEach((item) => {
          if (item.message_id === m?.id) item.favorite = body.value;
        });
        return { ok: true };
      }
      if (route.startsWith("/api/tasks/")) return { status: "done" };
      if (options.method && options.method !== "GET") {
        return { ok: true, task_id: 1 };
      }
      return { items: [], next_cursor: null };
    },
  };
}
