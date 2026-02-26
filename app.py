import json
from pathlib import Path

from flask import Flask, render_template, request

app = Flask(__name__)
DATA_FILE = Path(__file__).parent / "data" / "agents.json"


SITE_NAME_MAP = {
    "경기": "경기남부",
}


def load_data():
    if not DATA_FILE.exists():
        return []
    with open(DATA_FILE, encoding="utf-8") as f:
        data = json.load(f)
    for r in data:
        r["site"] = SITE_NAME_MAP.get(r["site"], r["site"])
    return data


@app.route("/")
def index():
    data = load_data()

    # 쿼리 파라미터
    sel_site = request.args.get("site", "")
    sel_tab = request.args.get("tab", "")
    sel_region = request.args.get("region", "")
    query = request.args.get("q", "").strip()

    # 필터 옵션 목록 (정렬)
    sites = sorted({r["site"] for r in data})
    tabs = sorted({r["tab"] for r in data})

    # 지역은 선택된 사이트 기준으로 필터링
    site_filtered = [r for r in data if not sel_site or r["site"] == sel_site]
    regions = sorted({r["region"] for r in site_filtered})

    # 필터 적용
    results = data
    if sel_site:
        results = [r for r in results if r["site"] == sel_site]
    if sel_tab:
        results = [r for r in results if r["tab"] == sel_tab]
    if sel_region:
        results = [r for r in results if r["region"] == sel_region]
    if query:
        results = [
            r for r in results
            if query in r["name"] or query in r["office"]
        ]

    return render_template(
        "index.html",
        results=results,
        sites=sites,
        tabs=tabs,
        regions=regions,
        sel_site=sel_site,
        sel_tab=sel_tab,
        sel_region=sel_region,
        query=query,
        total=len(results),
        has_data=bool(data),
    )


if __name__ == "__main__":
    app.run(debug=True)
