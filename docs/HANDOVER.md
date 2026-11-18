# 交接说明：这套代码到底做了什么

> 读这一份就够把项目接过去。想改哪里、为什么这么设计、下一步往哪走，都在下面。

## 1. 一句话

一张 12 个节点的 LangGraph 状态图，把「自然语言问题」变成「可信的 SQL 结果 + 一句结论」，
中间带一条**失败归因 → 定向修复 → 重验**的自纠错回路。

## 2. 一次提问，图里发生了什么

| # | 节点 | 输入 | 做什么 | 写回状态 |
|---|---|---|---|---|
| 1 | `prepare` | 本轮问题 | 清空上一轮的中间结果，保留 history | `raw_question` + 清空累加器 |
| 2 | `rewrite` | 原问题 + 历史 | 把省略句补成完整问题（首轮跳过） | `question` `standalone_question` |
| 3 | `router` | 问题 | 判断走分析 / 闲聊 / 澄清 | `intent` |
| 2 | `schema_retrieval` | 库结构 | 读表与列，拼成 schema 文本（大库以后换检索） | `schema_context` `candidate_tables` |
| 3 | `plan` | 问题 + schema | 出 3-5 步执行计划 | `plan` |
| 4 | `sql_gen` | 问题 + schema + 计划 | 生成 SQL | `sql` |
| 5 | `execute` | sql | 只读沙箱执行 | `columns` `rows` `row_count` `exec_error` |
| 6 | `critic` | 结果 / 错误 | 归因，给出 verdict | `verdict` `critique` `attempts` |
| 7 | `repair` | 失败 SQL + 原因 | 定向修复（只在失败时进入） | `sql` `repair_attempts` |
| 8 | `visualize` | 结果 | 选图表类型 | `chart_spec` |
| 9 | `report` | 全部 | 组织结论 + 证据 | `answer` `evidence` |
| 10 | `remember` | 本轮结果 | 追加进 history，只留最近 N 轮 | `history` |

边上的三个旁路：

- `router → chat`：打招呼，直接回，不烧 token；
- `router → clarify`：问题太短（<4 字），反问而不是瞎编 SQL；
- `critic → escalate`：修复次数达到 `INSIGHT_MAX_REPAIR` 或判断为不可修复，带着定位信息交给人。

**回路**：`critic →(非 ok)→ repair → execute → critic`。这是整个项目的技术主线，
也是面试时唯一值得展开讲的东西。

## 3. 文件清单（你以后要改哪里）

| 文件 | 职责 | 什么时候动它 |
|---|---|---|
| `src/insight_agent/state.py` | **状态契约**：图里流动的所有字段 | 加新能力（比如"用户偏好"）时第一个改这里 |
| `src/insight_agent/graph/build.py` | 节点注册 + 连边 + 路由条件 | 加节点、改路由、加人工审批节点 |
| `src/insight_agent/graph/nodes.py` | 每个节点的实现 | 改节点行为；调 critic 的归因规则 |
| `src/insight_agent/llm/base.py` | `Backend` 协议 + `LLMReply` | 换厂商、加新方法（如"多候选生成"） |
| `src/insight_agent/llm/chat.py` | 千问实现（OpenAI 兼容） | 调温度、换模型、改 JSON 容错 |
| `src/insight_agent/llm/fake.py` | 确定性基线 | 做评测对照、离线开发 |
| `src/insight_agent/llm/prompts.py` | 四段提示词 | 调效果先改这里，别改代码 |
| `src/insight_agent/db/executor.py` | 只读执行器（安全边界） | 换数据库、调超时/行数 |
| `src/insight_agent/db/demo_db.py` | 演示库生成（4 表 4000 单） | 加表、改数据分布 |
| `src/insight_agent/runtime.py` | 装配：配置 + 库 + 执行器 + 模型 | 接新数据源时改这里 |
| `src/insight_agent/config.py` | 环境变量配置 | 加开关 |
| `src/insight_agent/checkpoint.py` | SQLite 会话持久化 | 换存储、加清理策略 |
| `src/insight_agent/cli.py` | 命令行跑图 + 打印每步 | 加参数、改输出 |
| `tests/` | 回路、短路、安全、模型装配、多轮 | 改行为前后都要过 |

## 4. 状态里几个关键字段

| 字段 | 含义 | 为什么需要 |
|---|---|---|
| `attempts` | 每次尝试的 sql/错误/行数/判定 | 复盘"它试过什么"，也是评测数据来源 |
| `repair_attempts` | 已修复次数（累加器） | 重试预算，防止无限循环烧钱 |
| `verdict` | 六选一的归因结论 | 不同结论走不同出口 |
| `usage` | 各节点的 token 累加 | 算单次提问成本 |
| `trace` | 节点级事件流 | 前端做进度条 / LangSmith 之外的自建观测 |
| `evidence` | SQL + 表 + 样本行 | 让结论可回溯，业务方敢用 |

## 5. 两种运行模式

| 模式 | 触发条件 | 用途 |
|---|---|---|
| `fake` | `INSIGHT_FAKE_LLM=1`，或没配 `INSIGHT_API_KEY` | 离线开发、跑测试、当评测基线，0 成本 |
| `chat` | 配了 Key 且 `INSIGHT_FAKE_LLM=0` | 真实问答 |

两者实现同一套 `Backend` 接口，**切的只是 `runtime.py` 里的一行装配**，图不变。

## 6. 接千问：三步

**第一步，拿 Key**：阿里云百炼控制台 → API-KEY 管理，新用户有免费额度。

**第二步，写 `.env`**（复制 `.env.example` 后改这一行）：

```
INSIGHT_API_KEY=sk-你的key
```

其余默认值就是千问：
`qwen-plus` 写 SQL，`qwen-turbo` 做规划和校验，端点 `dashscope.aliyuncs.com/compatible-mode/v1`。

**第三步，跑**：

```bash
.venv\Scripts\python.exe -m insight_agent.cli "2024 年华东区销售额是多少？"
```

预期：`tokens={"input_tokens": ..., "output_tokens": ...}` 不再是 `{}`，
并且 `sql_gen` 那次调用用的是 `qwen-plus`。

## 7. 目前还没做的

- **多库与 schema 检索**：现在 4 张表全量塞 prompt，换成 40 张表就爆了。
- **同义词 / 实体别名**：库里叫"客户 001"，用户说"华为"，只能如实说没有。
- **Docker + CI**：评测门禁（`--fail-under`）能跑，还没接进流水线。
- **审批策略可配置**：现在是两条固定规则，将来应该能按表、按成本阈值配。

## 7.1 口径记忆（Store）

数据助手最烦的事：同一个指标每家口径不同。默认口径写在 prompt 里能覆盖 80%，
剩下 20% 必须记住。实现分四块：

| 组件 | 干什么 |
|---|---|
| `memory.SemanticStore` | 读写门面，namespace `("semantics","metric")`，key 用指标名 |
| `Runtime.semantics` | 装配时建一次（`data/store.db`），全局共享 |
| `recall_semantics` 节点 | 每轮读一次，拼成文本块塞进 `semantics_context` |
| `learn` 节点 | 识别"以后…""我们公司…"，抽取口径并写入 |

三个设计决定：

1. **Store 和 Checkpointer 分开存**：checkpointer 按 thread 存对话，store 存跨会话知识。
   换个会话问同样的问题，口径不该丢。
2. **用户约定优先**：prompt 里写"客单价 = 销售额 / 订单数"，Store 里若是"客户数"，
   模板里明确写了"与上面冲突时以这里为准"——否则模型会两边打架。
3. **抽不出就不写**：模型漏了 `definition` 就退回确定性解析，两边都失败就回答
   "没听出要记住什么"。错的口径比没有口径危险得多。

实测（真模型，同一 thread）：

```
Q: 以后我们公司客单价按客户数算
   → 已记住：客单价 = 销售额 / 客户数
Q: 2024 年华东的客单价是多少？
   → SQL: ... / COUNT(DISTINCT o.customer_id) ...   （之前是 COUNT(DISTINCT o.id)）
```

## 7.2 人工审批（interrupt）

| 环节 | 实现 |
|---|---|
| 判定 | `graph/policy.py`：无 `WHERE`、无时间过滤 → 需要审批 |
| 暂停 | `approval` 节点调用 `interrupt({...})`，图挂起 |
| 推送 | SSE 推 `approval` 事件（复用同一条流） |
| 恢复 | `POST /api/approve` → `Command(resume={"action": ...})` |
| 三种决定 | 批准执行 / 改 SQL 后执行 / 拒绝（一行 SQL 都不执行） |

四个容易踩的坑：

1. **interrupt 之前的代码会重跑**：恢复时节点从头执行，所以 `interrupt()` 前面不能有副作用。
2. **`__interrupt__` 会混进 stream**：`stream_mode="updates"` 会吐一个内部 key，
   要过滤掉，否则前端多一行空白节点。
3. **待审批时不能接新问题**：同一 thread 再调 `/api/ask` 会 409，否则会和新输入打架。
   前端会提示"先批准或拒绝它"。
4. **不要默认全拦**：规则只覆盖真正危险的查询。什么都拦，人很快就学会闭眼点同意，
   审批就退化成了装饰。

## 8. 多轮会话与持久化

三个节点加一个存储，职责分得很开：

| 组件 | 干什么 | 为什么单独拆 |
|---|---|---|
| `prepare` | 每轮开始清空上一轮的 attempts / usage / trace | 它们是累加器，不清就会串轮 |
| `rewrite` | 有历史时把"那华北呢"补成完整问题 | 省略句是追问的常态，不补全下游全错 |
| `remember` | 把这一问一答追加进 history | 只保留最近 N 轮，防止上下文线性膨胀 |
| `SqliteSaver` | 按 thread_id 存整个状态 | 换进程还能接着聊，这是"多轮"成立的前提 |

四个容易踩的坑：

1. **初始输入只带本轮问题**：`initial_state(question)` 不再塞 history，
   history 由 checkpointer 按 thread 恢复。同一 thread 连续调用天然就是会话。
2. **累加器和恢复会打架**：`attempts` 用 `operator.add`，恢复后不清空会越积越多；
   `prepare` 用 `langgraph.types.Overwrite` 强制清零。
3. **取值的顺序有讲究**：`prepare` 必须先读 `question` 再读 `raw_question`——
   上一轮的 `raw_question` 还留在 checkpoint 里，先读它会把新问题顶掉。
4. **改写失败要能退化**：模型没按 JSON 回、或回了空串，直接退回原问题。
   宁可问得笨，不能因为改写挂了就答非所问。

实测（qwen3.8-27b，同一 thread 两轮）：

```
Q: 2024 年华东区销售额是多少？   → 理解为：同一句            → 2,219,926.11
Q: 那华北呢？                   → 理解为：2024 年华北区销售额是多少？ → 2,349,781.10
```

换一个进程读同一个 thread，历史轮数仍然是 2——这就是 SQLite 相对 MemorySaver 的意义。

## 9. 评测怎么用、怎么扩

```bash
python -m insight_agent.eval build                 # 生成 eval/cases.jsonl（100 条）
python -m insight_agent.eval run --limit 5         # 先跑几条看效果
python -m insight_agent.eval run --fake            # 确定性基线，做对照数字
python -m insight_agent.eval run --fail-under 0.7  # CI 门禁
```

- **加用例**：改 `eval/cases.py` 的 `build_cases()`，模板生成比手写可靠；
  `build` 会先把每条标准 SQL 跑一遍，跑不通直接报错，不会污染评测集。
- **看结果**：`eval/runs/<时间戳>/results.jsonl` 是逐条明细（含预测 SQL、标准 SQL、尝试次数），
  `summary.json` 是汇总。哪条错了、错在哪一步，直接看 JSONL。
- **EX 的口径**：结果集等价，不是字符串相等。行序无关、列序可换、数值两位小数归一。
  这个口径要和面试官讲清楚，否则"准确率"三个字站不住。

### EX 口径的三个边界（面试官一定会追问）

1. **行序 / 列序不同**：判等价。模型经常把"地区, 金额"写成"金额, 地区"。
2. **宽表 vs 长表**：一行两个数 与 两行 (年份, 数值) 判等价。
   "2023 和 2024 分别是多少"这种问题，两种形状都是合理答案。
3. **只给维度不给数值**：判错。问"哪个品类最高"只回"食品"、不回金额，
   业务方还得再问一次——这不是形状问题，是信息缺失，所以不放宽。

## 10. 评测迭代记录

同一批用例反复跑，每轮只改一个变量，才有可比性。这也是面试时最能展开的部分。

| 轮次 | 改了什么 | 样本 | EX | 暴露的问题 |
|---|---|---|---|---|
| v1 | 初始提示词（口径是第 4 条规则） | 5 条 easy | 60% | 模型漏掉 `status='paid'`，订单数 360 vs 309 |
| v2 | 业务口径前置成独立块，critic 也核对口径 | 5 条 easy | 80% | 订单数修好了；客单价按"除以客户数"算 |
| v3 | 指标定义写进 prompt（客单价=销售额/订单数） | 10 条 easy | 100% | easy 已饱和，需要用 medium/hard 拉开差距 |
| v4 | 排序题要求"维度+数值"；EX 容忍宽/长表与 SQLite 类型亲和 | 10 条 hard | 80% → 回放修正后 100% | 剩余 2 条是"宽表 vs 长表""整数 2023 vs 文本 '2023'"，属判定口径，不是算错 |
| 基线 | 确定性实现（不做 LLM 调用） | 100 条 | 1.5% | 只能命中固定问题，证明"模型确实在干活" |

分层看：medium 档 10/10，hard 档修口径后 10/10。**失分几乎全部来自"口径/形状"，不是 SQL 能力**——
这正是这类项目真正的工程难点，也是简历上最值得写的一句。

两个观察，写简历时可以直接用：

1. **准确率的提升来自"口径显式化"，不是换更大的模型**——v1→v3 模型没变，变量只有 prompt。
2. **easy 档会饱和**，真实差距在分组聚合和多期对比上；所以评测集必须分层，否则数字好看但没有信息量。

## 11. Web 服务与界面

| 端点 | 干什么 |
|---|---|
| `GET /` | 单页界面（纯 HTML/CSS/JS，没有构建步骤） |
| `GET /api/health` | 模型、后端类型、可见的表、审批开关、口径条数 |
| `POST /api/ask` | SSE 流：`node` 进度 → `final` 结果；暂停时是 `approval`；异常走 `error` |
| `POST /api/approve` | 用 `Command(resume=...)` 继续跑暂停的图 |
| `GET /api/threads/{id}` | 会话历史 + `pending`（有待审批的查询时非空） |
| `DELETE /api/threads/{id}` | 清空会话 |
| `GET/POST/DELETE /api/semantics` | 口径约定的查看、新增、删除 |

三个实现要点：

1. **POST + SSE，而不是 EventSource**：EventSource 只能 GET，问题得拼进 URL；
   前端用 `fetch` + `ReadableStream` 自己解析 `data: {...}\n\n`，没有长度和编码问题。
2. **同步生成器交给线程池**：图是同步的（模型调用也是同步的），
   `StreamingResponse` 收到同步迭代器会自动放到线程池执行，不阻塞事件循环。
3. **流式 delta 里是节点原始返回值**：`prepare` 的 `trace` 是 `Overwrite` 对象，
   不能当下标用——踩过一次，`tests/test_api.py` 里有对应断言。

## 12. 查不到数据怎么处理

用户问"华为在华北的销售额"，库里根本没有华为。SQL 能跑通，只是结果为 NULL。
这一版之前的表现是：判 `empty_result` → 修复两次 → 两次修复都没改动 SQL →
最后告诉用户"自动修复没有收敛"。三个问题叠在一起。

现在分三种情况处理：

| 情况 | 判定 | 回答 |
|---|---|---|
| 问题里的实体库里没有 | `no_data`（`db/grounding.py` 反查不到） | "未查询到数据：数据库中没有与「华为」相关的记录" |
| 实体都在，但筛选条件过严 | `empty_result`，修复后仍为空 | "未查询到符合条件的数据，建议放宽条件" |
| 真的是 0（例如某维度订单数为 0） | `ok` | 正常回答 0 |

实现要点：

1. **空结果的判断不需要模型**：有没有行、指标是不是 NULL 是客观事实，
   这一层直接跳过 LLM 调用（省一次 ~15s 的请求）。
2. **"整行都是 NULL"不够**：SQL 经常把过滤字面量一起 SELECT 出来，
   典型结果是 `['华为', '华北', None]`——前两列是回显，真正的指标是 None。
   所以判据是"这一行有没有有意义的数值"，而不是"整行是否为 NULL"。
3. **探测是廉价的**：把 SQL 里的字符串字面量拿出来，对每张表的文本列做
   `LIKE '%值%' LIMIT 1`，最多 3 个字面量。4 张表、几十毫秒。
4. **修不动就别修**：`repair` 返回的 SQL 和原来一样时置 `repair_noop`，
   `after_critic` 直接升级，不再空转一轮。

实测对比（同一问题，qwen3.8-27b）：

| | 之前 | 现在 |
|---|---|---|
| 耗时 | 218s | 114s |
| 尝试次数 | 3 次（2 次无效修复） | 1 次 |
| 回复 | "自动修复没有收敛…" | "未查询到数据：数据库中没有与「华为」相关的记录" |

## 13. 常见改法

- **换模型**：只改 `.env` 的 `INSIGHT_MODEL_NAME` / `INSIGHT_MODEL_BASE_URL` / `INSIGHT_API_KEY`。
- **换数据库**：改 `INSIGHT_DB_PATH`，执行器是通用的只读 SQLite；换 Postgres 就重写 `db/executor.py`。
- **加一个节点**：`state.py` 加字段 → `nodes.py` 加函数 → `build.py` 注册并连边。
- **容忍更多或更少重试**：`INSIGHT_MAX_REPAIR`。
- **关掉自纠错看效果差多少**：把 `max_repair` 设成 0，就是"一次成型"的对照组。
