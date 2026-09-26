from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS boom_segments (
                    segment_no TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','scrapped')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS boom_deployments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    segment_no TEXT NOT NULL REFERENCES boom_segments(segment_no),
                    vessel TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    length REAL NOT NULL CHECK(length>0),
                    start_x REAL NOT NULL,
                    start_y REAL NOT NULL,
                    end_x REAL NOT NULL,
                    end_y REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'deployed'
                        CHECK(status IN ('deployed','recovered')),
                    join_status TEXT NOT NULL DEFAULT 'complete'
                        CHECK(join_status IN ('complete','pending_redeploy')),
                    recovered_length REAL,
                    loss REAL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    recovered_by TEXT,
                    recovered_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_boom_deployments_active
                    ON boom_deployments(segment_no) WHERE status='deployed';
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def ensure_boom_segment(self, segment_no: str) -> None:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT OR IGNORE INTO boom_segments(segment_no, status, created_at, updated_at)
                   VALUES(?, 'active', ?, ?)""",
                (segment_no, now, now),
            )

    def get_boom_segment(self, segment_no: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM boom_segments WHERE segment_no=?", (segment_no,)
            ).fetchone()
        return dict(row) if row is not None else None

    def active_boom_deployment(self, segment_no: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM boom_deployments
                   WHERE segment_no=? AND status='deployed'""",
                (segment_no,),
            ).fetchone()
        return dict(row) if row is not None else None

    def previous_active_deployment(self, start_time: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM boom_deployments
                   WHERE status='deployed' AND start_time<=?
                   ORDER BY start_time DESC, id DESC LIMIT 1""",
                (start_time,),
            ).fetchone()
        return dict(row) if row is not None else None

    def create_boom_deployment(self, segment_no: str, vessel: str, start_time: str,
                               end_time: str, length: float, start_x: float, start_y: float,
                               end_x: float, end_y: float, join_status: str,
                               actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO boom_deployments(segment_no, vessel, start_time, end_time,
                       length, start_x, start_y, end_x, end_y, status, join_status,
                       created_by, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,'deployed',?,?,?)""",
                    (segment_no, vessel, start_time, end_time, length, start_x, start_y,
                     end_x, end_y, join_status, actor, now),
                )
                deployment_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            occupant = self.active_boom_deployment(segment_no)
            if occupant is not None:
                raise ConflictError(
                    f"段号{segment_no}已有未撤收布设：事件#{occupant['id']}"
                    f"（布设船{occupant['vessel']}，{occupant['start_time']}起）"
                ) from exc
            raise ConflictError("布设记录冲突") from exc
        return self.get_boom_deployment(deployment_id)

    def get_boom_deployment(self, deployment_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM boom_deployments WHERE id=?", (deployment_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("布设记录不存在")
        return dict(row)

    def list_boom_deployments(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM boom_deployments"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def set_boom_join_status(self, deployment_id: int, join_status: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE boom_deployments SET join_status=? WHERE id=?",
                (join_status, deployment_id),
            )

    def recover_boom_deployment(self, deployment_id: int, recovered_length: float,
                                loss: float, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE boom_deployments SET status='recovered', recovered_length=?,
                   loss=?, recovered_by=?, recovered_at=?
                   WHERE id=? AND status='deployed'""",
                (recovered_length, loss, actor, now, deployment_id),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM boom_deployments WHERE id=?", (deployment_id,)
                ).fetchone()
                if exists is None:
                    raise NotFoundError("布设记录不存在")
                raise ConflictError("该布设记录已撤收")
        return self.get_boom_deployment(deployment_id)

    def scrap_boom_segment(self, segment_no: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE boom_segments SET status='scrapped', updated_at=? WHERE segment_no=?",
                (now, segment_no),
            )
        segment = self.get_boom_segment(segment_no)
        assert segment is not None
        return segment

    def boom_summary(self) -> Dict[str, Any]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT join_status, COUNT(*) AS n FROM boom_deployments
                   WHERE status='deployed' GROUP BY join_status"""
            ).fetchall()
            scrapped = self.conn.execute(
                """SELECT segment_no FROM boom_segments
                   WHERE status='scrapped' ORDER BY segment_no"""
            ).fetchall()
        counts = {row["join_status"]: int(row["n"]) for row in rows}
        return {
            "active": sum(counts.values()),
            "complete": counts.get("complete", 0),
            "pending_redeploy": counts.get("pending_redeploy", 0),
            "scrapped_segments": [row["segment_no"] for row in scrapped],
        }

    def close(self) -> None:
        with self._lock:
            self.conn.close()
