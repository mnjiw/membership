"""
run_daily.py
channels.csv の全チャンネルについて「取得 → 集計」を並列で行い、最後にサイトを作る (毎日の更新用)。
GitHub Actions からも、手元のPCからも同じように動く。

使い方:
    python run_daily.py                          # 全チャンネル (初回取得がまだのチャンネルは飛ばす)
    python run_daily.py --only UCxxxx UCyyyy     # 指定したチャンネルだけ (テスト用)
    python run_daily.py --limit 3                # 先頭から3チャンネルだけ (テスト用)
    python run_daily.py --groups にじさんじ       # グループ名がこれで始まるチャンネルだけ (毎朝の分割実行)
    python run_daily.py --groups '!にじさんじ'    # それ以外のチャンネルだけ
    python run_daily.py --workers 4              # 同時に処理するチャンネル数 (既定: 3)
    python run_daily.py --new-only               # 初回取得がまだのチャンネルだけ、過去30日分を取得する (手元で使う)
                                                 #   途中で止めても、もう一度同じコマンドで続きから再開する
    python run_daily.py --initial --only UCxxxx  # 指定したチャンネルの初回取得

結果:
    logs/run_<日時>.md に、チャンネルごとの結果 (新規取得数・エラー) を書く。
    GitHub Actions では、同じ内容を実行結果のページ (Summary) にも出し、エラーは注釈 (::error::) にする。
    エラーがあったときは終了コード 1 で終わる → GitHub Actions の実行が「失敗」になり、メールで通知される。
    その日2回目以降の取得 (予備の実行など) で、時刻のほかに何も変わらなかったチャンネルは、
    videos.json を取得前のまま戻して集計もしない (GitHub の履歴が無駄に増えないようにするため)。

エラーの扱い:
    ブロック   … チャンネルの一覧すら取れない (fetch_chats.py が終了コード 2)。YouTube にアクセスを止められた可能性
    取得エラー … 一部の配信でチャットを取れなかった (直近30日の集計に入る間は、次回以降も自動で再挑戦する)
    異常終了   … プログラム自体のエラー

毎回の点検 (異常があれば終了コード 1 → GitHub Actions の実行が「失敗」になり、メールで通知される):
    取りこぼし   … チャンネルの一覧 (直近30日) に出ているのに、記録されていない枠がある
    記録の矛盾   … 「保存」なのにチャットの記録がない、開始時刻がない・日付だけ、記録があるのに「保存」以外
    再確認待ち   … 取得エラー・配信中・アーカイブ処理中のまま7日を超えた枠 (直近30日の分)
    取得数       … その日最初の実行 (--groups の範囲ごと) で、新しく取得した配信が10本未満
    手動で動かすとき、--test-alert をつけると点検の異常を1件わざと出す (メールが届くかの確認用)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from channel_list import load_channels

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

JST = timezone(timedelta(hours=9))
HERE = Path(__file__).resolve().parent
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
BLOCK_HINTS = ("Sign in to confirm", "not a bot", "HTTP Error 429", "Too Many Requests")
NOT_FOUND_HINTS = ("does not exist", "HTTP Error 404", "This channel is not available", "has been terminated")
RETRY_STATUSES = {"error", "chat_failed"}
PENDING_ALERT_DAYS = 7    # 再確認待ちがこの日数を超えたら異常とする
MIN_NEW_FIRST_RUN = 10    # その日最初の実行 (--groups の範囲ごと) で、新しく取得した配信がこれ未満なら異常とする
VOLATILE_KEYS = ("fetched_at", "fetched_at_ts", "checked_from")   # 取得のたびに変わる時刻 (中身の変化ではない)
PARTIAL_MARK = "- 対象: 一部のチャンネル"   # 一部のチャンネルだけの実行 (テスト・手動) のレポートにつける
GROUPS_MARK = "- 対象グループ: "   # --groups で範囲を決めた実行 (毎朝の分割実行) のレポートにつける


def run(cmd, timeout):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        p = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env)
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        return -9, out + f"\n時間切れ ({timeout} 秒)"


def initial_done(cid: str) -> bool:
    """初回 (30日分) の取得を最後までやり終えたか。途中で止まったチャンネルは False"""
    vp = HERE / "data" / cid / "videos.json"
    return vp.exists() and bool(json.loads(vp.read_text(encoding="utf-8")).get("initial_fetch_done"))


def snapshot(data_dir: Path):
    """取得の前の状態: videos.json の中身と、元のチャットファイルの一覧"""
    vp, chats = data_dir / "videos.json", data_dir / "chats"
    return (vp.read_bytes() if vp.exists() else None,
            {p.name for p in chats.glob("*.live_chat.json")} if chats.exists() else set())


def keep_if_unchanged(data_dir: Path, before) -> bool:
    """今日すでに取得・集計したチャンネルで、今回の取得で時刻のほかに何も変わらなかったら、
    videos.json を取得前のまま戻して True を返す (集計とサイトのデータも書き換えずに済み、GitHub の履歴が増えない)。
    その日最初の取得では、集計期間 (直近30日) を進めるために必ず書き換える"""
    old_bytes, old_chats = before
    vp, sp = data_dir / "videos.json", data_dir / "report" / "summary.json"
    if old_bytes is None or not vp.exists() or not sp.exists():
        return False
    old = json.loads(old_bytes.decode("utf-8"))
    if not str(old.get("fetched_at", "")).startswith(datetime.now(JST).strftime("%Y-%m-%d")):
        return False   # 今日最初の取得
    if json.loads(sp.read_text(encoding="utf-8")).get("fetched_at") != old.get("fetched_at"):
        return False   # 集計が取得前の状態と合っていない
    new = json.loads(vp.read_text(encoding="utf-8"))
    strip = lambda s: {k: v for k, v in s.items() if k not in VOLATILE_KEYS}
    if strip(old) != strip(new) or snapshot(data_dir)[1] != old_chats:
        return False
    vp.write_bytes(old_bytes)
    return True


def process(ch: dict, args) -> dict:
    """1チャンネル分: 取得 → 集計"""
    cid = ch["channel_id"]
    data_dir = HERE / "data" / cid
    name = ch["name"] or ch["channel_title"]
    if not name and (data_dir / "videos.json").exists():
        name = json.loads((data_dir / "videos.json").read_text(encoding="utf-8")).get("channel", "")
    res = {"id": cid, "name": name or cid, "status": "ok", "new": 0,
           "retry": 0, "errors": [], "seconds": 0}
    t0 = time.time()
    is_new = not (data_dir / "videos.json").exists()   # 一度も取得していない
    if is_new and not args.initial:
        res["status"] = "skipped"
        res["errors"].append("初回取得がまだです (手元で `python run_daily.py --initial --only " + cid + "` を実行してください)")
        return res

    before = snapshot(data_dir)
    if not args.skip_fetch:
        code, out = run([sys.executable, "fetch_chats.py", cid, "--sleep", str(args.sleep)], args.timeout)
        lm = re.search(r"⚠ (\d+) 枠で、YouTube から欠けた情報", out)
        res["limited"] = int(lm.group(1)) if lm else 0
        res["warned"] = "⚠ YouTube の制限に関する警告" in out
        gm = re.search(r"⚠ 取りこぼし: .*?(\d+) 件 \((.*)\)", out)
        res["gaps"] = (int(gm.group(1)), gm.group(2)) if gm else None
        m = re.search(r"完了: 新規 (\d+) / 確認済みで省略 (\d+) / 対象外 (\d+) / 次回再確認 (\d+)", out)
        if m:
            res["new"], res["retry"] = int(m.group(1)), int(m.group(4))
        failed_lines = [ln.strip() for ln in out.splitlines() if "… 失敗:" in ln or "一覧を取得できませんでした" in ln]
        blocked = code == 2 or any(h in out for h in BLOCK_HINTS)
        if code == 2 and any(h in out for h in NOT_FOUND_HINTS):
            res["status"] = "not_found"
            res["errors"].append("チャンネルが見つかりません (IDの誤り、またはチャンネルが削除・非公開になった可能性)")
        elif code == 2:
            res["status"] = "blocked"
            res["errors"].append("チャンネルの一覧を取得できませんでした (YouTube にアクセスを止められた可能性)")
        elif code != 0:
            res["status"] = "crashed"
            res["errors"].append(f"fetch_chats.py が異常終了しました (終了コード {code}): " + out.strip().splitlines()[-1][:200]
                                 if out.strip() else f"fetch_chats.py が異常終了しました (終了コード {code})")
        elif failed_lines:
            res["status"] = "blocked" if blocked else "fetch_error"
            res["errors"] += failed_lines[:5]
        res["log"] = out[-4000:]

    if not args.skip_fetch and keep_if_unchanged(data_dir, before):
        res["kept"] = True   # 時刻のほかに変化がないので、集計もしない (今日の前の実行の結果をそのまま使う)
        sj = json.loads((data_dir / "report" / "summary.json").read_text(encoding="utf-8"))
        if sj.get("no_streams") and res["status"] == "ok":
            res["status"] = "no_streams"
    elif res["status"] not in ("blocked", "crashed", "not_found") or (data_dir / "videos.json").exists():
        code, out = run([sys.executable, "analyze_badges.py", str(data_dir), "--channels", args.channels], args.timeout)
        if code == 0 and "配信なし:" in out and res["status"] == "ok":
            res["status"] = "no_streams"   # 直近30日に配信がない (エラーではない)
        if code != 0:
            res["status"] = "crashed" if res["status"] == "ok" else res["status"]
            tail = out.strip().splitlines()[-1][:200] if out.strip() else ""
            res["errors"].append(f"analyze_badges.py が異常終了しました (終了コード {code}): {tail}")

    # 再挑戦中の枠 (取得エラー) を数えておく。再挑戦するのは直近30日の集計に入る間だけ (fetch_chats.py と同じ判定)
    vp = data_dir / "videos.json"
    if vp.exists():
        vids = json.loads(vp.read_text(encoding="utf-8")).get("videos", {})
        retry_cutoff = time.time() - 30 * 86400
        res["pending"] = sum(1 for v in vids.values() if v.get("status") in RETRY_STATUSES
                             and (v.get("start_ts") or v.get("failed_since_ts") or retry_cutoff) >= retry_cutoff)
    res["seconds"] = round(time.time() - t0)
    return res


def recorded_ids(data_dir: Path) -> set:
    """records に変換済みの動画ID (fetch_chats.py の recorded_ids と同じ)"""
    rec_dir = data_dir / "records"
    if not rec_dir.exists():
        return set()
    ids = {p.stem for p in rec_dir.glob("*.json")}
    for pp in rec_dir.glob("*.jsonl"):
        with pp.open(encoding="utf-8") as f:
            ids.update(json.loads(line)["video_id"] for line in f if line.strip())
    return ids


def parse_jst(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=JST).timestamp()
    except (TypeError, ValueError):
        return None


def check_channel(r: dict) -> list:
    """1チャンネル分の点検。異常の説明の一覧を返す"""
    out = []
    name = r["name"]
    if r.get("gaps"):
        n, ids = r["gaps"]
        out.append(f"{name}: 取りこぼし {n} 件 — 一覧にあるのに記録されていない枠 ({ids})")
    data_dir = HERE / "data" / r["id"]
    vp = data_dir / "videos.json"
    if not vp.exists():
        return out
    vids = json.loads(vp.read_text(encoding="utf-8")).get("videos", {})
    rec = recorded_ids(data_dir)
    now = time.time()
    retry_cutoff = now - 30 * 86400
    url = "https://www.youtube.com/watch?v="
    for vid, v in vids.items():
        st = v.get("status")
        if st == "ok" and vid not in rec and not (data_dir / "chats" / f"{vid}.live_chat.json").exists():
            out.append(f"{name}: 「保存」なのにチャットの記録がない枠 {url}{vid}")
        if vid in rec and st != "ok":
            out.append(f"{name}: チャットの記録があるのに「{st}」になっている枠 {url}{vid}")
        if st in ("ok", "no_chat") and not v.get("start_ts"):
            out.append(f"{name}: 開始時刻がない枠 ({st}) {url}{vid}")
        if st == "ok" and v.get("info_limited") and not v.get("start_from_chat"):
            out.append(f"{name}: 開始時刻が日付だけの枠 {url}{vid}")
        if st in RETRY_STATUSES | {"processing", "live_now"}:
            since = v.get("failed_since_ts") or parse_jst(v.get("seen_live_at")) or v.get("start_ts")
            start = v.get("start_ts") or since
            if since and now - since > PENDING_ALERT_DAYS * 86400 and (start or now) >= retry_cutoff:
                days = int((now - since) // 86400)
                out.append(f"{name}: {days} 日たっても再確認待ちの枠 ({st}: {(v.get('error') or '')[:60]}) {url}{vid}")
    return out


def pick_groups(channels: list, groups: str) -> list:
    """--groups の指定で絞る。「にじさんじ」ならグループ名がそれで始まるもの、「!にじさんじ」ならそれ以外"""
    exclude = groups.startswith("!")
    prefix = groups.removeprefix("!")
    return [c for c in channels if c["group"].startswith(prefix) != exclude]


def earlier_run_today(logs: Path, started: datetime, groups: str) -> bool:
    """今日 (日本時間) すでに同じ範囲の実行をしたか (logs/ のレポートで判断する)
    全チャンネルの実行 (予備の実行) は、グループごとの実行も実行済みに数える"""
    for lp in logs.glob(f"run_{started:%Y%m%d}_*.md"):
        text = lp.read_text(encoding="utf-8")
        if PARTIAL_MARK in text:
            continue
        earlier = next((ln[len(GROUPS_MARK):].strip() for ln in text.splitlines() if ln.startswith(GROUPS_MARK)), "")
        if not groups or not earlier or earlier == groups:
            return True
    return False


STATUS_LABEL = {"ok": "成功", "no_streams": "配信なし", "fetch_error": "取得エラー", "blocked": "ブロック", "not_found": "チャンネルなし",
                "crashed": "異常終了", "skipped": "未取得"}


def write_report(results, started, finished, build_msg, alerts, partial, groups="") -> str:
    counts = {k: sum(1 for r in results if r["status"] == k) for k in STATUS_LABEL}
    L = [f"# 毎日の更新 {started:%Y-%m-%d %H:%M} (JST)", ""]
    L.append(f"- 所要時間: {round((finished - started).total_seconds() / 60)} 分")
    L.append(f"- チャンネル: {len(results)} 件 / " + " / ".join(f"{STATUS_LABEL[k]} {v}" for k, v in counts.items() if v))
    L.append(f"- 新しく取得した配信: {sum(r['new'] for r in results)} 本")
    L.append(f"- 再挑戦中の配信 (取得エラー・直近30日の分は自動で再挑戦): {sum(r.get('pending', 0) for r in results)} 本")
    limited = sum(r.get("limited", 0) for r in results)
    warned = sum(1 for r in results if r.get("warned"))
    L.append(f"- YouTube から情報が欠けて返ってきた枠: {limited} 本 / 制限の警告が出たチャンネル: {warned} 件"
             + ("（開始時刻と配信時間はチャットから計算して補っています。"
                "「新しく取得した配信」が0本に近い日が続く場合だけ、取得自体が止められていないか確認してください）"
                if limited or warned else ""))
    kept = sum(1 for r in results if r.get("kept"))
    if kept:
        L.append(f"- 変化がなく書き換えなかったチャンネル: {kept} 件 (今日の前の実行の集計をそのまま使っています)")
    L.append(f"- サイト: {build_msg}")
    L.append(f"- 点検: " + (f"異常 {len(alerts)} 件 (下の「点検で見つかった異常」を確認してください)" if alerts else "異常なし"))
    if partial:
        L.append(PARTIAL_MARK + " (テスト・手動の実行。取得数の点検はしていません)")
    if groups:
        L.append(GROUPS_MARK + groups)
    L.append("")
    if alerts:
        L.append("## 点検で見つかった異常")
        L.append("")
        L += [f"- {a}" for a in alerts]
        L.append("")
    bad = [r for r in results if r["status"] not in ("ok", "no_streams")]
    if bad:
        L.append("## 確認が必要なチャンネル")
        L.append("")
        L.append("| チャンネル | 結果 | 内容 |")
        L.append("|---|---|---|")
        for r in bad:
            msg = "<br>".join(e.replace("|", "｜") for e in r["errors"][:3])
            L.append(f"| {r['name']} (`{r['id']}`) | {STATUS_LABEL[r['status']]} | {msg} |")
        L.append("")
        if any(r["status"] == "blocked" for r in bad):
            L.append("> 「ブロック」は YouTube にアクセスを止められた可能性があります。7日以内に解ければ、止まっていた間の配信も自動で取り直します。"
                     "続く場合は、手元のPCで `python run_daily.py --only <チャンネルID>` を実行して確認してください。")
            L.append("")
    L.append("## チャンネルごとの結果")
    L.append("")
    L.append("| チャンネル | 結果 | 新規 | 再挑戦中 | 秒 |")
    L.append("|---|---|---:|---:|---:|")
    for r in sorted(results, key=lambda r: (r["status"] in ("ok", "no_streams"), r["name"])):
        L.append(f"| {r['name']} | {STATUS_LABEL[r['status']]} | {r['new']} | {r.get('pending', 0)} | {r['seconds']} |")
    return "\n".join(L) + "\n"


def main():
    p = argparse.ArgumentParser(description="全チャンネルの取得・集計・サイト作成をまとめて行う")
    p.add_argument("--channels", default="channels.csv", help="チャンネル一覧 (既定: channels.csv)")
    p.add_argument("--workers", type=int, default=3, help="同時に処理するチャンネル数 (既定: 3)")
    p.add_argument("--only", nargs="*", help="このチャンネルIDだけ処理する (テスト用)")
    p.add_argument("--limit", type=int, default=0, help="先頭から N チャンネルだけ処理する (テスト用)")
    p.add_argument("--groups", default="", help="グループ名がこれで始まるチャンネルだけ処理する (! を付けるとそれ以外)")
    p.add_argument("--initial", action="store_true", help="初回取得がまだのチャンネルも取得する (手元で使う)")
    p.add_argument("--new-only", action="store_true",
                   help="初回取得がまだのチャンネルだけを処理する (--initial を含む。手元で使う)")
    p.add_argument("--skip-fetch", action="store_true", help="取得はせず、集計とサイト作成だけ行う")
    p.add_argument("--no-build", action="store_true", help="サイトを作らない")
    p.add_argument("--sleep", type=float, default=2.0, help="配信ごとの待ち時間 (秒)")
    p.add_argument("--test-alert", action="store_true", help="点検の異常を1件わざと出す (メール通知の確認用)")
    p.add_argument("--timeout", type=int, default=None,
                   help="1チャンネルの処理の制限時間 (秒。既定: 毎日の更新 3600、初回取得 10800)")
    args = p.parse_args()

    channels = load_channels(args.channels, strict=True)
    if args.only:
        channels = [c for c in channels if c["channel_id"] in set(args.only)]
        missing = set(args.only) - {c["channel_id"] for c in channels}
        if missing:
            sys.exit(f"{args.channels} にないチャンネルIDです: {', '.join(sorted(missing))}")
    if args.groups:
        channels = pick_groups(channels, args.groups)
    if args.new_only:
        args.initial = True
        done = [c for c in channels if initial_done(c["channel_id"])]
        channels = [c for c in channels if c not in done]
        print(f"初回取得がまだ・途中のチャンネル: {len(channels)} 件 (初回取得を終えた {len(done)} 件は飛ばします)")
    if args.timeout is None:
        args.timeout = 10800 if args.initial else 3600
    if args.limit:
        channels = channels[:args.limit]
    if not channels:
        sys.exit("処理するチャンネルがありません。")

    started = datetime.now(JST)
    print(f"{started:%Y-%m-%d %H:%M} 開始: {len(channels)} チャンネル / 同時 {args.workers}")
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process, c, args): c for c in channels}
        for i, f in enumerate(as_completed(futs), 1):
            try:
                r = f.result()
            except Exception as e:   # 想定外の例外でも、他のチャンネルの処理は続ける
                c = futs[f]
                r = {"id": c["channel_id"], "name": c["name"] or c["channel_id"], "status": "crashed",
                     "new": 0, "retry": 0, "errors": [repr(e)[:200]], "seconds": 0}
            results.append(r)
            note = f" — {r['errors'][0][:80]}" if r["errors"] else ""
            print(f"  ({i}/{len(channels)}) {r['name']}: {STATUS_LABEL[r['status']]} 新規 {r['new']}{note}", flush=True)

    build_msg = "作成しませんでした (--no-build)"
    if not args.no_build:
        code, out = run([sys.executable, "build_site.py", "--channels", args.channels], 600)
        build_msg = "作成しました" if code == 0 else f"作成に失敗しました: {out.strip().splitlines()[-1][:200] if out.strip() else code}"
        if code != 0:
            results.append({"id": "-", "name": "サイト作成 (build_site.py)", "status": "crashed", "new": 0,
                            "retry": 0, "errors": [build_msg], "seconds": 0})

    # ---- 点検
    logs = HERE / "logs"
    logs.mkdir(exist_ok=True)
    partial = bool(args.only or args.limit or args.new_only or args.skip_fetch)
    alerts = []
    for r in sorted(results, key=lambda r: r["name"]):
        if r["id"] != "-":
            alerts += check_channel(r)
    total_new = sum(r["new"] for r in results)
    if not partial and not earlier_run_today(logs, started, args.groups) and total_new < MIN_NEW_FIRST_RUN:
        alerts.append(f"今日最初の実行なのに、新しく取得した配信が {total_new} 本しかありません "
                      f"({MIN_NEW_FIRST_RUN} 本未満)。YouTube に取得を止められていないか確認してください")
    if args.test_alert:
        alerts.append("テスト用の異常です (--test-alert)。メール通知の確認のために、わざと出しています")

    finished = datetime.now(JST)
    report = write_report(results, started, finished, build_msg, alerts, partial, args.groups)
    (logs / f"run_{started:%Y%m%d_%H%M}.md").write_text(report, encoding="utf-8")
    print("\n" + report)

    errors = [r for r in results if r["status"] in ("fetch_error", "blocked", "not_found", "crashed")]
    if IN_ACTIONS:
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
                f.write(report)
        for r in errors:   # 実行結果のページに赤い注釈として出る
            msg = " / ".join(r["errors"][:2]).replace("\n", " ")
            print(f"::error title={STATUS_LABEL[r['status']]}: {r['name']}::{msg}")
        for r in results:
            if r["status"] == "skipped":
                print(f"::warning title=未取得: {r['name']}::{r['errors'][0]}")
        for a in alerts[:20]:
            print(f"::error title=点検で見つかった異常::{a}")
    sys.exit(1 if errors or alerts else 0)


if __name__ == "__main__":
    main()
