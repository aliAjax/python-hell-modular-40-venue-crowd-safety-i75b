import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS headcount_entries (
                    id TEXT PRIMARY KEY,
                    zone_id TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    occurred_at TEXT,
                    status TEXT NOT NULL DEFAULT 'confirmed',
                    data TEXT NOT NULL DEFAULT '{}',
                    voided_by TEXT,
                    voided_at TEXT,
                    void_reason TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_type, source_ref)
                );
                CREATE INDEX IF NOT EXISTS idx_entries_zone
                    ON headcount_entries(zone_id, status);
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    # ---- 区域人数分录（按来源汇算） ----

    @staticmethod
    def _entry_from_row(row):
        return {
            "id": row["id"],
            "zone_id": row["zone_id"],
            "source_type": row["source_type"],
            "source_ref": row["source_ref"],
            "quantity": int(row["quantity"]),
            "occurred_at": row["occurred_at"],
            "status": row["status"],
            "data": json.loads(row["data"]),
            "voided_by": row["voided_by"],
            "voided_at": row["voided_at"],
            "void_reason": row["void_reason"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _get_entry_tx(self, connection, entry_id):
        row = connection.execute(
            "SELECT * FROM headcount_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        return self._entry_from_row(row) if row else None

    def get_headcount_entry(self, entry_id):
        with self._connect() as connection:
            return self._get_entry_tx(connection, entry_id)

    def list_headcount_entries(self, zone_id=None, status=None):
        clauses = []
        params = []
        if zone_id:
            clauses.append("zone_id = ?")
            params.append(zone_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM headcount_entries" + where + " ORDER BY id", params
            ).fetchall()
        return [self._entry_from_row(row) for row in rows]

    def _recompute_headcount(self, connection, zone_id):
        row = connection.execute(
            "SELECT COALESCE(SUM(quantity), 0) AS total FROM headcount_entries "
            "WHERE zone_id = ? AND status = 'confirmed'",
            (zone_id,),
        ).fetchone()
        return int(row["total"])

    def _headcount_by_source(self, connection, zone_id):
        rows = connection.execute(
            "SELECT source_type, COALESCE(SUM(quantity), 0) AS total "
            "FROM headcount_entries WHERE zone_id = ? AND status = 'confirmed' "
            "GROUP BY source_type",
            (zone_id,),
        ).fetchall()
        return {row["source_type"]: int(row["total"]) for row in rows}

    def _sync_zone_headcount(self, connection, zone_id, headcount):
        """把汇算后的人数写回区域；超出容量则转为受限。与分录落账在同一事务内。"""
        row = connection.execute(
            "SELECT kind, status, data FROM entities WHERE id = ?", (zone_id,)
        ).fetchone()
        if not row or row["kind"] != "zone":
            return
        data = json.loads(row["data"])
        data["current_occupancy"] = headcount
        status = row["status"]
        capacity = int(data.get("capacity", 0) or 0)
        if headcount > capacity and status == "open":
            status = "limited"
        connection.execute(
            "UPDATE entities SET data = ?, status = ?, updated_at = ? WHERE id = ?",
            (json.dumps(data, ensure_ascii=False, sort_keys=True), status, utcnow(), zone_id),
        )

    def _backfill_opening(self, connection, zone_id):
        """旧数据没有分录的，按现有人数回填期初。"""
        count = connection.execute(
            "SELECT COUNT(*) AS c FROM headcount_entries WHERE zone_id = ?", (zone_id,)
        ).fetchone()["c"]
        if count:
            return False
        row = connection.execute(
            "SELECT data FROM entities WHERE id = ?", (zone_id,)
        ).fetchone()
        opening = 0
        if row:
            opening = int(json.loads(row["data"]).get("current_occupancy", 0) or 0)
        if opening == 0:
            return False
        now = utcnow()
        connection.execute(
            "INSERT INTO headcount_entries(id, zone_id, source_type, source_ref, quantity, "
            "occurred_at, status, data, created_by, created_at, updated_at) "
            "VALUES (?, ?, 'opening', ?, ?, ?, 'confirmed', '{}', 'system', ?, ?)",
            (str(uuid4()), zone_id, "opening:" + zone_id, opening, now, now, now),
        )
        return True

    def record_headcount_entry(
        self, actor_id, zone_id, source_type, source_ref, quantity,
        occurred_at, idempotency_key, extra=None,
    ):
        """落账一条来源分录。幂等键或来源引用重复时只算一条。

        所有检查与写入都在 BEGIN IMMEDIATE 事务内，两个操作员同时提交按服务端先后落账。
        """
        now = utcnow()
        entry_id = str(uuid4())
        connection = self._connect()
        duplicated = False
        opening_created = False
        headcount = 0
        try:
            connection.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                row = connection.execute(
                    "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                    (actor_id, idempotency_key),
                ).fetchone()
                if row:
                    existing = self._get_entry_tx(connection, row["entity_id"])
                    if existing:
                        headcount = self._recompute_headcount(connection, zone_id)
                        connection.commit()
                        return {
                            "entry": existing,
                            "duplicated": True,
                            "opening_created": False,
                            "headcount": headcount,
                        }
            row = connection.execute(
                "SELECT * FROM headcount_entries WHERE source_type = ? AND source_ref = ?",
                (source_type, source_ref),
            ).fetchone()
            if row:
                entry = self._entry_from_row(row)
                duplicated = True
            else:
                opening_created = self._backfill_opening(connection, zone_id)
                connection.execute(
                    "INSERT INTO headcount_entries(id, zone_id, source_type, source_ref, quantity, "
                    "occurred_at, status, data, created_by, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'confirmed', ?, ?, ?, ?)",
                    (
                        entry_id, zone_id, source_type, source_ref, int(quantity),
                        occurred_at, json.dumps(extra or {}, ensure_ascii=False, sort_keys=True),
                        actor_id, now, now,
                    ),
                )
                entry = self._get_entry_tx(connection, entry_id)
            headcount = self._recompute_headcount(connection, zone_id)
            self._sync_zone_headcount(connection, zone_id, headcount)
            if idempotency_key:
                connection.execute(
                    "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (actor_id, idempotency_key, entry["id"], now),
                )
            connection.commit()
            return {
                "entry": entry,
                "duplicated": duplicated,
                "opening_created": opening_created,
                "headcount": headcount,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def void_headcount_entry(self, entry_id, actor_id, reason):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM headcount_entries WHERE id = ?", (entry_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("headcount entry not found: " + entry_id)
            if row["status"] != "confirmed":
                raise ConflictError("entry is already void: " + entry_id)
            zone_id = row["zone_id"]
            connection.execute(
                "UPDATE headcount_entries SET status = 'void', voided_by = ?, voided_at = ?, "
                "void_reason = ?, updated_at = ? WHERE id = ?",
                (actor_id, utcnow(), reason, utcnow(), entry_id),
            )
            headcount = self._recompute_headcount(connection, zone_id)
            self._sync_zone_headcount(connection, zone_id, headcount)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_headcount_entry(entry_id)

    def update_headcount_entry(self, entry_id, quantity, occurred_at, actor_id):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM headcount_entries WHERE id = ?", (entry_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("headcount entry not found: " + entry_id)
            if row["status"] != "confirmed":
                raise ConflictError("cannot update a void entry: " + entry_id)
            zone_id = row["zone_id"]
            new_quantity = int(quantity) if quantity is not None else int(row["quantity"])
            new_occurred = occurred_at if occurred_at is not None else row["occurred_at"]
            connection.execute(
                "UPDATE headcount_entries SET quantity = ?, occurred_at = ?, updated_at = ? "
                "WHERE id = ?",
                (new_quantity, new_occurred, utcnow(), entry_id),
            )
            headcount = self._recompute_headcount(connection, zone_id)
            self._sync_zone_headcount(connection, zone_id, headcount)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_headcount_entry(entry_id)

    def headcount_for_zone(self, zone_id):
        with self._connect() as connection:
            total = self._recompute_headcount(connection, zone_id)
            by_source = self._headcount_by_source(connection, zone_id)
        return {"zone_id": zone_id, "headcount": total, "by_source": by_source}

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
