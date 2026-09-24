#!/usr/bin/env bash
# run-tests.sh — 全テストをファイル単位の別プロセスで実行する
#
# 背景: 全体テストを単一プロセスで実行すると、fastembed の embedding モデル
# （~670MB/回）がプロセス内に蓄積し、7GB 級マシンではスワップ枯渇 → ハング、
# OOM キラーが opencode など他プロセスを巻き込むことがある。
# このスクリプトは:
#   1. テストファイルごとに新しい python プロセスを起動（終了時にメモリ解放）
#   2. systemd-run の cgroup メモリ制限で隔離（OOM がシステム全体に波及しない）
#
# 使い方:
#   ./scripts/run-tests.sh            # 通常実行
#   TEST_MEMLIMIT=2G ./scripts/run-tests.sh   # メモリ上限を変更
#   TEST_TIMEOUT=300 ./scripts/run-tests.sh   # ファイルごとのタイムアウト変更
#   TEST_FILE=tests/test_validators.py ./scripts/run-tests.sh  # 単一ファイル

set -u
cd "$(dirname "$0")/.."

MEMLIMIT="${TEST_MEMLIMIT:-3G}"
TIMEOUT="${TEST_TIMEOUT:-300}"

# プロジェクト venv を優先（無ければ PATH の python3）
if [ -x ".venv/bin/python" ]; then
    PY="$(pwd)/.venv/bin/python"
else
    PY="python3"
fi
FAILED=0
RAN=0

# systemd-run が無い環境（コンテナ等）はフォールバックで直接実行する
# （別プロセス化は維持されるためメモリ蓄積の主因は回避できる）
if command -v systemd-run >/dev/null 2>&1; then
    run_isolated() {
        systemd-run --user --scope -p MemoryMax="$MEMLIMIT" -- "$@"
    }
else
    run_isolated() {
        "$@"
    }
fi

# 単一ファイル指定があればそれだけ実行
if [ -n "${TEST_FILE:-}" ]; then
    echo "=== $TEST_FILE ==="
    run_isolated timeout "$TIMEOUT" "$PY" -m pytest "$TEST_FILE" -q -p no:cacheprovider
    exit $?
fi

for f in tests/test_*.py; do
    [ -f "$f" ] || continue
    echo "=== $f ==="
    RAN=$((RAN + 1))
    if run_isolated timeout "$TIMEOUT" "$PY" -m pytest "$f" -q -p no:cacheprovider; then
        echo "PASS: $f"
    else
        echo "FAIL: $f"
        FAILED=$((FAILED + 1))
    fi
done

echo "----"
echo "ran=$RAN failed=$FAILED"
exit $((FAILED > 0 ? 1 : 0))
