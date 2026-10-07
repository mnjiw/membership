"""
fetch_chats.py
YouTubeチャンネルの過去N日間(デフォルト30日)のライブ配信とプレミア公開について、
チャットリプレイ(live_chat.json)をまとめてダウンロードする。

使い方:
    python fetch_chats.py @チャンネルハンドル                     # 初回は過去30日、2回目以降は直近7日を確認
    python fetch_chats.py https://www.youtube.com/@xxxx --days 14
    python fetch_chats.py @xxxx --cookies-from-browser firefox   # メンバー限定配信も取る場合

保存先 (デフォルト):
    data/<チャンネルID>/chats/<動画ID>.live_chat.json
    data/<チャンネルID>/videos.json   ← 配信のメタ情報と取得状況
    (チャンネル名を変えられても同じフォルダに保存されるよう、フォルダ名はチャンネルID)

一度結論が出た配信 (保存済み・チャットなし・メンバー限定など) は次回から飛ばすので、
何度実行しても新しい配信だけを確認する。配信中だった枠や取得に失敗した枠は次回また確認する。
"""

import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import yt_dlp
except ImportError:
    sys.exit("yt-dlp が入っていません。  pip install -U yt-dlp  を実行してください。")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

JST = timezone(timedelta(hours=9))
FIRST_DAYS = 30   # 初めて取得するチャンネルは、直近30日の集計が埋まるよう30日分を確認する
DAILY_DAYS = 7    # 2回目以降は直近7日を確認する (1日止まっても取りこぼさないように)

# 一度この結論になった枠は次回から確認しない
FINAL_STATUSES = {"ok", "skip_old", "skip_not_stream", "no_chat", "members_only",
                  "became_members_only", "members_only_video"}

STATUS_LABEL = {
    "ok": "保存",
    "skip_old": "期間外",
    "skip_not_stream": "配信・プレミアではない",
    "no_chat": "チャットリプレイなし (アーカイブ編集・チャット無効の可能性)",
    "members_only": "メンバー限定のため取得不可",
    "became_members_only": "配信後にメンバー限定に変更されたため取得不可",
    "members_only_video": "メンバー限定動画 (配信かどうか不明)",
    "processing": "アーカイブ処理中 (次回また確認)",
    "chat_failed": "チャット取得失敗 (次回また確認)",
    "live_now": "配信中 (次回また確認)",
    "error": "エラー (次回また確認)",
}


# ---------------------------------------------------------------------------
# 引数
# ---------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="過去N日間の配信・プレミア公開のチャットリプレイを一括ダウンロード")
    p.add_argument("channel", help="チャンネルのURL / @ハンドル / チャンネルID(UC...)")
    p.add_argument("--days", type=float, default=None,
                   help=f"何日前までの配信を対象にするか (既定: 初回 {FIRST_DAYS} 日、2回目以降 {DAILY_DAYS} 日)")
    p.add_argument("--hours", type=float, default=None, help="何時間前までを対象にするか (指定すると --days より優先)")
    p.add_argument("--out", default=None, help="保存先フォルダ (既定: data/<チャンネルID>)")
    p.add_argument("--max-scan", type=int, default=150,
                   help="各タブを最大何件までさかのぼって調べるか (既定: 150)")
    p.add_argument("--no-premieres", action="store_true", help="動画タブ (プレミア公開) を調べない")
    p.add_argument("--cookies-from-browser", default=None, metavar="BROWSER",
                   help="ブラウザのログイン情報を使う (firefox / chrome / edge など)。メンバー限定配信の取得に必要")
    p.add_argument("--cookies", default=None, metavar="FILE", help="cookies.txt を使う場合のパス")
    p.add_argument("--sleep", type=float, default=2.0, help="配信ごとの待ち時間(秒) (既定: 2)")
    return p.parse_args()


def channel_base_url(s: str) -> str:
    s = s.strip()
    if s.startswith("http"):
        return re.sub(r"/(videos|streams|shorts|featured|live|community|playlists)/?$", "", s.rstrip("/"))
    if s.startswith("@"):
        return f"https://www.youtube.com/{s}"
    if s.startswith("UC") and len(s) == 24:
        return f"https://www.youtube.com/channel/{s}"
    return f"https://www.youtube.com/@{s}"


def safe_dirname(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_") or "channel"


def base_ydl_opts(args) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,                # 進捗表示でログが埋まらないようにする
        "ignoreerrors": False,
        "ignore_no_formats_error": True,   # 動画本体は落とさないので形式エラーは無視
        "skip_download": True,
        "retries": 5,
        "fragment_retries": 10,
    }
    if args.cookies_from_browser:
        opts["cookiesfrombrowser"] = (args.cookies_from_browser,)
    if args.cookies:
        opts["cookiefile"] = args.cookies
    return opts


def fmt_ts(ts) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# メンバー限定コンテンツの「どのレベル以上が見られるか」
# ---------------------------------------------------------------------------
LEVEL_RES = [
    re.compile(r"レベル\s*(.+?)\s*以上のメンバー"),
    re.compile(r"members on level:?\s*(.+?)\s*\(or any higher level\)", re.I),
]


def level_from_text(text: str) -> str:
    for rx in LEVEL_RES:
        m = rx.search(text or "")
        if m:
            return m.group(1).strip()
    return ""


def members_only_level(video_id: str) -> str:
    """ログインなしで視聴ページを開き、「この動画は、このチャンネルのレベル level2 以上のメンバーが対象です」から
    レベル名を取り出す。取れなければ空文字 (レベルが1種類しかないチャンネルはレベル名が出ないこともある)"""
    req = urllib.request.Request(
        f"https://www.youtube.com/watch?v={video_id}&hl=ja&gl=JP",
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36",
                 "Accept-Language": "ja-JP"})
    try:
        html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    except Exception:
        return ""
    m = re.search(r'"playabilityStatus":\{"status":"[A-Z_]+","reason":"([^"]+)"', html)
    return level_from_text(m.group(1) if m else html)


# ---------------------------------------------------------------------------
# 1. ライブタブ・動画タブから候補を列挙 (おおよその日付で粗く絞る)
# ---------------------------------------------------------------------------
def list_tab(args, url: str, rough_cutoff: float):
    opts = base_ydl_opts(args)
    opts.update({
        "extract_flat": "in_playlist",
        "playlistend": args.max_scan,
        # 「3日前」などの表記からおおよその日時を計算させる
        "extractor_args": {"youtubetab": {"approximate_date": [""]}},
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as e:
            if "does not have a" in str(e):   # 動画タブ・ライブタブがないチャンネル
                return None, []
            raise
    entries = []
    for e in info.get("entries") or []:
        if not e or not e.get("id"):
            continue
        ts = e.get("timestamp")
        if ts is not None and ts < rough_cutoff:
            break  # タブは新しい順なので、ここから先は全部古い
        entries.append(e)
    return info, entries


def rough_margin(cutoff_ts: float) -> float:
    # 「1か月前」「2日前」などは粒度が粗いので、余裕を持たせて取りこぼしを防ぐ
    return 14 * 86400 if (time.time() - cutoff_ts) > 8 * 86400 else 2 * 86400


def list_candidates(args, cutoff_ts: float):
    base = channel_base_url(args.channel)
    rough_cutoff = cutoff_ts - rough_margin(cutoff_ts)
    tabs = [("live", base + "/streams")]
    if not args.no_premieres:
        tabs.append(("video", base + "/videos"))

    info0, candidates, live_now, seen = None, [], [], set()
    for kind, url in tabs:
        print(f"[1/2] 一覧を取得中: {url}")
        info, entries = list_tab(args, url, rough_cutoff)
        info0 = info0 or info
        for e in entries:
            if e["id"] in seen:
                continue
            seen.add(e["id"])
            status = e.get("live_status")
            item = {"id": e["id"], "title": e.get("title"), "approx_ts": e.get("timestamp"), "tab": kind,
                    "members_only": e.get("availability") == "subscriber_only"}
            if status == "is_live":
                live_now.append(item)   # 配信中は取らない。公開状態だけ記録して次回確認する
            elif status != "is_upcoming":
                candidates.append(item)
    if info0 is None:
        sys.exit("チャンネルが見つかりません。")
    channel = {
        "channel": info0.get("channel") or info0.get("uploader") or info0.get("title") or "channel",
        "channel_id": info0.get("channel_id") or info0.get("id"),
        "handle": info0.get("uploader_id") or "",              # 例: @LainPaterson
        "subscribers": info0.get("channel_follower_count"),    # 登録者数 (取得時点・概数)
    }
    return channel, candidates, live_now


# ---------------------------------------------------------------------------
# 2. 各枠を詳しく調べ、期間内の配信・プレミア公開ならチャットを保存
# ---------------------------------------------------------------------------
def fetch_one(args, video_id: str, chats_dir: Path, cutoff_ts: float, prev: dict):
    opts = base_ydl_opts(args)
    opts.update({
        "writesubtitles": True,
        "subtitleslangs": ["live_chat"],
        "outtmpl": {"default": str(chats_dir / "%(id)s.%(ext)s")},
    })
    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        start_ts = info.get("release_timestamp") or info.get("timestamp")
        if not start_ts and info.get("upload_date"):
            start_ts = datetime.strptime(info["upload_date"], "%Y%m%d").replace(tzinfo=JST).timestamp()

        live_status = info.get("live_status")
        has_chat = "live_chat" in (info.get("subtitles") or {})
        members_only = info.get("availability") == "subscriber_only"
        if live_status in ("was_live", "post_live", "is_live"):
            kind = "live"
        elif has_chat and info.get("release_timestamp"):
            kind = "premiere"     # プレミア公開は not_live だがチャットリプレイがある
        else:
            kind = "video"
        meta = {
            "id": video_id,
            "title": info.get("title"),
            "kind": kind,
            "start_ts": start_ts,
            "start_jst": fmt_ts(start_ts),
            "duration_sec": info.get("duration"),
            "view_count": info.get("view_count"),
            "like_count": info.get("like_count"),
            "live_status": live_status,
            "members_only": members_only,
            "has_chat": has_chat,
            "checked_at": fmt_ts(time.time()),
        }
        if prev.get("seen_live_at"):
            meta["seen_live_at"] = prev["seen_live_at"]
            meta["was_public_when_live"] = prev.get("was_public_when_live")
        if start_ts and start_ts < cutoff_ts:
            return meta, "skip_old"
        if live_status == "is_live":
            return meta, "live_now"
        if members_only and not has_chat:
            meta["members_level"] = members_only_level(video_id)
            if kind == "video":
                return meta, "members_only_video"
            if prev.get("was_public_when_live"):
                return meta, "became_members_only"
            return meta, "members_only"
        if kind == "video":
            return meta, "skip_not_stream"
        if not has_chat:
            # 配信直後はアーカイブの処理が終わっておらずチャットがまだないことがある
            return meta, "processing" if live_status == "post_live" else "no_chat"

        ydl.process_ie_result(info, download=True)  # live_chat.json を書き出す
    # yt-dlp はチャットの取得失敗をエラーにせず警告だけで済ませることがあるので、ファイルの有無で確認する
    chat_file = chats_dir / f"{video_id}.live_chat.json"
    if not chat_file.exists() or chat_file.stat().st_size == 0:
        return meta, "chat_failed"
    return meta, "ok"


def recorded_ids(out_dir: Path) -> set:
    """records に変換済みの動画ID (元のチャットファイルは24時間で消えるので、こちらで「取得済み」を判定する)"""
    rec_dir = out_dir / "records"
    ids = {p.stem for p in rec_dir.glob("*.json")} if rec_dir.exists() else set()
    for pp in rec_dir.glob("*.jsonl") if rec_dir.exists() else []:
        with pp.open(encoding="utf-8") as f:
            ids.update(json.loads(line)["video_id"] for line in f if line.strip())
    return ids


class PartFileLocked(Exception):
    pass


def remove_parts(chats_dir: Path, vid: str) -> bool:
    """途中ファイル (.part) を消す。消せなかったら False"""
    ok = True
    for part in chats_dir.glob(f"{vid}.live_chat.json.part*"):
        try:
            part.unlink(missing_ok=True)
        except OSError:
            ok = False
    return ok


MEMBERS_ONLY_STATUSES = {"members_only", "became_members_only", "members_only_video"}
LIKES_REFRESH_DAYS = 35   # メンバー限定コンテンツの高評価数を取り直す期間 (直近30日の集計に使うため)


def refresh_members_only(args, store: dict, now: float) -> int:
    """直近のメンバー限定コンテンツの高評価数・再生数を取り直す (高評価数は公開後も増えていくため)。
    高評価数は「そのレベル以上のメンバーが少なくともこれだけいる」という推定の手がかりに使う。"""
    n = 0
    for vid, v in store["videos"].items():
        if v.get("status") not in MEMBERS_ONLY_STATUSES or not v.get("start_ts"):
            continue
        if v["start_ts"] < now - LIKES_REFRESH_DAYS * 86400:
            continue
        last = v.get("likes_checked_ts") or 0
        if now - last < 20 * 3600:   # 1日1回で十分
            continue
        try:
            with yt_dlp.YoutubeDL(base_ydl_opts(args)) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
        except Exception:
            continue   # メンバー限定動画はログインなしでは情報が取れないことがある
        if isinstance(info.get("like_count"), int):
            v["like_count"] = info["like_count"]
        if isinstance(info.get("view_count"), int):
            v["view_count"] = info["view_count"]
        v["likes_checked_ts"] = now
        v["likes_checked_at"] = fmt_ts(now)
        n += 1
        time.sleep(args.sleep)
    return n


def is_settled(vid: str, prev: dict, chat_file: Path, cutoff_ts: float, recorded: set) -> bool:
    """前回までに結論が出ていて、もう確認しなくてよい枠か"""
    st = prev.get("status")
    if vid in recorded or (chat_file.exists() and st == "ok"):
        return True   # チャットを取得済み (records に変換済み、または元ファイルがある)
    if st not in FINAL_STATUSES:
        return False
    if st == "ok":
        return False  # 「保存」なのに records も元ファイルもない → 取り直す
    if st == "skip_old":
        # 対象期間を広げて実行したときは、前回「期間外」だった枠も見直す
        return not (prev.get("start_ts") and prev["start_ts"] >= cutoff_ts)
    if st == "no_chat" and prev.get("members_only"):
        return False   # 旧版で「チャットなし」と記録したメンバー限定配信は、レベルを調べ直す
    return True


def main():
    args = parse_args()
    now = time.time()
    # 一覧は長めの範囲で取り、保存先 (= 初回かどうか) がわかってから対象期間を決める
    if args.hours:
        span = args.hours * 3600
    elif args.days:
        span = args.days * 86400
    else:
        span = None
    try:
        channel, candidates, live_now = list_candidates(args, now - (span or FIRST_DAYS * 86400))
    except yt_dlp.utils.DownloadError as e:
        # 一覧すら取れないときは、YouTube にアクセスを止められている可能性が高い。
        # videos.json には何も書かずに終わる (サイトの数値は前回のまま残る)
        print(f"一覧を取得できませんでした: {str(e).splitlines()[0][:200]}")
        print("※「Sign in to confirm you're not a bot」などなら、YouTube にアクセスを制限されています。時間をおいて再実行してください。")
        sys.exit(2)
    out_dir = Path(args.out) if args.out else Path("data") / safe_dirname(channel["channel_id"])
    meta_path = out_dir / "videos.json"
    store = {"videos": {}}
    if meta_path.exists():
        store = json.loads(meta_path.read_text(encoding="utf-8"))
        store.setdefault("videos", {})
    if span is None:
        # 初回の30日分の取得を最後までやり終えていなければ (途中で止まった場合も) 30日分、終えていれば7日分
        span = (DAILY_DAYS if store.get("initial_fetch_done") else FIRST_DAYS) * 86400
    cutoff_ts = now - span
    candidates = [c for c in candidates if c["approx_ts"] is None or c["approx_ts"] >= cutoff_ts - rough_margin(cutoff_ts)]
    chats_dir = out_dir / "chats"
    chats_dir.mkdir(parents=True, exist_ok=True)

    store.update(channel)
    store["fetched_at"] = fmt_ts(now)
    store["fetched_at_ts"] = now
    store["checked_from"] = fmt_ts(cutoff_ts)

    def save():
        meta_path.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")

    # 配信中の枠は取らずに、その時点で公開だったかどうかだけ記録する (配信後のメンバー限定化を見分けるため)
    for c in live_now:
        v = store["videos"].setdefault(c["id"], {"id": c["id"], "title": c["title"], "status": "live_now"})
        v.setdefault("seen_live_at", fmt_ts(now))
        v.setdefault("was_public_when_live", not c["members_only"])
        print(f"  配信中のため今回は取得しません: {c['id']} {(c['title'] or '')[:40]}")

    print(f"  チャンネル: {channel['channel']}  /  候補 {len(candidates)} 件 (対象: {fmt_ts(cutoff_ts)} 以降)")
    print(f"[2/2] チャットリプレイを取得中 → {chats_dir}")

    counts = {"ok": 0, "cached": 0, "skip": 0, "retry": 0}
    recorded = recorded_ids(out_dir)
    for i, c in enumerate(candidates, 1):
        vid = c["id"]
        head = f"  ({i}/{len(candidates)}) {vid} {(c['title'] or '')[:40]}"
        prev = store["videos"].get(vid) or {}
        chat_file = chats_dir / f"{vid}.live_chat.json"
        if is_settled(vid, prev, chat_file, cutoff_ts, recorded):
            counts["cached"] += 1
            continue   # 結論が出ている枠は確認しない (ログも出さない)
        try:
            for attempt in range(3):
                # 前回の中断で残った途中ファイルがあると壊れたまま再開されることがあるので消す。
                # Windows では、失敗した直後の途中ファイルを yt-dlp がまだ開いていて消せないことがある。
                # その場合はこの枠を「次回また確認」にして先へ進む (次回は別のプロセスなので消せる)
                if not remove_parts(chats_dir, vid):
                    raise PartFileLocked()
                meta, status = fetch_one(args, vid, chats_dir, cutoff_ts, prev)
                if status != "chat_failed":
                    break
                if attempt < 2:
                    print(f"{head}  … チャット取得に失敗、再試行します ({attempt + 1}/2)")
                    time.sleep(args.sleep * 3)
        except PartFileLocked:
            print(f"{head}  … 途中ファイルが使用中のため、次回また取得します")
            store["videos"][vid] = {**prev, "id": vid, "title": c["title"], "status": "chat_failed",
                                    "error": "途中ファイルが使用中で削除できなかった"}
            counts["retry"] += 1
            save()
            continue
        except Exception as e:   # 1枠の想定外のエラーで、チャンネル全体を止めない
            if not isinstance(e, yt_dlp.utils.DownloadError):
                msg = f"{type(e).__name__}: {e}"
                print(f"{head}  … 想定外のエラー (次回また確認): {msg[:150]}")
                store["videos"][vid] = {**prev, "id": vid, "title": c["title"], "status": "error", "error": msg[:300]}
                counts["retry"] += 1
                save()
                continue
            msg = str(e).splitlines()[0]
            level = level_from_text(msg)
            if level or "members" in msg.lower():
                status = "members_only_video" if c["tab"] == "video" else "members_only"
                meta = {"id": vid, "title": c["title"], "kind": c["tab"], "members_only": True,
                        "members_level": level or members_only_level(vid), "start_ts": c["approx_ts"],
                        "start_jst": fmt_ts(c["approx_ts"]), "checked_at": fmt_ts(time.time())}
            else:
                print(f"{head}  … 失敗: {msg[:120]}")
                store["videos"][vid] = {**prev, "id": vid, "title": c["title"], "status": "error", "error": msg[:300]}
                counts["retry"] += 1
                save()
                continue

        meta["status"] = status
        store["videos"][vid] = meta
        label = STATUS_LABEL[status]
        if status in ("members_only", "became_members_only", "members_only_video") and meta.get("members_level"):
            label += f" [{meta['members_level']} 以上]"
        if c["tab"] == "video" and status == "skip_not_stream":
            counts["skip"] += 1   # 動画タブの普通の動画はログを出さない
        else:
            kind = {"premiere": "プレミア", "live": "配信"}.get(meta.get("kind"), "")
            print(f"{head}  [{meta['start_jst']}] {kind} … {label}")
            if status == "ok":
                counts["ok"] += 1
            elif status in FINAL_STATUSES:
                counts["skip"] += 1
            else:
                counts["retry"] += 1
        save()
        time.sleep(args.sleep)

    refreshed = refresh_members_only(args, store, now)
    if refreshed:
        print(f"  メンバー限定コンテンツ {refreshed} 本の高評価数を更新しました")
    if span >= FIRST_DAYS * 86400 - 60:
        store["initial_fetch_done"] = True   # 30日分を最後まで確認できた
    save()
    print(f"\n完了: 新規 {counts['ok']} / 確認済みで省略 {counts['cached']} / 対象外 {counts['skip']} / 次回再確認 {counts['retry']}")
    print(f"次は集計:  python analyze_badges.py \"{out_dir}\"")


if __name__ == "__main__":
    main()
