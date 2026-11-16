import time
from fastapi.testclient import TestClient
from app.main import app


def test_api_roundtrip():
    with TestClient(app) as c:
        assert c.get("/api/health").json()["status"] == "ok"
        assert c.get("/").status_code == 200
        r = c.post("/api/chat", json={"question": "What is the maximum daily dose of Zentrovax?"}).json()
        assert "600 mg" in r["answer"] and r["budget"]["percent"] > 0
        with c.stream("POST", "/api/chat/stream", json={"question": "How many orders were placed in 2026?"}) as s:
            assert any(line.startswith("event: final") for line in s.iter_lines())
        assert c.post("/api/tokenize", json={"text": "hello"}).json()["count"] == 1
        up = c.post("/api/knowledge/upload", files={"files": ("note.txt", b"The vault code is blue.", "text/plain")})
        assert up.status_code == 200 and c.delete("/api/knowledge/docs/note.txt").status_code == 200
        assert c.post("/api/knowledge/upload", files={"files": ("x.exe", b"x", "application/octet-stream")}).status_code == 400
        job = c.post("/api/evals/run", json={"config": {}, "case_ids": ["d01", "s01"]}).json()
        for _ in range(50):
            st = c.get(f"/api/jobs/{job['id']}").json()
            if st["status"] != "running":
                break
            time.sleep(0.2)
        assert st["status"] == "done" and st["result"]["total"] == 2


def test_deployment_protections(monkeypatch):
    import base64
    from app import main

    with TestClient(main.app) as c:
        # password protection: everything except /api/health needs it
        monkeypatch.setattr(main, "PASSWORD", "s3cret")
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/knowledge").status_code == 401
        bad = {"Authorization": "Basic " + base64.b64encode(b"u:wrong").decode()}
        good = {"Authorization": "Basic " + base64.b64encode(b"anyone:s3cret").decode()}
        assert c.get("/api/knowledge", headers=bad).status_code == 401
        assert c.get("/api/knowledge", headers=good).status_code == 200
        monkeypatch.setattr(main, "PASSWORD", "")

        # read-only public demo: no uploads, batch jobs forced onto the simulator
        monkeypatch.setattr(main, "READONLY", True)
        assert c.post("/api/knowledge/upload", files={"files": ("a.txt", b"x", "text/plain")}).status_code == 403
        assert c.post("/api/knowledge/reset-demo").status_code == 403
        assert c.get("/api/health").json()["readonly"] is True
        monkeypatch.setattr(main, "READONLY", False)

        # rate limit
        monkeypatch.setattr(main, "RATE_LIMIT", 2)
        main._hits.clear()
        codes = [c.post("/api/tokenize", json={"text": "hi"}).status_code for _ in range(3)]
        assert codes == [200, 200, 200]   # tokenize is not a model call, so it is not limited
        codes = [c.post("/api/playground", json={"prompt": "hello", "n": 1}).status_code for _ in range(3)]
        assert codes[:2] == [200, 200] and codes[2] == 429
        monkeypatch.setattr(main, "RATE_LIMIT", 0)
