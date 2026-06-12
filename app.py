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
HISTORY_PAGE_SIZE = 60

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


async def poll_server(server) -> None:
    """查询单个服务器并写入快照"""
    try:
        srv = await JavaServer.async_lookup(server["address"])
        status = await asyncio.wait_for(srv.async_status(), timeout=10)
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
    while True:
        servers = await asyncio.to_thread(db.list_servers)
        await asyncio.gather(*(poll_server(s) for s in servers))
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
    cards = []
    for s in servers:
        snap, players = db.latest_snapshot(s["id"])
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


@app.get("/server/{server_id}/history")
async def server_history(
    request: Request,
    server_id: int,
    page: int = 1,
    chart_limit: int = 100,
):
    server = db.get_server(server_id)
    if not server:
        raise HTTPException(404)
    total = db.history_count(server_id)
    total_pages = max(1, ceil(total / HISTORY_PAGE_SIZE))
    page = min(max(page, 1), total_pages)
    chart_limit = min(max(chart_limit, 50), 500)
    rows = db.history_page(server_id, page, HISTORY_PAGE_SIZE)
    chart_rows = db.chart_history(server_id, chart_limit)
    start_index = (page - 1) * HISTORY_PAGE_SIZE + 1 if total else 0
    end_index = min(page * HISTORY_PAGE_SIZE, total)
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "server": server,
            "rows": rows,
            "chart_rows": chart_rows,
            "page": page,
            "page_size": HISTORY_PAGE_SIZE,
            "total": total,
            "total_pages": total_pages,
            "start_index": start_index,
            "end_index": end_index,
            "chart_limit": chart_limit,
        },
    )


# ---------- 操作 ----------

@app.post("/servers/add")
async def servers_add(name: str = Form(...), address: str = Form(...)):
    db.add_server(name.strip(), address.strip())
    # 新增后立即查询一次
    servers = [s for s in db.list_servers() if s["address"] == address.strip()]
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
    return db.history(server_id, limit)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=18006)
