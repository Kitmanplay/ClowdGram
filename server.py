#!/usr/bin/env python3
"""ClawdGram — сервер.  Запуск:  python server.py [порт]   (по умолчанию 8765)

Хранит аккаунты и сообщения в файле clawdgram.db рядом со скриптом.
Протокол: JSON-строки по TCP.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sqlite3
import sys
import time
from collections import deque

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
DB_PATH = os.environ.get("CLAWDGRAM_DB") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "clawdgram.db")
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
GENERAL = "#general"
NICK_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
TAG_RE = re.compile(r"^[\w-]{1,20}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_TEXT = 2000
COLORS = ["#e17076", "#7bc862", "#e5ca77", "#65aadd", "#a695e7", "#ee7aae", "#6ec9cb", "#faa774"]

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.create_function("lc", 1, lambda s: (s or "").casefold())
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  nick TEXT UNIQUE COLLATE NOCASE NOT NULL,
  salt BLOB NOT NULL, pwd BLOB NOT NULL,
  name TEXT NOT NULL DEFAULT '', bio TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '',
  color TEXT NOT NULL DEFAULT '#65aadd',
  created REAL, last_seen REAL);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sender TEXT NOT NULL, recipient TEXT NOT NULL,
  text TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_msg_rec ON messages(recipient, id);
CREATE INDEX IF NOT EXISTS idx_msg_snd ON messages(sender, id);
CREATE TABLE IF NOT EXISTS tokens(
  token TEXT PRIMARY KEY, nick TEXT NOT NULL, created REAL);
CREATE TABLE IF NOT EXISTS reads(
  user TEXT, peer TEXT, last_id INTEGER, PRIMARY KEY(user, peer));
""")
db.commit()

online = {}  # ник -> множество подключений (writer)


class Err(Exception):
    pass


def hash_pwd(pwd, salt):
    return hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt, 200_000)


def get_user(nick):
    return db.execute("SELECT * FROM users WHERE nick=?", (nick,)).fetchone()


def public(u):
    return {
        "nick": u["nick"], "name": u["name"] or u["nick"], "bio": u["bio"],
        "tags": [t for t in u["tags"].split(",") if t], "status": u["status"],
        "color": u["color"], "online": u["nick"] in online,
        "last_seen": u["last_seen"], "created": u["created"],
    }


def msg_dict(r):
    u = get_user(r["sender"])
    return {"id": r["id"], "sender": r["sender"],
            "sname": (u["name"] or u["nick"]) if u else r["sender"],
            "to": r["recipient"], "text": r["text"], "ts": r["ts"]}


def send(w, obj):
    try:
        w.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
    except Exception:
        pass


def push(nicks, obj, skip=None):
    for n in nicks:
        for w in list(online.get(n, ())):
            if w is not skip:
                send(w, obj)


def broadcast(obj, skip=None):
    for ws in list(online.values()):
        for w in list(ws):
            if w is not skip:
                send(w, obj)


def esc(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def clean_tags(raw):
    if isinstance(raw, str):
        raw = re.split(r"[,\s]+", raw)
    out = []
    for t in raw or []:
        t = str(t).strip().lstrip("#").casefold()
        if t and TAG_RE.match(t) and t not in out:
            out.append(t)
    return out[:5]


def conv_where(me, peer):
    if peer == GENERAL:
        return "recipient=?", (GENERAL,)
    return ("((sender=? AND recipient=?) OR (sender=? AND recipient=?))",
            (me, peer, peer, me))


def canon_peer(peer):
    if peer == GENERAL:
        return GENERAL
    u = get_user(str(peer))
    if not u:
        raise Err("Пользователь не найден")
    return u["nick"]


def new_token(nick):
    tk = secrets.token_urlsafe(32)
    db.execute("INSERT INTO tokens(token,nick,created) VALUES(?,?,?)", (tk, nick, time.time()))
    db.commit()
    return tk


def go_online(w, sess, u):
    sess["nick"] = u["nick"]
    first = u["nick"] not in online
    online.setdefault(u["nick"], set()).add(w)
    db.execute("UPDATE users SET last_seen=? WHERE nick=?", (time.time(), u["nick"]))
    db.commit()
    if first:
        broadcast({"ev": "presence", "nick": u["nick"], "online": True,
                   "last_seen": time.time()}, skip=w)


async def process(op, req, w, sess):
    if op == "ping":
        return {}

    if op == "register":
        nick = str(req.get("nick", "")).strip()
        pwd = str(req.get("password", ""))
        name = str(req.get("name", "")).strip()[:30]
        if not NICK_RE.match(nick):
            raise Err("Ник: 3–20 символов, только латиница, цифры и _")
        if len(pwd) < 6:
            raise Err("Пароль должен быть не короче 6 символов")
        salt = secrets.token_bytes(16)
        try:
            db.execute(
                "INSERT INTO users(nick,salt,pwd,name,color,created,last_seen) VALUES(?,?,?,?,?,?,?)",
                (nick, salt, hash_pwd(pwd, salt), name, secrets.choice(COLORS),
                 time.time(), time.time()))
            db.commit()
        except sqlite3.IntegrityError:
            raise Err("Этот ник уже занят")
        u = get_user(nick)
        go_online(w, sess, u)
        return {"profile": public(u), "token": new_token(u["nick"])}

    if op == "resume":
        tk = str(req.get("token", ""))
        r = db.execute("SELECT nick FROM tokens WHERE token=?", (tk,)).fetchone()
        u = get_user(r["nick"]) if r else None
        if not u:
            await asyncio.sleep(0.5)
            raise Err("Сессия устарела, войди заново")
        if sess.get("nick") != u["nick"]:
            go_online(w, sess, u)
        return {"profile": public(u), "token": tk}

    if op == "login":
        u = get_user(str(req.get("nick", "")).strip())
        pwd = str(req.get("password", ""))
        if not u or not hmac.compare_digest(hash_pwd(pwd, u["salt"]), u["pwd"]):
            await asyncio.sleep(1)
            raise Err("Неверный ник или пароль")
        go_online(w, sess, u)
        return {"profile": public(u), "token": new_token(u["nick"])}

    me = sess.get("nick")
    if not me:
        raise Err("Сначала нужно войти")

    if op == "logout":
        db.execute("DELETE FROM tokens WHERE token=?", (str(req.get("token", "")),))
        db.commit()
        return {}

    if op == "profile_get":
        u = get_user(str(req.get("nick", "")))
        if not u:
            raise Err("Пользователь не найден")
        return {"profile": public(u)}

    if op == "profile_update":
        u = get_user(me)
        name = str(req.get("name", u["name"])).strip()[:30]
        bio = str(req.get("bio", u["bio"])).strip()[:200]
        status = str(req.get("status", u["status"])).strip()[:60]
        tags = ",".join(clean_tags(req.get("tags", u["tags"].split(","))))
        color = str(req.get("color", u["color"]))
        if not COLOR_RE.match(color):
            color = u["color"]
        db.execute("UPDATE users SET name=?,bio=?,status=?,tags=?,color=? WHERE nick=?",
                   (name, bio, status, tags, color, me))
        db.commit()
        p = public(get_user(me))
        broadcast({"ev": "profile", "profile": p}, skip=w)
        return {"profile": p}

    if op == "search":
        q = str(req.get("q", "")).strip().lstrip("@")[:30]
        if not q:
            return {"users": []}
        if q.startswith("#"):
            t = esc(q[1:].casefold())
            rows = db.execute(
                "SELECT * FROM users WHERE (',' || tags) LIKE ? ESCAPE '\\' AND nick!=? "
                "ORDER BY nick LIMIT 30", ("%," + t + "%", me)).fetchall()
        else:
            c = esc(q.casefold())
            rows = db.execute(
                "SELECT * FROM users WHERE (lc(nick) LIKE ? ESCAPE '\\' OR lc(name) LIKE ? ESCAPE '\\') "
                "AND nick!=? ORDER BY (lc(nick) LIKE ? ESCAPE '\\') DESC, nick LIMIT 30",
                ("%" + c + "%", "%" + c + "%", me, c + "%")).fetchall()
        return {"users": [public(r) for r in rows]}

    if op == "chats":
        chats = []
        g = db.execute("SELECT * FROM messages WHERE recipient=? ORDER BY id DESC LIMIT 1",
                       (GENERAL,)).fetchone()
        lr = db.execute("SELECT last_id FROM reads WHERE user=? AND peer=?", (me, GENERAL)).fetchone()
        unread = db.execute(
            "SELECT COUNT(*) FROM messages WHERE recipient=? AND sender!=? AND id>?",
            (GENERAL, me, lr["last_id"] if lr else 0)).fetchone()[0]
        chats.append({"key": GENERAL, "kind": "room", "profile": None,
                      "last": msg_dict(g) if g else None, "unread": unread})
        rows = db.execute(
            "SELECT CASE WHEN sender=?1 THEN recipient ELSE sender END AS peer, MAX(id) AS mid "
            "FROM messages WHERE (sender=?1 OR recipient=?1) AND recipient!=?2 "
            "GROUP BY peer ORDER BY mid DESC", (me, GENERAL)).fetchall()
        for r in rows:
            u = get_user(r["peer"])
            if not u:
                continue
            last = db.execute("SELECT * FROM messages WHERE id=?", (r["mid"],)).fetchone()
            lr = db.execute("SELECT last_id FROM reads WHERE user=? AND peer=?",
                            (me, u["nick"])).fetchone()
            unread = 0 if u["nick"] == me else db.execute(
                "SELECT COUNT(*) FROM messages WHERE sender=? AND recipient=? AND id>?",
                (u["nick"], me, lr["last_id"] if lr else 0)).fetchone()[0]
            chats.append({"key": u["nick"], "kind": "dm", "profile": public(u),
                          "last": msg_dict(last), "unread": unread})
        return {"chats": chats}

    if op == "history":
        peer = canon_peer(req.get("peer"))
        cond, args = conv_where(me, peer)
        sql = "SELECT * FROM messages WHERE " + cond
        if req.get("before"):
            sql += " AND id<?"
            args = args + (int(req["before"]),)
        rows = db.execute(sql + " ORDER BY id DESC LIMIT 100", args).fetchall()
        return {"peer": peer, "messages": [msg_dict(r) for r in reversed(rows)]}

    if op == "read":
        peer = canon_peer(req.get("peer"))
        cond, args = conv_where(me, peer)
        mx = db.execute("SELECT MAX(id) FROM messages WHERE " + cond, args).fetchone()[0] or 0
        db.execute("INSERT INTO reads(user,peer,last_id) VALUES(?,?,?) "
                   "ON CONFLICT(user,peer) DO UPDATE SET last_id=excluded.last_id", (me, peer, mx))
        db.commit()
        return {}

    if op == "send":
        now = time.time()
        recent = sess["sent"]
        while recent and now - recent[0] > 5:
            recent.popleft()
        if len(recent) >= 8:
            raise Err("Слишком часто, подожди секунду")
        recent.append(now)
        text = str(req.get("text", "")).strip()
        if not text:
            raise Err("Пустое сообщение")
        if len(text) > MAX_TEXT:
            raise Err("Слишком длинное сообщение (максимум %d символов)" % MAX_TEXT)
        to = canon_peer(req.get("to"))
        cur = db.execute("INSERT INTO messages(sender,recipient,text,ts) VALUES(?,?,?,?)",
                         (me, to, text, now))
        db.commit()
        m = msg_dict(db.execute("SELECT * FROM messages WHERE id=?", (cur.lastrowid,)).fetchone())
        ev = {"ev": "msg", "msg": m}
        if to == GENERAL:
            broadcast(ev, skip=w)
        else:
            push({me, to}, ev, skip=w)
        return {"msg": m}

    if op == "delete":
        row = db.execute("SELECT * FROM messages WHERE id=?", (int(req.get("id", 0)),)).fetchone()
        if not row or row["sender"] != me:
            raise Err("Можно удалять только свои сообщения")
        db.execute("DELETE FROM messages WHERE id=?", (row["id"],))
        db.commit()
        ev = {"ev": "deleted", "id": row["id"], "sender": me, "to": row["recipient"]}
        if row["recipient"] == GENERAL:
            broadcast(ev)
        else:
            push({me, row["recipient"]}, ev)
        return {}

    if op == "typing":
        to = canon_peer(req.get("to"))
        ev = {"ev": "typing", "from": me, "to": to}
        if to == GENERAL:
            broadcast(ev, skip=w)
        else:
            push({to}, ev)
        return {}

    raise Err("Неизвестная команда")


class WSWriter:
    """Обёртка: то, что сервер пишет строкой JSON, уходит WebSocket-кадром."""

    def __init__(self, writer):
        self.w = writer

    def write(self, data):
        p = data.rstrip(b"\n")
        n = len(p)
        if n < 126:
            head = bytes([0x81, n])
        elif n < 65536:
            head = bytes([0x81, 126]) + n.to_bytes(2, "big")
        else:
            head = bytes([0x81, 127]) + n.to_bytes(8, "big")
        self.w.write(head + p)

    async def drain(self):
        await self.w.drain()

    def close(self):
        self.w.close()

    def get_extra_info(self, *a):
        return self.w.get_extra_info(*a)


async def ws_recv(reader, raw):
    buf = b""
    while True:
        h = await reader.readexactly(2)
        fin, opcode = h[0] & 0x80, h[0] & 0x0F
        masked, ln = h[1] & 0x80, h[1] & 0x7F
        if ln == 126:
            ln = int.from_bytes(await reader.readexactly(2), "big")
        elif ln == 127:
            ln = int.from_bytes(await reader.readexactly(8), "big")
        if ln > (1 << 20):
            return None
        mask = await reader.readexactly(4) if masked else b""
        data = await reader.readexactly(ln)
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if opcode == 8:
            return None
        if opcode == 9:  # ping -> pong
            raw.write(bytes([0x8A, len(data) & 0x7F]) + data[:125])
            continue
        if opcode == 10:
            continue
        buf += data
        if fin:
            return buf


async def session(first, recv, w, raw):
    sess = {"nick": None, "sent": deque()}
    peer = raw.get_extra_info("peername")
    line = first
    try:
        while True:
            if line is None:
                try:
                    line = await recv()
                except (ValueError, asyncio.LimitOverrunError):
                    break
            if not line:
                break
            try:
                req = json.loads(line)
                if not isinstance(req, dict):
                    line = None
                    continue
            except ValueError:
                line = None
                continue
            line = None
            rid = req.get("rid")
            try:
                res = await process(req.get("op"), req, w, sess)
                out = {"rid": rid, "ok": True, **res}
            except Err as e:
                out = {"rid": rid, "ok": False, "error": str(e)}
            except Exception as e:  # не роняем сервер из-за чужого запроса
                print("Ошибка запроса:", repr(e))
                out = {"rid": rid, "ok": False, "error": "Ошибка сервера"}
            if rid is not None:
                send(w, out)
            await raw.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        nick = sess["nick"]
        if nick:
            ws = online.get(nick)
            if ws:
                ws.discard(w)
                if not ws:
                    del online[nick]
                    now = time.time()
                    db.execute("UPDATE users SET last_seen=? WHERE nick=?", (now, nick))
                    db.commit()
                    broadcast({"ev": "presence", "nick": nick, "online": False, "last_seen": now})
        try:
            raw.close()
        except Exception:
            pass
        print("отключился:", peer, nick or "")


STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/manifest.json": ("manifest.json", "application/manifest+json"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
}


async def handle_http(first, reader, writer):
    try:
        parts = first.decode("latin-1").split()
        path = parts[1].split("?")[0] if len(parts) > 1 else "/"
        headers = {}
        for _ in range(60):
            l = await asyncio.wait_for(reader.readline(), 10)
            if l in (b"\r\n", b"\n", b""):
                break
            k, _, v = l.decode("latin-1").partition(":")
            headers[k.strip().lower()] = v.strip()
        if headers.get("upgrade", "").lower() == "websocket" and "sec-websocket-key" in headers:
            acc = base64.b64encode(hashlib.sha1(
                (headers["sec-websocket-key"] + WS_GUID).encode()).digest()).decode()
            writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                          "Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n" % acc).encode())
            await writer.drain()
            await session(None, lambda: ws_recv(reader, writer), WSWriter(writer), writer)
            return
        item = STATIC.get(path)
        body, ctype, status = b"Not found", "text/plain; charset=utf-8", "404 Not Found"
        if item:
            try:
                with open(os.path.join(WEB_DIR, item[0]), "rb") as f:
                    body, ctype, status = f.read(), item[1], "200 OK"
            except OSError:
                body = "Папка web не найдена рядом с server.py".encode("utf-8")
        if first.startswith(b"HEAD"):
            body_out = b""
        else:
            body_out = body
        writer.write(("HTTP/1.1 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
                      "Cache-Control: no-cache\r\nConnection: close\r\n\r\n"
                      % (status, ctype, len(body))).encode() + body_out)
        await writer.drain()
    except (ConnectionError, asyncio.TimeoutError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle(reader, writer):
    try:
        first = await asyncio.wait_for(reader.readline(), 30)
    except Exception:
        writer.close()
        return
    if first.startswith((b"GET ", b"HEAD ")):
        await handle_http(first, reader, writer)
    else:
        await session(first, reader.readline, writer, writer)


def lan_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for i in socket.gethostbyname_ex(socket.gethostname())[2]:
            ips.add(i)
    except Exception:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", PORT, limit=1 << 16)
    print("ClawdGram сервер запущен на порту %d. База: %s" % (PORT, DB_PATH))
    print("Для приложения на ПК адрес:  localhost:%d" % PORT)
    for ip in lan_ips():
        print("Для телефона открой в браузере:  http://%s:%d" % (ip, PORT))
    print("Остановить: Ctrl+C")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nСервер остановлен")
