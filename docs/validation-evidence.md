# 验收完成证据（协议 1）

进程退出码只描述进程结束状态。候选在导入或调用时执行 `raise SystemExit(0)`，可能让验收器在检查前退出。因此，0.3.0 要求冻结的验收脚本主动报告真实执行的用例，父进程核对完成证据后才能通过验收。

## 固定预期用例

每条 `doctor.json` 验证命令增加 `expected_cases`：

```json
{
  "name": "task_contract",
  "kind": "target",
  "argv": ["{python}", "-m", "evaluator", "target"],
  "expected_cases": ["target:1", "target:2"],
  "timeout_seconds": 15
}
```

清单必须非空且 ID 唯一，由维护验收的人定义并随配置冻结。不能根据候选实际发现的测试反向缩减清单。测试脚本及辅助模块需要处于保护范围。

## 输出协议

每次命令执行时，运行器生成新的 `AGENTDOCTOR_VALIDATION_ID` 环境变量。本地和 Docker 执行均传入。验收器在实际检查完成后，向 stdout 输出恰好一行以 `AGENTDOCTOR_RESULT ` 开头的 JSON；普通日志可输出在其他行。

```json
{
  "schema_version": 1,
  "invocation_id": "本次环境变量 AGENTDOCTOR_VALIDATION_ID 的值",
  "completed": true,
  "executed_count": 2,
  "results": [
    {"id": "target:1", "status": "passed"},
    {"id": "target:2", "status": "failed", "detail": "实际结果不符合契约"}
  ],
  "suite_error": null
}
```

上面是便于阅读的展开格式；实际输出必须是 `print("AGENTDOCTOR_RESULT " + json.dumps(report))` 的单行形式。

- `completed` 表示验收流程走到结尾，不表示全部通过。
- `executed_count` 是实际执行的用例数；`results` 中每项 ID 必须唯一，并与 `expected_cases` 完全匹配。
- `status` 为 `passed`、`failed`、`error` 或 `skipped`。失败和异常可作为原版本故障证据；跳过不能满足必需覆盖范围。
- 0.5.5 起，逐项结果还可提供结构化 `input`、`actual`、`expected`，均为 JSON 值。旧协议保持兼容；重试也能读取 `detail` 中明确的 `{"actual": ..., "expected": ...}` JSON，不从异常文本猜测输入或预期值。
- 全部 `passed` 时退出 0，有 `failed` / `error` 时退出非 0；退出码与逐项结果矛盾时，整条证据无效。
- 导入失败或提前终止时报告 `completed=false`，并填写 `suite_error`；不能把未执行的测试补写为通过。
- 缺少报告、零用例、遗漏/额外/重复 ID、错误数量、重复报告、旧执行标识及截断输出均不能通过。stdout 捕获上限为 64 KiB，逐项详情需保持简短。

## 可运行参考

自带 [evaluator.py](../agentdoctor/benchmark_data/cases/argument_alias/evaluator.py) 展示普通函数检查：[doctor.json](../agentdoctor/benchmark_data/cases/argument_alias/doctor.json) 固定两条目标用例与两条回归用例。它先保存执行标识，再在异常保护范围内导入候选；每次实际调用后追加结果，捕获 `SystemExit` 等 `BaseException` 为失败，最后输出报告。

采用 unittest 的项目可参考 [checks.py](../agentdoctor/demo_project/checks.py)：逐个执行固定测试并记录结果。pytest 等框架需要类似适配，从框架真实完成事件读取 ID 和状态，不能仅解析 `exitcode` 或预填成功结果。

`os._exit(0)` 无法被 Python 异常捕获，但会导致结果缺失，因此仍不能通过。进程超时、无法启动及文件完整性变化另行阻止验收。

## 原版本、候选和独立评测

原版本也必须提供完整结果；没有执行完检查不能被当成“稳定复现故障”。候选必须运行与原版本相同的命令、重复次数及用例清单，且逐项通过。JSON/HTML 报告保存 `expected_cases`、`invocation_id`、`completion` 和 `completion_error`，便于复核。

独立评测的预期 ID 由外部固定样本清单生成，不从候选输出推导。独立验收器不导入项目的 `evaluator.py`，也必须提供相同完成协议。候选提前退出导致缺失结果，计入独立验收失败；若主流程曾错误交付为 `verified`，计入错误验证率，不以“环境不完整”从分母中排除。

## 可见失败重放与重试基准（0.5.5）

在需要重放的**可见验收命令**上增加 `replay`，原有 `argv`、`expected_cases`、重复次数、超时和通过条件保持不变：

```json
{
  "name": "task_contract",
  "kind": "target",
  "argv": ["{python}", "-m", "evaluator", "target"],
  "expected_cases": ["target:1", "target:2"],
  "timeout_seconds": 15,
  "replay": {
    "module": "subject",
    "cases_file": "visible.json",
    "case_group": "target"
  }
}
```

对应的 `visible.json`：

```json
{
  "target": [
    {"id": "target:1", "function": "run", "kwargs": {"kind": "first", "value": 3}, "expected": 13},
    {"id": "target:2", "function": "run", "kwargs": {"kind": "second", "value": 3}, "expected": 23}
  ]
}
```

这些必须是现有可见验收实际使用的输入和预期结果，不能另外创建更容易通过的检查。`cases_file` 自动加入保护范围；不指定 `case_group` 时文件应直接为用例数组。全部用例有 `id` 时按 ID 关联；旧数组全部无 ID 时按冻结 `expected_cases` 的顺序关联。混合 ID、重复 ID、数量或集合不匹配会拒绝配置。支持可选 `args` 和 `kwargs`，函数返回 awaitable 时会等待完成。

有自定义初始化、状态或断言的项目，可提供一个可见重放适配函数，重现相同的调用前置条件并返回可比较的 JSON 结果。重放采用 JSON 值相等作诊断对照，不替代原验收器的自定义断言。自定义适配器应加入 `protected_paths`；实际待修复函数继续在允许修改的范围内。隐藏验收输入应放在修复项目之外，不能声明到 `replay`；程序只读取这里明确绑定的可见用例，不从独立评测清单或历史独立结果补充输入。

候选可见验收失败、即将请求下一稿时，控制器对每个失败的 `(command, case_id)` 重放一次，合并关联原验收的重复执行编号。每次重放使用新的候选副本，并沿用本地／Docker 后端及该命令的超时；Docker 继续使用只读挂载、禁网和资源限制。项目中允许修改的 Python 函数会采集调用片段和父子关系，记录候选源码哈希；可选 `trace_functions` 限定函数名称，`max_spans` 默认 64、最大 1,000。异步函数的挂起／恢复可产生多个 profile 片段；它们不冒充完整的节点生命周期。

请求中的 `retry_feedback` 提供候选版本、失败用例、完整用例输入、原验收实际／预期结果及重放证据；`case_evidence_id` 指向该次重放的用例 span。`reproduces_validation=false` 表示这次结果未复现原失败，`null` 表示缺少可比较的实际值。没有绑定时明确 `input_available=false` 和 `replay.status=unavailable`。超时、提前退出、完成记录缺失／不匹配、输出截断、文件变化均标为不完整，不提供可信重放轨迹，也不会把候选改判通过。

完整记录在 `retry-evidence-N/feedback.json`、各用例的 `audit.json`、`traces.json` 和 `candidate-manifest.json` 中，模型上下文审计也保存关联数据。持久化请求、轨迹和审计按既有规则脱敏。采集达到上限会标记 `trace.status=partial` 与省略数量。字符预算紧张时，附带的栈帧参数预览可以明确省略；完整用例输入和结果、所有已采集调用片段、父子关系、源码位置及版本仍保留，预览原文留在审计。用例 span 的 `input_ref` 是请求内的 JSON 指针，复用相同输入。必要证据仍放不下时，继续拒绝发起请求，不扩大预算。

`patch_base.snapshot_id` 固定标识**原始快照**。新模型响应必须返回对应的 `base_snapshot_id`；错误版本会被拒绝。每稿都应用于新的原始副本，`before` 必须匹配原始源码（或同一提案中更早的修改），不能以失败候选为基准只提交增量。完整的上一稿提案仍在历史中；`preserve_observed_improvements` 列出相对基线已改善的可见用例，要求下一稿保留这些有效修改或实现等效修复。旧存档／规则补丁未声明版本时保持原始快照语义。

例如，上一稿只修好 `for`，下一稿必须包含 `for` 和 `while` 的完整修改。如果只提交 `while`，`for` 会回到原始状态；冻结验收会拒绝该候选。程序不会自动合并两稿，也不会强制保留可能需要改写的旧代码片段。候选轨迹行号属于候选版本，请求中用于构造补丁的 `sources` 始终属于原始快照，二者不会混用。

## 信任范围

该协议检查可信验收器的覆盖范围、完成状态和一致性，执行标识用于防止误用旧结果，不是签名。验收器与候选同进程运行时，不能据此抵御候选主动篡改 Python 运行时或伪造整份报告。需要对抗这类恶意代码时，应把判定和结果签发放在候选无法修改或访问的独立进程/服务中；本地文件副本本身不是安全沙箱。
