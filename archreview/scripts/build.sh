#!/usr/bin/env bash
# Build deployable bundles (no Docker needed):
#   build/mcp-standards/  build/mcp-catalog/  -> AgentCore Runtime direct-code packages (arm64, Python 3.13)
#   build/lambda/                              -> Lambda bundle (arm64, Python 3.13)
# Dependencies are installed from hash-locked requirement files.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
UV="${UV:-$ROOT/.venv/bin/uv}"
export UV_LINK_MODE=copy
PLAT=(--python-platform aarch64-manylinux2014 --python-version 3.13 --only-binary=:all: --require-hashes)

rm -rf build/mcp-standards build/mcp-catalog build/lambda
for srv in standards catalog; do
  out="build/mcp-$srv"
  mkdir -p "$out"
  "$UV" pip install --quiet "${PLAT[@]}" --target "$out" -r mcp_servers/requirements.lock
  cp mcp_servers/$srv/main.py mcp_servers/$srv/tools.py "$out/"
  cp -r mcp_servers/$srv/data "$out/data"
done

mkdir -p build/lambda
"$UV" pip install --quiet "${PLAT[@]}" --target build/lambda -r backend/requirements.lock
cp -r backend/src/archrev build/lambda/archrev

# Clean caches and set POSIX permissions required by AgentCore (644 files / 755 dirs)
find build -name "__pycache__" -type d -prune -exec rm -rf {} +
find build -type d -exec chmod 755 {} +
find build -type f -exec chmod 644 {} +
# Strip pip metadata noise that changes between builds (keeps asset hashes stable)
find build -name "direct_url.json" -delete
find build -name "RECORD" -path "*.dist-info/*" -delete
du -sh build/mcp-standards build/mcp-catalog build/lambda
