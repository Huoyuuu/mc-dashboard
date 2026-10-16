"""生成演示数据库（README 截图、压测用）。只用标准库，固定随机种子，结果可复现。

    uv run python scripts/seed_demo.py [数据目录] [--days 14] [--interval 30]

然后用 MC_DASH_DATA_DIR=<数据目录> MC_DASH_NO_POLL=1 uv run python app.py 启动。
地址都是 example.net/example.org 保留域名，玩家名都是虚构的。
"""
import argparse
import base64
import math
import os
import random
import struct
import sys
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("dir", nargs="?", default=str(Path(__file__).resolve().parents[2] / "Temp" / "demo"))
ap.add_argument("--days", type=int, default=14)
ap.add_argument("--interval", type=int, default=30)
args = ap.parse_args()

os.environ["MC_DASH_DATA_DIR"] = args.dir
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db  # noqa: E402

for f in Path(args.dir).glob("mcstatus.db*"):
    f.unlink()
db.init_db()
random.seed(42)


def png(rows, palette, scale=8):
    """8×8 像素画 → 64×64 PNG 的 data URI"""
    raw = b"".join(
        b"\x00" + b"".join(bytes(palette[c]) * scale for c in row) for row in rows for _ in range(scale)
    )
    size = len(rows[0]) * scale

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    body = chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + body).decode()


GRASS = ["gggggggg", "gGggGggg", "dgdddgdd", "dddddddd", "ddDdddDd", "dddddddd", "dDdddDdd", "dddddddd"]
CREEPER = ["gggggggg", "gGgggGgg", "gkkggkkg", "gkkggkkg", "gggkkggg", "ggkkkkgg", "ggkggkgg", "gggggggg"]
DIAMOND = ["ssssssss", "sSsscsss", "ssccSsss", "sscCccss", "sscccCss", "sssccsss", "sSsssSss", "ssssssss"]
TNT = ["rrrrrrrr", "RrrRrrRr", "wwwwwwww", "wkwkkwkw", "wkwkwwkw", "wwwwwwww", "rRrrRrrR", "rrrrrrrr"]
PAL = {"g": (95, 159, 53), "G": (124, 189, 74), "d": (134, 96, 67), "D": (110, 77, 52), "k": (28, 32, 28),
       "s": (125, 125, 125), "S": (150, 150, 150), "c": (93, 219, 213), "C": (160, 240, 235),
       "r": (186, 52, 38), "R": (150, 40, 30), "w": (235, 230, 220)}

NAMES = ["Pixel_Ming", "BlockCat", "RedstoneYu", "MossyStone", "Creeper_Lin", "LapisLazy", "OakWen",
         "NetherNi", "AmethystQ", "SnowGolem7", "Copper_Fox", "BambooBao", "Glowberry", "DeepslateDu",
         "Ender_Kai", "SlimeHop", "BeaconXu", "Honeycomb", "AxolotlPu", "Shulker_Tan"]

# (名称, 地址, 图标, 上限, 玩家池, 是否公开样本, 基础延迟 ms, 活跃度)
SERVERS = [
    ("生存一服", "survival.example.net", GRASS, 20, NAMES[:9], True, 42, 1.0),
    ("创造服", "creative.example.net:25566", DIAMOND, 50, NAMES[9:15], True, 96, 0.6),
    ("小游戏大厅", "lobby.example.org", TNT, 100, [f"_{i}" for i in range(14)], False, 61, 0.9),
    ("测试服", "test.example.org", CREEPER, 10, NAMES[15:18], True, 168, 0.35),
]
MOTD = ["欢迎回家，记得先去村民那里换绿宝石", "周末建筑大赛进行中", "今日小游戏：起床战争 / 跑酷", "测试服，数据随时会重置"]

step = args.interval
now = datetime.now(timezone.utc).replace(microsecond=0)
start = now - timedelta(days=args.days)
n_steps = args.days * 86400 // step
downtime = range(n_steps - 3 * 86400 // step, n_steps - 3 * 86400 // step + 25 * 60 // step)  # 3 天前面板停机 25 分钟


def activity(t):
    """北京时间的作息：晚上 21 点高峰、凌晨低谷、周末更活跃"""
    local = t + timedelta(hours=8)
    h = local.hour + local.minute / 60
    base = 0.08 + 0.92 * max(0.0, math.cos((h - 21) / 24 * 2 * math.pi)) ** 2
    if local.weekday() >= 5:
        base = min(1.0, base * 1.4 + 0.08)
    return base


with db.get_conn() as conn:
    conn.execute("PRAGMA user_version = 1")
    for sid0, (name, addr, art, cap, pool, public, lat0, act) in enumerate(SERVERS):
        sid = conn.execute(
            "INSERT INTO servers (name, address, created_at, icon) VALUES (?, ?, ?, ?)",
            (name, addr, start.strftime(db.TS_FMT), png(art, PAL)),
        ).lastrowid
        outages = [(o, o + random.randint(20, 80)) for o in random.sample(range(n_steps - 4000), random.randint(2, 3))]
        if sid0 == 3:  # 测试服最近 2 小时离线
            outages.append((n_steps - 7200 // step, n_steps + 1))
        online_set, snaps, players = set(), [], []
        for i in range(n_steps):
            if i in downtime:
                continue
            t = start + timedelta(seconds=i * step)
            a = activity(t) * act
            for p in pool:  # 上下线状态机：在线时平均待 40~90 分钟
                if p in online_set:
                    if random.random() < step / (60 * random.uniform(40, 90)):
                        online_set.discard(p)
                elif random.random() < a * step / 3600 * 0.9:
                    online_set.add(p)
            down = any(s <= i < e for s, e in outages)
            spike = random.random() < 0.004
            lat = round(lat0 * (1 + random.gauss(0, 0.08)) + (random.uniform(80, 220) if spike else 0), 1)
            snaps.append((sid, t.strftime(db.TS_FMT), 0 if down else 1, None if down else len(online_set), None if down else cap,
                          None if down else lat, None if down else "Paper 1.21.4", None if down else MOTD[sid0]))
            players.append(sorted(online_set, key=str.casefold) if public and not down else [])
        cur = conn.execute("SELECT COALESCE(MAX(id), 0) FROM snapshots").fetchone()[0]
        conn.executemany(
            "INSERT INTO snapshots (server_id, ts, online, player_count, max_players, latency, version, motd) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            snaps,
        )
        conn.executemany(
            "INSERT INTO snapshot_players (snapshot_id, player_name) VALUES (?, ?)",
            [(cur + k + 1, p) for k, ps in enumerate(players) for p in ps],
        )
        print(f"{name}: {len(snaps)} 条快照")
print("演示库：", db.DB_PATH)
