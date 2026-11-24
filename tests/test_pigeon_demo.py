"""演示真值：pigeon.demo 的各场景结论与独立预期一致。"""

from __future__ import annotations

from pigeon.demo import run


def test_demo_outcomes():
    report = run()
    assert report["原案"]["裁决"] == "盖章"
    assert report["读环缺失"]["裁决"] == "待核"
    assert len(report["读环缺失"]["相反两份见证"]) == 2
    assert report["乙航距更正"]["裁决"] == "撤证"
    assert report["截断尾部"]["封棚"] is False
    assert report["截断尾部"]["裁决"] == "未封棚"
    check = report["互斥评比"]["当场对质"]
    assert check["jointly_satisfiable"] is False
    assert check["favorable_verdict"] == "不同真"
    review = report["复议"]
    assert review["联合听证·基准"]["保证期限内判定件数"] == 2
    assert review["队席分权·基准"]["逾期未判"] == []
    tight = review["联合听证·期限压到同一末拍"]
    assert tight["保证期限内判定件数"] == 1
    assert len(tight["开庭记录"]) == 1  # 不虚报已开两片
    assert tight["逾期未判"] == ["复议二"]
    assert review["联合听证·一片多用"]["保证期限内判定件数"] == 3
    shared = review["队席分权·孤本不得转用"]
    assert shared["逾期未判"] == ["复议乙"]
    assert "已拦截" in shared["重复发权"]
