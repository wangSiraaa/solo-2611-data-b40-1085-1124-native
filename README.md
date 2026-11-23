case.json分录每行是[account,debit,credit]，金额为整数分，0代表该侧无金额；一行必须恰一侧大于0。document为null表示普通外部/期初凭证。seller_i1/buyer_i1文档镜像同i1；remaining_stock i1=2是买方尚未售出的件数。external_sale是买方对外售出两件的普通凭证；settlements独立发生，不改变剩余实物数量。材料不含账簿或合并服务代码。

---

仓库另含一道独立任务：**以旧换新·旧打字机拆件回收**（`tradein/`）。
它与上面的集团合并底稿（`group_consol/`）互不依赖。

- 文档：`docs/TRADEIN.md`
- 演示：`python3 scripts/run_tradein.py`
- 测试：`python3 -m unittest tests.test_tradein -v`
