import os
import re
import json
import sys
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://cyphers.nexon.com"
LIST_URL = f"{BASE_URL}/article/update"
STATE_FILE = "state.json"
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    )
}


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_url": None}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "last_url" not in data:
                data["last_url"] = None
            return data
    except Exception:
        return {"last_url": None}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_html(url):
    response = requests.get(url, headers=HEADERS, timeout=20)
    response.raise_for_status()
    return response.text


def unique_keep_order(items):
    seen = set()
    result = []

    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)

    return result


def extract_topic_urls(list_html):
    matches = re.findall(r'["\'](/article/update/topic/\d+)["\']', list_html)
    matches = unique_keep_order(matches)
    return [urljoin(BASE_URL, m) for m in matches]


def clean_title(text):
    text = re.sub(r"\s+", " ", text).strip()

    suffixes = [
        " - 액션본능! 사이퍼즈",
        " - 사이퍼즈 - Nexon",
        " - 사이퍼즈",
    ]
    for suffix in suffixes:
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()

    return text


def extract_title_from_soup(soup, fallback="사이퍼즈 업데이트"):
    og = soup.select_one('meta[property="og:title"]')
    if og and og.get("content"):
        return clean_title(og["content"])

    if soup.title and soup.title.get_text(strip=True):
        return clean_title(soup.title.get_text(" ", strip=True))

    for tag_name in ["h1", "h2", "h3"]:
        tag = soup.find(tag_name)
        if tag:
            text = tag.get_text(" ", strip=True)
            if text:
                return clean_title(text)

    return fallback


def is_target_update_title(title):
    """
    정기점검 업데이트만 대상으로 하고,
    퍼스트 서버/기타 공지는 제외하고 싶을 때 쓰는 필터
    """
    if "정기점검 업데이트" not in title:
        return False

    excluded_keywords = [
        "퍼스트 서버",
        "점검 안내",
        "오픈 안내",
        "이벤트",
    ]
    if any(keyword in title for keyword in excluded_keywords):
        return False

    return True


def find_latest_target_post():
    list_html = fetch_html(LIST_URL)
    urls = extract_topic_urls(list_html)

    if not urls:
        raise RuntimeError("업데이트 글 링크를 찾지 못했습니다.")

    checked = []

    # 앞쪽 몇 개만 검사하면 충분
    for url in urls[:12]:
        html = fetch_html(url)
        soup = BeautifulSoup(html, "html.parser")
        title = extract_title_from_soup(soup)
        checked.append((url, title, soup))

        if is_target_update_title(title):
            return {
                "url": url,
                "title": title,
                "soup": soup,
            }

    # 필터에 걸리는 글이 하나도 없으면 첫 글을 fallback
    first_url, first_title, first_soup = checked[0]
    return {
        "url": first_url,
        "title": first_title,
        "soup": first_soup,
    }


def article_text_lines(soup):
    text = soup.get_text("\n", strip=True)
    raw_lines = [line.strip() for line in text.splitlines()]

    lines = []
    seen = set()

    for line in raw_lines:
        if not line:
            continue
        key = re.sub(r"\s+", " ", line)
        if key in seen:
            continue
        seen.add(key)
        lines.append(key)

    return lines


def split_sections(lines):
    sections = {
        "system": [],
        "balance": [],
        "etc": [],
    }

    current = None

    for line in lines:
        upper = line.upper()

        if "SYSTEM" in upper and "시스템" in line:
            current = "system"
            continue
        elif "BALANCE" in upper:
            current = "balance"
            continue
        elif "ETC" in upper:
            current = "etc"
            continue

        if current:
            sections[current].append(line)

    return sections


def cleanup_lines(lines, limit=5):
    skip_contains = [
        "안녕하세요, 능력자 여러분",
        "아래는",
        "액션본능! 사이퍼즈",
        "개발자 코멘트",
        "새소식",
        "공지사항",
        "업데이트",
        "매거진",
        "리그안내",
        "이벤트",
        "개인정보처리방침",
        "청소년보호정책",
        "운영정책",
        "사업자등록번호",
        "통신판매업",
        "All Rights Reserved",
    ]

    skip_exact = {
        "시스템",
        "캐릭터 밸런스",
        "버그 수정 및 개선 사항",
        "SYSTEM",
        "BALANCE",
        "ETC",
    }

    cleaned = []
    seen = set()

    for line in lines:
        line = line.strip()
        line = re.sub(r"^[\*\-•\s]+", "", line).strip()

        if not line:
            continue

        if line in skip_exact:
            continue

        if any(keyword in line for keyword in skip_contains):
            continue

        if line.startswith("http://") or line.startswith("https://"):
            continue

        if len(line) < 2:
            continue

        # 너무 긴 문장은 잘라서 Discord embed 부담 줄이기
        if len(line) > 180:
            line = line[:177] + "..."

        key = line[:80]
        if key in seen:
            continue

        seen.add(key)
        cleaned.append(line)

        if len(cleaned) >= limit:
            break

    if not cleaned:
        return ["상세 내용은 원문 링크를 확인해 주세요."]

    return cleaned


def build_summary_text(lines, limit=4, max_len=1000):
    picked = cleanup_lines(lines, limit=limit)
    text = "\n".join(f"• {line}" for line in picked)
    return text[:max_len]


def parse_post(post):
    soup = post["soup"]
    title = post["title"]
    url = post["url"]

    lines = article_text_lines(soup)
    sections = split_sections(lines)

    system_text = build_summary_text(sections["system"], limit=4)
    balance_text = build_summary_text(sections["balance"], limit=5)
    etc_text = build_summary_text(sections["etc"], limit=4)

    return {
        "title": title,
        "url": url,
        "system": system_text[:1024],
        "balance": balance_text[:1024],
        "etc": etc_text[:1024],
    }


def make_payload(post):
    return {
        "username": "사이퍼즈 업데이트 알리미",
        "content": f"새 업데이트 감지: {post['title']}\n{post['url']}",
        "embeds": [
            {
                "title": post["title"][:256],
                "url": post["url"],
                "description": "사이퍼즈 공식 업데이트 새 글을 감지해 자동으로 정리했습니다.",
                "color": 15158332,
                "fields": [
                    {
                        "name": "시스템",
                        "value": post["system"] or "변경 사항 없음",
                        "inline": False
                    },
                    {
                        "name": "밸런스",
                        "value": post["balance"] or "변경 사항 없음",
                        "inline": False
                    },
                    {
                        "name": "버그 수정 / 기타",
                        "value": post["etc"] or "변경 사항 없음",
                        "inline": False
                    },
                ],
                "footer": {
                    "text": "제목을 누르면 원문으로 이동합니다."
                }
            }
        ]
    }


def send_to_discord(payload):
    if not WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL 이 비어 있습니다. GitHub Secret을 확인하세요.")

    response = requests.post(WEBHOOK_URL, json=payload, timeout=20)
    if response.status_code not in (200, 204):
        raise RuntimeError(
            f"Discord 전송 실패: {response.status_code} / {response.text}"
        )


def main():
    state = load_state()
    latest_post = find_latest_target_post()
    parsed_post = parse_post(latest_post)

    if parsed_post["url"] == state.get("last_url"):
        print("새 업데이트 없음")
        return

    send_to_discord(make_payload(parsed_post))
    state["last_url"] = parsed_post["url"]
    save_state(state)
    print("새 업데이트 전송 완료:", parsed_post["url"])


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("오류 발생:", e)
        sys.exit(1)
