"""
analyze_badges.py
fetch_chats.py で保存したチャットリプレイから、メンバーバッジとメンバーシップの種類(レベル)を集計し、
チャンネル主の取り分(推定収益)を計算する。

使い方:
    python analyze_badges.py data/<チャンネルID>                  # 直近30日 + 月ごとの集計
    python analyze_badges.py data/<チャンネルID> --end 2026-09-30     # 指定日までの30日
    python analyze_badges.py data/<チャンネルID> --keep-raw           # 元のチャットファイルを消さない

処理の流れ:
    1. chats/<動画ID>.live_chat.json (数MB〜数十MB) を1本ずつ読み、必要な情報だけを
       records/<動画ID>.json (十数KB) に書き出す。一度書いた records は次回から再利用する。
       元のチャットファイルは、取得から24時間たったら消す (--raw-keep-hours で変更)。
    2. records から「直近30日」と「月ごと」の期間で集計する。
       月が明けて7日たった月は「確定」とし、それ以降は作り直さない。
    3. 月末から31日たった月 (= 直近30日の集計に使われなくなった月) の records は、
       非メンバーの発言者IDを捨てて人数だけを残し、records/<YYYY-MM>.jsonl の1ファイルにまとめる。

料金の設定:
    data/<チャンネル名>/tiers.json にメンバーシップの種類と月額、料金の変更履歴を書く。
    ファイルがなければ、チャットで見つかったレベル名を入れたひな形を作る。

出力 (data/<チャンネル名>/report/):
    summary.md / summary.json   … 直近30日のまとめ (人が読む用 / Webサイト用)
    members.csv                 … 確認できたメンバー1人1行 (名前を含むので非公開)
    tiers.csv                   … レベルごとの人数と推定収益
    badge_distribution.csv      … バッジ(継続月数)ごとの人数
    streams.csv                 … 配信ごとの集計
    events.csv                  … 加入・レベル変更・マイルストーン・ギフトの一覧 (名前を含むので非公開)
    monthly/<YYYY-MM>.json      … 月ごとの集計 (Webサイトの過去データ用。"finalized": true なら確定)
"""

import argparse
import csv
import json
import re
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from channel_list import channel_map

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

JST = timezone(timedelta(hours=9))
RECORD_VERSION = 1
FINALIZE_DAYS = 7     # 月が明けて何日たったら、その月の集計を確定するか
COMPACT_DAYS = 31     # 月末から何日たったら、その月の records を1ファイルにまとめて非メンバーIDを捨てるか
RECORD_KEYS = ("v", "video_id", "chatters", "chatter_count", "messages", "superchats", "members", "events")

DEFAULT_TAX_RATE = 0.10       # 日本の消費税。表示価格は税込
DEFAULT_CREATOR_SHARE = 0.70  # YouTube がクリエイターに渡す割合 (税抜き売上の70%)


# ---------------------------------------------------------------------------
# テキストまわりのユーティリティ
# ---------------------------------------------------------------------------
def runs_text(obj) -> str:
    """YouTubeの {simpleText} / {runs:[{text}]} 形式を文字列にする"""
    if not obj:
        return ""
    if "simpleText" in obj:
        return obj["simpleText"]
    out = []
    for r in obj.get("runs", []):
        if "text" in r:
            out.append(r["text"])
        elif "emoji" in r:
            sc = r["emoji"].get("shortcuts") or [""]
            out.append(sc[0])
    return "".join(out)


MONTH_RE = re.compile(r"(\d+)\s*(?:months?|か月|ヶ月|ヵ月|カ月|ケ月|个月|個月)", re.I)
YEAR_RE = re.compile(r"(\d+)\s*(?:years?|年)", re.I)
NEW_RE = re.compile(r"new member|新規メンバー|新メンバー|新しいメンバー", re.I)


def parse_months(text: str):
    """'Member (6 months)' / 'メンバー（1 年）' / '新規メンバー' などを継続月数に変換。
    読み取れなければ None。"""
    if not text:
        return None
    if NEW_RE.search(text):
        return 0
    y = YEAR_RE.search(text)
    m = MONTH_RE.search(text)
    if not y and not m:
        return None
    return (int(y.group(1)) * 12 if y else 0) + (int(m.group(1)) if m else 0)


def months_label(m) -> str:
    if m is None:
        return "不明(バッジあり)"
    if m == 0:
        return "新規(1ヶ月未満)"
    y, mm = divmod(m, 12)
    if y and mm:
        return f"{y}年{mm}ヶ月"
    if y:
        return f"{y}年"
    return f"{mm}ヶ月"


def read_badges(renderer: dict):
    """authorBadges から (メンバーか, 継続月数, バッジ文言) を返す"""
    is_member, months, label = False, None, ""
    for b in renderer.get("authorBadges", []) or []:
        br = b.get("liveChatAuthorBadgeRenderer", {})
        if "customThumbnail" in br:          # メンバーバッジは独自画像を持つ
            is_member = True
            label = br.get("tooltip") or (br.get("accessibility", {}).get("accessibilityData", {}).get("label", ""))
            months = parse_months(label)
    return is_member, months, label


FIRST_INT_RE = re.compile(r"(\d[\d,]*)")


def gift_count(primary: dict, text: str) -> int:
    """「Sent 5 ○○ gift memberships」の 5 を取り出す。
    人数は数字だけの run に分かれて入っているので、それを優先する
    (チャンネル名に数字が含まれていても誤読しないため)"""
    for r in (primary or {}).get("runs", []):
        t = r.get("text", "").strip().replace(",", "")
        if t.isdigit():
            return int(t)
    n = FIRST_INT_RE.search(text)
    return int(n.group(1).replace(",", "")) if n else 1


WELCOME_RE = [re.compile(r"^\s*Welcome to (.+?)\s*!?\s*$", re.I),
              re.compile(r"^\s*(.+?)\s*へようこそ\s*[!！]?\s*$")]


def welcome_tier(sub: dict) -> str:
    """「Welcome to level1!」「level1 へようこそ！」からレベル名を取り出す"""
    runs = [r.get("text", "") for r in (sub or {}).get("runs", [])]
    text = "".join(runs)
    for rx in WELCOME_RE:
        m = rx.match(text)
        if m:
            return m.group(1).strip()
    # 想定外の言語: 3つ以上の run に分かれていれば真ん中がレベル名
    if len(runs) >= 3:
        return "".join(runs[1:-1]).strip()
    return ""


def tier_key(name: str) -> str:
    return (name or "").strip().lower()


def md_cell(v) -> str:
    return str(v).replace("|", "｜").replace("\n", " ")


def fmt_ts(ts):
    return datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d %H:%M") if ts else "?"


def fmt_date(ts):
    return datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d") if ts else "?"


def yen(v) -> str:
    return "–" if v is None else f"¥{v:,.0f}"


def yen_range(lo, hi) -> str:
    """下限と推定が同じ (メンバーシップが1種類など) なら1つだけ"""
    return yen(lo) if round(lo) == round(hi) else f"{yen(lo)} 〜 {yen(hi)}"


# ---------------------------------------------------------------------------
# 1. チャットファイル → records (1本ごとの必要最小限の情報)
# ---------------------------------------------------------------------------
def iter_items(path: Path):
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            rep = obj.get("replayChatItemAction", {})
            offset = rep.get("videoOffsetTimeMsec") or obj.get("videoOffsetTimeMsec")
            for act in rep.get("actions", []):
                item = (act.get("addChatItemAction") or {}).get("item")
                if not item:
                    continue
                for rtype, r in item.items():
                    yield rtype, r, offset


def parse_chat(path: Path, video_id: str) -> dict:
    """チャットリプレイ1本を読み、集計に必要な情報だけを返す。
    chatters は発言者のチャンネルID (メンバー率の計算用)、members はメンバーバッジ付きの発言者、
    events は加入・マイルストーン・ギフトを時刻順に並べたもの。"""
    chatters, members, events = set(), {}, []
    messages = superchats = 0
    for rtype, r, offset in iter_items(path):
        cid = r.get("authorExternalChannelId")
        name = runs_text(r.get("authorName"))
        off = int(offset or 0)

        if rtype in ("liveChatTextMessageRenderer", "liveChatPaidMessageRenderer", "liveChatPaidStickerRenderer"):
            if not cid:
                continue
            chatters.add(cid)
            messages += 1
            paid = rtype != "liveChatTextMessageRenderer"
            superchats += paid
            is_member, months, label = read_badges(r)
            if is_member:
                m = members.setdefault(cid, {"h": name, "b": None, "l": "", "n": 0, "sc": 0})
                m["h"] = name or m["h"]
                if months is not None and (m["b"] is None or months > m["b"]):
                    m["b"], m["l"] = months, label
                elif label and not m["l"]:
                    m["l"] = label
                m["n"] += 1
                m["sc"] += paid

        elif rtype == "liveChatMembershipItemRenderer":
            if not cid:
                continue
            chatters.add(cid)
            _, b_months, label = read_badges(r)
            primary = runs_text(r.get("headerPrimaryText"))
            if primary:   # 「Member for 6 months」+ headerSubtext にレベル名 = 継続マイルストーン
                events.append({"t": "milestone", "cid": cid, "h": name, "b": b_months, "l": label,
                               "tier": runs_text(r.get("headerSubtext")).strip(),
                               "ms": parse_months(primary), "off": off, "text": primary})
            else:         # 「Welcome to level1!」
                # バッジが2ヶ月以上なら新規ではなく、再加入かレベル変更 (アップグレード/ダウングレード)
                t = "rejoin" if b_months is not None and b_months >= 2 else "join"
                events.append({"t": t, "cid": cid, "h": name, "b": b_months, "l": label,
                               "tier": welcome_tier(r.get("headerSubtext")), "off": off,
                               "text": runs_text(r.get("headerSubtext"))})

        elif rtype == "liveChatSponsorshipsGiftPurchaseAnnouncementRenderer":
            header = (r.get("header") or {}).get("liveChatSponsorshipsHeaderRenderer", {})
            name = runs_text(header.get("authorName")) or name
            text = runs_text(header.get("primaryText"))
            is_member, months, label = read_badges(header)
            if cid:
                chatters.add(cid)
            events.append({"t": "gift_buy", "cid": cid, "h": name, "member": is_member, "b": months, "l": label,
                           "n": gift_count(header.get("primaryText"), text), "off": off, "text": text})

        elif rtype == "liveChatSponsorshipsGiftRedemptionAnnouncementRenderer":
            if not cid:
                continue
            events.append({"t": "gift_received", "cid": cid, "h": name, "off": off,
                           "text": runs_text(r.get("message"))})
    events.sort(key=lambda e: e["off"])
    return {"v": RECORD_VERSION, "video_id": video_id, "chatters": sorted(chatters),
            "messages": messages, "superchats": superchats, "members": members, "events": events}


def load_records(data_dir: Path, videos: dict, raw_keep_hours):
    """chats/ を records/ に変換しつつ、全 records (1本1ファイルのものと、月ごとにまとめたもの) を読み込む。
    raw_keep_hours 時間より前に取得した元のチャットファイルは、records に変換済みなら消す (None なら消さない)"""
    chats_dir, rec_dir = data_dir / "chats", data_dir / "records"
    rec_dir.mkdir(exist_ok=True)
    now, removed = time.time(), 0
    packed = set()   # 月ごとにまとめ済みの動画 (元ファイルが残っていても変換し直さない)
    for pp in rec_dir.glob("*.jsonl"):
        with pp.open(encoding="utf-8") as f:
            packed.update(json.loads(line)["video_id"] for line in f if line.strip())
    for fp in sorted(chats_dir.glob("*.live_chat.json")) if chats_dir.exists() else []:
        vid = fp.name[: -len(".live_chat.json")]
        rp = rec_dir / f"{vid}.json"
        if vid not in packed and (not rp.exists() or json.loads(rp.read_text(encoding="utf-8")).get("v") != RECORD_VERSION):
            print(f"  変換中: {vid} {((videos.get(vid) or {}).get('title') or '')[:40]}")
            rec = parse_chat(fp, vid)
            rp.write_text(json.dumps(rec, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        if raw_keep_hours is not None and now - fp.stat().st_mtime >= raw_keep_hours * 3600:
            fp.unlink()
            removed += 1
    if removed:
        print(f"  元のチャットファイル {removed} 本を削除しました (records に変換済み)")

    def attach(rec, src):
        v = videos.get(rec["video_id"]) or {}
        rec["start_ts"] = v.get("start_ts") or src.stat().st_mtime
        rec["title"] = v.get("title") or ""
        rec["kind"] = v.get("kind") or "live"
        rec["members_only"] = bool(v.get("members_only"))
        rec["view_count"], rec["duration_sec"] = v.get("view_count"), v.get("duration_sec")
        rec["_file"] = src
        return rec

    records = []
    for rp in rec_dir.glob("*.json"):
        records.append(attach(json.loads(rp.read_text(encoding="utf-8")), rp))
    for pp in rec_dir.glob("*.jsonl"):
        with pp.open(encoding="utf-8") as f:
            records.extend(attach(json.loads(line), pp) for line in f if line.strip())
    records.sort(key=lambda r: r["start_ts"])
    return records


def month_bounds(mon: str):
    y, mo = map(int, mon.split("-"))
    return (datetime(y, mo, 1, tzinfo=JST).timestamp(),
            datetime(y + (mo == 12), mo % 12 + 1, 1, tzinfo=JST).timestamp())


def compact_records(data_dir: Path, records, now: float):
    """月末から COMPACT_DAYS 日たった月の records を records/<YYYY-MM>.jsonl にまとめ、
    非メンバーの発言者ID (chatters) を捨てて人数 (chatter_count) だけ残す。
    その月は確定済みで、直近30日の集計にも使われないので、IDがなくても困らない。"""
    rec_dir = data_dir / "records"
    by_month = {}
    for r in records:
        if r["_file"].suffix == ".json":
            by_month.setdefault(datetime.fromtimestamp(r["start_ts"], JST).strftime("%Y-%m"), []).append(r)
    for mon, recs in sorted(by_month.items()):
        if now < month_bounds(mon)[1] + COMPACT_DAYS * 86400:
            continue
        pack = rec_dir / f"{mon}.jsonl"
        lines = {}
        if pack.exists():
            with pack.open(encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        o = json.loads(line)
                        lines[o["video_id"]] = o
        for r in recs:
            o = {k: r[k] for k in RECORD_KEYS if k in r}
            if "chatters" in o:
                o["chatter_count"] = len(o.pop("chatters"))
            lines[o["video_id"]] = o
        tmp = pack.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(json.dumps(o, ensure_ascii=False, separators=(",", ":")) + "\n"
                               for o in sorted(lines.values(), key=lambda o: o["video_id"])), encoding="utf-8")
        tmp.replace(pack)
        for r in recs:
            r["_file"].unlink(missing_ok=True)
        print(f"  {mon} の records {len(recs)} 本を {pack.name} にまとめました (非メンバーのIDを削除)")


# ---------------------------------------------------------------------------
# 2. 料金の設定 (tiers.json)
# ---------------------------------------------------------------------------
def load_tier_config(path: Path, found_tiers):
    """tiers.json を読む。なければ見つかったレベル名でひな形を書き出す。
    料金の変更履歴に対応した形式に正規化して返す (料金が未設定なら None)"""
    if not path.exists():
        template = {
            "tiers": [{"name": t, "prices": [{"from": None, "price": None}]} for t in sorted(found_tiers, key=tier_key)]
                     or [{"name": "（レベル名）", "prices": [{"from": None, "price": None}]}],
            "gift_tier": None,
            "currency": "JPY",
            "tax_rate": DEFAULT_TAX_RATE,
            "creator_share": DEFAULT_CREATOR_SHARE,
            "source": "",
            "changes": [],
        }
        path.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
        return None
    cfg = json.loads(path.read_text(encoding="utf-8"))
    tiers = []
    for t in cfg.get("tiers", []):
        if not t.get("name"):
            continue
        prices = t.get("prices") or [{"from": None, "price": t.get("price")}]   # 旧形式 {"price": 490} にも対応
        prices = sorted(prices, key=lambda p: p.get("from") or "")
        tiers.append({"name": t["name"], "prices": prices})
    # 一度も料金が入っていないレベル (雛形の「（レベル名）」や、チャットから拾っただけの名前) は料金表に含めない
    tiers = [t for t in tiers if any(isinstance(p.get("price"), (int, float)) for p in t["prices"])]
    if not tiers or cfg.get("membership") is False:
        return None
    cfg["tiers"] = tiers
    cfg.setdefault("tax_rate", DEFAULT_TAX_RATE)
    cfg.setdefault("creator_share", DEFAULT_CREATOR_SHARE)
    cfg.setdefault("currency", "JPY")
    cfg.setdefault("changes", [])
    return cfg


def tiers_at(cfg, date: str):
    """date (YYYY-MM-DD) 時点の料金表。料金が null のレベルはその時点で提供されていないとみなす。
    最初の確認日より前は、最初に確認した料金で計算する。"""
    out = []
    for t in cfg["tiers"]:
        eff = [p for p in t["prices"] if not p.get("from") or p["from"] <= date]
        p = eff[-1] if eff else t["prices"][0]
        if isinstance(p.get("price"), (int, float)):
            out.append({"name": t["name"], "price": p["price"]})
    out.sort(key=lambda t: t["price"])
    return out


def price_history(cfg):
    """料金の変更履歴を、日付順の一覧にする"""
    rows = []
    for t in cfg["tiers"]:
        prev = None
        for p in t["prices"]:
            rows.append({"date": p.get("from") or "", "tier": t["name"], "price": p.get("price"), "before": prev})
            prev = p.get("price")
    rows.sort(key=lambda r: (r["date"], r["tier"]))
    return rows


# ---------------------------------------------------------------------------
# 3. 期間を指定して集計
# ---------------------------------------------------------------------------
def aggregate(records, w_from, w_to, cfg_all, videos, lookback_days):
    """w_from <= 開始時刻 < w_to の配信を集計する。
    レベルは期間内のイベントに加え、lookback_days 日前までの加入通知・マイルストーンも手がかりにする。"""
    in_win = [r for r in records if w_from <= r["start_ts"] < w_to]
    members, events, stream_rows = {}, [], []
    all_chatters, ids_dropped = set(), False

    def touch(cid, name, months, label, ts, vid):
        m = members.get(cid)
        if m is None:
            m = members[cid] = {
                "channel_id": cid, "name": name, "max_months": None, "badge_label": "",
                "streams": set(), "messages": 0, "superchats": 0, "first_seen_ts": ts, "last_seen_ts": ts,
                "joined_in_period": False, "gift_received": False, "milestone_months": None,
                "tier": "", "tier_source": "", "tier_at": None,
            }
        m["name"] = name or m["name"]
        if months is not None and (m["max_months"] is None or months > m["max_months"]):
            m["max_months"] = months
            if label:
                m["badge_label"] = label
        elif label and not m["badge_label"]:
            m["badge_label"] = label
        m["streams"].add(vid)
        m["first_seen_ts"] = min(m["first_seen_ts"], ts)
        m["last_seen_ts"] = max(m["last_seen_ts"], ts)
        return m

    type_label = {"join": "新規加入", "rejoin": "再加入/レベル変更", "milestone": "継続マイルストーン",
                  "gift_buy": "ギフト購入", "gift_received": "ギフト受け取り"}
    for rec in in_win:
        ts, vid = rec["start_ts"], rec["video_id"]
        member_ids = set()
        if "chatters" in rec:
            all_chatters.update(rec["chatters"])
        else:
            ids_dropped = True   # 古い月はIDを捨てているので、期間全体のユニーク人数は出せない
        for cid, o in rec["members"].items():
            m = touch(cid, o["h"], o["b"], o["l"], ts, vid)
            m["messages"] += o["n"]
            m["superchats"] += o["sc"]
            member_ids.add(cid)
        st = Counter()
        for e in rec["events"]:
            cid, t = e.get("cid"), e["t"]
            if t == "join":
                m = touch(cid, e["h"], e["b"] if e["b"] is not None else 0, e["l"], ts, vid)
                m["joined_in_period"] = True
            elif t in ("rejoin", "milestone"):
                m = touch(cid, e["h"], e["b"], e["l"], ts, vid)
                if t == "milestone" and e.get("ms") is not None:
                    m["milestone_months"] = max(e["ms"], m["milestone_months"] or 0)
            elif t == "gift_buy":
                st["gifted_total"] += e["n"]
                if cid and e.get("member"):
                    touch(cid, e["h"], e["b"], e["l"], ts, vid)
                    member_ids.add(cid)
            elif t == "gift_received":
                m = touch(cid, e["h"], 0, "", ts, vid)
                m["gift_received"] = m["joined_in_period"] = True
            if t in ("join", "rejoin", "milestone", "gift_received"):
                member_ids.add(cid)
            st[t] += 1
            events.append({"stream_jst": fmt_ts(ts), "video_id": vid, "offset_ms": e["off"], "type": type_label[t],
                           "tier": e.get("tier", ""), "name": e["h"], "channel_id": cid,
                           "value": e.get("n") if t == "gift_buy" else (e.get("ms") if t == "milestone" else e.get("l", "")),
                           "text": e.get("text", "")})
        nc = len(rec["chatters"]) if "chatters" in rec else rec.get("chatter_count", 0)
        nm = len(member_ids)
        stream_rows.append({
            "video_id": vid, "start_jst": fmt_ts(ts), "title": rec["title"],
            "kind": {"premiere": "プレミア"}.get(rec["kind"], "配信"),
            "members_only": "○" if rec["members_only"] else "",
            "view_count": rec.get("view_count") or "",
            "duration_min": round(rec["duration_sec"] / 60) if rec.get("duration_sec") else "",
            "chatters": nc, "member_chatters": nm, "member_ratio_pct": round(nm / nc * 100, 1) if nc else "",
            "messages": rec["messages"], "member_messages": sum(o["n"] for o in rec["members"].values()),
            "new_members": st["join"], "rejoins": st["rejoin"], "milestones": st["milestone"],
            "gift_purchases": st["gift_buy"], "gifted_total": st["gifted_total"],
            "gift_redemptions": st["gift_received"], "superchats": rec["superchats"],
        })

    # レベル: 期間内 + lookback のイベントのうち、期間終了までで一番新しいもの
    lb_from = w_from - lookback_days * 86400
    for rec in records:
        if not (lb_from <= rec["start_ts"] < w_to):
            continue
        for e in rec["events"]:
            if e["t"] not in ("join", "rejoin", "milestone") or not e.get("tier"):
                continue
            m = members.get(e["cid"])
            if m is None:
                continue
            at = (rec["start_ts"], e["off"])
            if m["tier_at"] is None or at >= m["tier_at"]:
                src = {"join": "新規加入", "rejoin": "再加入/レベル変更", "milestone": "マイルストーン"}[e["t"]]
                if rec["start_ts"] < w_from:
                    src += f"（{fmt_date(rec['start_ts'])}）"
                m["tier"], m["tier_source"], m["tier_at"] = e["tier"], src, at

    # 料金 (期間の最終日時点)
    end_date = fmt_date(w_to - 1)
    tiers_now = tiers_at(cfg_all, end_date) if cfg_all else None
    found_tiers = {m["tier"] for m in members.values() if m["tier"]}
    tier_keys = {tier_key(t["name"]): t for t in tiers_now} if tiers_now else {}
    gift_tier = (cfg_all or {}).get("gift_tier") or (tiers_now[0]["name"] if tiers_now else "")
    single = bool(tiers_now) and len(tiers_now) == 1
    for m in members.values():
        if m["gift_received"] and not m["tier"] and tiers_now:
            m["tier"], m["tier_source"] = gift_tier, "ギフト"
        m["class"] = classify_member(m, cfg_all, tiers_now)
        # レベル名が料金表にない人は「レベル不明」として扱う (レベルが1種類なら、どの名前でもそのレベル)。
        # 例: レベル名を付けていないチャンネルでは「チャンネル メンバーシップ」「Channel Membership」など
        #     見る人の言語で名前が変わる
        if m["class"] == "known" and tiers_now and not single and tier_key(m["tier"]) not in tier_keys:
            m["class"] = "unknown"
    cls = Counter(m["class"] for m in members.values())

    tier_rows, rev, known_total = [], None, 0
    if tiers_now:
        tax, share = cfg_all["tax_rate"], cfg_all["creator_share"]
        net = lambda price: price / (1 + tax) * share   # noqa: E731
        n_t = len(tiers_now)
        if single:
            known_by_idx = [cls["known"]]
        else:
            known_by_tier = Counter(tier_key(m["tier"]) for m in members.values() if m["class"] == "known")
            known_by_idx = [known_by_tier[tier_key(t["name"])] for t in tiers_now]
        known_total = sum(known_by_idx)
        n_unknown = cls["unknown"]
        # 推定: レベル不明の人を、わかった人の比率で割り振る (わかった人がいなければ全員いちばん安いレベル)
        ratios = [(k / known_total) if known_total else (1.0 if i == 0 else 0.0) for i, k in enumerate(known_by_idx)]
        est = [known_by_idx[i] + n_unknown * ratios[i] for i in range(n_t)]
        # 下限: レベル不明の人を全員いちばん安いレベルとみなす
        low = list(known_by_idx)
        low[0] += n_unknown
        for i, t in enumerate(tiers_now):
            tier_rows.append({
                "tier": t["name"], "price": t["price"], "net_per_member": round(net(t["price"]), 1),
                "confirmed_members": known_by_idx[i],
                "share_of_confirmed_pct": round(ratios[i] * 100, 1) if known_total else "",
                "allocated_unknown": round(n_unknown * ratios[i], 1),
                "estimated_members": round(est[i], 1),
                "monthly_net_confirmed": round(known_by_idx[i] * net(t["price"])),
                "monthly_net_estimated": round(est[i] * net(t["price"])),
            })
        rev = {"low": sum(low[i] * net(t["price"]) for i, t in enumerate(tiers_now)),
               "est": sum(est[i] * net(t["price"]) for i, t in enumerate(tiers_now))}
        gift_price = tier_keys.get(tier_key(gift_tier), tiers_now[0])["price"]
        gifted = sum(r["gifted_total"] for r in stream_rows)
        rev["gift_once"] = gifted * net(gift_price)
        rev["paying"] = cls["known"] + cls["unknown"]
        rev["single_tier"] = single
        rev["net_lowest"] = net(tiers_now[0]["price"])
        rev["gift_price"] = gift_price
        rev["gifted"] = gifted

    dist = Counter(m["max_months"] for m in members.values())
    total = len(members)
    keys = sorted(dist, key=lambda k: (k is None, k if k is not None else 0))
    dist_rows = [{"badge_months": "" if k is None else k, "badge": months_label(k), "members": dist[k],
                  "percent": round(dist[k] / total * 100, 1) if total else 0} for k in keys]

    # 期間内で、チャットを集計できなかった枠・メンバー限定コンテンツ
    in_period = [v for v in videos.values() if v.get("start_ts") and w_from <= v["start_ts"] < w_to]
    missed = sorted([v for v in in_period if v.get("status") not in ("ok", "skip_old", "skip_not_stream")
                     and not (v.get("status") == "members_only_video")], key=lambda v: v["start_ts"])
    members_only = sorted([v for v in in_period if v.get("members_only")], key=lambda v: v["start_ts"])

    return {
        "from": w_from, "to": w_to, "records": in_win, "members": members, "events": events,
        "stream_rows": stream_rows, "unique_chatters": None if ids_dropped else len(all_chatters), "total_members": total,
        "cls": cls, "tier_rows": tier_rows, "tiers_now": tiers_now, "rev": rev, "known_total": known_total,
        "dist_rows": dist_rows, "found_tiers": found_tiers,
        "unknown_tier_names": [] if single else sorted(t for t in found_tiers if tiers_now and tier_key(t) not in tier_keys),
        "missed": missed, "members_only": members_only, "end_date": end_date,
    }


def classify_member(m, cfg, tiers_now):
    """収益計算上の区分を返す: 'gift' / 'known' / 'unknown'"""
    if m["gift_received"] and m["tier_source"] in ("", "ギフト"):
        return "gift"
    return "known" if m["tier"] else "unknown"


# ---------------------------------------------------------------------------
# 4. 出力
# ---------------------------------------------------------------------------
def write_csv(path: Path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


MISSED_REASON = {
    "no_chat": "チャットリプレイなし（アーカイブ編集・チャット無効の可能性）",
    "members_only": "メンバー限定配信",
    "became_members_only": "配信後にメンバー限定に変更",
    "chat_failed": "チャット取得失敗（次回再確認）",
    "error": "取得エラー（次回再確認）",
    "processing": "アーカイブ処理中（次回再確認）",
    "live_now": "取得時に配信中（次回再確認）",
}


def public_summary(R, meta, cfg_all, ch):
    """Webサイト用。個人を特定する情報 (名前・チャンネルID) は入れない"""
    rev = R["rev"]
    return {
        "channel": ch, "channel_id": meta.get("channel_id"), "handle": meta.get("handle", ""),
        "subscribers": meta.get("subscribers"), "fetched_at": meta.get("fetched_at"),
        "generated_at": fmt_ts(datetime.now().timestamp()),
        "period": {"from": fmt_ts(R["from"]), "to": fmt_ts(R["to"]),
                   "days": round((R["to"] - R["from"]) / 86400)},
        "streams_analyzed": len(R["stream_rows"]), "streams_missed": len(R["missed"]),
        "unique_chatters": R["unique_chatters"], "members_confirmed": R["total_members"],
        "member_classes": dict(R["cls"]),
        "badges": [{"months": r["badge_months"], "label": r["badge"], "members": r["members"]} for r in R["dist_rows"]],
        "events": {k: sum(r[k] for r in R["stream_rows"]) for k in
                   ("new_members", "rejoins", "milestones", "gift_purchases", "gifted_total", "gift_redemptions")},
        "tiers_configured": bool(R["tiers_now"]),
        "tiers": R["tier_rows"] if R["tiers_now"] else
                 [{"tier": k, "confirmed_members": v} for k, v in
                  Counter(m["tier"] for m in R["members"].values() if m["tier"]).items()],
        "revenue": ({"monthly_low": round(rev["low"]), "monthly_est": round(rev["est"]),
                     "gift_once": round(rev["gift_once"]), "total_low": round(rev["low"] + rev["gift_once"]),
                     "total_est": round(rev["est"] + rev["gift_once"]), "paying_members": rev["paying"],
                     "single_tier": rev["single_tier"],
                     "tax_rate": cfg_all["tax_rate"], "creator_share": cfg_all["creator_share"],
                     "overseas": bool(cfg_all.get("overseas")), "currency": cfg_all.get("currency", "JPY"),
                     "price_source": "" if (cfg_all.get("source") or "").startswith("料金の出典 (") else cfg_all.get("source", "")}
                    if rev else None),
        "streams": [{"video_id": r["video_id"], "start": r["start_jst"], "title": r["title"], "kind": r["kind"],
                     "chatters": r["chatters"], "member_chatters": r["member_chatters"],
                     "new_members": r["new_members"], "gifted_total": r["gifted_total"]} for r in R["stream_rows"]],
        "missed_streams": [{"video_id": v["id"], "start": v.get("start_jst"), "title": v.get("title"),
                            "kind": v.get("kind", "live"), "status": v.get("status"),
                            "reason": MISSED_REASON.get(v.get("status"), v.get("status")),
                            "members_only": bool(v.get("members_only")), "members_level": v.get("members_level", ""),
                            "like_count": v.get("like_count"), "view_count": v.get("view_count")} for v in R["missed"]],
        "members_only_content": [{"video_id": v["id"], "start": v.get("start_jst"), "title": v.get("title"),
                                  "kind": v.get("kind", "live"), "status": v.get("status"),
                                  "members_level": v.get("members_level", ""), "like_count": v.get("like_count"),
                                  "view_count": v.get("view_count")} for v in R["members_only"]],
    }


def monthly_summary(R, month, coverage_from, coverage_to):
    rev = R["rev"]
    return {
        "month": month, "coverage": {"from": fmt_date(coverage_from), "to": fmt_date(coverage_to - 1)},
        "streams_analyzed": len(R["stream_rows"]), "streams_missed": len(R["missed"]),
        "unique_chatters": R["unique_chatters"], "members_confirmed": R["total_members"],
        "member_classes": dict(R["cls"]),
        "long_term_members": sum(r["members"] for r in R["dist_rows"]
                                 if isinstance(r["badge_months"], int) and r["badge_months"] >= 12),
        "badges": [{"months": r["badge_months"], "members": r["members"]} for r in R["dist_rows"]],
        "events": {k: sum(r[k] for r in R["stream_rows"]) for k in
                   ("new_members", "rejoins", "milestones", "gifted_total")},
        "tiers": [{"tier": t["tier"], "price": t["price"], "confirmed_members": t["confirmed_members"],
                   "estimated_members": t["estimated_members"]} for t in R["tier_rows"]],
        "revenue": ({"monthly_low": round(rev["low"]), "monthly_est": round(rev["est"]),
                     "gift_once": round(rev["gift_once"])} if rev else None),
        "avg_member_chatters": round(statistics.mean(r["member_chatters"] for r in R["stream_rows"]), 1)
                               if R["stream_rows"] else None,
    }


def write_markdown(R, meta, cfg_all, ch, tiers_path, history, report_dir):
    L = []
    rows, rev, cls = R["stream_rows"], R["rev"], R["cls"]
    total = R["total_members"]
    L.append(f"# メンバーシップ集計レポート: {ch}")
    L.append("")
    L.append(f"- 対象期間: {fmt_ts(R['from'])} 〜 {fmt_ts(R['to'])}（チャットを集計できた配信・プレミア {len(rows)} 本）")
    if meta.get("fetched_at"):
        L.append(f"- データ取得日時: {meta['fetched_at']}")
    if R["missed"]:
        L.append(f"- チャットを集計できなかった枠: {len(R['missed'])} 本（下の「集計できなかった配信」を参照）")
    L.append("")

    L.append("## 概要")
    L.append("")
    pct = f"{total / R['unique_chatters'] * 100:.1f}%" if R["unique_chatters"] else "-"
    L.append("| 項目 | 値 |")
    L.append("|---|---:|")
    L.append(f"| **確認できたメンバー（確実な下限）** | **{total:,} 人** |")
    if R["unique_chatters"] is not None:
        L.append(f"| 発言したユニークユーザー | {R['unique_chatters']:,} 人（うちメンバー {pct}） |")
    if rev:
        L.append(f"| 推定収益（1ヶ月・チャンネル主の取り分） | {yen_range(rev['low'] + rev['gift_once'], rev['est'] + rev['gift_once'])} |")
    L.append("")
    L.append("> メンバーかどうかは、発言のメンバーバッジ・加入通知・ギフト受け取りで判定しています。"
             "期間中にチャットに出てこなかったメンバー（ROM専など）は数えられないため、実際の人数・収益はこれより多いはずです。")
    L.append("")

    L.append("## メンバーシップの種類（レベル）別")
    L.append("")
    if R["tiers_now"]:
        L.append(f"料金は {R['end_date']} 時点のものです。")
        L.append("")
        L.append("| レベル | 月額（税込） | 1人あたりの取り分 | 確認できた人数 | 不明分の割り振り | 推定人数 | 推定月収 |")
        L.append("|---|---:|---:|---:|---:|---:|---:|")
        for r in R["tier_rows"]:
            L.append(f"| {md_cell(r['tier'])} | {yen(r['price'])} | {yen(r['net_per_member'])} | {r['confirmed_members']} 人 "
                     f"| +{r['allocated_unknown']:.1f} 人 | {r['estimated_members']:.1f} 人 | {yen(r['monthly_net_estimated'])} |")
        L.append("")
        L.append("| 区分 | 人数 | 説明 |")
        L.append("|---|---:|---|")
        L.append(f"| レベルがわかった | {cls['known']} 人 | 加入通知・継続マイルストーンにレベル名が出た人（過去{LOOKBACK_DAYS}日のイベントも使用） |")
        L.append(f"| レベル不明 | {cls['unknown']} 人 | チャットのバッジだけで確認した人（バッジはレベル共通のため判別不可） |")
        L.append(f"| ギフトで加入 | {cls['gift']} 人 | 料金は贈った人が払った一回きりの支払いなので、ギフト収益として別に計上 |")
        L.append("")
        if R["unknown_tier_names"]:
            L.append(f"> ⚠ tiers.json にないレベル名がチャットに出ています: {', '.join(R['unknown_tier_names'])}。"
                     "該当者は推定月収に含まれていません。tiers.json に追加してください。")
            L.append("")
    else:
        names = ", ".join(sorted(R["found_tiers"])) or "（見つかりませんでした）"
        L.append(f"チャットで見つかったレベル名: {names}")
        L.append("")
        L.append(f"> 料金が未設定のため、収益は計算していません。`{tiers_path}` に各レベルの月額（税込）を書き込んで再実行してください。")
        L.append("")

    if rev:
        L.append("## チャンネル主の推定収益")
        L.append("")
        L.append("### 人数の内訳")
        L.append("")
        L.append("| | 人数 |")
        L.append("|---|---:|")
        L.append(f"| 確認できたメンバー | {total} 人 |")
        L.append(f"| − ギフトで加入（贈った人が支払い済み） | {cls['gift']} 人 |")
        L.append(f"| **= 月額を払っているメンバー** | **{rev['paying']} 人** |")
        L.append("")
        L.append("### 金額")
        L.append("")
        L.append("| 見積もり | 月額課金分 | ギフト | 合計（1ヶ月） | 年換算（月額課金分×12） |")
        L.append("|---|---:|---:|---:|---:|")
        if rev["single_tier"]:
            L.append(f"| 推定（メンバーシップは1種類） | {yen(rev['low'])} | {yen(rev['gift_once'])} "
                     f"| **{yen(rev['low'] + rev['gift_once'])}** | {yen(rev['low'] * 12)} |")
        else:
            L.append(f"| 下限（レベル不明の人を全員いちばん安いレベルとみなす） | {yen(rev['low'])} | {yen(rev['gift_once'])} "
                     f"| **{yen(rev['low'] + rev['gift_once'])}** | {yen(rev['low'] * 12)} |")
            L.append(f"| 推定（レベル不明の人を、わかった人の比率で割り振る） | {yen(rev['est'])} | {yen(rev['gift_once'])} "
                     f"| **{yen(rev['est'] + rev['gift_once'])}** | {yen(rev['est'] * 12)} |")
        L.append("")
        L.append(f"- 1人あたりの取り分 = 月額 ÷ (1 + 消費税 {cfg_all['tax_rate']:.0%}) × クリエイター取り分 {cfg_all['creator_share']:.0%}"
                 f"（最安レベル {yen(R['tiers_now'][0]['price'])} なら {yen(rev['net_lowest'])}）")
        if rev["gifted"]:
            L.append(f"- ギフト = {rev['gifted']} 人分 × {yen(rev['gift_price'])} の取り分")
        if R["known_total"]:
            L.append(f"- 比率はレベルがわかった {R['known_total']} 人から出しています。人数が少ないと比率がぶれやすいので、推定値は目安です。")
        L.append("- 事務所との分配、iOS アプリ経由の価格差、為替などは考慮していません。")
        if cfg_all.get("overseas"):
            L.append("- 海外チャンネルのため、料金は日本から見た表示価格（円）で計算しています。海外から加入したメンバーの実際の支払額とは異なります。")
        if cfg_all.get("source"):
            L.append(f"- 料金の出典: {cfg_all['source']}")
        L.append("")

    if cfg_all:
        hist = price_history(cfg_all)
        L.append("## 料金の履歴")
        L.append("")
        L.append("| 日付 | レベル | 月額 | 変更前 |")
        L.append("|---|---|---:|---:|")
        for h in hist:
            L.append(f"| {h['date'] or '（初回確認より前）'} | {md_cell(h['tier'])} | {yen(h['price']) if h['price'] is not None else '提供なし'} "
                     f"| {yen(h['before']) if h['before'] is not None else '–'} |")
        for c in cfg_all.get("changes", []):
            L.append(f"| {c.get('date', '')} | （メモ） | {md_cell(c.get('note', ''))} | |")
        L.append("")

    if history:
        L.append("## 月ごとの推移")
        L.append("")
        L.append("| 月 | 集計した日 | 配信 | 確認メンバー | 新規加入 | 推定収益（1ヶ月、ギフト込み） |")
        L.append("|---|---|---:|---:|---:|---:|")
        for h in history:
            r = h["revenue"]
            L.append(f"| {h['month']} | {h['coverage']['from'][5:]}〜{h['coverage']['to'][5:]} | {h['streams_analyzed']} "
                     f"| {h['members_confirmed']} 人 | {h['events']['new_members']} | "
                     f"{yen_range(r['monthly_low'] + r['gift_once'], r['monthly_est'] + r['gift_once']) if r else '–'} |")
        L.append("")
        L.append("> 月の途中からしかデータがない月は、集計した日の範囲が短くなっています。")
        L.append("")

    L.append("## バッジ（継続期間）の分布")
    L.append("")
    L.append("| バッジ | 人数 | 割合 | |")
    L.append("|---|---:|---:|---|")
    for r in R["dist_rows"]:
        bar = "█" * max(1, round(r["percent"] / 2.5)) if r["members"] else ""
        L.append(f"| {r['badge']} | {r['members']:,} 人 | {r['percent']:.1f}% | {bar} |")
    L.append("")
    L.append("> 期間中に確認できた最大のバッジで1人1回カウント。「6ヶ月」は「6ヶ月以上、次の段階未満」の意味です。")
    L.append("")

    L.append("## 加入・ギフトのイベント（配信中に流れたものだけ）")
    L.append("")
    L.append("| イベント | 件数 |")
    L.append("|---|---:|")
    L.append(f"| 新規加入 | {sum(r['new_members'] for r in rows):,} 件 |")
    L.append(f"| 再加入 / レベル変更 | {sum(r['rejoins'] for r in rows):,} 件 |")
    L.append(f"| 継続マイルストーン | {sum(r['milestones'] for r in rows):,} 件 |")
    L.append(f"| ギフト購入 | {sum(r['gift_purchases'] for r in rows):,} 件（計 {sum(r['gifted_total'] for r in rows):,} 人分） |")
    L.append(f"| ギフト受け取り | {sum(r['gift_redemptions'] for r in rows):,} 件 |")
    L.append("")

    freq = Counter(len(m["streams"]) for m in R["members"].values())
    L.append("## メンバーが何本の配信に出てきたか")
    L.append("")
    L.append("| 配信数 | 人数 |")
    L.append("|---|---:|")
    for k in sorted(k for k in freq if k <= 10):
        L.append(f"| {k} 本 | {freq[k]:,} 人 |")
    if any(k > 10 for k in freq):
        L.append(f"| 11本以上 | {sum(v for k, v in freq.items() if k > 10):,} 人 |")
    L.append("")

    L.append("## 配信ごとの集計")
    L.append("")
    L.append("| 日時 | 種類 | タイトル | 発言者 | うちメンバー | メンバー比率 | 新規加入 | ギフト |")
    L.append("|---|---|---|---:|---:|---:|---:|---:|")
    for r in rows:
        title = md_cell((r["title"] or r["video_id"])[:40])
        ratio = f"{r['member_ratio_pct']}%" if r["member_ratio_pct"] != "" else "-"
        L.append(f"| {r['start_jst']} | {r['kind']} | [{title}](https://www.youtube.com/watch?v={r['video_id']}) | {r['chatters']:,} "
                 f"| {r['member_chatters']:,} | {ratio} | {r['new_members']} | {r['gifted_total']} |")
    L.append("")

    if R["missed"]:
        L.append("## 集計できなかった配信")
        L.append("")
        L.append("| 日時 | タイトル | 理由 | 視聴できるレベル | 高評価数 |")
        L.append("|---|---|---|---|---:|")
        for v in R["missed"]:
            likes = f"{v['like_count']:,}" if isinstance(v.get("like_count"), int) else "-"
            lv = (v.get("members_level") or "メンバー全員") + " 以上" if v.get("members_only") else "–"
            if v.get("members_only") and not v.get("members_level"):
                lv = "不明（レベル表記なし）"
            L.append(f"| {v.get('start_jst', '?')} | [{md_cell((v.get('title') or v['id'])[:40])}]"
                     f"(https://www.youtube.com/watch?v={v['id']}) | {MISSED_REASON.get(v.get('status'), v.get('status'))} | {lv} | {likes} |")
        L.append("")
        L.append("> メンバー限定配信の高評価数は「その配信を見られるメンバーが少なくともこれだけいる」という目安になります。")
        L.append("")

    mo_videos = [v for v in R["members_only"] if v.get("status") == "members_only_video"]
    if mo_videos:
        L.append("## メンバー限定動画（参考）")
        L.append("")
        L.append("| 公開日 | タイトル | 視聴できるレベル | 高評価数 |")
        L.append("|---|---|---|---:|")
        for v in mo_videos:
            likes = f"{v['like_count']:,}" if isinstance(v.get("like_count"), int) else "-"
            L.append(f"| {v.get('start_jst', '?')[:10]} | [{md_cell((v.get('title') or v['id'])[:40])}](https://www.youtube.com/watch?v={v['id']}) "
                     f"| {(v.get('members_level') or '不明') + ' 以上'} | {likes} |")
        L.append("")

    L.append("## 出力ファイル")
    L.append("")
    L.append("| ファイル | 内容 |")
    L.append("|---|---|")
    L.append("| members.csv | メンバー1人1行（名前を含むため非公開用） |")
    if R["tiers_now"]:
        L.append("| tiers.csv | レベルごとの人数と推定月収 |")
    L.append("| badge_distribution.csv | バッジ（継続月数）ごとの人数 |")
    L.append("| streams.csv | 配信ごとの発言者数・メンバー数・加入・ギフト |")
    L.append("| events.csv | 加入・レベル変更・マイルストーン・ギフトの一覧（名前を含むため非公開用） |")
    L.append("| summary.json / monthly/*.json | Webサイト用（名前・IDは含まない） |")
    summary = "\n".join(L) + "\n"
    (report_dir / "summary.md").write_text(summary, encoding="utf-8")
    return summary


LOOKBACK_DAYS = 180


def main():
    global LOOKBACK_DAYS
    p = argparse.ArgumentParser(description="チャットリプレイからメンバーバッジ・レベルを集計し、推定収益を計算")
    p.add_argument("data_dir", help="fetch_chats.py の保存先 (例: data/チャンネル名)")
    p.add_argument("--report", default=None, help="出力先 (既定: <data_dir>/report)")
    p.add_argument("--tiers", default=None, help="料金設定ファイル (既定: <data_dir>/tiers.json)")
    p.add_argument("--days", type=int, default=30, help="集計する日数 (既定: 30)")
    p.add_argument("--end", default=None, help="集計の最終日 YYYY-MM-DD (既定: 最後にデータを取得した日時)")
    p.add_argument("--tier-lookback-days", type=int, default=LOOKBACK_DAYS,
                   help=f"レベルの手がかりに使うイベントを何日前までさかのぼるか (既定: {LOOKBACK_DAYS})")
    p.add_argument("--no-monthly", action="store_true", help="月ごとの集計を作らない")
    p.add_argument("--raw-keep-hours", type=float, default=24,
                   help="取得から何時間たった元のチャットファイルを消すか (既定: 24。0 ならすぐ消す)")
    p.add_argument("--keep-raw", action="store_true", help="元のチャットファイルを消さない")
    p.add_argument("--channels", default="channels.csv",
                   help="チャンネル一覧 (既定: channels.csv)。country が JP 以外なら海外チャンネルとして注記する")
    args = p.parse_args()
    LOOKBACK_DAYS = args.tier_lookback_days

    data_dir = Path(args.data_dir)
    if not (data_dir / "chats").exists() and not (data_dir / "records").exists():
        sys.exit(f"{data_dir} にデータがありません。先に fetch_chats.py を実行してください。")
    report_dir = Path(args.report) if args.report else data_dir / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    tiers_path = Path(args.tiers) if args.tiers else data_dir / "tiers.json"

    meta = {"videos": {}}
    if (data_dir / "videos.json").exists():
        meta = json.loads((data_dir / "videos.json").read_text(encoding="utf-8"))
    videos = meta.get("videos", {})
    ch = meta.get("channel", data_dir.name)

    records = load_records(data_dir, videos, None if args.keep_raw else args.raw_keep_hours)

    found_all = {e["tier"] for r in records for e in r["events"] if e.get("tier")}
    cfg_all = load_tier_config(tiers_path, found_all)
    if cfg_all:
        # 海外チャンネルかどうかは channels.csv の country で決める (tiers.json の overseas でも指定可)
        info = channel_map(args.channels).get(meta.get("channel_id") or data_dir.name, {})
        cfg_all["overseas"] = bool(cfg_all.get("overseas") or info.get("overseas"))

    # ---- 直近N日
    if args.end:
        w_to = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=JST).timestamp() + 86400
    else:
        w_to = meta.get("fetched_at_ts") or (max(r["start_ts"] for r in records) + 1 if records else time.time())
    w_from = w_to - args.days * 86400
    R = aggregate(records, w_from, w_to, cfg_all, videos, LOOKBACK_DAYS)
    if not R["records"]:
        # 配信していない・アーカイブを残していないチャンネルもあるので、エラーにせず「配信なし」として0人で出力する
        print(f"配信なし: {fmt_ts(w_from)} 〜 {fmt_ts(w_to)} に集計できる配信がありません (0人として出力します)")

    # ---- 月ごと
    history = []
    if not args.no_monthly and records:
        mdir = report_dir / "monthly"
        mdir.mkdir(exist_ok=True)
        first_ts = min(r["start_ts"] for r in records)
        months = sorted({datetime.fromtimestamp(r["start_ts"], JST).strftime("%Y-%m") for r in records})
        now = time.time()
        for mon in months:
            mp = mdir / f"{mon}.json"
            if mp.exists():
                old = json.loads(mp.read_text(encoding="utf-8"))
                if old.get("finalized"):
                    history.append(old)   # 確定済みの月は作り直さない
                    continue
            m_from, m_to = month_bounds(mon)
            cov_from = max(m_from, first_ts if first_ts > m_from + 86400 else m_from)
            cov_to = min(m_to, meta.get("fetched_at_ts") or m_to)
            MR = aggregate(records, m_from, m_to, cfg_all, videos, LOOKBACK_DAYS)
            ms = monthly_summary(MR, mon, cov_from, cov_to)
            ms["partial"] = cov_from > m_from or cov_to < m_to
            ms["finalized"] = now >= m_to + FINALIZE_DAYS * 86400
            if ms["finalized"]:
                ms["finalized_at"] = fmt_ts(now)
            mp.write_text(json.dumps(ms, ensure_ascii=False, indent=1), encoding="utf-8")
            history.append(ms)

    # ---- 古い月の records をまとめて、非メンバーのIDを捨てる
    compact_records(data_dir, records, time.time())

    # ---- CSV
    class_label = {"known": "", "unknown": "", "gift": "ギフトで加入"}
    member_rows = []
    for m in sorted(R["members"].values(), key=lambda x: (-(x["max_months"] or -1), -len(x["streams"]))):
        member_rows.append({
            "channel_id": m["channel_id"], "name": m["name"],
            "badge_months": "" if m["max_months"] is None else m["max_months"],
            "badge": months_label(m["max_months"]), "badge_tooltip": m["badge_label"],
            "milestone_months": "" if m["milestone_months"] is None else m["milestone_months"],
            "tier": m["tier"] or "不明", "tier_source": m["tier_source"], "revenue_note": class_label[m["class"]],
            "streams_seen": len(m["streams"]), "messages": m["messages"], "superchats": m["superchats"],
            "first_seen": fmt_ts(m["first_seen_ts"]), "last_seen": fmt_ts(m["last_seen_ts"]),
            "joined_in_period": "○" if m["joined_in_period"] else "",
            "gift_received": "○" if m["gift_received"] else "",
            "url": f"https://www.youtube.com/channel/{m['channel_id']}",
        })
    write_csv(report_dir / "members.csv", member_rows, list(member_rows[0].keys()) if member_rows else ["channel_id"])
    write_csv(report_dir / "badge_distribution.csv", R["dist_rows"], ["badge_months", "badge", "members", "percent"])
    write_csv(report_dir / "streams.csv", R["stream_rows"],
              list(R["stream_rows"][0].keys()) if R["stream_rows"] else ["video_id", "start_jst", "title"])
    write_csv(report_dir / "events.csv", R["events"],
              ["stream_jst", "video_id", "offset_ms", "type", "tier", "name", "channel_id", "value", "text"])
    if R["tier_rows"]:
        write_csv(report_dir / "tiers.csv", R["tier_rows"], list(R["tier_rows"][0].keys()))

    # ---- summary.json / summary.md
    sj = public_summary(R, meta, cfg_all, ch)
    sj["history"] = history
    sj["no_streams"] = not R["records"]
    # メンバーシップの有無 (price_check.py が tiers.json に記録。false = メンバーシップなし、null = 未確認)
    raw_tiers = json.loads(tiers_path.read_text(encoding="utf-8")) if tiers_path.exists() else {}
    sj["membership"] = raw_tiers.get("membership")
    sj["price_history"] = price_history(cfg_all) if cfg_all else []
    sj["price_notes"] = (cfg_all or {}).get("changes", [])
    (report_dir / "summary.json").write_text(json.dumps(sj, ensure_ascii=False, indent=1), encoding="utf-8")
    summary = write_markdown(R, meta, cfg_all, ch, tiers_path, history, report_dir)
    old = report_dir / "summary.txt"
    if old.exists():
        old.unlink()   # 旧形式のまとめは残すと紛らわしいので消す

    if not cfg_all:
        print(f"\n※ 料金が未設定です。{tiers_path} に月額を書き込んで再実行すると収益も計算します。")
    print("\n" + summary)
    print(f"出力先: {report_dir}")


if __name__ == "__main__":
    main()
