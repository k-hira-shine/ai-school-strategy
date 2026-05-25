"""
YouTube コメント収集 → Google スプレッドシート出力スクリプト

使い方:
  python fetch_comments.py --channel CHANNEL_ID [--max 200] [--videos 50]
  python fetch_comments.py --channel CHANNEL_ID --dry-run   # コンソール確認のみ

初回実行時にブラウザが開いてGoogleログイン認証が求められます（1回だけ）。
"""

import os
import sys
import argparse
from pathlib import Path
from dotenv import load_dotenv
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build as sheets_build
import json

load_dotenv()

API_KEY        = os.getenv("YOUTUBE_API_KEY")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")   # 初回実行後に .env に追記
CREDENTIALS_FILE = Path(__file__).parent / "credentials.json"
TOKEN_FILE       = Path(__file__).parent / "token.json"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# スプレッドシートのヘッダー行
HEADERS = [
    "日付", "チャンネル名", "動画タイトル", "動画URL",
    "いいね数", "種別", "制約カテゴリ", "コメント本文"
]

# 制約カテゴリの自動分類
CATEGORY_RULES = {
    "A（ツール制限）": [
        "使えない", "ブロック", "禁止", "アクセスできない", "インストール", "有料",
        "経費", "申請", "セキュリティ", "社内", "会社のPC", "ポリシー",
    ],
    "B（時間・場所）": [
        "時間がない", "残業", "疲れ", "スマホ", "PC持ってない", "帰宅後",
        "副業する時間", "忙しい",
    ],
    "C（副業禁止）": [
        "副業禁止", "バレる", "就業規則", "公務員", "住民税", "顔出し",
        "本名", "会社にバレ",
    ],
    "D（心理的）": [
        "不安", "難しい", "わからない", "できるか", "自信", "挫折",
        "やり切れ", "信じられ", "詐欺",
    ],
}


def classify(text: str) -> str:
    for category, keywords in CATEGORY_RULES.items():
        if any(kw in text for kw in keywords):
            return category
    return "未分類"


# ── YouTube ────────────────────────────────────────────

def get_channel_info(youtube, channel_id: str) -> str:
    """チャンネル名を取得する。"""
    res = youtube.channels().list(id=channel_id, part="snippet").execute()
    items = res.get("items", [])
    return items[0]["snippet"]["title"] if items else channel_id


def get_channel_videos(youtube, channel_id: str, max_videos: int = 50) -> list[dict]:
    """チャンネルの最新動画（ID＋タイトル）を取得する。"""
    res = youtube.search().list(
        channelId=channel_id,
        part="id,snippet",
        order="date",
        type="video",
        maxResults=min(max_videos, 50),
    ).execute()
    videos = []
    for item in res.get("items", []):
        videos.append({
            "id":    item["id"]["videoId"],
            "title": item["snippet"]["title"],
            "url":   f"https://www.youtube.com/watch?v={item['id']['videoId']}",
        })
    # 50件超える場合はnextPageTokenで追加取得
    next_token = res.get("nextPageToken")
    while next_token and len(videos) < max_videos:
        res = youtube.search().list(
            channelId=channel_id,
            part="id,snippet",
            order="date",
            type="video",
            maxResults=min(max_videos - len(videos), 50),
            pageToken=next_token,
        ).execute()
        for item in res.get("items", []):
            videos.append({
                "id":    item["id"]["videoId"],
                "title": item["snippet"]["title"],
                "url":   f"https://www.youtube.com/watch?v={item['id']['videoId']}",
            })
        next_token = res.get("nextPageToken")
    return videos[:max_videos]


def get_comments(youtube, video: dict, max_per_video: int = 500) -> list[dict]:
    """1動画分のトップレベルコメント＋返信を取得する。"""
    comments = []
    try:
        request = youtube.commentThreads().list(
            videoId=video["id"],
            part="snippet,replies",
            maxResults=100,  # API上限は1回100件。ページネーションで上限まで取得
            order="time",    # 全コメントを時系列で取得（relevanceだと人気コメのみ）
            textFormat="plainText",
        )
        while request and len(comments) < max_per_video:
            res = request.execute()
            for item in res.get("items", []):
                # トップレベルコメント
                s = item["snippet"]["topLevelComment"]["snippet"]
                thread_id = item["id"]
                comments.append({
                    "text":         s["textDisplay"],
                    "likes":        s.get("likeCount", 0),
                    "published_at": s["publishedAt"][:10],
                    "video_title":  video["title"],
                    "video_url":    video["url"],
                    "kind":         "コメント",
                })
                # 返信（replies に含まれていれば取得）
                for reply in item.get("replies", {}).get("comments", []):
                    r = reply["snippet"]
                    comments.append({
                        "text":         r["textDisplay"],
                        "likes":        r.get("likeCount", 0),
                        "published_at": r["publishedAt"][:10],
                        "video_title":  video["title"],
                        "video_url":    video["url"],
                        "kind":         "返信",
                    })
                # 返信が多い場合（replies に全件ない場合）は Comments.list で追加取得
                total_replies = item["snippet"].get("totalReplyCount", 0)
                fetched_replies = len(item.get("replies", {}).get("comments", []))
                if total_replies > fetched_replies:
                    rep_req = youtube.comments().list(
                        parentId=thread_id,
                        part="snippet",
                        maxResults=100,
                        textFormat="plainText",
                    )
                    fetched_ids = {r["id"] for r in item.get("replies", {}).get("comments", [])}
                    while rep_req:
                        rep_res = rep_req.execute()
                        for r_item in rep_res.get("items", []):
                            if r_item["id"] not in fetched_ids:
                                r = r_item["snippet"]
                                comments.append({
                                    "text":         r["textDisplay"],
                                    "likes":        r.get("likeCount", 0),
                                    "published_at": r["publishedAt"][:10],
                                    "video_title":  video["title"],
                                    "video_url":    video["url"],
                                    "kind":         "返信",
                                })
                        rep_req = youtube.comments().list_next(rep_req, rep_res)
            request = youtube.commentThreads().list_next(request, res)
    except Exception:
        # コメント無効の動画はスキップ
        pass
    return comments


# ── Google Sheets ──────────────────────────────────────

def get_sheets_service():
    """OAuth認証してSheets APIサービスを返す。初回のみブラウザ認証。"""
    if not CREDENTIALS_FILE.exists():
        sys.exit(
            "❌ credentials.json が見つかりません。\n"
            "   Google Cloud Console でOAuth クライアントIDを作成し、\n"
            "   credentials.json をこのフォルダに置いてください。\n"
            "   詳細: README.md の「Google Sheets 認証設定」を参照"
        )
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return sheets_build("sheets", "v4", credentials=creds)


def get_or_create_spreadsheet(service, channel_name: str) -> str:
    """SPREADSHEET_ID が .env にあればそれを使う。なければ新規作成して .env に追記。"""
    if SPREADSHEET_ID:
        return SPREADSHEET_ID

    # 新規作成
    body = {
        "properties": {"title": f"YouTubeコメントリサーチ｜{channel_name}"},
        "sheets": [{"properties": {"title": "コメント一覧"}}],
    }
    res = service.spreadsheets().create(body=body).execute()
    new_id = res["spreadsheetId"]

    # .env に追記
    env_path = Path(__file__).parent / ".env"
    with open(env_path, "a") as f:
        f.write(f"\nSPREADSHEET_ID={new_id}\n")

    print(f"📊 スプレッドシート作成: https://docs.google.com/spreadsheets/d/{new_id}")
    return new_id


def get_sheet_id(service, spreadsheet_id: str, sheet_title: str) -> int | None:
    """シートタイトルからsheetId（数値）を返す。存在しなければNone。"""
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == sheet_title:
            return s["properties"]["sheetId"]
    return None


def ensure_sheet(service, spreadsheet_id: str, title: str) -> int:
    """指定タイトルのシートがなければ作成し、sheetIdを返す。"""
    sheet_id = get_sheet_id(service, spreadsheet_id, title)
    if sheet_id is not None:
        return sheet_id
    res = service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
    ).execute()
    return res["replies"][0]["addSheet"]["properties"]["sheetId"]


def clear_and_write(service, spreadsheet_id: str, sheet_title: str, rows: list[list]) -> None:
    """シートを全消去してヘッダー＋データを書き直す。"""
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_title}!A:Z",
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_title}!A1",
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()


def get_existing_video_urls(service, spreadsheet_id: str) -> set[str]:
    """コメント一覧シートにすでに存在する動画URLの集合を返す（重複チェック用）。"""
    try:
        res = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range="コメント一覧!D:D",  # 動画URL列
        ).execute()
        values = res.get("values", [])
        return {row[0] for row in values[1:] if row}  # ヘッダー行を除く
    except Exception:
        return set()


def append_comment_rows(service, spreadsheet_id: str, rows: list[list]) -> None:
    """コメント一覧シートにデータを追記する。"""
    service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range="コメント一覧!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


def upsert_summary_rows(service, spreadsheet_id: str, videos: list[dict], comment_counts: dict, channel_name: str) -> None:
    """動画別サマリーシートに今回分を追記し、末尾の合計行を更新する。"""
    ensure_sheet(service, spreadsheet_id, "動画別サマリー")
    summary_headers = ["チャンネル名", "動画タイトル", "動画URL", "コメント数"]

    # 既存データを取得
    res = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range="動画別サマリー!A:D",
    ).execute()
    existing = res.get("values", [])

    # ヘッダーがなければ書く
    if not existing or existing[0] != summary_headers:
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range="動画別サマリー!A1",
            valueInputOption="RAW",
            body={"values": [summary_headers]},
        ).execute()
        existing = [summary_headers]

    # 既存の動画URLセット（合計行は除く）
    existing_urls = {row[2] for row in existing[1:] if len(row) > 2 and row[0] != "【合計】"}

    new_rows = []
    for v in videos:
        if v["url"] not in existing_urls:
            new_rows.append([channel_name, v["title"], v["url"], comment_counts.get(v["id"], 0)])

    if new_rows:
        service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range="動画別サマリー!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": new_rows},
        ).execute()


def write_summary(service, spreadsheet_id: str, videos: list[dict], comment_counts: dict, channel_name: str = "") -> None:
    """動画別サマリーシートをリセットして書き直す（--reset 時用）。"""
    ensure_sheet(service, spreadsheet_id, "動画別サマリー")
    summary_headers = ["チャンネル名", "動画タイトル", "動画URL", "コメント数"]
    rows = [summary_headers]
    for v in videos:
        rows.append([channel_name, v["title"], v["url"], comment_counts.get(v["id"], 0)])
    clear_and_write(service, spreadsheet_id, "動画別サマリー", rows)


# ── メイン ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="YouTube コメント収集 → Google Sheets")
    parser.add_argument("--channel", required=True, help="YouTube チャンネルID")
    parser.add_argument("--videos",  type=int, default=50,  help="取得する動画数（デフォルト: 50）")
    parser.add_argument("--max",     type=int, default=500, help="1動画あたりの最大コメント数（デフォルト: 500）")
    parser.add_argument("--dry-run", action="store_true",   help="Sheetsに書かずコンソールだけに出力")
    parser.add_argument("--reset",   action="store_true",   help="シートを全消去してから書き直す（デフォルトは追記）")
    args = parser.parse_args()

    if not API_KEY:
        sys.exit("❌ YOUTUBE_API_KEY が .env にありません。")

    youtube = build("youtube", "v3", developerKey=API_KEY)

    print(f"📡 チャンネル情報を取得中…")
    channel_name = get_channel_info(youtube, args.channel)
    print(f"   チャンネル名: {channel_name}")

    print(f"📹 最新 {args.videos} 動画を取得中…")
    videos = get_channel_videos(youtube, args.channel, max_videos=args.videos)
    print(f"   {len(videos)} 件取得")

    all_rows = []
    comment_counts = {}  # video_id → コメント数
    for i, video in enumerate(videos, 1):
        comments = get_comments(youtube, video, max_per_video=args.max)
        comment_counts[video["id"]] = len(comments)
        for c in comments:
            all_rows.append([
                c["published_at"],
                channel_name,
                c["video_title"],
                c["video_url"],
                c["likes"],
                c.get("kind", "コメント"),
                classify(c["text"]),
                c["text"],
            ])
        print(f"   [{i}/{len(videos)}] {video['title'][:40]}… {len(comments)}件", end="\r")

    print(f"\n   合計 {len(all_rows)} 件取得")

    if args.dry_run:
        print("\n--- DRY RUN ---")
        for row in all_rows[:5]:
            print(row)
        print(f"（以下 {len(all_rows)-5} 件省略）")
        return

    print("🔐 Google Sheets に接続中…")
    service = get_sheets_service()
    spreadsheet_id = get_or_create_spreadsheet(service, channel_name)
    ensure_sheet(service, spreadsheet_id, "コメント一覧")

    if args.reset:
        # リセットモード：全消去して今回分だけ書き直す
        clear_and_write(service, spreadsheet_id, "コメント一覧", [HEADERS] + all_rows)
        write_summary(service, spreadsheet_id, videos, comment_counts, channel_name)
        print(f"✅ コメント一覧: {len(all_rows)} 件をリセット書き込みしました")
    else:
        # 追記モード：既存の動画URLと重複するコメントを除いて追記
        existing_urls = get_existing_video_urls(service, spreadsheet_id)
        new_rows = [r for r in all_rows if r[3] not in existing_urls]  # r[3] = 動画URL
        skip = len(all_rows) - len(new_rows)

        if not new_rows:
            print("ℹ️  新規コメントはありませんでした（すべて取得済み）")
        else:
            # ヘッダーがなければ書く
            existing = service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id, range="コメント一覧!A1:H1"
            ).execute().get("values", [])
            if not existing or existing[0] != HEADERS:
                service.spreadsheets().values().update(
                    spreadsheetId=spreadsheet_id,
                    range="コメント一覧!A1",
                    valueInputOption="RAW",
                    body={"values": [HEADERS]},
                ).execute()
            append_comment_rows(service, spreadsheet_id, new_rows)
            if skip:
                print(f"ℹ️  {skip} 件はすでに取得済みのためスキップ")
            print(f"✅ コメント一覧: {len(new_rows)} 件を追記しました")

        upsert_summary_rows(service, spreadsheet_id, videos, comment_counts, channel_name)

    print(f"✅ 動画別サマリー: {len(videos)} 動画分を書き込みました")
    print(f"📊 https://docs.google.com/spreadsheets/d/{spreadsheet_id}")


if __name__ == "__main__":
    main()
