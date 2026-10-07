"""
price_check.py
メンバーシップ料金の確認 (月1回、手元のPCで行う)。

料金はログインしないと表示されないため、次の2通りの方法がある。

■ 自動 (おすすめ)
    python price_check.py login          # 初回だけ: 確認用のブラウザが開くので、YouTube にログインして閉じる
    python price_check.py auto           # 今月まだ確認していないチャンネルを自動で確認する
    python price_check.py auto --all     # 全チャンネルを確認する
    python price_check.py auto --only <チャンネル> ...   # 指定したチャンネルだけ

    確認用のブラウザ (Chrome) で各チャンネルのページを開き、「メンバーになる」を押して
    表示されたレベル名と月額 (例: level1 ￥490/月) を読み取る。人が画面で確認するのと同じ操作で、
    「メンバーになる」の画面を開くだけなので、加入や支払いは一切しない。
    料金が変わっていたら tiers.json に日付つきで記録し、そのチャンネルを集計し直してサイトを作り直す
    (= ページの料金・推定収益が自動で直る)。最後に commit して push すれば公開サイトにも反映される。

    ログイン情報は .price_check_profile/ (確認用ブラウザの専用プロフィール) にだけ保存される。
    このプログラムが cookie やパスワードを読み取ることはない。このフォルダは GitHub に入れない (.gitignore 済み)。

■ 手動
    python price_check.py                     # 今月まだ確認していないチャンネルの一覧 (確認用のURLつき)
    python price_check.py ok <チャンネル>       # 確認した。料金は変わっていない
    python price_check.py set <チャンネル> level1=490 level2=1290 [--note "概要欄で確認"]
    python price_check.py set <チャンネル> level3=none   # そのレベルの提供が終わった
    python price_check.py none <チャンネル>     # このチャンネルにはメンバーシップがない
                                              (auto でも「メンバーになる」ボタンがなければ自動でこう記録する)

<チャンネル> はチャンネルID (UC...) か、チャンネル名の一部。--date 2026-11-01 で確認日を指定できる (既定: 今日)。

記録先は data/<チャンネルID>/tiers.json:
    tiers[].prices に {"from": 確認日, "price": 月額} を追記 (料金が変わったときだけ)
    checks に確認日を追記 (毎回)
    changes に「level2: ¥1,190 → ¥1,290」のようなメモを追記 (料金が変わったときだけ)
"""

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

JST = timezone(timedelta(hours=9))
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
PROFILE = HERE / ".price_check_profile"   # 確認用ブラウザの専用プロフィール (ログイン状態はここにだけ残る)
JOIN_LABELS = ["このチャンネルのメンバーになります", "Join this channel"]


def today() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d")


def yen(v) -> str:
    return "提供なし" if v is None else f"¥{v:,}"


def channels():
    out = []
    for d in sorted(DATA.iterdir()) if DATA.exists() else []:
        vp = d / "videos.json"
        if not vp.exists():
            continue
        meta = json.loads(vp.read_text(encoding="utf-8"))
        tp = d / "tiers.json"
        cfg = json.loads(tp.read_text(encoding="utf-8")) if tp.exists() else {"tiers": []}
        out.append({"dir": d, "id": meta.get("channel_id") or d.name, "name": meta.get("channel", d.name),
                    "handle": meta.get("handle", ""), "cfg": cfg, "tiers_path": tp})
    return out


def find(query: str, chs=None):
    chs = chs if chs is not None else channels()
    hit = [c for c in chs if c["id"] == query] or [c for c in chs if query.lower() in c["name"].lower()]
    if len(hit) != 1:
        names = "\n".join(f"  {c['id']}  {c['name']}" for c in hit) or "  (なし)"
        sys.exit(f"チャンネルを1つに絞れません: {query}\n{names}")
    return hit[0]


def current_prices(cfg) -> dict:
    """レベル名 → いまの月額 (最新の履歴)"""
    out = {}
    for t in cfg.get("tiers", []):
        prices = t.get("prices") or [{"from": None, "price": t.get("price")}]
        prices = sorted(prices, key=lambda p: p.get("from") or "")
        out[t["name"]] = prices[-1].get("price") if prices else None
    return out


def last_check(cfg) -> str:
    dates = list(cfg.get("checks", []))
    for t in cfg.get("tiers", []):
        dates += [p["from"] for p in t.get("prices", []) if p.get("from")]
    return max(dates) if dates else ""


def tiers_seen_in_chat(d: Path) -> set:
    """records の加入通知・マイルストーンに出てきたレベル名"""
    seen = set()
    rec_dir = d / "records"
    for fp in list(rec_dir.glob("*.json")) + list(rec_dir.glob("*.jsonl")) if rec_dir.exists() else []:
        for line in fp.read_text(encoding="utf-8").splitlines():
            if line.strip():
                for e in json.loads(line).get("events", []):
                    if e.get("tier"):
                        seen.add(e["tier"])
    return seen


def save(c):
    c["tiers_path"].write_text(json.dumps(c["cfg"], ensure_ascii=False, indent=2), encoding="utf-8")


def apply_prices(c, prices: dict, date: str, note: str = "", remove_missing: bool = False) -> list:
    """確認した料金 {レベル名: 月額 or None} を tiers.json に反映する。変わったレベルの説明のリストを返す。
    remove_missing=True なら、確認画面に出てこなかったレベルを「提供なし」にする"""
    cfg = c["cfg"]
    tiers = cfg.setdefault("tiers", [])
    changed = []
    if remove_missing:
        shown = {k.strip().lower() for k in prices}
        for t in tiers:
            if t["name"].strip().lower() not in shown and current_prices({"tiers": [t]}).get(t["name"]) is not None:
                prices = {**prices, t["name"]: None}
    for name, price in prices.items():
        t = next((t for t in tiers if t["name"].strip().lower() == name.strip().lower()), None)
        if t is None:
            t = {"name": name.strip(), "prices": []}
            tiers.append(t)
        if "prices" not in t:
            t["prices"] = [{"from": None, "price": t.pop("price", None)}]
        plist = t["prices"]
        # ひな形の「料金未設定」行は置き換える
        plist[:] = [p for p in plist if not (p.get("from") is None and p.get("price") is None)]
        plist.sort(key=lambda p: p.get("from") or "")
        before = plist[-1]["price"] if plist else "（初回）"
        if plist and before == price:
            continue
        plist.append({"from": date, "price": price})
        changed.append(f"{t['name']}: {yen(before) if before != '（初回）' else before} → {yen(price)}")
    tiers[:] = [t for t in tiers if any(p.get("price") is not None for p in t.get("prices", []))]
    cfg.setdefault("checks", []).append(date)
    if changed:
        cfg.setdefault("changes", []).append({"date": date, "note": " / ".join(changed) + (f"（{note}）" if note else "")})
    save(c)
    return changed


# ---------------------------------------------------------------------------
# 手動
# ---------------------------------------------------------------------------
def cmd_list(show_all: bool):
    month = today()[:7]
    chs = channels()
    due = [c for c in chs if not last_check(c["cfg"]).startswith(month)]
    targets = chs if show_all else due
    print(f"料金の確認: 全 {len(chs)} チャンネル中、今月 ({month}) まだ確認していないもの {len(due)} 件\n")
    for c in targets:
        lc = last_check(c["cfg"]) or "未確認"
        prices = current_prices(c["cfg"])
        known = {k.strip().lower() for k in prices}
        missing = sorted(t for t in tiers_seen_in_chat(c["dir"]) if t.strip().lower() not in known)
        mark = "  " if lc.startswith(month) else "★ "
        print(f"{mark}{c['name']}  ({c['id']})")
        print(f"    最終確認: {lc}")
        if c["cfg"].get("membership") is False:
            print("    料金: メンバーシップなし")
        else:
            print(f"    料金: " + (" / ".join(f"{k} {yen(v)}" for k, v in prices.items()) or "未設定"))
        if missing:
            print(f"    ⚠ チャットに出ているが tiers.json にないレベル: {', '.join(missing)}")
        print(f"    確認先: https://www.youtube.com/channel/{c['id']}  (ログインして「メンバーになる」を押す)")
    if due and not show_all:
        print("\n自動で確認する:  python price_check.py auto")
        print("手で記録する:    python price_check.py ok <チャンネル>   /   python price_check.py set <チャンネル> レベル名=月額 ...")


def set_membership(c, has: bool, date: str, record_check: bool = True) -> str:
    """メンバーシップの有無を記録する。状態が変わったときは、その説明を返す (変わらなければ空文字)"""
    cfg = c["cfg"]
    before = cfg.get("membership")
    cfg["membership"] = has
    note = ""
    if has is False and before is not False:
        had_prices = any(v is not None for v in current_prices(cfg).values())
        note = "メンバーシップの提供が終わった" if (before is True or had_prices) else "メンバーシップなし"
    elif has is True and before is False:
        note = "メンバーシップの提供が始まった"
    if note:
        cfg.setdefault("changes", []).append({"date": date, "note": note})
    if record_check:
        cfg.setdefault("checks", []).append(date)
    save(c)
    return note


def cmd_none(query: str, date: str):
    c = find(query)
    note = set_membership(c, False, date)
    print(f"{c['name']}: {date} に「メンバーシップなし」と記録しました")
    rebuild([c])


def cmd_ok(query: str, date: str):
    c = find(query)
    c["cfg"].setdefault("checks", []).append(date)
    save(c)
    print(f"{c['name']}: {date} に確認（変更なし）と記録しました")


def cmd_set(query: str, pairs, date: str, note: str):
    c = find(query)
    prices = {}
    for pair in pairs:
        if "=" not in pair:
            sys.exit(f"「レベル名=月額」の形で指定してください: {pair}")
        name, val = pair.rsplit("=", 1)
        prices[name.strip()] = (None if val.strip().lower() in ("none", "null", "なし")
                                else int(val.replace(",", "").replace("¥", "").replace("￥", "")))
    changed = apply_prices(c, prices, date, note)
    print(f"{c['name']}: {date} に確認と記録しました")
    print("  " + ("\n  ".join(changed) if changed else "料金の変更はありませんでした"))
    if changed:
        rebuild([c])


# ---------------------------------------------------------------------------
# 自動 (確認用ブラウザで「メンバーになる」の画面を開いて読む)
# ---------------------------------------------------------------------------
PRICE_LINE = re.compile(r"^[￥¥]\s*([\d,]+)\s*/\s*月")
# 「メンバーになる」の画面の見出しなど、レベル名ではない行
HEADER_LINES = {"メンバーシップ", "このチャンネルのメンバーになります", "メンバーシップの特典をご利用ください",
                "メンバーになる", "メンバーシップに参加", "Join this channel", "Membership"}


def single_tier_name(known_tiers) -> str:
    """レベルが1種類のチャンネルの名前 (画面にレベル名が出ないとき)。
    レベル名を付けていないチャンネルは、チャットでは「チャンネル メンバーシップ」「Channel Membership」など
    見る人の言語で名前が変わるので、日本語のものを優先する"""
    known = list(dict.fromkeys(k for k in known_tiers if k))
    if len(known) == 1:
        return known[0]
    ja = [k for k in known if re.search(r"[ぁ-んァ-ン]", k)]
    return ja[0] if ja else "メンバーシップ"
ANY_PRICE = re.compile(r"[￥¥]\s*([\d,]+)\s*/\s*月")


def parse_offer_text(text: str, known_tiers=()) -> dict:
    """「メンバーになる」の画面の文字から {レベル名: 月額} を取り出す。
    画面の例:  level1 / ￥490/月 ￥0 / level2 / ￥1,190/月 ￥0 / level4 / ￥6,300/月 / ...
    レベルが1種類だけのチャンネルなど、レベル名が出ない画面では、tiers.json か チャットのレベル名が
    1つだけのときに限り、その名前を使う (わからなければ空の dict を返す)"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    found = []   # (レベル名の候補, 月額)
    for i, ln in enumerate(lines):
        m = PRICE_LINE.match(ln)
        if m and i > 0 and not ANY_PRICE.search(lines[i - 1]) and len(lines[i - 1]) <= 40:
            found.append((lines[i - 1], int(m.group(1).replace(",", ""))))
    if len(found) >= 2:    # レベルが複数: 「レベル名 / ￥490/月」が並ぶ
        return {name: price for name, price in found}
    if len(found) == 1:    # レベルが1種類: 画面にレベル名は出ず、直前の行は見出しのことが多い
        name, price = found[0]
        if name in HEADER_LINES:
            name = single_tier_name(known_tiers)
        return {name: price}
    prices = {int(m.group(1).replace(",", "")) for m in ANY_PRICE.finditer(text)}
    if len(prices) == 1:
        return {single_tier_name(known_tiers): prices.pop()}
    return {}


def open_browser(headless: bool):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright が入っていません。  pip install playwright  を実行してください。")
    pw = sync_playwright().start()
    try:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE), channel="chrome", headless=headless, locale="ja-JP",
            viewport={"width": 1280, "height": 900}, args=["--lang=ja-JP"],
            ignore_default_args=["--enable-automation"])
    except Exception as e:
        pw.stop()
        msg = str(e)
        if "ProcessSingleton" in msg or "already in use" in msg or "SingletonLock" in msg:
            sys.exit("確認用の Chrome がまだ開いています。login で開いたウィンドウを閉じてから、もう一度実行してください。")
        sys.exit(f"確認用のブラウザ (Chrome) を起動できませんでした: {msg[:300]}")
    return pw, ctx


def find_chrome() -> str:
    """インストールされている Chrome の場所"""
    import os
    import shutil
    cands = [os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
             os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
             os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
             "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    for c in cands:
        if Path(c).exists():
            return c
    for name in ("chrome", "google-chrome", "chrome.exe"):
        if shutil.which(name):
            return shutil.which(name)
    sys.exit("Chrome が見つかりませんでした。Google Chrome をインストールしてください。")


def cmd_login():
    """確認用プロフィールの Chrome を「普通のブラウザとして」開き、本人にログインしてもらう。
    自動操作ツール (playwright) から開いたブラウザでは、Google が「安全でない可能性があります」として
    ログインを拒否するため、ログインだけは自動操作なしで行う。ログイン状態はプロフィールに残り、
    auto ではそのプロフィールを使って料金を読み取る。"""
    PROFILE.mkdir(exist_ok=True)
    chrome = find_chrome()
    url = "https://accounts.google.com/ServiceLogin?service=youtube&hl=ja&continue=https%3A%2F%2Fwww.youtube.com%2F%3Fhl%3Dja"
    print("確認用の Chrome (普段の Chrome とは別のプロフィール) が開きます。YouTube にログインしてください。")
    print("ログインできたら、その Chrome のウィンドウを閉じてください (閉じると、ここも終わります)。")
    proc = subprocess.Popen([chrome, f"--user-data-dir={PROFILE}", "--no-first-run",
                             "--no-default-browser-check", url])
    proc.wait()
    # Windows では最初の chrome.exe がすぐ終わることがあるので、プロフィールのロック (lockfile) が消えるまで待つ
    lock = PROFILE / "lockfile"
    time.sleep(3)
    while lock.exists():
        try:
            lock.unlink()   # Chrome が開いている間は消せない。消せたら Chrome は閉じている
        except OSError:
            time.sleep(2)
    time.sleep(2)   # Chrome が終了処理を終えるのを少し待つ
    print(f"ログイン状態は {PROFILE.name}/ に保存されました。次からは  python price_check.py auto  で確認できます。")


def read_offer(page, cid: str) -> tuple:
    """チャンネルページで「メンバーになる」を押し、画面の文字を返す。(状態, 文字)"""
    page.goto(f"https://www.youtube.com/channel/{cid}?hl=ja", wait_until="domcontentloaded")
    btn = None
    for label in JOIN_LABELS:
        loc = page.get_by_role("button", name=label)
        try:
            loc.first.wait_for(state="visible", timeout=15000)
            btn = loc.first
            break
        except Exception:
            continue
    if btn is None:
        # ページの読み込みに失敗しただけでボタンが見つからないこともあるので、
        # 「チャンネル登録」ボタンが見えているときだけ「メンバーシップなし」と判断する
        sub = page.get_by_role("button", name=re.compile("チャンネル登録|登録済み|Subscribe"))
        try:
            sub.first.wait_for(state="visible", timeout=5000)
            return "no_membership", ""
        except Exception:
            return "no_page", ""
    offer = page.locator("tp-yt-paper-dialog").filter(has_text=re.compile(r"/\s*月"))
    login = page.locator("ytd-modal-with-title-and-button-renderer").filter(has_text=re.compile("ログイン|Sign in"))
    for _ in range(3):   # 読み込み直後は押しても反応しないことがあるので、数回押す
        btn.click()
        for _ in range(16):   # 最大8秒、料金の画面か「ログインしてください」が出るのを待つ
            if login.count() and login.first.is_visible():
                return "login_required", login.first.inner_text()
            if offer.count() and offer.first.is_visible():
                text = offer.first.inner_text()
                page.keyboard.press("Escape")
                return "ok", text
            time.sleep(0.5)
    return "no_dialog", ""


def cmd_auto(only, check_all: bool, headless: bool, date: str, build: bool):
    chs = channels()
    month = today()[:7]
    if only:
        targets = [find(q, chs) for q in only]
    elif check_all:
        targets = chs
    else:
        targets = [c for c in chs if not last_check(c["cfg"]).startswith(month)]
    if not targets:
        print(f"今月 ({month}) はすべて確認済みです。全部確認し直すときは --all を付けてください。")
        return
    if not PROFILE.exists():
        sys.exit("先に  python price_check.py login  で、確認用のブラウザから YouTube にログインしてください。")
    print(f"{len(targets)} チャンネルの料金を確認します (「メンバーになる」の画面を開くだけで、加入はしません)")
    pw, ctx = open_browser(headless)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    changed_chs, problems = [], []
    try:
        for i, c in enumerate(targets, 1):
            head = f"  ({i}/{len(targets)}) {c['name']}"
            status, text = read_offer(page, c["id"])
            if status == "login_required":
                problems.append((c, "ログインが切れています"))
                print(f"{head}: ログインが必要です。 python price_check.py login を実行してください。")
                break
            if status == "no_membership":
                note = set_membership(c, False, date)
                print(f"{head}: メンバーシップなし (「メンバーになる」ボタンがありません){'  → ' + note if note else ''}")
                if note:
                    changed_chs.append(c)
                continue
            if status != "ok":
                msg = {"no_dialog": "「メンバーになる」の画面が開きませんでした",
                       "no_page": "チャンネルのページを読み込めませんでした"}[status]
                problems.append((c, msg))
                print(f"{head}: {msg}")
                continue
            known = [k for k, v in current_prices(c["cfg"]).items() if v is not None] or sorted(tiers_seen_in_chat(c["dir"]))
            prices = parse_offer_text(text, known)
            if not prices:
                problems.append((c, "料金を読み取れませんでした: " + " / ".join(text.splitlines()[:8])[:150]))
                print(f"{head}: 料金を読み取れませんでした (手で確認してください)")
                continue
            started = set_membership(c, True, date, record_check=False)
            changed = apply_prices(c, prices, date, "メンバー登録画面で自動確認", remove_missing=len(prices) > 1)
            c["cfg"]["source"] = f"メンバー登録画面で確認（{date}）"
            save(c)
            if started:
                changed.insert(0, started)
            shown = " / ".join(f"{k} {yen(v)}" for k, v in prices.items())
            print(f"{head}: {shown}" + (f"  → 変更あり: {'; '.join(changed)}" if changed else "  (変更なし)"))
            if changed:
                changed_chs.append(c)
            time.sleep(2)
    finally:
        ctx.close()
        pw.stop()

    print(f"\n確認 {len(targets) - len(problems)} 件 / 料金の変更 {len(changed_chs)} 件 / 要確認 {len(problems)} 件")
    for c, msg in problems:
        print(f"  要確認: {c['name']} ({c['id']}) — {msg}")
    if changed_chs and build:
        rebuild(changed_chs)


def rebuild(chs):
    """料金が変わったチャンネルを集計し直し、サイトを作り直す (ページの料金・推定収益が直る)"""
    print("\n料金が変わったチャンネルを集計し直して、サイトを作り直します…")
    env_py = sys.executable
    for c in chs:
        r = subprocess.run([env_py, str(HERE / "analyze_badges.py"), str(c["dir"])], cwd=HERE,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        print(f"  集計: {c['name']} … {'OK' if r.returncode == 0 else 'エラー: ' + (r.stdout + r.stderr).strip()[-200:]}")
    r = subprocess.run([env_py, str(HERE / "build_site.py")], cwd=HERE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    print(f"  サイト: {'作り直しました' if r.returncode == 0 else 'エラー: ' + (r.stdout + r.stderr).strip()[-200:]}")
    print("commit して push すると、公開サイトにも反映されます。")


def main():
    p = argparse.ArgumentParser(description="メンバーシップ料金の確認 (月1回)")
    p.add_argument("command", nargs="?", default="list", choices=["list", "ok", "set", "none", "login", "auto"])
    p.add_argument("channel", nargs="?")
    p.add_argument("prices", nargs="*", help="レベル名=月額 (set のとき)")
    p.add_argument("--date", default=today(), help="確認日 YYYY-MM-DD (既定: 今日)")
    p.add_argument("--note", default="", help="メモ (set のとき。changes に一緒に記録)")
    p.add_argument("--all", action="store_true", help="list: 確認済みも表示 / auto: 全チャンネルを確認")
    p.add_argument("--only", nargs="*", help="auto: このチャンネルだけ確認する")
    p.add_argument("--headless", action="store_true", help="auto: ブラウザの画面を出さずに確認する")
    p.add_argument("--no-build", action="store_true", help="auto: 料金が変わっても集計・サイト作成をしない")
    a = p.parse_args()
    if a.command == "list":
        cmd_list(a.all)
    elif a.command == "login":
        cmd_login()
    elif a.command == "auto":
        cmd_auto(a.only, a.all, a.headless, a.date, not a.no_build)
    elif not a.channel:
        sys.exit("チャンネルを指定してください")
    elif a.command == "ok":
        cmd_ok(a.channel, a.date)
    elif a.command == "none":
        cmd_none(a.channel, a.date)
    else:
        if not a.prices:
            sys.exit("レベル名=月額 を1つ以上指定してください")
        cmd_set(a.channel, a.prices, a.date, a.note)


if __name__ == "__main__":
    main()
