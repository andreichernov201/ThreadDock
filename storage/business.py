"""Persistent arbitration of mirrored Business download requests."""

import time

BUSINESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS business_events (
 id INTEGER PRIMARY KEY, conversation TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS business_events_match ON business_events(conversation,fingerprint,id);
CREATE TABLE IF NOT EXISTS business_event_routes (
 event_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, peer_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
 PRIMARY KEY(owner_id,peer_id,message_id)
);
CREATE INDEX IF NOT EXISTS business_event_routes_event ON business_event_routes(event_id,owner_id);
CREATE TABLE IF NOT EXISTS business_requests (
 id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, slot INTEGER NOT NULL,
 winner INTEGER NOT NULL DEFAULT 0, notified INTEGER NOT NULL DEFAULT 0, UNIQUE(event_id,slot)
);
"""

# A successful preparation can beat a failed preparation. Different links in
# the same physical conversation must still be delivered in their input order.
BUSINESS_READY = """
AND NOT EXISTS (SELECT 1 FROM business_requests r
 WHERE r.id=j.business_request_id AND r.winner NOT IN (0,j.id))
AND (j.error_code IS NULL OR j.business_request_id=0 OR NOT EXISTS (
 SELECT 1 FROM jobs x WHERE x.business_request_id=j.business_request_id AND x.id!=j.id
 AND x.status IN ('queued','running','ready','sending') AND (x.error_code IS NULL OR x.status!='ready')))
AND NOT EXISTS (
 SELECT 1 FROM business_requests current
 JOIN business_events ce ON ce.id=current.event_id
 JOIN business_events pe ON pe.conversation=ce.conversation
 JOIN business_requests previous ON previous.event_id=pe.id AND previous.id<current.id
 JOIN jobs p ON p.business_request_id=previous.id
 WHERE current.id=j.business_request_id AND p.status IN ('queued','running','ready','sending'))
"""


class BusinessDedup:
    def _business_event(self, destination, fingerprint: str) -> int:
        if not fingerprint or not destination.business_id:
            return 0
        info = self.connection_info(destination.business_id)
        if not info or destination.chat_id <= 0:
            return 0
        owner, peer = info["owner_id"], destination.chat_id
        route = (owner, peer, destination.message_id)
        existing = self.connection.execute(
            "SELECT event_id FROM business_event_routes WHERE owner_id=? AND peer_id=? AND message_id=?", route
        ).fetchone()
        if existing:
            return existing[0]
        conversation = ":".join(map(str, sorted((owner, peer))))
        # Repeated identical messages in one second are separate occurrences.
        # Only a route from the other account can join an existing occurrence.
        event = self.connection.execute(
            """SELECT e.id FROM business_events e WHERE e.conversation=? AND e.fingerprint=?
            AND NOT EXISTS (SELECT 1 FROM business_event_routes r WHERE r.event_id=e.id AND r.owner_id=?)
            ORDER BY e.id LIMIT 1""",
            (conversation, fingerprint, owner),
        ).fetchone()
        event_id = (
            event[0]
            if event
            else self.connection.execute(
                "INSERT INTO business_events(conversation,fingerprint,created_at) VALUES (?,?,?)",
                (conversation, fingerprint, time.time()),
            ).lastrowid
        )
        self.connection.execute("INSERT INTO business_event_routes VALUES (?,?,?,?)", (event_id, *route))
        return event_id

    def _business_request(self, event_id: int, slot: int) -> int:
        if not event_id:
            return 0
        self.connection.execute("INSERT OR IGNORE INTO business_requests(event_id,slot) VALUES (?,?)", (event_id, slot))
        return self.connection.execute(
            "SELECT id FROM business_requests WHERE event_id=? AND slot=?", (event_id, slot)
        ).fetchone()[0]

    def business_state(self, job_id: int):
        return self.connection.execute(
            """SELECT j.id,j.status,j.progress,j.error_code,j.business_request_id,r.winner,r.notified,e.conversation
            FROM jobs j LEFT JOIN business_requests r ON r.id=j.business_request_id
            LEFT JOIN business_events e ON e.id=r.event_id WHERE j.id=?""",
            (job_id,),
        ).fetchone()

    def business_lock(self, job_id: int, fallback: str) -> str:
        state = self.business_state(job_id)
        return "business:" + state["conversation"] if state and state["conversation"] else fallback

    def _business_claim(self, job_id: int) -> bool:
        state = self.business_state(job_id)
        if not state or state["status"] == "cancelled":
            return False
        if not state["business_request_id"]:
            return True
        if state["winner"] not in (0, job_id):
            return False
        self.connection.execute(
            "UPDATE business_requests SET winner=? WHERE id=?", (job_id, state["business_request_id"])
        )
        return True

    def business_delivery_allowed(self, job_id: int) -> bool:
        state = self.business_state(job_id)
        return bool(
            state and state["status"] != "cancelled" and (not state["business_request_id"] or state["winner"] == job_id)
        )

    def _business_cancel_others(self, request_id: int, winner: int) -> None:
        rows = self.connection.execute(
            "SELECT id FROM jobs WHERE business_request_id=? AND id!=? AND status NOT IN ('done','uncertain','cancelled')",
            (request_id, winner),
        ).fetchall()
        for row in rows:
            self._pause(row[0], time.time())
            self.connection.execute(
                "UPDATE jobs SET status='cancelled',retryable=0,error_code='business_duplicate',updated_at=? WHERE id=?",
                (time.time(), row[0]),
            )

    def _business_transition(self, job_id: int, status: str) -> None:
        state = self.business_state(job_id)
        if not state or not state["business_request_id"]:
            return
        request_id = state["business_request_id"]
        if status in {"done", "uncertain"} or state["progress"]:
            self.connection.execute("UPDATE business_requests SET winner=? WHERE id=?", (job_id, request_id))
            self._business_cancel_others(request_id, job_id)
        elif status in {"failed", "cancelled", "queued"}:
            self.connection.execute(
                "UPDATE business_requests SET winner=0 WHERE id=? AND winner=?", (request_id, job_id)
            )

    def business_notify(self, job_id: int) -> bool:
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            state = self.business_state(job_id)
            if not state or state["status"] == "cancelled":
                return False
            request_id = state["business_request_id"]
            if not request_id:
                return True
            if state["winner"] not in (0, job_id) or state["notified"]:
                return False
            if self.connection.execute(
                """SELECT 1 FROM jobs x WHERE x.business_request_id=? AND x.id!=?
                AND x.status IN ('queued','running','ready','sending') AND (x.error_code IS NULL OR x.status!='ready')""",
                (request_id, job_id),
            ).fetchone():
                return False
            self.connection.execute("UPDATE business_requests SET notified=1 WHERE id=?", (request_id,))
            return True

    def _business_prune(self) -> None:
        self.connection.execute("DELETE FROM business_requests WHERE id NOT IN (SELECT business_request_id FROM jobs)")
        self.connection.execute("DELETE FROM business_events WHERE id NOT IN (SELECT event_id FROM business_requests)")
        self.connection.execute(
            "DELETE FROM business_event_routes WHERE event_id NOT IN (SELECT id FROM business_events)"
        )
