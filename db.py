"""SQLite 数据层：服务器列表 + 状态快照 + 在线玩家样本"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "mcstatus.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                address TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
                ts TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                online INTEGER NOT NULL,
                player_count INTEGER,
                max_players INTEGER,
                latency REAL,
                version TEXT,
                motd TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_server_ts
                ON snapshots(server_id, ts DESC);
            CREATE INDEX IF NOT EXISTS idx_snapshots_server_id
                ON snapshots(server_id, id DESC);
            CREATE TABLE IF NOT EXISTS snapshot_players (
                snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
                player_name TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sp_snapshot ON snapshot_players(snapshot_id);
            """
        )


def list_servers():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM servers ORDER BY id").fetchall()


def get_server(server_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()


def add_server(name: str, address: str):
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO servers (name, address) VALUES (?, ?)", (name, address))


def update_server(server_id: int, name: str, address: str):
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE servers SET name = ?, address = ? WHERE id = ?",
            (name, address, server_id),
        )
        return cur.rowcount


def delete_server(server_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM servers WHERE id = ?", (server_id,))


def save_snapshot(server_id, online, player_count=None, max_players=None,
                  latency=None, version=None, motd=None, players=()):
    players = sorted(set(players), key=str.casefold)
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO snapshots
               (server_id, online, player_count, max_players, latency, version, motd)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (server_id, int(online), player_count, max_players, latency, version, motd),
        )
        sid = cur.lastrowid
        if players:
            conn.executemany(
                "INSERT INTO snapshot_players (snapshot_id, player_name) VALUES (?, ?)",
                [(sid, p) for p in players],
            )
        return sid


def latest_snapshot(server_id: int):
    with get_conn() as conn:
        snap = conn.execute(
            "SELECT * FROM snapshots WHERE server_id = ? ORDER BY id DESC LIMIT 1",
            (server_id,),
        ).fetchone()
        if not snap:
            return None, []
        players = [
            r["player_name"]
            for r in conn.execute(
                """SELECT player_name FROM snapshot_players
                   WHERE snapshot_id = ?
                   ORDER BY lower(player_name), player_name""",
                (snap["id"],),
            )
        ]
        return snap, players


def _attach_players(conn, snaps):
    if not snaps:
        return []
    snap_ids = [s["id"] for s in snaps]
    placeholders = ",".join("?" for _ in snap_ids)
    players_by_snapshot = {sid: [] for sid in snap_ids}
    for row in conn.execute(
        f"""SELECT snapshot_id, player_name FROM snapshot_players
            WHERE snapshot_id IN ({placeholders})
            ORDER BY snapshot_id, lower(player_name), player_name""",
        snap_ids,
    ):
        players_by_snapshot[row["snapshot_id"]].append(row["player_name"])
    return [{**dict(s), "players": players_by_snapshot[s["id"]]} for s in snaps]


def history_count(server_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE server_id = ?",
            (server_id,),
        ).fetchone()[0]


def history_page(server_id: int, page: int = 1, page_size: int = 60):
    """分页历史快照（最新优先），每条附带排序后的玩家样本。"""
    offset = (page - 1) * page_size
    with get_conn() as conn:
        snaps = conn.execute(
            """SELECT * FROM snapshots
               WHERE server_id = ?
               ORDER BY id DESC
               LIMIT ? OFFSET ?""",
            (server_id, page_size, offset),
        ).fetchall()
        return _attach_players(conn, snaps)


def chart_history(server_id: int, start_ts: str | None = None, end_ts: str | None = None):
    """图表快照（按时间升序），默认最近 24 小时。"""
    conditions = ["server_id = ?"]
    params = [server_id]
    if start_ts:
        conditions.append("ts >= ?")
        params.append(start_ts)
    else:
        conditions.append("ts >= datetime('now', 'localtime', '-24 hours')")
    if end_ts:
        conditions.append("ts <= ?")
        params.append(end_ts)
    where = " AND ".join(conditions)
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT id, ts, online, player_count, max_players, latency
                FROM snapshots
                WHERE {where}
                ORDER BY ts ASC""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def history(server_id: int, limit: int = 200):
    """最近 limit 条快照（按时间升序返回），每条附带玩家样本。"""
    rows = history_page(server_id, 1, limit)
    return list(reversed(rows))
