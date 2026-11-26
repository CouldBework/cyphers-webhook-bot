# -*- coding: utf-8 -*-
"""
사이퍼즈 정기점검 업데이트 → Discord Webhook 봇 (표 기반 최종본 v2)

- 공식 업데이트 페이지의 모든 <table>(역할군 고정 버프/전투 아이템/캐릭터 밸런싱)을
  행(1열=이름, 2열=내용) 단위로 그대로 파싱 → 내용 누락 방지
- 줄머리 불릿 기호("- ")만 제거하고 "-10%" 같은 음수 값은 보존
- 표 밖의 문단형 '개발자 코멘트'도 별도 임베드로 전송
- rowspan/colspan 확장 처리, Discord embed 제한(필드 1024자)에 맞춰 자동 분할
- state.json 으로 마지막 전송 글 저장 → 중복 전송 방지
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
MAGAZINE_LIST_URL = f"{BASE_URL}/article/magazine"
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

# '스킬명(조작키)' 패턴: 박쥐(L), 사영도(2nd R), 라이트닝 스텝(SP) 등
SKILL_KEY_PATTERN = re.compile(
    r"\((?:[^)]*(?:L|R|E|F|SP|SL|LR|2nd|Shift|Tab|Wheel)[^)]*)\)", re.I
)

# 가격표(아이템 가격 조정 표) 판별용 헤더 키워드 → 이 표는 스킵
PRICE_TABLE_HEADERS = {
    "상의", "하의", "장갑", "신발", "손", "가슴", "허리", "발",
    "1단계", "2단계", "3단계", "유니크/OM/공용", "레전더리",
}

# '변경 내용'으로 판단할 힌트 키워드 (스킬명 헤더 오분리 방지에 사용)
CHANGE_HINTS = (
    "쿨타임", "대미지", "증가", "감소", "조정", "적용", "체력", "공격력",
    "방어", "흡혈", "보호막", "속도", "범위", "코인", "시야", "지속시간",
    "딜레이", "관통", "경직", "무적", "넉백", "타격",
)


# ---------- 텍스트 정규화 ----------

def normalize_line(text: str) -> str:
    """nbsp / 전각 공백 / zero-width 문자를 일반 공백으로 통일"""
    text = re.sub(r"[\u00a0\u2000-\u200b\u3000\ufeff]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_cell_line(text: str) -> str:
    """
    표 셀 내부 한 줄 정규화.
    줄머리 불릿 기호('- ', '•' 등)는 제거하되, '-10%' 같은 음수 값은 보존:
      - 불릿 제거 조건 = 대시 뒤에 공백이 오는 경우만 ('- 쿨타임이...' → '쿨타임이...')
      - '-10%→-15%' 는 대시 뒤에 숫자가 바로 오므로 그대로 유지
    """
    text = re.sub(r"[\u00a0\u2000-\u200b\u3000\ufeff]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(?:[-•·▪▫▶▷►]\s+|[•·▪▫▶▷►※]\s*)+", "", text)
    return text


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


def looks_like_change_content(line: str) -> bool:
    """그 줄이 '변경 내용'(스킬명이 아닌 실제 내용)인지 판단"""
    if not line:
        return False
    if "→" in line:
        return True
    if any(k in line for k in CHANGE_HINTS):
        return True
    return bool(re.search(r"\d", line) and any(t in line for t in (":", "%", "초")))


# ---------- 상태 저장 ----------

def load_state():
    """state.json 전체를 보존 (업데이트 봇용 last_url + 매거진용 magazine_last_url)"""
    if not os.path.exists(STATE_FILE):
        return {"last_url": None, "magazine_last_url": None}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {"last_url": None, "magazine_last_url": None}
            state = {
                "last_url": data.get("last_url"),
                "magazine_last_url": data.get("magazine_last_url"),
            }
            return state
    except Exception:
        return {"last_url": None, "magazine_last_url": None}


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


def extract_title_from_soup(soup):
    for sel in [
        "h1", ".title", ".subject", ".tit",
        ".view_tit", ".board_tit", ".article_tit",
    ]:
        el = soup.select_one(sel)
        if el:
            title = normalize_line(el.get_text(" ", strip=True))
            if title:
                return title

    if soup.title:
        title = normalize_line(soup.title.get_text(" ", strip=True))
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


# ---------- 섹션 분리 (표 밖 텍스트 기준) ----------

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
        line = normalize_cell_line(raw)
        if not line:
            continue
        if should_skip_line(line):
            continue
        if is_section_header(line):
            continue
        result.append(line)
    return result


def is_dev_comment_line(line: str) -> bool:
    normalized = normalize_line(line).strip("[]").lstrip("◎*•- ")
    return normalized.startswith("개발자 코멘트")


def extract_standalone_dev_comments(lines):
    """
    표 밖에 문단으로 쓰인 '◎ 개발자 코멘트' 블록을 추출.
    마커 뒤에 이어지는 긴 문단 줄들을 코멘트로 묶는다.
    """
    comments = []
    current = None

    for raw in lines:
        line = normalize_cell_line(raw)
        if not line or should_skip_line(line):
            continue

        if is_dev_comment_line(line):
            if current:
                comments.append(" ".join(current))
            current = []
            # '개발자 코멘트: 내용...' 한 줄 형태면 내용만 분리
            inline = re.sub(r"^.*?개발자\s*코멘트\]?[:：]?\s*", "", line)
            if inline and inline != line:
                current.append(inline)
            continue

        if current is None:
            continue

        # 코멘트 문단은 보통 긴 문장. 짧은 제목성 줄이 나오면 문단 끝으로 판단.
        if len(line) < 25 or line.startswith("*") or looks_like_change_content(line) and len(line) < 40:
            comments.append(" ".join(current))
            current = None
            continue

        current.append(line)

    if current:
        comments.append(" ".join(current))

    return unique_keep_order(comments)


# ---------- 표 구조 파싱 (rowspan / colspan 처리) ----------

def expand_table_rows(table):
    """
    <table>을 rowspan/colspan 확장한 2차원 그리드로 변환.
    각 셀은 (tag_name, text) 형태. 텍스트는 줄바꿈(\\n) 보존.
    """
    grid = {}
    tracked = []  # (row_idx, col_idx, rowspan_remaining, tag, text)

    for r, tr in enumerate(table.select("tr")):
        col = 0
        occupied = set()
        # 이전 행에서 rowspan 으로 이어지는 셀 채우기
        for (ri, ci, remaining, tag, text) in tracked:
            if remaining > 0 and ci not in occupied:
                grid[(r, ci)] = (tag, text)
                occupied.add(ci)

        for cell in tr.select("th, td"):
            while col in occupied:
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

        # rowspan 잔여 횟수 갱신
        new_tracked = []
        for (ri, ci, remaining, tag, text) in tracked:
            if ri == r:
                continue
            new_tracked.append((ri, ci, remaining - 1, tag, text))
        tracked = new_tracked

    if not grid:
        return []

    max_row = max(r for (r, _) in grid)
    max_col = max(c for (_, c) in grid)

    rows = []
    for r in range(max_row + 1):
        rows.append([grid.get((r, c), (None, "")) for c in range(max_col + 1)])

    return rows


def split_cell_into_entries(cell_text: str):
    """
    셀 텍스트를 [(이름, [줄...]), ...] 로 구조화.

    - '스킬명(조작키)' 줄은 스킬 헤더로 인식 (단, 뒤에 변경 내용이 붙어 있으면 내용 줄로 취급)
      예: '궁극기(E) 쿨타임 -10%→-15%, SL스킬 쿨타임 -5%' → 스킬 분리 없이 내용 한 줄로 보존
    - '개발자 코멘트' 줄은 별도 엔트리로 분리
    - '기본 스탯' / '캐릭터 스탯' 줄도 스킬 헤더로 취급
    """
    entries = []
    current = None

    lines = [l for l in (normalize_cell_line(x) for x in cell_text.split("\n")) if l]

    for line in lines:
        if is_dev_comment_line(line):
            current = ["개발자 코멘트", []]
            entries.append(current)
            continue

        if line in ("기본 스탯", "캐릭터 스탯"):
            current = [line, []]
            entries.append(current)
            continue

        # 스킬명(조작키) 헤더 검사: 이름+키 뒤에 내용이 없어야 헤더로 인정
        m = re.match(
            r"^([가-힣A-Za-z0-9\s·ㆍ'/-]{1,30}" + SKILL_KEY_PATTERN.pattern + r")\s*(.*)$",
            line,
        )
        if m:
            rest = normalize_line(m.group(2))
            if not rest or not looks_like_change_content(rest):
                current = [normalize_line(m.group(1)), []]
                entries.append(current)
                if rest:
                    current[1].append(rest)
                continue
            # 뒤에 변경 내용이 붙어 있으면 → 그냥 내용 줄 (ex. 궁극기(E) 쿨타임 -10%→-15%)

        if current is None:
            current = ["변경 내용", []]
            entries.append(current)

        current[1].append(line)

    return [(name, ls) for name, ls in entries if ls or name not in ("변경 내용",)]


def extract_table_sections(soup):
    """
    페이지의 모든 <table>에서 밸런스 데이터를 행 단위로 수집.
    1열=이름(역할군/오브젝트/캐릭터), 2열=내용. 가격표는 제외.

    반환: OrderedDict {이름: [(이름, [줄...]), ...]}
    이전 버전의 '스킬 패턴이 있는 행만 통과' 조건을 없애서
    스트라이커/컨트롤러 역할군 버프처럼 스킬 패턴이 없는 행도 누락되지 않음.
    """
    result = OrderedDict()

    for table in soup.select("table"):
        rows = expand_table_rows(table)
        if not rows:
            continue

        # 가격표(상의/1단계...)는 제외
        first_texts = {normalize_line(c[1]) for c in rows[0] if c[1]}
        if first_texts & PRICE_TABLE_HEADERS:
            continue

        for row in rows:
            if len(row) < 2:
                continue

            name = normalize_line(row[0][1])
            detail = row[1][1]

            if not name or not detail:
                continue

            entries = split_cell_into_entries(detail)
            if not entries:
                continue

            if name in result:
                # rowspan 으로 이어진 행: 중복 엔트리 방지하며 병합
                existing = result[name]
                for entry in entries:
                    if entry not in existing:
                        existing.append(entry)
            else:
                result[name] = list(entries)

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

def build_skill_block(skill_name, lines):
    """스킬(또는 항목) 하나의 블록 텍스트"""
    if not lines:
        return ""

    if skill_name in ("변경 내용",):
        # 역할군 버프처럼 스킬 구분이 없는 경우: 굵은 헤더 없이 불릿만
        return "\n".join(f"　{l}" for l in lines).strip()

    out = [f"■ {skill_name}"]
    out += [f"　{l}" for l in lines]
    return "\n".join(out).strip()


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


def build_balance_fields_from_tables(table_sections):
    """표 기반: 행(역할군/오브젝트/캐릭터)별 필드 생성. 길면 자동 분할."""
    fields = []

    for name, entries in table_sections.items():
        blocks = []
        comments = []

        for entry_name, lines in entries:
            if entry_name == "개발자 코멘트":
                comments.append("\n".join(f"　{x}" for x in lines))
                continue
            block = build_skill_block(entry_name, lines)
            if block:
                blocks.append(block)

        for comment in comments:
            blocks.append("■ 개발자 코멘트\n" + comment)

        if not blocks:
            continue

        chunks = chunk_text_blocks(blocks, max_len=950)
        total = len(chunks)

        for idx, chunk in enumerate(chunks, start=1):
            field_name = name if total == 1 else f"{name} ({idx}/{total})"
            fields.append({
                "name": clip(field_name, 256),
                "value": clip(chunk, MAX_FIELD_VALUE),
                "inline": False,
            })

    return fields


def build_balance_fallback_fields(lines):
    """표가 아예 없는 옛 글용 fallback: 텍스트 줄을 묶어서 전송"""
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


# ---------- 매거진 파싱 (텍스트/이미지/영상 혼합) ----------

def extract_magazine_topic_urls(html_text):
    """매거진 목록 페이지에서 /article/magazine/topic/NNNN 링크 수집 (최신순)"""
    urls = re.findall(r"/article/magazine/topic/\d+", html_text)
    result, seen = [], set()
    for u in urls:
        full = urljoin(BASE_URL, u)
        if full not in seen:
            seen.add(full)
            result.append(full)
    return result


def build_magazine_title_map(html_text):
    """목록 페이지에서 {절대URL: 제목} 지도를 만든다 (a 태그의 title 속성)"""
    soup = BeautifulSoup(html_text, "html.parser")
    title_map = {}
    for a in soup.find_all("a", href=True):
        if "/article/magazine/topic/" in a["href"]:
            t = a.get("title") or normalize_line(a.get_text(" ", strip=True))
            if t:
                title_map[urljoin(BASE_URL, a["href"])] = t
    return title_map


def youtube_embed_to_watch(src: str):
    """
    YouTube iframe src 를 시청 URL로 변환.
    https://www.youtube.com/embed/OMSZq_ZraiM?si=... → https://www.youtube.com/watch?v=OMSZq_ZraiM
    """
    m = re.search(r"(?:youtube\.com/embed/|youtu\.be/)([A-Za-z0-9_-]{6,})", src)
    if m:
        return f"https://www.youtube.com/watch?v={m.group(1)}"
    # 이미 watch URL 형태면 그대로
    if "youtube.com/watch" in src or "youtu.be/" in src:
        return src
    return None


def collect_magazine_blocks(el, blocks):
    """
    매거진 본문을 문서 순서대로 수집.
    blocks: [(kind, payload)] — kind: 'text' | 'image' | 'video'
    """
    from bs4 import NavigableString, Tag

    for child in el.children:
        if isinstance(child, NavigableString):
            continue
        if not isinstance(child, Tag):
            continue

        if child.name in ("script", "style", "nav", "footer"):
            continue

        # 이미지
        if child.name == "img":
            src = child.get("src") or child.get("data-src") or ""
            if src:
                blocks.append(("image", urljoin(BASE_URL, src)))
            continue

        # 영상 (YouTube iframe 등)
        if child.name == "iframe":
            src = child.get("src") or ""
            video = youtube_embed_to_watch(src)
            if video:
                blocks.append(("video", video))
            continue

        # 표를 품은 컨테이너면 더 파고듦
        if child.find("table") or child.find("iframe") or child.find("img"):
            # 이 태그 자체에 직접 텍스트가 있는지도 확인
            own_text = "".join(
                s for s in child.find_all(string=True, recursive=False)
            ).strip()
            if own_text:
                blocks.append(("text", own_text))
            collect_magazine_blocks(child, blocks)
            continue

        # 일반 요소: 내부에 이미지/영상이 있으면 재귀, 없으면 텍스트로
        text = child.get_text("\n", strip=True)
        if text:
            blocks.append(("text", text))


def parse_magazine_post(url):
    """매거진 글 1개를 텍스트/이미지/영상 블록으로 파싱 (문서 순서 유지)"""
    soup = BeautifulSoup(fetch_html(url), "html.parser")

    container = pick_article_container(soup)

    # 제목: 목록 페이지 a 태그의 title 속성 우선 → 기존 추출기 폴백
    title = None
    try:
        title_map = build_magazine_title_map(fetch_html(MAGAZINE_LIST_URL))
        title = title_map.get(url)
    except Exception:
        pass
    if not title:
        title = extract_title_from_soup(soup)

    blocks = []
    collect_magazine_blocks(container, blocks)

    # 문서 순서 유지하면서 종류별 정리 + 중복 제거
    text_lines = []
    images = []
    videos = []

    for kind, payload in blocks:
        if kind == "text":
            for line in payload.split("\n"):
                line = normalize_cell_line(line)
                if not line or should_skip_line(line):
                    continue
                if is_section_header(line):
                    continue
                # 조회수/댓글수 같은 숫자-only 줄과 사이트 공통 타이틀 제외
                if re.fullmatch(r"[\d,]+", line):
                    continue
                if line in ("액션본능! 사이퍼즈", "CYPHERS"):
                    continue
                if line not in text_lines:
                    text_lines.append(line)
        elif kind == "image":
            # 페이지 UI 아이콘/썸네일 공통 리소스 제외 (실제 본문 이미지만)
            if "/thum/" in payload or "resource.cyphers.co.kr/ui/img" in payload:
                continue
            if payload not in images:
                images.append(payload)
        elif kind == "video":
            if payload not in videos:
                videos.append(payload)

    return {
        "title": title or "사이퍼즈 매거진",
        "url": url,
        "text_lines": text_lines,
        "images": images,
        "videos": videos,
    }


def build_magazine_payloads(post):
    """
    매거진 전송용 payload 생성.
    - 텍스트: 첫 embed 의 description (4096자 제한, 넘으면 분할)
    - 이미지: 1 embed 당 이미지 1개, 한 메시지 최대 10개
    - 영상: embed 는 재생 미리보기가 안 되므로 일반 메시지 content 로 전송
    """
    payloads = []

    # 1) 텍스트 embed (description 4096자 제한)
    desc_text = "\n".join(f"- {l}" for l in post["text_lines"]).strip()
    if desc_text:
        for i in range(0, len(desc_text), 4000):
            part = desc_text[i:i + 4000]
            payloads.append({
                "embeds": [{
                    "title": clip(post["title"], 256) if i == 0 else f"{post['title']} (계속)",
                    "url": post["url"],
                    "description": part,
                    "color": 0xE67E22,
                }]
            })

    # 2) 이미지 embeds: 1 embed = 이미지 1개, 메시지당 10개
    img_embeds = [
        {"image": {"url": img}, "color": 0xE67E22}
        for img in post["images"]
    ]
    for i in range(0, len(img_embeds), 10):
        payloads.append({"embeds": img_embeds[i:i + 10]})

    # 3) 영상: 일반 메시지 content (Discord 가 자동으로 미리보기 재생)
    if post["videos"]:
        content = "\n".join(post["videos"])
        payloads.append({"content": clip(content, 2000)})

    # 아무 것도 없을 때: 최소한 링크라도
    if not payloads:
        payloads.append({
            "embeds": [{
                "title": clip(post["title"], 256),
                "url": post["url"],
                "description": "이미지·영상·텍스트를 가져오지 못했습니다. 제목을 눌러 원문을 확인해 주세요.",
                "color": 0xE67E22,
            }]
        })

    return payloads


def run_magazine_check(state):
    """매거진 새 글 확인 → 전송 → state 갱신. state dict 를 갱신해 반환."""
    print("--- 매거진 체크 ---")
    html_text = fetch_html(MAGAZINE_LIST_URL)
    urls = extract_magazine_topic_urls(html_text)
    if not urls:
        print("매거진 글 URL을 찾지 못했습니다.")
        return state

    latest_url = urls[0]
    print("매거진 최신 URL:", latest_url)

    if state.get("magazine_last_url") == latest_url:
        print("매거진 새 글 없음")
        return state

    post = parse_magazine_post(latest_url)
    print("매거진 제목:", post["title"])
    print("텍스트 줄 수:", len(post["text_lines"]), "/ 이미지:", len(post["images"]), "/ 영상:", len(post["videos"]))

    payloads = build_magazine_payloads(post)
    print(f"매거진 전송할 payload 수: {len(payloads)}")
    for idx, payload in enumerate(payloads, start=1):
        print(f"매거진 payload 전송 중... ({idx}/{len(payloads)})")
        send_payload(payload)

    state["magazine_last_url"] = latest_url
    return state


# ---------- 게시글 파싱 ----------

def parse_post(url):
    soup = BeautifulSoup(fetch_html(url), "html.parser")

    title = extract_title_from_soup(soup)
    lines = article_text_lines(soup)
    sections = split_sections(lines)

    system_summary = build_summary_text(sections["system"], max_len=900)
    etc_summary = build_summary_text(sections["etc"], max_len=900)

    table_sections = extract_table_sections(soup)
    balance_dev_comments = extract_standalone_dev_comments(sections["balance"])

    return {
        "title": title,
        "url": url,
        "system_summary": system_summary,
        "etc_summary": etc_summary,
        "table_sections": table_sections,
        "balance_dev_comments": balance_dev_comments,
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

    # 2) 표 밖 문단형 '밸런스 개발자 코멘트' (역할군 조정 방향 등)
    if post["balance_dev_comments"]:
        for chunk in chunk_text_blocks(post["balance_dev_comments"], max_len=950):
            payloads.append({
                "embeds": [{
                    "title": "밸런스 개발자 코멘트",
                    "url": post["url"],
                    "color": 0xF1C40F,
                    "fields": [{
                        "name": "코멘트",
                        "value": clip(chunk, MAX_FIELD_VALUE),
                        "inline": False,
                    }],
                }]
            })

    # 3) 밸런스 상세 (표 전체 → 실패 시 fallback)
    balance_fields = build_balance_fields_from_tables(post["table_sections"])
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

    # 4) 버그 수정 / 기타
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

    # --- 1) 정기점검 업데이트 체크 ---
    try:
        latest_url, latest_title = find_latest_target_post()
        print("최신 URL:", latest_url)
        print("최신 제목:", latest_title)

        if state.get("last_url") == latest_url:
            print("새 업데이트 없음")
        else:
            post = parse_post(latest_url)
            if latest_title and latest_title.strip():
                post["title"] = latest_title

            print("system 요약 존재:", post["system_summary"] != "내용 없음")
            print("표 섹션 수:", len(post["table_sections"]))
            print("표 밖 개발자 코멘트 수:", len(post["balance_dev_comments"]))

            send_post_to_discord(post)
            state["last_url"] = latest_url
            print("전송 완료:", latest_url)
    except Exception as e:
        print(f"[warn] 업데이트 체크 실패: {e}")

    # --- 2) 매거진 체크 (업데이트와 독립 실행, 실패해도 업데이트는 유지) ---
    try:
        state = run_magazine_check(state)
    except Exception as e:
        print(f"[warn] 매거진 체크 실패: {e}")

    save_state(state)
    print("state 저장 완료:", state)


if __name__ == "__main__":
    main()
