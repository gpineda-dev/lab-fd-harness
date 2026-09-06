#!/usr/bin/env bash
# run-demo.sh - Demonstrates both command-wrapping and pipe modes of fd-harness redact

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "========================================================="
echo "Mode 1: Command Wrapping (Intercepting 'cat' stdout/stderr)"
echo "========================================================="
uv run fd-harness redact --rules "$DIR/dlp-rules.toml" -- cat "$DIR/demo-server-logs.txt"

echo ""
echo "========================================================="
echo "Mode 2: Standard Unix Pipe ('cat ... | fd-harness redact')"
echo "========================================================="
cat "$DIR/demo-server-logs.txt" | uv run fd-harness redact --rules "$DIR/dlp-rules.toml"

echo ""
echo "========================================================="
echo "Mode 3: Ad-hoc CLI rule without TOML file (--mask)"
echo "========================================================="
echo "My internal secret is SECRET-998811-XYZ" | uv run fd-harness redact -m 'SECRET-[0-9]+-[A-Z]+=[SHIELDED]'
