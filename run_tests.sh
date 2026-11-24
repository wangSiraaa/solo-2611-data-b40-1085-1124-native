#!/usr/bin/env bash
# 一键复跑：功能断言（含手算案例）、20/300/1000 规模测试、CLI 子进程端到端、
# 三色冰砖模拟（备料台/单窗/两窗试切）与全部题面杯单。
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
echo "=== 4/4 三色冰砖模拟（27 项断言 + 题面杯单演示） ==="
python3 -m tests.test_icebrick
python3 -m icebrick.demo > /tmp/icebrick_demo.out
echo "冰砖演示输出：/tmp/icebrick_demo.out（$(grep -c '^====' /tmp/icebrick_demo.out) 个场景段）"
