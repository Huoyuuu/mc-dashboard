"""Minecraft 服务器状态面板：FastAPI + mcstatus + SQLite"""
import asyncio
import contextlib
import sqlite3
from math import ceil
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from mcstatus import JavaServer

import db

POLL_INTERVAL = 60  # 秒
POLL_TIMEOUT = 15  # 单台服务器整次查询（含 DNS/SRV 解析）的硬超时，秒
HISTORY_PAGE_SIZE = 60
CHART_MAX_POINTS = 600  # 图表最多点数，超出则按时间桶聚合
RETENTION_DAYS = 90  # 快照保留天数
PRUNE_EVERY = 60  # 每多少个轮询周期清理一次过期快照（约 1 小时）

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


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
        db.save_snapshot(
            server["id"],
            online=True,
            player_count=status.players.online,
            max_players=status.players.max,
            latency=round(status.latency, 1),
            version=status.version.name,
            motd=motd,
            players=[p.name for p in sample],
        )
    except Exception:
        db.save_snapshot(server["id"], online=False)


async def poll_loop():
    cycle = 0
    while True:
        # 任何一次意外（如数据库瞬时锁定）都不应终结监控循环
        try:
            servers = await asyncio.to_thread(db.list_servers)
            await asyncio.gather(*(poll_server(s) for s in servers))
            cycle += 1
            if cycle % PRUNE_EVERY == 0:
                await asyncio.to_thread(db.prune_snapshots, RETENTION_DAYS)
        except Exception:
            pass
        await asyncio.sleep(POLL_INTERVAL)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    task = asyncio.create_task(poll_loop())
    yield
    task.cancel()


app = FastAPI(title="MC 服务器状态面板", lifespan=lifespan)


# ---------- 页面 ----------

@app.get("/")
async def index(request: Request):
    servers = db.list_servers()
    latest = db.latest_snapshots()  # 一次查询取回全部最新快照，避免逐台往返
    cards = []
    for s in servers:
        snap, players = latest.get(s["id"], (None, []))
        cards.append({"server": s, "snap": snap, "players": players})
    return templates.TemplateResponse(request, "index.html", {"cards": cards})


@app.get("/server/{server_id}")
async def server_detail(request: Request, server_id: int):
    server = db.get_server(server_id)
    if not server:
        raise HTTPException(404)
    snap, players = db.latest_snapshot(server_id)
    return templates.TemplateResponse(
        request, "server.html", {"server": server, "snap": snap, "players": players}
    )


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


@app.get("/server/{server_id}/history")
async def server_history(
    request: Request,
    server_id: int,
    page: int = 1,
    hours: int = 24,
):
    server = db.get_server(server_id)
    if not server:
        raise HTTPException(404)
    total = db.history_count(server_id)
    total_pages = max(1, ceil(total / HISTORY_PAGE_SIZE))
    page = min(max(page, 1), total_pages)
    hours = hours if hours in {6, 24, 168, 720} else 24
    rows = db.history_page(server_id, page, HISTORY_PAGE_SIZE)
    chart_rows = db.range_history(server_id, hours)
    online_rows = [r for r in chart_rows if r["online"]]
    online_checks = len(online_rows)
    player_present_checks = sum(1 for r in online_rows if (r["player_count"] or 0) > 0)
    stats = {
        "uptime": round(online_checks / len(chart_rows) * 100, 1) if chart_rows else None,
        "avg_players": round(sum((r["player_count"] or 0) for r in online_rows) / online_checks, 1) if online_checks else None,
        "peak": max((r["player_count"] or 0) for r in online_rows) if online_rows else None,
        "avg_latency": round(sum((r["latency"] or 0) for r in online_rows if r["latency"] is not None) / len([r for r in online_rows if r["latency"] is not None]), 1) if any(r["latency"] is not None for r in online_rows) else None,
        "checks": len(chart_rows),
        "online_checks": online_checks,
        "player_present_checks": player_present_checks,
    }
    start_index = (page - 1) * HISTORY_PAGE_SIZE + 1 if total else 0
    end_index = min(page * HISTORY_PAGE_SIZE, total)
    chart_points = downsample(chart_rows, CHART_MAX_POINTS)  # 统计用原始数据，图表用聚合后数据
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "server": server,
            "rows": rows,
            "chart_rows": chart_points,
            "chart_aggregated": len(chart_points) < len(chart_rows),
            "page": page,
            "page_size": HISTORY_PAGE_SIZE,
            "total": total,
            "total_pages": total_pages,
            "start_index": start_index,
            "end_index": end_index,
            "hours": hours,
            "stats": stats,
            "player_summary": db.player_summary(server_id, hours),
        },
    )


# ---------- 操作 ----------

@app.post("/servers/add")
async def servers_add(name: str = Form(...), address: str = Form(...)):
    clean_name = name.strip()
    clean_address = address.strip()
    if not clean_name or not clean_address:
        raise HTTPException(400, "Name and address are required")
    db.add_server(clean_name, clean_address)
    # 新增后立即查询一次（地址已存在时 add 为 no-op，这里也顺带触发一次刷新）
    servers = [s for s in db.list_servers() if s["address"] == clean_address]
    if servers:
        asyncio.create_task(poll_server(servers[0]))
    return RedirectResponse("/", status_code=303)


@app.post("/servers/{server_id}/update")
async def servers_update(server_id: int, name: str = Form(...), address: str = Form(...)):
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
        asyncio.create_task(poll_server(server))
    return RedirectResponse(f"/server/{server_id}", status_code=303)


@app.post("/servers/{server_id}/delete")
async def servers_delete(server_id: int):
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
async def api_servers():
    out = []
    for s in db.list_servers():
        snap, players = db.latest_snapshot(s["id"])
        out.append({"server": dict(s), "snapshot": dict(snap) if snap else None, "players": players})
    return out


@app.get("/api/server/{server_id}/history")
async def api_history(server_id: int, limit: int = 200):
    if not db.get_server(server_id):
        raise HTTPException(404)
    limit = min(max(limit, 1), 2000)
    return db.history(server_id, limit)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=18006)
