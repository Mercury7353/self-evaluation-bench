# 合作者交接：2026-09-19 固定评测契约

代码仓库：https://github.com/Mercury7353/self-evaluation-bench ，分支 `campaign/restart-20260915`。
本包是本轮新 researcher 实验的入口；不要从 09-15 的 Claude 模板复制旧时限或旧评分模式。
实验记录在 MyContext 的 `research/self-evaluation-benchmark`，此仓库只维护代码与使用说明。

## 已固定的共同约定

| 项目 | 约定 |
|---|---|
| 输入 | 中性研究 prompt、可执行 SDK、4 个开发模型句柄、8 个 visible 目标的开发标签和获准资源；没有强制辅助题库 |
| 输出 | 自包含 `run.py`、`evaluation.json`、README 和判分资产；至少100道不同题、逐题实测分、三个冻结域聚合规则 |
| 领域 | 一次联合研究 coding / co-work / reasoning；不要求每域100题；同一情景多个评分标准仍只算一题 |
| 优化 | 各目标的实测域分与参考分做 Pearson → 域内平均 → 三域等权；高正确率本身不奖励 |
| 禁止替代 | 不拟合预测分，不按模型身份或参考标签生成成绩，不在验收后修改聚合/题目 |
| 研究窗口 | 3小时；返回时若剩余至少90分钟，同一上下文收到中性继续提示；不重置计时 |
| 钱包 | 每次新实验 researcher $100、开发/辅助调用 $100、验收 $120；每候选整套上限 $30 |
| 迭代 | researcher自行读资源、选题/合成、测试、读逐题证据及开发反馈、改题/判分并留存版本；可用小pilot |
| 冻结 | 最后有效保存版本；不是按验收挑最优。最后修改不强制重新开发测试 |
| 验收 | 冻结后重新测全部可用候选，包含开发与sealed；不能假定开发已测全 |
| 重试 | 有界基础设施恢复，保留已完成错答/空答与已有费用；不能答案导向重跑或释放未知费用 |

无固定2500输出token限制，由 researcher 在平台和费用限制内选择并冻结协议。
`design.rounds: 1` 是一个持续研究窗口，不是只允许一次pilot；SDK内可多次实验。
验收单独有24小时窗口，不占研究3小时；不是无限重试或保证一定全覆盖。
钱包按配置价格、缓存usage记账；保守预留、未知费用和真实发票须区分。
费用预留可能令 researcher 在实际支出未满$100时停止，最后有效快照仍按契约冻结。

## Visible / sealed 是两个独立维度

候选：4开发（GPT-5.5、GLM-5.2、DeepSeek V4 Flash、Qwen3.8 Max），10登记sealed；其中MiniMax M2.7 pending，不替换为M3，当前13可执行。
所有新增candidate均sealed；合作者更换的是researcher，不是随意更换candidate panel。

| 领域 | Visible | Sealed（仅操作员可见） |
|---|---|---|
| Coding | DeepSWE、SciCode、SWE Multilingual、SWE Atlas | TB2.1、SWE Verified、ProgramBench |
| Co-work | GDPval、Advanced IF | BrowseComp、DeepResearchBench II、Harvey LAB |
| Reasoning | HLE、ArxivMath | AA-LCR、SUPER Chem |

本目录是**操作员文档**，不可整包塞给researcher。控制器仅交付visible资源和开发标签。
输入中DeepSWE/GDPval/HLE/ArxivMath有原题材料，DeepSWE/ArxivMath有整理轨迹；其余visible有描述与标签，没有预装原题包。
研究者可联网取得允许的资源，须记录出处；不得检索非开发标签或探查sealed目标。
sealing是本地资料/API隔离加协作约定，不是公共互联网的信息保密保证。

## 评分口径：不要混用两个报表

执行配置 `domain_protocol.minimum_models` 和 `overall.minimum_models` 均为 **3**，`raw_domain` / `macro_pearson`，无predictor。
每目标缺参考、缺实测、常数分及不满足条件均需明确记录，不能把缺项当完整验收。
MyContext中Astra/Sol/Random对照另用**三方共同完整面板、每目标至少4模型**，并按同一目标集平均。
这是显式的历史横向比较口径，不是偷偷将运行契约改成4；合作者应同时保留配置原生报表与对照报表。
Visible、sealed、combined结果分别标明；公开参考跨不同协议，不能称为同协议真值矩阵。
不同researcher更换模型/harness会有协议差异，记录清楚，不因名次调整面板。

## 安装与启动

按仓库根README的安装段安装Python3.12、Bubblewrap与兼容rootfs。Codex另需Node和锁定SDK：

```bash
git clone --branch campaign/restart-20260915 https://github.com/Mercury7353/self-evaluation-bench.git
cd self-evaluation-bench
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,agent]'
npm ci --prefix harness/codex --ignore-scripts --no-audit --no-fund
```

Claude researcher需要本机原生Linux Claude Code可执行文件；不需要Codex SDK来运行Claude。
跨机器先准备rootfs/science_packages/image_cache，修改本地配置中的绝对路径；不要直接使用示例NFS路径。

1. 私下取得**同一冻结输入包**，设置 `SEB_ASTRA_INPUTS`。目录结构为 `references/<target>.json` 和 `materials/<visible-target>/`。Git中不含真实数据/标签，clone本身不够启动可比实验；不可自行换成新下载的榜单。
2. 复制本目录至私有配置目录，保留 `instructions.txt` 的相对关系。保持candidate、辅助模型、目标、价格、可见性、预算、时限、验收策略不变。
3. 仅替换 `researchers` 条目的id/model/provider/harness/effort/native_limits与对应真实价格缓存规则；新供应商在providers增添配置，key只走环境变量。删除继承的旧researcher model_key等元数据或同步修改，别把Astra价格套给Claude。
4. OpenAI researcher使用 `codex` / `openai_responses`；Claude使用 `claude_code`，必须核验原生工具调用及完整工具返回轮次。网关默认Messages路径为 `upstream + /anthropic/v1/messages`，不能盲填会重复拼接的 `/v1` 地址。先确认实际端点兼容。
5. 记录代码commit、配置/prompt/输入哈希和harness版本。用真实环境完成零费用验证，再由操作者启动自己的独立付费实验。

```bash
export SEB_ASTRA_INPUTS=/absolute/private/frozen-inputs
# 在私有环境加载 SEB_OPENAI_KEY / SEB_DEEPINFRA_KEY / 新researcher所需key。
seb doctor --rootfs /absolute/path/to/rootfs
seb experiment validate --config /absolute/private/contract/config.yaml
# validate不调用模型；以下run会产生费用，且output必须是全新目录。
seb experiment run --config /absolute/private/contract/config.yaml \
  --researcher YOUR_RESEARCHER_ID --output /absolute/private/runs/unique-run
seb budget /absolute/private/runs/unique-run
```

以安装环境运行子进程；若调度器使用其他Python，显式设置PYTHONPATH到当前固定代码checkout并检查 `import seb; print(seb.__file__)`，避免误导入旧仓库。
preflight使用同一计费通道，费用进原开发钱包。端点“能发文本”不等于工具协议可用。
多个researcher各自独立上述预算，**不是共享$320总额**；不要自动批量启动。
运行失败先读state/trace/ledger；新output意味着新实验而不是原任务resume，不能用它重置预算。

## 合作者回传什么

代码commit；精确researcher模型/provider/harness/effort；最终配置与输入哈希；作业ID/起止时间；研究轨迹与版本/开发job ID；冻结快照和冻结原因；全候选逐题结果、空答/infra与缺测；原始usage和完整账本；原生及共同面板报表。
大文件、真实题库、轨迹、key不提交Git；私下提供artifact位置/哈希。
仓库内 `contract-lock.json` 固定本包配置与prompt哈希，`runtime-code-lock.json`对应实际Astra启动时执行代码哈希；更换researcher后的本地配置需另存哈希和差异，不能声称文件字节完全相同。
