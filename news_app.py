#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AIニュースまとめアプリ（第1段階：APIなし・GitHubなし）

やること:
  1. feeds.txt に書いた RSS を集める
  2. 記事を新しい順に並べて、重複を除く
  3. スマホで見やすい1枚の index.html を output/ に書き出す

使い方:
  python news_app.py
  → output/index.html ができます。ダブルクリックでブラウザで開けます。

外部パッケージは不要（Python標準ライブラリだけで動きます）。
"""

import html
import os
import re
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

# ---- 設定 -------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
FEEDS_FILE = os.path.join(HERE, "feeds.txt")
OUTPUT_HTML = os.path.join(HERE, "output", "index.html")

MAX_ITEMS = 120         # 全体で表示する最大記事数
MAX_PER_FEED = 15       # 1ソースあたりの取り込み上限
GUARANTEED_PER_SOURCE = 4  # 各ソースから最低この件数は必ず載せる（埋没防止）
MAX_PER_SOURCE_FINAL = 6   # 1ソースが最終一覧を占有しないための上限
SUMMARY_CHARS = 140     # 抜粋の最大文字数
HTTP_TIMEOUT = 15       # 1ソースの取得タイムアウト（秒）
USER_AGENT = "Mozilla/5.0 (AI-News-Digest; personal use)"

# 総合ニュースソース(|filter付き)で「AI記事だけ」に絞るためのキーワード。
# タイトルか抜粋にどれか1つでも含まれていれば AI 関連とみなす。
AI_KEYWORDS = [
    "ai", "a.i.", "人工知能", "生成ai", "機械学習", "ディープラーニング", "深層学習",
    "ニューラル", "llm", "大規模言語モデル", "言語モデル", "chatgpt", "gpt",
    "claude", "gemini", "copilot", "openai", "anthropic", "deepmind", "google ai",
    "midjourney", "stable diffusion", "画像生成", "動画生成", "hugging face",
    "llama", "mistral", "transformer", "agi", "チャットボット", "grok", "sora",
    "機械翻訳", "音声認識", "推論モデル", "エージェント",
]


# 分野（カテゴリ）ごとの色バッジ。(背景色, 文字色)。feeds.txt の #cat: で指定。
DEFAULT_CATEGORY = "その他"
CATEGORY_STYLE = {
    "国内":     ("#FAECE7", "#993C1D"),  # coral
    "主要ラボ": ("#EEEDFE", "#3C3489"),  # purple
    "メディア": ("#E6F1FB", "#0C447C"),  # blue
    "金融":     ("#E1F5EE", "#0F6E56"),  # teal
    "コンサル": ("#FAEEDA", "#854F0B"),  # amber
    "研究":     ("#F1EFE8", "#444441"),  # gray
    DEFAULT_CATEGORY: ("#F1EFE8", "#444441"),
}


# ---- フィードの読み込み ------------------------------------------------
def load_feeds(path):
    """feeds.txt を読んで [(表示名, URL, 要フィルタか, 分野), ...] を返す。"""
    feeds = []
    category = DEFAULT_CATEGORY
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("#cat:"):       # 分野ディレクティブ
                category = line[len("#cat:"):].strip() or DEFAULT_CATEGORY
                continue
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            name = parts[0] if parts[0] else None
            url = parts[1] if len(parts) > 1 else parts[0]
            # 3つ目に "filter" があれば、AI関連記事だけに絞る
            needs_filter = len(parts) > 2 and parts[2].lower() == "filter"
            if len(parts) == 1:        # 表示名なし・URLのみの行
                name, url = None, parts[0]
            # "gnews:キーワード" は Googleニュース検索RSS に展開する
            if url.startswith("gnews:"):
                url = google_news_url(url[len("gnews:"):].strip())
            feeds.append((name, url, needs_filter, category))
    return feeds


def google_news_url(query):
    """検索キーワードから Googleニュースの日本語RSS URL を組み立てる。"""
    q = urllib.parse.quote(query)
    return f"https://news.google.com/rss/search?q={q}&hl=ja&gl=JP&ceid=JP:ja"


# ---- ユーティリティ ---------------------------------------------------
def tag(elem):
    """名前空間を取り除いたタグ名を返す（例: '{...}entry' -> 'entry'）。"""
    return elem.tag.rsplit("}", 1)[-1] if isinstance(elem.tag, str) else ""


def clean_text(raw, limit=None):
    """HTMLタグを除去し、実体参照を戻し、空白を整える。"""
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", "", raw)       # タグ除去
    text = html.unescape(text)               # &amp; などを戻す
    text = re.sub(r"\s+", " ", text).strip()  # 連続空白をまとめる
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def parse_date(raw):
    """RSS(RFC822) / Atom(ISO8601) どちらの日付文字列も datetime に変換。"""
    if not raw:
        return None
    raw = raw.strip()
    # RSS形式: "Tue, 24 Jun 2026 09:00:00 +0900"
    try:
        dt = parsedate_to_datetime(raw)
        if dt:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    # Atom形式: "2026-06-24T09:00:00Z"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def relative_time(dt):
    """日時を「3時間前」のような相対表記にする。"""
    if not dt:
        return ""
    now = datetime.now(timezone.utc)
    diff = now - dt.astimezone(timezone.utc)
    secs = diff.total_seconds()
    if secs < 0:
        return "たった今"
    mins = secs / 60
    if mins < 60:
        return f"{int(mins)}分前"
    hours = mins / 60
    if hours < 24:
        return f"{int(hours)}時間前"
    days = hours / 24
    if days < 7:
        return f"{int(days)}日前"
    return dt.astimezone().strftime("%Y/%m/%d")


# ---- フィードの取得・解析 ----------------------------------------------
def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


def get_link(item):
    """RSS/Atom 両対応でリンクURLを取り出す。"""
    # RSS: <link>URL</link>
    for child in item:
        if tag(child) == "link":
            if child.text and child.text.strip():
                return child.text.strip()
            # Atom: <link href="URL" rel="alternate"/>
            href = child.attrib.get("href")
            rel = child.attrib.get("rel", "alternate")
            if href and rel == "alternate":
                return href.strip()
    # 予備: rel指定なしの最初のhref
    for child in item:
        if tag(child) == "link" and child.attrib.get("href"):
            return child.attrib["href"].strip()
    return ""


def get_text(item, names):
    for child in item:
        if tag(child) in names and child.text:
            return child.text
    return ""


# 英語キーワードは単語境界で判定（"mail"の中の"ai"などの誤検出を防ぐ）。
# 日本語キーワードはそのまま部分一致でよい。
_ASCII_KW = [k for k in AI_KEYWORDS if k.isascii()]
_CJK_KW = [k for k in AI_KEYWORDS if not k.isascii()]
_ASCII_RE = re.compile(
    r"(?<![a-z0-9])(?:" + "|".join(re.escape(k) for k in _ASCII_KW) + r")(?![a-z0-9])"
)


def is_ai_related(title, summary):
    """タイトル/抜粋にAI関連キーワードが含まれるか。"""
    text = f"{title} {summary}".lower()
    if _ASCII_RE.search(text):
        return True
    return any(kw in text for kw in _CJK_KW)


def parse_feed(data, source_name, needs_filter=False, category=DEFAULT_CATEGORY):
    """フィードのバイト列を解析して記事のリストを返す。"""
    root = ET.fromstring(data)
    # RSS は channel/item、Atom は feed/entry
    items = root.iter()
    entries = [e for e in items if tag(e) in ("item", "entry")]

    results = []
    for it in entries:
        if len(results) >= MAX_PER_FEED:
            break
        title = clean_text(get_text(it, ("title",)))
        link = get_link(it)
        if not title or not link:
            continue
        raw_summary = get_text(it, ("description", "summary", "content"))
        # 総合ソースは、AIに関係する記事だけを残す
        if needs_filter and not is_ai_related(title, clean_text(raw_summary)):
            continue
        summary = make_summary(title, raw_summary)
        date_raw = get_text(it, ("pubDate", "published", "updated", "date"))
        dt = parse_date(date_raw)
        results.append({
            "title": title,
            "link": link,
            "summary": summary,
            "date": dt,
            "source": source_name,
            "category": category,
        })
    return results


# ---- 要約（第1段階は抜粋。あとでAIに差し替える場所） --------------------
def make_summary(title, raw_summary):
    """
    今は「記事冒頭の抜粋」をそのまま要約として使う。

    ★ AIモードを足すとき:
       ここで ANTHROPIC_API_KEY があれば Claude に投げて
       日本語3行要約を返す、という分岐を入れれば賢くなります。
       本体の他の部分は一切変えずに差し替えられる設計です。
    """
    text = clean_text(raw_summary)
    # arXivの定型前置き「arXiv:xxxx Announce Type: ... Abstract:」を除去
    text = re.sub(r"^arXiv:\S+\s*Announce Type:.*?Abstract:\s*", "", text)
    if len(text) > SUMMARY_CHARS:
        text = text[:SUMMARY_CHARS].rstrip() + "…"
    return text


# ---- HTML生成（白基調・分野バッジ・フィルタ付き） ----------------------
def badge_style(category):
    bg, fg = CATEGORY_STYLE.get(category, CATEGORY_STYLE[DEFAULT_CATEGORY])
    return bg, fg


def render_card(a):
    bg, fg = badge_style(a["category"])
    rel = relative_time(a["date"])
    meta = html.escape(a["source"]) + (f" ・ {html.escape(rel)}" if rel else "")
    summary_html = (
        f'<p class="summary">{html.escape(a["summary"])}</p>' if a["summary"] else ""
    )
    cat = html.escape(a["category"])
    return (
        f'<a class="card" data-cat="{cat}" href="{html.escape(a["link"])}" '
        f'target="_blank" rel="noopener">'
        f'<div class="cardhead">'
        f'<span class="badge" style="background:{bg};color:{fg}">{cat}</span>'
        f'<span class="meta">{meta}</span></div>'
        f'<div class="title">{html.escape(a["title"])}</div>'
        f'{summary_html}</a>'
    )


def render_chips(articles):
    present = [c for c in CATEGORY_STYLE if any(a["category"] == c for a in articles)]
    chips = ['<button class="chip active" data-filter="all">すべて</button>']
    for c in present:
        chips.append(f'<button class="chip" data-filter="{html.escape(c)}">{html.escape(c)}</button>')
    return "".join(chips)


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#ffffff">
<title>AIニュースまとめ</title>
<style>
  :root{
    --bg:#f6f7f9; --card:#ffffff; --card-h:#f1f3f6; --text:#1a1d24;
    --muted:#6b7280; --accent:#2f6fde; --border:#e6e8ec;
  }
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent;}
  body{margin:0;background:var(--bg);color:var(--text);
    font-family:-apple-system,"Hiragino Kaku Gothic ProN","Yu Gothic UI","Meiryo",system-ui,sans-serif;
    line-height:1.6;padding-bottom:40px;}
  header{position:sticky;top:0;z-index:10;background:rgba(255,255,255,.92);
    backdrop-filter:blur(8px);border-bottom:1px solid var(--border);padding:14px 16px 10px;}
  header h1{font-size:18px;font-weight:600;margin:0;}
  header .sub{font-size:12px;color:var(--muted);margin-top:4px;}
  .chips{display:flex;gap:6px;margin-top:12px;overflow-x:auto;padding-bottom:2px;
    -webkit-overflow-scrolling:touch;}
  .chip{flex:0 0 auto;background:#fff;border:1px solid var(--border);color:var(--muted);
    border-radius:999px;padding:6px 13px;font-size:13px;cursor:pointer;white-space:nowrap;}
  .chip.active{background:var(--text);border-color:var(--text);color:#fff;}
  main{max-width:720px;margin:0 auto;padding:12px;}
  .card{display:block;text-decoration:none;color:inherit;background:var(--card);
    border:1px solid var(--border);border-radius:12px;padding:14px 16px;margin:10px 0;
    transition:background .15s,border-color .15s,transform .05s;}
  .card:active{transform:scale(.99);}
  @media(hover:hover){.card:hover{background:var(--card-h);border-color:#d3d7de;}}
  .cardhead{display:flex;align-items:center;gap:8px;margin-bottom:6px;}
  .badge{font-size:11px;font-weight:600;padding:2px 9px;border-radius:999px;white-space:nowrap;}
  .meta{font-size:12px;color:var(--muted);}
  .title{font-size:16px;font-weight:600;line-height:1.5;}
  .summary{font-size:13px;color:var(--muted);margin:6px 0 0;}
  .empty{color:var(--muted);padding:20px;text-align:center;}
  .failed{max-width:720px;margin:16px auto;padding:0 12px;font-size:12px;color:var(--muted);}
  .failed summary{cursor:pointer;}
  footer{text-align:center;color:var(--muted);font-size:11px;margin-top:24px;}
</style>
</head>
<body>
  <header>
    <h1>AIニュースまとめ</h1>
    <div class="sub">__SUB__</div>
    <div class="chips">__CHIPS__</div>
  </header>
  <main id="results">__CARDS__</main>
  __FAILED__
  <footer>RSSから自動生成 ・ タップで元記事へ</footer>
<script>
  const chips=[...document.querySelectorAll('.chip')];
  const cards=[...document.querySelectorAll('.card')];
  chips.forEach(c=>c.addEventListener('click',()=>{
    chips.forEach(x=>x.classList.remove('active'));
    c.classList.add('active');
    const f=c.dataset.filter;
    cards.forEach(card=>{card.style.display=(f==='all'||card.dataset.cat===f)?'':'none';});
  }));
</script>
</body>
</html>"""


def render_html(articles, sources_ok, sources_failed):
    generated = datetime.now().astimezone().strftime("%Y/%m/%d %H:%M")
    cards_html = ("\n".join(render_card(a) for a in articles) if articles
                  else '<p class="empty">記事を取得できませんでした。'
                       'feeds.txt とネット接続を確認してください。</p>')
    failed_note = ""
    if sources_failed:
        items = "".join(f"<li>{html.escape(s)}</li>" for s in sources_failed)
        failed_note = (
            f'<details class="failed"><summary>取得できなかったソース '
            f'({len(sources_failed)})</summary><ul>{items}</ul></details>'
        )
    sub = f"更新: {generated}　/　{len(articles)}件　/　{sources_ok}ソース"
    return (HTML_TEMPLATE
            .replace("__SUB__", html.escape(sub))
            .replace("__CHIPS__", render_chips(articles))
            .replace("__CARDS__", cards_html)
            .replace("__FAILED__", failed_note))


# ---- 収集（バッチ版・アプリ版で共用） ----------------------------------
def newest_first(items):
    return sorted(
        items,
        key=lambda a: a["date"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )


def spread_sources(items, gap=2):
    """新しい順をおおむね保ちつつ、同じソースが連続しないよう散らす。
    （arXivのように同時刻で大量に出るソースが冒頭で固まるのを防ぐ）"""
    pending = list(items)
    out = []
    while pending:
        recent = {x["source"] for x in out[-gap:]}
        for i, a in enumerate(pending):
            if a["source"] not in recent:
                out.append(pending.pop(i))
                break
        else:
            out.append(pending.pop(0))  # 全部同じなら先頭をそのまま
    return out


def _fetch_one(feed):
    """1ソースを取得・解析。失敗しても例外を投げず結果に含める。"""
    name, url, needs_filter, category = feed
    label = name or url
    try:
        entries = parse_feed(fetch(url), name or url, needs_filter, category)
        return label, entries, None
    except Exception as e:
        return label, [], str(e)


def collect_articles(verbose=False):
    """全ソースから集めて、重複除去・埋没防止・新しい順に並べた記事リストを返す。

    戻り値: (記事リスト, 成功ソース数, 失敗ソースのラベル一覧)
    """
    feeds = load_feeds(FEEDS_FILE)
    if verbose:
        print(f"購読ソース: {len(feeds)}件")

    all_articles, ok, failed = [], 0, []
    # 並列取得で高速化（1ソース失敗しても全体は止めない）
    with ThreadPoolExecutor(max_workers=8) as ex:
        for label, entries, err in ex.map(_fetch_one, feeds):
            if err is None:
                all_articles.extend(entries)
                ok += 1
                if verbose:
                    print(f"  [OK]   {label}: {len(entries)}件")
            else:
                failed.append(label)
                if verbose:
                    print(f"  [NG]   {label}: {err}")

    # 重複除去（同じURL）
    seen, unique = set(), []
    for a in all_articles:
        if a["link"] not in seen:
            seen.add(a["link"])
            unique.append(a)

    # ① 各ソースから最新 GUARANTEED_PER_SOURCE 件を必ず確保（埋没防止）
    by_source = {}
    for a in unique:
        by_source.setdefault(a["source"], []).append(a)
    chosen, chosen_links = [], set()
    per_source = {}
    for src, src_items in by_source.items():
        picked = newest_first(src_items)[:GUARANTEED_PER_SOURCE]
        per_source[src] = len(picked)
        for a in picked:
            chosen.append(a)
            chosen_links.add(a["link"])
    # ② 残り枠を全体の新しい順で埋める。ただし1ソースが占有しないよう上限を設ける
    #    （arXivのように同時刻で大量に出るソースが上位を埋め尽くすのを防ぐ）
    for a in newest_first(unique):
        if len(chosen) >= MAX_ITEMS:
            break
        if a["link"] in chosen_links:
            continue
        if per_source.get(a["source"], 0) >= MAX_PER_SOURCE_FINAL:
            continue
        chosen.append(a)
        chosen_links.add(a["link"])
        per_source[a["source"]] = per_source.get(a["source"], 0) + 1
    # ③ 新しい順をベースに、同じソースが連続しないよう散らして見やすく
    return spread_sources(newest_first(chosen))[:MAX_ITEMS], ok, failed


# ---- メイン（バッチ：HTMLファイルを書き出す） --------------------------
def main():
    articles, ok, failed = collect_articles(verbose=True)

    os.makedirs(os.path.dirname(OUTPUT_HTML), exist_ok=True)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(render_html(articles, ok, failed))

    print(f"\n完成: {OUTPUT_HTML}")
    print(f"記事 {len(articles)}件 / 成功 {ok}ソース / 失敗 {len(failed)}ソース")
    print("→ output/index.html をブラウザで開いてください。")


if __name__ == "__main__":
    sys.exit(main())
