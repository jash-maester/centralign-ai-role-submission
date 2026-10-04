"""Track H: GET /stream (SSE) backlog, run filter, Last-Event-ID resume, live tail."""

from __future__ import annotations

import json
import threading
import time

import pytest
import redis

from ledger_core.events import encode_event
from ledger_core.protocol import Event, EventType
from ledger_core.settings import get_settings

from api_helpers import api_client, sse_events


@pytest.fixture
def client(ns, r):
    with api_client(ns) as c:
        yield c


def _ids(frames):
    return [f["id"] for f in frames]


def test_backlog_for_run_and_resume(client):
    rid = client.post("/runs", json={"goal": "stream me"}).json()["id"]
    other = client.post("/runs", json={"goal": "other"}).json()["id"]
    body = client.get("/stream", params={"run_id": rid, "follow": "false"})
    assert body.status_code == 200
    assert body.headers["content-type"].startswith("text/event-stream")
    frames = sse_events(body.text)
    assert [f["event"] for f in frames] == ["run.created", "run.config_updated"]
    data = [json.loads(f["data"]) for f in frames]
    assert all(d["run_id"] == rid for d in data) and data[0]["id"] == frames[0]["id"]
    assert "retry: 2000" in body.text

    # Last-Event-ID resume: only what came after
    client.put(f"/runs/{rid}/config", json={"dry_run": True})
    resumed = sse_events(client.get("/stream", params={"run_id": rid, "follow": "false"},
                                    headers={"Last-Event-ID": frames[-1]["id"]}).text)
    assert [f["event"] for f in resumed] == ["run.config_updated"]
    assert json.loads(resumed[0]["data"])["payload"]["diff"] == {"dry_run": [False, True]}
    # query-string variant
    q = sse_events(client.get("/stream", params={"run_id": rid, "follow": "false",
                                                 "last_event_id": frames[0]["id"]}).text)
    assert _ids(q) == [frames[1]["id"], resumed[0]["id"]]
    # unfiltered stream with a resume id sees both runs
    allrun = sse_events(client.get("/stream", params={"follow": "false", "last_event_id": "0-0"}).text)
    assert {json.loads(f["data"])["run_id"] for f in allrun} == {rid, other}


def test_from_now_skips_backlog(client):
    rid = client.post("/runs", json={"goal": "x"}).json()["id"]
    frames = sse_events(client.get("/stream", params={"run_id": rid, "from": "now", "follow": "false"}).text)
    assert frames == []


def test_live_tail_delivers_new_events(client, ns):
    from ledger_core.keys import Keys

    rid = client.post("/runs", json={"goal": "live"}).json()["id"]
    keys = Keys(ns)
    sync = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)

    def later():
        time.sleep(0.6)
        ev = Event(run_id=rid, actor="test", type=EventType.STEP_OBSERVATION, payload={"saw": "login page"})
        sync.xadd(keys.events, encode_event(ev))
        ev2 = Event(run_id="run_other", actor="test", type=EventType.STEP_OBSERVATION, payload={})
        sync.xadd(keys.events, encode_event(ev2))

    t = threading.Thread(target=later)
    started = time.monotonic()
    t.start()
    body = client.get("/stream", params={"run_id": rid, "from": "now", "max_s": 2.5}).text
    t.join()
    sync.close()
    frames = sse_events(body)
    assert [f["event"] for f in frames] == ["step.observation"]
    assert json.loads(frames[0]["data"])["payload"] == {"saw": "login page"}
    assert time.monotonic() - started < 6
