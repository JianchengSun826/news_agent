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

run init
import_json="$(run candidate import "$repo_root/examples/candidates.csv")"
candidate_ids=()
while IFS= read -r candidate_id; do
  candidate_ids+=("$candidate_id")
done < <(printf '%s' "$import_json" | python3 -c 'import json,sys; print(*json.load(sys.stdin)["candidate_ids"], sep="\n")')

expected=(NEW DUPLICATE FOLLOW_UP RELATED NEW)
selected_indexes=(0 2 4)
article_ids=()

for index in "${!candidate_ids[@]}"; do
  candidate_id="${candidate_ids[$index]}"
  result="$(run duplicate-check "$candidate_id")"
  classification="$(printf '%s' "$result" | python3 -c 'import json,sys; print(json.load(sys.stdin)["classification"])')"
  if [[ "$classification" != "${expected[$index]}" ]]; then
    echo "Unexpected classification for candidate $index: $classification" >&2
    exit 1
  fi
  if [[ "$classification" == "NEW" && "$index" == "0" ]]; then
    run candidate select "$candidate_id" --actor demo-editor --reason "Selected anchor event for duplicate demo" >/dev/null
  fi
done

for index in "${selected_indexes[@]}"; do
  candidate_id="${candidate_ids[$index]}"
  if [[ "$index" != "0" ]]; then
    run candidate select "$candidate_id" --actor demo-editor --reason "Selected for end-to-end demo" >/dev/null
  fi
  article_json="$(run brief create "$candidate_id")"
  article_id="$(printf '%s' "$article_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["article_id"])')"
  article_ids+=("$article_id")
  run prompt create "$article_id" --purpose research >/dev/null
  run research register "$article_id" --file "$repo_root/examples/research-result.md" >/dev/null
  run prompt create "$article_id" --purpose writing >/dev/null
  run article register "$article_id" --file "$repo_root/examples/article.md" --title "示例科技新闻稿 $index" --slug "demo-tech-news-$index" >/dev/null
  run prompt create "$article_id" --purpose fact_check >/dev/null
  run fact-check register "$article_id" --file "$repo_root/examples/fact-check.md" >/dev/null
  run prompt create "$article_id" --purpose image >/dev/null
  run image register "$article_id" --type AI_GENERATED --ai-provider Gemini --ai-model manual-subscription --prompt-path "output/prompts/$article_id-image.md" >/dev/null
  run review "$article_id" --actor demo-editor --reason "Checked title, dates, sources, claims, duplicate result, and image provenance" >/dev/null
  run approve "$article_id" --actor demo-editor --reason "Approved synthetic demo package" >/dev/null
  run publish-package "$article_id" >/dev/null
  run publish "$article_id" --actor demo-editor --url "https://example.com/demo/$article_id" >/dev/null
done

printf 'Demo classifications: %s\n' "${expected[*]}"
printf 'Selected and published articles: %s\n' "${article_ids[*]}"
run stats
run published list
