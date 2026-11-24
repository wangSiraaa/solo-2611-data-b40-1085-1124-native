#!/usr/bin/env bash
# 一键复跑：功能断言（含手算案例）、20/300/1000 规模测试、CLI 子进程端到端，
# 以及齿轮教具 / 演示台 / 测速盘端到端。
set -euo pipefail
cd "$(dirname "$0")"
echo "=== 1/4 功能测试（手算小例 + 六类规定场景 + 重开复读） ==="
python3 -m tests.test_service
echo
echo "=== 2/4 规模测试（20 模型 / 300 格 / 1000 引用 / 12 位分数） ==="
python3 -m tests.test_scale
echo
echo "=== 3/4 CLI 端到端（put-model/snapshot/compile/solve/query/explain/verify） ==="
python3 -m tests.demo_cli
echo
echo "=== 4/4 齿轮教具（测具岗 / 演示台 / 测速盘换盘） ==="
python3 -m tests.test_gear_demo
echo
echo "=== 齿轮演示 CLI 走查 ==="
python3 -m tests.demo_gear
