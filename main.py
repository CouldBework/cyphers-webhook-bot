import os
import re
import json
import textwrap
from collections import OrderedDict
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

TIMEOUT = 20
MAX_FIELD_VALUE = 1024
MAX_FIELDS_PER_EMBED = 4


def normalize_line(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def dedupe_consecutive(lines):
    result = []
    prev = None
    for line in lines:
        if line != prev:
            result.append(line)
        prev = line
    return result


def unique_keep_order(items):
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def wrap_text(text, initial_indent="", subsequent_indent="", width=44):
    return textwrap.fill(
        text,
        width=width,
        initial_indent=initial_indent,
        subsequent_indent=subsequent_indent,
        break_long_words=False,
        break_on_hyphens=False,
    )


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
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.text


def should_skip_line(line: str) -> bool:
    if not line:
        return True

    skip_exact = {
        "목록",
        "댓글",
        "공유",
        "닫기",
    }
    skip_contains = [
        "상세 변경 내용은 원문 확인",
        "이 글을 SNS로 공유하기",
        "본문 바로가기",
        "푸터 바로가기",
        "네비게이션",
        "copyright",
        "COPYRIGHT",
    ]

    if line in skip_exact:
        return True

    lowered = line.lower()
    for token in skip_contains:
        if token.lower() in lowered:
            return True

    return False


def extract_topic_urls(html_text):
    urls = re.findall(r"/article/update/topic/\d+", html_text)
    result = []
    seen = set()
    for u in urls:
        full = urljoin(BASE_URL, u)
        if full not in seen:
            seen.add(full)
            result.append(full)
    return result


def clean_title(text):
    text = normalize_line(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_title_from_soup(soup):
    selectors = [
        "h1",
        ".title",
        ".subject",
        ".tit",
        ".view_tit",
        ".board_tit",
        ".article_tit",
    ]
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            title = clean_title(el.get_text(" ", strip=True))
            if title:
                return title

    if soup.title:
        title = clean_title(soup.title.get_text(" ", strip=True))
        if title:
            return title

    return "사이퍼즈 업데이트"


def is_target_update_title(title: str) -> bool:
    if not title:
        return False

    # 퍼스트 서버는 제외
    if "퍼스트 서버" in title:
        return False

    # 기존 운영 방식 유지
    if "정기점검 업데이트" in title:
        return True

    # 혹시 제목 구조가 바뀌어도 점검+업데이트 글은 잡도록 완화
    return "업데이트" in title and "점검" in title


def find_latest_target_post():
    html_text = fetch_html(LIST_URL)
    soup = BeautifulSoup(html_text, "html.parser")

    # 앵커 기준으로 제목과 링크를 함께 탐색
    anchors = soup.find_all("a", href=True)
    candidates = []

    for a in anchors:
        href = a.get("href", "")
        if "/article/update/topic/" not in href:
            continue
        url = urljoin(BASE_URL, href)
        title = clean_title(a.get_text(" ", strip=True))
        if not title:
            continue
        candidates.append((title, url))

    for title, url in candidates:
        if is_target_update_title(title):
            return url, title

    # 제목 파싱이 실패해도 첫 번째 topic 링크는 fallback
    urls = extract_topic_urls(html_text)
    if urls:
        return urls[0], None

    raise RuntimeError("업데이트 글 URL을 찾지 못했습니다.")


def pick_article_container(soup):
    selectors = [
        ".board_view",
        ".board-view",
        ".view_cont",
        ".view_conts",
        ".view_content",
        ".article_view",
        ".article-view",
        ".cont_view",
        ".contents",
        ".content",
        "#container",
    ]
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            return el
    return soup.body or soup


def article_text_lines(soup):
    container = pick_article_container(soup)

    raw_lines = []
    for s in container.stripped_strings:
        line = normalize_line(s)
        if not line:
            continue
        raw_lines.append(line)

    return dedupe_consecutive(raw_lines)


def is_section_header(line: str):
    normalized = re.sub(r"\s+", "", line).upper()

    if normalized in {"SYSTEM", "[SYSTEM]"}:
        return "system"
    if normalized in {"BALANCE", "[BALANCE]"}:
        return "balance"
    if normalized in {"ETC", "[ETC]"}:
        return "etc"

    # 한국어 헤더도 대비
    if normalized in {"시스템"}:
        return "system"
    if normalized in {"밸런스"}:
        return "balance"
    if normalized in {"기타", "버그수정", "버그", "ETC/BUG"}:
        return "etc"

    return None


def split_sections(lines):
    sections = {
        "system": [],
        "balance": [],
        "etc": [],
    }

    mode = None
    for line in lines:
        header = is_section_header(line)
        if header:
            mode = header
            continue

        if mode in sections:
            sections[mode].append(line)

    return sections


def cleanup_general_lines(lines):
    result = []
    for raw in lines:
        line = normalize_line(raw)
        line = re.sub(r"^[\-•·▪▫▶▷►]+\s*", "", line)
        if should_skip_line(line):
            continue
        if is_section_header(line):
            continue
        result.append(line)
    return unique_keep_order(result)


def cleanup_balance_detail_lines(lines):
    result = []
    for raw in lines:
        line = normalize_line(raw)
        line = re.sub(r"^[\-•·▪▫▶▷►]+\s*", "", line)
        if should_skip_line(line):
            continue
        if is_section_header(line):
            continue
        result.append(line)
    return dedupe_consecutive(result)


def is_dev_comment_header(line: str) -> bool:
    normalized = normalize_line(line)
    normalized = normalized.strip("[]")
    return normalized.startswith("개발자 코멘트")


def split_dev_comment_inline(line: str):
    """
    '개발자 코멘트 : ...' 형태면 헤더/본문 분리
    """
    line = normalize_line(line).strip("[]")
    if not line.startswith("개발자 코멘트"):
        return False, line

    rest = line.replace("개발자 코멘트", "", 1).strip()
    rest = rest.lstrip(":：- ")
    return True, rest


def looks_like_change_line(line: str) -> bool:
    if "→" in line:
        return True

    keywords = [
        "증가",
        "감소",
        "변경",
        "조정",
        "수정",
        "추가",
        "삭제",
        "개선",
        "적용",
        "가능",
        "불가",
        "고정",
        "상향",
        "하향",
        "초",
        "범위",
        "속도",
        "데미지",
        "쿨타임",
        "재사용",
        "선 딜레이",
        "후 딜레이",
        "경직",
        "슈퍼아머",
        "무적",
        "넉백",
        "피격",
        "이동속도",
        "공격속도",
    ]

    if any(k in line for k in keywords):
        return True

    if re.search(r"\d", line) and any(token in line for token in [":", "%", ".", "초"]):
        return True

    return False


def looks_like_character_name(line: str) -> bool:
    if not line:
        return False
    if len(line) > 18:
        return False
    if is_dev_comment_header(line):
        return False
    if looks_like_change_line(line):
        return False
    if re.search(r"\((?:L|R|E|F|SP|2nd|Shift|Tab|Wheel)", line, re.I):
        return False
    if any(ch in line for ch in [":", "/", "[", "]"]):
        return False

    return bool(re.fullmatch(r"[가-힣A-Za-z0-9\s·ㆍ\-]{1,18}", line))


def looks_like_skill_name(line: str) -> bool:
    if not line:
        return False
    if len(line) > 40:
        return False
    if is_dev_comment_header(line):
        return False
    if looks_like_change_line(line):
        return False

    # 조작키/2nd 표기가 있으면 거의 확실히 스킬명
    if re.search(r"\((?:[^)]*(?:L|R|E|F|SP|2nd|Shift|Tab|Wheel)[^)]*)\)", line, re.I):
        return True

    # 짧은 제목형 텍스트면 스킬명으로 간주
    if bool(re.fullmatch(r"[가-힣A-Za-z0-9\s·ㆍ\-/]{1,28}", line)):
        return True

    return False


def next_nonempty(lines, start_idx):
    for i in range(start_idx, len(lines)):
        if lines[i]:
            return lines[i]
    return ""


def ensure_group(groups, character, skill):
    if character not in groups:
        groups[character] = OrderedDict()
    if skill not in groups[character]:
        groups[character][skill] = {
            "changes": [],
            "comments": [],
        }


def parse_balance_groups(lines):
    cleaned = cleanup_balance_detail_lines(lines)
    groups = OrderedDict()

    current_character = None
    current_skill = None
    current_mode = "changes"

    for idx, line in enumerate(cleaned):
        is_comment_header, inline_comment = split_dev_comment_inline(line)

        if is_comment_header:
            current_mode = "comments"
            if inline_comment:
                if not current_character:
                    current_character = "공통"
                if not current_skill:
                    current_skill = "기타"
                ensure_group(groups, current_character, current_skill)
                groups[current_character][current_skill]["comments"].append(inline_comment)
            continue

        nxt = next_nonempty(cleaned, idx + 1)

        # 캐릭터명 판단
        if looks_like_character_name(line):
            if looks_like_skill_name(nxt) or is_dev_comment_header(nxt) or looks_like_change_line(nxt):
                current_character = line
                current_skill = None
                current_mode = "changes"
                if current_character not in groups:
                    groups[current_character] = OrderedDict()
                continue

        # 스킬명 판단
        if current_character and looks_like_skill_name(line):
            current_skill = line
            current_mode = "changes"
            ensure_group(groups, current_character, current_skill)
            continue

        # 내용 라인
        if not current_character:
            current_character = "공통"
        if not current_skill:
            current_skill = "기타"

        ensure_group(groups, current_character, current_skill)
        groups[current_character][current_skill][current_mode].append(line)

    # 스킬별 중복 제거
    for character, skills in groups.items():
        for skill, data in skills.items():
            data["changes"] = unique_keep_order([x for x in data["changes"] if x.strip()])
            data["comments"] = unique_keep_order([x for x in data["comments"] if x.strip()])

    return groups


def build_summary_text(lines, max_len=900):
    cleaned = cleanup_general_lines(lines)
    if not cleaned:
        return "내용 없음"

    out = []
    current_len = 0

    for line in cleaned:
        bullet = wrap_text(
            line,
            initial_indent="- ",
            subsequent_indent="  ",
            width=44,
        )
        needed = len(bullet) + 1
        if current_len + needed > max_len:
            out.append("- ...")
            break
        out.append(bullet)
        current_len += needed

    text = "\n".join(out).strip()
    return text[:MAX_FIELD_VALUE]


def build_skill_block(skill_name, skill_data):
    changes = unique_keep_order(skill_data.get("changes", []))
    comments = unique_keep_order(skill_data.get("comments", []))

    if not changes and not comments:
        return ""

    lines = [f"■ {skill_name}"]

    if changes:
        lines.append("  변경 내용")
        for change in changes:
            lines.append(
                wrap_text(
                    change,
                    initial_indent="  - ",
                    subsequent_indent="    ",
                    width=44,
                )
            )

    if comments:
        lines.append("")
        lines.append("  개발자 코멘트")
        for comment in comments:
            lines.append(
                wrap_text(
                    comment,
                    initial_indent="  - ",
                    subsequent_indent="    ",
                    width=44,
                )
            )

    return "\n".join(lines).strip()


def chunk_text_blocks(blocks, max_len=950):
    if not blocks:
        return []

    chunks = []
    current = []
    current_len = 0

    for block in blocks:
        add_len = len(block) + (2 if current else 0)
        if current and current_len + add_len > max_len:
            chunks.append("\n\n".join(current))
            current = [block]
            current_len = len(block)
        else:
            current.append(block)
            current_len += add_len

    if current:
        chunks.append("\n\n".join(current))

    return chunks


def build_balance_fields(balance_groups):
    fields = []

    for character, skills in balance_groups.items():
        skill_blocks = []

        for skill_name, skill_data in skills.items():
            block = build_skill_block(skill_name, skill_data)
            if block:
                skill_blocks.append(block)

        if not skill_blocks:
            continue

        chunks = chunk_text_blocks(skill_blocks, max_len=950)
        total = len(chunks)

        for idx, chunk in enumerate(chunks, start=1):
            field_name = character if total == 1 else f"{character} ({idx}/{total})"
            fields.append(
                {
                    "name": field_name,
                    "value": chunk[:MAX_FIELD_VALUE],
                    "inline": False,
                }
            )

    return fields


def parse_post(url):
    html_text = fetch_html(url)
    soup = BeautifulSoup(html_text, "html.parser")

    title = extract_title_from_soup(soup)
    lines = article_text_lines(soup)
    sections = split_sections(lines)

    system_summary = build_summary_text(sections["system"], max_len=900)
    etc_summary = build_summary_text(sections["etc"], max_len=900)
    balance_groups = parse_balance_groups(sections["balance"])

    return {
        "title": title,
        "url": url,
        "system_summary": system_summary,
        "etc_summary": etc_summary,
        "balance_groups": balance_groups,
    }


def build_payloads(post):
    payloads = []

    # 1) 첫 메시지
    first_fields = []
    if post["system_summary"] and post["system_summary"] != "내용 없음":
        first_fields.append(
            {
                "name": "시스템",
                "value": post["system_summary"][:MAX_FIELD_VALUE],
                "inline": False,
            }
        )

    intro_embed = {
        "title": post["title"],
        "url": post["url"],
        "description": "원문은 제목을 눌러 확인할 수 있습니다.",
        "color": 0x5865F2,
        "fields": first_fields,
    }
    payloads.append({"embeds": [intro_embed]})

    # 2) 밸런스 상세 메시지들
    balance_fields = build_balance_fields(post["balance_groups"])
    if balance_fields:
        for i in range(0, len(balance_fields), MAX_FIELDS_PER_EMBED):
            chunk = balance_fields[i:i + MAX_FIELDS_PER_EMBED]
            embed = {
                "title": "밸런스 상세",
                "url": post["url"],
                "color": 0x2ECC71,
                "fields": chunk,
            }
            payloads.append({"embeds": [embed]})

    # 3) 기타 메시지
    if post["etc_summary"] and post["etc_summary"] != "내용 없음":
        etc_embed = {
            "title": "버그 수정 / 기타",
            "url": post["url"],
            "color": 0x95A5A6,
            "fields": [
                {
                    "name": "기타",
                    "value": post["etc_summary"][:MAX_FIELD_VALUE],
                    "inline": False,
                }
            ],
        }
        payloads.append({"embeds": [etc_embed]})

    return payloads


def send_payload(payload):
    if not WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL 환경변수가 없습니다.")

    resp = requests.post(WEBHOOK_URL, json=payload, timeout=TIMEOUT)
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"Discord 전송 실패: {resp.status_code} {resp.text}")


def send_post_to_discord(post):
    payloads = build_payloads(post)
    for payload in payloads:
        send_payload(payload)


def main():
    state = load_state()
    latest_url, latest_title = find_latest_target_post()

    if state.get("last_url") == latest_url:
        print("새 업데이트 없음")
        return

    post = parse_post(latest_url)

    # 목록에서 읽은 제목이 더 명확하면 보정
    if latest_title and latest_title.strip():
        post["title"] = latest_title

    send_post_to_discord(post)
    save_state({"last_url": latest_url})
    print("전송 완료:", latest_url)


if __name__ == "__main__":
    main()
