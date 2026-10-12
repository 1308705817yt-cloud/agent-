from __future__ import annotations

import html
import json
from pathlib import Path

from .core import write_json


def write_report(directory: Path, report: dict) -> None:
    write_json(directory / "report.json", report)
    esc = lambda value: html.escape(str(value))
    pretty = lambda value: esc(json.dumps(value, ensure_ascii=False, indent=2))
    findings = report.get("diagnosis", {}).get("findings", [])
    labels = {"verified": "验证通过", "diagnosed": "诊断完成", "rejected": "候选未通过", "inconclusive": "证据或验证不足"}
    rows = "".join(f'<tr><td><span class="severity {esc(f["severity"])}">{esc(f["severity"])}</span></td><td><strong>{esc(f["category"])}</strong><br>{esc(f["summary"])}</td><td>{esc(", ".join(f["evidence"]))}<br><small>{esc(f.get("source", {}))}</small></td></tr>' for f in findings)
    attempts = ""
    for attempt in report.get("attempts", []):
        attempts += f'<details open><summary>候选 {attempt["number"]} · {esc(attempt["status"])} · {esc(attempt["summary"])}</summary><p>{esc(attempt.get("reason", ""))}</p><pre>{pretty(attempt.get("proposal", {}))}</pre><details><summary>测试结果</summary><pre>{pretty(attempt.get("validation", {}))}</pre></details></details>'
    baseline_rows = report.get("baseline", {}).get("commands", []) if report.get("baseline") else []
    successful = sum(r["returncode"] == 0 and not r["timed_out"] and not r["error"] for r in baseline_rows)
    document = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>AgentDoctor · {esc(labels.get(report["status"], report["status"]))}</title>
<style>
:root{{color-scheme:light dark;--bg:#f3f5f8;--fg:#172231;--card:#fff;--muted:#536479;--border:#dce2ea;--accent:#12615b}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.65 system-ui,"Microsoft YaHei",sans-serif}}main{{max-width:1120px;margin:40px auto;padding:0 24px}}header{{padding:26px 0;border-bottom:2px solid var(--accent)}}.eyebrow{{font-size:13px;letter-spacing:.14em;color:var(--accent)}}h1{{font-size:32px;margin:10px 0}}h2{{font-size:20px}}p{{overflow-wrap:anywhere}}.meta,small{{color:var(--muted)}}.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:24px 0}}.card,section{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:22px}}.card b{{display:block;font-size:27px}}section{{margin:18px 0}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:12px 10px;border-bottom:1px solid var(--border);vertical-align:top;overflow-wrap:anywhere}}th{{font-size:13px;color:var(--muted)}}.severity{{font-weight:650}}.high{{color:#bf453b}}.medium{{color:#956308}}pre{{overflow:auto;background:var(--bg);padding:16px;border-radius:8px;font:13px/1.6 Consolas,monospace;max-height:520px}}summary{{cursor:pointer;font-weight:600;padding:10px 0}}footer{{padding:20px 0;color:var(--muted);font-size:13px}}@media(max-width:700px){{.cards{{grid-template-columns:1fr}}main{{padding:0 12px}}table{{font-size:12px}}}}@media(prefers-color-scheme:dark){{:root{{--bg:#111821;--fg:#e5ebf3;--card:#1b2532;--muted:#a2b2c5;--border:#304053;--accent:#66c8b2}}.high{{color:#ff9d93}}.medium{{color:#e6c277}}}}
</style></head><body><main>
<header><div class="eyebrow">AGENTDOCTOR / RUN REPORT</div><h1>{esc(labels.get(report["status"], report["status"]))}</h1><p>{esc(report["reason"])}</p><div class="meta">{esc(report["run_id"])} · {esc(report["started_at"])} · {esc(report["mode"])}</div></header>
<div class="cards"><div class="card">发现的问题<b>{len(findings)}</b></div><div class="card">补丁尝试<b>{len(report.get("attempts", []))}</b></div><div class="card">原版本检查通过<b>{successful} / {len(baseline_rows)}</b></div></div>
<section><h2>执行与验证范围</h2><p>{esc(report.get("executor_description", "仅静态诊断，没有执行被测项目。"))}</p><p>{esc(report["scope"])}</p><p>原始项目：{esc(report["project"])}</p><p>已验证补丁：{esc(report.get("verified_patch") or "无")}</p><p>模型：{esc(report["model"])}</p></section>
<section><h2>问题与证据</h2><table><thead><tr><th>级别</th><th>观察结果</th><th>证据位置</th></tr></thead><tbody>{rows or '<tr><td colspan="3">当前检查没有发现问题；这不等于完整健康认证。</td></tr>'}</tbody></table></section>
<section><h2>根因假设</h2><pre>{pretty(report.get("diagnosis", {}).get("hypotheses", []))}</pre></section>
<section><h2>修复候选</h2>{attempts or '<p>尚无可验证的补丁。</p>'}</section>
<section><details><summary>原版本测试结果</summary><pre>{pretty(report.get("baseline"))}</pre></details><details><summary>检查覆盖范围与提示</summary><pre>{pretty(report.get("diagnosis", {}).get("coverage", {}))}</pre><pre>{pretty(report.get("warnings", []))}</pre></details></section>
<footer>本报告区分异常、根因假设和测试证据。源代码未自动合并；verified 仅表示配置的检查通过。时间为带时区的 ISO 8601 时间。</footer>
</main></body></html>'''
    (directory / "report.html").write_text(document, encoding="utf-8")
