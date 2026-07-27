from __future__ import annotations

from sqlite3 import Connection, Row
from typing import Any, Callable


class AutomationRepository:
    def __init__(self, connection: Connection, *, to_json: Callable[[Any], str]) -> None:
        self.connection = connection
        self.to_json = to_json

    def create_automation_action(self, action: Any) -> Row:
        self.connection.execute(
            """
            INSERT INTO automation_actions (
                id, kind, market_id, status, reason, command_preview, idempotency_key,
                requested_by, created_at, updated_at, expires_at
            ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(idempotency_key) DO NOTHING
            """,
            (
                action.id,
                action.kind,
                action.market_id,
                action.reason,
                action.command_preview,
                action.idempotency_key,
                action.requested_by,
                action.created_at.isoformat(),
                action.created_at.isoformat(),
                action.expires_at.isoformat(),
            ),
        )
        row = self.connection.execute(
            "SELECT * FROM automation_actions WHERE id = ? OR idempotency_key = ?",
            (action.id, action.idempotency_key),
        ).fetchone()
        if row is None:
            raise RuntimeError("failed to create automation action")
        if row["id"] == action.id:
            self.append_automation_audit_event(
                action.id, "created", action.requested_by, {"kind": action.kind}
            )
        return row

    def get_automation_action(self, action_id: str) -> Row | None:
        return self.connection.execute(
            """
            SELECT a.*, m.title AS market_title, m.slug AS market_slug
            FROM automation_actions a
            LEFT JOIN markets m ON m.id = a.market_id
            WHERE a.id = ?
            """,
            (action_id,),
        ).fetchone()

    def approve_automation_action(self, action_id: str, actor: str, now: str) -> bool:
        cursor = self.connection.execute(
            """
            UPDATE automation_actions
            SET status = 'approved', approved_by = ?, approved_at = ?, updated_at = ?
            WHERE id = ? AND status = 'pending' AND expires_at > ?
            """,
            (actor, now, now, action_id, now),
        )
        if cursor.rowcount:
            self.append_automation_audit_event(action_id, "approved", actor, {})
            return True
        return False

    def reject_automation_action(
        self, action_id: str, actor: str, reason: str | None, now: str
    ) -> bool:
        cursor = self.connection.execute(
            """
            UPDATE automation_actions
            SET status = 'rejected', rejected_by = ?, rejected_at = ?, failure_reason = ?, updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (actor, now, reason, now, action_id),
        )
        if cursor.rowcount:
            self.append_automation_audit_event(action_id, "rejected", actor, {"reason": reason})
            return True
        return False

    def expire_automation_actions(self, now: str) -> int:
        rows = list(
            self.connection.execute(
                """
                SELECT id, status FROM automation_actions
                WHERE status IN ('pending', 'approved') AND expires_at <= ?
                """,
                (now,),
            )
        )
        for row in rows:
            self.connection.execute(
                """
                UPDATE automation_actions
                SET status = 'expired', updated_at = ?
                WHERE id = ? AND status IN ('pending', 'approved')
                """,
                (now, row["id"]),
            )
            self.append_automation_audit_event(
                row["id"], "expired", None, {"previous_status": row["status"]}
            )
        return len(rows)

    def claim_approved_automation_action(self, action_id: str, now: str) -> Row | None:
        self.expire_automation_actions(now)
        cursor = self.connection.execute(
            """
            UPDATE automation_actions
            SET status = 'executing', claimed_at = ?, updated_at = ?
            WHERE id = ? AND status = 'approved' AND expires_at > ?
            """,
            (now, now, action_id, now),
        )
        if not cursor.rowcount:
            return None
        self.append_automation_audit_event(action_id, "claimed", "local-executor", {})
        return self.get_automation_action(action_id)

    def claim_next_approved_automation_action(self, now: str) -> Row | None:
        self.expire_automation_actions(now)
        row = self.connection.execute(
            """
            SELECT id FROM automation_actions
            WHERE status = 'approved' AND expires_at > ?
            ORDER BY approved_at ASC, created_at ASC
            LIMIT 1
            """,
            (now,),
        ).fetchone()
        if row is None:
            return None
        return self.claim_approved_automation_action(row["id"], now)

    def mark_automation_action_executing(
        self, action_id: str, argv: list[str], started_at: str
    ) -> None:
        self.connection.execute(
            """
            UPDATE automation_actions
            SET execution_started_at = ?, execution_argv = ?, updated_at = ?
            WHERE id = ? AND status = 'executing'
            """,
            (started_at, self.to_json(argv), started_at, action_id),
        )

    def mark_automation_action_executed(
        self,
        action_id: str,
        return_code: int,
        result_summary: str,
        now: str,
        duration_ms: int | None = None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE automation_actions
            SET status = 'executed', return_code = ?, result_summary = ?, executed_at = ?,
                execution_finished_at = ?, execution_duration_ms = ?, updated_at = ?
            WHERE id = ? AND status = 'executing'
            """,
            (return_code, result_summary, now, now, duration_ms, now, action_id),
        )
        self.append_automation_audit_event(
            action_id,
            "executed",
            "local-executor",
            {"return_code": return_code, "duration_ms": duration_ms},
        )

    def mark_automation_action_failed(
        self,
        action_id: str,
        return_code: int | None,
        failure_reason: str,
        now: str,
        duration_ms: int | None = None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE automation_actions
            SET status = 'failed', return_code = ?, failure_reason = ?, failed_at = ?,
                execution_finished_at = ?, execution_duration_ms = ?, updated_at = ?
            WHERE id = ? AND status = 'executing'
            """,
            (return_code, failure_reason, now, now, duration_ms, now, action_id),
        )
        self.append_automation_audit_event(
            action_id,
            "failed",
            "local-executor",
            {"return_code": return_code, "reason": failure_reason, "duration_ms": duration_ms},
        )

    def append_automation_audit_event(
        self, action_id: str, event: str, actor: str | None, details: Any
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO automation_audit_events (action_id, event, actor, details, created_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (action_id, event, actor, self.to_json(details)),
        )

    def list_automation_audit_events(self, action_id: str) -> list[Row]:
        return list(
            self.connection.execute(
                """
                SELECT * FROM automation_audit_events
                WHERE action_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (action_id,),
            )
        )

    def list_automation_actions(
        self,
        limit: int = 20,
        status: str | None = None,
        kind: str | None = None,
        market_id: str | None = None,
    ) -> list[Row]:
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("a.status = ?")
            params.append(status)
        if kind:
            clauses.append("a.kind = ?")
            params.append(kind)
        if market_id:
            clauses.append("a.market_id = ?")
            params.append(market_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return list(
            self.connection.execute(
                f"""
                SELECT a.*, m.title AS market_title, m.slug AS market_slug
                FROM automation_actions a
                LEFT JOIN markets m ON m.id = a.market_id
                {where}
                ORDER BY
                    CASE a.status
                        WHEN 'approved' THEN 0
                        WHEN 'pending' THEN 1
                        WHEN 'executing' THEN 2
                        WHEN 'failed' THEN 3
                        ELSE 4
                    END,
                    a.updated_at DESC,
                    a.created_at DESC
                LIMIT ?
                """,
                tuple(params),
            )
        )

    def automation_status_counts(self) -> list[Row]:
        return list(
            self.connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM automation_actions
                GROUP BY status
                ORDER BY status ASC
                """
            )
        )

    def latest_action_for_market(self, market_id: str, kind: str | None = None) -> Row | None:
        if kind:
            return self.connection.execute(
                """
                SELECT * FROM automation_actions
                WHERE market_id = ? AND kind = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (market_id, kind),
            ).fetchone()
        return self.connection.execute(
            """
            SELECT * FROM automation_actions
            WHERE market_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (market_id,),
        ).fetchone()

    def latest_action_by_status(self, *statuses: str) -> Row | None:
        if not statuses:
            return None
        placeholders = ", ".join("?" for _ in statuses)
        return self.connection.execute(
            f"""
            SELECT * FROM automation_actions
            WHERE status IN ({placeholders})
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 1
            """,
            statuses,
        ).fetchone()

    def latest_failed_action(self) -> Row | None:
        return self.connection.execute(
            """
            SELECT * FROM automation_actions
            WHERE status = 'failed'
            ORDER BY failed_at DESC, updated_at DESC
            LIMIT 1
            """
        ).fetchone()
