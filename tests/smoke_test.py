"""End-to-end smoke test against a running server: python tests/smoke_test.py http://127.0.0.1:8080"""
import base64, io, json, sys, zipfile
import httpx

B = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
ok = 0
def check(cond, msg):
    global ok
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    ok += 1
    print("  ok -", msg)

H = {"X-Requested-With": "mydrive"}
s = httpx.Client(base_url=B, headers=H)
check(s.post("/api/login", json={"username": "admin", "password": "nope"}).status_code == 401, "bad login rejected")
r = s.post("/api/login", json={"username": "Admin", "password": "adminpass1"}); check(r.status_code == 200, "login (case-insensitive username)")
check(s.get("/api/me").json()["is_admin"], "me is admin")
# CSRF: cookie auth without header must fail on writes
check(httpx.post(B + "/api/mkdir", json={"path": "x"}, cookies=s.cookies).status_code == 403, "CSRF header enforced")

check(s.post("/api/mkdir", json={"path": "Docs/sub"}).status_code == 200, "mkdir nested")
r = s.put("/api/upload", params={"path": "Docs/hello.txt"}, content=b"hello world"); check(r.status_code == 200 and r.json()["size"] == 11, "upload")
r = s.put("/api/upload", params={"path": "Docs/hello.txt"}, content=b"again"); check(r.json()["path"] == "Docs/hello (1).txt", "upload conflict auto-rename")
r = s.put("/api/upload", params={"path": "Docs/hello.txt", "conflict": "error"}, content=b"x"); check(r.status_code == 409, "conflict=error")
items = s.get("/api/files", params={"path": ""}).json()["items"]
check([i["name"] for i in items] == ["Docs", "Shared"], f"root listing has Docs + Shared: {[i['name'] for i in items]}")
check(s.get("/api/download", params={"path": "Docs/hello.txt"}).content == b"hello world", "download")
r = s.get("/api/download", params={"path": "Docs/hello.txt"}, headers={"Range": "bytes=0-4"}); check(r.status_code == 206 and r.content == b"hello", "range request")
check(s.get("/api/download", params={"path": "../etc/passwd"}).status_code == 400, "path traversal rejected")
check(s.get("/api/download", params={"path": "Docs/%2e%2e/%2e%2e"}).status_code in (400, 404), "encoded traversal rejected")
# html must not render inline
s.put("/api/upload", params={"path": "x.html"}, content=b"<script>alert(1)</script>")
r = s.get("/api/download", params={"path": "x.html", "inline": 1}); check(r.headers["content-type"].startswith("text/plain") and "sandbox" in r.headers["content-security-policy"], "html served as text/plain + sandbox")
r = s.get("/api/zip", params={"paths": ["Docs"]}); z = zipfile.ZipFile(io.BytesIO(r.content)); check(sorted(z.namelist()) == ["Docs/hello (1).txt", "Docs/hello.txt"], f"zip folder {z.namelist()}")
check(s.post("/api/move", json={"src": "Docs/hello (1).txt", "dst": "Docs/sub/renamed.txt"}).status_code == 200, "move/rename")
check(s.post("/api/copy", json={"src": "Docs", "dst": "Docs2"}).status_code == 200, "copy folder")
check(s.post("/api/move", json={"src": "Docs", "dst": "Docs/sub/x"}).status_code == 400, "no move into itself")
check(s.post("/api/move", json={"src": "Docs2/hello.txt", "dst": "Docs/hello.txt"}).status_code == 412, "move onto existing refused")
check(len(s.get("/api/search", params={"q": "HELLO"}).json()["items"]) == 2, "search")
# shared folder
check(s.put("/api/upload", params={"path": "Shared/family.txt"}, content=b"for all").status_code == 200, "upload to Shared")
check(s.post("/api/delete", json={"paths": ["Shared"]}).status_code == 400, "cannot delete Shared root")
# trash
r = s.post("/api/delete", json={"paths": ["Docs2"]}); check(r.status_code == 200, "delete -> trash")
t = s.get("/api/trash").json()["items"]; check(len(t) == 1 and t[0]["vpath"] == "Docs2", "trash lists item")
check(s.post(f"/api/trash/{t[0]['id']}/restore").json()["path"] == "Docs2", "restore")
s.post("/api/delete", json={"paths": ["Docs2"]}); tid = s.get("/api/trash").json()["items"][0]["id"]
check(s.delete(f"/api/trash/{tid}").json()["deleted"] == 1, "delete forever")

# shares
sh = s.post("/api/shares", json={"path": "Docs/hello.txt"}).json()
anon = httpx.Client(base_url=B)
check("Download" in anon.get(f"/s/{sh['token']}").text, "share page")
check(anon.get(f"/s/{sh['token']}/dl").content == b"hello world", "share download")
check(anon.get(f"/s/{sh['token']}/dl", params={"path": "../x"}).status_code == 400, "share traversal rejected")
shd = s.post("/api/shares", json={"path": "Docs"}).json()
check("hello.txt" in anon.get(f"/s/{shd['token']}").text, "folder share listing")
check(anon.get(f"/s/{shd['token']}/dl", params={"path": "sub/renamed.txt"}).content == b"again", "folder share file download")
check(anon.get("/s/nope").status_code == 404, "bad share 404")

# API keys
k = s.post("/api/keys", json={"name": "blog", "scope": "projects/blog", "can_write": True}).json()["key"]
ro = s.post("/api/keys", json={"name": "ro", "scope": "", "can_write": False}).json()["key"]
kc = httpx.Client(base_url=B, headers={"Authorization": f"Bearer {k}"})
check(kc.get("/api/files").json()["items"] == [], "scoped key sees empty scope root")
check(kc.put("/api/upload", params={"path": "post.md"}, content=b"# hi").status_code == 200, "scoped key upload (no CSRF header needed)")
check(s.get("/api/download", params={"path": "projects/blog/post.md"}).content == b"# hi", "scoped write landed in projects/blog")
check(kc.get("/api/download", params={"path": "../../Docs/hello.txt"}).status_code == 400, "scoped key cannot escape")
check(kc.get("/api/keys").status_code == 403, "key cannot manage keys")
check(kc.post("/api/delete", json={"paths": [""]}).status_code == 400, "key cannot delete its root")
rc = httpx.Client(base_url=B, headers={"Authorization": f"Bearer {ro}"})
check(rc.get("/api/download", params={"path": "Docs/hello.txt"}).status_code == 200, "ro key read")
check(rc.put("/api/upload", params={"path": "z.txt"}, content=b"z").status_code == 403, "ro key cannot write")
check(httpx.get(B + "/api/files", headers={"Authorization": "Bearer mdk_bad"}).status_code == 401, "bad key 401")

# quota (mom has ~1MB)
m = httpx.Client(base_url=B, headers=H)
m.post("/api/login", json={"username": "mom", "password": "mompass123"})
check(m.put("/api/upload", params={"path": "small.bin"}, content=b"x" * 1000).status_code == 200, "mom small upload")
check(m.put("/api/upload", params={"path": "big.bin"}, content=b"x" * 2_000_000).status_code == 507, "quota enforced")
check(m.put("/api/upload", params={"path": "Shared/big.bin"}, content=b"x" * 2_000_000).status_code == 200, "Shared not counted in quota")
check("Docs" not in [i["name"] for i in m.get("/api/files").json()["items"]], "mom cannot see admin files")
check("family.txt" in [i["name"] for i in m.get("/api/files", params={"path": "Shared"}).json()["items"]], "mom sees Shared")
check(m.get("/api/admin/users").status_code == 403, "non-admin blocked from admin")

# admin
u = s.get("/api/admin/users").json()["items"]; check(len(u) == 2, "admin list users")
mom = [x for x in u if x["username"] == "mom"][0]
check(s.patch(f"/api/admin/users/{mom['id']}", json={"disabled": True}).status_code == 200, "disable user")
check(m.get("/api/me").status_code == 401, "disabled user's session revoked")
s.patch(f"/api/admin/users/{mom['id']}", json={"disabled": False})

# WebDAV
dav = httpx.Client(base_url=B, auth=("admin", "adminpass1"))
check(httpx.request("PROPFIND", B + "/dav/").status_code == 401, "dav needs auth")
r = dav.request("PROPFIND", "/dav/", headers={"Depth": "1"}); check(r.status_code == 207 and "<d:href>/dav/Docs/</d:href>" in r.text and "/dav/Shared/" in r.text, "dav propfind")
check(dav.request("MKCOL", "/dav/DavDir").status_code == 201, "dav mkcol")
check(dav.request("MKCOL", "/dav/DavDir").status_code == 405, "dav mkcol exists")
check(dav.put("/dav/DavDir/a%20b.txt", content=b"dav!").status_code == 201, "dav put")
check(dav.put("/dav/DavDir/a%20b.txt", content=b"dav2").status_code == 204, "dav put overwrite")
check(dav.get("/dav/DavDir/a%20b.txt").content == b"dav2", "dav get")
check(dav.request("MOVE", "/dav/DavDir/a%20b.txt", headers={"Destination": B + "/dav/DavDir/c.txt"}).status_code == 201, "dav move")
check(dav.request("COPY", "/dav/DavDir/c.txt", headers={"Destination": B + "/dav/DavDir/d.txt"}).status_code == 201, "dav copy")
check(dav.request("COPY", "/dav/DavDir/c.txt", headers={"Destination": B + "/dav/DavDir/d.txt", "Overwrite": "F"}).status_code == 412, "dav copy no-overwrite")
r = dav.request("LOCK", "/dav/DavDir/c.txt", content=b"<lockinfo/>"); check(r.status_code == 200 and "Lock-Token" in r.headers, "dav lock")
check(dav.request("DELETE", "/dav/DavDir/d.txt").status_code == 204, "dav delete")
dk = httpx.Client(base_url=B, auth=("admin", k))
r = dk.request("PROPFIND", "/dav/", headers={"Depth": "1"}); check(r.status_code == 207 and "post.md" in r.text, "dav with scoped API key")
check(httpx.request("PROPFIND", B + "/dav/", auth=("mom", k)).status_code == 401, "dav key must match username")

# MCP
def rpc(c, method, params=None, id=1, url="/mcp"):
    r = c.post(url, json={"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}})
    return r
mc = httpx.Client(base_url=B, headers={"Authorization": f"Bearer {k}", "Accept": "application/json, text/event-stream"})
r = rpc(mc, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
check(r.json()["result"]["protocolVersion"] == "2025-06-18", "mcp initialize")
check(mc.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202, "mcp notification 202")
tools = [t["name"] for t in rpc(mc, "tools/list").json()["result"]["tools"]]; check("write_file" in tools and len(tools) == 11, f"mcp tools/list {len(tools)}")
call = lambda c, n, a, url="/mcp": rpc(c, "tools/call", {"name": n, "arguments": a}, url=url).json()["result"]
res = call(mc, "write_file", {"path": "notes/idea.md", "content": "Line 1\n"}); check(not res["isError"], "mcp write_file")
call(mc, "write_file", {"path": "notes/idea.md", "content": "Line 2\n", "mode": "append"})
res = call(mc, "read_file", {"path": "notes/idea.md"}); check(res["content"][0]["text"] == "Line 1\nLine 2\n", "mcp read_file + append")
res = call(mc, "read_file", {"path": "notes/idea.md", "offset": 2, "max_bytes": 4}); check(res["content"][0]["text"].endswith("ne 1"), "mcp paged read")
png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
kc.put("/api/upload", params={"path": "dot.png"}, content=png)
res = call(mc, "read_file", {"path": "dot.png"}); check(res["content"][0]["type"] == "image", "mcp image read")
res = call(mc, "list_files", {"path": ""}); check("notes" in res["content"][0]["text"], "mcp list_files")
res = call(mc, "search", {"query": "idea"}); check("notes/idea.md" in res["content"][0]["text"], "mcp search")
res = call(mc, "move", {"source": "notes/idea.md", "destination": "notes/idea2.md"}); check(not res["isError"], "mcp move")
res = call(mc, "read_file", {"path": "../../Docs/hello.txt"}); check(res["isError"], "mcp traversal blocked")
res = call(mc, "create_share_link", {"path": "notes/idea2.md", "expires_days": 1}); check("/s/" in res["content"][0]["text"], "mcp share link")
res = call(mc, "delete", {"path": "notes/idea2.md"}); check(not res["isError"], "mcp delete -> trash")
res = call(mc, "storage_usage", {}); check("used_bytes" in res["content"][0]["text"], "mcp usage")
murl = httpx.Client(base_url=B)
check(rpc(murl, "tools/list", url=f"/mcp/{ro}").json()["result"]["tools"][0]["name"] == "list_files", "mcp key-in-URL auth")
check(len(rpc(murl, "tools/list", url=f"/mcp/{ro}").json()["result"]["tools"]) == 5, "ro key only gets read tools")
check(call(murl, "write_file", {"path": "q.txt", "content": "x"}, url=f"/mcp/{ro}")["isError"], "ro key write via mcp refused")
check(murl.post("/mcp", json={}).status_code == 401, "mcp without key 401")
check(murl.get(f"/mcp/{ro}").status_code == 405, "mcp GET 405")
b = murl.post(f"/mcp/{ro}", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "id": 2, "method": "nope"}]).json()
check(isinstance(b, list) and b[1]["error"]["code"] == -32601, "mcp batch + unknown method")

# UI
check("MyDrive" in httpx.get(B + "/").text, "index served")
check(httpx.get(B + "/static/app.js").status_code == 200, "app.js served")
print(f"\nALL {ok} CHECKS PASSED")
