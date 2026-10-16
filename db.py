"""SQLite 数据层：服务器列表 + 状态快照 + 在线玩家样本"""
import contextlib
import json
import logging
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

APP_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("MC_DASH_DATA_DIR", APP_DIR / "data"))
DB_PATH = DATA_DIR / "mcstatus.db"
OLD_DB_PATH = APP_DIR / "mcstatus.db"  # d56b438 ~ 8288955 的版本把库放在仓库根目录
LEGACY_SERVERS = DATA_DIR / "servers.json"
LEGACY_HISTORY = DATA_DIR / "history.json"

# 时间统一存 UTC，形如 2026-10-16T08:00:00Z；字符串比较即时间比较，所有条件都必须用同一格式
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"
SQL_SINCE = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)"
TZ = os.environ.get("MC_DASH_TZ", "Asia/Shanghai")  # 页面显示时区，也是旧数据（无时区）的解读时区
GAP_CAP = 90  # 估算在线时长时，相邻快照间隔的上限（秒）；超过说明面板停过机
# 一次被记录到代表的在线秒数：与同服上一条快照的间隔（走 server_id, ts 索引），封顶 GAP_CAP，之前没有快照按 30 秒
SQL_SECS = f"""MIN(CAST(strftime('%s', s.ts) AS INTEGER) - COALESCE(
    (SELECT CAST(strftime('%s', MAX(p.ts)) AS INTEGER) FROM snapshots p WHERE p.server_id = s.server_id AND p.ts < s.ts),
    CAST(strftime('%s', s.ts) AS INTEGER) - 30), {GAP_CAP})"""

log = logging.getLogger("mc_dashboard")


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime(TS_FMT)


@contextlib.contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DB_PATH.exists() and OLD_DB_PATH.exists():
        for suffix in ("", "-wal", "-shm"):
            src = OLD_DB_PATH.with_name(OLD_DB_PATH.name + suffix)
            if src.exists():
                shutil.move(src, DB_PATH.with_name(DB_PATH.name + suffix))
        log.warning("已把旧版本的 %s 移到 %s", OLD_DB_PATH, DB_PATH)
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                address TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
                ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
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
            CREATE INDEX IF NOT EXISTS idx_sp_name ON snapshot_players(player_name);
            CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts);
            """
        )
        with contextlib.suppress(sqlite3.OperationalError):  # 增量加列，可重复执行
            conn.execute("ALTER TABLE servers ADD COLUMN icon TEXT")
    migrate_legacy_json()  # 必须在 UTC 转换之前：JSON 导入的是本地时间
    migrate_to_utc()


def migrate_to_utc():
    """旧版本存的是不带时区的本地时间，按 TZ 解读后换算成 UTC；只执行一次，换算前先整库备份。"""
    zone = ZoneInfo(TZ)

    def to_utc(value):
        try:
            return datetime.fromisoformat(value).replace(tzinfo=zone).astimezone(timezone.utc).strftime(TS_FMT)
        except (TypeError, ValueError):
            return value

    with get_conn() as conn:
        if conn.execute("PRAGMA user_version").fetchone()[0] >= 1:
            return
        if conn.execute("SELECT 1 FROM snapshots LIMIT 1").fetchone():
            backup = DB_PATH.with_name(f"{DB_PATH.name}.bak-{datetime.now():%Y%m%d%H%M%S}")
            with contextlib.closing(sqlite3.connect(backup)) as dst:
                conn.backup(dst)
            log.warning("时间格式迁移到 UTC，原库已备份到 %s", backup)
        conn.create_function("to_utc", 1, to_utc, deterministic=True)
        for table, col in (("snapshots", "ts"), ("servers", "created_at")):
            conn.execute(f"UPDATE {table} SET {col} = to_utc({col}) WHERE {col} NOT LIKE '%Z'")
        conn.execute("PRAGMA user_version = 1")


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def migrate_legacy_json():
    if not LEGACY_SERVERS.exists():
        return
    legacy_servers = _read_json(LEGACY_SERVERS, [])
    legacy_history = _read_json(LEGACY_HISTORY, [])
    if not legacy_servers:
        return
    with get_conn() as conn:
        if conn.execute("SELECT COUNT(*) FROM servers").fetchone()[0]:
            return
        id_map = {}
        for srv in legacy_servers:
            address = srv.get("address", "").strip()
            port = srv.get("port")
            if port and ":" not in address:
                address = f"{address}:{port}"
            if not address:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO servers (name, address) VALUES (?, ?)",
                (srv.get("name") or address, address),
            )
            server_id = cur.lastrowid or conn.execute(
                "SELECT id FROM servers WHERE address = ?", (address,)
            ).fetchone()["id"]
            id_map[str(srv.get("id"))] = server_id
        for item in legacy_history:
            server_id = id_map.get(str(item.get("server_id")))
            if not server_id:
                continue
            ts = (item.get("timestamp") or "").replace("T", " ")[:19]
            conn.execute(
                """INSERT INTO snapshots
                   (server_id, ts, online, player_count, max_players, latency)
                   VALUES (?, COALESCE(NULLIF(?, ''), datetime('now', 'localtime')), ?, ?, ?, ?)""",
                (
                    server_id,
                    ts,
                    int(bool(item.get("online"))),
                    item.get("players_online"),
                    item.get("players_max"),
                    item.get("latency"),
                ),
            )


def list_servers():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM servers ORDER BY id").fetchall()


def get_server(server_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()


def add_server(name: str, address: str):
    """地址重复时抛 sqlite3.IntegrityError"""
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO servers (name, address, created_at) VALUES (?, ?, ?)", (name, address, utcnow())
        ).lastrowid


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
                  latency=None, version=None, motd=None, players=(), icon=None):
    players = sorted(set(players), key=str.casefold)
    with get_conn() as conn:
        # ts 显式写入：老库的列默认值仍是本地时间
        cur = conn.execute(
            """INSERT INTO snapshots
               (server_id, ts, online, player_count, max_players, latency, version, motd)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (server_id, utcnow(), int(online), player_count, max_players, latency, version, motd),
        )
        if online:
            conn.execute("UPDATE servers SET icon = COALESCE(?, icon) WHERE id = ?", (icon, server_id))
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


def latest_snapshots():
    """所有服务器的最新快照（含玩家样本），一次查询避免逐台往返。

    返回 {server_id: (snap_dict, players_list)}。
    """
    with get_conn() as conn:
        snaps = conn.execute(
            """SELECT s.* FROM snapshots s
               JOIN (SELECT server_id, MAX(id) AS max_id
                     FROM snapshots GROUP BY server_id) m
                 ON m.max_id = s.id"""
        ).fetchall()
        return {s["server_id"]: (dict(s), s["players"]) for s in _attach_players(conn, snaps)} if snaps else {}


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


def range_history(server_id: int, hours: int = 24):
    """时间范围内的快照（升序），附带该次巡查记录到的玩家名单；t 为 Unix 秒。"""
    since = (server_id, f"-{hours} hours")
    where = f"s.server_id = ? AND s.ts >= {SQL_SINCE}"
    with get_conn() as conn:
        rows = [dict(r, players=[]) for r in conn.execute(
            f"""SELECT s.id, s.ts, CAST(strftime('%s', s.ts) AS INTEGER) AS t,
                      s.online, s.player_count, s.max_players, s.latency
               FROM snapshots s WHERE {where} ORDER BY s.ts ASC""", since)]
        by_id = {r["id"]: r for r in rows}
        for sid, name in conn.execute(
            f"""SELECT sp.snapshot_id, sp.player_name FROM snapshot_players sp
               JOIN snapshots s ON s.id = sp.snapshot_id WHERE {where}""", since):
            by_id[sid]["players"].append(name)
    for r in rows:
        r["players"].sort(key=str.casefold)
    return rows


def prune_snapshots(days: int = 90):
    """删除超过保留期的快照（连带玩家样本），防止数据库无限增长。"""
    with get_conn() as conn:
        cur = conn.execute(
            f"DELETE FROM snapshots WHERE ts < {SQL_SINCE}",
            (f"-{days} days",),
        )
        return cur.rowcount


def hourly(hours: int, server_id: int | None = None):
    """按 UTC 小时分桶的统计（首页迷你曲线、24h 在线率、热力图共用）。"""
    where = f"ts >= {SQL_SINCE}" + (" AND server_id = ?" if server_id else "")
    params = (f"-{hours} hours",) + ((server_id,) if server_id else ())
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT server_id, substr(ts, 1, 13) AS h, COUNT(*) AS n, SUM(online) AS up,
                      MAX(player_count) AS peak,
                      AVG(CASE WHEN online THEN COALESCE(player_count, 0) END) AS avg_players,
                      AVG(CASE WHEN online THEN latency END) AS latency
               FROM snapshots {"" if server_id else "INDEXED BY idx_snapshots_ts"} WHERE {where}
               GROUP BY server_id, h ORDER BY server_id, h""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def player_seen(name: str):
    with get_conn() as conn:
        return conn.execute(
            """SELECT MIN(s.ts) AS first_seen, MAX(s.ts) AS last_seen, COUNT(*) AS sightings
               FROM snapshot_players sp JOIN snapshots s ON s.id = sp.snapshot_id
               WHERE sp.player_name = ?""",
            (name,),
        ).fetchone()


def player_hours(name: str, hours: int = 720):
    """玩家在各服务器、各 UTC 小时的估算在线秒数。"""
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT s.server_id, substr(s.ts, 1, 13) AS h, SUM({SQL_SECS}) AS secs, COUNT(*) AS n
               FROM snapshot_players sp JOIN snapshots s ON s.id = sp.snapshot_id
               WHERE sp.player_name = ? AND s.ts >= {SQL_SINCE}
               GROUP BY s.server_id, h ORDER BY h""",
            (name, f"-{hours} hours"),
        ).fetchall()
        return [dict(r) for r in rows]


def history(server_id: int, limit: int = 200):
    """最近 limit 条快照（按时间升序返回），每条附带玩家样本。"""
    rows = history_page(server_id, 1, limit)
    return list(reversed(rows))
