"""
YouTube コメント収集スクリプト
対象チャンネルの動画からコメントを取得し、
会社員制約リサーチフレーム.html の PART 5 テーブルに追記する。

使い方:
  python fetch_comments.py --channel CHANNEL_ID [--max 100] [--keywords "使えない,禁止,制約"]
"""

import os
import re
import sys
import json
import argparse
from datetime import date
from pathlib import Path
from googleapiclient.discovery import build
from dotenv import load_dotenv

load_dotenv()

HTML_PATH = Path(__file__).parent / "会社員制約リサーチフレーム.html"
API_KEY = os.getenv("YOUTUBE_API_KEY")

# 制約カテゴリの自動分類キーワード
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
    """テキストを制約カテゴリに分類する。複数ヒットは最初のもの。"""
    for category, keywords in CATEGORY_RULES.items():
        if any(kw in text for kw in keywords):
            return category
    return "未分類"


def get_channel_videos(youtube, channel_id: str, max_videos: int = 20) -> list[str]:
    """チャンネルの最新動画IDを取得する。"""
    res = youtube.search().list(
        channelId=channel_id,
        part="id",
        order="date",
        type="video",
        maxResults=max_videos,
    ).execute()
    return [item["id"]["videoId"] for item in res.get("items", [])]


def get_comments(youtube, video_id: str, max_comments: int = 100) -> list[dict]:
    """1動画分のトップレベルコメントを取得する。"""
    comments = []
    request = youtube.commentThreads().list(
        videoId=video_id,
        part="snippet",
        maxResults=min(max_comments, 100),
        order="relevance",
        textFormat="plainText",
    )
    while request and len(comments) < max_comments:
        res = request.execute()
        for item in res.get("items", []):
            snippet = item["snippet"]["topLevelComment"]["snippet"]
            comments.append({
                "text": snippet["textDisplay"],
                "published_at": snippet["publishedAt"][:10],
                "video_id": video_id,
            })
        request = youtube.commentThreads().list_next(request, res)
    return comments


def filter_by_keywords(comments: list[dict], keywords: list[str]) -> list[dict]:
    """指定キーワードを含むコメントだけ返す。キーワード未指定なら全件。"""
    if not keywords:
        return comments
    return [c for c in comments if any(kw in c["text"] for kw in keywords)]


def build_table_rows(comments: list[dict], source_prefix: str) -> str:
    """HTML の <tr> 文字列を生成する。"""
    rows = []
    today = date.today().isoformat()
    for c in comments:
        category = classify(c["text"])
        source = f"YouTube ({source_prefix})"
        text = c["text"].replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        # 長すぎるコメントは省略
        if len(text) > 200:
            text = text[:197] + "…"
        row = (
            f'        <tr>\n'
            f'          <td style="padding:10px 12px; border-bottom:1px solid #1e1e1e;">{today}</td>\n'
            f'          <td style="padding:10px 12px; border-bottom:1px solid #1e1e1e;">{category}</td>\n'
            f'          <td style="padding:10px 12px; border-bottom:1px solid #1e1e1e;">{source}</td>\n'
            f'          <td style="padding:10px 12px; border-bottom:1px solid #1e1e1e;">{text}</td>\n'
            f'        </tr>'
        )
        rows.append(row)
    return "\n".join(rows)


def inject_into_html(new_rows: str) -> None:
    """HTML の PART 5 テーブルにある「まだ収集なし」行を削除して新行を挿入する。"""
    html = HTML_PATH.read_text(encoding="utf-8")

    # プレースホルダー行を削除
    placeholder_pattern = re.compile(
        r'\s*<tr>\s*<td[^>]*>—</td>\s*<td[^>]*>—</td>\s*<td[^>]*>—</td>'
        r'\s*<td[^>]*>\（まだ収集なし.*?\）</td>\s*</tr>',
        re.DOTALL,
    )
    html = placeholder_pattern.sub("", html)

    # </tbody> の直前に追記
    if "</tbody>" not in html:
        print("⚠️  </tbody> が見つかりません。HTMLを確認してください。", file=sys.stderr)
        return

    html = html.replace("</tbody>", f"{new_rows}\n      </tbody>", 1)
    HTML_PATH.write_text(html, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="YouTube コメント収集 → HTML 追記")
    parser.add_argument("--channel", required=True, help="YouTube チャンネル ID")
    parser.add_argument("--max", type=int, default=50, help="取得コメント最大数（デフォルト: 50）")
    parser.add_argument(
        "--keywords",
        default="",
        help="絞り込みキーワード（カンマ区切り）。未指定なら全コメント対象",
    )
    parser.add_argument("--videos", type=int, default=10, help="対象にする最新動画数（デフォルト: 10）")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="HTMLに書き込まずコンソールだけに出力する",
    )
    args = parser.parse_args()

    if not API_KEY:
        sys.exit("❌ YOUTUBE_API_KEY が .env に設定されていません。")

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]

    youtube = build("youtube", "v3", developerKey=API_KEY)

    print(f"📡 チャンネル {args.channel} の最新 {args.videos} 動画を取得中…")
    video_ids = get_channel_videos(youtube, args.channel, max_videos=args.videos)
    print(f"   動画 {len(video_ids)} 件取得")

    all_comments = []
    for vid in video_ids:
        comments = get_comments(youtube, vid, max_comments=args.max // len(video_ids) + 1)
        all_comments.extend(comments)

    filtered = filter_by_keywords(all_comments, keywords)
    print(f"   コメント {len(all_comments)} 件取得 → キーワード絞り込み後 {len(filtered)} 件")

    if not filtered:
        print("ℹ️  条件に合うコメントがありませんでした。")
        return

    new_rows = build_table_rows(filtered, source_prefix=args.channel)

    if args.dry_run:
        print("\n--- DRY RUN: 追記予定 HTML ---")
        print(new_rows)
        return

    inject_into_html(new_rows)
    print(f"✅ {len(filtered)} 件を 会社員制約リサーチフレーム.html に追記しました。")


if __name__ == "__main__":
    main()
