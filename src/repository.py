import json
import sqlite3
from datetime import datetime, timezone

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
                CREATE TABLE IF NOT EXISTS zone_ledger (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    zone_id TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    note TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    voided_by TEXT,
                    voided_at TEXT,
                    void_reason TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_zone_ledger_active_source
                    ON zone_ledger(zone_id, source_type, source_ref) WHERE status = 'active';
                CREATE INDEX IF NOT EXISTS idx_zone_ledger_zone
                    ON zone_ledger(zone_id, status, seq);
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

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True

    @staticmethod
    def _entry_from_row(row):
        return {
            "seq": int(row["seq"]),
            "id": row["id"],
            "zone_id": row["zone_id"],
            "source_type": row["source_type"],
            "source_ref": row["source_ref"],
            "quantity": int(row["quantity"]),
            "status": row["status"],
            "note": row["note"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "voided_by": row["voided_by"],
            "voided_at": row["voided_at"],
            "void_reason": row["void_reason"],
        }

    @staticmethod
    def _insert_entry(connection, entry):
        connection.execute(
            "INSERT INTO zone_ledger(id, zone_id, source_type, source_ref, quantity, status, note, "
            "created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry["id"],
                entry["zone_id"],
                entry["source_type"],
                entry["source_ref"],
                entry["quantity"],
                entry.get("status", "active"),
                entry.get("note"),
                entry["created_by"],
                entry["created_at"],
                entry["updated_at"],
            ),
        )

    @staticmethod
    def _active_total(connection, zone_id):
        row = connection.execute(
            "SELECT COALESCE(SUM(quantity), 0) AS total FROM zone_ledger "
            "WHERE zone_id = ? AND status = 'active'",
            (zone_id,),
        ).fetchone()
        return int(row["total"])

    def _recalculate_zone_locked(self, connection, zone, recalc):
        """Apply the recalculation callback and persist the zone inside an open transaction."""
        total = self._active_total(connection, zone["id"])
        new_status, new_data = recalc(zone, total)
        payload = json.dumps(new_data, ensure_ascii=False, sort_keys=True)
        cursor = connection.execute(
            "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
            "WHERE id = ? AND version = ?",
            (new_status, payload, utcnow(), zone["id"], zone["version"]),
        )
        if cursor.rowcount != 1:
            raise ConflictError("version conflict on zone: " + zone["id"])
        return self._entity_from_row(
            connection.execute("SELECT * FROM entities WHERE id = ?", (zone["id"],)).fetchone()
        )

    def record_zone_entry(self, entry, actor_id, idem_key=None, check_zone=None,
                          make_opening=None, recalc=None):
        """Atomically record a ledger entry and recalculate the zone.

        Runs under BEGIN IMMEDIATE so concurrent submitters are serialized in
        server arrival order. Idempotent replays (same idempotency key or same
        active source) return the already-confirmed entry without duplicating
        it. ``make_opening`` may supply a backfill entry when the zone has no
        ledger rows yet; ``recalc`` maps (zone, active total) to (status, data).
        Returns (entry, zone, created).
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if idem_key:
                row = connection.execute(
                    "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                    (actor_id, idem_key),
                ).fetchone()
                if row:
                    existing = connection.execute(
                        "SELECT * FROM zone_ledger WHERE id = ?", (row["entity_id"],)
                    ).fetchone()
                    if existing:
                        zone = self._entity_from_row(
                            connection.execute(
                                "SELECT * FROM entities WHERE id = ?", (existing["zone_id"],)
                            ).fetchone()
                        )
                        connection.commit()
                        return self._entry_from_row(existing), zone, False
            duplicate = connection.execute(
                "SELECT * FROM zone_ledger WHERE zone_id = ? AND source_type = ? "
                "AND source_ref = ? AND status = 'active'",
                (entry["zone_id"], entry["source_type"], entry["source_ref"]),
            ).fetchone()
            if duplicate:
                zone = self._entity_from_row(
                    connection.execute(
                        "SELECT * FROM entities WHERE id = ?", (entry["zone_id"],)
                    ).fetchone()
                )
                connection.commit()
                return self._entry_from_row(duplicate), zone, False
            zone_row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entry["zone_id"],)
            ).fetchone()
            if not zone_row:
                raise NotFoundError("entity not found: " + entry["zone_id"])
            zone = self._entity_from_row(zone_row)
            if check_zone:
                check_zone(zone)
            ledger_rows = connection.execute(
                "SELECT COUNT(*) AS c FROM zone_ledger WHERE zone_id = ?", (entry["zone_id"],)
            ).fetchone()["c"]
            if ledger_rows == 0 and make_opening:
                opening = make_opening(zone)
                if opening:
                    self._insert_entry(connection, opening)
            self._insert_entry(connection, entry)
            updated_zone = self._recalculate_zone_locked(connection, zone, recalc)
            if idem_key:
                connection.execute(
                    "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (actor_id, idem_key, entry["id"], utcnow()),
                )
            created_entry = self._entry_from_row(
                connection.execute(
                    "SELECT * FROM zone_ledger WHERE id = ?", (entry["id"],)
                ).fetchone()
            )
            connection.commit()
            return created_entry, updated_zone, True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mutate_zone_entry(self, entry_id, mutate, recalc):
        """Atomically update/void a ledger entry and recalculate its zone.

        ``mutate`` receives the current entry and returns a column->value dict
        (or raises); ``recalc`` is the same callback as in record_zone_entry.
        Returns (entry, zone).
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM zone_ledger WHERE id = ?", (entry_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("ledger entry not found: " + entry_id)
            entry = self._entry_from_row(row)
            changes = mutate(entry)
            if changes:
                changes["updated_at"] = utcnow()
                assignments = ", ".join("%s = ?" % column for column in changes)
                connection.execute(
                    "UPDATE zone_ledger SET %s WHERE id = ?" % assignments,
                    tuple(changes.values()) + (entry_id,),
                )
            zone = self._entity_from_row(
                connection.execute(
                    "SELECT * FROM entities WHERE id = ?", (entry["zone_id"],)
                ).fetchone()
            )
            updated_zone = self._recalculate_zone_locked(connection, zone, recalc)
            connection.commit()
            return self.get_zone_entry(entry_id), updated_zone
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_zone_entry(self, entry_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM zone_ledger WHERE id = ?", (entry_id,)
            ).fetchone()
        return self._entry_from_row(row) if row else None

    def list_zone_entries(self, zone_id, include_voided=True):
        sql = "SELECT * FROM zone_ledger WHERE zone_id = ?"
        params = [zone_id]
        if not include_voided:
            sql += " AND status = 'active'"
        sql += " ORDER BY seq"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._entry_from_row(row) for row in rows]
