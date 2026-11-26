# -*- coding: utf-8 -*-
"""
사이퍼즈 정기점검 공지를 원본 표(<table>) 구조 그대로 Markdown / CSV 로 저장하는 스크립트

사용법:
    python3 cyphers_notice_scraper.py                    # 최신 '정기점검 업데이트' 글 자동 탐색
    python3 cyphers_notice_scraper.py <공지URL>          # 특정 글 직접 지정

출력:
    notice_<MMDD>.md        - 표가 살아있는 Markdown 원문
    notice_<MMDD>_table_N.csv - 표마다 CSV 1개 (rowspan/colspan 확장됨)

※ main.py 와 같은 폴더에 있어야 공용 함수(탐색/파싱)를 재사용할 수 있습니다.
"""

import sys
import csv
import re
from bs4 import BeautifulSoup, NavigableString, Tag

from main import (
    HEADERS, TIMEOUT, fetch_html, find_latest_target_post,
    extract_title_from_soup, pick_article_container, normalize_line,
    expand_table_rows,
)

import requests


def grid_to_markdown(rows):
    """rowspan/colspan 확장된 그리드 → Markdown 표"""
    if not rows:
        return ""
    md = []
    header = [normalize_line(c[1]).replace("|", "\\|") or " " for c in rows[0]]
    md.append("| " + " | ".join(header) + " |")
    md.append("|" + "|".join([" --- "] * len(header)) + "|")
    for row in rows[1:]:
        cells = [normalize_line(c[1]).replace("|", "\\|").replace("\n", "<br>") or " " for c in row]
        md.append("| " + " | ".join(cells) + " |")
    return "\n".join(md)


def grid_to_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow([normalize_line(c[1]) for c in row])


def collect_blocks(el, blocks):
    """본문을 문서 순서대로 (제목/문단/표) 블록으로 수집 — 표는 통째로 보존"""
    for child in el.children:
        if isinstance(child, NavigableString):
            continue
        if not isinstance(child, Tag):
            continue

        if child.name == "table":
            blocks.append(("table", child))

        elif re.match(r"^h[1-6]$", child.name or ""):
            text = normalize_line(child.get_text(" ", strip=True))
            if text:
                blocks.append(("h", text))

        elif child.name in ("script", "style", "nav", "footer"):
            continue

        elif child.find("table"):
            # 표를 품은 컨테이너면 더 파고듦
            collect_blocks(child, blocks)

        else:
            text = child.get_text("\n", strip=True)
            if text:
                blocks.append(("text", text))


def scrape(url: str, md_path: str, csv_prefix: str):
    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")
    container = pick_article_container(soup)
    title = extract_title_from_soup(soup)

    blocks = []
    collect_blocks(container, blocks)

    table_no = 0
    md_lines = [f"# {title}", "", f"출처: {url}", ""]

    for kind, payload in blocks:
        if kind == "h":
            md_lines += [f"## {payload}", ""]
        elif kind == "text":
            for line in payload.split("\n"):
                line = normalize_line(line)
                if not line:
                    continue
                if line in md_lines[-2:] if len(md_lines) > 1 else False:
                    continue
                md_lines.append(line)
            md_lines.append("")
        elif kind == "table":
            rows = expand_table_rows(payload)
            if not rows:
                continue
            table_no += 1
            md_lines += [grid_to_markdown(rows), ""]
            grid_to_csv(rows, f"{csv_prefix}_{table_no}.csv")
            print(f"  표 {table_no}: {len(rows)}행 × {len(rows[0])}열 → CSV 저장")

    md_text = "\n".join(md_lines)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_text)

    print(f"저장 완료: {md_path} (표 {table_no}개)")
    return md_text


def main():
    if len(sys.argv) > 1:
        url = sys.argv[1]
        print(f"지정된 공지 처리: {url}")
    else:
        url, title = find_latest_target_post()
        print(f"최신 공지 자동 탐색: {url} ({title})")

    # 파일명용 날짜: 제목에서 MMDD 추출, 실패 시 오늘
    _, latest_title = (url, None)
    try:
        soup = BeautifulSoup(fetch_html(url), "html.parser")
        latest_title = extract_title_from_soup(soup)
    except Exception:
        pass

    m = re.search(r"(\d{1,2})월\s*(\d{1,2})일", latest_title or "")
    if m:
        tag = f"{int(m.group(1)):02d}{int(m.group(2)):02d}"
    else:
        import datetime
        tag = datetime.datetime.now().strftime("%m%d")

    scrape(url, f"notice_{tag}.md", f"notice_{tag}_table")


if __name__ == "__main__":
    main()
