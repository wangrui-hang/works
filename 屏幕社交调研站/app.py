# -*- coding: utf-8 -*-
"""
《在线，但不敢说话》· 大学生屏幕社交调研站（纯调研版）

特点
  1. 完全离线：不引用任何 CDN、不从网络获取任何数据。图表用本地 SVG 手绘，
     页面里出现的每一个数字都来自本机 SQLite 里回收到的问卷。
  2. 十个板块各自独立收数据，每个板块都能单独导出 CSV / JSON。
  3. 零依赖：只用 Python 标准库（http.server + sqlite3）。

启动
    python3 app.py
    问卷   http://127.0.0.1:5000/
    看板   http://127.0.0.1:5000/admin

接口
    GET  /api/sections                       板块与题目定义（前端据此渲染）
    POST /api/answer                         保存某板块答案（可反复保存，按 会话+板块+题目 覆盖）
    POST /api/finish                         标记这份问卷已完成
    GET  /api/stats?section=<key|all>        某板块 / 全部板块的选项分布
    GET  /api/export?section=<key|all>&fmt=csv|json   导出（每个板块页面都挂了按钮）
    GET  /api/mine?sid=xxx                   取回本浏览器已填过的答案（刷新不丢）
    GET  /api/sessions                       会话进度明细（看板用）
"""

import csv
import io
import json
import os
import re
import sqlite3
import threading
from collections import Counter, OrderedDict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HOST = "0.0.0.0"
PORT = 5000
BASE = os.path.dirname(os.path.abspath(__file__))
# 数据库默认与 app.py 同目录；放在网盘/挂载盘报 disk I/O error 时用 SSS_DB 指到本地磁盘
DB = os.environ.get("SSS_DB") or os.path.join(BASE, "survey.db")
LOCK = threading.Lock()

# ============================================================
# 一、调研内容定义（十个板块，改这里就够，前端自动渲染）
#    item.type: single 单选 / multi 多选 / text 填空 / scale 1-5 打分
# ============================================================
SECTIONS = [
    {
        "key": "basic", "no": 0, "kicker": "基本信息 · 可选",
        "title": "先填三个背景项",
        "desc": "只用于分组统计，不收集任何身份信息，可以直接跳过。",
        "items": [
            {"id": "grade", "type": "single", "q": "你现在的年级",
             "options": ["大一", "大二", "大三", "大四", "研究生及以上"]},
            {"id": "gender", "type": "single", "q": "性别",
             "options": ["男", "女", "不愿透露"]},
            {"id": "screen_time", "type": "single", "q": "日均屏幕使用时间",
             "options": ["3 小时以内", "3–6 小时", "6–9 小时", "9 小时以上"]},
        ],
    },
    {
        "key": "meal", "no": 1, "kicker": "板块一 · 食堂",
        "title": "你有多久没有专心吃完一顿饭了？",
        "desc": "这不是一个「手机毁掉一代人」的故事。我们想谈的是另一件事：当屏幕随时可以提供刺激，"
                "沉默就变成了需要被填补的漏洞，而面对面的交流，变成了一件需要鼓起勇气的事。",
        "items": [
            {"id": "meal", "type": "single", "q": "吃饭时，你通常是哪种状态？",
             "options": ["经常一个人吃饭看视频", "和朋友吃饭也各自看手机", "我一般不看手机"]},
            {"id": "meal_feel", "type": "single", "q": "如果吃饭时不能看手机，你会",
             "options": ["很不自在", "有点不习惯", "没什么感觉"]},
        ],
    },
    {
        "key": "table", "no": 2, "kicker": "板块二 · 沉默的餐桌",
        "title": "沉默的餐桌",
        "desc": "一个人吃饭时，看视频能减少尴尬；和朋友吃饭时，看手机能避免没话找话。"
                "手机于是成了一个「社交缓冲器」——它并不解决沉默，只是让沉默变得可以被忍受。",
        "items": [
            {"id": "scene", "type": "multi", "q": "下面哪些场合，你会低头看手机？（可多选）",
             "options": ["独自吃饭", "和熟人吃饭", "和不熟的人吃饭"]},
            {"id": "scene_why", "type": "single", "q": "最主要的原因是",
             "options": ["不看点什么会尴尬", "不知道聊什么", "习惯了，没特别原因"]},
        ],
    },
    {
        "key": "online", "no": 3, "kicker": "板块三 · 线上与线下",
        "title": "线上活跃，线下失语",
        "desc": "社交障碍往往不是「不会说话」，而是「在屏幕里安全，在现实里紧张」。"
                "两边的差别不在能力，在于可控。",
        "items": [
            {"id": "on_state", "type": "multi", "q": "线上状态：哪些符合你？（可多选）",
             "options": ["表情包很多，情绪可以外包", "群聊很活跃，随时能接上话",
                         "朋友圈点赞很快，回应很及时", "消息可以编辑、可以撤回",
                         "回复可以延迟，没人催你"]},
            {"id": "off_state", "type": "multi", "q": "线下状态：哪些符合你？（可多选）",
             "options": ["见面不知道说什么，脑子一片空白", "害怕冷场，三秒沉默就窒息",
                         "不敢主动打招呼，假装没看见", "必须即时反应，说出去就收不回",
                         "要管理表情、眼神、语气和身体"]},
        ],
    },
    {
        "key": "compare", "no": 4, "kicker": "板块四 · 比较带来的焦虑",
        "title": "比较带来的焦虑",
        "desc": "看到别人拿奖、实习、旅行、恋爱，会觉得自己「落后」。刷得越多，越容易低估自己的生活。"
                "焦虑不一定来自真实差距，而来自持续暴露在他人的高光片段里。",
        "items": [
            {"id": "hours", "type": "single", "q": "今天你刷了多久社交平台？",
             "options": ["基本没刷", "1–2 小时", "3–5 小时", "6 小时以上"]},
            {"id": "after_feed", "type": "single", "q": "刷完之后，你的感受更接近",
             "options": ["更焦虑，觉得自己落后", "没什么变化", "放松了一些"]},
            {"id": "compare_level", "type": "scale", "q": "你会拿别人的动态和自己的生活作比较",
             "options": ["1 完全不会", "2", "3", "4", "5 一直在比"]},
        ],
    },
    {
        "key": "feedback", "no": 5, "kicker": "板块五 · 点赞与自我价值",
        "title": "点赞，成了情绪开关",
        "desc": "发完朋友圈反复刷新看有没有人点赞；消息没被及时回复，就开始怀疑「是不是我说错话了」。"
                "当自我价值被绑在数字反馈上，情绪的控制权就交出去了。",
        "items": [
            {"id": "delete_post", "type": "single", "q": "如果发出去的内容没人点赞，你会删掉吗？",
             "options": ["会删", "看情况", "不会"]},
            {"id": "slow_reply", "type": "single", "q": "对方很久没回消息，你第一反应是",
             "options": ["我是不是说错话了", "他可能在忙", "不太在意"]},
            {"id": "mood_swing", "type": "single", "q": "你最近一次因为一条消息情绪波动，是什么时候？",
             "options": ["今天", "这一周内", "更久以前", "想不起来"]},
            {"id": "refresh", "type": "single", "q": "发完动态后，你会反复刷新看反馈吗？",
             "options": ["经常", "偶尔", "从不"]},
        ],
    },
    {
        "key": "dorm", "no": 6, "kicker": "板块六 · 宿舍",
        "title": "宿舍里的沉默",
        "desc": "物理距离三米，心理距离三个屏幕。四个人各自戴着耳机，想聊天但怕打扰别人，"
                "明明很近，却习惯用消息交流——发一条微信给睡在对面床上的人，是很多宿舍的日常。",
        "items": [
            {"id": "dorm_silent", "type": "multi", "q": "下面哪些是你没说出口的真实想法？（可多选）",
             "options": ["其实想聊，但不知道从哪开始", "怕自己说得太无聊",
                         "他们好像都在忙", "还是刷会儿手机吧"]},
            {"id": "dorm_msg", "type": "single", "q": "你会给同宿舍的人发消息，而不是直接开口说吗？",
             "options": ["经常", "偶尔", "从不"]},
        ],
    },
    {
        "key": "escape", "no": 7, "kicker": "板块七 · 逃避式刷手机",
        "title": "刷手机，常常是在逃避",
        "desc": "作业不想写，先刷一会儿；社交有压力，先躲进手机里。这不是单纯的「上瘾」，"
                "而是一种情绪调节方式——只是它调节的速度太快，代价也来得太快。",
        "items": [
            {"id": "escape_freq", "type": "single",
             "q": "「感到压力 → 打开手机 → 短暂放松 → 时间消失 → 更焦虑」这个循环，你",
             "options": ["几乎每天", "每周几次", "偶尔", "几乎没有"]},
            {"id": "break_way", "type": "multi", "q": "你用过哪些办法打断它？（可多选）",
             "options": ["把手机放到伸手够不到的地方", "先做 5 分钟最小的一件小事",
                         "找一个真实的人说一句话", "允许自己无聊一会儿", "没试过"]},
        ],
    },
    {
        "key": "quiz", "no": 8, "kicker": "板块八 · 社交信心测试（5 题）",
        "title": "社交能力退化了吗？",
        "desc": "不是能力消失，而是练习变少。眼神接触变少、电话恐惧增加、害怕临时对话、"
                "对冷场更敏感、更依赖文字和表情包表达情绪。",
        "items": [
            {"id": "greet", "type": "single", "q": "见到半熟的人，你会主动打招呼吗？",
             "options": ["会", "看情况", "不会"]},
            {"id": "call", "type": "single", "q": "需要打电话办事时，你会紧张吗？",
             "options": ["很紧张", "有点", "不会"]},
            {"id": "at", "type": "single", "q": "群里有人@你，你会立刻想好怎么回吗？",
             "options": ["会", "要缓一下", "先装作没看到"]},
            {"id": "silence", "type": "single", "q": "吃饭时突然冷场，你会焦虑吗？",
             "options": ["很焦虑", "有点", "不焦虑"]},
            {"id": "prefer", "type": "single", "q": "你更愿意线上聊天还是线下见面？",
             "options": ["线上", "都行", "线下"]},
        ],
    },
    {
        "key": "overview", "no": 9, "kicker": "板块九 · 数据总览",
        "title": "我们把回收到的问卷画了出来",
        "desc": "下面所有图表都由本机数据库实时聚合生成，没有任何外部数据源；"
                "样本量为 0 时显示空状态，不会用演示数据冒充。",
        "items": [],
    },
    {
        "key": "challenge", "no": 10, "kicker": "板块十 · 行动意愿",
        "title": "今天的小挑战，你愿意试哪一个？",
        "desc": "手机给了我们连接世界的能力，也可能让我们失去连接身边人的勇气。"
                "真正的社交，不是永远有话可说，而是愿意在沉默里，仍然靠近彼此。",
        "items": [
            {"id": "challenge", "type": "single", "q": "选一个你今天愿意做的",
             "options": ["一顿饭不看视频", "主动和一个同学聊 5 分钟",
                         "把手机放在书包里走完一段路", "给很久没联系的人发一句真实问候"]},
            {"id": "willing", "type": "scale", "q": "你觉得自己能坚持三天以上的可能性",
             "options": ["1 几乎不可能", "2", "3", "4", "5 很有可能"]},
        ],
    },
]

# 第八屏测试的计分（分越高越偏回避）与结果分档
SCORE_MAP = {"greet": [0, 1, 2], "call": [2, 1, 0], "at": [0, 1, 2],
             "silence": [2, 1, 0], "prefer": [2, 1, 0]}
RESULT_BANDS = [
    (0, 4, "社交从容型", "你在现实里的练习量足够，屏幕只是工具，不是避难所。"),
    (5, 8, "正在恢复型", "你已经在往回走，继续保持面对面的频率就好。"),
    (9, 12, "选择性社恐型", "你不是不会社交，只是挑场合——熟人面前很松弛，生人面前想消失。"),
    (13, 16, "线上活跃型", "屏幕里的你比现实里的你更完整，该给线下一点练习机会了。"),
    (17, 99, "回避型社交者", "回避让你短期舒服，长期会更难开口。从一句真实的问候开始。"),
]
DIMS = ["焦虑", "孤独", "比较", "拖延", "回避"]
HOURS_STEP = {"基本没刷": 0, "1–2 小时": 1, "3–5 小时": 2, "6 小时以上": 3}

SEC_BY_KEY = OrderedDict((s["key"], s) for s in SECTIONS)
DATA_SECTIONS = [s for s in SECTIONS if s["items"]]      # overview 不直接收数据


def items_of(keys=None):
    secs = DATA_SECTIONS if keys is None else [SEC_BY_KEY[k] for k in keys if k in SEC_BY_KEY]
    return [(s["key"], it) for s in secs for it in s["items"]]


def section_of_item(item_id):
    for key, it in items_of():
        if it["id"] == item_id:
            return key
    return None


def type_of(score):
    for lo, hi, name, desc in RESULT_BANDS:
        if lo <= score <= hi:
            return name, desc
    return RESULT_BANDS[-1][2], RESULT_BANDS[-1][3]


# ============================================================
# 二、数据库
# ============================================================
def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with LOCK, db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS sessions(
            sid TEXT PRIMARY KEY, created TEXT, updated TEXT,
            finished INTEGER DEFAULT 0, ua TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS answers(
            sid TEXT, section TEXT, item TEXT, value TEXT, ts TEXT,
            PRIMARY KEY(sid, section, item))""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ans_item ON answers(item)")


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def valid_sid(sid):
    return bool(sid) and bool(re.fullmatch(r"[\w\-]{6,64}", sid))


def load_records():
    """长表 → 每份问卷一条宽记录 {sid, _created, _finished, item_id: value, ...}"""
    with db() as c:
        ss = c.execute("SELECT * FROM sessions ORDER BY created").fetchall()
        aa = c.execute("SELECT * FROM answers").fetchall()
    recs = OrderedDict()
    for s in ss:
        recs[s["sid"]] = {"_sid": s["sid"], "_created": s["created"],
                          "_updated": s["updated"], "_finished": bool(s["finished"])}
    for a in aa:
        r = recs.setdefault(a["sid"], {"_sid": a["sid"], "_created": a["ts"],
                                       "_updated": a["ts"], "_finished": False})
        r[a["item"]] = a["value"]
    return list(recs.values())


def save_answers(sid, section, answers, ua=""):
    ts = now()
    with LOCK, db() as c:
        c.execute("""INSERT INTO sessions(sid,created,updated,ua) VALUES(?,?,?,?)
                     ON CONFLICT(sid) DO UPDATE SET updated=excluded.updated,
                       ua=CASE WHEN sessions.ua IS NULL OR sessions.ua=''
                               THEN excluded.ua ELSE sessions.ua END""",
                  (sid, ts, ts, (ua or "")[:300]))
        n = 0
        for item_id, value in answers.items():
            sec = section or section_of_item(item_id)
            if not sec:
                continue                                   # 不在问卷定义里的字段一律丢弃
            if isinstance(value, (list, tuple)):
                value = "|".join(str(v) for v in value)
            value = str(value)[:200]
            if not value:
                continue
            c.execute("""INSERT INTO answers(sid,section,item,value,ts) VALUES(?,?,?,?,?)
                         ON CONFLICT(sid,section,item) DO UPDATE SET
                           value=excluded.value, ts=excluded.ts""",
                      (sid, sec, item_id, value, ts))
            n += 1
    return n


def mark_finished(sid):
    with LOCK, db() as c:
        c.execute("UPDATE sessions SET finished=1, updated=? WHERE sid=?", (now(), sid))


# ============================================================
# 三、聚合与派生指标（全部基于本机回收的数据）
# ============================================================
def quiz_score(rec):
    total, hit = 0, False
    for qid, band in SCORE_MAP.items():
        opts = next(it["options"] for k, it in items_of(["quiz"]) if it["id"] == qid)
        v = rec.get(qid)
        if v in opts:
            total += band[opts.index(v)]
            hit = True
    if not hit:
        return None, None
    name, desc = type_of(total)
    return total, name


def scale5(rec, item_id, options):
    """1–5 打分题取值；未作答返回 None"""
    v = rec.get(item_id)
    if v in options:
        return options.index(v) + 1
    try:
        f = float(v)
        return f if 1 <= f <= 5 else None
    except (TypeError, ValueError):
        return None


def clamp5(v):
    return round(max(1.0, min(5.0, v)), 2)


def raw(rec, qid):
    opts = next(it["options"] for k, it in items_of(["quiz"]) if it["id"] == qid)
    v = rec.get(qid)
    return SCORE_MAP[qid][opts.index(v)] if v in opts else None


def dim_scores(rec):
    """五维 1–5，由真实作答推导；某维全部缺项则不计入均值"""
    def pick(vals):
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    meal = rec.get("meal") or ""
    alone = 2.0 if "一个人吃饭" in meal else (1.0 if "各自看手机" in meal else
                                             (0.0 if meal else None))
    h = HOURS_STEP.get(rec.get("hours"))            # 0–3，未作答为 None
    cmp_opts = SEC_BY_KEY["compare"]["items"][2]["options"]
    lv = scale5(rec, "compare_level", cmp_opts)
    cmp_lv = (lv - 1) / 2 if lv is not None else None   # 1–5 → 0–2，与其它项同量纲
    brk = rec.get("break_way") or ""
    out = {}
    pairs = {
        "焦虑": [raw(rec, "silence"), raw(rec, "call"),
                 {"很不自在": 2, "有点不习惯": 1, "没什么感觉": 0}.get(rec.get("meal_feel"))],
        "孤独": [raw(rec, "prefer"), alone,
                 {"经常": 2, "偶尔": 1, "从不": 0}.get(rec.get("dorm_msg"))],
        "比较": [cmp_lv, 2 * h / 3 if h is not None else None,
                 {"更焦虑，觉得自己落后": 2, "没什么变化": 1, "放松了一些": 0}.get(rec.get("after_feed"))],
        "拖延": [2 * h / 3 if h is not None else None,
                 (0.0 if "没试过" not in brk else 2.0) if brk else None,
                 {"几乎每天": 2, "每周几次": 1.5, "偶尔": 1, "几乎没有": 0}.get(rec.get("escape_freq"))],
        "回避": [raw(rec, "greet"), raw(rec, "at"),
                 {"经常": 2, "偶尔": 1, "从不": 0}.get(rec.get("refresh"))],
    }
    for d, vals in pairs.items():
        p = pick(vals)
        out[d] = clamp5(1 + 2 * p) if p is not None else None
    return out


def item_stats(recs, key, it):
    """单题分布：单选/打分 → 按选项计数；多选 → 按选项分别计数（可 >n）"""
    if it["type"] == "multi":
        cnt = Counter()
        for r in recs:
            for v in str(r.get(it["id"]) or "").split("|"):
                if v:
                    cnt[v] += 1
        return [{"label": o, "count": cnt.get(o, 0)} for o in it["options"]]
    if it["type"] == "text":
        vals = [str(r[it["id"]]) for r in recs if r.get(it["id"])]
        return [{"label": v, "count": 1} for v in vals[:200]]
    cnt = Counter(r.get(it["id"]) for r in recs if r.get(it["id"]) not in (None, ""))
    return [{"label": o, "count": cnt.get(o, 0)} for o in it["options"]]


def build_stats(section="all"):
    recs = load_records()
    n = len(recs)
    finished = sum(1 for r in recs if r.get("_finished"))

    out = {"n": n, "finished": finished, "sections": [], "dims": DIMS}

    keys = list(SEC_BY_KEY) if section == "all" else [section]
    for k in keys:
        s = SEC_BY_KEY.get(k)
        if not s:
            continue
        block = {"key": k, "no": s["no"], "title": s["title"], "kicker": s["kicker"],
                 "items": []}
        for it in s["items"]:
            st = item_stats(recs, k, it)
            answered = sum(x["count"] for x in st) if it["type"] != "multi" else \
                sum(1 for r in recs if r.get(it["id"]))
            block["items"].append({"id": it["id"], "q": it["q"], "type": it["type"],
                                   "options": it["options"], "stats": st,
                                   "answered": answered,
                                   "rate": round(answered / n * 100, 1) if n else 0})
        out["sections"].append(block)

    # 测试类型分布 + 平均分
    types, scores = Counter(), []
    for r in recs:
        sc, name = quiz_score(r)
        if name:
            types[name] += 1
            scores.append(sc)
    out["types"] = [{"label": b[2], "count": types.get(b[2], 0)} for b in RESULT_BANDS]
    out["avg_score"] = round(sum(scores) / len(scores), 2) if scores else 0

    # 五维均值（只对有作答的维度取均值）
    radar = []
    for d in DIMS:
        vals = [dim_scores(r)[d] for r in recs]
        vals = [v for v in vals if v is not None]
        radar.append(round(sum(vals) / len(vals), 2) if vals else 0)
    out["radar"] = radar

    # 高频表述（真实选项原文的出现次数）
    wc = Counter()
    for r in recs:
        for k, it in items_of():
            v = r.get(it["id"])
            if not v:
                continue
            if it["type"] == "multi":
                for x in str(v).split("|"):
                    if x:
                        wc[x] += 1
            elif it["type"] != "scale":
                wc[str(v)] += 1
        _, name = quiz_score(r)
        if name:
            wc[name] += 1
    out["words"] = [{"name": k, "value": v} for k, v in wc.most_common(18)]

    # 板块完成率（回收质量）
    out["progress"] = []
    for s in DATA_SECTIONS:
        done = sum(1 for r in recs
                   if all(r.get(it["id"]) not in (None, "") for it in s["items"]))
        out["progress"].append({"key": s["key"], "no": s["no"], "title": s["title"],
                               "done": done, "rate": round(done / n * 100, 1) if n else 0})
    return out


# ============================================================
# 四、导出（每个板块都能单独导）
# ============================================================
def yn(v):
    return "是" if v is True else ("否" if v is False else v)


def export(section="all", fmt="csv"):
    recs = load_records()
    pairs = items_of(None if section == "all" else [section])
    if section == "all":
        pairs += [("_derived", {"id": "score", "label": "测试得分"}),
                  ("_derived", {"id": "type", "label": "测试类型"})]
        pairs += [("_derived", {"id": "scale_" + d, "label": "五维_" + d}) for d in DIMS]

    def cell(r, sec, it):
        iid = it["id"]
        if sec == "_derived":
            if iid == "score":
                return quiz_score(r)[0] if quiz_score(r)[0] is not None else ""
            if iid == "type":
                return quiz_score(r)[1] or ""
            if iid.startswith("scale_"):
                v = dim_scores(r)[iid[6:]]
                return v if v is not None else ""
            return ""
        v = r.get(iid)
        if v is None:
            return ""
        return v.replace("|", "、") if isinstance(v, str) else yn(v)

    rows = []
    head = ["sid", "提交时间", "是否完成"] + [it.get("label") or it["q"] for _, it in pairs]
    for r in recs:
        rows.append([r["_sid"], r.get("_created", ""), "已完成" if r.get("_finished") else "进行中"]
                    + [cell(r, sec, it) for sec, it in pairs])

    if fmt == "json":
        obj = {"section": section, "exported_at": now(), "count": len(recs),
               "columns": head, "records": [dict(zip(head, row)) for row in rows]}
        return json.dumps(obj, ensure_ascii=False, indent=2)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(head)
    w.writerows(rows)
    return "\ufeff" + buf.getvalue()          # BOM：Excel 打开不乱码


# ============================================================
# 五、HTTP 服务
# ============================================================
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8", ".json": "application/json",
        ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
        ".csv": "text/csv; charset=utf-8"}


class Handler(BaseHTTPRequestHandler):
    server_version = "SurveyStation/2.0"

    def log_message(self, fmt, *args):
        print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), fmt % args))

    def send_bytes(self, body, ctype="application/json; charset=utf-8", status=200, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def send_json(self, obj, status=200):
        self.send_bytes(json.dumps(obj, ensure_ascii=False), status=status)

    def send_file(self, path):
        if not os.path.isfile(path):
            return self.send_json({"error": "not found"}, 404)
        with open(path, "rb") as f:
            self.send_bytes(f.read(), MIME.get(os.path.splitext(path)[1].lower(),
                                                "application/octet-stream"))

    def read_json(self, limit=128 * 1024):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > limit:
            return {}
        try:
            obj = json.loads(self.rfile.read(length).decode("utf-8"))
            return obj if isinstance(obj, dict) else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        q = parse_qs(u.query)

        if path == "/":
            return self.send_file(os.path.join(BASE, "templates", "index.html"))
        if path == "/admin":
            return self.send_file(os.path.join(BASE, "templates", "admin.html"))
        if path == "/api/sections":
            return self.send_json({"sections": SECTIONS, "dims": DIMS,
                                   "bands": [[b[0], b[1], b[2], b[3]] for b in RESULT_BANDS]})
        if path == "/api/stats":
            sec = (q.get("section") or ["all"])[0]
            if sec != "all" and sec not in SEC_BY_KEY:
                return self.send_json({"error": "unknown section"}, 400)
            return self.send_json(build_stats(sec))
        if path == "/api/export":
            sec = (q.get("section") or ["all"])[0]
            fmt = (q.get("fmt") or ["csv"])[0].lower()
            if sec != "all" and sec not in SEC_BY_KEY:
                return self.send_json({"error": "unknown section"}, 400)
            if fmt not in ("csv", "json"):
                return self.send_json({"error": "fmt must be csv or json"}, 400)
            body = export(sec, fmt)
            name = "survey-%s.%s" % (sec, fmt)
            ctype = MIME[".csv"] if fmt == "csv" else MIME[".json"]
            return self.send_bytes(body, ctype,
                                   extra={"Content-Disposition": 'attachment; filename="%s"' % name})
        if path == "/api/sessions":
            recs = load_records()
            rows = []
            for r in recs[-300:][::-1]:
                sc, name = quiz_score(r)
                answered = sum(1 for k, it in items_of() if r.get(it["id"]) not in (None, ""))
                rows.append({"sid": r["_sid"], "created": r.get("_created"),
                             "finished": bool(r.get("_finished")), "answered": answered,
                             "score": sc, "type": name})
            return self.send_json({"total": len(recs), "rows": rows,
                                   "item_total": len(items_of())})
        if path == "/api/mine":
            sid = (q.get("sid") or [""])[0]
            if not valid_sid(sid):
                return self.send_json({"answers": {}})
            with db() as c:
                rows = c.execute("SELECT item,value FROM answers WHERE sid=?", (sid,)).fetchall()
            return self.send_json({"sid": sid,
                                   "answers": {r["item"]: r["value"] for r in rows}})
        if path.startswith("/static/"):
            safe = re.sub(r"\.\.+", "", path[len("/static/"):])
            return self.send_file(os.path.join(BASE, "static", safe))
        return self.send_json({"error": "not found"}, 404)

    def do_HEAD(self):
        """支持下载工具/浏览器预检；只回头部"""
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        body = self.read_json()
        sid = str(body.get("sid") or "").strip()[:64]
        if not valid_sid(sid):
            return self.send_json({"error": "bad sid"}, 400)

        if path == "/api/answer":
            section = str(body.get("section") or "")
            if section and section not in SEC_BY_KEY:
                return self.send_json({"error": "unknown section"}, 400)
            raw = body.get("answers")
            if not isinstance(raw, dict):
                return self.send_json({"error": "answers must be an object"}, 400)
            # 只接受问卷定义里存在的题目，值必须是标量或字符串数组
            clean = {}
            legal = {it["id"] for _, it in items_of()}
            for k, v in raw.items():
                if k not in legal:
                    continue
                if isinstance(v, (list, tuple)):
                    v = [str(x)[:80] for x in v if isinstance(x, (str, int, float))][:20]
                    if v:
                        clean[k] = v
                elif isinstance(v, (str, int, float, bool)):
                    if str(v).strip():
                        clean[k] = v
            n = save_answers(sid, section, clean, self.headers.get("User-Agent") or "")
            return self.send_json({"ok": True, "saved": n})

        if path == "/api/finish":
            save_answers(sid, "", {})          # 确保会话存在
            mark_finished(sid)
            return self.send_json({"ok": True})

        return self.send_json({"error": "not found"}, 404)


def main():
    init_db()
    with db() as c:
        n = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        m = c.execute("SELECT COUNT(*) FROM answers").fetchone()[0]
    print("=" * 58)
    print("  问卷    http://127.0.0.1:%d/" % PORT)
    print("  看板    http://127.0.0.1:%d/admin" % PORT)
    print("  已回收  %d 份问卷 / %d 条作答（库：%s）" % (n, m, DB))
    print("  全站离线运行，不请求任何外部资源")
    print("  Ctrl+C 停止")
    print("=" * 58)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已停止。")
