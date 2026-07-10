#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot nhac viec tuong tac qua Telegram (chay moi 30' boi GitHub Actions).

Tao / quan ly lich ngay tren Telegram bang tin nhan:
  them <viec>                    -> viec thuong
  nhac 15:00 <viec>              -> nhac hom nay 15:00 (qua gio -> mai)
  nhac 12/07 15:00 <viec>        -> nhac dung ngay/gio
  nhac hangngay 09:00 <viec>     -> nhac moi ngay 09:00
  nhac <viec>                    -> bot hoi "may gio?" roi ban tra loi gio
  doigio N 21:00                 -> doi gio viec so N (ho tro ca ngay / hangngay)
  hoan N 30p                     -> hoan (snooze) viec N: 30 phut nua nhac lai
  lap N 60p                      -> viec N nhac lai moi 60 phut (mac dinh 30)
  ds                             -> xem danh sach
  xoa N                          -> xoa viec N
Bao xong: nut ✅ / voice "viec so N xong" / go "xong N".
"""
import os, re, sys, json, subprocess
from datetime import datetime, timezone, timedelta
import requests

TOKEN = os.environ["TG_TOKEN"]
CHAT  = os.environ["TG_CHAT"]
API   = f"https://api.telegram.org/bot{TOKEN}"

TASKS_FILE    = "viec-can-lam.txt"
OFFSET_FILE   = "state/offset.txt"
DAILY_FILE    = "state/daily_done.json"
SNOOZE_FILE   = "state/snooze.json"
INTERVAL_FILE = "state/interval.json"
LASTSENT_FILE = "state/last_sent.json"
PENDING_FILE  = "state/pending.json"

VN = timezone(timedelta(hours=7))
def now_vn():
    return datetime.now(VN)

NUM_WORDS = {"mot":1,"một":1,"hai":2,"ba":3,"bon":4,"bốn":4,"tu":4,"tư":4,"nam":5,"năm":5,
             "sau":6,"sáu":6,"bay":7,"bảy":7,"tam":8,"tám":8,"chin":9,"chín":9,"muoi":10,"mười":10}
DONE_HINTS = ("xong","hoan thanh","hoàn thành","da lam","đã làm","lam xong","làm xong","done")
CMD_PREFIX = ("them","/them","nhac","nhắc","/nhac","xoa","xóa","doigio","hoan","hoãn",
              "lap","lặp","moi","mỗi","ds","danhsach")

HELP = ("🤖 Lệnh bot nhắc việc:\n"
        "• them <việc> — việc thường\n"
        "• nhac 20:00 <việc> — nhắc hôm nay 20:00\n"
        "• nhac 12/07 15:00 <việc> — nhắc ngày & giờ\n"
        "• nhac hangngay 09:00 <việc> — mỗi ngày\n"
        "• nhac <việc> — bot sẽ hỏi mấy giờ\n"
        "• doigio N 21:00 — đổi giờ việc N\n"
        "• hoan N 30p — hoãn việc N 30 phút\n"
        "• lap N 60p — việc N nhắc lại mỗi 60 phút\n"
        "• ds — danh sách | xoa N — xoá | xong N — xong")


def tg(method, **params):
    try:
        return requests.post(f"{API}/{method}", json=params, timeout=60).json()
    except Exception as e:
        print("TG error", method, e); return {}


# ---------------- tasks: parse / render / io ----------------
def parse_meta(s):
    s = s.strip()
    m = re.match(r"^🔁\s*(\d{1,2}):(\d{2})\s*—\s*(.*)$", s)
    if m:
        return {"kind": "daily", "hh": int(m.group(1)), "mm": int(m.group(2)), "text": m.group(3).strip()}
    m = re.match(r"^⏰\s*(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})\s*—\s*(.*)$", s)
    if m:
        return {"kind": "once", "y": int(m.group(1)), "mo": int(m.group(2)), "d": int(m.group(3)),
                "hh": int(m.group(4)), "mm": int(m.group(5)), "text": m.group(6).strip()}
    return {"kind": "plain", "text": s}


def render(t):
    box = "[x] " if t["done"] else "[ ] "
    m = t["meta"]
    if m["kind"] == "plain":
        body = m["text"]
    elif m["kind"] == "once":
        body = "⏰ %04d-%02d-%02d %02d:%02d — %s" % (m["y"], m["mo"], m["d"], m["hh"], m["mm"], m["text"])
    else:
        body = "🔁 %02d:%02d — %s" % (m["hh"], m["mm"], m["text"])
    return box + body


def load_tasks():
    if not os.path.exists(TASKS_FILE):
        return [], []
    lines = open(TASKS_FILE, encoding="utf-8").read().split("\n")
    tasks = []
    for i, l in enumerate(lines):
        mm = re.match(r"^\s*\[( |x|X)\]\s*(.*)$", l)
        if mm:
            tasks.append({"lineno": i, "done": mm.group(1).lower() == "x", "meta": parse_meta(mm.group(2))})
    return lines, tasks


def load_json(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(obj, open(path, "w", encoding="utf-8"), ensure_ascii=False)


# ---------------- schedule / duration parsing ----------------
def parse_hm(tok):
    m = re.match(r"^(\d{1,2})[:h](\d{2})$", tok)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"^(\d{1,2})h$", tok)
    if m:
        return int(m.group(1)), 0
    return None


def parse_date(tok, now):
    m = re.match(r"^(\d{1,2})/(\d{1,2})(?:/(\d{4}))?$", tok)
    if not m:
        return None
    return (int(m.group(3)) if m.group(3) else now.year), int(m.group(2)), int(m.group(1))


def parse_schedule(toks, now):
    """Tra ve (meta_khong_text | None, tokens_con_lai). meta la 'once' hoac 'daily'."""
    low0 = toks[0].lower() if toks else ""
    if low0 in ("hangngay", "hằngngày") and len(toks) >= 2:
        hm = parse_hm(toks[1])
        if hm:
            return {"kind": "daily", "hh": hm[0], "mm": hm[1]}, toks[2:]
    if len(toks) >= 2:
        dt = parse_date(toks[0], now)
        if dt:
            hm = parse_hm(toks[1])
            if hm:
                return {"kind": "once", "y": dt[0], "mo": dt[1], "d": dt[2], "hh": hm[0], "mm": hm[1]}, toks[2:]
    if toks:
        hm = parse_hm(toks[0])
        if hm:
            due = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
            if due < now:
                due = due + timedelta(days=1)
            return {"kind": "once", "y": due.year, "mo": due.month, "d": due.day, "hh": hm[0], "mm": hm[1]}, toks[1:]
    return None, toks


def parse_dur(text):
    """'30p','1h','2h30','45 phut','30' -> so phut."""
    text = text.lower(); total = 0; found = False
    m = re.search(r"(\d+)\s*h(?:\s*(\d+))?", text)
    if m:
        total += int(m.group(1)) * 60 + (int(m.group(2)) if m.group(2) else 0); found = True
    m = re.search(r"(\d+)\s*(?:p|ph|phut|phút)\b", text)
    if m:
        total += int(m.group(1)); found = True
    if not found:
        m = re.search(r"(\d+)", text)
        if m:
            total = int(m.group(1)); found = True
    return total if (found and total > 0) else None


def when_text(m):
    if m["kind"] == "daily":
        return "mỗi ngày %02d:%02d" % (m["hh"], m["mm"])
    if m["kind"] == "once":
        return "%02d/%02d lúc %02d:%02d" % (m["d"], m["mo"], m["hh"], m["mm"])
    return "việc thường"


# ---------------- active / due / done ----------------
def is_active(t, now, st):
    m = t["meta"]; text = m["text"]
    snz = st["snooze"].get(text)
    if snz and datetime.fromisoformat(snz) > now:
        return False
    k = m["kind"]
    if k == "plain":
        return not t["done"]
    if k == "once":
        if t["done"]:
            return False
        return now >= datetime(m["y"], m["mo"], m["d"], m["hh"], m["mm"], tzinfo=VN)
    if k == "daily":
        if st["daily"].get(text) == now.date().isoformat():
            return False
        return now >= now.replace(hour=m["hh"], minute=m["mm"], second=0, microsecond=0)
    return False


def due_to_send(t, now, st):
    if not is_active(t, now, st):
        return False
    text = t["meta"]["text"]
    iv = int(st["interval"].get(text, 30))
    ls = st["last_sent"].get(text)
    if ls and (now - datetime.fromisoformat(ls)).total_seconds() < iv * 60 - 120:
        return False
    return True


def mark_done(t, now, st):
    text = t["meta"]["text"]
    if t["meta"]["kind"] == "daily":
        st["daily"][text] = now.date().isoformat()
    else:
        t["done"] = True
    st["snooze"].pop(text, None)
    return text


# ---------------- voice ----------------
_model = None
def transcribe(file_id):
    global _model
    info = tg("getFile", file_id=file_id)
    path = info.get("result", {}).get("file_path")
    if not path:
        return ""
    audio = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{path}", timeout=120).content
    open("voice.ogg", "wb").write(audio)
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "faster-whisper"], check=True)
        from faster_whisper import WhisperModel
    if _model is None:
        _model = WhisperModel("small", device="cpu", compute_type="int8")
    segs, _ = _model.transcribe("voice.ogg", language="vi")
    text = " ".join(s.text for s in segs).strip()
    print("Voice ->", text)
    return text


def find_number(text):
    m = re.search(r"\b(\d+)\b", text.lower())
    if m:
        return int(m.group(1))
    for w, n in NUM_WORDS.items():
        if re.search(r"\b" + re.escape(w) + r"\b", text.lower()):
            return n
    return None


VOICE_FILLER = {"nhắc", "nhac", "nhở", "nho", "hãy", "hay", "giúp", "giup", "mình", "minh",
                "tôi", "toi", "cho", "lúc", "luc", "vào", "vao", "rưỡi", "ruoi", "sáng", "sang",
                "chiều", "chieu", "tối", "toi", "trưa", "trua", "giờ", "gio", "và", "va", "ơi",
                "đi", "hằng", "hang", "hàng", "ngày", "ngay", "mỗi", "moi", "là", "la"}


def parse_voice_reminder(spoken, now):
    """Hieu voice kieu '12 gio an com' / 'hang ngay 7 gio tap the duc' -> meta lich."""
    s = spoken.lower().strip()
    daily = any(k in s for k in ("hằng ngày", "hang ngay", "hàng ngày", "mỗi ngày", "moi ngay", "mỗi sáng"))
    ruoi = ("rưỡi" in s) or ("ruoi" in s)
    m = re.search(r"(\d{1,2})\s*(?::|h|giờ|gio)\s*(\d{1,2})?", s)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2)) if m.group(2) else (30 if ruoi else 0)
    if re.search(r"chiều|chieu|tối|toi", s) and hh < 12:
        hh += 12
    if re.search(r"trưa|trua", s) and hh < 11:
        hh += 12
    hh %= 24; mm %= 60
    src = (s[:m.start()] + " " + s[m.end():])
    toks = [w for w in re.split(r"[\s,\.]+", src)
            if w and w not in VOICE_FILLER and not re.fullmatch(r"\d+", w)]
    text = " ".join(toks).strip()
    if not text:
        return None
    if daily:
        return {"kind": "daily", "hh": hh, "mm": mm, "text": text}
    due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if due < now:
        due += timedelta(days=1)
    return {"kind": "once", "y": due.year, "mo": due.month, "d": due.day, "hh": hh, "mm": mm, "text": text}


# ---------------- main ----------------
def main():
    lines, tasks = load_tasks()
    st = {"daily": load_json(DAILY_FILE, {}), "snooze": load_json(SNOOZE_FILE, {}),
          "interval": load_json(INTERVAL_FILE, {}), "last_sent": load_json(LASTSENT_FILE, {})}
    pending = load_json(PENDING_FILE, None)
    offset = 0
    try:
        offset = int(open(OFFSET_FILE).read().strip())
    except Exception:
        pass

    now = now_vn()
    upd = tg("getUpdates", offset=offset + 1, timeout=0)
    updates = upd.get("result", []) if upd.get("ok") else []

    replies, done_msgs = [], []
    new_offset = offset

    def actionable():
        return [t for t in tasks if not t["done"]]

    def add_task(meta):
        lines.append(render({"done": False, "meta": meta}))
        tasks.append({"lineno": len(lines) - 1, "done": False, "meta": meta})

    def mark_num(n):
        act = actionable()
        if 1 <= n <= len(act):
            return mark_done(act[n - 1], now, st)
        return None

    def process_text(raw):
        nonlocal pending
        raw = raw.strip()
        low = raw.lower()
        low = (low.replace("đổi giờ", "doigio").replace("doi gio", "doigio")
                  .replace("hằng ngày", "hangngay").replace("hang ngay", "hangngay")
                  .replace("mỗi ngày", "hangngay").replace("moi ngay", "hangngay"))

        # 0) dang cho tra loi gio cho 1 viec
        if pending:
            if low in ("luon", "luôn", "ngay", "ng", "khong", "không", "ko"):
                add_task({"kind": "plain", "text": pending["text"]})
                replies.append("➕ Đã thêm việc: " + pending["text"]); pending = None; return
            meta, rem = parse_schedule(raw.split(), now)
            if meta is not None and not rem:
                meta["text"] = pending["text"]; add_task(meta)
                replies.append("⏰ Đã đặt lịch: \"%s\" — %s" % (meta["text"], when_text(meta)))
                pending = None; return
            pending = None  # go lenh khac -> huy cho

        # 1) bao xong
        if any(h in low for h in DONE_HINTS) and not low.startswith(CMD_PREFIX):
            n = find_number(low)
            if n:
                txt = mark_num(n)
                replies.append(("✅ Xong: " + txt) if txt else ("Không có việc số %d." % n))
            return

        # 2) help / danh sach
        if low in ("?", "help", "huongdan", "hướng dẫn", "menu", "/start", "start"):
            replies.append(HELP); return
        if low in ("ds", "danhsach", "danh sach", "/list", "list"):
            act = actionable()
            if not act:
                replies.append("📋 Chưa có việc nào."); return
            out = ["📋 Danh sách việc (%d):" % len(act)]
            for i, t in enumerate(act, 1):
                m = t["meta"]
                tag = " (🔁 %02d:%02d)" % (m["hh"], m["mm"]) if m["kind"] == "daily" else \
                      (" (⏰ %02d/%02d %02d:%02d)" % (m["d"], m["mo"], m["hh"], m["mm"]) if m["kind"] == "once" else "")
                mark = "🔔" if is_active(t, now, st) else "⏳"
                iv = st["interval"].get(m["text"])
                ivs = " [mỗi %d']" % int(iv) if iv else ""
                out.append("%d. %s %s%s%s" % (i, mark, m["text"], tag, ivs))
            replies.append("\n".join(out)); return

        # 3) doi gio: doigio N <lich>
        if low.startswith("doigio"):
            toks = raw.split()[1:]
            act = actionable()
            n = int(toks[0]) if toks and toks[0].isdigit() else None
            if n and 1 <= n <= len(act) and len(toks) >= 2:
                meta, rem = parse_schedule(toks[1:], now)
                if meta:
                    tgt = act[n - 1]; meta["text"] = tgt["meta"]["text"]
                    tgt["meta"] = meta
                    st["snooze"].pop(meta["text"], None); st["last_sent"].pop(meta["text"], None)
                    replies.append("🕑 Đã đổi giờ \"%s\" — %s" % (meta["text"], when_text(meta)))
                else:
                    replies.append("Giờ chưa đúng. VD: doigio 2 21:00")
            else:
                replies.append("Cú pháp: doigio N 21:00 (N là số việc trong 'ds')")
            return

        # 4) hoan (snooze): hoan N 30p
        if low.startswith(("hoan", "hoãn", "snooze")):
            toks = raw.split()[1:]
            act = actionable()
            n = int(toks[0]) if toks and toks[0].isdigit() else None
            dur = parse_dur(" ".join(toks[1:])) if len(toks) >= 2 else None
            if n and 1 <= n <= len(act) and dur:
                text = act[n - 1]["meta"]["text"]
                until = now + timedelta(minutes=dur)
                st["snooze"][text] = until.isoformat()
                replies.append("😴 Hoãn \"%s\" — nhắc lại lúc %02d:%02d" % (text, until.hour, until.minute))
            else:
                replies.append("Cú pháp: hoan N 30p")
            return

        # 5) lap (khoang lap): lap N 60p
        if low.startswith(("lap", "lặp", "moi", "mỗi")):
            toks = raw.split()[1:]
            act = actionable()
            n = int(toks[0]) if toks and toks[0].isdigit() else None
            dur = parse_dur(" ".join(toks[1:])) if len(toks) >= 2 else None
            if n and 1 <= n <= len(act) and dur:
                text = act[n - 1]["meta"]["text"]
                st["interval"][text] = dur
                replies.append("🔁 \"%s\" sẽ nhắc lại mỗi %d phút." % (text, dur))
            else:
                replies.append("Cú pháp: lap N 60p")
            return

        # 6) xoa N
        if low.startswith(("xoa", "xóa")):
            n = find_number(low); act = actionable()
            if n and 1 <= n <= len(act):
                tgt = act[n - 1]; lines[tgt["lineno"]] = None; tasks.remove(tgt)
                replies.append("🗑️ Đã xoá: " + tgt["meta"]["text"])
            else:
                replies.append("Không tìm thấy việc số %s." % n)
            return

        # 7) them <viec>
        if low.startswith(("them ", "/them ")):
            text = raw.split(" ", 1)[1].strip()
            if text:
                add_task({"kind": "plain", "text": text})
                replies.append("➕ Đã thêm việc: " + text)
            return

        # 8) nhac ...
        if low.startswith(("nhac ", "nhắc ", "/nhac ")):
            rest = raw.split(" ", 1)[1].strip()
            meta, rem = parse_schedule(rest.split(), now)
            if meta is not None:
                meta["text"] = " ".join(rem).strip()
                if meta["text"]:
                    add_task(meta)
                    replies.append("⏰ Đã đặt lịch nhắc: \"%s\" — %s" % (meta["text"], when_text(meta)))
                else:
                    replies.append("Thiếu nội dung việc. VD: nhac 20:00 Gọi mẹ")
            elif rest:
                pending = {"text": rest}
                replies.append("⏰ Mấy giờ nhắc việc \"%s\"?\nTrả lời: giờ (20:00), hoặc '12/07 15:00', 'hangngay 09:00', hoặc 'luon' để nhắc ngay." % rest)
            else:
                replies.append(HELP)
            return

        replies.append("Mình chưa hiểu.\n" + HELP)

    # ---- xu ly updates ----
    for u in updates:
        new_offset = max(new_offset, u["update_id"])

        if "callback_query" in u:
            cq = u["callback_query"]; data = cq.get("data", "")
            if data.startswith("done:"):
                try:
                    lid = int(data.split(":")[1])
                except ValueError:
                    lid = -1
                found = None
                for t in tasks:
                    if t["lineno"] == lid and not t["done"] and is_active(t, now, st):
                        found = mark_done(t, now, st); break
                tg("answerCallbackQuery", callback_query_id=cq["id"],
                   text=("Đã xong ✅" if found else "Việc này xong rồi"))
                if found:
                    done_msgs.append(found)
            continue

        msg = u.get("message") or u.get("edited_message")
        if not msg:
            continue

        if "voice" in msg:
            spoken = transcribe(msg["voice"]["file_id"])
            low = spoken.lower()
            if any(h in low for h in DONE_HINTS):
                # bao xong
                n = find_number(low)
                if n:
                    txt = mark_num(n)
                    replies.append(("✅ Xong: " + txt) if txt else ("🎤 Nghe \"%s\" — không có việc số %d." % (spoken, n)))
                else:
                    replies.append("🎤 Nghe: \"%s\" — chưa rõ việc số mấy. Nói vd \"việc số 2 xong\"." % spoken)
            else:
                # tao lich bang voice
                meta = parse_voice_reminder(spoken, now)
                if meta:
                    add_task(meta)
                    replies.append("⏰ (voice) Đã đặt lịch: \"%s\" — %s\nNghe: \"%s\". Sai thì gõ: doigio N ... / xoa N" % (meta["text"], when_text(meta), spoken))
                else:
                    replies.append("🎤 Nghe: \"%s\" — chưa hiểu.\n• Tạo lịch: nói \"12 giờ ăn cơm\"\n• Báo xong: nói \"việc số 2 xong\"" % spoken)
            continue

        if "text" in msg:
            for cmd in msg["text"].split("\n"):
                if cmd.strip():
                    process_text(cmd)
            continue

    # ---- ghi trang thai ----
    for t in tasks:
        lines[t["lineno"]] = render(t)
    lines = [l for l in lines if l is not None]
    open(TASKS_FILE, "w", encoding="utf-8").write("\n".join(lines))
    save_json(DAILY_FILE, st["daily"]); save_json(SNOOZE_FILE, st["snooze"])
    save_json(INTERVAL_FILE, st["interval"])
    save_json(PENDING_FILE, pending)
    os.makedirs(os.path.dirname(OFFSET_FILE), exist_ok=True)
    open(OFFSET_FILE, "w").write(str(new_offset))

    # ---- gui xac nhan ----
    for r in replies:
        tg("sendMessage", chat_id=CHAT, text=r)
    if done_msgs:
        tg("sendMessage", chat_id=CHAT, text="✅ Đã đánh dấu XONG:\n" + "\n".join("• " + d for d in done_msgs))

    # ---- gui nhac cac viec DEN GIO (theo interval rieng) ----
    lines, tasks = load_tasks()
    now = now_vn()
    act = actionable()
    due = [(i + 1, t) for i, t in enumerate(act) if due_to_send(t, now, st)]
    if due:
        out = ["📋 Việc cần làm — nhấn ✅ / gửi voice / gõ \"xong N\" khi xong:"]
        kb = []
        for num, t in due:
            out.append("%d. %s" % (num, t["meta"]["text"]))
            kb.append([{"text": "✅ Xong %d" % num, "callback_data": "done:%d" % t["lineno"]}])
            st["last_sent"][t["meta"]["text"]] = now.isoformat()
        tg("sendMessage", chat_id=CHAT, text="\n".join(out), reply_markup={"inline_keyboard": kb})
    elif done_msgs:
        tg("sendMessage", chat_id=CHAT, text="🎉 Hết việc rồi, nghỉ thôi!")

    save_json(LASTSENT_FILE, st["last_sent"])


if __name__ == "__main__":
    main()
