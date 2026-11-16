"""Bounded owner-scoped statistics; samples are labelled, never presented as totals."""

import re
import statistics
import time
from collections import Counter, defaultdict

from core import texts as T


def statistics_for(
    repository, owner_id: int, *, days: int = 30, chat_id: int = 0, start: float | None = None, end: float | None = None
) -> dict:
    if not 1 <= days <= 3660:
        raise ValueError(T.ARCHIVE_ANALYTICS_PERIOD_DAYS_RANGE)
    end = time.time() if end is None else end
    start = end - days * 86400 if start is None else start
    if not 0 <= start < end or end - start > 3660 * 86400:
        raise ValueError(T.ARCHIVE_ANALYTICS_INVALID_PERIOD)
    if chat_id and not repository.chat(owner_id, chat_id):
        raise LookupError("Chat not found")
    db = repository.db
    route, values = "owner_id=? AND (?=0 OR chat_id=?)", (owner_id, chat_id, chat_id)

    def totals(low, high):
        row = db.execute(
            f"""SELECT count(*) AS messages,count(DISTINCT chat_id) AS chats,
            coalesce(sum(deleted_at IS NOT NULL),0) AS deleted,coalesce(sum(edited_at IS NOT NULL),0) AS edited,
            coalesce(sum(outgoing),0) AS outgoing,coalesce(sum(favorite),0) AS favorites
            FROM archive_messages WHERE {route} AND sent_at>=? AND sent_at<?""",
            (*values, low, high),
        ).fetchone()
        return dict(row)

    current = totals(start, end)
    previous = totals(start - (end - start), start)
    owner = repository.owner(owner_id)
    offset = owner["utc_offset"] * 60
    activity = [
        dict(row)
        for row in db.execute(
            f"""SELECT strftime('%Y-%m-%d',sent_at+?,'unixepoch') AS day,
        count(*) AS messages,sum(deleted_at IS NOT NULL) AS deleted FROM archive_messages
        WHERE {route} AND sent_at>=? AND sent_at<? GROUP BY day ORDER BY day""",
            (offset, *values, start, end),
        )
    ]
    heatmap = [[0] * 24 for _ in range(7)]
    for row in db.execute(
        f"""SELECT strftime('%w',sent_at+?,'unixepoch') AS weekday,
        strftime('%H',sent_at+?,'unixepoch') AS hour,count(*) AS n FROM archive_messages
        WHERE {route} AND sent_at>=? AND sent_at<? GROUP BY weekday,hour""",
        (offset, offset, *values, start, end),
    ):
        heatmap[(int(row[0]) + 6) % 7][int(row[1])] = row[2]
    delays = [0] * 5
    for row in db.execute(
        f"SELECT deleted_at-sent_at FROM archive_messages WHERE {route} AND deleted_at IS NOT NULL AND deleted_at>=? AND deleted_at<?",
        (*values, start, end),
    ):
        value = max(0, row[0])
        index = next((i for i, border in enumerate((60, 3600, 86400, 604800)) if value < border), 4)
        delays[index] += 1
    sample = db.execute(
        f"SELECT chat_id,sent_at,outgoing,body,deleted_at FROM archive_messages WHERE {route} AND sent_at>=? AND sent_at<? ORDER BY sent_at,id LIMIT 20000",
        (*values, start, end),
    ).fetchall()
    last, reply_times, deleted_words, bursts = {}, [], Counter(), defaultdict(int)
    for row in sample:
        old = last.get(row["chat_id"])
        if old and old["outgoing"] != row["outgoing"] and 0 < row["sent_at"] - old["sent_at"] < 86400:
            reply_times.append(row["sent_at"] - old["sent_at"])
        last[row["chat_id"]] = row
        if row["deleted_at"]:
            deleted_words.update(word.casefold() for word in re.findall(r"[^\W\d_]{4,}", row["body"], re.UNICODE))
            bursts[(row["chat_id"], int(row["deleted_at"] // 60))] += 1
    largest = db.execute(
        f"SELECT chat_id,count(*) AS n FROM archive_messages WHERE {route} AND sent_at>=? AND sent_at<? GROUP BY chat_id ORDER BY n DESC LIMIT 1",
        (*values, start, end),
    ).fetchone()
    return {
        "totals": current,
        "previous": previous,
        "activity": activity,
        "heatmap": heatmap,
        "deletion_delays": delays,
        "period": {"start": start, "end": end},
        "reply_median_seconds": statistics.median(reply_times) if reply_times else None,
        "reply_sample": len(reply_times),
        "sampled_messages": len(sample),
        "sample_is_partial": current["messages"] > len(sample),
        "frequent_deleted_words": [{"word": word, "count": count} for word, count in deleted_words.most_common(16)],
        "bursts": [
            {"chat_id": chat, "at": minute * 60, "count": count}
            for (chat, minute), count in sorted(bursts.items(), key=lambda item: item[0][1], reverse=True)
            if count >= 5
        ][:30],
        "largest_chat": dict(largest) if largest else None,
    }
