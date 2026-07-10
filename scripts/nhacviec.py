#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nhac viec tuong tac qua Telegram.
- Doc cac reply cua user (nut ✅ / text 'xong N' / voice) -> danh dau [x].
- Gui lai danh sach viec con [ ], kem nut '✅ Xong' cho tung viec.
Chay dinh ky (moi 30') boi GitHub Actions.
"""
import os, re, sys, json, subprocess
import requests

TOKEN = os.environ["TG_TOKEN"]
CHAT  = os.environ["TG_CHAT"]
API   = f"https://api.telegram.org/bot{TOKEN}"

TASKS_FILE  = "viec-can-lam.txt"
OFFSET_FILE = "state/offset.txt"

NUM_WORDS = {
    "mot":1,"một":1,"hai":2,"ba":3,"bon":4,"bốn":4,"tu":4,"tư":4,"nam":5,"năm":5,
    "sau":6,"sáu":6,"bay":7,"bảy":7,"tam":8,"tám":8,"chin":9,"chín":9,"muoi":10,"mười":10,
}
DONE_HINTS = ("xong","hoan thanh","hoàn thành","da lam","đã làm","lam xong","làm xong","done","xhong")


def tg(method, **params):
    try:
        r = requests.post(f"{API}/{method}", json=params, timeout=60)
        return r.json()
    except Exception as e:
        print("TG error", method, e)
        return {}


# ---------- doc / ghi file viec ----------
def load_tasks():
    if not os.path.exists(TASKS_FILE):
        return [], []
    with open(TASKS_FILE, encoding="utf-8") as f:
        lines = f.read().split("\n")
    tasks = []  # [lineno, done(bool), text]
    for i, l in enumerate(lines):
        m = re.match(r"^\s*\[( |x|X)\]\s*(.*)$", l)
        if m:
            tasks.append([i, m.group(1).lower() == "x", m.group(2).strip()])
    return lines, tasks


def save_tasks(lines, tasks):
    for t in tasks:
        lines[t[0]] = ("[x] " if t[1] else "[ ] ") + t[2]
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def remaining(tasks):
    return [t for t in tasks if not t[1]]


def mark_by_display(tasks, n):
    rem = remaining(tasks)
    if 1 <= n <= len(rem):
        rem[n - 1][1] = True
        return rem[n - 1][2]
    return None


def mark_by_lineid(tasks, lineid):
    for t in tasks:
        if t[0] == lineid and not t[1]:
            t[1] = True
            return t[2]
    return None


# ---------- offset ----------
def load_offset():
    try:
        return int(open(OFFSET_FILE).read().strip())
    except Exception:
        return 0


def save_offset(v):
    os.makedirs(os.path.dirname(OFFSET_FILE), exist_ok=True)
    with open(OFFSET_FILE, "w") as f:
        f.write(str(v))


# ---------- voice ----------
_model = None
def transcribe(file_id):
    global _model
    info = tg("getFile", file_id=file_id)
    path = info.get("result", {}).get("file_path")
    if not path:
        return ""
    url = f"https://api.telegram.org/file/bot{TOKEN}/{path}"
    audio = requests.get(url, timeout=120).content
    with open("voice.ogg", "wb") as f:
        f.write(audio)
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
    t = text.lower()
    m = re.search(r"\b(\d+)\b", t)
    if m:
        return int(m.group(1))
    for w, n in NUM_WORDS.items():
        if re.search(r"\b" + re.escape(w) + r"\b", t):
            return n
    return None


# ---------- main ----------
def main():
    lines, tasks = load_tasks()
    offset = load_offset()
    upd = tg("getUpdates", offset=offset + 1, timeout=0)
    updates = upd.get("result", []) if upd.get("ok") else []

    done_msgs = []
    new_offset = offset

    for u in updates:
        new_offset = max(new_offset, u["update_id"])

        # 1) Nut bam
        if "callback_query" in u:
            cq = u["callback_query"]
            data = cq.get("data", "")
            if data.startswith("done:"):
                try:
                    lineid = int(data.split(":")[1])
                except ValueError:
                    lineid = -1
                txt = mark_by_lineid(tasks, lineid)
                tg("answerCallbackQuery", callback_query_id=cq["id"],
                   text=("Đã đánh dấu xong ✅" if txt else "Việc này đã xong rồi"))
                if txt:
                    done_msgs.append(txt)
            continue

        msg = u.get("message") or u.get("edited_message")
        if not msg:
            continue

        # 2) Text: 'xong N'
        if "text" in msg:
            t = msg["text"].lower()
            if any(h in t for h in DONE_HINTS):
                n = find_number(t)
                if n:
                    txt = mark_by_display(tasks, n)
                    if txt:
                        done_msgs.append(txt)
            continue

        # 3) Voice
        if "voice" in msg:
            spoken = transcribe(msg["voice"]["file_id"])
            n = find_number(spoken)
            if n and (any(h in spoken.lower() for h in DONE_HINTS) or True):
                txt = mark_by_display(tasks, n)
                if txt:
                    done_msgs.append(txt)
            else:
                tg("sendMessage", chat_id=CHAT,
                   text="🎤 Mình nghe: \"%s\"\nChưa rõ việc số mấy. Bạn nói kèm số, ví dụ \"việc số 2 xong rồi\"." % spoken)
            continue

    # Ghi trang thai
    save_tasks(lines, tasks)
    save_offset(new_offset)

    # Xac nhan cac viec vua xong
    if done_msgs:
        body = "✅ Đã đánh dấu XONG:\n" + "\n".join("• " + d for d in done_msgs)
        tg("sendMessage", chat_id=CHAT, text=body)

    # Gui nhac cac viec con lai
    rem = remaining(tasks)
    if rem:
        lines_msg = ["📋 Việc chưa xong (%d) — nhấn ✅ hoặc gửi voice khi làm xong:" % len(rem)]
        keyboard = []
        for i, t in enumerate(rem, 1):
            lines_msg.append("%d. %s" % (i, t[2]))
            keyboard.append([{"text": "✅ Xong %d" % i, "callback_data": "done:%d" % t[0]}])
        tg("sendMessage", chat_id=CHAT, text="\n".join(lines_msg),
           reply_markup={"inline_keyboard": keyboard})
    else:
        if done_msgs:
            tg("sendMessage", chat_id=CHAT, text="🎉 Hết việc rồi, nghỉ thôi!")


if __name__ == "__main__":
    main()
