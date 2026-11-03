# 贡献指南

欢迎提 Issue 和 PR。这个项目的主张是：**可审计 > 聪明**。

## 开发约定

- **零第三方依赖**：只用 Python 标准库 + 原生前端，不引入 numpy/pandas/框架；
- **纯函数优先**：`indicators.py` / `strategy.py` 不碰 IO，便于回测复用；
- **风控写在代码里**：不许把止损、仓位上限这类硬约束交给模型"自觉"；
- **可审计**：任何新增的 AI 决策都要落盘（提示词、原始输出、裁决结果）。

## 本地自检（改完必须全绿）

```bash
python main.py test          # 数据源连通
python selftest.py           # 盯盘告警规则
python selftest_ai.py        # 硬风控 11 个越界场景
python selftest_plans.py     # 计划单执行规则
python main.py ai --doctor   # 模型网关（需要你自己的 Key）
node selftest_kline.js 8770  # K 线渲染（先 python main.py serve）
```

## 提交前检查

- 不要提交 `config.json`、`data/`、任何密钥；
- 新增文件请保持 UTF-8（无 BOM），`.bat` 用 **ASCII + CRLF**；
- 代码注释与文档用中文，标识符用英文。
