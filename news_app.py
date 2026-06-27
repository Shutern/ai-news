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
import json
import os
import re
import struct
import sys
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

# ---- 設定 -------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
FEEDS_FILE = os.path.join(HERE, "feeds.txt")
OUTPUT_DIR = os.path.join(HERE, "output")
OUTPUT_HTML = os.path.join(OUTPUT_DIR, "index.html")
ICON_PATH = os.path.join(OUTPUT_DIR, "icon.png")
MANIFEST_PATH = os.path.join(OUTPUT_DIR, "manifest.json")

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


# ---- 重要度スコアリング（銀行での活用を重視） --------------------------
# 銀行業務に直結するテーマ。マッチするとタグ表示にも使う。
BANK_THEMES = {
    "業務効率化": ["業務効率", "バックオフィス", "議事録", "文字起こし", "コールセンター",
        "問い合わせ", "エージェント", "自動化", "rpa", "内製", "効率化", "省力化"],
    "リスク・不正": ["不正検知", "不正利用", "マネロン", "aml", "kyc", "本人確認",
        "リスク管理", "与信", "審査", "詐欺", "なりすまし"],
    "コンプラ・規制": ["規制", "金融庁", "コンプライアンス", "ガバナンス", "監査", "法規制"],
    "営業・提案": ["営業支援", "提案書", "顧客対応", "パーソナライズ", "レコメンド", "渉外"],
    "セキュリティ": ["セキュリティ", "サイバー", "個人情報", "プライバシー", "オンプレ",
        "セキュア", "情報漏えい", "情報漏洩", "データ主権"],
    "文書・ナレッジ": ["契約書", "稟議", "ocr", "rag", "社内文書", "ナレッジ", "文書検索",
        "マニュアル"],
    "基盤モデル": ["gpt-5", "gpt5", "claude", "gemini", "新モデル", "大規模言語モデル", "llm"],
}
THEME_WEIGHT = {"基盤モデル": 1}  # 既定は2、基盤モデルだけ控えめ
DEFAULT_THEME_WEIGHT = 2
FINANCE_SOURCE_BONUS = 4         # 金融カテゴリのソースは無条件で加点

# 一般的な重要語（大ニュースの兆候）
GENERAL_KW = ["発表", "提携", "買収", "資金調達", "兆円", "億円", "国内初", "初の", "導入"]
TIER_BONUS = {"主要ラボ": 2, "メディア": 1, "研究": -2}

# 銀行と無関係＝重要度を下げる語
NOISE_KW = ["ゲーム", "スマートグラス", "スピーカー", "イヤホン", "家電", "映画", "音楽",
    "アニメ", "スポーツ", "セール", "お買い得", "レビュー", "開封", "福袋", "値下げ",
    "クーポン", "%off", "オフサイド"]

# 複数社報道（コラボレーション）を測る固有名詞
CORROBORATION_ENTITIES = ["openai", "anthropic", "google", "gpt", "gemini", "claude",
    "microsoft", "deepmind", "nvidia", "メガバンク", "三菱ufj", "mufg", "三井住友",
    "smbc", "みずほ", "nttデータ"]

HIGH_CUT = 8   # このスコア以上＝重要度「高」
MID_CUT = 3    # このスコア以上＝「中」、未満＝「低」


def _kw_in(text, kw):
    """英数字キーワードは単語境界で、日本語は部分一致で判定（text は小文字前提）。"""
    if kw.isascii():
        return re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", text) is not None
    return kw in text


def annotate_importance(articles):
    """各記事に score / importance(高中低) / themes を付与する。"""
    texts = [(a["title"] + " " + a.get("summary", "")).lower() for a in articles]

    # 固有名詞ごとに「何社が報じているか」を集計
    entity_sources = {}
    for a, t in zip(articles, texts):
        for ent in CORROBORATION_ENTITIES:
            if _kw_in(t, ent):
                entity_sources.setdefault(ent, set()).add(a["source"])

    for a, t in zip(articles, texts):
        # 銀行活用度
        bank, themes = 0.0, []
        for theme, kws in BANK_THEMES.items():
            if any(_kw_in(t, kw) for kw in kws):
                themes.append(theme)
                bank += THEME_WEIGHT.get(theme, DEFAULT_THEME_WEIGHT)
        if a["category"] == "金融":
            bank += FINANCE_SOURCE_BONUS
        # 一般重要度
        gen = sum(1 for kw in GENERAL_KW if _kw_in(t, kw))
        gen += TIER_BONUS.get(a["category"], 0)
        if any(_kw_in(t, e) and len(entity_sources.get(e, ())) >= 3
               for e in CORROBORATION_ENTITIES):
            gen += 2  # 3社以上が報じている話題
        # ノイズ減点
        noise = min(sum(1 for kw in NOISE_KW if _kw_in(t, kw)), 3) * 2
        if a["category"] == "研究" and bank == 0:
            noise += 2  # 銀行に無関係なarXiv個別論文
        # 合計：銀行活用度を2倍で重視
        score = bank * 2 + gen - noise
        a["score"] = score
        a["bank"] = bank
        a["themes"] = themes[:2]
        a["importance"] = "高" if score >= HIGH_CUT else ("中" if score >= MID_CUT else "低")
    return articles


# ---- HTML生成（白基調・分野バッジ・フィルタ付き） ----------------------
def badge_style(category):
    bg, fg = CATEGORY_STYLE.get(category, CATEGORY_STYLE[DEFAULT_CATEGORY])
    return bg, fg


# 重要度バッジの色（高＝赤、中＝琥珀、低＝表示しない）
IMPORTANCE_STYLE = {"高": ("#FCEBEB", "#A32D2D", "重要"), "中": ("#FAEEDA", "#854F0B", "注目")}


def importance_badge(imp):
    if imp not in IMPORTANCE_STYLE:
        return ""
    bg, fg, label = IMPORTANCE_STYLE[imp]
    return f'<span class="badge imp" style="background:{bg};color:{fg}">{label}</span>'


def theme_tags(themes):
    if not themes:
        return ""
    tags = "".join(f'<span class="tag">{html.escape(t)}</span>' for t in themes)
    return f'<div class="tags">{tags}</div>'


def render_card(a):
    bg, fg = badge_style(a["category"])
    rel = relative_time(a["date"])
    meta = html.escape(a["source"]) + (f" ・ {html.escape(rel)}" if rel else "")
    summary_html = (
        f'<p class="summary">{html.escape(a["summary"])}</p>' if a["summary"] else ""
    )
    cat = html.escape(a["category"])
    return (
        f'<a class="card" data-cat="{cat}" data-imp="{a["importance"]}" '
        f'data-score="{round(a["score"])}" '
        f'href="{html.escape(a["link"])}" target="_blank" rel="noopener">'
        f'<div class="cardhead">'
        f'{importance_badge(a["importance"])}'
        f'<span class="badge" style="background:{bg};color:{fg}">{cat}</span>'
        f'<span class="meta">{meta}</span></div>'
        f'<div class="title">{html.escape(a["title"])}</div>'
        f'{theme_tags(a["themes"])}'
        f'{summary_html}</a>'
    )


def render_chips(articles):
    present = [c for c in CATEGORY_STYLE if any(a["category"] == c for a in articles)]
    # 先頭の「重要」をホーム（デフォルト表示）にする
    chips = ['<button class="chip active" data-filter="__imp__">🏦 重要</button>',
             '<button class="chip" data-filter="all">すべて</button>']
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
<link rel="apple-touch-icon" href="icon.png">
<link rel="icon" type="image/png" href="icon.png">
<link rel="manifest" href="manifest.json">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="AIニュース">
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
  .cardhead{display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap;}
  .badge{font-size:11px;font-weight:600;padding:2px 9px;border-radius:999px;white-space:nowrap;}
  .meta{font-size:12px;color:var(--muted);}
  .title{font-size:16px;font-weight:600;line-height:1.5;}
  .tags{display:flex;gap:5px;flex-wrap:wrap;margin-top:6px;}
  .tag{font-size:11px;color:#3b4252;background:#eef0f4;border:1px solid var(--border);
    border-radius:6px;padding:1px 7px;white-space:nowrap;}
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
  const results=document.querySelector('#results');
  const chips=[...document.querySelectorAll('.chip')];
  const cards=[...document.querySelectorAll('.card')];
  const original=[...cards];
  function apply(f){
    if(f==='__imp__'){
      // ホーム：重要(高)だけを、銀行活用度の高い順に並べて表示
      const imp=cards.filter(c=>c.dataset.imp==='高')
                     .sort((a,b)=>(+b.dataset.score)-(+a.dataset.score));
      const rest=original.filter(c=>!imp.includes(c));
      [...imp,...rest].forEach(c=>results.appendChild(c));
      cards.forEach(c=>{c.style.display=(c.dataset.imp==='高')?'':'none';});
    }else{
      // 他タブ：元の新しい順に戻して、分野で絞り込む
      original.forEach(c=>results.appendChild(c));
      cards.forEach(c=>{c.style.display=(f==='all'||c.dataset.cat===f)?'':'none';});
    }
  }
  chips.forEach(c=>c.addEventListener('click',()=>{
    chips.forEach(x=>x.classList.remove('active'));
    c.classList.add('active');
    apply(c.dataset.filter);
  }));
  apply('__imp__');  // 最初の画面は重要ニュース
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
    n_high = sum(1 for a in articles if a["importance"] == "高")
    sub = f"更新: {generated}　/　{len(articles)}件（重要{n_high}）　/　{sources_ok}ソース"
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
    final = spread_sources(newest_first(chosen))[:MAX_ITEMS]
    annotate_importance(final)  # 重要度スコア・テーマを付与
    return final, ok, failed


# ---- アプリアイコン生成（ホーム画面用・標準ライブラリのみでPNG出力） ----
def _png(width, height, px):
    """RGBAバイト列(px)から最小構成のPNGバイト列を作る。"""
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)                       # 各行のフィルタ種別=0
        raw.extend(px[y * stride:(y + 1) * stride])

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def make_app_icon(size=192):
    """ニュースフィードを表す簡潔なアプリアイコン（青地＋白カード＋3行）。"""
    px = bytearray(size * size * 4)

    def put(x, y, c):
        if 0 <= x < size and 0 <= y < size:
            i = (y * size + x) * 4
            px[i], px[i + 1], px[i + 2], px[i + 3] = c[0], c[1], c[2], 255

    def rrect(x0, y0, x1, y1, rad, c):
        for y in range(y0, y1):
            for x in range(x0, x1):
                cx = min(max(x, x0 + rad), x1 - 1 - rad)
                cy = min(max(y, y0 + rad), y1 - 1 - rad)
                if (x - cx) ** 2 + (y - cy) ** 2 <= rad * rad:
                    put(x, y, c)

    def disc(cx, cy, r, c):
        for y in range(cy - r, cy + r + 1):
            for x in range(cx - r, cx + r + 1):
                if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                    put(x, y, c)

    BLUE, WHITE, BAR, RED = (47, 111, 222), (255, 255, 255), (150, 163, 184), (226, 75, 74)
    s = size / 192.0

    def S(v):
        return int(round(v * s))

    rrect(0, 0, size, size, 0, BLUE)                 # 背景（角はOSが丸める）
    rrect(S(34), S(40), S(158), S(152), S(22), WHITE)  # 白いニュースカード
    for i, cy in enumerate((S(68), S(96), S(124))):    # 3行（左にドット＋バー）
        disc(S(52), cy, S(7), RED if i == 0 else BLUE)
        rrect(S(66), cy - S(6), S(146), cy + S(6), S(6), BAR)
    return _png(size, size, px)


def write_static_assets():
    """ホーム画面アイコンとマニフェストを output/ に書き出す。"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(ICON_PATH, "wb") as f:
        f.write(make_app_icon(192))
    manifest = {
        "name": "AIニュースまとめ", "short_name": "AIニュース",
        "start_url": ".", "display": "standalone",
        "background_color": "#f6f7f9", "theme_color": "#ffffff",
        "icons": [{"src": "icon.png", "sizes": "192x192", "type": "image/png"}],
    }
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


# ---- メイン（バッチ：HTMLファイルを書き出す） --------------------------
def main():
    articles, ok, failed = collect_articles(verbose=True)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(render_html(articles, ok, failed))
    write_static_assets()  # ホーム画面アイコン・マニフェスト

    print(f"\n完成: {OUTPUT_HTML}")
    print(f"記事 {len(articles)}件 / 成功 {ok}ソース / 失敗 {len(failed)}ソース")
    print("→ output/index.html をブラウザで開いてください。")


if __name__ == "__main__":
    sys.exit(main())
