# Real-time Translation (EN → JA)

英語音声をリアルタイムで日本語に翻訳するWebツール。

Google Cloud Speech-to-Text (streaming + interim results) と Cloud Translation API を使用し、WebSocket経由でブラウザに翻訳結果をストリーミング表示する。

## 前提条件

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (`brew install uv`)
- PortAudio (`brew install portaudio`)
- Blackhole 2ch (`brew install --cask blackhoke-2ch`)
- Google Cloud プロジェクトで以下のAPIを有効化:
  - Cloud Speech-to-Text API
  - Cloud Translation API
- サービスアカウントキー (JSON)
  - サービスアカウントに以下の権限を付与
    - Cloud Speech 管理者
    - Cloud Translate API 管理者

## セットアップ

```bash
# 依存関係インストール
uv sync

# .envファイルを作成し、サービスアカウントキーのパスを設定
cp .env.example .env
# .env を編集: GOOGLE_APPLICATION_CREDENTIALS=./credentials.json
```

## 使い方

### Webサーバー起動 (マイク入力)

```bash
uv run main.py
```

起動するとマイクデバイスの選択画面が表示される:

```
=== Available input devices ===
  [0] BlackHole 2ch  (device_index=0, rate=48000)
  [1] 外部マイク  (device_index=1, rate=48000)
  [2] MacBook Proのマイク  (device_index=3, rate=48000)

Select device [0-2]: 1
  -> Using: 外部マイク

Server running at http://localhost:8080
```

ブラウザで http://localhost:8080 を開くと、翻訳結果がリアルタイムで表示される。

### ファイルからテスト

```bash
uv run python test_file.py
```

`test_5m.wav` をリアルタイムペースでSTTに流し、翻訳結果をコンソールに出力する。

## アーキテクチャ

```
[Microphone/File] → [Audio Thread] → Queue → [STT Thread] → [Translation] → [WebSocket] → [Browser]
```

### STT層

- Google Cloud Speech-to-Text v1 ストリーミングAPI
- `interim_results=True`, `enable_automatic_punctuation=True`
- ストリーム再構築の段階的無音検出:
  - 3分未満: 切断しない
  - 3〜4分: 0.7秒の無音で切断
  - 4〜5分: 0.3秒の無音で切断
  - 5分以上: 強制切断
- ストリームをまたいでtranscriptを蓄積

### Translation層

- Google Cloud Translation API v2
- STTストリームの境界とは独立して、`.?!` でメッセージを区切る
- interim resultは0.5秒に1回の頻度で翻訳

### フロントエンド

- ピリオド等に到達するまで同一メッセージが更新され続ける
- 句点で区切られたら確定し、新しいメッセージとして表示

## ファイル構成

| ファイル | 内容 |
|---------|------|
| `main.py` | Webサーバー (aiohttp) + マイクキャプチャ |
| `translator.py` | コアモジュール: STT + Translation エンジン |
| `index.html` | フロントエンドUI |
| `test_file.py` | 音声ファイルからのテストスクリプト |
| `.env.example` | 環境変数テンプレート |
