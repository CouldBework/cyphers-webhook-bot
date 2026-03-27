import os
import re
import json
import sys
import textwrap
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


def normalize_space(text):
    return re.sub(r"\s+", " ", text or "").strip()


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
    text = normalize_space(text)

    suffixes = [
        " - 액션본능! 사이퍼즈",
        " - 사이퍼즈 - Nexon",
        " - 사이퍼즈",
    ]
    for suffix in suffixes:
        if text.endswith(suffix):
            text = text[:-len(suffix)].strip()

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

        line = normalize_space(line)

        if line in seen:
            continue

        seen.add(line)
        lines.append(line)

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


def cleanup_lines_summary(lines, limit=4):
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
        line = normalize_space(re.sub(r"^[\*\-•\s]+", "", line))

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
    picked = cleanup_lines_summary(lines, limit=limit)
    text = "\n".join(f"• {line}" for line in picked)
    return text[:max_len]


def cleanup_balance_detail_lines(lines):
    skip_contains = [
        "안녕하세요, 능력자 여러분",
        "아래는",
        "액션본능! 사이퍼즈",
        "새소식",
        "공지사항",
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
        line = normalize_space(re.sub(r"^[\*\-•\s]+", "", line))

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

        if line in seen:
            continue

        seen.add(line)
        cleaned.append(line)

    return cleaned


def looks_like_character_name(line):
    line = normalize_space(line)

    if len(line) < 2 or len(line) > 20:
        return False

    if re.search(r"\d", line):
        return False

    bad_tokens = [
        "(", ")", ":", "→", "%", "+", "-", "/", "[", "]",
        "증가", "감소", "변경", "수정", "적용", "공격", "범위",
        "속도", "데미지", "딜레이", "가능", "문제", "오류",
        "축소", "확대", "단축", "연장"
    ]
    if any(token in line for token in bad_tokens):
        return False

    return True


def looks_like_skill_name(line):
    line = normalize_space(line)

    if len(line) < 2 or len(line) > 60:
        return False

    if line.startswith(("◎", "※")):
        return False

    change_words = [
        "데미지", "공격", "범위", "속도", "선 딜레이", "후 딜레이",
        "감소됩니다", "증가됩니다", "변경됩니다", "수정됩니다",
        "조정됩니다", "개선됩니다",
        "축소됩니다", "확대됩니다", "단축됩니다", "연장됩니다",
        "적용됩니다", "가능하게", "문제가", "발생", "추적 속도",
        "연속 타격", "기본 속도", "회전 각도"
    ]
    if any(word in line for word in change_words):
        return False

    if "→" in line or "%" in line or ":" in line:
        return False

    if line.endswith(".") or line.endswith(".)"):
        return False

    if re.search(r"\([^)]+\)$", line):
        return True

    if re.fullmatch(r"[가-힣A-Za-z0-9\s'·\-]+", line) and len(line) <= 25:
        return True

    return False


def merge_character_groups(groups):
    merged = []
    index_map = {}

    for group in groups:
        character = normalize_space(group["character"])
        lines = cleanup_balance_detail_lines(group["lines"])

        if not character or not lines:
            continue

        if character not in index_map:
            index_map[character] = len(merged)
            merged.append({
                "character": character,
                "lines": []
            })

        target = merged[index_map[character]]["lines"]
        for line in lines:
            if line not in target:
                target.append(line)

    return merged


def parse_balance_groups_from_tables(soup):
    raw_groups = []

    for tr in soup.select("tr"):
        th = tr.find("th")
        td = tr.find("td")

        if not th or not td:
            continue

        character = normalize_space(th.get_text(" ", strip=True))
        detail_text = td.get_text("\n", strip=True)

        if not character or not detail_text:
            continue

        if len(character) > 30:
            continue

        if any(bad in character for bad in ["SYSTEM", "BALANCE", "ETC", "시스템", "버그 수정"]):
            continue

        detail_lines = [normalize_space(x) for x in detail_text.splitlines()]
        detail_lines = cleanup_balance_detail_lines(detail_lines)

        if not detail_lines:
            continue

        raw_groups.append({
            "character": character,
            "lines": detail_lines
        })

    return merge_character_groups(raw_groups)


def parse_balance_groups_from_lines(balance_lines):
    cleaned = cleanup_balance_detail_lines(balance_lines)
    groups = []
    current = None

    for line in cleaned:
        if looks_like_character_name(line):
            current = {
                "character": line,
                "lines": []
            }
            groups.append(current)
        else:
            if current is None:
                current = {
                    "character": "기타 밸런스",
                    "lines": []
                }
                groups.append(current)
            current["lines"].append(line)

    result = []
    for group in groups:
        if group["lines"]:
            result.append(group)

    return merge_character_groups(result)


def extract_developer_comments(balance_lines):
    cleaned = cleanup_balance_detail_lines(balance_lines)

    comments = []
    remaining = []

    in_comment = False
    for line in cleaned:
        if "개발자 코멘트" in line:
            in_comment = True
            continue

        if in_comment:
            if looks_like_character_name(line):
                in_comment = False
                remaining.append(line)
                continue

            if looks_like_skill_name(line):
                in_comment = False
                remaining.append(line)
                continue

            comments.append(line)
        else:
            remaining.append(line)

    return comments, remaining


def build_skill_blocks(lines):
    cleaned = cleanup_balance_detail_lines(lines)
    skills = []
    misc = []
    current_skill = None

    for line in cleaned:
        if looks_like_skill_name(line):
            current_skill = {
                "skill": line,
                "changes": []
            }
            skills.append(current_skill)
        else:
            if current_skill is None:
                misc.append(line)
            else:
                current_skill["changes"].append(line)

    filtered_skills = []
    for item in skills:
        if item["changes"]:
            filtered_skills.append(item)

    return {
        "skills": filtered_skills,
        "misc": misc
    }


def enrich_balance_groups_with_skills(balance_groups):
    enriched = []

    for group in balance_groups:
        character = group["character"]
        parsed = build_skill_blocks(group["lines"])

        enriched.append({
            "character": character,
            "skills": parsed["skills"],
            "misc": parsed["misc"]
        })

    return enriched


def wrap_text_lines(text, width=44, first_prefix="", cont_prefix="  "):
    text = normalize_space(text)
    if not text:
        return []

    wrapped = textwrap.wrap(
        text,
        width=width,
        break_long_words=False,
        break_on_hyphens=False
    )

    if not wrapped:
        return [first_prefix + text]

    result = []
    for i, part in enumerate(wrapped):
        prefix = first_prefix if i == 0 else cont_prefix
        result.append(prefix + part)
    return result


def build_skill_block_lines(skill_name, changes):
    lines = []
    lines.extend(wrap_text_lines(skill_name, width=40, first_prefix="■ ", cont_prefix="  "))

    for change in changes:
        lines.extend(wrap_text_lines(change, width=44, first_prefix="↳ ", cont_prefix="  "))

    return lines


def build_bullet_chunks(lines, max_len=1000):
    chunks = []
    current = ""

    for line in lines:
        formatted = f"• {line}\n"
        if len(current) + len(formatted) > max_len:
            if current.strip():
                chunks.append(current.strip())
            current = formatted
        else:
            current += formatted

    if current.strip():
        chunks.append(current.strip())

    return chunks or []


def chunk_character_skill_blocks(group, max_len=1000):
    blocks = []

    if group["misc"]:
        misc_lines = ["■ 기타"]
        for item in group["misc"]:
            misc_lines.extend(wrap_text_lines(item, width=44, first_prefix="↳ ", cont_prefix="  "))
        blocks.append("\n".join(misc_lines))

    for skill in group["skills"]:
        block_lines = build_skill_block_lines(skill["skill"], skill["changes"])
        if block_lines:
            blocks.append("\n".join(block_lines))

    if not blocks:
        return []

    chunks = []
    current = ""

    for block in blocks:
        candidate = block if not current else current + "\n\n" + block

        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = block

    if current:
        chunks.append(current)

    return chunks


def build_balance_fields(balance_groups):
    fields = []

    if not balance_groups:
        return []

    for group in balance_groups:
        character = group["character"]
        chunks = chunk_character_skill_blocks(group, max_len=1000)

        for i, chunk in enumerate(chunks, start=1):
            if len(chunks) == 1:
                field_name = character
            else:
                field_name = f"{character} ({i}/{len(chunks)})"

            fields.append({
                "name": field_name[:256],
                "value": chunk[:1024],
                "inline": False
            })

    return fields


def parse_post(post):
    soup = post["soup"]
    title = post["title"]
    url = post["url"]

    lines = article_text_lines(soup)
    sections = split_sections(lines)

    system_text = build_summary_text(sections["system"], limit=4)
    etc_text = build_summary_text(sections["etc"], limit=4)

    developer_comments, balance_lines_without_comments = extract_developer_comments(sections["balance"])

    balance_groups = parse_balance_groups_from_tables(soup)
    if not balance_groups:
        balance_groups = parse_balance_groups_from_lines(balance_lines_without_comments)

    balance_groups = enrich_balance_groups_with_skills(balance_groups)

    return {
        "title": title,
        "url": url,
        "system": system_text[:1024],
        "developer_comments": developer_comments,
        "balance_groups": balance_groups,
        "etc": etc_text[:1024],
    }


def build_payloads(post):
    payloads = []

    main_fields = [
        {
            "name": "시스템",
            "value": post["system"] or "변경 사항 없음",
            "inline": False
        }
    ]

    payloads.append({
        "username": "사이퍼즈 업데이트 알리미",
        "content": f"새 업데이트 감지: {post['title']}\n{post['url']}",
        "embeds": [
            {
                "title": post["title"][:256],
                "url": post["url"],
                "description": "사이퍼즈 공식 업데이트 새 글을 감지해 자동으로 정리했습니다.",
                "color": 15158332,
                "fields": main_fields,
                "footer": {
                    "text": "개발자 코멘트와 밸런스는 아래 메시지에 이어서 전송됩니다."
                }
            }
        ]
    })

    comment_chunks = build_bullet_chunks(post["developer_comments"], max_len=1000)
    for i, chunk in enumerate(comment_chunks, start=1):
        payloads.append({
            "username": "사이퍼즈 업데이트 알리미",
            "embeds": [
                {
                    "title": f"{post['title'][:220]} - 개발자 코멘트",
                    "url": post["url"],
                    "color": 3447003,
                    "fields": [
                        {
                            "name": "개발자 코멘트" if len(comment_chunks) == 1 else f"개발자 코멘트 ({i}/{len(comment_chunks)})",
                            "value": chunk[:1024],
                            "inline": False
                        }
                    ],
                    "footer": {
                        "text": "개발자 코멘트 분리 표시"
                    }
                }
            ]
        })

    balance_fields = build_balance_fields(post["balance_groups"])
    fields_per_message = 3

    for start in range(0, len(balance_fields), fields_per_message):
        group = balance_fields[start:start + fields_per_message]

        payloads.append({
            "username": "사이퍼즈 업데이트 알리미",
            "embeds": [
                {
                    "title": f"{post['title'][:220]} - 밸런스",
                    "url": post["url"],
                    "color": 15158332,
                    "fields": group,
                    "footer": {
                        "text": "캐릭터 > 스킬별 밸런스 정리"
                    }
                }
            ]
        })

    payloads.append({
        "username": "사이퍼즈 업데이트 알리미",
        "embeds": [
            {
                "title": f"{post['title'][:220]} - 기타",
                "url": post["url"],
                "color": 15158332,
                "fields": [
                    {
                        "name": "버그 수정 / 기타",
                        "value": post["etc"] or "변경 사항 없음",
                        "inline": False
                    }
                ],
                "footer": {
                    "text": "제목을 누르면 원문으로 이동합니다."
                }
            }
        ]
    })

    return payloads


def send_payload(payload):
    if not WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL 이 비어 있습니다. GitHub Secret을 확인하세요.")

    response = requests.post(WEBHOOK_URL, json=payload, timeout=20)
    if response.status_code not in (200, 204):
        raise RuntimeError(f"Discord 전송 실패: {response.status_code} / {response.text}")


def send_post_to_discord(post):
    payloads = build_payloads(post)
    for payload in payloads:
        send_payload(payload)


def main():
    state = load_state()
    latest_post = find_latest_target_post()
    parsed_post = parse_post(latest_post)

    if parsed_post["url"] == state.get("last_url"):
        print("새 업데이트 없음")
        return

    send_post_to_discord(parsed_post)
    state["last_url"] = parsed_post["url"]
    save_state(state)
    print("새 업데이트 전송 완료:", parsed_post["url"])


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("오류 발생:", e)
        sys.exit(1)
