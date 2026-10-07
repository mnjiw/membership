"""
run_daily.py
channels.csv の全チャンネルについて「取得 → 集計」を並列で行い、最後にサイトを作る (毎日の更新用)。
GitHub Actions からも、手元のPCからも同じように動く。

使い方:
    python run_daily.py                          # 全チャンネル (初回取得がまだのチャンネルは飛ばす)
    python run_daily.py --only UCxxxx UCyyyy     # 指定したチャンネルだけ (テスト用)
    python run_daily.py --limit 3                # 先頭から3チャンネルだけ (テスト用)
    python run_daily.py --workers 4              # 同時に処理するチャンネル数 (既定: 3)
    python run_daily.py --new-only               # 初回取得がまだのチャンネルだけ、過去30日分を取得する (手元で使う)
                                                 #   途中で止めても、もう一度同じコマンドで続きから再開する
    python run_daily.py --initial --only UCxxxx  # 指定したチャンネルの初回取得

結果:
    logs/run_<日時>.md に、チャンネルごとの結果 (新規取得数・エラー) を書く。
    GitHub Actions では、同じ内容を実行結果のページ (Summary) にも出し、エラーは注釈 (::error::) にする。
    エラーがあったときは終了コード 1 で終わる → GitHub Actions の実行が「失敗」になり、メールで通知される。

エラーの扱い:
    ブロック   … チャンネルの一覧すら取れない (fetch_chats.py が終了コード 2)。YouTube にアクセスを止められた可能性
    取得エラー … 一部の配信でチャットを取れなかった (次回以降も7日間は自動で再挑戦する)
    異常終了   … プログラム自体のエラー
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

    if not args.skip_fetch:
        code, out = run([sys.executable, "fetch_chats.py", cid, "--sleep", str(args.sleep)], args.timeout)
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

    if res["status"] not in ("blocked", "crashed", "not_found") or (data_dir / "videos.json").exists():
        code, out = run([sys.executable, "analyze_badges.py", str(data_dir), "--channels", args.channels], args.timeout)
        if code == 0 and "配信なし:" in out and res["status"] == "ok":
            res["status"] = "no_streams"   # 直近30日に配信がない (エラーではない)
        if code != 0:
            res["status"] = "crashed" if res["status"] == "ok" else res["status"]
            tail = out.strip().splitlines()[-1][:200] if out.strip() else ""
            res["errors"].append(f"analyze_badges.py が異常終了しました (終了コード {code}): {tail}")

    # 7日のあいだ再挑戦中の枠 (取得エラー) を数えておく
    vp = data_dir / "videos.json"
    if vp.exists():
        vids = json.loads(vp.read_text(encoding="utf-8")).get("videos", {})
        res["pending"] = sum(1 for v in vids.values() if v.get("status") in RETRY_STATUSES)
    res["seconds"] = round(time.time() - t0)
    return res


STATUS_LABEL = {"ok": "成功", "no_streams": "配信なし", "fetch_error": "取得エラー", "blocked": "ブロック", "not_found": "チャンネルなし",
                "crashed": "異常終了", "skipped": "未取得"}


def write_report(results, started, finished, build_msg) -> str:
    counts = {k: sum(1 for r in results if r["status"] == k) for k in STATUS_LABEL}
    L = [f"# 毎日の更新 {started:%Y-%m-%d %H:%M} (JST)", ""]
    L.append(f"- 所要時間: {round((finished - started).total_seconds() / 60)} 分")
    L.append(f"- チャンネル: {len(results)} 件 / " + " / ".join(f"{STATUS_LABEL[k]} {v}" for k, v in counts.items() if v))
    L.append(f"- 新しく取得した配信: {sum(r['new'] for r in results)} 本")
    L.append(f"- 再挑戦中の配信 (取得エラー・7日間は自動で再挑戦): {sum(r.get('pending', 0) for r in results)} 本")
    L.append(f"- サイト: {build_msg}")
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
    p.add_argument("--initial", action="store_true", help="初回取得がまだのチャンネルも取得する (手元で使う)")
    p.add_argument("--new-only", action="store_true",
                   help="初回取得がまだのチャンネルだけを処理する (--initial を含む。手元で使う)")
    p.add_argument("--skip-fetch", action="store_true", help="取得はせず、集計とサイト作成だけ行う")
    p.add_argument("--no-build", action="store_true", help="サイトを作らない")
    p.add_argument("--sleep", type=float, default=2.0, help="配信ごとの待ち時間 (秒)")
    p.add_argument("--timeout", type=int, default=None,
                   help="1チャンネルの処理の制限時間 (秒。既定: 毎日の更新 3600、初回取得 10800)")
    args = p.parse_args()

    channels = load_channels(args.channels, strict=True)
    if args.only:
        channels = [c for c in channels if c["channel_id"] in set(args.only)]
        missing = set(args.only) - {c["channel_id"] for c in channels}
        if missing:
            sys.exit(f"{args.channels} にないチャンネルIDです: {', '.join(sorted(missing))}")
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

    finished = datetime.now(JST)
    report = write_report(results, started, finished, build_msg)
    logs = HERE / "logs"
    logs.mkdir(exist_ok=True)
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
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
