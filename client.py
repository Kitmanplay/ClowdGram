#!/usr/bin/env python3
"""ClawdGram — клиент (окно).  Запуск:  python client.py"""
import json
import os
import queue
import socket
import threading
import time
import traceback
import tkinter as tk
from tkinter import colorchooser, messagebox

CFG_PATH = os.path.join(os.path.expanduser("~"), ".clawdgram.json")
GENERAL = "#general"
BG, SIDE, PANEL = "#0e1621", "#17212b", "#1f2d3b"
ME_BG, OTHER_BG = "#2b5278", "#182533"
FG, MUTED, ACCENT, GREEN, RED = "#f5f5f5", "#7f91a4", "#5eb5f7", "#4fcf70", "#ec6a6a"
FONT = ("Segoe UI", 10)
EMOJI = "😀 😂 😍 😎 😭 😡 👍 👎 🙏 🔥 ❤ 🎮 ⛏ 💀 🤔 😴 🎉 👀 💯 ✅".split()


def fmt_time(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def fmt_seen(p):
    if p.get("online"):
        return "в сети"
    ts = p.get("last_seen")
    if not ts:
        return "не в сети"
    day = time.strftime("%d.%m", time.localtime(ts))
    if day == time.strftime("%d.%m"):
        return "был(а) в " + fmt_time(ts)
    return "был(а) " + day + " в " + fmt_time(ts)


def avatar(parent, p, size=40):
    c = tk.Canvas(parent, width=size, height=size, bg=parent["bg"], highlightthickness=0)
    c.create_oval(1, 1, size - 1, size - 1, fill=p.get("color", "#65aadd"), outline="")
    ch = (p.get("name") or p.get("nick") or "?")[:1].upper()
    c.create_text(size / 2, size / 2, text=ch, fill="white",
                  font=("Segoe UI", max(8, int(size * 0.4)), "bold"))
    return c


class Net:
    """Соединение с сервером. Ответы и события кладутся в общую очередь."""

    def __init__(self, q):
        self.q, self.sock, self.rid, self.cbs = q, None, 0, {}
        self.lock = threading.Lock()

    def connect(self, host, port):
        s = socket.create_connection((host, port), timeout=6)
        s.settimeout(None)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self.sock = s
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for line in self.sock.makefile("r", encoding="utf-8"):
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                m["_src"] = self
                self.q.put(m)
        except (OSError, ValueError):
            pass
        self.q.put({"ev": "disconnected", "_src": self})

    def call(self, op, cb=None, **kw):
        with self.lock:
            self.rid += 1
            if cb:
                self.cbs[self.rid] = cb
            data = json.dumps({"op": op, "rid": self.rid, **kw}, ensure_ascii=False) + "\n"
            try:
                self.sock.sendall(data.encode("utf-8"))
            except (OSError, AttributeError):
                pass

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("ClawdGram")
        self.root.geometry("1000x660")
        self.root.minsize(780, 500)
        self.root.configure(bg=BG)
        self.q = queue.Queue()
        self.net = None
        self.me = None
        self.busy = False
        self.cfg = self.load_cfg()
        self.chats, self.cache, self.names = {}, {}, {}
        self.cur = None
        self.mode, self.items = "chats", []
        self.typing_until, self.last_typing_sent = 0, 0
        self.search_job = None
        self.frame = None
        self.last_sender, self.last_ts = None, 0
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.show_login()
        self.root.after(60, self.poll)

    # ---------- конфиг ----------
    def load_cfg(self):
        try:
            with open(CFG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_cfg(self):
        try:
            with open(CFG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f)
        except Exception:
            pass

    # ---------- вход ----------
    def clear(self):
        if self.frame:
            self.frame.destroy()
        self.frame = tk.Frame(self.root, bg=BG)
        self.frame.pack(fill="both", expand=True)

    def show_login(self, note=""):
        self.clear()
        self.busy = False
        f = tk.Frame(self.frame, bg=SIDE, padx=36, pady=30)
        f.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(f, text="✈ ClawdGram", bg=SIDE, fg=ACCENT,
                 font=("Segoe UI", 24, "bold")).pack(pady=(0, 4))
        tk.Label(f, text="Мессенджер для своих", bg=SIDE, fg=MUTED, font=FONT).pack(pady=(0, 16))

        def field(label, value="", show=None):
            tk.Label(f, text=label, bg=SIDE, fg=MUTED, font=("Segoe UI", 9), anchor="w").pack(fill="x")
            e = tk.Entry(f, bg=PANEL, fg=FG, insertbackground=FG, relief="flat", show=show,
                         font=("Segoe UI", 11), width=30)
            e.insert(0, value)
            e.pack(fill="x", ipady=5, pady=(0, 8))
            return e

        self.e_server = field("Сервер (адрес:порт)", self.cfg.get("server", "127.0.0.1:8765"))
        self.e_nick = field("Ник (латиница, цифры, _)", self.cfg.get("nick", ""))
        self.e_pwd = field("Пароль", show="•")
        self.e_name = field("Имя (только при регистрации)", self.cfg.get("name", ""))
        self.status = tk.Label(f, text=note, bg=SIDE, fg=RED, font=("Segoe UI", 9), wraplength=260)
        self.status.pack(pady=(0, 6))
        row = tk.Frame(f, bg=SIDE)
        row.pack(fill="x")
        for text, mode, bg in (("Войти", "login", ACCENT), ("Регистрация", "register", PANEL)):
            tk.Button(row, text=text, command=lambda m=mode: self.auth(m), bg=bg,
                      fg="white" if mode == "register" else "#0e1621", relief="flat",
                      activebackground=bg, font=("Segoe UI", 10, "bold"), cursor="hand2",
                      padx=14, pady=6).pack(side="left", expand=True, fill="x", padx=3)
        self.e_pwd.bind("<Return>", lambda e: self.auth("login"))
        (self.e_nick if not self.cfg.get("nick") else self.e_pwd).focus_set()

    def auth(self, mode):
        if self.busy:
            return
        server = self.e_server.get().strip()
        host, _, port = server.partition(":")
        try:
            port = int(port or 8765)
        except ValueError:
            self.status.config(text="Порт должен быть числом")
            return
        nick, pwd = self.e_nick.get().strip(), self.e_pwd.get()
        if not host or not nick or not pwd:
            self.status.config(text="Заполни сервер, ник и пароль")
            return
        self.busy = True
        self.status.config(text="Подключение…", fg=MUTED)
        self.pending = (mode, nick, pwd, self.e_name.get().strip())
        self.cfg.update(server=server, nick=nick, name=self.e_name.get().strip())
        if self.net:
            self.net.close()
        net = self.net = Net(self.q)

        def go():
            try:
                net.connect(host, port)
                self.q.put({"ev": "connected", "_src": net})
            except Exception as e:
                self.q.put({"ev": "connect_error", "error": str(e), "_src": net})

        threading.Thread(target=go, daemon=True).start()

    def ev_connected(self, m):
        mode, nick, pwd, name = self.pending
        self.net.call(mode, cb=self.on_auth, nick=nick, password=pwd, name=name)

    def ev_connect_error(self, m):
        self.busy = False
        self.status.config(text="Не удалось подключиться: " + m["error"], fg=RED)

    def on_auth(self, r):
        self.busy = False
        if not r.get("ok"):
            self.status.config(text=r.get("error", "Ошибка"), fg=RED)
            return
        self.me = r["profile"]
        self.save_cfg()
        self.show_main()
        self.reload_chats()
        self.root.after(30000, self.keepalive)

    def keepalive(self):
        if self.me and self.net:
            self.net.call("ping")
            self.root.after(30000, self.keepalive)

    # ---------- главное окно ----------
    def show_main(self):
        self.clear()
        self.chats, self.cache, self.cur, self.mode = {}, {}, None, "chats"
        f = self.frame
        f.columnconfigure(1, weight=1)
        f.rowconfigure(0, weight=1)

        side = tk.Frame(f, bg=SIDE, width=300)
        side.grid(row=0, column=0, sticky="ns")
        side.grid_propagate(False)
        self.top = tk.Frame(side, bg=SIDE)
        self.top.pack(fill="x", padx=10, pady=(10, 4))
        self.draw_top()
        self.search_var = tk.StringVar()
        se = tk.Entry(side, textvariable=self.search_var, bg=PANEL, fg=FG, insertbackground=FG,
                      relief="flat", font=("Segoe UI", 11))
        se.pack(fill="x", padx=10, ipady=6)
        se.bind("<KeyRelease>", self.on_search_key)
        tk.Label(side, text="Поиск: ник, имя или #тег", bg=SIDE, fg=MUTED,
                 font=("Segoe UI", 8)).pack(anchor="w", padx=12, pady=(2, 6))
        self.lb = tk.Listbox(side, bg=SIDE, fg=FG, selectbackground=ME_BG, selectforeground=FG,
                             activestyle="none", bd=0, highlightthickness=0, font=("Segoe UI", 11))
        self.lb.pack(fill="both", expand=True)
        self.lb.bind("<<ListboxSelect>>", self.on_pick)

        right = tk.Frame(f, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        self.head = tk.Frame(right, bg=SIDE, height=58)
        self.head.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.head.grid_propagate(False)

        self.txt = tk.Text(right, bg=BG, fg=FG, bd=0, highlightthickness=0, wrap="word",
                           font=("Segoe UI", 11), state="disabled", cursor="arrow", padx=6, pady=6)
        sb = tk.Scrollbar(right, command=self.txt.yview)
        self.txt.config(yscrollcommand=sb.set)
        self.txt.grid(row=1, column=0, sticky="nsew")
        sb.grid(row=1, column=1, sticky="ns")
        t = self.txt
        t.tag_config("me", background=ME_BG, justify="right", lmargin1=150, lmargin2=150,
                     rmargin=14, spacing1=2, spacing3=6)
        t.tag_config("other", background=OTHER_BG, justify="left", lmargin1=14, lmargin2=14,
                     rmargin=150, spacing1=2, spacing3=6)
        t.tag_config("hdr_me", foreground=MUTED, justify="right", rmargin=14, lmargin1=150,
                     font=("Segoe UI", 8), spacing1=8)
        t.tag_config("hdr_other", foreground=ACCENT, justify="left", lmargin1=14, rmargin=150,
                     font=("Segoe UI", 8, "bold"), spacing1=8)
        t.tag_config("hint", foreground=MUTED, justify="center", spacing1=30)
        t.bind("<Button-3>", self.on_msg_menu)
        t.bind("<Button-2>", self.on_msg_menu)

        bar = tk.Frame(right, bg=SIDE)
        bar.grid(row=2, column=0, columnspan=2, sticky="ew")
        bar.columnconfigure(1, weight=1)
        tk.Button(bar, text="😊", command=self.emoji_menu, bg=SIDE, fg=FG, relief="flat",
                  activebackground=SIDE, font=("Segoe UI", 14), cursor="hand2").grid(row=0, column=0, padx=6)
        self.input = tk.Text(bar, height=2, bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                             font=("Segoe UI", 11), wrap="word", padx=8, pady=6)
        self.input.grid(row=0, column=1, sticky="ew", pady=8)
        self.input.bind("<Return>", self.send)
        self.input.bind("<Shift-Return>", lambda e: None)
        self.input.bind("<Key>", self.on_key)
        tk.Button(bar, text="Отправить", command=self.send, bg=ACCENT, fg="#0e1621", relief="flat",
                  activebackground=ACCENT, font=("Segoe UI", 10, "bold"), cursor="hand2",
                  padx=12, pady=6).grid(row=0, column=2, padx=8)
        self.draw_header()
        self.render_chat()

    def draw_top(self):
        for w in self.top.winfo_children():
            w.destroy()
        avatar(self.top, self.me, 42).pack(side="left")
        info = tk.Frame(self.top, bg=SIDE)
        info.pack(side="left", padx=8)
        tk.Label(info, text=self.me["name"], bg=SIDE, fg=FG, font=("Segoe UI", 11, "bold")).pack(anchor="w")
        tk.Label(info, text="@" + self.me["nick"], bg=SIDE, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w")
        tk.Button(self.top, text="⚙", command=self.edit_profile, bg=SIDE, fg=FG, relief="flat",
                  activebackground=SIDE, font=("Segoe UI", 14), cursor="hand2").pack(side="right")

    # ---------- список чатов и поиск ----------
    def refresh_list(self):
        self.lb.delete(0, "end")
        self.items = []
        if self.mode == "search":
            rows = self.search_rows
        else:
            rows = sorted(self.chats.values(), key=lambda c: (c["key"] != GENERAL,
                          -((c.get("last") or {}).get("id", 0))))
        for c in rows:
            if c.get("empty"):
                self.lb.insert("end", "  Ничего не найдено")
                self.lb.itemconfig("end", fg=MUTED)
                self.items.append(None)
                continue
            if c["kind"] == "room":
                label, color = "  📢  Общий чат", FG
            else:
                p = c["profile"]
                label = "  %s  %s  @%s" % ("●" if p.get("online") else "○", p["name"], p["nick"])
                color = GREEN if p.get("online") else FG
            if c.get("unread"):
                label += "   [%d]" % c["unread"]
                color = ACCENT
            self.lb.insert("end", label)
            self.lb.itemconfig("end", fg=color)
            self.items.append(c)
        for i, c in enumerate(self.items):
            if c and self.mode == "chats" and c["key"] == self.cur:
                self.lb.selection_set(i)
        total = sum(c.get("unread", 0) for c in self.chats.values())
        self.root.title("ClawdGram" + (" (%d)" % total if total else ""))

    def reload_chats(self):
        self.net.call("chats", cb=self.on_chats)

    def on_chats(self, r):
        if not r.get("ok"):
            return
        new = {c["key"]: c for c in r["chats"]}
        if self.cur and self.cur not in new and self.cur in self.chats:
            new[self.cur] = self.chats[self.cur]
        if self.cur in new:
            new[self.cur]["unread"] = 0
        for c in new.values():
            if c.get("profile"):
                self.names[c["key"]] = c["profile"]["name"]
        self.chats = new
        if self.mode == "chats":
            self.refresh_list()

    def on_search_key(self, e=None):
        if self.search_job:
            self.root.after_cancel(self.search_job)
        self.search_job = self.root.after(300, self.do_search)

    def do_search(self):
        q = self.search_var.get().strip()
        if not q:
            self.mode = "chats"
            self.refresh_list()
            return
        self.net.call("search", cb=lambda r, q=q: self.on_results(q, r), q=q)

    def on_results(self, q, r):
        if not r.get("ok") or q != self.search_var.get().strip():
            return
        self.mode = "search"
        self.search_rows = [{"key": u["nick"], "kind": "dm", "profile": u, "unread": 0, "last": None}
                            for u in r["users"]] or [{"empty": True}]
        self.refresh_list()

    def on_pick(self, e=None):
        sel = self.lb.curselection()
        if not sel or sel[0] >= len(self.items) or not self.items[sel[0]]:
            return
        item = self.items[sel[0]]
        if self.mode == "search":
            self.chats.setdefault(item["key"], item)
            self.search_var.set("")
            self.mode = "chats"
        self.open_chat(item["key"])

    # ---------- чат ----------
    def open_chat(self, key):
        self.cur = key
        self.chats[key]["unread"] = 0
        self.typing_until = 0
        self.draw_header()
        self.render_chat()
        self.net.call("history", cb=lambda r, k=key: self.on_history(k, r), peer=key)
        self.net.call("read", peer=key)
        self.refresh_list()
        self.input.focus_set()

    def on_history(self, k, r):
        if not r.get("ok"):
            return
        self.cache[k] = r["messages"]
        for m in r["messages"]:
            self.names[m["sender"]] = m["sname"]
        if self.cur == k:
            self.render_chat()

    def draw_header(self):
        for w in self.head.winfo_children():
            w.destroy()
        c = self.chats.get(self.cur)
        if not c:
            return
        if c["kind"] == "room":
            tk.Label(self.head, text="📢", bg=SIDE, fg=FG, font=("Segoe UI", 20)).pack(side="left", padx=12)
            title, self.sub_text = "Общий чат", "Здесь пишут все пользователи"
        else:
            p = c["profile"]
            avatar(self.head, p, 38).pack(side="left", padx=12)
            title, self.sub_text = p["name"], fmt_seen(p)
        box = tk.Frame(self.head, bg=SIDE)
        box.pack(side="left")
        tk.Label(box, text=title, bg=SIDE, fg=FG, font=("Segoe UI", 12, "bold")).pack(anchor="w")
        self.sub = tk.Label(box, text=self.sub_text, bg=SIDE, fg=MUTED, font=("Segoe UI", 9))
        self.sub.pack(anchor="w")
        if c["kind"] == "dm":
            tk.Button(self.head, text="Профиль", command=lambda: self.show_profile(self.cur),
                      bg=PANEL, fg=FG, relief="flat", activebackground=PANEL, cursor="hand2",
                      padx=10).pack(side="right", padx=12)

    def render_chat(self):
        t = self.txt
        t.config(state="normal")
        t.delete("1.0", "end")
        self.last_sender, self.last_ts = None, 0
        msgs = self.cache.get(self.cur, [])
        if not self.cur:
            t.insert("end", "Выбери чат слева или найди друга по нику ✈\n", "hint")
        elif not msgs:
            t.insert("end", "Сообщений пока нет. Напиши первым!\n", "hint")
        for m in msgs:
            self._put(m)
        t.config(state="disabled")
        t.see("end")

    def _put(self, m):
        t = self.txt
        mine = m["sender"] == self.me["nick"]
        side = "me" if mine else "other"
        if m["sender"] != self.last_sender or m["ts"] - self.last_ts > 300:
            who = "Ты" if mine else m.get("sname") or m["sender"]
            t.insert("end", "%s · %s\n" % (who, fmt_time(m["ts"])), "hdr_" + side)
        t.insert("end", m["text"] + "\n", (side, "msg_%d" % m["id"]))
        self.last_sender, self.last_ts = m["sender"], m["ts"]

    def conv_key(self, m):
        if m["to"] == GENERAL:
            return GENERAL
        return m["to"] if m["sender"] == self.me["nick"] else m["sender"]

    def add_msg(self, m):
        key = self.conv_key(m)
        self.names[m["sender"]] = m["sname"]
        lst = self.cache.get(key)
        if lst is not None and not any(x["id"] == m["id"] for x in lst):
            lst.append(m)
            if self.cur == key:
                if len(lst) == 1:
                    self.render_chat()
                else:
                    self.txt.config(state="normal")
                    self._put(m)
                    self.txt.config(state="disabled")
                    self.txt.see("end")
        mine = m["sender"] == self.me["nick"]
        if key in self.chats:
            self.chats[key]["last"] = m
            if self.cur == key or mine:
                self.chats[key]["unread"] = 0
            else:
                self.chats[key]["unread"] = self.chats[key].get("unread", 0) + 1
        if self.cur == key and not mine:
            self.net.call("read", peer=key)
        if key not in self.chats:
            self.reload_chats()
        elif self.mode == "chats":
            self.refresh_list()

    def send(self, e=None):
        text = self.input.get("1.0", "end").strip()
        if e is not None and (e.state & 0x1):  # Shift+Enter — новая строка
            return None
        if not text or not self.cur:
            return "break"
        self.input.delete("1.0", "end")
        self.net.call("send", cb=self.on_sent, to=self.cur, text=text)
        return "break"

    def on_sent(self, r):
        if r.get("ok"):
            self.add_msg(r["msg"])
        else:
            self.flash(r.get("error", "Не отправлено"))

    def flash(self, text):
        if hasattr(self, "sub") and self.sub.winfo_exists():
            self.sub.config(text=text, fg=RED)
            self.root.after(3000, self.restore_sub)

    def restore_sub(self):
        if hasattr(self, "sub") and self.sub.winfo_exists():
            self.sub.config(text=self.sub_text, fg=MUTED)

    def on_key(self, e=None):
        now = time.time()
        if self.cur and now - self.last_typing_sent > 2.5 and e and e.keysym not in ("Return", "Shift_L", "Shift_R"):
            self.last_typing_sent = now
            self.net.call("typing", to=self.cur)

    def emoji_menu(self):
        sub = tk.Toplevel(self.root)
        sub.overrideredirect(True)
        sub.configure(bg=PANEL)
        x = self.root.winfo_pointerx() - 100
        y = self.root.winfo_pointery() - 160
        sub.geometry("+%d+%d" % (x, y))
        for i, em in enumerate(EMOJI):
            tk.Button(sub, text=em, bg=PANEL, fg=FG, relief="flat", activebackground=ME_BG,
                      font=("Segoe UI", 14), cursor="hand2",
                      command=lambda em=em, w=sub: (self.input.insert("insert", em), w.destroy(),
                                                    self.input.focus_set())
                      ).grid(row=i // 5, column=i % 5)
        sub.bind("<FocusOut>", lambda e: sub.destroy())
        sub.focus_force()

    def on_msg_menu(self, e):
        idx = self.txt.index("@%d,%d" % (e.x, e.y))
        mid = None
        for tg in self.txt.tag_names(idx):
            if tg.startswith("msg_"):
                mid = int(tg[4:])
        if mid is None:
            return
        m = next((x for x in self.cache.get(self.cur, []) if x["id"] == mid), None)
        if not m:
            return
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Копировать", command=lambda: (self.root.clipboard_clear(),
                                                               self.root.clipboard_append(m["text"])))
        if m["sender"] == self.me["nick"]:
            menu.add_command(label="Удалить", command=lambda: self.net.call("delete", id=mid))
        menu.tk_popup(e.x_root, e.y_root)

    # ---------- события от сервера ----------
    def ev_msg(self, e):
        self.add_msg(e["msg"])

    def ev_deleted(self, e):
        key = GENERAL if e["to"] == GENERAL else (
            e["to"] if e["sender"] == self.me["nick"] else e["sender"])
        lst = self.cache.get(key)
        if lst is not None:
            self.cache[key] = [m for m in lst if m["id"] != e["id"]]
            if self.cur == key:
                self.render_chat()
        self.reload_chats()

    def ev_typing(self, e):
        if (e["to"] == GENERAL and self.cur == GENERAL) or (e["to"] != GENERAL and e["from"] == self.cur):
            self.typing_until = time.time() + 4
            name = self.names.get(e["from"], e["from"])
            if hasattr(self, "sub") and self.sub.winfo_exists():
                self.sub.config(text=("%s печатает…" % name) if e["to"] == GENERAL else "печатает…", fg=GREEN)

    def ev_presence(self, e):
        c = self.chats.get(e["nick"])
        if c and c.get("profile"):
            c["profile"]["online"] = e["online"]
            c["profile"]["last_seen"] = e["last_seen"]
            if self.cur == e["nick"]:
                self.draw_header()
            if self.mode == "chats":
                self.refresh_list()

    def ev_profile(self, e):
        p = e["profile"]
        self.names[p["nick"]] = p["name"]
        c = self.chats.get(p["nick"])
        if c:
            c["profile"] = p
            if self.cur == p["nick"]:
                self.draw_header()
            if self.mode == "chats":
                self.refresh_list()

    def ev_disconnected(self, e):
        if self.me:
            self.me = None
            messagebox.showwarning("ClawdGram", "Соединение с сервером потеряно")
            self.show_login("Соединение потеряно. Войди снова.")

    # ---------- профили ----------
    def show_profile(self, nick):
        self.net.call("profile_get", cb=self.on_profile_view, nick=nick)

    def on_profile_view(self, r):
        if not r.get("ok"):
            self.flash(r.get("error", "Ошибка"))
            return
        p = r["profile"]
        w = tk.Toplevel(self.root)
        w.title("Профиль @" + p["nick"])
        w.configure(bg=SIDE, padx=24, pady=20)
        w.transient(self.root)
        avatar(w, p, 80).pack()
        tk.Label(w, text=p["name"], bg=SIDE, fg=FG, font=("Segoe UI", 15, "bold")).pack(pady=(8, 0))
        tk.Label(w, text="@" + p["nick"], bg=SIDE, fg=ACCENT, font=FONT).pack()
        tk.Label(w, text=fmt_seen(p), bg=SIDE, fg=GREEN if p["online"] else MUTED, font=("Segoe UI", 9)).pack(pady=(2, 8))
        if p["status"]:
            tk.Label(w, text="“" + p["status"] + "”", bg=SIDE, fg=FG, font=("Segoe UI", 10, "italic"),
                     wraplength=280).pack(pady=2)
        if p["bio"]:
            tk.Label(w, text=p["bio"], bg=SIDE, fg=MUTED, font=FONT, wraplength=280, justify="center").pack(pady=4)
        if p["tags"]:
            row = tk.Frame(w, bg=SIDE)
            row.pack(pady=6)
            for t in p["tags"]:
                lb = tk.Label(row, text="#" + t, bg=PANEL, fg=ACCENT, padx=8, pady=2, cursor="hand2")
                lb.pack(side="left", padx=3)
                lb.bind("<Button-1>", lambda e, t=t, win=w: (win.destroy(), self.search_var.set("#" + t),
                                                            self.do_search()))
        tk.Label(w, text="Регистрация: " + time.strftime("%d.%m.%Y", time.localtime(p["created"] or 0)),
                 bg=SIDE, fg=MUTED, font=("Segoe UI", 8)).pack(pady=(8, 0))
        if p["nick"] != self.me["nick"]:
            def write():
                w.destroy()
                self.chats.setdefault(p["nick"], {"key": p["nick"], "kind": "dm", "profile": p,
                                                  "unread": 0, "last": None})
                self.open_chat(p["nick"])
            tk.Button(w, text="Написать", command=write, bg=ACCENT, fg="#0e1621", relief="flat",
                      font=("Segoe UI", 10, "bold"), cursor="hand2", padx=14, pady=5).pack(pady=(10, 0))

    def edit_profile(self):
        me = self.me
        w = tk.Toplevel(self.root)
        w.title("Мой профиль")
        w.configure(bg=SIDE, padx=22, pady=18)
        w.transient(self.root)
        color = [me["color"]]

        def label(t):
            tk.Label(w, text=t, bg=SIDE, fg=MUTED, font=("Segoe UI", 9), anchor="w").pack(fill="x")

        def entry(v):
            e = tk.Entry(w, bg=PANEL, fg=FG, insertbackground=FG, relief="flat", font=("Segoe UI", 11), width=36)
            e.insert(0, v)
            e.pack(fill="x", ipady=4, pady=(0, 8))
            return e

        label("Имя")
        e_name = entry(me["name"])
        label("Статус")
        e_status = entry(me["status"])
        label("О себе")
        t_bio = tk.Text(w, height=4, bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                        font=("Segoe UI", 11), width=36, wrap="word")
        t_bio.insert("1.0", me["bio"])
        t_bio.pack(fill="x", pady=(0, 8))
        label("Теги через запятую (до 5): minecraft, моды, python")
        e_tags = entry(", ".join(me["tags"]))
        prev = tk.Label(w, text="  Цвет аватарки  ", bg=color[0], fg="white", font=("Segoe UI", 10, "bold"))

        def pick():
            c = colorchooser.askcolor(color=color[0], parent=w)[1]
            if c:
                color[0] = c
                prev.config(bg=c)
        row = tk.Frame(w, bg=SIDE)
        row.pack(fill="x", pady=(0, 10))
        prev.pack(in_=row, side="left")
        tk.Button(row, text="Выбрать…", command=pick, bg=PANEL, fg=FG, relief="flat").pack(side="left", padx=8)
        msg = tk.Label(w, text="", bg=SIDE, fg=RED)
        msg.pack()

        def save():
            self.net.call("profile_update", cb=done, name=e_name.get(), status=e_status.get(),
                          bio=t_bio.get("1.0", "end"), tags=e_tags.get(), color=color[0])

        def done(r):
            if not r.get("ok"):
                msg.config(text=r.get("error", "Ошибка"))
                return
            self.me = r["profile"]
            self.draw_top()
            w.destroy()
        tk.Button(w, text="Сохранить", command=save, bg=ACCENT, fg="#0e1621", relief="flat",
                  font=("Segoe UI", 10, "bold"), cursor="hand2", padx=14, pady=5).pack(side="left")
        tk.Button(w, text="Выйти из аккаунта", command=lambda: (w.destroy(), self.logout()), bg=SIDE,
                  fg=RED, relief="flat", cursor="hand2").pack(side="right")

    def logout(self):
        self.me = None
        if self.net:
            self.net.close()
        self.show_login()

    # ---------- цикл ----------
    def dispatch(self, m):
        if "rid" in m and "ev" not in m:
            cb = self.net.cbs.pop(m["rid"], None)
            if cb:
                cb(m)
            return
        h = getattr(self, "ev_" + str(m.get("ev")), None)
        if h:
            h(m)

    def poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                if m.get("_src") is not self.net:
                    continue
                try:
                    self.dispatch(m)
                except Exception:
                    traceback.print_exc()
        except queue.Empty:
            pass
        if self.typing_until and time.time() > self.typing_until:
            self.typing_until = 0
            self.restore_sub()
        self.root.after(60, self.poll)

    def on_close(self):
        if self.net:
            self.net.close()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
