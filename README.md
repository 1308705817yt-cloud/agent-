# AgentDoctor

面向 **Python / LangGraph 自研 agent** 的诊断与修复工具：从运行轨迹发现问题，关联源码，生成受限补丁，在项目副本中运行固定验收检查，并输出可审查的报告。

这是一个能运行的第一版，借鉴 IBM AgentFixer 的检测和归因思路，独立实现了补丁生成与验证流程。它不是 IBM 官方实现，也没有复刻论文的全部 15 个检测工具。

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
        "argv": ["{python}", "-m", "unittest", "tests.test_failed_case"],
        "timeout_seconds": 60
      },
      {
        "name": "known_good_cases",
        "kind": "regression",
        "argv": ["{python}", "-m", "unittest", "tests.test_regression"],
        "timeout_seconds": 60
      }
    ],
    "repetitions": 2,
    "docker_image": "my-agent-test:local"
  },
  "policy": {
    "protected_paths": ["tests/**", "doctor.json", "src/authorization.py"],
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

记录器不采集模型隐藏推理，只保存显式输入、输出、工具调用和状态。自定义对象、消息类、日期等请在接入层转换成 JSON。记录失败会抛异常，由接入方决定遥测失败时的策略。

### 先诊断

```powershell
.\.venv\Scripts\python.exe -m agentdoctor diagnose C:\projects\my-agent `
  --traces C:\projects\my-agent\runs\failed.jsonl
```

诊断只读源码和轨迹，不执行目标项目。未发现问题时返回 `inconclusive`，不会声称整个 agent 正确。

### 配置模型

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
- 超时、环境错误、测试集合缺失、快照文件被改写，都不能通过验证。

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
| `contracts.py` / `traces.py` | 输入契约、JSONL 加载与记录器 |
| `detectors.py` | 规则检测、归因线索、轨迹对照 |
| `model.py` | Ollama / OpenAI 结构化模型接口 |
| `patches.py` | 精确文本补丁、路径及预算限制 |
| `project.py` | 快照、文件范围和完整性检查 |
| `runner.py` | 本地 / Docker 执行和验收规则 |
| `report.py` / `cli.py` | 报告和命令行入口 |

当前快照限制为 2000 个文件、每文件 2 MB、合计 25 MB；轨迹限制为 20 MB、10000 个 span。大型项目应提供精简复现工程。符号链接和 Windows junction 不跟随；必要资源应作为普通文件提供。

## 开发与测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试覆盖实际故障修复、错误补丁后再次修复、冻结测试保护、路径越界、旧快照、伪造证据、模型拒绝/截断、两个 HTTP 适配器的本地模拟服务、异步轨迹、超时和凭据环境隔离。模型服务测试使用本地模拟响应，不会产生外部 API 费用。

本机端到端验证使用自带示例和本地执行器；没有配置真实模型密钥，也没有 Docker，因此真实模型修复质量与 Docker 集成需要在对应环境进一步验收。

## 资料来源

- [IBM Research：AgentFixer 官方介绍](https://research.ibm.com/publications/agentfixer-from-failure-detection-to-fix-recommendations-in-agentic-systems)：检测与根因分析的研究背景。
- [AgentFixer 论文](https://arxiv.org/html/2603.29848v1)：借鉴规则/模型组合检测、轨迹归因与成功/失败对照；工程实现和测试门槛为本项目设计。
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)：Responses API 的 `text.format` JSON Schema 输出、拒绝及截断处理。
- [Ollama Chat API](https://docs.ollama.com/api/chat)：非流式 `/api/chat` 和 schema 格式输出。
- [jsonschema 验证接口](https://python-jsonschema.readthedocs.io/en/stable/validate/)：Draft 2020-12 契约校验。
