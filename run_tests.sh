#!/usr/bin/env bash
# 一键复跑：旧预算解算器的功能/规模/CLI 测试 + 烘焙展盲评/颁章/复尝系统测试与演示。
set -euo pipefail
cd "$(dirname "$0")"
echo "=== 1/5 功能测试（手算小例 + 六类规定场景 + 重开复读） ==="
python3 -m tests.test_service
echo
echo "=== 2/5 规模测试（20 模型 / 300 格 / 1000 引用 / 12 位分数） ==="
python3 -m tests.test_scale
echo
echo "=== 3/5 CLI 端到端（put-model/snapshot/compile/solve/query/explain/verify） ==="
python3 -m tests.demo_cli
echo
echo "=== 4/5 盲评/颁章/复尝系统测试（Part C 规定例 + 通用机制） ==="
python3 -m tests.test_baking_judge
echo
echo "=== 5/5 盲评系统可复跑演示（逐条算 Part C） ==="
python3 -m tests.demo_baking_judge
