"""Exact aggregates scoped to one owner and selected period; no new ingestion."""

import statistics


def insights(repository, owner_id, start, end, chat_id, offset, sample):
    db = repository.db
    where = "m.owner_id=? AND (?=0 OR m.chat_id=?) AND m.sent_at>=? AND m.sent_at<?"
    args = (owner_id, chat_id, chat_id, start, end)

    def rows(sql, values=args):
        return [dict(row) for row in db.execute(sql, values)]

    content = rows(
        f"""SELECT coalesce(sum(m.characters),0) AS characters,
        coalesce(avg(m.characters),0) AS mean_characters,
        coalesce(max(m.characters),0) AS longest_characters,
        sum(m.has_link) AS messages_with_links,
        sum(m.has_reply) AS replies,
        sum(m.without_text) AS without_text,
        count(DISTINCT date(m.sent_at+?,'unixepoch')) AS active_days
        FROM archive_message_statistics m WHERE {where}""",
        (offset, *args),
    )[0]
    hourly = rows(
        f"""SELECT cast(strftime('%H',m.sent_at+?,'unixepoch') AS INTEGER) AS hour,
        count(*) AS messages,sum(m.outgoing) AS outgoing FROM archive_message_statistics m
        WHERE {where} GROUP BY hour ORDER BY hour""",
        (offset, *args),
    )
    weekdays = rows(
        f"""SELECT (cast(strftime('%w',m.sent_at+?,'unixepoch') AS INTEGER)+6)%7 AS weekday,
        count(*) AS messages FROM archive_message_statistics m WHERE {where} GROUP BY weekday ORDER BY weekday""",
        (offset, *args),
    )
    chats = rows(f"""SELECT m.chat_id,c.title,count(*) AS messages,sum(m.outgoing) AS outgoing,
        sum(m.deleted_at IS NOT NULL) AS deleted,sum(m.edited) AS edited,
        min(m.sent_at) AS first_at,max(m.sent_at) AS last_at
        FROM archive_message_statistics m JOIN archive_chats c ON c.id=m.chat_id AND c.owner_id=m.owner_id
        WHERE {where} GROUP BY m.chat_id ORDER BY messages DESC,m.chat_id LIMIT 30""")
    media = rows(f"""SELECT m.kind,sum(m.files) AS files,sum(m.declared_bytes) AS declared_bytes,
        sum(m.saved_files) AS saved_files,sum(m.saved_bytes) AS saved_bytes
        FROM archive_media_statistics m WHERE {where} GROUP BY m.kind ORDER BY files DESC""")
    events = rows(
        """SELECT e.kind,count(*) AS count FROM archive_event_statistics e
        WHERE e.owner_id=? AND (?=0 OR e.chat_id=?) AND e.occurred_at>=? AND e.occurred_at<?
        AND e.kind!='new' GROUP BY e.kind ORDER BY count DESC""",
        args,
    )
    edits = rows(
        """SELECT count(*) AS edits FROM archive_event_statistics e
        WHERE e.owner_id=? AND (?=0 OR e.chat_id=?) AND e.occurred_at>=? AND e.occurred_at<?
        AND e.kind='edited'""",
        args,
    )[0]["edits"]
    daily = rows(
        f"""SELECT date(m.sent_at+?,'unixepoch') AS day,count(*) AS messages
        FROM archive_message_statistics m WHERE {where} GROUP BY day ORDER BY day""",
        (offset, *args),
    )
    from datetime import date

    longest, current, previous = 0, 0, None
    for item in daily:
        day = date.fromisoformat(item["day"])
        current = current + 1 if previous and (day - previous).days == 1 else 1
        longest, previous = max(longest, current), day
    last, replies = {}, {"incoming": [], "outgoing": []}
    for message in sample:
        before = last.get(message["chat_id"])
        if before and before["outgoing"] != message["outgoing"]:
            delay = message["sent_at"] - before["sent_at"]
            if 0 < delay < 86400:
                replies["outgoing" if message["outgoing"] else "incoming"].append(delay)
        last[message["chat_id"]] = message
    response = {}
    for direction, values in replies.items():
        ordered = sorted(values)
        response[direction] = {
            "count": len(values),
            "median_seconds": statistics.median(values) if values else None,
            "p90_seconds": ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))] if ordered else None,
            "within_5_minutes": sum(value <= 300 for value in values),
        }
    return {
        "content": content,
        "hourly": hourly,
        "weekdays": weekdays,
        "top_chats": chats,
        "media_types": media,
        "event_counts": events,
        "edit_events": edits,
        "longest_active_streak": longest,
        "busiest_day": max(daily, key=lambda item: item["messages"]) if daily else None,
        "responses": response,
        "response_method": "adjacent_direction_changes_under_24h",
        "coverage": {"source": "received_business_updates", "history_before_connection": False},
    }
