# AgentDoctor

面向 **Python / LangGraph 自研 agent** 的诊断与修复工具：从运行轨迹发现问题，关联源码，生成受限补丁，在项目副本中运行固定验收检查，并输出可审查的报告。

这是一个能运行的第一版，借鉴 IBM AgentFixer 的检测和归因思路，独立实现了补丁生成与验证流程。它不是 IBM 官方实现，也没有复刻论文的全部 15 个检测工具。

当前版本为 **0.5.5**：可按冻结配置重放失败的可见用例，将 `case_id`、输入、实际／预期结果、候选版本和新轨迹关联起来；重试明确要求基于原始快照提交完整替换补丁，保留上一稿已改善的用例。重放仅提供诊断证据，不替代原有验收；隐藏验收输入继续隔离。接入方式见[可见失败重放与重试基准](docs/validation-evidence.md#可见失败重放与重试基准055)。

最近一次真实模型测量仍是 **0.5.4**：固定 8 项补测中，4 项条件重试通过，公开组件严格根因诊断 **4/4**、独立修复 **1/2**。0.5.5 的公开 `for/while` 回放使用归档诊断与预设补丁验证流程，不算新增模型成绩。`diagnose`、`repair`、`benchmark` 均支持 Codex CLI；详见[评测说明](benchmarks/README.md)。

历史 0.5.3 在合成新八例、旧十例上的独立修复为 **4/4、5/5**，但随后公开补测暴露了上下文阻断：长反馈 **41,147／24,000**、公开故障修复 **0/2**。0.5.4 将长反馈请求降至 **21,880／24,000** 并完成修复；公开循环案例仍在首稿漏修 `while…else` 后失败，后续模型返回空补丁，因此不能宣称公开项目修复满分。旧结果不回写；见[新版审计](benchmarks/codex-supplemental-v0.5.4-2026-10-13-audit.json)及[旧版补测](benchmarks/codex-supplemental-v0.5.3-2026-10-13-audit.json)。

## 立即运行

当前工作目录已经创建 `.venv` 并安装运行依赖。Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe -m agentdoctor demo
```

演示不需要 API 密钥。它会：

1. 创建一份带故障的客户查询 agent。
2. 实际调用 agent，记录错误参数、异常和失败的业务断言。
3. 重现原版本失败，并确认原有回归检查通过。
4. 根据配置中明确声明的 `user_id → customer_id` 兼容关系，修改唯一匹配的字典键。
5. 对候选运行两轮目标检查与回归检查。
6. 保留原始项目，输出补丁、修复后的项目副本、JSON 和 HTML 报告。

控制台最后打印报告路径。`artifacts/demo/last-run.json` 记录最近一次演示的位置，打开其中工作目录下的 `report.html` 即可查看。每次运行创建新目录，不覆盖历史结果。

在另一台机器安装：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m agentdoctor demo
```

Python 版本要求为 3.11 或以上。Linux/macOS 将解释器路径替换为 `.venv/bin/python`。

## 已实现的能力

| 能力 | 当前实现 |
| --- | --- |
| 输入/输出契约 | JSON Schema Draft 2020-12；禁用外部 schema 引用 |
| 工具调用检测 | 参数校验、未知工具、运行异常 |
| 状态与进展 | 明确配置的不变量、同一运行内的重复调用告警 |
| 任务结果 | 已记录的业务断言失败、缺少验收证据的完成声明 |
| 源码检查 | 对允许范围内的 Python 文件进行 AST 语法检查 |
| 语义分析 | 可选模型检查提示词矛盾、计划与行动不一致，并提出根因假设 |
| 根因定位 | 规则归类、模型假设、源码映射、相同 task_id 的成功/失败轨迹对照 |
| 补丁生成 | 狭义的显式字段别名修复；模型生成其他代码/提示词文本补丁 |
| 迭代修复 | 失败候选的测试结果反馈给模型；下一轮基于原快照重新提案 |
| 验证 | 基线失败复现、既有功能回归、重复执行、文件完整性检查 |
| 可观察性 | 每阶段保存 state.json、模型调用数量与返回的 token 用量 |
| 接入方式 | CLI、Python 函数、同步/异步 JSONL 轨迹记录器 |

默认 `--provider none` 完全不调用模型。此模式可以检测多类问题，但只会自动修复满足严格前提的字段别名错误。通用代码或提示词修复需要配置模型。

没有实现生产热修复、自动合并、在线服务发布、任意框架日志自动解析、跨任务长期修复记忆、完整 AgentFixer 复现、模型成本计价，以及从 checkpoint 自动续跑。`state.json` 目前用于检查过程和审计，重新运行会创建一个新任务。

## 接入自己的项目

建议先选一个可在本地测试环境复现的失败任务。

```powershell
.\.venv\Scripts\python.exe -m agentdoctor init C:\projects\my-agent
```

该命令只创建 `doctor.json` 模板。已存在的配置不会被覆盖。配置示例：

```json
{
  "schema_version": 1,
  "project_name": "my-agent",
  "source_paths": ["src/**", "prompts/**"],
  "tools": {
    "lookup_customer": {
      "input_schema": {
        "type": "object",
        "properties": {"customer_id": {"type": "string"}},
        "required": ["customer_id"],
        "additionalProperties": false
      }
    }
  },
  "checks": {
    "loop_threshold": 3,
    "preserve_state_keys": ["customer_id", "user_constraints"]
  },
  "validation": {
    "commands": [
      {
        "name": "failed_case",
        "kind": "target",
        "argv": ["{python}", "acceptance.py", "target"],
        "expected_cases": ["lookup.valid_customer", "lookup.unknown_customer"],
        "timeout_seconds": 60
      },
      {
        "name": "known_good_cases",
        "kind": "regression",
        "argv": ["{python}", "acceptance.py", "regression"],
        "expected_cases": ["lookup.by_email", "lookup.no_storage_mutation"],
        "timeout_seconds": 60
      }
    ],
    "repetitions": 2,
    "docker_image": "my-agent-test:local"
  },
  "policy": {
    "protected_paths": ["tests/**", "acceptance.py", "doctor.json", "src/authorization.py"],
    "max_candidates": 3,
    "max_changed_files": 3,
    "max_changed_lines": 120,
    "max_model_calls": 8,
    "max_context_chars": 48000
  }
}
```

`source_paths` 是补丁可修改范围，使用项目相对路径和正斜杠。保护路径优先于可修改范围。验证脚本、测试目录、配置、凭据文件不能通过补丁修改。额外的自定义验收文件或权限实现应加入 `protected_paths`。

第一版补丁只支持对现有文件进行精确文本替换，不创建、删除或重命名文件。修改片段必须唯一匹配当前快照，Python 修改还必须通过 AST 解析。

`aliases` 是可选的人工确认契约，例如 `"aliases": {"user_id": "customer_id"}`。只有确定旧字段与新字段语义相同才配置它。系统不会根据名称相似自行认定等价。

`preserve_state_keys` 只用于真正不允许变化的顶层状态字段。不要把会正常更新的任务状态放进去。部分更新式框架需要记录合并后的 `state_after`，不能把节点返回的部分更新直接当完整状态。

### 轨迹格式

支持 JSONL（每行一个 span）或 JSON 数组。必须包含 `run_id`、`span_id`、`node`、`kind`；`kind` 为 `node`、`tool`、`llm` 或 `task`。

```json
{
  "run_id": "run-001",
  "span_id": "tool-2",
  "parent_id": "task-1",
  "node": "customer_lookup",
  "kind": "tool",
  "tool": "lookup_customer",
  "input": {"user_id": "C001"},
  "error": "TypeError: unexpected keyword argument 'user_id'",
  "source": {"path": "src/agent.py", "function": "lookup_customer", "line": 27},
  "metadata": {"task_id": "lookup-C001", "code_revision": "your-commit-id"}
}
```

可选字段包括 `output`、`prompt`、`input_schema`、`output_schema`、`state_before`、`state_after`、`timestamp`、`duration_ms` 和 `assertions`。字段拼写严格检查；额外信息放入 `metadata`。输入输出必须能序列化成 JSON。

任务结果使用显式断言：

```json
{
  "run_id": "run-001",
  "span_id": "task-1",
  "node": "customer_agent",
  "kind": "task",
  "task_status": "failed",
  "assertions": [
    {"name": "correct_customer", "passed": false, "expected": "C001", "actual": null}
  ]
}
```

轨迹中的断言属于诊断证据；它们不会取代独立执行的验收测试。传入相同任务的成功轨迹时，可以使用 `--reference successful.jsonl`。对照只匹配显式 `metadata.task_id`，顺序差异只是线索，不被当成因果证明。

当前轨迹与成功参考轨迹必须使用不同的 `run_id/span_id` 组合。语义请求显式提供可引用的 `evidence_id` 和目录，输出 Schema 将引用限制到本次实际发送的 ID；本地仍会再次校验。问题发现必须引用当前轨迹；根因假设可以同时引用成功参考，但不能只引用成功样本。补丁请求包含这些假设的状态、关联告警、首次轨迹差异及对应参考 span。上下文保留全部告警、假设、对照、轮询观察及其完整证据和关联告警；40 条采样上限只限制非必要轨迹，必要证据可以超过此数量。裁剪无关轨迹、源码和历史时报告遗漏数量；必要引用缺失或核心上下文超出字符预算时明确失败，原始诊断仍保存在报告中。

允许读取的源码优先提供完整文件，按上下文 JSON 的字符预算裁剪（不含系统指令和输出 Schema），不再固定截取前 12,000 字符。预算不足时，按告警、当前证据中的 `source.function`／`source.line` 保留完整 Python 函数（含装饰器）；同名函数通过限定名称或行号定位，无法消歧时保留所有匹配。只有行号且不位于可解析函数内时提供附近行；仅有文件名或定位失效时保留整文件。相关文件不受 12 个文件采样限制，也不会被当作无关源码丢弃；必要源码仍超预算时明确失败。每个 `sources` 条目携带 `start_line`、`end_line`、`total_lines`、`truncated` 和原文件哈希，保留原始换行及缩进并经过脱敏。同一文件可能提供多个不连续片段，不能跨省略区拼接补丁。

静态依赖追踪从已定位代码出发，纳入模块级常量、辅助函数、普通／相对导入、别名和再导出；相关父 span 帮助补充调用方及外层控制流程。仅读取允许范围内的源码，不导入或执行目标模块，不读取受保护验收文件。每次最多检查 32 个文件、纳入 256 个声明，循环引用去重。`dependency_context` 记录解析到的引用和未解决项；动态分派、运行时对象、外部库、通配导入、不可解析代码、范围或数量限制均不等于实际程序出错。此机制仍不能证明运行时依赖完整，也不涵盖全部插件加载、猴子补丁、类实例和闭包绑定行为。

`model-requests/` 保存实际请求的脱敏内容，`model-events.json` 记录请求文件哈希和上下文字符数；benchmark 同时保存实现源码及其清单，并检查运行期间实现是否变化。

每条 finding 的 `observed_source` 表示**错误被观察到的位置**，兼容字段 `source` 保持这个含义。`root_causes` 单独记录**根因假设的位置**、解释、轨迹引用和源码摘录。例如工具在 `transport.py` 接收到错误类型，原因可能是 `packing.py` 把整数转成了字符串；归因关联原有 `tool_argument_schema` 告警，不需为同一个错误新增 `other`。

模型通过 `attributions[].finding_id` 补充已有告警的根因，或在新 finding 中指定 `observed_at` 和可为空的 `root_cause`。观察位置必须来自该 finding 引用的当前 span；根因必须引用被归因的观察。在线引用格式为 `source={source_id, text}`：从本次提供的源码片段复制 ID 和连续完整行，由程序在该片段内唯一定位，模型不再计算行号。片段 ID 绑定文件路径、哈希和范围；未知 ID、未发送的源码、重复文本无法唯一定位、跨省略区拼接或无效引用均导致整份响应被拒绝，保留原有诊断。

引用只容忍 LF／CRLF／CR 的换行表示差异，以及最后一个行结束符的有无；缩进、空格、空白行和代码内容仍须一致。报告中的 `source_evidence` 保存实际提供的源码文本、规范行号和哈希，`citation` 保存提交的原引用及规范化记录。旧 `path/start_line/end_line/text` 格式仍可读取，但行号错位仍拒绝，不根据相似文本自动改正。归并和补丁上下文保留根因证据；根因状态始终为 `hypothesized`，引用有效不代表因果已被证明。

模型请求使用独立的诊断表示：其中的 `root_causes[].source_evidence={source_id, start_line, end_line}` 指向本次 `sources` 列表里的连续源码，多个归因共享同一份源码文本；轨迹证据继续通过 ID 指向 `spans`／`reference_spans`。完整原引用和规范化审计保留在报告，不重复塞入每条请求归因。源码裁剪后重新解析引用，核对路径、哈希、行范围和摘录，不能沿用已被移除的旧片段 ID。必要源码／轨迹缺失或确实无法满足字符预算时仍明确失败；不会为压缩大小丢掉根因解释和必要证据。

`task_assertion`、`execution_error` 等保留为症状，报告的 `classification_level` 区分症状与故障类别。给症状补充原因并不自动完成分类：在线 `attributions` 需显式提供 `classification={category, contract, violation}` 或 `null`。有支持证据时生成关联的故障 finding，通过 `symptom_ids`／`classified_by` 双向关联，并保存分类所依据的契约和违反行为；原症状和历史类别不被重写。已属具体故障类别的告警只补充根因，不能借归因接口改名。没有分类的症状列在 `coverage.unclassified_symptoms`。

优先使用已有具体故障类别。`other` 表示这些类别以外、有明确业务／断言／Schema 契约的独立缺陷，不把所有异常或断言失败自动转为 `other`。新 finding 和症状归因使用同一分类校验：`other` 必须有被报告观察自身的失败断言，或规则记录的 Schema 违例；直接 finding 绑定 `observed_at`（旧格式取首个引用），归因绑定原症状的观察证据。泛化异常、通过的契约、其他步骤的失败契约均不足以支持此分类。计划偏差和有界轮询校验也由两个入口共用；证据不足时保留为假设并记录原因。契约解释和因果关系仍需要独立验证，本地校验不能证明模型解释正确。

`plan_action_mismatch` 用于有独立证据的计划与动作偏离，需要在**同一个 span** 的 `metadata.plan` 和 `metadata.action` 中记录明确计划和实际动作，并且二者不同。不同步骤之间不能交叉比较；当前不推断分散在不同 span 中的计划／动作关联。仅从“本来应该成功”推断的意图，或对参数错误、提示词冲突、状态丢失、终止错误的泛化复述，不作为第二个故障类别；证据未满足要求的模型输出保留为待验证假设，并记录调整原因。同类别、同源位置且证据重叠的规则/模型告警归并时保留来源、原始描述和证据，不合并不同位置或独立事件。

重复调用依旧是待核实的循环线索，同一次运行中按节点和工具检测，覆盖不同 `parent_id`。降为 `bounded_polling` 观察需要所有相关调用明确返回待完成状态，并沿父链接找到同一个完整根调用；各层正常返回且未记录失败断言。每条调用边分别核对：直接包装调用应执行一次，简单 `while counter < budget` / `for ... in range(budget)` 应耗尽其预算，实际子调用数必须与之相等。支持同步／异步包装、静态导入别名、参数预算和跨文件整数常量；多层预算分别核对，不只比较工具调用总数。报告的 `details.call_chain` 保存逐层证据、调用源码行、预期和实际次数。

内层轮询有界不能证明外层重复调用有界。完成后继续调用、无界或重置计数器、条件计数、部分／额外调用、错挂父链接、缺失祖先、异常或未知状态均不据此降级。简单 `if result['done']`／`['completed']` 停止分支只有在每次子调用都明确返回 `false` 时才纳入判断。本规则是静态源码与已记录执行的有限一致性检查，不是通用终止证明；动态分派、复杂并发调度、多个不相连的根调用和未支持的控制流仍保守保留告警。

可直接使用记录器：

```python
from agentdoctor.traces import TraceRecorder

recorder = TraceRecorder("runs/trace.jsonl", run_id="run-001")
observed_tool = recorder.wrap(
    "customer_lookup",
    lookup_customer,
    kind="tool",
    tool="lookup_customer",
    source={"path": "src/tools.py", "function": "lookup_customer"},
)
result = observed_tool(customer_id="C001")
```

同步与 `async` 函数均支持。每次逻辑任务使用独立 recorder/run_id。记录器按函数签名绑定参数，使用 `**kwargs` 的函数会保留其绑定结构；需要扁平工具参数时可自行调用 `recorder.record(...)`。使用 LangGraph 时可包装 `builder.add_node()` 的节点；见 [接入示例](examples/langgraph_adapter.py)。不需要用 LangGraph 重写已有 agent。

记录器先创建 span，再捕获和绑定参数。未知关键字、缺参、多余位置参数或重复赋值都会留下轨迹，实际函数的异常类型与消息会保留。`metadata.call` 记录原始 `args`/`kwargs`，敏感命名的位置参数同样脱敏；绑定失败时，纯关键字调用的 `input` 保持实际关键字对象，混合调用则为 `args`/`kwargs` 包装对象。`metadata.input_encoding` 标明表示方式。自带 demo 也通过这个公开记录器采集，不再使用单独的错误记录逻辑。

记录器不采集模型隐藏推理，只保存显式输入、输出、工具调用和状态。自定义对象、消息类、日期等请在接入层转换成 JSON。记录失败会抛异常，由接入方决定遥测失败时的策略。

### 先诊断

```powershell
.\.venv\Scripts\python.exe -m agentdoctor diagnose C:\projects\my-agent `
  --traces C:\projects\my-agent\runs\failed.jsonl
```

诊断只读源码和轨迹，不执行目标项目。未发现问题时返回 `inconclusive`，不会声称整个 agent 正确。

### 配置模型

已登录的 Codex CLI 可直接用于正式诊断和修复，无需单独配置 API 密钥：

```powershell
codex.cmd login status
.\.venv\Scripts\python.exe -m agentdoctor diagnose C:\projects\my-agent `
  --traces C:\projects\my-agent\runs\failed.jsonl --provider codex
.\.venv\Scripts\python.exe -m agentdoctor repair C:\projects\my-agent `
  --traces C:\projects\my-agent\runs\failed.jsonl --provider codex --executor docker
```

三个入口共用登录预检与客户端初始化，均可用 `--model` 指定模型；省略时记录为 `codex-default`，不代表固定模型修订。Codex 不接受 `--base-url`，该参数仅用于下面的 HTTP 服务适配器。正式修复默认 Docker；对可信项目可显式使用 `--executor local`。模型子进程仍只接收脱敏后的上下文并禁用工具，补丁应用和验收由 AgentDoctor 控制。若有参考轨迹，可在上述命令加 `--reference successful.jsonl`。

本地 Ollama，需要事先启动服务并准备自己选择的模型：

```powershell
$env:OLLAMA_MODEL = "你的本地模型名称"
.\.venv\Scripts\python.exe -m agentdoctor diagnose C:\projects\my-agent `
  --traces C:\projects\my-agent\runs\failed.jsonl --provider ollama
```

OpenAI 使用 Responses API 和严格 JSON Schema 输出，模型名称由你指定：

```powershell
$env:OPENAI_API_KEY = "你的密钥"
$env:OPENAI_MODEL = "你的可用模型名称"
.\.venv\Scripts\python.exe -m agentdoctor diagnose C:\projects\my-agent `
  --traces C:\projects\my-agent\runs\failed.jsonl --provider openai
```

也可以通过 `--model` 显式指定名称。默认地址分别为 `http://localhost:11434` 和 `https://api.openai.com/v1`。`--base-url` 用于自定义兼容服务，OpenAI 适配器需要支持 **Responses API**，不是仅支持 Chat Completions 的服务。

选择外部模型后，相关源码、工具契约、轨迹片段和诊断信息会发送到该服务。代码做常见字段及密钥模式脱敏，但不能保证识别所有个人信息或硬编码凭据；敏感项目应使用合规的数据处理方式或本地模型。报告和项目副本保存在本地，副本与 `.patch` 本身仍可能包含业务数据。

模型输出在本地再次校验。拒绝、截断、无效 JSON、未知证据引用不会作为成功分析使用。规则检查结果会保留，报告标明模型检查未完成。调用次数、上下文字符数、候选数量均有限制；token 用量按接口实际返回值记录，不估算费用。

### 验证修复

默认使用 Docker，需要本机 Docker 可用，并**预先构建/拉取**包含目标项目依赖的测试镜像：

```powershell
.\.venv\Scripts\python.exe -m agentdoctor repair C:\projects\my-agent `
  --traces C:\projects\my-agent\runs\failed.jsonl `
  --provider ollama --executor docker
```

容器禁用网络，项目挂载为只读，禁用额外 capabilities，使用非 root 用户，限制进程数、内存和 CPU；只有 `/tmp` 可写。镜像不会自动拉取。测试应把临时文件写到临时目录，项目依赖需要安装在镜像内，不能依赖复制 `.venv`。涉及业务服务的测试使用模拟工具或测试数据，不连接生产系统。

仅对你信任的项目，可以显式选择本地执行：

```powershell
.\.venv\Scripts\python.exe -m agentdoctor repair C:\projects\my-agent `
  --traces C:\projects\my-agent\runs\failed.jsonl `
  --provider ollama --executor local
```

本地模式使用独立文件副本和过滤后的环境变量，但**不是操作系统安全沙箱**，目标代码仍有当前账户的主机权限。它使用运行 AgentDoctor 的 Python 解释器，因此该解释器需要目标项目依赖。自带 demo 是可信本地代码，演示会明确选用这个模式。

验证命令以参数数组执行，`{python}` 替换为对应解释器，不经过 shell。至少需要一个 `target` 命令和一个 `regression` 命令：

- 原版本的目标失败必须在每次重复运行中稳定重现。
- 原版本的回归检查必须全部通过。
- 候选版本的所有目标和回归检查必须在每次重复运行中通过。
- 每条命令提供本次执行标识、非空的实际用例集合、执行数量和逐项结果，与冻结的 `expected_cases` 完全对应。
- 超时、环境错误、结果缺失、零用例、跳过用例、退出码与逐项结果不一致、快照文件被改写，都不能通过验证。

**从 0.2 升级**：旧配置仍可用于诊断；修复要求每条验证命令配置非空、不重复的 `expected_cases`，并改造验收脚本输出完成证据。缺少配置会返回 `inconclusive`；缺少或无效结果的候选会被拒绝。直接运行普通 `python -m unittest` / pytest 并只依赖退出码不再足够，需要验收适配器输出逐项结果。协议及完整示例见 [验收完成证据](docs/validation-evidence.md)。

完整性检查在运行前以及每条命令结束后重新扫描文件集合，检查**新增、删除、修改**；发现变化会停止后续命令。原项目在分析期间新增文件也会使已验证交付变为 `inconclusive`。扫描采用与快照一致的排除规则（虚拟环境、凭据、缓存等），并明确排除本轮报告输出目录；不能把不应改变的源文件放在排除范围内。目录无法读取或出现符号链接时按检查失败处理。

验收保护除了直接脚本路径，也会静态识别 `python -m evaluator`、包模块、`unittest` 的模块/类/方法选择器和 pytest 文件节点。普通根目录及 `src/` 布局均支持；目标包子树和祖先 `__init__.py` 会被冻结。解析不导入项目模块。动态 import、自定义 `sys.path` 和通过包装器间接选择的验收依赖仍需显式配置 `protected_paths`。

每条命令在新进程中运行，但同一套检查共享项目副本。测试应自行重置数据库、文件和外部状态。默认两轮是小规模确定性验收，不是模型成功率的统计证明。涉及随机模型输出时，应使用更大任务集和独立统计评估。

## 输出与状态

默认输出到被测项目的 `.agentdoctor/run-<id>/`，也可以使用 `--output` 指定目录。

```text
run-<id>/
  report.html             可直接打开的中文报告
  report.json             完整机器可读结果
  diagnosis.json          异常、证据、根因假设
  verification.json       基线、候选和验证结论
  state.json              最近阶段与中间结果
  manifest.json           原始快照文件 SHA-256
  config.frozen.json      本轮固定的策略和测试配置
  traces.redacted.json    脱敏后的输入轨迹
  reference-traces.redacted.json  脱敏后的成功参考轨迹
  model-events.json      结构化模型响应、用量及语义证据验收状态
  snapshot/               原始项目快照
  baseline/               原版本执行目录
  candidate-1/            候选项目副本
  candidate-1.patch       对应修改差异
```

| 状态 | 含义 | CLI 退出码 |
| --- | --- | --- |
| `diagnosed` | 已记录问题与假设，没有执行修复验证 | 0 |
| `verified` | 候选通过了配置的验证范围 | 0 |
| `rejected` | 已尝试的候选均未通过 | 2 |
| `inconclusive` | 证据不足、未复现、无合适补丁或环境不满足 | 2 |
| 输入/配置错误 | 无法启动本轮任务 | 1 |

`verified` 不表示所有告警都已解决，也不保证所有未来任务正确。报告保留原始诊断，只有与补丁关联的根因假设标记为 `supported_by_configured_checks`。系统不会把补丁自动写回原项目、合并到 Git 或发布到生产。

```powershell
.\.venv\Scripts\python.exe -m agentdoctor show <运行目录>
```

## Python 接口与结构

```python
from agentdoctor.engine import run

report = run(
    project="path/to/agent",
    traces="path/to/failed.jsonl",
    mode="diagnose",
)
```

主控制器采用明确的状态流程，模型负责语义判断和补丁提案，执行、权限和通过条件由程序控制。`model=ModelClient(...)` 启用模型；`mode="repair"` 和 `executor="docker"` 启用验证修复。

| 文件 | 职责 |
| --- | --- |
| `engine.py` | 诊断、迭代修复和状态保存 |
| `analysis.py` | 有预算限制的模型上下文、证据校验与告警归并 |
| `context_view.py` | 将报告归因投影为共享源码引用，保持报告审计与请求表示分离 |
| `source_context.py` | 按定位提取完整函数与连续源码片段，保留原始行号和换行 |
| `citations.py` / `attribution.py` | 校验源码片段引用，记录规范摘录与根因假设 |
| `taxonomy.py` | 共用症状和故障类别定义、分类证据约束 |
| `dependencies.py` | 有界静态依赖追踪，记录解析结果与未解决引用 |
| `polling.py` | 有界待完成轮询的保守识别 |
| `contracts.py` / `traces.py` | 输入契约、JSONL 加载与记录器 |
| `detectors.py` | 规则检测、归因线索、轨迹对照 |
| `model.py` | Ollama / OpenAI 结构化模型接口 |
| `codex_cli.py` | Codex CLI 结构化提案适配器与登录预检 |
| `patches.py` | 精确文本补丁、路径及预算限制 |
| `project.py` | 快照、文件范围和完整性检查 |
| `runner.py` | 本地 / Docker 执行和验收规则 |
| `report.py` / `cli.py` | 报告和命令行入口 |

当前快照限制为 2000 个文件、每文件 2 MB、合计 25 MB；轨迹限制为 20 MB、10000 个 span。大型项目应提供精简复现工程。符号链接和 Windows junction 不跟随；必要资源应作为普通文件提供。

## 开发与测试

公开的 [配置 Schema](schemas/doctor.schema.json) 与 [轨迹 Schema](schemas/trace.schema.json) 从 `agentdoctor/contracts.py` 生成。修改定义后重新生成；`--check` 仅校验，文件缺失或不同步时返回非零退出码，适用于 CI：

```powershell
.\.venv\Scripts\python.exe -m agentdoctor.export_schemas
.\.venv\Scripts\python.exe -m agentdoctor.export_schemas --check
```

回归测试包含 Schema 完整定义一致性、自带配置校验与 `expected_cases` 非空/唯一约束检查。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

运行独立案例集，生成逐案例 JSON/CSV 及汇总指标：

```powershell
.\.venv\Scripts\python.exe -m agentdoctor benchmark
```

案例包含参数名/类型、状态合并、循环终止、提示词冲突，以及正常样本和有意轮询样本。默认使用无模型规则模式与可信本地案例。标签和额外验收样本不进入修复项目；每个候选在修复后接受独立检查。模型模式可通过 `--provider ollama --model <模型名>` 开启，默认采用 Docker；也可以显式指定可信本地执行。不要把此小型合成集的结果当成生产环境准确率。

`benchmark` 也支持 `--provider codex`，复用本机 Codex CLI 登录。使用固定案例集执行本地评测：

```powershell
codex.cmd login
.\.venv\Scripts\python.exe -m agentdoctor benchmark --provider codex --executor local --dataset-lock benchmarks/independent-v2.lock.json --output artifacts/model-benchmarks
```

新增统计分别记录“没有生成补丁”“策略拒绝”“可见测试失败”“独立验收失败”，并单列修复成功及调用/环境问题；按故障案例和按候选两种口径输出，规则补丁与模型补丁分开。固定清单、默认模型含义和统计规则详见 [评测说明](benchmarks/README.md)。CLI 未登录时在调度前终止，不会把单元测试的模拟输出当成真实模型成绩。

测试覆盖实际故障修复、错误补丁后再次修复、冻结测试保护、路径越界、旧快照、伪造证据、模型拒绝/截断、两个 HTTP 适配器的本地模拟服务、异步轨迹、超时和凭据环境隔离。模型服务测试使用本地模拟响应，不会产生外部 API 费用。

2026-10-13 的 0.4.0 重测保持案例与评分器不变：严格诊断匹配 **10/10**，无效证据引用 **0**；独立验收后的修复成功 **5/5**（4 个模型补丁、1 个规则补丁），四类修复失败均为 0，回归失败 **0/5**。正式 Codex 诊断与修复入口也已通过真实调用验证，135 项回归测试通过。详见 [0.4.0 评测归档](benchmarks/codex-independent-v2-v0.4.0-2026-10-13.json) 和 [新旧统计说明](benchmarks/README.md)；[旧版结果](benchmarks/codex-independent-v2-2026-10-13.json) 保留。该结果仅适用于已知合成回归集的本次运行；本次使用可信本地执行器，Docker 集成仍需在对应环境验证。

0.4.1 补充了原案例集未覆盖的跨父调用循环、超过 40 条的必要证据、跨步骤计划／动作误配等回归，共 **144 项测试通过**。核心证据超预算时，正式诊断入口返回 `inconclusive` 并保留原始诊断。此次未重跑真实模型评测；上述 10/10 是 0.4.0 的历史结果，不覆盖这三类新发现的问题。

0.4.2 新增 11 项定位源码回归，全量 **155 项测试通过**。14,882 字符文件中第 961 行的故障函数已纳入上下文；正式修复流程通过测试模型验证了整文件和函数片段两种供应方式。此次未重跑真实模型评测，详见[源码上下文验证记录](benchmarks/README.md#042定位源码进入模型上下文)。

0.5.0 已完成 **26 次真实 Codex CLI 调用**：旧十例严格诊断 **10/10**、独立验收修复 **5/5**（4 个模型补丁、1 个规则补丁）；新 `workflow-v1` 八例严格诊断 **6/8**、修复 **4/4**（全部模型补丁）。两套四类修复失败均为 0，模型／环境未完成项为 0。新集仍暴露跨文件根因的类别／结构化定位错误，以及正常嵌套轮询的规则误报，修复成功不等于诊断准确。全套 **166 项测试通过**。完整请求、响应、补丁与验收证据见[首次实测记录](benchmarks/README.md#2026-10-13-首次真实模型结果)；这些仍是合成小样本，不代表生产泛化成功率。

0.5.1 又运行一轮 **26 次真实 Codex CLI 调用**。将未通过语义校验的案例也计入计划分母：新八例严格根因诊断 **6/8（75%）**，旧十例 **6/10（60%）**；只看故障案例分别为 **2/4、1/5**。六例诊断被源码摘录校验拒绝，其中五例只缺少末尾换行，另一例源码行范围不符。两套有效诊断子集的根因匹配虽均为 6/6，但不代表整体满分。独立修复分别为 **4/4、5/5**，候选来源与上轮相同，四类修复失败均为 0。实现、标签和历史结果未在测量期间修改，失败也没有通过重跑替换；见[完整实测与失败记录](benchmarks/README.md)和[审计汇总](benchmarks/codex-v0.5.1-2026-10-13-audit.json)。

0.5.2 调整引用协议和症状分类规则，先使用上述 18 份原始诊断响应进行[离线回放](benchmarks/diagnosis-v0.5.2-offline-replay-2026-10-13.json)：五份仅缺末尾换行的引用恢复有效，错行响应仍被拒绝，原先有效的响应均未回归。`async_fanout_fault` 的旧响应仍只有症状归因，没有显式故障类别，因此不会自动变为正确分类。该回放没有调用外部模型，不代表新提示词的准确率，历史实测分数保持原样。

随后固定 0.5.2 实现、案例和标签，各运行一轮，完成 **25 次真实 Codex CLI 调用**：18 份诊断均通过引用校验，新八例严格根因诊断 **8/8**、旧十例 **10/10**；故障案例单独为 **4/4、5/5**。独立修复分别为 **3/4、5/5**，共 7 个模型补丁和 1 个规则补丁。新集 `dependency_contract_fault` 根因正确，但补丁上下文 **27,092 > 24,000**，未发起补丁请求，因此保留在计划修复分母中；不能只报告可测子集的 3/3。四类正常提案／候选失败均为 0，另有这 1 例上下文阻断。详见[实测审计](benchmarks/codex-v0.5.2-2026-10-13-audit.json)和[完整口径](benchmarks/README.md)。这是固定合成集的单轮结果，不代表生产项目准确率。

0.5.3 完成 **26 次真实 Codex CLI 调用**：18 份诊断全部通过校验，严格根因诊断仍为 **8/8、10/10**，独立修复恢复为 **4/4、5/5**（共 8 个模型补丁、1 个规则补丁）。两套报告均为 `complete=true`，四类修复失败及上下文／模型／环境阻断均为 0。`dependency_contract_fault` 本轮补丁上下文为 **19,780／24,000 字符**，5 条根因记录共享 1 个源码片段引用，补丁生成及独立验收均完成；本轮模型响应与此前不同，不能将全部字符差额归因于压缩。完整请求、响应和验收证据见[新集归档](benchmarks/codex-workflow-v1-v0.5.3-2026-10-13.json)、[旧集归档](benchmarks/codex-independent-v2-v0.5.3-2026-10-13.json)和[审计汇总](benchmarks/codex-v0.5.3-2026-10-13-audit.json)。

运行独立锁定的新工作流集：

```powershell
.\.venv\Scripts\python.exe -m agentdoctor benchmark --suite workflow-v1 --provider codex --executor local --dataset-lock benchmarks/workflow-v1.lock.json --output artifacts/workflow-model
```

## 资料来源

- [IBM Research：AgentFixer 官方介绍](https://research.ibm.com/publications/agentfixer-from-failure-detection-to-fix-recommendations-in-agentic-systems)：检测与根因分析的研究背景。
- [AgentFixer 论文](https://arxiv.org/html/2603.29848v1)：借鉴规则/模型组合检测、轨迹归因与成功/失败对照；工程实现和测试门槛为本项目设计。
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)：Responses API 的 `text.format` JSON Schema 输出、拒绝及截断处理。
- [Ollama Chat API](https://docs.ollama.com/api/chat)：非流式 `/api/chat` 和 schema 格式输出。
- [jsonschema 验证接口](https://python-jsonschema.readthedocs.io/en/stable/validate/)：Draft 2020-12 契约校验。
