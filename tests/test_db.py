import contextlib
import sqlite3

import pytest

import db


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "mcstatus.db")
    monkeypatch.setattr(db, "OLD_DB_PATH", tmp_path / "mcstatus.db")
    monkeypatch.setattr(db, "LEGACY_SERVERS", tmp_path / "data" / "servers.json")
    monkeypatch.setattr(db, "TZ", "Asia/Shanghai")
    return tmp_path


def test_old_db_moved_and_migrated_to_utc(tmp_db):
    # d56b438 时代：库在仓库根目录，时间是不带时区的本地时间
    with contextlib.closing(sqlite3.connect(tmp_db / "mcstatus.db")) as old:
        old.executescript("""
            CREATE TABLE servers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, address TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')));
            CREATE TABLE snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
                ts TEXT NOT NULL DEFAULT (datetime('now', 'localtime')), online INTEGER NOT NULL, player_count INTEGER,
                max_players INTEGER, latency REAL, version TEXT, motd TEXT);
            CREATE TABLE snapshot_players (snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE, player_name TEXT NOT NULL);
            INSERT INTO servers (name, address, created_at) VALUES ('s', 'a', '2026-06-12 08:00:00');
            INSERT INTO snapshots (server_id, ts, online) VALUES (1, '2026-06-12 10:00:00', 1);
        """)
    db.init_db()
    db.init_db()  # 重复执行不能再换算一次
    assert not (tmp_db / "mcstatus.db").exists()
    assert list(db.DATA_DIR.glob("mcstatus.db.bak-*"))
    assert db.get_server(1)["created_at"] == "2026-06-12T00:00:00Z"
    assert db.history(1)[0]["ts"] == "2026-06-12T02:00:00Z"
    sid = db.save_snapshot(1, online=True, players=["b", "A"])  # 老表的默认值仍是本地时间，写入必须显式带 UTC
    assert db.latest_snapshot(1)[0]["ts"].endswith("Z") and db.latest_snapshot(1)[1] == ["A", "b"] and sid


def test_prune_and_cascade():
    db.init_db()
    sid = db.add_server("s", "a")
    with pytest.raises(sqlite3.IntegrityError):
        db.add_server("s2", "a")
    old = db.save_snapshot(sid, online=True, players=["x"])
    db.save_snapshot(sid, online=True, players=["y"])
    with db.get_conn() as conn:
        conn.execute("UPDATE snapshots SET ts = '2020-01-01T00:00:00Z' WHERE id = ?", (old,))
    assert db.prune_snapshots(90) == 1
    assert [r["players"] for r in db.range_history(sid, 24)] == [["y"]]
    db.delete_server(sid)
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM snapshot_players").fetchone()[0] == 0
