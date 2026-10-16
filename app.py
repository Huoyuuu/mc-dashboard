"""Minecraft 服务器状态面板：FastAPI + mcstatus + SQLite"""
import asyncio
import contextlib
import functools
import logging
import os
import sqlite3
import time
from math import ceil
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from mcstatus import JavaServer

import db

POLL_INTERVAL = 30  # 秒
POLL_TIMEOUT = 15  # 单台服务器整次查询（含 DNS/SRV 解析）的硬超时，秒
HISTORY_PAGE_SIZE = 60
CHART_MAX_POINTS = 600  # 图表最多点数，超出则按时间桶聚合
RETENTION_DAYS = int(os.environ.get("MC_DASH_RETENTION_DAYS", "180"))  # 快照保留天数，负数表示永久保留
PRUNE_EVERY = 3600  # 清理过期快照的间隔，秒；启动后立即先清一次
GAP = 3 * POLL_INTERVAL  # 相邻快照间隔超过它，说明面板自己停过机

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals.update(poll_interval=POLL_INTERVAL, tz=db.TZ)
log = logging.getLogger("mc_dashboard")
last_poll = 0.0  # 最近一轮巡查结束的时间，/healthz 用


def lat_cls(ms):
    """延迟分级配色：<80 墨绿、<150 赭黄、其余砖红"""
    if ms is None:
        return "text-ink-faint"
    return "text-moss" if ms < 80 else "text-ochre" if ms < 150 else "text-rust"


def spark(values, w=120, h=26):
    """把一串数值画成内联 SVG polyline 的 points（None 跳过）"""
    pts = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    span, n = (hi - lo) or 1, max(len(values) - 1, 1)
    return " ".join(f"{i * w / n:.1f},{h - 2 - (v - lo) / span * (h - 4):.1f}" for i, v in pts)


templates.env.filters["lat_cls"] = lat_cls


async def poll_server(server) -> None:
    """查询单个服务器并写入快照"""
    try:
        async def _query():
            srv = await JavaServer.async_lookup(server["address"])
            return srv, await srv.async_status()

        # async_lookup 的 DNS/SRV 解析也可能挂死，超时须罩住整个流程
        srv, status = await asyncio.wait_for(_query(), timeout=POLL_TIMEOUT)
        sample = status.players.sample or []
        motd = status.motd.to_plain() if hasattr(status.motd, "to_plain") else str(status.motd)
        icon = status.icon if (status.icon or "").startswith("data:image/png;base64,") else None
        await asyncio.to_thread(
            db.save_snapshot,
            server["id"],
            online=True,
            player_count=status.players.online,
            max_players=status.players.max,
            latency=round(status.latency, 1),
            version=status.version.name,
            motd=motd,
            players=[p.name for p in sample],
            icon=icon,
        )
    except Exception as e:
        log.debug("poll %s failed: %r", server["address"], e)
        await asyncio.to_thread(db.save_snapshot, server["id"], online=False)


async def poll_loop():
    global last_poll
    last_prune = 0.0
    while True:
        # 任何一次意外（如数据库瞬时锁定）都不应终结监控循环
        try:
            servers = await asyncio.to_thread(db.list_servers)
            await asyncio.gather(*(poll_server(s) for s in servers))
            last_poll = time.time()
            if RETENTION_DAYS >= 0 and last_poll - last_prune >= PRUNE_EVERY:
                await asyncio.to_thread(db.prune_snapshots, RETENTION_DAYS)
                last_prune = last_poll
        except Exception:
            log.exception("poll cycle failed")
        await asyncio.sleep(POLL_INTERVAL)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    task = None if os.environ.get("MC_DASH_NO_POLL") else asyncio.create_task(poll_loop())
    yield
    if task:
        task.cancel()


app = FastAPI(title="MC 服务器状态面板", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


# ---------- 页面 ----------

@app.get("/")
def index(request: Request):
    servers = db.list_servers()
    latest = db.latest_snapshots()  # 一次查询取回全部最新快照，避免逐台往返
    buckets = {}
    for b in db.hourly(24):
        buckets.setdefault(b["server_id"], []).append(b)
    cards = []
    for s in servers:
        snap, players = latest.get(s["id"], (None, []))
        bs = buckets.get(s["id"], [])
        n = sum(b["n"] for b in bs)
        day = {
            "uptime": round(sum(b["up"] for b in bs) / n * 100, 1) if n else None,
            "peak": max((b["peak"] or 0 for b in bs), default=None),
            "spark": spark([b["latency"] for b in bs]),
        }
        cards.append({"server": s, "snap": snap, "players": players, "day": day})
    return templates.TemplateResponse(request, "index.html", {"cards": cards})


@app.get("/healthz")
def healthz():
    ok = os.environ.get("MC_DASH_NO_POLL") or time.time() - last_poll < 3 * POLL_INTERVAL + POLL_TIMEOUT
    return JSONResponse({"ok": bool(ok), "last_poll": last_poll}, status_code=200 if ok else 503)


@app.get("/server/{server_id}")
def server_detail(request: Request, server_id: int):
    server = db.get_server(server_id)
    if not server:
        raise HTTPException(404)
    snap, players = db.latest_snapshot(server_id)
    return templates.TemplateResponse(
        request, "server.html", {"server": server, "snap": snap, "players": players}
    )


def mark_gaps(rows: list) -> list:
    """面板停机造成的空档插一个离线占位点，让图表在此处断线"""
    out, prev = [], None
    for r in rows:
        t = r["t"]
        if prev is not None and t - prev > GAP:
            out.append({"ts": out[-1]["ts"], "online": 0, "player_count": None, "max_players": None,
                        "latency": None, "players": [], "gap": True})
        out.append(r)
        prev = t
    return out


def merge_segments(rows: list) -> list:
    """把连续的同状态快照合并成一段，新的在前。

    同状态 = 在线与否相同且玩家名单相同；名单为空时比较人数。中间有停机空档时拆段，并插一段"无数据"。
    一段的时长从它第一条快照算到下一段第一条；最新一段算到最后一条快照，标记为进行中。
    """
    segs = []
    for r in rows:
        key = (r["online"], tuple(r["players"]) or (r["player_count"] if r["online"] else None))
        t = r["t"]
        last = segs[-1] if segs else None
        if last and t - last["t_end"] > GAP:
            last["closed"] = True
            segs.append({"gap": True, "start": last["end"], "end": r["ts"], "t_start": last["t_end"], "t_end": t})
            last = None
        elif last and last["key"] == key:
            last.update(end=r["ts"], t_end=t)
            if r["latency"] is not None:
                last["lat"].append(r["latency"])
            continue
        elif last:
            last.update(end=r["ts"], t_end=t, closed=True)
        segs.append({"key": key, "start": r["ts"], "end": r["ts"], "t_start": t, "t_end": t,
                     "online": r["online"], "player_count": r["player_count"], "max_players": r["max_players"],
                     "players": r["players"], "lat": [r["latency"]] if r["latency"] is not None else []})
    for sg in segs:
        sg["secs"] = sg["t_end"] - sg["t_start"]
        if not sg.get("gap"):
            sg["latency"] = round(sum(sg["lat"]) / len(sg["lat"]), 1) if sg["lat"] else None
            sg["ongoing"] = not sg.get("closed")
    return segs[::-1]


def roster(rows: list) -> list:
    """玩家出现名册：次数、估算在线秒数（与上一条快照的间隔累加，封顶 GAP_CAP）、首末出现时间"""
    out, prev = {}, None
    for r in rows:
        t = r["t"]
        secs = min(t - prev, db.GAP_CAP) if prev is not None else 30
        prev = t
        for name in r["players"]:
            p = out.setdefault(name, {"name": name, "sightings": 0, "secs": 0, "first_seen": r["ts"]})
            p["sightings"] += 1
            p["secs"] += secs
            p["last_seen"] = r["ts"]
    return sorted(out.values(), key=lambda p: (-p["sightings"], p["name"].casefold()))


def downsample(rows: list, max_points: int) -> list:
    """把巡查快照按时间桶聚合到 max_points 以内，控制长区间页面体积。

    每桶：人数取峰值、延迟取均值、玩家名单取并集；桶内只要有一次在线即视为在线。
    """
    if len(rows) <= max_points:
        return rows
    bucket_size = ceil(len(rows) / max_points)
    out = []
    for i in range(0, len(rows), bucket_size):
        bucket = rows[i:i + bucket_size]
        online_rows = [r for r in bucket if r["online"]]
        latencies = [r["latency"] for r in online_rows if r["latency"] is not None]
        names = sorted({p for r in online_rows for p in r.get("players", [])}, key=str.casefold)
        out.append({
            "ts": bucket[-1]["ts"],
            "online": 1 if online_rows else 0,
            "player_count": max((r["player_count"] or 0) for r in online_rows) if online_rows else None,
            "max_players": next((r["max_players"] for r in reversed(online_rows) if r["max_players"] is not None), None),
            "latency": round(sum(latencies) / len(latencies), 1) if latencies else None,
            "players": names,
        })
    return out


@functools.lru_cache(maxsize=32)
def history_data(server_id: int, hours: int, latest_id: int) -> dict:
    """历史页的计算结果。latest_id 是该服最新快照 id，有新快照就自然失效；只缓存小结果，不缓存原始行"""
    rows = db.range_history(server_id, hours)
    online_rows = [r for r in rows if r["online"]]
    online_checks = len(online_rows)
    lats = [r["latency"] for r in online_rows if r["latency"] is not None]
    chart = downsample(mark_gaps(rows), CHART_MAX_POINTS)  # 统计用原始数据，图表用聚合后数据
    return {
        "segments": merge_segments(rows),
        "roster": roster(rows),
        "chart": chart,
        "aggregated": len(chart) < len(rows),
        "heat": [[b["h"], b["avg_players"]] for b in db.hourly(28 * 24, server_id) if b["avg_players"] is not None],
        "stats": {
            "uptime": round(online_checks / len(rows) * 100, 1) if rows else None,
            "avg_players": round(sum((r["player_count"] or 0) for r in online_rows) / online_checks, 1) if online_checks else None,
            "peak": max((r["player_count"] or 0) for r in online_rows) if online_rows else None,
            "avg_latency": round(sum(lats) / len(lats), 1) if lats else None,
            "p95_latency": sorted(lats)[ceil(len(lats) * 0.95) - 1] if lats else None,
            "jitter": round(sum(abs(a - b) for a, b in zip(lats, lats[1:])) / (len(lats) - 1), 1) if len(lats) > 1 else None,
            "checks": len(rows),
            "online_checks": online_checks,
            "player_present_checks": sum(1 for r in online_rows if (r["player_count"] or 0) > 0),
        },
    }


@app.get("/server/{server_id}/history")
def server_history(
    request: Request,
    server_id: int,
    page: int = 1,
    hours: int = 24,
    view: str = "merged",
):
    server = db.get_server(server_id)
    if not server:
        raise HTTPException(404)
    hours = hours if hours in {6, 24, 168, 720} else 24
    view = view if view in {"merged", "raw"} else "merged"
    snap, _ = db.latest_snapshot(server_id)
    d = history_data(server_id, hours, snap["id"] if snap else 0)
    total = len(d["segments"]) if view == "merged" else db.history_count(server_id)
    total_pages = max(1, ceil(total / HISTORY_PAGE_SIZE))
    page = min(max(page, 1), total_pages)
    if view == "merged":
        rows = d["segments"][(page - 1) * HISTORY_PAGE_SIZE:page * HISTORY_PAGE_SIZE]
    else:
        rows = db.history_page(server_id, page, HISTORY_PAGE_SIZE)
    start_index = (page - 1) * HISTORY_PAGE_SIZE + 1 if total else 0
    end_index = min(page * HISTORY_PAGE_SIZE, total)
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "server": server,
            "rows": rows,
            "view": view,
            "chart_rows": d["chart"],
            "chart_aggregated": d["aggregated"],
            "heat": d["heat"],
            "page": page,
            "page_size": HISTORY_PAGE_SIZE,
            "total": total,
            "total_pages": total_pages,
            "start_index": start_index,
            "end_index": end_index,
            "hours": hours,
            "stats": d["stats"],
            "player_summary": d["roster"],
        },
    )


@app.get("/player/{name}")
def player_page(request: Request, name: str):
    seen = db.player_seen(name)
    if not seen["sightings"]:
        raise HTTPException(404)
    names = {s["id"]: s["name"] for s in db.list_servers()}
    hours = db.player_hours(name, 720)
    per_server = {}
    for r in hours:
        ps = per_server.setdefault(r["server_id"], {"id": r["server_id"], "name": names.get(r["server_id"], "已删除"), "secs": 0, "n": 0})
        ps["secs"] += r["secs"]
        ps["n"] += r["n"]
    return templates.TemplateResponse(
        request,
        "player.html",
        {
            "name": name,
            "seen": seen,
            "servers": sorted(per_server.values(), key=lambda x: -x["secs"]),
            "heat": [[r["h"], r["secs"]] for r in hours],
        },
    )


# ---------- 操作 ----------

@app.post("/servers/add")
def servers_add(background_tasks: BackgroundTasks, name: str = Form(...), address: str = Form(...)):
    clean_name = name.strip()
    clean_address = address.strip()
    if not clean_name or not clean_address:
        raise HTTPException(400, "Name and address are required")
    try:
        server_id = db.add_server(clean_name, clean_address)
    except sqlite3.IntegrityError:
        raise HTTPException(400, "Server address already exists") from None
    background_tasks.add_task(poll_server, db.get_server(server_id))  # 新增后立即查询一次
    return RedirectResponse("/", status_code=303)


@app.post("/servers/{server_id}/update")
def servers_update(server_id: int, background_tasks: BackgroundTasks, name: str = Form(...), address: str = Form(...)):
    clean_name = name.strip()
    clean_address = address.strip()
    if not clean_name or not clean_address:
        raise HTTPException(400, "Name and address are required")
    try:
        updated = db.update_server(server_id, clean_name, clean_address)
    except sqlite3.IntegrityError:
        raise HTTPException(400, "Server address already exists") from None
    if not updated:
        raise HTTPException(404)
    server = db.get_server(server_id)
    if server:
        background_tasks.add_task(poll_server, server)
    return RedirectResponse(f"/server/{server_id}", status_code=303)


@app.post("/servers/{server_id}/delete")
def servers_delete(server_id: int):
    db.delete_server(server_id)
    return RedirectResponse("/", status_code=303)


@app.post("/server/{server_id}/refresh")
async def server_refresh(server_id: int):
    server = db.get_server(server_id)
    if not server:
        raise HTTPException(404)
    await poll_server(server)
    return RedirectResponse(f"/server/{server_id}", status_code=303)


# ---------- JSON API ----------

@app.get("/api/servers")
def api_servers():
    latest = db.latest_snapshots()
    out = []
    for s in db.list_servers():
        snap, players = latest.get(s["id"], (None, []))
        out.append({"server": dict(s), "snapshot": snap, "players": players})
    return out


@app.get("/api/server/{server_id}/history")
def api_history(server_id: int, limit: int = 200):
    if not db.get_server(server_id):
        raise HTTPException(404)
    limit = min(max(limit, 1), 2000)
    return db.history(server_id, limit)


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host=os.environ.get("MC_DASH_HOST", "0.0.0.0"), port=int(os.environ.get("MC_DASH_PORT", "18000")))
