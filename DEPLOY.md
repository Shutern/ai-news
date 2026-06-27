# スマホで常に最新を見る（GitHub で完全自動・PC不要）

GitHub の無料機能だけで、3時間ごとに自動でニュースを集めて
スマホから見られるWebページを公開します。**一度設定すればPCは不要**です。

所要時間は最初の設定だけで約10〜15分。git の知識は不要（画面操作だけ）です。

---

## 手順

### 1. GitHub アカウントを作る（無料・5分）
1. ブラウザで https://github.com/signup を開く
2. メールアドレス・パスワード・ユーザー名を入力（ユーザー名が公開URLの一部になります。例: `taro` → `taro.github.io`）
3. メールに届く確認コードを入力して完了

### 2. リポジトリ（置き場所）を作る
1. 右上の「＋」→「New repository」
2. Repository name に `ai-news` と入力
3. **Public**（公開）を選択 ※Pages無料公開のため
4. 「Create repository」をクリック

### 3. ファイルをアップロードする
1. 作成直後の画面で「uploading an existing file」のリンクをクリック
   （無ければ「Add file」→「Upload files」）
2. パソコンの `ai_news_app` フォルダを開き、**中身をすべて**ブラウザにドラッグ＆ドロップ
   - `news_app.py` / `feeds.txt` / `output` フォルダ / `.github` フォルダ などすべて
   - ※`.github` フォルダ（自動実行の設定）を忘れず入れること
3. 下の「Commit changes」をクリック

### 4. Pages（公開）を有効にする
1. リポジトリ上部の「Settings」→ 左メニュー「Pages」
2. 「Build and deployment」の Source を **「GitHub Actions」** に変更

### 5. 自動実行を確認する
1. 上部の「Actions」タブを開く
2. 「Build AI News」が動いて緑のチェックになれば成功（数分かかります）
3. 失敗（赤）の場合は、もう一度開いて「Re-run jobs」で再実行

### 6. スマホで開く
1. 公開URLは `https://（ユーザー名）.github.io/ai-news/`
2. スマホのブラウザでこのURLを開く
3. 「ホーム画面に追加」しておくと、アプリのように起動できます

---

## 運用メモ

- **更新頻度**: 3時間ごとに自動。すぐ更新したいときは Actions タブ →
  「Build AI News」→「Run workflow」で手動実行できます。
- **ソースを足す/減らす**: GitHub上で `feeds.txt` を編集して保存すると、
  自動で再実行されてページに反映されます。
- **更新間隔を変える**: `.github/workflows/build.yml` の `cron` を編集
  （例: `0 */6 * * *` で6時間ごと）。
- **公開範囲**: URLを知っている人は誰でも閲覧できます（集めているのは
  すべて公開ニュースなので実用上は問題ありません）。
