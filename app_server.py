#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AIニュースまとめアプリ（ローカルサーバー版・白基調UI）

起動してブラウザで開くと、その場で feeds.txt の全ソースから記事を集めて
一覧表示します。分野フィルタ（すべて/金融/主要ラボ…）で絞り込めます。
画面右上の「更新」ボタンでいつでも最新を取り直せます。

使い方:
  python app_server.py
  → ブラウザで http://127.0.0.1:8770/ を開く
  （アプリ.bat をダブルクリックすると自動で開きます）

外部パッケージ不要。収集は news_app.py の collect_articles() を再利用します。
"""

import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import news_app as na  # 既存の収集ロジック・分野色を再利用

PORT = 8770


def build_digest():
    """全ソースを集めて、画面表示用のJSONデータを返す。"""
    articles, ok, failed = na.collect_articles(verbose=False)
    items = [{
        "title": a["title"],
        "link": a["link"],
        "source": a["source"],
        "category": a["category"],
        "time": na.relative_time(a["date"]),
        "summary": a["summary"],
    } for a in articles]
    # 実際に登場する分野を、定義順で並べる（フィルタchip用）
    present = [c for c in na.CATEGORY_STYLE if any(a["category"] == c for a in articles)]
    return {
        "articles": items,
        "categories": present,
        "styles": na.CATEGORY_STYLE,  # {分野: [背景色, 文字色]}
        "ok": ok,
        "failed": failed,
        "generated": datetime.now().astimezone().strftime("%Y/%m/%d %H:%M"),
    }


PAGE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#ffffff">
<title>AIニュースまとめ</title>
<style>
  :root{--bg:#f6f7f9;--card:#ffffff;--card-h:#f1f3f6;--text:#1a1d24;
    --muted:#6b7280;--accent:#2f6fde;--border:#e6e8ec;}
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent;}
  body{margin:0;background:var(--bg);color:var(--text);
    font-family:-apple-system,"Hiragino Kaku Gothic ProN","Yu Gothic UI","Meiryo",system-ui,sans-serif;
    line-height:1.6;padding-bottom:40px;}
  header{position:sticky;top:0;z-index:10;background:rgba(255,255,255,.92);
    backdrop-filter:blur(8px);border-bottom:1px solid var(--border);padding:14px 16px 10px;}
  .row{display:flex;align-items:center;justify-content:space-between;gap:10px;}
  h1{font-size:18px;font-weight:600;margin:0;}
  #refresh{background:var(--accent);border:none;border-radius:10px;color:#fff;
    font-weight:600;padding:8px 14px;font-size:14px;cursor:pointer;white-space:nowrap;}
  #refresh:disabled{opacity:.5;cursor:default;}
  .sub{font-size:12px;color:var(--muted);margin-top:4px;}
  .chips{display:flex;gap:6px;margin-top:12px;overflow-x:auto;padding-bottom:2px;-webkit-overflow-scrolling:touch;}
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
  .spinner{width:28px;height:28px;border:3px solid var(--border);border-top-color:var(--accent);
    border-radius:50%;animation:spin .8s linear infinite;margin:40px auto;}
  @keyframes spin{to{transform:rotate(360deg);}}
  .failed{max-width:720px;margin:16px auto;padding:0 12px;font-size:12px;color:var(--muted);}
  .failed summary{cursor:pointer;}
</style>
</head>
<body>
  <header>
    <div class="row">
      <h1>AIニュースまとめ</h1>
      <button id="refresh">更新</button>
    </div>
    <div class="sub" id="sub">起動中…</div>
    <div class="chips" id="chips"></div>
  </header>
  <main id="results"></main>
  <div class="failed" id="failed"></div>
<script>
  const $=s=>document.querySelector(s);
  let STYLES={};
  function esc(t){const d=document.createElement('div');d.textContent=t??'';return d.innerHTML;}

  function cardHTML(a){
    const st=STYLES[a.category]||['#F1EFE8','#444441'];
    const meta=[a.source,a.time].filter(Boolean).join(' ・ ');
    return `<a class="card" data-cat="${esc(a.category)}" href="${esc(a.link)}" target="_blank" rel="noopener">
      <div class="cardhead">
        <span class="badge" style="background:${st[0]};color:${st[1]}">${esc(a.category)}</span>
        <span class="meta">${esc(meta)}</span>
      </div>
      <div class="title">${esc(a.title)}</div>
      ${a.summary?`<p class="summary">${esc(a.summary)}</p>`:''}
    </a>`;
  }

  function applyFilter(f){
    document.querySelectorAll('#results .card').forEach(c=>{
      c.style.display=(f==='all'||c.dataset.cat===f)?'':'none';
    });
  }

  function renderChips(cats){
    const wrap=$('#chips');
    wrap.innerHTML='<button class="chip active" data-f="all">すべて</button>'
      + cats.map(c=>`<button class="chip" data-f="${esc(c)}">${esc(c)}</button>`).join('');
    wrap.querySelectorAll('.chip').forEach(ch=>ch.onclick=()=>{
      wrap.querySelectorAll('.chip').forEach(x=>x.classList.remove('active'));
      ch.classList.add('active');
      applyFilter(ch.dataset.f);
    });
  }

  async function load(){
    const btn=$('#refresh'); btn.disabled=true;
    $('#sub').textContent='ニュースを集めています…';
    $('#results').innerHTML='<div class="spinner"></div>';
    $('#failed').innerHTML=''; $('#chips').innerHTML='';
    try{
      const r=await fetch('/api/digest');
      const d=await r.json();
      STYLES=d.styles||{};
      $('#sub').textContent='更新: '+d.generated+'　/　'+d.articles.length+'件　/　'+d.ok+'ソース';
      renderChips(d.categories||[]);
      $('#results').innerHTML=d.articles.map(cardHTML).join('');
      if(d.failed&&d.failed.length){
        $('#failed').innerHTML='<details><summary>取得できなかったソース ('+d.failed.length+
          ')</summary><ul>'+d.failed.map(s=>'<li>'+esc(s)+'</li>').join('')+'</ul></details>';
      }
    }catch(e){
      $('#sub').textContent='取得に失敗しました: '+e;
      $('#results').innerHTML='';
    }finally{ btn.disabled=false; }
  }

  $('#refresh').onclick=load;
  load();  // 起動時に自動で集める
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/digest":
            try:
                body = json.dumps(build_digest(), ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode("utf-8"),
                           "application/json; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *args):
        pass  # アクセスログは抑制


def main():
    print(f"AIニュースまとめアプリ起動: http://127.0.0.1:{PORT}/")
    print("（このウィンドウを閉じると停止します）")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
