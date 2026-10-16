from fastapi.testclient import TestClient

import app
import db
from test_db import tmp_db  # noqa: F401  复用临时库

client = TestClient(app.app)  # 不用 with：不触发 lifespan，不会启动轮询


def snap(t, players, online=1):
    return {"t": t, "ts": f"t{t}", "online": online, "player_count": len(players), "max_players": 20,
            "latency": 50.0, "players": players}


def test_merge_segments():
    rows = [snap(0, ["A"]), snap(30, ["A"]), snap(60, ["A", "B"]), snap(90, ["A", "B"]), snap(600, ["A", "B"])]
    segs = app.merge_segments(rows)[::-1]
    assert [s.get("gap") or s["players"] for s in segs] == [["A"], ["A", "B"], True, ["A", "B"]]
    assert [s["secs"] for s in segs] == [60, 30, 510, 0]
    assert segs[-1]["ongoing"] and not segs[1]["ongoing"]


def test_pages(monkeypatch):
    monkeypatch.setattr(app, "poll_server", lambda server: None)  # 不发真实查询
    db.init_db()
    assert client.post("/servers/add", data={"name": "生存服", "address": "a.example"}, follow_redirects=False).status_code == 303
    assert client.post("/servers/add", data={"name": "x", "address": "a.example"}).status_code == 400
    db.save_snapshot(1, online=True, player_count=1, max_players=20, latency=40.0, players=["Steve"])
    assert "生存服" in client.get("/").text
    for url in ["/server/1", "/server/1/history", "/server/1/history?view=raw&hours=720", "/player/Steve", "/api/servers"]:
        assert client.get(url).status_code == 200, url
    assert client.get("/server/999").status_code == 404
    assert client.get("/player/nobody").status_code == 404
