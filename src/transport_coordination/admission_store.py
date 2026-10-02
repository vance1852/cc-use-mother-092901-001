"""准入退出域的仅追加事件存储与折叠投影。

业务状态全部由 admission_events 重放得到，因此可以还原任一时点采用的
项目版本、规则版本、证据与批准链；pending 审查按事件序号天然形成 FIFO，
服务重启后顺序不变。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Iterator

from .audit import canonical_json


class AdmissionStore:
    """在基础库同一个 SQLite 连接上追加并回放准入域事件。"""

    def __init__(self, database) -> None:
        self.database = database

    def append(self, connection, *, aggregate_type: str, aggregate_id: str,
               event_type: str, payload: dict[str, Any], actor_id: str,
               request_id: str | None, created_at: str) -> dict[str, Any]:
        event_id = uuid.uuid4().hex
        sequence = connection.execute(
            "INSERT INTO admission_events(event_id,aggregate_type,aggregate_id,event_type,"
            "payload_json,actor_id,request_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (event_id, aggregate_type, aggregate_id, event_type,
             canonical_json(payload), actor_id, request_id, created_at),
        ).lastrowid
        return {"sequence": sequence, "event_id": event_id, "created_at": created_at,
                "aggregate_type": aggregate_type, "aggregate_id": aggregate_id,
                "event_type": event_type, "payload": payload, "actor_id": actor_id,
                "request_id": request_id}

    def read_events(self, *, aggregate_type: str | None = None,
                    aggregate_id: str | None = None,
                    as_of: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM admission_events"
        clauses: list[str] = []
        parameters: list[Any] = []
        if aggregate_type is not None:
            clauses.append("aggregate_type=?")
            parameters.append(aggregate_type)
        if aggregate_id is not None:
            clauses.append("aggregate_id=?")
            parameters.append(aggregate_id)
        if as_of is not None:
            clauses.append("created_at<=?")
            parameters.append(as_of)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY sequence"
        rows = self.database.connection.execute(query, parameters).fetchall()
        return [self._row(row) for row in rows]

    def stream(self, connection, *, as_of: str | None = None) -> Iterator[dict[str, Any]]:
        if as_of is None:
            rows = connection.execute("SELECT * FROM admission_events ORDER BY sequence")
        else:
            rows = connection.execute(
                "SELECT * FROM admission_events WHERE created_at<=? ORDER BY sequence", (as_of,))
        for row in rows:
            yield self._row(row)

    @staticmethod
    def _row(row) -> dict[str, Any]:
        return {"sequence": row["sequence"], "event_id": row["event_id"],
                "aggregate_type": row["aggregate_type"], "aggregate_id": row["aggregate_id"],
                "event_type": row["event_type"], "payload": json.loads(row["payload_json"]),
                "actor_id": row["actor_id"], "request_id": row["request_id"],
                "created_at": row["created_at"]}

    def save_receipt(self, connection, *, request_id: str, action: str,
                     payload_hash: str, response: dict[str, Any], created_at: str) -> None:
        connection.execute(
            "INSERT INTO admission_receipts(request_id,action,payload_hash,response_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (request_id, action, payload_hash, canonical_json(response), created_at),
        )

    def get_receipt(self, request_id: str) -> dict[str, Any] | None:
        row = self.database.connection.execute(
            "SELECT * FROM admission_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return None
        return {"request_id": row["request_id"], "action": row["action"],
                "payload_hash": row["payload_hash"],
                "response": json.loads(row["response_json"]), "created_at": row["created_at"]}
