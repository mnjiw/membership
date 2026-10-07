"""
build_site.py
全チャンネルの集計結果 (data/*/report/summary.json と monthly/*.json) から、公開用のサイトを site/ に作る。

使い方:
    python build_site.py                 # data/ 以下の全チャンネルで site/ を作り直す
    python build_site.py --demo 500      # 一覧の見え方を試すためのダミーチャンネルを500件足す (公開しないこと)

確認するとき:
    python -m http.server 8000 -d site   → ブラウザで http://localhost:8000/ を開く

作るもの (site/ は毎回まるごと作り直す。元になるページの型は site_src/template.html):
    index.html                 … ランキング (直近30日)
    ranking/<YYYY-MM>/index.html … 月別ランキング
    ch/<チャンネルID>/index.html … チャンネル詳細
    groups/ compare/ about/ policy/ … 事務所別・比較・集計方法・免責事項とプライバシーポリシー
    404.html, sitemap.xml, robots.txt, _headers (Cloudflare 用の設定)
    data/                      … ページが読み込む集計データ (視聴者の名前やIDは含まない)

検索エンジン対策:
    - ページごとに URL を分け (#/ を使わない)、本文を HTML に書き出しておく (JavaScript なしでも読める)
    - ページごとの title / description / canonical / OGP (画像なし) / 構造化データ (JSON-LD)
    - sitemap.xml と robots.txt。Google Search Console の確認用メタタグ (site_config.json)

設定 (site_config.json):
    site_name, site_url (公開するURL。canonical と sitemap に使う), beta (テスト版の表示),
    established (制定日), contact_url (お問い合わせ先), google_site_verification
"""

import argparse
import html
import json
import random
import shutil
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from channel_list import channel_map

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

JST = timezone(timedelta(hours=9))
HERE = Path(__file__).resolve().parent
esc = html.escape


# ---------------------------------------------------------------------------
# 数値の書式 (サイトの JavaScript と同じ表記にそろえる)
# ---------------------------------------------------------------------------
def num(v):
    return "–" if v is None else f"{round(v):,}"


def yen(v):
    return "–" if v is None else f"¥{round(v):,}"


def pct(a, b=None):
    if b is not None:
        a = a / b if (a is not None and b) else None
    return "–" if a is None else f"{a * 100:.1f}%"


def man_yen(v):
    if v is None:
        return "–"
    return f"{v / 10000:.1f}万円" if v >= 10000 else yen(v)


def man_yen_range(lo, hi):
    """下限と推定が同じ (メンバーシップが1種類など) なら1つだけ"""
    if lo is None:
        return "–"
    return man_yen(lo) if round(lo) == round(hi) else f"{man_yen(lo)} 〜 {man_yen(hi)}"


def yen_range(lo, hi):
    if lo is None:
        return "–"
    return yen(lo) if round(lo) == round(hi) else f"{yen(lo)} 〜 {yen(hi)}"


def month_label(m):
    return "直近30日" if m == "recent" else f"{m[:4]}年{int(m[5:])}月"


def ratio(a, b):
    return round(a / b, 4) if a is not None and b else None


# ---------------------------------------------------------------------------
# 一覧用の指標
# ---------------------------------------------------------------------------
def metrics_recent(sj: dict) -> dict:
    members = sj["members_confirmed"]
    long_term = sum(b["members"] for b in sj.get("badges", []) if isinstance(b["months"], int) and b["months"] >= 12)
    rev = sj.get("revenue") or {}
    return {
        "members": members, "chatters": sj["unique_chatters"],
        "member_ratio": ratio(members, sj["unique_chatters"]), "long_term_ratio": ratio(long_term, members),
        "new_members": sj["events"]["new_members"], "streams": sj["streams_analyzed"],
        "rev_low": rev.get("total_low"), "rev_est": rev.get("total_est"),
        "per_10k_subs": round(members / sj["subscribers"] * 10000, 2) if sj.get("subscribers") else None,
    }


def metrics_month(h: dict) -> dict:
    rev = h.get("revenue") or {}
    gift = rev.get("gift_once") or 0
    return {
        "members": h["members_confirmed"], "chatters": h["unique_chatters"],
        "member_ratio": ratio(h["members_confirmed"], h["unique_chatters"]),
        "long_term_ratio": ratio(h.get("long_term_members"), h["members_confirmed"]),
        "new_members": h["events"]["new_members"], "streams": h["streams_analyzed"],
        "rev_low": rev["monthly_low"] + gift if rev else None, "rev_est": rev["monthly_est"] + gift if rev else None,
        "partial": h.get("partial", False), "coverage": h.get("coverage"),
    }


def demo_channels(n: int, seed: int = 1) -> list:
    """一覧の見え方を試すためのダミー。名前に必ず【ダミー】を付け、demo フラグを立てる"""
    rnd = random.Random(seed)
    groups = ["ダミー事務所A", "ダミー事務所B", "ダミー事務所C", "ダミー個人勢"]
    out = []
    for i in range(1, n + 1):
        subs = int(10 ** rnd.uniform(4, 6.4))
        chatters = int(subs * rnd.uniform(0.005, 0.03))
        members = max(5, int(chatters * rnd.uniform(0.02, 0.12)))
        configured = rnd.random() < 0.7
        low = members * 490 / 1.1 * 0.7
        out.append({
            "demo": True, "id": f"DEMO{i:04d}", "name": f"【ダミー】サンプルチャンネル{i:03d}", "handle": "",
            "group": rnd.choice(groups), "tags": [], "yomi": "", "subscribers": subs,
            "m": {"members": members, "chatters": chatters, "member_ratio": round(members / chatters, 4),
                  "long_term_ratio": round(rnd.uniform(0.2, 0.7), 4), "new_members": rnd.randint(0, 15),
                  "streams": rnd.randint(4, 30), "rev_low": round(low) if configured else None,
                  "rev_est": round(low * rnd.uniform(1.0, 1.8)) if configured else None,
                  "per_10k_subs": round(members / subs * 10000, 2)},
        })
    return out


def write_js(path: Path, statement: str, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(statement + " = " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";\n",
                    encoding="utf-8")


# ---------------------------------------------------------------------------
# ページ
# ---------------------------------------------------------------------------
class Site:
    def __init__(self, out: Path, cfg: dict, built_at: str):
        self.out, self.cfg, self.built_at = out, cfg, built_at
        self.template = (HERE / "site_src" / "template.html").read_text(encoding="utf-8")
        self.base = cfg.get("site_url", "").rstrip("/")
        self.sitemap = []   # (path, lastmod)

    def url(self, path: str) -> str:
        return f"{self.base}/{path}"

    def page(self, path: str, *, title: str, description: str, main: str, page: str = "static", arg: str = "",
             nav: str = "", scripts=(), index: bool = True, breadcrumbs=(), lastmod: str = "", og_type="website"):
        """path は "" (トップ) / "ch/UC.../" / "404.html" など。サイトの一番上からの相対"""
        depth = path.count("/")
        root = "../" * depth if depth else "./"
        name = self.cfg["site_name"]
        graph = [{"@type": "WebSite", "@id": self.url("") + "#website", "name": name, "url": self.url(""),
                  "inLanguage": "ja"},
                 {"@type": "WebPage", "@id": self.url(path), "url": self.url(path), "name": title,
                  "description": description, "isPartOf": {"@id": self.url("") + "#website"}, "inLanguage": "ja",
                  **({"dateModified": lastmod} if lastmod else {})}]
        if breadcrumbs:
            graph.append({"@type": "BreadcrumbList", "itemListElement": [
                {"@type": "ListItem", "position": i, "name": n, "item": self.url(p)}
                for i, (n, p) in enumerate(breadcrumbs, 1)]})
        jsonld = json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False).replace("</", "<\\/")
        verification = (f'<meta name="google-site-verification" content="{esc(self.cfg["google_site_verification"])}">\n'
                        if self.cfg.get("google_site_verification") and path == "" else "")
        contact = (f'<a href="{esc(self.cfg["contact_url"])}" rel="noopener" target="_blank">お問い合わせ</a>'
                   if self.cfg.get("contact_url") else "")
        values = {
            "title": esc(title), "description": esc(description), "canonical": esc(self.url(path)),
            "robots": "" if index else '<meta name="robots" content="noindex,follow">\n',
            "og_type": og_type, "site_name": esc(name), "verification": verification, "jsonld": jsonld,
            "page": page, "arg": esc(arg), "nav": nav or page, "root": root,
            "beta": "<small>テスト版</small>" if self.cfg.get("beta") else "",
            "main": main, "contact": "", "built_at": esc(self.built_at),
            "scripts": "".join(f'<script src="{root}{s}"></script>\n' for s in scripts),
        }
        text = self.template
        for k, v in values.items():
            text = text.replace("{{" + k + "}}", v)
        dest = self.out / (path + "index.html" if path == "" or path.endswith("/") else path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        if index:
            self.sitemap.append((path, lastmod or self.built_at[:10]))


def table(headers, rows, num_cols=()):
    cls = ' class="num"'
    th = "".join(f'<th{cls if i in num_cols else ""}>{esc(h)}</th>' for i, h in enumerate(headers))
    trs = "".join("<tr>" + "".join(f'<td{cls if i in num_cols else ""}>{c}</td>' for i, c in enumerate(r))
                  + "</tr>" for r in rows)
    return f'<div class="table-wrap"><table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>'


NOTICE = ('<div class="notice">「確認メンバー」は期間中のチャットに姿が見えたメンバーの人数で、実際の人数の下限です。'
          '発言しないメンバーは数えられないため、実際はこれより多くなります。 <a href="{root}about/">集計方法を見る</a></div>')


def ranking_main(rows, period, months, root):
    """rows: [(channel, metrics)] を確認メンバーの多い順に"""
    rows = sorted(rows, key=lambda r: -r[1]["members"])
    title = "YouTube メンバーシップ人数ランキング" + ("（直近30日）" if period == "recent" else f"（{month_label(period)}）")
    body = [f"<h1>{esc(title)}</h1>",
            '<p class="lead">公開されているライブ配信・プレミア公開のチャットから、各チャンネルのメンバーシップの規模（確認できたメンバーの人数）を推定・比較しています。</p>',
            NOTICE.format(root=root)]
    trs = []
    for i, (c, m) in enumerate(rows, 1):
        partial = (' <span class="chip warn">一部期間</span>' if m.get("partial") else "") +                   (' <span class="chip">配信なし</span>' if not m.get("streams") else "") + \
                  (' <span class="chip">メンバーシップなし</span>' if c.get("no_membership") else "")
        trs.append([str(i), f'<a class="ch-name" href="{root}ch/{c["id"]}/">{esc(c["name"])}</a>'
                    f'<span class="chip">{esc(c["group"])}</span>{partial}',
                    f"<b>{num(m['members'])} 人</b>",
                    "メンバーシップなし" if c.get("no_membership") else
                    (man_yen_range(m['rev_low'], m['rev_est']) if m.get("rev_est") is not None else "料金未設定"),
                    pct(m.get("member_ratio"))])
    body.append(table(["#", "チャンネル", "確認メンバー", "推定収益（1ヶ月）", "発言者中のメンバー率"], trs, num_cols=(0, 2, 3, 4)))
    links = [f'<a href="{root}">直近30日</a>'] + [f'<a href="{root}ranking/{m}/">{month_label(m)}</a>' for m in months]
    body.append(f'<nav aria-label="月別ランキング"><h2>月別ランキング</h2><p>{" ・ ".join(links)}</p></nav>')
    return "\n".join(body)


def detail_main(sj, root):
    name, rev = sj["display_name"], sj.get("revenue")
    url = f"https://www.youtube.com/{sj['handle']}" if sj.get("handle") else f"https://www.youtube.com/channel/{sj['channel_id']}"
    members = sj["members_confirmed"]
    long_term = sum(b["members"] for b in sj["badges"] if isinstance(b["months"], int) and b["months"] >= 12)
    p0, p1 = sj["period"]["from"][:10], sj["period"]["to"][:10]
    B = [f'<nav class="crumbs" aria-label="パンくずリスト"><a href="{root}">ランキング</a> / '
         f'<a href="{root}?group={esc(sj["group"])}">{esc(sj["group"])}</a></nav>',
         f"<h1>{esc(name)} のメンバーシップ</h1>",
         f'<p class="lead small">YouTube: <a href="{esc(url)}" rel="noopener" target="_blank">{esc(sj["channel"])}</a>'
         f" ・ 直近30日 {p0} 〜 {p1}（配信・プレミア {sj['streams_analyzed']} 本） ・ データ取得 {esc(sj.get('fetched_at') or '–')}</p>"]
    if sj.get("membership") is False:
        B.append(f'<p class="notice">{esc(name)} には、YouTube のメンバーシップがありません。</p>')
    if sj.get("no_streams"):
        B.append(f'<p class="notice">{esc(name)} は、直近30日に集計できるライブ配信・プレミア公開がありませんでした'
                 '（配信がない、またはアーカイブが残っていない）。そのため確認メンバーは0人と表示しています。</p>')
    s = (f"{esc(name)}（{esc(sj['group'])}）の直近30日のライブ配信・プレミア公開 {sj['streams_analyzed']} 本のチャットから、"
         f"少なくとも <strong>{num(members)} 人</strong>のメンバーを確認しました。")
    if sj.get("unique_chatters"):
        s += f"発言した {num(sj['unique_chatters'])} 人のうちメンバーは {pct(members, sj['unique_chatters'])}、"
    s += f"1年以上継続しているメンバーは {pct(long_term, members)} です。"
    if rev:
        s += (f"チャンネル主の推定収益（YouTube の手数料と消費税を除いた取り分）は、1ヶ月で "
              f"{yen_range(rev['total_low'], rev['total_est'])} と推定されます。")
    B.append(f"<p>{s}</p>")
    if sj.get("tiers_configured") and sj.get("tiers"):
        B.append("<h2>メンバーシップの種類と料金</h2>")
        B.append(table(["レベル", "月額", "確認できた人数", "推定人数"],
                       [[esc(t["tier"]), yen(t["price"]), f"{num(t['confirmed_members'])} 人",
                         f"{t['estimated_members']:.1f} 人"] for t in sj["tiers"]], num_cols=(1, 2, 3)))
        if rev and rev.get("overseas"):
            B.append('<p class="small muted">海外チャンネルのため、料金は日本から見た表示価格（円）で計算しています。</p>')
    hist = sj.get("history") or []
    if hist:
        B.append("<h2>月ごとの確認メンバー数</h2>")
        B.append(table(["月", "集計した日", "配信", "確認メンバー", "新規加入"],
                       [[f'<a href="{root}ranking/{h["month"]}/">{month_label(h["month"])}</a>',
                         f"{h['coverage']['from'][5:]}〜{h['coverage']['to'][5:]}", str(h["streams_analyzed"]),
                         f"{num(h['members_confirmed'])} 人", num(h["events"]["new_members"])] for h in hist],
                       num_cols=(2, 3, 4)))
    mo = sj.get("members_only_content") or []
    if mo:
        B.append("<h2>メンバー限定コンテンツ</h2><ul class=\"plain\">")
        for v in mo:
            lv = f"{v['members_level']} 以上" if v.get("members_level") else "レベル不明"
            B.append(f'<li>{esc(v.get("start") or "")} <a href="https://www.youtube.com/watch?v={esc(v["video_id"])}" '
                     f'rel="noopener" target="_blank">{esc(v.get("title") or v["video_id"])}</a>（{esc(lv)}・高評価 {num(v.get("like_count"))}）</li>')
        B.append("</ul>")
    ph = sj.get("price_history") or []
    if any(p.get("before") is not None for p in ph):
        B.append("<h2>料金の改定履歴</h2>")
        B.append(table(["日付", "レベル", "月額", "変更前"],
                       [[esc(p["date"] or "–"), esc(p["tier"]), "提供なし" if p["price"] is None else yen(p["price"]),
                         "初回確認" if p["before"] is None else yen(p["before"])] for p in ph], num_cols=(2, 3)))
    B.append(f'<p class="small muted">数値はすべて公開されているチャットからの推定です。<a href="{root}about/">集計方法</a></p>')
    return "\n".join(B)


def groups_main(index, root):
    groups = defaultdict(list)
    for c in index:
        groups[c["group"]].append(c)
    rows = []
    for g, cs in sorted(groups.items(), key=lambda x: -sum(c["m"]["members"] for c in x[1])):
        top = max(cs, key=lambda c: c["m"]["members"])
        rows.append([f'<a href="{root}?group={esc(g)}">{esc(g)}</a>', str(len(cs)),
                     f"{num(sum(c['m']['members'] for c in cs))} 人",
                     f"{num(statistics.median(c['m']['members'] for c in cs))} 人",
                     f'<a href="{root}ch/{top["id"]}/">{esc(top["name"])}</a>（{num(top["m"]["members"])}人）'])
    return "\n".join([
        "<h1>事務所・グループ別のメンバーシップ集計</h1>",
        '<p class="lead">直近30日の確認メンバーを、所属ごとにまとめています。</p>',
        table(["事務所・グループ", "チャンネル数", "確認メンバー合計", "中央値", "最多のチャンネル"], rows, num_cols=(1, 2, 3))])


ABOUT = """<div class="prose">
<h1>集計方法と注意点</h1>
<h2>何を数えているか</h2>
<ul>
<li>各チャンネルのライブ配信とプレミア公開について、公開されているチャットリプレイを集計しています。データは毎日 5:00 ごろに更新します。</li>
<li>発言にメンバーバッジが付いている人、加入通知が出た人、メンバーシップギフトを受け取った人を「メンバー」として数えます。</li>
<li><b>「確認メンバー」はチャットに一度でも姿が見えたメンバーの人数で、実際の人数の下限です。</b>発言しないメンバーは数えられません。</li>
<li>トップページは直近30日、月別ランキングはその月の1日〜末日の配信を集計しています。データを取り始めた月などは一部の期間だけの集計になり、「一部期間」と表示します。月が明けて7日たった月の集計は確定し、それ以降は変わりません。</li>
</ul>
<h2>レベル（メンバーシップの種類）</h2>
<ul>
<li>チャットのバッジはレベルに関係なく共通なので、バッジだけではレベルはわかりません。</li>
<li>「Welcome to ○○!」の加入通知や継続マイルストーンにレベル名が出た人だけ、レベルが確定します。過去180日分の通知も手がかりにします。</li>
<li>レベル不明の人は、確定した人の比率で各レベルに割り振っています（推定）。確定した人が少ないと、この比率は大きくぶれます。</li>
</ul>
<h2>推定収益</h2>
<ul>
<li>1人あたりの取り分 = 月額（税込）÷ 1.1 × 0.7（YouTube がクリエイターに渡す割合）として計算しています。</li>
<li>ギフトで加入した人の分は、贈った人が払った一回きりの支払いとして「ギフト」に計上し、月額課金とは分けています。</li>
<li>「最初の1か月無料」などのキャンペーンは考慮せず、確認できたメンバー（ギフトを除く）は全員が月額を払っているものとして計算しています。</li>
<li>海外のチャンネルも、料金は日本から見た表示価格（円）で計算しています。海外から加入したメンバーの実際の支払額とは異なります。</li>
<li>「下限」はレベル不明の人を全員いちばん安いレベルとみなした値、「推定」は比率で割り振った値です。</li>
<li>料金は運営者が毎月確認しています。改定があったチャンネルは、詳細ページの最下段に改定履歴を載せています。事務所との分配、iOS アプリ経由の価格差、返金などは反映していません。</li>
</ul>
<h2>集計に入らないもの</h2>
<ul>
<li>メンバー限定配信のチャット（詳細ページには「見られるレベル」と高評価数だけを参考に載せています）。</li>
<li>配信後にメンバー限定に変更されたアーカイブ、アーカイブの編集などでチャットリプレイが消えた配信。詳細ページの「集計できなかった配信」に理由つきで載せています。</li>
<li>配信中に流れなかった加入（配信外で加入した人の通知は見えません）。</li>
</ul>
<h2>掲載していない情報</h2>
<ul><li>個々の視聴者の名前・ID・発言内容は、このサイトには掲載していません。</li></ul>
</div>"""


def policy_main(cfg):
    est = esc(cfg.get("established") or "【公開日】")
    contact = esc(cfg.get("contact_url") or "")
    contact_html = (f'<a href="{contact}" rel="noopener" target="_blank">{contact}</a>' if contact else "【お問い合わせ先】")
    return f"""<div class="prose">
<h1>免責事項・プライバシーポリシー</h1>
<p class="muted small">制定日: {est}</p>
<h2>1. 当サイトについて</h2>
<p>{esc(cfg["site_name"])}（以下「当サイト」）は、YouTube チャンネルのメンバーシップの規模を、公開されている情報から推定・比較する非公式のサイトです。</p>
<p>当サイトは、YouTube（Google LLC）、掲載している各チャンネル、各チャンネルが所属する事務所・団体とは一切関係ありません。各社・各チャンネルへのお問い合わせはご遠慮ください。</p>
<h2>2. データの出典</h2>
<ul>
<li>YouTube で公開されている次の情報
<ul><li>ライブ配信・プレミア公開のチャットリプレイ</li><li>動画のタイトル・公開日時・高評価数・再生数</li><li>チャンネルの登録者数</li></ul></li>
</ul>
<h2>3. 掲載している数値について（免責事項）</h2>
<ul>
<li>当サイトの数値はすべて、公開されている情報からの推定値です。実際のメンバー数・収益とは異なります。</li>
<li>「確認メンバー」はチャットで確認できた人数であり、実際のメンバー数の下限です。</li>
<li>「推定収益」は、確認できた人数と運営者が確認した料金から、一定の仮定で計算した参考値です。税、手数料の詳細、返金などは反映しておらず、実際の収入を示すものではありません。</li>
<li>YouTube の仕様変更、データ取得の失敗、アーカイブの編集・非公開化などにより、数値が欠けたり誤ったりすることがあります。</li>
<li>当サイトの情報を利用したことによって生じたいかなる損害についても、運営者は責任を負いません。</li>
<li>掲載内容は予告なく変更・削除することがあります。</li>
</ul>
<h2>4. 個人情報の取り扱い（プライバシーポリシー）</h2>
<h3>4-1. チャット参加者の情報</h3>
<ul><li>集計のため、公開チャットに表示されたチャンネル名・チャンネルID・メンバーバッジなどを取得し、保存しています。</li></ul>
<h3>4-2. 当サイトの閲覧者の情報</h3>
<ul>
<li>比較の選択・表示の設定は、閲覧者のブラウザ内（localStorage）にだけ保存し、運営者には送信されません。</li>
<li>当サイトは Cloudflare で配信しています。配信のため、Cloudflare がアクセスログ（IPアドレス等）を取得する場合があります。</li>
<li>お問い合わせでいただいた情報は、お問い合わせへの対応にのみ使います。</li>
</ul>
<h2>5. 著作権・商標</h2>
<p>掲載しているチャンネル名・配信タイトルなどの権利は、各権利者に帰属します。YouTube は Google LLC の商標です。</p>
<h2>6. お問い合わせ</h2>
<ul>
<li>サイト管理者にご連絡・ご要望のある方はこちらにお願いします。{contact_html}</li>
<li>このページの内容は、必要に応じて改定することがあります。</li>
</ul>
</div>"""


def main():
    p = argparse.ArgumentParser(description="公開用のサイトを site/ に作る")
    p.add_argument("--data", default="data", help="集計結果の場所 (既定: data)")
    p.add_argument("--out", default="site", help="サイトの出力先 (既定: site。毎回まるごと作り直す)")
    p.add_argument("--channels", default="channels.csv", help="チャンネル一覧 (既定: channels.csv)")
    p.add_argument("--config", default="site_config.json", help="サイトの設定 (既定: site_config.json)")
    p.add_argument("--demo", type=int, default=0, help="ダミーチャンネルを何件足すか (公開しないこと)")
    args = p.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if "example" in cfg.get("site_url", "") or not cfg.get("site_url"):
        print(f"※ {args.config} の site_url がまだ仮の値です。公開する URL が決まったら書き換えてください (canonical と sitemap に使います)。")
    meta = channel_map(args.channels)
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)   # 掲載をやめたチャンネルのページが残らないよう、毎回作り直す
    out.mkdir(parents=True)
    built_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    site = Site(out, cfg, built_at)

    index, months, details = [], defaultdict(dict), []
    for sp in sorted(Path(args.data).glob("*/report/summary.json")):
        sj = json.loads(sp.read_text(encoding="utf-8"))
        cid = sj.get("channel_id")
        if not cid:
            continue
        cm = meta.get(cid, {})
        sj.update({"group": cm.get("group") or "未分類", "tags": cm.get("tags", []), "yomi": cm.get("yomi", ""),
                   "display_name": cm.get("name") or sj["channel"]})
        if sj.get("revenue") and cm.get("overseas"):
            sj["revenue"]["overseas"] = True
        index.append({"id": cid, "name": sj["display_name"], "title": sj["channel"], "handle": sj.get("handle", ""),
                      "group": sj["group"], "tags": sj["tags"], "yomi": sj["yomi"], "subscribers": sj.get("subscribers"),
                      "period": sj["period"], "fetched_at": sj.get("fetched_at"),
                      "m": metrics_recent(sj), "months": [h["month"] for h in sj.get("history", [])],
                      "no_membership": sj.get("membership") is False})
        for h in sj.get("history", []):
            months[h["month"]][cid] = metrics_month(h)
        write_js(out / "data" / "ch" / f"{cid}.js", f"window.MW_CH = window.MW_CH || {{}}; window.MW_CH[{json.dumps(cid)}]", sj)
        details.append(sj)
        print(f"  {sj['display_name']}  メンバー {sj['members_confirmed']} 人  (月別 {len(sj.get('history', []))} か月)")
    if not index:
        sys.exit("summary.json が見つかりません。先に analyze_badges.py を実行してください。")
    month_keys = sorted(months, reverse=True)
    for mon, chs in months.items():
        write_js(out / "data" / "months" / f"{mon}.js",
                 f"window.MW_MONTH = window.MW_MONTH || {{}}; window.MW_MONTH[{json.dumps(mon)}]", chs)
    write_js(out / "data" / "index.js", "window.MW_INDEX", {"built_at": built_at, "months": month_keys, "channels": index})
    if args.demo:
        write_js(out / "data" / "demo.js", "window.MW_DEMO", demo_channels(args.demo))
        print(f"  ダミーチャンネル {args.demo} 件 (公開しないこと)")

    name = cfg["site_name"]
    n = len(index)
    # トップ (直近30日のランキング)
    site.page("", page="list", arg="recent", nav="list",
              title=f"YouTubeメンバーシップ人数ランキング（推定）| {name}",
              description=f"{n}チャンネルのYouTubeメンバーシップの規模を、公開されているライブ配信のチャットから推定。"
                          "直近30日に確認できたメンバー数・推定収益・継続率をランキングで比較できます。",
              main=ranking_main([(c, c["m"]) for c in index], "recent", month_keys, "./"),
              scripts=["data/index.js"] + (["data/demo.js"] if args.demo else []))
    # 月別ランキング
    by_id = {c["id"]: c for c in index}
    for mon in month_keys:
        rows = [(by_id[cid], m) for cid, m in months[mon].items()]
        site.page(f"ranking/{mon}/", page="list", arg=mon, nav="list",
                  title=f"{month_label(mon)}のYouTubeメンバーシップ人数ランキング | {name}",
                  description=f"{month_label(mon)}のライブ配信・プレミア公開のチャットから推定した、{len(rows)}チャンネルのYouTubeメンバーシップ人数と推定収益のランキング。",
                  main=ranking_main(rows, mon, month_keys, "../../"),
                  scripts=["data/index.js", f"data/months/{mon}.js"],
                  breadcrumbs=[("ランキング", ""), (f"{month_label(mon)}のランキング", f"ranking/{mon}/")])
    # チャンネル詳細
    for sj in details:
        cid, dn = sj["channel_id"], sj["display_name"]
        rev = sj.get("revenue")
        desc = (f"{dn}（{sj['group']}）のYouTubeメンバーシップを公開チャットから推定。直近30日に確認できたメンバーは"
                f"{num(sj['members_confirmed'])}人以上"
                + (f"、推定収益は1ヶ月{man_yen_range(rev['total_low'], rev['total_est'])}" if rev else "")
                + "。レベル別の人数、月ごとの推移、メンバー限定配信も掲載。")
        site.page(f"ch/{cid}/", page="detail", arg=cid, nav="list", og_type="article",
                  title=f"{dn}のメンバーシップ人数・推定収益 | {name}", description=desc,
                  main=detail_main(sj, "../../"), scripts=[f"data/ch/{cid}.js"],
                  lastmod=(sj.get("fetched_at") or built_at)[:10],
                  breadcrumbs=[("ランキング", ""), (dn, f"ch/{cid}/")])
    site.page("groups/", page="groups", nav="groups", title=f"事務所・グループ別のメンバーシップ集計 | {name}",
              description="YouTubeメンバーシップの確認メンバー数を、事務所・グループごとに合計・比較しています。",
              main=groups_main(index, "../"), scripts=["data/index.js"],
              breadcrumbs=[("ランキング", ""), ("事務所・グループ別", "groups/")])
    site.page("compare/", page="compare", nav="compare", index=False, title=f"チャンネルを比較 | {name}",
              description="YouTubeチャンネルのメンバーシップの推定値を、最大4チャンネルまで並べて比較できます。",
              main='<h1>チャンネルを比較</h1><p class="lead">ランキングの「比較」欄で2〜4チャンネルを選んでください（JavaScript が必要です）。</p>',
              scripts=["data/index.js"])
    site.page("about/", nav="about", title=f"集計方法と注意点 | {name}",
              description="YouTubeメンバーシップの確認メンバー数・レベル・推定収益を、公開チャットからどのように集計・推定しているかの説明です。",
              main=ABOUT, breadcrumbs=[("ランキング", ""), ("集計方法", "about/")])
    site.page("policy/", nav="policy", title=f"免責事項・プライバシーポリシー | {name}",
              description=f"{name}の免責事項・プライバシーポリシー・お問い合わせ先です。",
              main=policy_main(cfg), breadcrumbs=[("ランキング", ""), ("免責事項・プライバシーポリシー", "policy/")])
    site.page("404.html", index=False, title=f"ページが見つかりません | {name}", description="ページが見つかりません。",
              main='<h1>ページが見つかりません</h1><p><a href="/">ランキングへ戻る</a></p>')

    # sitemap.xml / robots.txt / _headers
    urls = "".join(f"<url><loc>{esc(site.url(p))}</loc><lastmod>{d}</lastmod></url>\n" for p, d in site.sitemap)
    (out / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                                     '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + urls + "</urlset>\n",
                                     encoding="utf-8")
    (out / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {site.url('sitemap.xml')}\n",
                                    encoding="utf-8")
    (out / "_headers").write_text("/data/*\n  Cache-Control: public, max-age=600\n"
                                  "/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: strict-origin-when-cross-origin\n",
                                  encoding="utf-8")

    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    pages = sum(1 for _ in out.rglob("*.html"))
    print(f"\n{n} チャンネル / 月別 {len(month_keys)} か月 / {pages} ページ → {out}  (合計 {size / 1024:.0f} KB)")
    print(f"確認: python -m http.server 8000 -d {out}   → http://localhost:8000/")


if __name__ == "__main__":
    main()
