# GLM-5.3 researcher：同设置实验

遵循[当前完整交接README](../2026-09-19-astra-expanded/README.md)。本配置仅将researcher替换为 `zai-org/GLM-5.3` / Claude Code / DeepInfra原生Messages接口（effort max），采用已冻结价格input $1.2 / output $4 per million、cache read multiplier 0.1。不是重新查询的实时价格。

相同prompt、同一私有输入包、候选/目标/辅助模型、3小时及90分钟reprompt、独立$100/$100/$120上限、最终至少100题及全可用面板验收。模型与harness变化需报告；不改旧Astra钱包。

按交接README配置本地runtime及环境变量后执行：

```bash
seb experiment validate --config contracts/2026-09-19-glm53-expanded/config.yaml
seb experiment run --config contracts/2026-09-19-glm53-expanded/config.yaml --researcher r-glm-5.3 --output /absolute/private/new-run
```

命令产生新独立实验；不要把它作为原任务重试命令。

Claude Code同上下文接续现由 `seb/claude_continuation.py` 实现：保存每次原始trace、使用原session ID、递减剩余时间；90分钟规则与Codex一致，终态传输错误最多接续两次，政策/认证/额度错误不自动重试。此为本次新增harness支持；运行代码版本需与Astra旧版本分开记录。
