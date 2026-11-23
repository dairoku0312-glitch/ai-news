#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AIニュース自動収集・要約スクリプト（標準ライブラリのみ）

処理の流れ:
  1. ニュース各社の公式 RSS（Yahoo!ニュース / NHK / ITmedia）から候補記事を収集し、
     タイトルのキーワードでカテゴリ分類する
     ※ Google News RSS の記事リンクはサーバー環境から元記事URLへ解決できず
       本文取得が不可能なため、元記事へ直接リンクする RSS のみを使う
  2. 既掲載URL・タイトルで重複排除し、公式/大手メディアと新しさを優先して順位付け
  3. 記事本文を取得し、抽出テキストが 400 文字未満ならその記事はスキップ
     （タイトルだけで要約することは絶対にしない）
  4. Gemini API（無料枠）の REST で要約を生成。失敗・skip 応答なら記事をスキップ
  5. 成功したら data/news.json に当日分を追加。失敗したら前日分を残したまま
     status="error" とエラー内容だけを記録する

APIキーは環境変数 GEMINI_API_KEY からのみ読む（コードに秘密は書かない）。
キーが無い場合は収集・本文チェックだけを行い、件数を表示して
news.json を変更せずに異常終了する。
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

JST = timezone(timedelta(hours=9))

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_PATH = os.path.join(BASE_DIR, "data", "news.json")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
             "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 ai-news-bot/1.0"

MIN_BODY_CHARS = 400        # 本文がこれ未満なら記事をスキップ
MAX_BODY_CHARS = 6000       # Gemini に送る本文の上限
PER_CATEGORY_CAP = 3        # 1カテゴリあたりの掲載上限
TOTAL_TARGET = 8            # 1日の掲載目標件数
PER_CATEGORY_POOL = 10      # 順位付け後に残す候補プール（カテゴリごと）
MAX_DAYS = 30               # news.json に保持する日数

# 収集元 RSS: (URL, デフォルトカテゴリ, 情報源名)
# デフォルトカテゴリが None のフィードは「プール」として扱い、
# キーワードに一致した記事だけを採用する（無関係な記事を載せないため）。
DIRECT_FEEDS = [
    ("https://news.yahoo.co.jp/rss/topics/top-picks.xml", None, "Yahoo!ニュース"),
    ("https://news.yahoo.co.jp/rss/topics/domestic.xml", None, "Yahoo!ニュース"),
    ("https://news.yahoo.co.jp/rss/topics/world.xml", "海外ニュース", "Yahoo!ニュース"),
    ("https://news.yahoo.co.jp/rss/topics/business.xml", "経済・ビジネス", "Yahoo!ニュース"),
    ("https://news.yahoo.co.jp/rss/topics/it.xml", "AI・IT", "Yahoo!ニュース"),
    ("https://news.yahoo.co.jp/rss/topics/science.xml", None, "Yahoo!ニュース"),
    ("https://www.nhk.or.jp/rss/news/cat0.xml", None, "NHKニュース"),
    ("https://www.nhk.or.jp/rss/news/cat1.xml", None, "NHKニュース"),
    ("https://www.nhk.or.jp/rss/news/cat4.xml", "国内政治", "NHKニュース"),
    ("https://www.nhk.or.jp/rss/news/cat5.xml", "経済・ビジネス", "NHKニュース"),
    ("https://www.nhk.or.jp/rss/news/cat6.xml", "海外ニュース", "NHKニュース"),
    ("https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml", "AI・IT", "ITmedia"),
]

# キーワード分類ルール（上から順に判定し、最初に一致したカテゴリを採用）
KEYWORD_RULES = [
    ("外国人材・特定技能・在留資格",
     ["特定技能", "在留資格", "技能実習", "育成就労", "外国人材", "外国人労働者",
      "入管", "在留外国人", "外国人雇用"]),
    ("介護・福祉",
     ["介護", "福祉", "ケアマネ", "デイサービス", "訪問介護", "介護保険",
      "障害福祉", "社会保障", "社会福祉"]),
    ("AI・IT",
     ["生成AI", "人工知能", "AI", "ＡＩ", "IT", "ＩＴ", "半導体", "ロボット",
      "データセンター", "サイバー", "デジタル"]),
    ("国内政治",
     ["政府", "国会", "内閣", "首相", "総理", "大臣", "与党", "野党", "選挙",
      "法案", "予算案", "自民党", "立憲民主党", "政権", "閣議"]),
    ("経済・ビジネス",
     ["経済", "景気", "株価", "日経平均", "円安", "円高", "金利", "物価",
      "賃上げ", "倒産", "決算", "市場"]),
    ("海外ニュース",
     ["米国", "アメリカ", "中国", "韓国", "ロシア", "ウクライナ", "欧州",
      "EU", "トランプ", "台湾", "北朝鮮", "中東", "イスラエル"]),
]

# プール記事を採用する際の鮮度（これより古い記事は対象外）
MAX_AGE_DAYS = 2
# 介護・福祉と外国人材は報道頻度が低く一般フィードでは2日以内に無い日が多いため、
# この2カテゴリだけ少し広めに取る（公開日はカードに明示される）
NICHE_CATEGORIES = {"介護・福祉", "外国人材・特定技能・在留資格"}
NICHE_MAX_AGE_DAYS = 7

# 優先するドメイン（公式・大手）
OFFICIAL_SUFFIX = ".go.jp"
MAJOR_DOMAINS = {
    "nhk.or.jp", "www.nhk.or.jp", "news.web.nhk",
    "news.yahoo.co.jp", "itmedia.co.jp", "www.itmedia.co.jp",
    "nikkei.com", "www.nikkei.com", "asahi.com", "www.asahi.com",
    "mainichi.jp", "yomiuri.co.jp", "www.yomiuri.co.jp",
    "sankei.com", "www.sankei.com", "jiji.com", "www.jiji.com",
    "kyodonews.jp", "47news.jp", "reuters.com", "jp.reuters.com",
    "bloomberg.co.jp", "toyokeizai.net", "diamond.jp",
}

# 要約に使うモデル候補（新しい順）。実際に使えるモデルは実行時に
# ListModels で確認してから選ぶ（モデル名の世代交代で 404 になるのを防ぐ）。
PREFERRED_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]
_env_model = os.environ.get("GEMINI_MODEL", "").strip()
if _env_model:
    PREFERRED_MODELS = [_env_model] + [m for m in PREFERRED_MODELS if m != _env_model]

_model_candidates_cache = None


def resolve_model_candidates(api_key):
    """ListModels で generateContent 対応モデルを確認し、優先順の候補を返す。"""
    global _model_candidates_cache
    if _model_candidates_cache is not None:
        return _model_candidates_cache
    available = []
    try:
        req = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": api_key, "User-Agent": USER_AGENT},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            listing = json.loads(resp.read().decode("utf-8"))
        for m in listing.get("models", []):
            methods = m.get("supportedGenerationMethods") or []
            if "generateContent" in methods:
                available.append(str(m.get("name", "")).replace("models/", ""))
    except Exception as e:
        print(f"[warn] モデル一覧の取得に失敗（候補リストで続行）: {e}")
    if available:
        ordered = [m for m in PREFERRED_MODELS if m in available]
        extras = [m for m in available
                  if m not in ordered and "flash" in m and "image" not in m]
        _model_candidates_cache = ordered + extras
    else:
        _model_candidates_cache = list(PREFERRED_MODELS)
    print(f"[info] モデル候補: {_model_candidates_cache}")
    return _model_candidates_cache

PROMPT_TEMPLATE = """あなたはニュース要約アシスタントです。以下の記事本文だけを使って要約してください。

厳守事項:
- 記事本文に書かれていない情報を絶対に付け加えないこと。推測や一般論で補わないこと。
- 制度に関する記事では、段階が本文から読み取れる場合のみ policy_status に
  「検討中」「決定済み」「施行済み」のいずれかを入れること。読み取れなければ空文字。
- impact は、介護・福祉事業や外国人材の受け入れ業務への影響が本文から読み取れる場合のみ書き、
  読み取れなければ空文字にすること。
- 本文の情報量が少なく要約できない場合は {{"skip": true}} だけを返すこと。

出力は次の JSON のみ（説明文やコードフェンスは付けない）:
{{"summary": "2〜3文の日本語要約", "points": ["重要ポイント（最大3つ）"],
  "impact": "", "policy_status": ""}}

記事タイトル: {title}
情報源: {source}
記事本文:
{body}
"""


# ---------------------------------------------------------------- ユーティリティ

def now_jst():
    return datetime.now(JST)


def iso_jst(dt):
    return dt.astimezone(JST).isoformat(timespec="seconds")


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.geturl(), resp.read()


def load_data():
    if os.path.exists(DATA_PATH):
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}
    data.setdefault("last_success_at", None)
    data.setdefault("last_attempt_at", None)
    data.setdefault("status", "init")
    data.setdefault("error", None)
    data.setdefault("days", [])
    return data


def save_data(data):
    tmp = DATA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_PATH)


def record_failure(data, message):
    """前日分（days）は一切変更せず、試行結果だけを記録する。"""
    data["last_attempt_at"] = iso_jst(now_jst())
    data["status"] = "error"
    data["error"] = (message or "unknown error")[:300]
    save_data(data)


# ---------------------------------------------------------------- RSS 収集

def parse_pubdate(text):
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
        if dt is not None and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _child_text(item, names):
    """名前空間付きでも拾えるよう、タグ末尾の名前で子要素を探す。"""
    for child in item:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag in names and child.text:
            return child.text.strip()
    return ""


def fetch_feed(url):
    _, raw = http_get(url, timeout=20)
    root = ET.fromstring(raw)
    items = []
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1] != "item":
            continue
        title = _child_text(item, ("title",))
        link = _child_text(item, ("link",))
        if not title or not link:
            continue
        items.append({
            "title": title,
            "link": link,
            "source": _child_text(item, ("source", "creator")),
            "description": _child_text(item, ("description",)),
            "published": parse_pubdate(
                _child_text(item, ("pubDate", "date", "published"))),
        })
    return items


# 概要文で分類してよいのは、この強いキーワードに一致した場合だけ
# （概要文は言及が広く、誤分類しやすいため）
STRONG_KEYWORDS = {
    "外国人材・特定技能・在留資格":
        ["特定技能", "在留資格", "技能実習", "育成就労", "外国人材"],
    "介護・福祉":
        ["介護保険", "介護報酬", "介護施設", "訪問介護", "デイサービス", "ケアマネ"],
    "AI・IT": ["生成AI", "人工知能"],
    "国内政治": ["国会", "内閣", "法案"],
    "経済・ビジネス": ["日経平均", "賃上げ"],
    "海外ニュース": ["ウクライナ", "トランプ"],
}


def classify(title, description, default_category):
    """タイトルのキーワードでカテゴリを決める。タイトルで決まらない場合のみ、
    概要文の強いキーワードでも判定する。
    プール（default=None）で一致しなければ None（採用しない）。"""
    for category, keywords in KEYWORD_RULES:
        if any(k in title for k in keywords):
            return category
    desc = description or ""
    if desc:
        for category, keywords in STRONG_KEYWORDS.items():
            if any(k in desc for k in keywords):
                return category
    return default_category


def domain_of(url):
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


def rank_score(item):
    """公式ドメイン・大手メディアを優先し、同格なら新しい記事を優先する。"""
    domain = domain_of(item["link"])
    bonus = 0
    if domain.endswith(OFFICIAL_SUFFIX):
        bonus = 2
    elif domain in MAJOR_DOMAINS:
        bonus = 1
    ts = item["published"].timestamp() if item["published"] else 0
    return (bonus, ts)


def collect_candidates(existing_urls):
    """全フィードを集めてカテゴリ分類し、カテゴリごとの候補（順位付け済み）を返す。"""
    per_category = {cat: [] for cat, _ in KEYWORD_RULES}
    seen_links = set(existing_urls)
    seen_titles = set()
    now = now_jst()
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    niche_cutoff = now - timedelta(days=NICHE_MAX_AGE_DAYS)
    for url, default_category, feed_source in DIRECT_FEEDS:
        try:
            items = fetch_feed(url)
        except Exception as e:
            print(f"[warn] フィード取得失敗 ({feed_source} {url}): {e}",
                  file=sys.stderr)
            continue
        for it in items:
            if it["link"] in seen_links or it["title"] in seen_titles:
                continue
            category = classify(it["title"], it["description"], default_category)
            if category is None:
                continue
            limit = niche_cutoff if category in NICHE_CATEGORIES else cutoff
            if it["published"] is not None and it["published"] < limit:
                continue
            seen_links.add(it["link"])
            seen_titles.add(it["title"])
            it["category"] = category
            if not it["source"]:
                it["source"] = feed_source
            per_category[category].append(it)
    for category in per_category:
        per_category[category].sort(key=rank_score, reverse=True)
        per_category[category] = per_category[category][:PER_CATEGORY_POOL]
        print(f"[info] {category}: 候補 {len(per_category[category])} 件")
    return per_category


def interleave(per_category):
    """カテゴリを横断してラウンドロビンに並べる（各カテゴリ最低1件を優先）。"""
    queues = {c: list(v) for c, v in per_category.items() if v}
    ordered = []
    while queues:
        for cat in list(queues.keys()):
            if queues[cat]:
                ordered.append(queues[cat].pop(0))
            if not queues[cat]:
                del queues[cat]
    return ordered


# ---------------------------------------------------------------- 本文取得

class ParagraphExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._in_p = False
        self._buf = []
        self.paragraphs = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip_depth += 1
        elif tag == "p" and self._skip_depth == 0:
            self._in_p = True
            self._buf = []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "p" and self._in_p:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if text:
                self.paragraphs.append(text)
            self._in_p = False
            self._buf = []

    def handle_data(self, data):
        if self._in_p and self._skip_depth == 0:
            self._buf.append(data)


def _extract_paragraphs(html):
    parser = ParagraphExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    return "\n".join(parser.paragraphs)


def fetch_body(url):
    """記事ページを取得し、<p> テキストを抽出する。
    Yahoo!ニュースの pickup ページは要約ページなので、本文ページ
    （articles/...）へのリンクがあればそちらを優先して取得する。
    戻り値: (最終URL, 抽出テキスト)"""
    final_url, raw = http_get(url, timeout=15)
    html = raw.decode("utf-8", errors="replace")
    if "news.yahoo.co.jp/pickup" in final_url:
        m = re.search(r'href="(https://news\.yahoo\.co\.jp/articles/[0-9a-f]+)',
                      html)
        if m:
            try:
                art_url, art_raw = http_get(m.group(1), timeout=15)
                art_text = _extract_paragraphs(
                    art_raw.decode("utf-8", errors="replace"))
                if art_text:
                    return art_url, art_text
            except Exception:
                pass
    return final_url, _extract_paragraphs(html)


# ---------------------------------------------------------------- Gemini 要約

class TransientGeminiError(Exception):
    """503/429 など一時的な失敗。全モデル・リトライを使い切った場合に投げる。"""


def summarize(api_key, title, source, body):
    """Gemini REST で要約。成功時は dict、skip/失敗時は None を返す。"""
    prompt = PROMPT_TEMPLATE.format(
        title=title, source=source, body=body[:MAX_BODY_CHARS])
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2},
    }
    result = None
    last_error = None
    for model in resolve_model_candidates(api_key):
        url = ("https://generativelanguage.googleapis.com/v1beta/models/" +
               model + ":generateContent")
        # 503（混雑）は少し待って再試行、429（上限）は長めに待って1回だけ再試行、
        # 404（モデル無し）は即次の候補へ。それ以外は即エラー。
        attempts = 0
        while attempts < 3:
            attempts += 1
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": api_key,
                    "User-Agent": USER_AGENT,
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                last_error = e
                if e.code == 404:
                    break  # 次のモデル候補へ
                if e.code == 503 and attempts < 3:
                    print(f"[warn] Gemini 503（{model}）→ 10秒待って再試行")
                    time.sleep(10)
                    continue
                if e.code == 429 and attempts < 2:
                    print(f"[warn] Gemini 429（{model}）→ 30秒待って再試行")
                    time.sleep(30)
                    continue
                if e.code in (503, 429):
                    break  # このモデルは諦めて次の候補へ
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                # 応答タイムアウト・接続エラーも一時的な失敗として再試行する
                last_error = e
                if attempts < 2:
                    print(f"[warn] Gemini応答なし（{model}）: {e} → 10秒待って再試行")
                    time.sleep(10)
                    continue
                break  # 次のモデル候補へ
        if result is not None:
            break
    if result is None:
        if last_error is not None and (
                getattr(last_error, "code", None) in (503, 429)
                or isinstance(last_error, (urllib.error.URLError, TimeoutError, ConnectionError))):
            raise TransientGeminiError(str(last_error))
        if last_error is not None:
            raise last_error
        raise RuntimeError("利用できるGeminiモデルが見つかりませんでした")

    text = ""
    try:
        parts = result["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
    except Exception:
        return None
    # コードフェンスが付いていたら外す
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except Exception:
        return None
    if not isinstance(parsed, dict) or parsed.get("skip"):
        return None
    summary = str(parsed.get("summary", "")).strip()
    if not summary:
        return None
    points = parsed.get("points") or []
    points = [str(p).strip() for p in points if str(p).strip()][:3]
    policy = str(parsed.get("policy_status", "")).strip()
    if policy not in ("検討中", "決定済み", "施行済み"):
        policy = ""
    return {
        "summary": summary,
        "points": points,
        "impact": str(parsed.get("impact", "")).strip(),
        "policy_status": policy,
    }


# ---------------------------------------------------------------- メイン

def main():
    data = load_data()
    existing_urls = set()
    for day in data.get("days", []):
        for item in day.get("items", []):
            if item.get("url"):
                existing_urls.add(item["url"])

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    collect_only = not api_key

    per_category = collect_candidates(existing_urls)
    ordered = interleave(per_category)
    print(f"[info] 候補合計 {len(ordered)} 件（重複排除・順位付け後）")

    accepted = []
    per_cat_count = {}
    body_passed = 0
    consecutive_transient = 0
    for cand in ordered:
        if len(accepted) >= TOTAL_TARGET:
            break
        cat = cand["category"]
        if per_cat_count.get(cat, 0) >= PER_CATEGORY_CAP:
            continue
        try:
            final_url, body = fetch_body(cand["link"])
        except Exception as e:
            print(f"[skip] 本文取得失敗: {cand['title']} ({e})")
            continue
        if len(body) < MIN_BODY_CHARS:
            print(f"[skip] 本文不足 ({len(body)}文字): {cand['title']}")
            continue
        body_passed += 1

        if collect_only:
            # キーが無い場合は要約せず「要約可能だった件数」として数えるだけ
            accepted.append(cand)
            per_cat_count[cat] = per_cat_count.get(cat, 0) + 1
            continue

        try:
            result = summarize(api_key, cand["title"], cand["source"], body)
        except TransientGeminiError as e:
            # 一時的な混雑・上限：この記事だけ諦めて次へ（全部ダメなら最後に0件で失敗記録）
            print(f"[skip] Gemini一時エラー（混雑/上限）: {cand['title']} ({e})")
            consecutive_transient += 1
            if consecutive_transient >= 3:
                record_failure(data, f"Geminiが応答しません（混雑/タイムアウトが継続）: {e}")
                print("[error] Geminiの一時エラーが3件続いたため中断しました。")
                return 1
            continue
        except Exception as e:
            print(f"[error] Gemini 呼び出し失敗: {e}")
            record_failure(data, f"Gemini API error: {e}")
            return 1
        consecutive_transient = 0
        if result is None:
            print(f"[skip] 要約不可/skip応答: {cand['title']}")
            continue

        accepted.append({
            "title": cand["title"],
            "url": final_url,
            "category": cat,
            "summary": result["summary"],
            "points": result["points"],
            "impact": result["impact"],
            "policy_status": result["policy_status"],
            "source": cand["source"],
            "published_at": iso_jst(cand["published"]) if cand["published"] else "",
        })
        per_cat_count[cat] = per_cat_count.get(cat, 0) + 1
        print(f"[ok] 掲載: {cand['title']}")

    print(f"[info] 本文チェック通過: {body_passed} 件 / 採用: {len(accepted)} 件")

    if collect_only:
        print("GEMINI_API_KEY not set: 収集・本文チェックのみ実施しました。"
              "要約は行わず、data/news.json は変更しません。")
        return 2

    if not accepted:
        record_failure(data, "掲載できる記事が0件でした（収集・本文取得・要約のいずれかで全件スキップ）")
        print("[error] 掲載記事0件のため失敗として記録しました。")
        return 1

    today = now_jst().strftime("%Y-%m-%d")
    days = [d for d in data["days"] if d.get("date") != today]
    days.insert(0, {"date": today, "items": accepted})
    data["days"] = days[:MAX_DAYS]
    ts = iso_jst(now_jst())
    data["last_success_at"] = ts
    data["last_attempt_at"] = ts
    data["status"] = "ok"
    data["error"] = None
    save_data(data)
    print(f"[done] {today} のニュース {len(accepted)} 件を保存しました。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        # 予期しない例外でも、失敗状態をサイトに伝えるため記録を試みる
        try:
            record_failure(load_data(), f"unexpected error: {e}")
        except Exception:
            pass
        print(f"[fatal] {e}", file=sys.stderr)
        sys.exit(1)
