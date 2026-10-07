"""
channel_list.py
channels.csv (掲載するチャンネルの一覧) を読む共通処理。

列 (1行目に列名):
    channel_id     … 必須。UC で始まる24文字
    channel_title  … YouTube 上のチャンネル名 (メモ用。サイトの表示には YouTube から取った名前を使う)
    group          … 事務所・グループ。ランキングの絞り込みと「事務所別」ページに使う
    country        … 活動の拠点 (JP など)。JP 以外は「海外チャンネル」として、料金の注記を出す
    name           … サイトで表示する短い名前 (空なら YouTube のチャンネル名)
    yomi           … 読み仮名。検索に使う
    tags           … 自由なラベル (; 区切り)。検索と表示に使う。集計には影響しない
    そのほかの列 (color, gender など) は読み込むが、今は使っていない。

文字コードは UTF-8 (BOM あり/なし) を想定。Excel で「CSV (コンマ区切り)」として保存した
Shift_JIS のファイルも読めるが、韓国語・絵文字などが「?」に化けるので、
Excel では「CSV UTF-8 (コンマ区切り)」で保存すること。
"""

import csv
import io
import re
import sys
from pathlib import Path

ID_RE = re.compile(r"UC[0-9A-Za-z_-]{22}")


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        print(f"※ {path} は UTF-8 ではありません (Shift_JIS として読みます)。"
              "韓国語・絵文字などが化けている可能性があるので、Excel では「CSV UTF-8 (コンマ区切り)」で保存し直してください。",
              file=sys.stderr)
        return raw.decode("cp932", errors="replace")


def load_channels(path="channels.csv", strict=False) -> list:
    """channels.csv を読み、1チャンネル1件の dict のリストを返す。
    strict=True なら、IDの形式の誤り・重複があったときに止める"""
    path = Path(path)
    if not path.exists():
        return []
    rows = list(csv.DictReader(io.StringIO(read_text(path))))
    out, seen, problems = [], set(), []
    for n, r in enumerate(rows, 2):
        r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
        cid = r.get("channel_id", "")
        if not cid:
            continue
        if not ID_RE.fullmatch(cid):
            problems.append(f"{n}行目: チャンネルIDの形式が正しくありません: {cid}")
            continue
        if cid in seen:
            problems.append(f"{n}行目: チャンネルIDが重複しています: {cid}")
            continue
        seen.add(cid)
        country = r.get("country", "").upper()
        out.append({
            "channel_id": cid,
            "channel_title": r.get("channel_title", ""),
            "group": r.get("group", "") or "未分類",
            "country": country,
            "overseas": bool(country) and country != "JP",
            "name": r.get("name", ""),
            "yomi": r.get("yomi", ""),
            "tags": [t.strip() for t in r.get("tags", "").split(";") if t.strip()],
            "row": n,
        })
    for p in problems:
        print("※ " + p, file=sys.stderr)
    if strict and problems:
        sys.exit(f"{path} に {len(problems)} 件の問題があります。")
    return out


def channel_map(path="channels.csv") -> dict:
    return {c["channel_id"]: c for c in load_channels(path)}
