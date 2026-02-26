import asyncio
import json
import re
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from sites import SITES, TABS

DATA_FILE = Path(__file__).parent / "data" / "agents.json"
MAX_RETRIES = 3


def parse_region(address: str) -> str:
    """주소에서 구/군/시 단위 지역명 추출. 실패 시 '기타' 반환."""
    if not address:
        return "기타"
    # 구/군 우선 (가장 세부)
    match = re.search(r"(\S+[구군])", address)
    if match:
        return match.group(1)
    # 시: 특별시/광역시/도 다음에 오는 세부 시
    match = re.search(r"(?:특별시|광역시|특별자치시|도)\s+(\S+시)", address)
    if match:
        return match.group(1)
    return "기타"


def parse_card_profile(card) -> dict:
    """Format 1: div.inf 존재 (시도회장, 지회장 등 프로필형)
    이름은 strong.lc01 또는 dd[font-weight:bold] 에 위치.
    """
    inf = card.select_one("div.inf")
    if not inf:
        return {}

    # 이름 추출: strong.lc01 우선, 없으면 bold dd 태그
    name_tag = inf.select_one("strong.lc01")
    if name_tag:
        name = name_tag.get_text(strip=True)
    else:
        # 경기 사이트: <dd style="...font-weight:bold...">이름</dd>
        dd_tags = card.select("dd")
        name = ""
        for dd in dd_tags:
            style = dd.get("style", "")
            if "font-weight:bold" in style or "font-weight: bold" in style:
                name = dd.get_text(strip=True)
                break
    if not name:
        return {}

    office = address = phone = fax = ""
    for row in inf.select("table tr"):
        cells = row.find_all("td")
        if not cells:
            continue
        label = cells[0].get_text(strip=True)
        if label == "사무소명칭" and len(cells) >= 2:
            office = cells[1].get_text(strip=True)
        elif label in ("사무소 소재지", "사무소소재지") and len(cells) >= 2:
            address = cells[1].get_text(strip=True)
        elif label == "일반전화" and len(cells) >= 2:
            phone = cells[1].get_text(strip=True)
            if len(cells) >= 3:
                fax_text = cells[2].get_text(strip=True)
                fax = re.sub(r"^FAX\s*", "", fax_text).strip()

    return {"name": name, "office": office, "address": address, "phone": phone, "fax": fax}


def parse_card_list(card) -> dict:
    """Format 2: div > table with 이름/사무소소재지 rows (분회장 등 목록형)"""
    name = office = address = phone = fax = ""
    for row in card.select("table tr"):
        cells = row.find_all("td")
        if not cells:
            continue
        label = cells[0].get_text(strip=True)

        if label == "이름" and len(cells) >= 2:
            name_strong = cells[1].find("strong")
            name = name_strong.get_text(strip=True) if name_strong else cells[1].get_text(strip=True)
            # 같은 행에 사무소명칭이 있을 수 있음
            if len(cells) >= 3:
                office_text = cells[2].get_text(strip=True)
                office = re.sub(r"^사무소명칭\s*", "", office_text).strip()
        elif label in ("사무소소재지", "사무소 소재지") and len(cells) >= 2:
            addr_strong = cells[1].find("strong")
            address = addr_strong.get_text(strip=True) if addr_strong else cells[1].get_text(strip=True)
        elif label == "일반전화" and len(cells) >= 2:
            phone = cells[1].get_text(strip=True)
            if len(cells) >= 3:
                fax_text = cells[2].get_text(strip=True)
                fax = re.sub(r"^FAX\s*", "", fax_text).strip()

    return {"name": name, "office": office, "address": address, "phone": phone, "fax": fax} if name else {}


def parse_cards(html: str, tab_name: str, site_name: str, seen: set) -> list:
    """AJAX 응답 HTML에서 모든 name_card를 파싱하여 레코드 리스트 반환."""
    soup = BeautifulSoup(html, "html.parser")
    records = []

    for card in soup.select("div.name_card"):
        # Format 1: div.inf 존재 여부로 구분
        if card.select_one("div.inf"):
            data = parse_card_profile(card)
        else:
            data = parse_card_list(card)

        if not data or not data.get("name"):
            continue

        key = f"{data['name']}|{data['phone']}"
        if key in seen:
            print(f"    중복 스킵: {data['name']} ({data['phone']})")
            continue
        seen.add(key)

        region = parse_region(data["address"])
        records.append({
            "site": site_name,
            "tab": tab_name,
            "name": data["name"],
            "office": data["office"],
            "address": data["address"],
            "phone": data["phone"],
            "fax": data["fax"],
            "region": region,
        })

    return records


async def scrape_tab(page, tab_name: str, site_name: str, code1: str, seen: set) -> list:
    """단일 탭 스크래핑. 실패 시 최대 MAX_RETRIES회 재시도."""
    for attempt in range(MAX_RETRIES):
        try:
            ajax_html = None

            async def capture_ajax(response):
                nonlocal ajax_html
                if "construction_ajax" in response.url or "construction_gn_ajax" in response.url:
                    try:
                        body = await response.body()
                        try:
                            ajax_html = body.decode("euc-kr")
                        except Exception:
                            ajax_html = body.decode("utf-8", errors="replace")
                    except Exception:
                        pass

            page.on("response", capture_ajax)
            await page.evaluate(f"fnChangeGrade('{code1}', '', '{tab_name}')")
            await page.wait_for_timeout(2000)
            page.remove_listener("response", capture_ajax)

            if not ajax_html:
                raise Exception("AJAX 응답 없음")

            records = parse_cards(ajax_html, tab_name, site_name, seen)
            print(f"  [{tab_name}] {len(records)}건 수집")
            return records

        except PlaywrightTimeoutError:
            wait = 2 ** attempt
            if attempt < MAX_RETRIES - 1:
                print(f"  [{tab_name}] 타임아웃 — {wait}초 후 재시도 ({attempt+1}/{MAX_RETRIES})")
                await asyncio.sleep(wait)
            else:
                print(f"  [{tab_name}] 최대 재시도 초과, 스킵")
                return []
        except Exception as e:
            wait = 2 ** attempt
            if attempt < MAX_RETRIES - 1:
                print(f"  [{tab_name}] 오류({e}) — {wait}초 후 재시도 ({attempt+1}/{MAX_RETRIES})")
                await asyncio.sleep(wait)
            else:
                print(f"  [{tab_name}] 실패: {e}")
                return []

    return []


async def scrape_site(browser, site: dict, seen: set) -> list:
    """단일 사이트 전체 스크래핑."""
    print(f"\n[{site['name']}] 시작: {site['url']}")
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        locale="ko-KR",
    )
    page = await context.new_page()
    records = []

    try:
        # 메인 페이지 먼저 방문하여 code1 세션 설정
        await page.goto(site["url"], wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(1000)

        # 메인 페이지 HTML에서 code1 추출
        html = await page.content()
        code1_match = re.search(r"var\s+code1\s*=\s*['\"](\w+)['\"]", html)
        code1 = code1_match.group(1) if code1_match else ""
        print(f"  code1: {code1!r}")

        # 조직도 페이지 이동
        await page.goto(f"{site['url'].rstrip('/')}/ptemplate/construction.asp",
                        wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)
        print(f"  조직도 페이지 진입 완료")

        for tab_name in TABS:
            tab_records = await scrape_tab(page, tab_name, site["name"], code1, seen)
            records.extend(tab_records)

    except Exception as e:
        print(f"  [{site['name']}] 사이트 접근 실패: {e}")
    finally:
        await context.close()

    print(f"[{site['name']}] 완료: 총 {len(records)}건")
    return records


async def scrape_all():
    """모든 사이트 스크래핑 후 JSON 저장."""
    all_records = []
    seen: set = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for site in SITES:
            records = await scrape_site(browser, site, seen)
            all_records.extend(records)
        await browser.close()

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False, indent=2)

    print(f"\n저장 완료: {DATA_FILE} ({len(all_records)}건)")
    return all_records


if __name__ == "__main__":
    asyncio.run(scrape_all())
