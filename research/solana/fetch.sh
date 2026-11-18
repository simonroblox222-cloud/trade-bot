#!/usr/bin/env bash
# Stiahne repozitáre z repos.txt do vendor/ (shallow, bez histórie).
# Cudzí kód nespúšťaj bez kontroly a nikdy mu nedávaj private key hlavnej peňaženky.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p vendor
while read -r repo; do
  [[ -z "$repo" || "$repo" == \#* ]] && continue
  dir="vendor/${repo//\//__}"
  if [[ -d "$dir" ]]; then echo "skip $repo"; continue; fi
  git clone --depth 1 -q "https://github.com/$repo.git" "$dir" && echo "ok   $repo" || echo "FAIL $repo"
done < repos.txt
