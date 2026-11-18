# InsightAgent

基于 LangGraph 的多智能体数据分析助手：**生成错了能自己发现并修好**。

不是 Text-to-SQL Demo —— 核心是那条自纠错回路：执行失败 → 归因分类 → 定向修复 → 重验，
并在重试预算耗尽时把定位信息交给人，而不是无限烧钱。

## 当前进度

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 骨架 | 状态定义、图结构、只读执行器、演示库、自纠错回路（确定性实现） | ✅ 跑通 |
| M1 接模型 | 千问（OpenAI 兼容）：plan / sql_gen / repair / critic + token 记账 + 无 Key 自动回退 | ✅ 代码就绪，待 Key 实测 |
| M2 数据与评测 | 自建评测集 100 条、EX 指标、回归门禁、消融 | ✅ 跑通 |
| M3 记忆与人审 | Checkpointer 多轮（SQLite）、追问改写、Store 口径记忆、interrupt 人工审批 | ✅ 全部完成 |
| M4 交付 | FastAPI + SSE + 内置 Web 界面 | ✅ 已通；Docker / LangSmith 未做 |

> 交接文档见 [docs/HANDOVER.md](docs/HANDOVER.md)：一次提问在图里怎么走、每个文件该在哪改。

## 图结构

```
START → prepare → rewrite → router
router ─ chat / clarify ───────────────────────────────────────→ END
router ─ remember ─→ learn ────────────────────────────────────→ remember → END
router ─ analyze ─→ schema_retrieval → recall_semantics → plan → sql_gen → approval
approval ─ 批准/改完 → execute → critic
approval ─ 拒绝 ────────────────────────────────────────────────→ escalate
critic ─ ok        ─→ visualize → report ─→ remember ─→ END
critic ─ 可修复    ─→ repair → execute（回到 critic）
critic ─ 超预算 / 无数据 ─→ escalate 或 report ─→ remember ─→ END
```

六个关键设计：

1. **critic 做归因而非判定**：`syntax_error / empty_result / magnitude_anomaly / semantic_mismatch`
   各自对应不同的修复策略，这是"自纠错"能收敛的原因。
2. **重试预算在图里**：`max_repair` 决定什么时候停机，防止 agent 无限循环。
3. **安全边界在代码里**：只读连接 + 单语句 + 关键字黑名单 + 超时 + 行数上限，不依赖 prompt。
4. **多轮靠 checkpointer**：`prepare` 清上一轮中间态、`rewrite` 补全省略、`remember` 记账，
   历史存在 SQLite 里，换进程能接着聊。
5. **查不到 ≠ 0**：空结果先反查问题里的实体在库里是否存在（`db/grounding.py`），
   不存在就判 `no_data`、直接回答"未查询到"，不进退修复循环；修不动（SQL 没变化）也立刻停，
   避免空转烧钱。
6. **该停下来就停下来**：高风险查询（全表扫描 / 不限时间）在 `approval` 节点 `interrupt()`，
   等人批准或改 SQL 才执行；口径约定用 Store 记住，下一问自动生效。

## 口径记忆（Store）

```bash
python -m insight_agent.cli "以后我们公司客单价按客户数算"   # 教它一个口径
python -m insight_agent.cli "2024 年华东的客单价是多少？"     # 下一问自动按新口径算
```

- 存在 LangGraph Store 里（`data/store.db`），**跨会话**生效，进程重启不丢；
- `recall_semantics` 每轮读一次 → 拼进 SQL 生成和校验的提示词；
- 用户约定优先于 prompt 里的默认定义，冲突时以 Store 为准；
- 抽不出明确口径时**不写**，宁可回一句"没听出要记住什么"，错的口径比没有更危险。

## 人工审批（interrupt）

```bash
python -m insight_agent.cli "统计一下总订单数"   # 无 WHERE → 暂停，终端里问 y/n/e
python -m insight_agent.cli --no-approval        # 关掉审批
```

两条确定性规则（`graph/policy.py`）：没有 `WHERE`（全表扫描）、没有时间过滤（跨全历史）。
两条都不命中就直接跑——大多数正常提问不该被打断，否则人很快就学会闭眼点同意。

审批也走 SSE：图暂停时推 `approval` 事件，前端弹卡片；`POST /api/approve` 用
`Command(resume=...)` 继续跑同一张图。拒绝则一行 SQL 都不执行。

## 多轮对话

```bash
python -m insight_agent.cli "2024 年华东区销售额是多少？"     # 第一轮
python -m insight_agent.cli "那华北呢？"                     # 第二轮：会被改写成完整问题
python -m insight_agent.cli --reset                          # 清空会话
```

改写在 `rewrite` 节点完成：没有历史时直接跳过（零成本），有历史时用快模型把
「那华北呢」补成「2024 年华北区销售额是多少」。改写失败会退回原问题，
宁可问得笨一点，也不能因为改写失败而答非所问。

## 界面（FastAPI + SSE）

```powershell
python -m insight_agent.api              # 默认 http://127.0.0.1:8010
```

页面里问一个问题，回答上方是**执行流水线**：每个节点实时亮一下
（判断意图 → 读取表结构 → 生成 SQL → 执行 → 校验 → …），下面是结果卡——
指标/柱状图、可展开的 SQL、每一次尝试的判定、token 消耗。

SSE 走 POST + `ReadableStream`，事件只有三种：`node`（节点进度）、`final`（最终结果）、
`error`。会话历史存在 SQLite 里，刷新页面还能看到之前的对话。

## 快速开始

```bash
uv venv .venv --python D:\python\python.exe
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"

copy .env.example .env
.venv\Scripts\python.exe -m insight_agent.cli "2024 年华东区销售额是多少？"
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

> 在受限沙箱里跑：uv 的缓存目录要指到仓库内（`$env:UV_CACHE_DIR`），
> pytest 加 `-p no:cacheprovider`（它的缓存目录在部分沙箱下不可写）。

M0 阶段不需要 API Key：`llm/fake.py` 用确定性实现跑通全链路（并且故意写错一次列名，
用来触发自纠错回路）。

## 评测

```bash
python -m insight_agent.eval build                 # 生成 100 条，并先把标准答案跑一遍
python -m insight_agent.eval run --limit 5         # 试水
python -m insight_agent.eval run --resume          # 全量，中断可续跑
python -m insight_agent.eval run --fake            # 确定性基线，做对照
python -m insight_agent.eval run --no-plan         # 少一次调用，提速
python -m insight_agent.eval run --fail-under 0.7  # 低于阈值退出码 1，可进 CI
```

口径是**执行准确率（EX）**：预测 SQL 与标准 SQL 在同一库上跑出的结果集是否等价。
比较时忽略行序、允许列顺序不同、数值按两位小数归一（见 `eval/metrics.py`）。
每条用例记录 attempts / tokens / 耗时，明细落在 `eval/runs/<时间戳>/`。

### 当前结果（同一批用例，只改一个变量）

| 轮次 | 改动 | 样本 | EX |
|---|---|---|---|
| v1 | 初始提示词 | 5 条 easy | 60% |
| v2 | 业务口径前置 | 5 条 easy | 80% |
| v3 | 指标定义显式化 | 10 条 easy | 100% |
| v4 | 排序题要"维度+数值"、EX 容忍宽/长表与类型亲和 | 10 条 hard | 80%→100%（回放修正） |
| 基线 | 确定性实现，不调模型 | 100 条 | 1.5% |

分层：medium 10/10，hard（修口径后）10/10。模型全程没换，提升来自"把口径写清楚"。

## 目录

```
src/insight_agent/
  state.py          # 图的状态契约（最重要的文件）
  runtime.py        # 配置 / 数据库 / 执行器装配
  config.py         # 环境变量配置
  graph/nodes.py    # 节点：每个只做一件事
  graph/build.py    # 建图与路由
  db/               # 演示库 + 只读执行器
  llm/              # 模型实现（fake / 待接 LLM）
  cli.py            # 命令行跑图
tests/              # 回路、短路、安全边界
```

## 设计决策（面试会被问的）

- **为什么用图而不是 while 循环**：需要持久化状态（断点续跑）、按会话恢复（多轮追问）、
  在环中间插入人工审批（interrupt）、把 schema 检索做成可复用子图。
- **为什么 fake 和 LLM 共用一套协议**：能在没有 Key 的情况下开发和写评测，
  也方便做消融（确定性基线 vs 模型）。
- **为什么先做执行器安全**：agent 能执行 SQL 就等于拿到了数据库写权限，
  这是这类项目最容易出事的地方。
