#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
demo_root="$repo_root/demo-workspace"

if [[ "$demo_root" != "$repo_root/demo-workspace" ]]; then
  echo "Refusing unexpected demo path" >&2
  exit 2
fi
rm -rf "$demo_root"
mkdir -p "$demo_root"

run() {
  uv run --project "$repo_root" newsroom --root "$demo_root" "$@"
}

run init >/dev/null
cat > "$demo_root/candidates.csv" <<'CSV'
candidate_title,core_fact,event_date,company,product,topic,subtopic,event_type,keywords,primary_source,secondary_sources,discovered_from,priority
Example launches Alpha,Example launched Alpha on 2026-10-01.,2026-10-01,Example,Alpha,AI,Developer Tools,launch,example|alpha,https://example.com/alpha,https://news.example.org/alpha,offline-demo,5
Other launches Beta,Other launched Beta on 2026-10-02.,2026-10-02,Other,Beta,Cloud,Developer Tools,launch,other|beta,https://other.example/beta,,offline-demo,4
CSV

prepare_json="$(run batch prepare "$demo_root/candidates.csv")"
batch_id="$(printf '%s' "$prepare_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["batch_id"])')"
content_request="$(printf '%s' "$prepare_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["content_request_path"])')"
batch_root="$demo_root/workspace/batches/$batch_id"
content_result="$batch_root/claude/inbox/$(python3 - "$content_request" <<'PY'
import json
import sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text())["output_file"])
PY
)"

python3 - "$content_request" "$content_result" <<'PY'
import json
import sys
from pathlib import Path

request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
items = []
for index, source in enumerate(request["items"], start=1):
    items.append({
        "article_id": source["article_id"],
        "research_brief_md": f"# Research Brief {index}\n\nSynthetic source findings for the offline workflow demo.",
        "title_cn": f"离线批处理示例文章 {index}",
        "slug": f"offline-batch-demo-{index}",
        "article_md": f"# 离线批处理示例文章 {index}\n\n这是用于验证本地批处理工作流的合成文章，不代表真实新闻。",
        "image_candidates": [{
            "landing_page_url": f"https://images.example.org/demo-{index}",
            "direct_image_url": f"https://images.example.org/demo-{index}.svg",
            "creator": "Newsroom Demo",
            "rights_basis": "OWNED",
            "license_name": "Project-owned demo fixture",
            "rights_url": "https://example.com/demo-rights",
            "rights_statement": "This synthetic fixture is owned by the demo project.",
            "attribution_required": False,
            "attribution_text": None,
            "relevance": "Synthetic technology illustration for the matching demo article."
        }]
    })
result = {
    "schema_version": request["schema_version"],
    "batch_id": request["batch_id"],
    "request_id": request["request_id"],
    "output_file": request["output_file"],
    "items": items,
}
Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
PY

content_import="$(run batch content-import "$batch_id" "$content_result")"
fact_request="$(printf '%s' "$content_import" | python3 -c 'import json,sys; print(json.load(sys.stdin)["fact_check_request_path"])')"
fact_result="$batch_root/claude/inbox/$(python3 - "$fact_request" <<'PY'
import json
import sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text())["output_file"])
PY
)"

python3 - "$fact_request" "$fact_result" <<'PY'
import json
import sys
from pathlib import Path

request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
result = {
    "schema_version": request["schema_version"],
    "batch_id": request["batch_id"],
    "request_id": request["request_id"],
    "output_file": request["output_file"],
    "items": [{
        "article_id": item["article_id"],
        "outcome": "PASS",
        "report_md": "# Fact-check: PASS\n\nAll synthetic demo claims match the supplied fixture data.",
        "blocking_issues": [],
    } for item in request["items"]],
}
Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
PY

run batch fact-check-import "$batch_id" "$fact_result" >/dev/null
image_plan="$batch_root/image-selections.json"
publication_result="$batch_root/publication-result.json"
python3 - "$content_request" "$batch_root" "$repo_root/examples/hero.svg" "$image_plan" "$publication_result" <<'PY'
import json
import sys
from pathlib import Path
request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
batch_root = Path(sys.argv[2])
images = []
publications = []
for item in request["items"]:
    article_id = item["article_id"]
    candidates = json.loads((batch_root / "articles" / article_id / "image-candidates.json").read_text())
    images.append({
        "article_id": article_id,
        "image_candidate_id": candidates[0]["image_candidate_id"],
        "image_file": str(Path(sys.argv[3]).resolve()),
        "rights_confirmed": True,
    })
    publications.append({"article_id": article_id, "article_url": f"https://example.com/demo/{article_id}"})
for path, items in ((sys.argv[4], images), (sys.argv[5], publications)):
    Path(path).write_text(json.dumps({
        "schema_version": 1, "batch_id": request["batch_id"], "items": items,
    }, indent=2), encoding="utf-8")
PY

run batch image-plan "$batch_id" "$image_plan" >/dev/null
run batch review "$batch_id" --actor demo-editor --reason "Checked all articles, fact-checks, image files, and original rights metadata" >/dev/null
run batch approve "$batch_id" --actor demo-editor --reason "Approved whole synthetic offline demo batch" >/dev/null
run batch publish-packages "$batch_id" >/dev/null
run batch publish-register "$batch_id" "$publication_result" --actor demo-editor >/dev/null

test -f "$demo_root/workspace/batches/$batch_id/README.md"
test -f "$demo_root/workspace/batches/$batch_id/publish/batch-manifest.json"
test ! -d "$demo_root/research"
test ! -d "$demo_root/articles"
test ! -d "$demo_root/images"
python3 - "$demo_root" "$batch_id" <<'PY'
import sqlite3
import sys
from pathlib import Path
with sqlite3.connect(Path(sys.argv[1]) / "data/newsroom.db") as connection:
    articles = connection.execute("SELECT article_id,status FROM articles").fetchall()
    batch_status = connection.execute("SELECT status FROM batches WHERE batch_id=?", (sys.argv[2],)).fetchone()[0]
assert len(articles) == 2 and all(status == "PUBLISHED" for _, status in articles), articles
assert batch_status == "PUBLISHED", batch_status
print("Selected and published articles:", ", ".join(article_id for article_id, _ in articles))
PY

printf 'Demo batch: %s\n' "$batch_id"
run batch status "$batch_id"
run stats
run published list
