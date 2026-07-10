#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot nhac viec tuong tac qua Telegram (chay moi 30' boi GitHub Actions).

Tao lich ngay tren Telegram bang tin nhan:
  them <viec>                -> viec thuong (nhac moi chu ky)
  nhac 15:00 <viec>          -> nhac hom nay luc 15:00 (neu qua gio -> ngay mai)
  nhac 12/07 15:00 <viec>    -> nhac dung ngay/gio
  nhac hangngay 09:00 <viec> -> nhac moi ngay luc 09:00
  ds                         -> xem danh sach
  xoa N                      -> xoa viec so N
Bao xong: nut ✅ / voice "viec so N xong" / go "xong N".
"""
import os, re, sys, json, subprocess
from datetime import datetime, timezone, timedelta
import requests

TOKEN = os.environ["TG_TOKEN"]
CHAT  = os.environ["TG_CHAT"]
API   = f"https://api.telegram.org/bot{TOKEN}"

TASKS_FILE  = "viec-can-lam.txt"
OFFSET_FILE = "state/offset.txt"
DAILY_FILE  = "state/daily_done.json"

VN = timezone(timedelta(hours=7))
def now_vn():
    return datetime.now(VN)

NUM_WORDS = {"mot":1,"một":1,"hai":2,"ba":3,"bon":4,"bốn":4,"tu":4,"tư":4,"nam":5,"năm":5,
             "sau":6,"sáu":6,"bay":7,"bảy":7,"tam":8,"tám":8,"chin":9,"chín":9,"muoi":10,"mười":10}
DONE_HINTS = ("xong","hoan thanh","hoàn thành","da lam","đã làm","lam xong","làm xong","done")

HELP = ("🤖 Cách dùng bot nhắc việc:\n"
        "• Thêm việc thường: them <việc>\n"
        "• Nhắc theo giờ hôm nay: nhac 15:00 <việc>\n"
        "• Nhắc ngày & giờ: nhac 12/07 15:00 <việc>\n"
        "• Nhắc mỗi ngày: nhac hangngay 09:00 <việc>\n"
        "• Xem danh sách: ds\n"
        "• Xoá việc số N: xoa N\n"
        "• Báo xong: bấm nút ✅, gửi voice \"việc số N xong\", hoặc gõ: xong N")


def tg(method, **params):
    try:
        return requests.post(f"{API}/{method}", json=params, timeout=60).json()
    except Exception as e:
        print("TG error", method, e); return {}


# ---------------- parse / render tasks ----------------
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


def save_tasks(lines, tasks):
    for t in tasks:
        lines[t["lineno"]] = render(t)
    open(TASKS_FILE, "w", encoding="utf-8").write("\n".join(lines))


def load_json(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(obj, open(path, "w", encoding="utf-8"), ensure_ascii=False)


# ---------------- active logic ----------------
def is_active(t, now, daily):
    m = t["meta"]; k = m["kind"]
    if k == "plain":
        return not t["done"]
    if k == "once":
        if t["done"]:
            return False
        due = datetime(m["y"], m["mo"], m["d"], m["hh"], m["mm"], tzinfo=VN)
        return now >= due
    if k == "daily":
        if daily.get(m["text"]) == now.date().isoformat():
            return False
        due = now.replace(hour=m["hh"], minute=m["mm"], second=0, microsecond=0)
        return now >= due
    return False


def mark_done(t, daily, now):
    if t["meta"]["kind"] == "daily":
        daily[t["meta"]["text"]] = now.date().isoformat()
    else:
        t["done"] = True
    return t["meta"]["text"]


# ---------------- parse time / date from user text ----------------
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
    d, mo = int(m.group(1)), int(m.group(2))
    y = int(m.group(3)) if m.group(3) else now.year
    return y, mo, d


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


# ---------------- main ----------------
def main():
    lines, tasks = load_tasks()
    daily = load_json(DAILY_FILE, {})
    offset = 0
    try:
        offset = int(open(OFFSET_FILE).read().strip())
    except Exception:
        pass

    now = now_vn()
    upd = tg("getUpdates", offset=offset + 1, timeout=0)
    updates = upd.get("result", []) if upd.get("ok") else []

    replies = []          # tin xac nhan gui lai
    done_msgs = []
    new_offset = offset

    def active_list():
        return [t for t in tasks if is_active(t, now, daily)]

    def mark_display(n):
        act = active_list()
        if 1 <= n <= len(act):
            return mark_done(act[n - 1], daily, now)
        return None

    for u in updates:
        new_offset = max(new_offset, u["update_id"])

        # ----- nut bam -----
        if "callback_query" in u:
            cq = u["callback_query"]; data = cq.get("data", "")
            if data.startswith("done:"):
                try:
                    lid = int(data.split(":")[1])
                except ValueError:
                    lid = -1
                found = None
                for t in tasks:
                    if t["lineno"] == lid and is_active(t, now, daily):
                        found = mark_done(t, daily, now); break
                tg("answerCallbackQuery", callback_query_id=cq["id"],
                   text=("Đã đánh dấu xong ✅" if found else "Việc này xong rồi"))
                if found:
                    done_msgs.append(found)
            continue

        msg = u.get("message") or u.get("edited_message")
        if not msg:
            continue

        # ----- voice -----
        if "voice" in msg:
            spoken = transcribe(msg["voice"]["file_id"])
            n = find_number(spoken)
            if n:
                txt = mark_display(n)
                if txt:
                    done_msgs.append(txt)
                else:
                    replies.append("🎤 Nghe: \"%s\" — không có việc số %d đang chờ." % (spoken, n))
            else:
                replies.append("🎤 Nghe: \"%s\" — chưa rõ việc số mấy. Nói kèm số, ví dụ \"việc số 2 xong rồi\"." % spoken)
            continue

        # ----- text lenh -----
        if "text" in msg:
            raw = msg["text"].strip()
            low = raw.lower()
            low = low.replace("hằng ngày", "hangngay").replace("hang ngay", "hangngay").replace("mỗi ngày", "hangngay").replace("moi ngay", "hangngay")

            # bao xong
            if any(h in low for h in DONE_HINTS) and not low.startswith(("them", "nhac", "nhắc", "xoa", "xóa")):
                n = find_number(low)
                if n:
                    txt = mark_display(n)
                    if txt:
                        done_msgs.append(txt)
                    else:
                        replies.append("Không có việc số %d đang chờ." % n)
                continue

            # help / list
            if low in ("?", "help", "huongdan", "hướng dẫn", "menu", "/start", "start"):
                replies.append(HELP); continue
            if low in ("ds", "danhsach", "danh sach", "/list", "list"):
                act = active_list()
                out = ["📋 Việc đang chờ (%d):" % len(act)]
                for i, t in enumerate(act, 1):
                    out.append("%d. %s" % (i, t["meta"]["text"]))
                upcoming = [t for t in tasks if not is_active(t, now, daily) and not t["done"] and t["meta"]["kind"] != "plain"]
                if upcoming:
                    out.append("\n⏳ Sắp tới:")
                    for t in upcoming:
                        m = t["meta"]
                        when = ("mỗi ngày %02d:%02d" % (m["hh"], m["mm"])) if m["kind"] == "daily" else ("%02d/%02d %02d:%02d" % (m["d"], m["mo"], m["hh"], m["mm"]))
                        out.append("• %s (%s)" % (m["text"], when))
                replies.append("\n".join(out)); continue

            # xoa N
            if low.startswith(("xoa ", "xóa ")):
                n = find_number(low)
                act = active_list()
                if n and 1 <= n <= len(act):
                    tgt = act[n - 1]
                    lines[tgt["lineno"]] = None  # danh dau xoa
                    tasks.remove(tgt)
                    replies.append("🗑️ Đã xoá: " + tgt["meta"]["text"])
                else:
                    replies.append("Không tìm thấy việc số %s để xoá." % n)
                continue

            # them <viec>
            if low.startswith(("them ", "/them ")):
                text = raw.split(" ", 1)[1].strip()
                if text:
                    lines.append(render({"done": False, "meta": {"kind": "plain", "text": text}}))
                    tasks.append({"lineno": len(lines) - 1, "done": False, "meta": {"kind": "plain", "text": text}})
                    replies.append("➕ Đã thêm việc: " + text)
                continue

            # nhac ...
            if low.startswith(("nhac ", "nhắc ", "/nhac ")):
                rest = raw.split(" ", 1)[1].strip()
                toks = rest.split()
                low_toks = low.split()[1:]
                meta = None; consume = 0
                if low_toks and low_toks[0] == "hangngay" and len(toks) >= 2:
                    hm = parse_hm(toks[1])
                    if hm:
                        meta = {"kind": "daily", "hh": hm[0], "mm": hm[1], "text": " ".join(toks[2:]).strip()}
                if meta is None and len(toks) >= 2:
                    dt = parse_date(toks[0], now)
                    if dt:
                        hm = parse_hm(toks[1])
                        if hm:
                            meta = {"kind": "once", "y": dt[0], "mo": dt[1], "d": dt[2], "hh": hm[0], "mm": hm[1], "text": " ".join(toks[2:]).strip()}
                if meta is None and len(toks) >= 1:
                    hm = parse_hm(toks[0])
                    if hm:
                        due = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
                        if due < now:
                            due = due + timedelta(days=1)
                        meta = {"kind": "once", "y": due.year, "mo": due.month, "d": due.day, "hh": hm[0], "mm": hm[1], "text": " ".join(toks[1:]).strip()}
                if meta and meta["text"]:
                    lines.append(render({"done": False, "meta": meta}))
                    tasks.append({"lineno": len(lines) - 1, "done": False, "meta": meta})
                    when = ("mỗi ngày %02d:%02d" % (meta["hh"], meta["mm"])) if meta["kind"] == "daily" else ("%02d/%02d lúc %02d:%02d" % (meta["d"], meta["mo"], meta["hh"], meta["mm"]))
                    replies.append("⏰ Đã đặt lịch nhắc: \"%s\" — %s" % (meta["text"], when))
                else:
                    replies.append("Cú pháp chưa đúng.\n" + HELP)
                continue

            # khong hieu
            replies.append("Mình chưa hiểu lệnh này.\n" + HELP)
            continue

    # ---- ghi trang thai ----
    for t in tasks:                                       # ap thay doi (done/meta) vao dong
        lines[t["lineno"]] = render(t)
    lines = [l for l in lines if l is not None]           # bo cac dong da xoa
    open(TASKS_FILE, "w", encoding="utf-8").write("\n".join(lines))
    save_json(DAILY_FILE, daily)
    os.makedirs(os.path.dirname(OFFSET_FILE), exist_ok=True)
    open(OFFSET_FILE, "w").write(str(new_offset))

    # ---- gui phan hoi ----
    for r in replies:
        tg("sendMessage", chat_id=CHAT, text=r)
    if done_msgs:
        tg("sendMessage", chat_id=CHAT, text="✅ Đã đánh dấu XONG:\n" + "\n".join("• " + d for d in done_msgs))

    # ---- reload sau khi da ghi file (lineno chuan) roi gui nhac ----
    lines, tasks = load_tasks()
    now = now_vn()
    act = [t for t in tasks if is_active(t, now, daily)]
    if act:
        out = ["📋 Việc chưa xong (%d) — nhấn ✅ hoặc gửi voice khi làm xong:" % len(act)]
        kb = []
        for i, t in enumerate(act, 1):
            out.append("%d. %s" % (i, t["meta"]["text"]))
            kb.append([{"text": "✅ Xong %d" % i, "callback_data": "done:%d" % t["lineno"]}])
        tg("sendMessage", chat_id=CHAT, text="\n".join(out), reply_markup={"inline_keyboard": kb})
    elif done_msgs:
        tg("sendMessage", chat_id=CHAT, text="🎉 Hết việc rồi, nghỉ thôi!")


def load_tasks_from_lines(lines):
    tasks = []
    for i, l in enumerate(lines):
        mm = re.match(r"^\s*\[( |x|X)\]\s*(.*)$", l)
        if mm:
            tasks.append({"lineno": i, "done": mm.group(1).lower() == "x", "meta": parse_meta(mm.group(2))})
    return lines, tasks


if __name__ == "__main__":
    main()
