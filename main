import os
import re
import json
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from urllib.parse import urljoin

load_dotenv()

BASE_URL = "https://cyphers.nexon.com"
LIST_URL = f"{BASE_URL}/article/update"
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
STATE_FILE = "state.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
}


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_url": None}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_url": None}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_html(url):
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def extract_latest_post_url(list_html):
    """
    목록 페이지 HTML에서 /article/update/topic/{id} 형태의 첫 번째 링크를 찾음
    """
    matches = re.findall(r'href=["\'](/article/update/topic/\d+)["\']', list_html)
    if not matches:
        raise RuntimeError("업데이트 글 링크를 찾지 못했습니다.")

    # 중복 제거 후 첫 번째 링크 사용
    seen = set()
    unique = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            unique.append(m)

    return urljoin(BASE_URL, unique[0])


def extract_title(soup, fallback_url):
    # 1순위: og:title
    og = soup.select_one('meta[property="og:title"]')
    if og and og.get("content"):
        return og["content"].strip()

    # 2순위: title
    if soup.title and soup.title.get_text(strip=True):
        return soup.title.get_text(strip=True)

    # 3순위: h1/h2
    for tag in ["h1", "h2", "h3"]:
        el = soup.find(tag)
        if el:
            text = el.get_text(" ", strip=True)
            if text:
                return text

    return f"사이퍼즈 업데이트 ({fallback_url.rsplit('/', 1)[-1]})"


def article_text_lines(soup):
    text = soup.get_text("\n", strip=True)
    raw_lines = [line.strip() for line in text.splitlines()]
    lines = []

    for line in raw_lines:
        if not line:
            continue
        if line in lines:
            continue
        lines.append(line)

    return lines


def split_sections(lines):
    sections = {
        "system": [],
        "balance": [],
        "etc": []
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
    skip_keywords = [
        "안녕하세요, 능력자 여러분",
        "아래는",
        "액션본능! 사이퍼즈",
        "개발자 코멘트",
        "새소식",
        "업데이트",
    ]

    cleaned = []
    for line in lines:
        line = line.strip()

        if not line:
            continue

        if any(keyword in line for keyword in skip_keywords):
            continue

        # 너무 짧은 라인 제거
        if len(line) < 2:
            continue

        # 섹션 헤더 중복 제거
        if line.upper() in ["SYSTEM", "BALANCE", "ETC"]:
            continue

        # bullet 모양 정리
        line = re.sub(r"^[\*\-•\s]+", "", line).strip()

        # HTML 표에서 잘게 쪼개진 쓰레기 라인 방지
        if line in ["시스템", "캐릭터 밸런스", "버그 수정 및 개선 사항"]:
            continue

        cleaned.append(line)

    # 비슷한 중복 제거
    result = []
    seen = set()
    for line in cleaned:
        key = line[:60]
        if key in seen:
            continue
        seen.add(key)
        result.append(line)
        if len(result) >= limit:
            break

    if not result:
        return ["상세 내용은 원문 링크를 확인해 주세요."]

    return result


def build_summary_text(lines, limit=4, max_len=900):
    picked = cleanup_lines(lines, limit=limit)
    text = "\n".join(f"• {line}" for line in picked)
    return text[:max_len]


def parse_post(post_url):
    html = fetch_html(post_url)
    soup = BeautifulSoup(html, "html.parser")

    title = extract_title(soup, post_url)
    lines = article_text_lines(soup)
    sections = split_sections(lines)

    return {
        "title": title,
        "url": post_url,
        "system": build_summary_text(sections["system"], limit=4),
        "balance": build_summary_text(sections["balance"], limit=5),
        "etc": build_summary_text(sections["etc"], limit=4),
    }


def make_payload(post):
    return {
        "username": "사이퍼즈 업데이트 알리미",
        "embeds": [
            {
                "title": post["title"][:256],
                "url": post["url"],
                "description": "사이퍼즈 공식 업데이트 새 글을 감지해 자동으로 정리했습니다.",
                "color": 0xE67E22,
                "fields": [
                    {
                        "name": "시스템",
                        "value": post["system"][:1024],
                        "inline": False
                    },
                    {
                        "name": "밸런스",
                        "value": post["balance"][:1024],
                        "inline": False
                    },
                    {
                        "name": "버그 수정 / 기타",
                        "value": post["etc"][:1024],
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
        raise RuntimeError("DISCORD_WEBHOOK_URL 이 설정되지 않았습니다.")

    resp = requests.post(WEBHOOK_URL, json=payload, timeout=20)
    resp.raise_for_status()


def main():
    state = load_state()

    list_html = fetch_html(LIST_URL)
    latest_url = extract_latest_post_url(list_html)

    if latest_url == state.get("last_url"):
        print("새 업데이트 없음")
        return

    post = parse_post(latest_url)
    payload = make_payload(post)
    send_to_discord(payload)

    state["last_url"] = latest_url
    save_state(state)
    print("새 업데이트 전송 완료:", latest_url)


if __name__ == "__main__":
    main()
