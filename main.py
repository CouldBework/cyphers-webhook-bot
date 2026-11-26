# -*- coding: utf-8 -*-
"""
사이퍼즈 정기점검 업데이트 → Discord Webhook 봇 (최종본)

- 공식 업데이트 페이지의 <table> 구조를 그대로 파싱 (rowspan/colspan 처리)
- 캐릭터 밸런싱: 표의 1열(th)=캐릭터명, 2열(td)=스킬/변경내용 → 정확한 묶음
- 시스템 / 밸런스 개발자 코멘트 / 밸런스 상세 / 버그 수정·기타 로 메시지 분리
- Discord embed 제한(필드 1024자, 필드 25개)에 맞게 자동 분할 전송
- state.json 으로 마지막 전송 글 저장 → 중복 전송 방지

사용법:
  1) GitHub 저장소 Secret에 DISCORD_WEBHOOK_URL 등록
  2) .github/workflows/cyphers.yml 의 schedule(예: 15분) 또는 workflow_dispatch 로 실행
  3) 강제 재전송: state.json 을 {"last_url": null} 로 바꾸고 실행
"""

import os
import re
import json
from collections import OrderedDict
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://cyphers.nexon.com"
LIST_URL = f"{BASE_URL}/article/update"
STATE_FILE = "state.json"

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
MAX_FIELDS_PER_EMBED_DISCORD = 25


# ---------- 텍스트 정규화 ----------

def normalize_line(text: str) -> str:
    """nbsp / 전각 공백 / zero-width 문자를 일반 공백으로 통일"""
    text = re.sub(r"[\u00a0\u2000-\u200b\u3000\ufeff]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def dedupe_consecutive(lines):
    result, prev = [], None
    for line in lines:
        if line != prev:
            result.append(line)
        prev = line
    return result


def unique_keep_order(items):
    seen, result = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def clip(text: str, max_len: int) -> str:
    """강제 줄바꿈 없이 길이만 제한 (줄바꿈은 Discord가 화면폭에 맞게 처리)"""
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


# ---------- 상태 저장 ----------

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_url": None}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {"last_url": None}
            return {"last_url": data.get("last_url")}
    except Exception:
        return {"last_url": None}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------- 페이지 가져오기 ----------

def fetch_html(url):
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.text


def should_skip_line(line: str) -> bool:
    if not line:
        return True

    skip_exact = {"목록", "댓글", "공유", "닫기", "TOP"}

    skip_contains = [
        "상세 변경 내용은 원문 확인",
        "이 글을 SNS로 공유하기",
        "본문 바로가기",
        "푸터 바로가기",
        "copyright",
        "All Rights Reserved",
        "사업자등록번호",
        "통신판매업 신고번호",
        "개인정보처리방침",
        "이용약관",
        "청소년보호정책",
        "운영정책",
    ]

    if line in skip_exact:
        return True

    lowered = line.lower()
    return any(token.lower() in lowered for token in skip_contains)


def extract_topic_urls(html_text):
    urls = re.findall(r"/article/update/topic/\d+", html_text)
    result, seen = [], set()
    for u in urls:
        full = urljoin(BASE_URL, u)
        if full not in seen:
            seen.add(full)
            result.append(full)
    return result


def clean_title(text):
    return normalize_line(text)


def extract_title_from_soup(soup):
    for sel in [
        "h1", ".title", ".subject", ".tit",
        ".view_tit", ".board_tit", ".article_tit",
    ]:
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
    if "퍼스트 서버" in title:
        return False
    if "정기점검 업데이트" in title:
        return True
    return "업데이트" in title and "점검" in title


def find_latest_target_post():
    """최신 글부터 확인해서 '정기점검 업데이트' 글 하나를 찾아 반환"""
    html_text = fetch_html(LIST_URL)
    urls = extract_topic_urls(html_text)

    if not urls:
        raise RuntimeError("업데이트 목록에서 글 URL을 찾지 못했습니다.")

    for url in urls[:12]:
        try:
            soup = BeautifulSoup(fetch_html(url), "html.parser")
            title = extract_title_from_soup(soup)
            if is_target_update_title(title):
                return url, title
        except Exception as e:
            print(f"[warn] 글 확인 실패: {url} / {e}")

    # 필터에 걸리는 제목이 없으면 최신 글이라도 반환
    fallback_url = urls[0]
    try:
        soup = BeautifulSoup(fetch_html(fallback_url), "html.parser")
        fallback_title = extract_title_from_soup(soup)
    except Exception:
        fallback_title = None

    return fallback_url, fallback_title


def pick_article_container(soup):
    for sel in [
        ".board_view", ".board-view", ".view_cont", ".view_conts",
        ".view_content", ".article_view", ".article-view", ".cont_view",
        ".contents", ".content", "#container",
    ]:
        el = soup.select_one(sel)
        if el:
            return el
    return soup.body or soup


def article_text_lines(soup):
    container = pick_article_container(soup)
    raw_lines = [normalize_line(s) for s in container.stripped_strings]
    raw_lines = [line for line in raw_lines if line]
    return dedupe_consecutive(raw_lines)


# ---------- 섹션 분리 ----------

def is_section_header(line: str):
    normalized = normalize_line(line)
    compact = re.sub(r"\s+", "", normalized).upper()

    if compact.startswith("SYSTEM"):
        return "system"
    if normalized == "시스템":
        return "system"

    if compact.startswith("BALANCE"):
        return "balance"
    if "밸런스" in normalized or "밸런싱" in normalized:
        return "balance"

    if compact.startswith("ETC"):
        return "etc"
    if normalized == "기타" or "버그 수정" in normalized:
        return "etc"

    return None


def split_sections(lines):
    sections = {"system": [], "balance": [], "etc": []}
    mode = None

    for line in lines:
        header = is_section_header(line)
        if header:
            mode = header
            continue
        if mode in sections:
            sections[mode].append(line)

    return sections


def clean_lines(lines):
    result = []
    for raw in lines:
        line = normalize_line(raw)
        line = re.sub(r"^[\-•·▪▫▶▷►◎※*]+\s*", "", line)
        if not line:
            continue
        if should_skip_line(line):
            continue
        if is_section_header(line):
            continue
        result.append(line)
    return result


# ---------- 표 구조 파싱 (rowspan / colspan 처리) ----------

def expand_table_rows(table):
    """
    <table>을 rowspan/colspan 확장한 2차원 그리드로 변환.
    각 셀은 (tag_name, text) 형태. 텍스트는 줄바꿈(\n) 보존.
    """
    grid = {}
    tracked = []  # (row_idx, col_idx, remaining, cell_tag, text)

    for r, tr in enumerate(table.select("tr")):
        col = 0
        # 이전 행에서 rowspan 이어지는 채우기
        for (ri, ci, remaining, tag, text) in tracked:
            if ri + remaining > r and ci >= col:
                grid[(r, ci)] = (tag, text)
                col = ci + 1

        for cell in tr.select("th, td"):
            while any(c == col for (_, c, _, _, _) in tracked if _ + _ > -1) and (
                any(ri <= r < ri + remaining and ci == col for (ri, ci, remaining, _, _) in tracked)
            ):
                col += 1

            tag = cell.name
            text = cell.get_text("\n", strip=True)

            rowspan = int(cell.get("rowspan", 1) or 1)
            colspan = int(cell.get("colspan", 1) or 1)

            for dc in range(colspan):
                grid[(r, col + dc)] = (tag, text)

            if rowspan > 1:
                tracked.append((r, col, rowspan, tag, text))

            col += colspan

    if not grid:
        return []

    max_row = max(r for (r, _) in grid)
    max_col = max(c for (_, c) in grid)

    rows = []
    for r in range(max_row + 1):
        row = []
        for c in range(max_col + 1):
            row.append(grid.get((r, c), (None, "")))
        rows.append(row)

    return rows


SKILL_KEY_PATTERN = re.compile(
    r"\((?:[^)]*(?:L|R|E|F|SP|SL|LR|2nd|Shift|Tab|Wheel)[^)]*)\)", re.I
)

PRICE_TABLE_HEADERS = {
    "상의", "하의", "장갑", "신발", "손", "가슴", "허리", "발",
    "1단계", "2단계", "3단계", "유니크/OM/공용", "레전더리",
}


def is_dev_comment_line(line: str) -> bool:
    normalized = normalize_line(line).strip("[]").lstrip("◎*•- ")
    return normalized.startswith("개발자 코멘트")


def split_cell_into_entries(cell_text: str):
    """
    캐릭터 밸런싱 셀(text)을 [(스킬명, [내용...]), ...] 로 구조화.
    셀 안의 '개발자 코멘트' 줄은 ('개발자 코멘트', [...]) 로 따로 모음.
    """
    entries = []  # [ [이름, [줄...]] ]
    current = None
    mode = "changes"

    raw_lines = cell_text.split("\n")
    for raw in raw_lines:
        line = normalize_cell_line(raw)
        if not line:
            continue

        if is_dev_comment_line(line):
            current = ["개발자 코멘트", []]
            entries.append(current)
            mode = "comments"
            continue

        # 스킬명 패턴 (예: 박쥐(L), 사영도(2nd R))
        m = re.match(
            r"^([가-힣A-Za-z0-9\s·ㆍ'/-]{1,30}?" + SKILL_KEY_PATTERN.pattern + r")\s*(.*)$",
            line,
        )
        if m and not line.startswith("-"):
            skill_name = normalize_line(m.group(1))
            rest = normalize_line(m.group(2))
            current = [skill_name, []]
            entries.append(current)
            mode = "changes"
            if rest:
                current[1].append(rest)
            continue

        if current is None:
            # 스킬명 없이 바로 내용이 나오는 경우
            current = ["기타", []]
            entries.append(current)

        current[1].append(line)

    return [(name, lines) for name, lines in entries if lines or name != "기타"]


def normalize_cell_line(text: str) -> str:
    text = re.sub(r"[\u00a0\u2000-\u200b\u3000\ufeff]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^[\-•·▪▫▶▷►※]+\s*", "", text)
    return text


def extract_character_balance_from_tables(soup):
    """
    페이지의 <table> 중 '캐릭터 밸런싱' 표(1열=th 캐릭터명, 2열=스킬 상세)를 찾아
    OrderedDict {캐릭터명: [(스킬명, [내용...]), ...]} 로 반환.
    rowspan 덕분에 캐릭터당 여러 행이어도 하나로 묶임.
    """
    result = OrderedDict()

    for table in soup.select("table"):
        rows = expand_table_rows(table)
        if not rows:
            continue

        # 표 모양 확인: 첫 행의 1번째 셀이 th 여야 함
        first_row = rows[0]
        first_tag = first_row[0][0] if first_row else None
        if first_tag != "th":
            continue

        # 가격표(상의/1단계...)는 제외
        first_texts = {normalize_line(cell[1]) for cell in first_row if cell[1]}
        if first_texts & PRICE_TABLE_HEADERS:
            continue

        char_count = 0
        for row in rows:
            if len(row) < 2:
                continue

            char_tag, char_text = row[0]
            _, detail_text = row[1]

            char_name = normalize_line(char_text)
            if not char_name or not detail_text:
                continue

            # 캐릭터 밸런싱 표인지 확인: 스킬 패턴이 등장해야 함
            if not SKILL_KEY_PATTERN.search(detail_text):
                continue

            entries = split_cell_into_entries(detail_text)
            if not entries:
                continue

            if char_name not in result:
                result[char_name] = []
            result[char_name].extend(entries)
            char_count += 1

        # 캐릭터 밸런싱 표를 찾았으면 이 표는 처리 완료

    return result


# ---------- 요약 텍스트 ----------

def build_summary_text(lines, max_len=900):
    cleaned = clean_lines(lines)
    if not cleaned:
        return "내용 없음"

    out = []
    current_len = 0
    for line in cleaned:
        bullet = f"- {line}"
        if current_len + len(bullet) + 1 > max_len:
            out.append("- …")
            break
        out.append(bullet)
        current_len += len(bullet) + 1

    return clip("\n".join(out), MAX_FIELD_VALUE)


# ---------- 밸런스 필드 생성 ----------

def build_skill_block(skill_name, change_lines, comment_lines):
    if not change_lines and not comment_lines:
        return ""

    lines = [f"■ {skill_name}"]

    if change_lines:
        lines += [f"　{c}" for c in change_lines]

    if comment_lines:
        lines.append("")
        lines.append("　개발자 코멘트")
        lines += [f"　{c}" for c in comment_lines]

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


def build_balance_fields_from_tables(balance_tables):
    """표 기반: 캐릭터별 필드 생성 (길면 자동 분할)"""
    fields = []

    for character, entries in balance_tables.items():
        change_blocks = []
        comment_blocks = []

        for skill_name, lines in entries:
            if skill_name == "개발자 코멘트":
                comment_blocks.append("\n".join(f"　{x}" for x in lines))
            else:
                block = build_skill_block(skill_name, lines, [])
                if block:
                    change_blocks.append(block)

        blocks = change_blocks + (["**개발자 코멘트**\n" + c for c in comment_blocks])

        if not blocks:
            continue

        chunks = chunk_text_blocks(blocks, max_len=950)
        total = len(chunks)

        for idx, chunk in enumerate(chunks, start=1):
            name = character if total == 1 else f"{character} ({idx}/{total})"
            fields.append({
                "name": clip(name, 256),
                "value": clip(chunk, MAX_FIELD_VALUE),
                "inline": False,
            })

    return fields


def build_balance_fallback_fields(lines):
    """표가 없는 옛 글용 fallback: 텍스트 줄을 그대로 묶어서 전송"""
    cleaned = clean_lines(lines)
    if not cleaned:
        return []

    chunks = []
    current = []
    current_len = 0
    for line in cleaned:
        bullet = f"- {line}"
        if current and current_len + len(bullet) + 1 > 950:
            chunks.append("\n".join(current))
            current = [bullet]
            current_len = len(bullet)
        else:
            current.append(bullet)
            current_len += len(bullet) + 1
    if current:
        chunks.append("\n".join(current))

    fields = []
    total = len(chunks)
    for idx, chunk in enumerate(chunks, start=1):
        name = "밸런스 원문" if total == 1 else f"밸런스 원문 ({idx}/{total})"
        fields.append({
            "name": clip(name, 256),
            "value": clip(chunk, MAX_FIELD_VALUE),
            "inline": False,
        })

    return fields


# ---------- 게시글 파싱 ----------

def parse_post(url):
    soup = BeautifulSoup(fetch_html(url), "html.parser")

    title = extract_title_from_soup(soup)
    lines = article_text_lines(soup)
    sections = split_sections(lines)

    system_summary = build_summary_text(sections["system"], max_len=900)
    etc_summary = build_summary_text(sections["etc"], max_len=900)

    balance_tables = extract_character_balance_from_tables(soup)

    return {
        "title": title,
        "url": url,
        "system_summary": system_summary,
        "etc_summary": etc_summary,
        "balance_tables": balance_tables,
        "balance_raw_lines": sections["balance"],
    }


# ---------- Discord 전송 ----------

def build_payloads(post):
    payloads = []

    # 1) 첫 메시지: 제목 + 시스템
    first_fields = []
    if post["system_summary"] != "내용 없음":
        first_fields.append({
            "name": "시스템",
            "value": post["system_summary"],
            "inline": False,
        })

    payloads.append({
        "embeds": [{
            "title": clip(post["title"], 256),
            "url": post["url"],
            "description": "원문은 제목을 눌러 확인할 수 있습니다.",
            "color": 0x5865F2,
            "fields": first_fields,
        }]
    })

    # 2) 밸런스 상세 (표 기반 → 실패 시 fallback)
    balance_fields = build_balance_fields_from_tables(post["balance_tables"])
    if not balance_fields:
        balance_fields = build_balance_fallback_fields(post["balance_raw_lines"])

    for i in range(0, len(balance_fields), MAX_FIELDS_PER_EMBED):
        payloads.append({
            "embeds": [{
                "title": "밸런스 상세",
                "url": post["url"],
                "color": 0x2ECC71,
                "fields": balance_fields[i:i + MAX_FIELDS_PER_EMBED],
            }]
        })

    # 3) 버그 수정 / 기타
    if post["etc_summary"] != "내용 없음":
        payloads.append({
            "embeds": [{
                "title": "버그 수정 / 기타",
                "url": post["url"],
                "color": 0x95A5A6,
                "fields": [{
                    "name": "기타",
                    "value": post["etc_summary"],
                    "inline": False,
                }],
            }]
        })

    return payloads


def send_payload(payload):
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        raise RuntimeError("DISCORD_WEBHOOK_URL 환경변수가 없습니다.")

    resp = requests.post(webhook_url, json=payload, timeout=TIMEOUT)
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"Discord 전송 실패: {resp.status_code} {resp.text}")


def send_post_to_discord(post):
    payloads = build_payloads(post)
    print(f"전송할 payload 수: {len(payloads)}")
    for idx, payload in enumerate(payloads, start=1):
        print(f"payload 전송 중... ({idx}/{len(payloads)})")
        send_payload(payload)


def main():
    state = load_state()
    print("현재 state:", state)

    latest_url, latest_title = find_latest_target_post()
    print("최신 URL:", latest_url)
    print("최신 제목:", latest_title)

    if state.get("last_url") == latest_url:
        print("새 업데이트 없음")
        return

    post = parse_post(latest_url)
    if latest_title and latest_title.strip():
        post["title"] = latest_title

    print("system 요약 존재:", post["system_summary"] != "내용 없음")
    print("balance 캐릭터 수:", len(post["balance_tables"]))

    send_post_to_discord(post)
    save_state({"last_url": latest_url})
    print("전송 완료:", latest_url)


if __name__ == "__main__":
    main()
