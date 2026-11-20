"""Markdown for every artifact, so a run can be followed on GitHub without logs."""
from __future__ import annotations

from typing import Any


def cell(value: Any) -> str:
    return str(value if value is not None else '').replace('|', '\\|').replace('\n', ' ').strip()


def bullets(items: list | None, empty: str = '_none_') -> str:
    items = [i for i in (items or []) if str(i).strip()]
    return '\n'.join(f'- {i}' for i in items) if items else empty


def header(ctx: Any, title: str) -> str:
    run = ctx.run
    link = f' · [Actions run]({run.run_url})' if run.run_url else ''
    return (f'# {title}\n\n> AutoSE · issue #{run.issue} · round {run.round} · variant `{run.variant}`'
            f' · job `{run.job}`{link}\n')


def stopped(reason: str) -> str:
    return f'\n> ❌ **This stage could not pass its check, so the run stopped here.**\n> {cell(reason)}\n'


def spec_md(ctx: Any, spec: dict | None, check: Any, judge: dict | None, skill_fallback: bool,
            stop_reason: str = '', questions: list | None = None) -> str:
    out = [header(ctx, 'spec.md · Stage 1: Requirements')]
    if stop_reason:
        out.append(stopped(stop_reason))
    if questions:
        out.append('## Questions for the maintainer\n' + bullets(questions))
    if spec:
        out.append(f"## {spec.get('title', '')}\n\n{spec.get('summary', '')}\n")
        out.append(f"**Kind:** {spec.get('kind', '')}\n\n## Problem\n{spec.get('problem', '')}\n")
        rows = ['| ID | Given | When | Then | How a test verifies it | Judge |', '|---|---|---|---|---|---|']
        for ac in spec.get('acceptance_criteria') or []:
            verdict = ''
            if judge and ac.get('id') in judge:
                ok, reason = judge[ac['id']]
                verdict = ('✅ ' if ok else '❌ ') + reason
            rows.append('| ' + ' | '.join(cell(ac.get(k)) for k in ('id', 'given', 'when', 'then', 'verification'))
                        + f' | {cell(verdict)} |')
        out.append('## Acceptance criteria\n' + '\n'.join(rows) + '\n')
        out.append('## Assumptions\n' + bullets(spec.get('assumptions')) + '\n')
        out.append('## Open questions\n' + bullets(spec.get('open_questions')) + '\n')
        out.append('## Scope\n**In:**\n' + bullets(spec.get('scope_in')) + '\n\n**Out:**\n'
                   + bullets(spec.get('scope_out')) + '\n')
        if spec.get('reproduction'):
            out.append('## Reproduction\n```\n' + str(spec['reproduction']).strip() + '\n```\n')
    if check is not None:
        out.append('## Check: all acceptance criteria are testable\n'
                   'Structural check by the harness, then an independent LLM judge per criterion.\n\n'
                   + check.render() + '\n')
    skill = 'fallback skill (A1 skill not installed!)' if skill_fallback else 'A1 spec-writing skill'
    out.append(f'---\n_Written by the planner agent using the {skill}. Input sanitation: '
               f"{ctx.issue.get('sanitize_note', 'n/a')}._\n")
    return '\n'.join(out)


def design_md(ctx: Any, design: dict | None, check: Any, arch_used: bool, stop_reason: str = '') -> str:
    out = [header(ctx, 'design.md · Stage 2: Design')]
    if stop_reason:
        out.append(stopped(stop_reason))
    if design:
        out.append(f"## Summary\n{design.get('summary', '')}\n\n## Approach\n{design.get('approach', '')}\n")
        if design.get('changes_since_previous'):
            out.append(f"## Changes since the previous design\n{design['changes_since_previous']}\n")
        rows = ['| File | Functions / classes | New file | What changes |', '|---|---|---|---|']
        for ch in design.get('changes') or []:
            symbols = ', '.join(f"`{s.get('name')}`" + (' (new)' if s.get('new') else '')
                                for s in ch.get('symbols') or [])
            rows.append(f"| `{cell(ch.get('path'))}` | {symbols or '-'} | {'yes' if ch.get('new_file') else 'no'}"
                        f" | {cell(ch.get('description'))} |")
        out.append('## Files and functions to change\n' + '\n'.join(rows) + '\n')
        rows = ['| Planned test | Covers | Checks |', '|---|---|---|']
        for t in design.get('tests') or []:
            rows.append(f"| `{cell(t.get('name'))}` | {cell(', '.join(t.get('covers') or []))} | "
                        f"{cell(t.get('description'))} |")
        out.append(f'## Planned acceptance tests (`{ctx.test_file}`)\n' + '\n'.join(rows) + '\n')
        out.append('## Risks\n' + bullets(design.get('risks')) + '\n')
        out.append('## Alternatives considered\n' + bullets(design.get('alternatives_considered')) + '\n')
        out.append('## Architecture notes\n' + (design.get('architecture_notes') or '_none_') + '\n')
    if check is not None:
        out.append('## Check: the plan names the files and functions to change\n'
                   'Every path must exist (or be marked new) and be a file the pipeline may change; every '
                   'function must exist in that file (AST) or be marked new; every criterion needs a test.\n\n'
                   + check.render() + '\n')
    arch = 'with' if arch_used else '**without** (ARCHITECTURE.md not found!)'
    out.append(f'---\n_Written by the planner agent {arch} the A2 architecture documentation._\n')
    return '\n'.join(out)


def test_report_md(ctx: Any, ev: dict, attempt: int, stop_reason: str = '') -> str:
    out = [header(ctx, f'test_report.md · Stage 4: Testing (implementation attempt {attempt})')]
    if stop_reason:
        out.append(stopped(stop_reason))
    status = '✅ all new and existing tests pass' if ev.get('passed') else '❌ check failed'
    out.append(f"## Result: {status}\n\n**Full suite:** {ev.get('summary', 'not run')}\n")
    rows = ['| Criterion | Test | Result after the change | Result on the base code |', '|---|---|---|---|']
    f2p = ev.get('fail_to_pass') or {}
    base_cases = f2p.get('cases') or {}
    for row in ev.get('ac_rows') or []:
        if not row['tests']:
            rows.append(f"| {row['ac']} | ❌ no test | - | - |")
        for t in row['tests']:
            outcomes = ', '.join(sorted(set(t['outcomes'])))
            base = sorted({o for n, o in base_cases.items() if n.split('[')[0] == t['name']})
            if not base and f2p.get('collection_error_on_base'):
                base = ['error (does not import on base)']
            rows.append(f"| {row['ac']} | `{t['name']}` | {outcomes} | {', '.join(base) or '-'} |")
    out.append('## Acceptance tests\n' + '\n'.join(rows) + '\n')
    if f2p.get('mode') and f2p.get('mode') != 'off':
        out.append(f"**Fail-to-pass ({f2p['mode']}):** {f2p.get('failing_on_base')} new test case(s) fail "
                   'when the source files are temporarily reset to the base commit, i.e. the tests detect '
                   'the change.\n')
    missing = ev.get('missing_baseline') or []
    out.append('## Existing tests\n' + (f'❌ {len(missing)} existing test(s) are gone: ' + ', '.join(missing[:20])
                                        if missing else '✅ every test that existed on the base commit is '
                                        'still collected') + '\n')
    if ev.get('problems'):
        out.append('## Problems\n' + bullets(ev['problems']) + '\n')
    if ev.get('notes'):
        out.append('## Notes\n' + bullets(ev['notes']) + '\n')
    if ev.get('failing'):
        details = '\n\n'.join(f"**{f['key']}**\n```\n{f['message'][:1500]}\n```" for f in ev['failing'][:8])
        out.append('<details><summary>Failure details</summary>\n\n' + details + '\n\n</details>\n')
    out.append(f"---\n_Tests written by the test-author agent in `{ctx.test_file}`; suite run and judged "
               'deterministically by the harness (pytest + JUnit XML)._\n')
    return '\n'.join(out)


def pr_md(ctx: Any, spec: dict, design: dict, tests: dict, impl_summary: str, ci: dict | None) -> str:
    issue = ctx.run.issue
    lines = [f"Closes #{issue}\n",
             '> 🤖 Opened by the **AutoSE** workflow. A person must review and decide whether to merge; '
             'the workflow never merges.\n',
             f"## What this changes\n{design.get('summary', '')}\n",
             f'**Implementation notes:** {impl_summary or "-"}\n',
             '## Acceptance criteria'] + [
        f"- **{ac['id']}**: given {ac.get('given')}, when {ac.get('when')}, then {ac.get('then')}"
        for ac in spec.get('acceptance_criteria') or []]
    lines.append(f"\n## Tests\n{tests.get('summary', '')} — new tests in `{ctx.test_file}`; "
                 f"fail-to-pass: {(tests.get('fail_to_pass') or {}).get('failing_on_base')} case(s) fail on the "
                 'base code.\n')
    if ci:
        icon = '✅' if ci.get('conclusion') == 'success' else '❌'
        lines.append(f"## CI\n{icon} `{ci.get('conclusion')}` — {ci.get('url', '')}\n")
    if spec.get('assumptions'):
        lines.append('## Assumptions made by the agents\n' + bullets(spec['assumptions']) + '\n')
    lines.append(f'## Trace\n- Run: {ctx.run.run_url or "(local)"}\n- Round: {ctx.run.round} · variant '
                 f'`{ctx.run.variant}`\n- Artifacts (spec.md, design.md, diff.patch, test_report.md, pr.md) are '
                 'attached to the run; progress is also mirrored in the status comment on the issue.\n')
    return '\n'.join(lines)


def review_md(meta: dict, review: dict | None, facts: dict, error: str = '') -> str:
    out = [f"# review.md · Stage 6: Independent review (round {meta.get('round')})\n",
           f"> PR #{meta.get('pr')} at `{str(meta.get('head_sha', ''))[:12]}` · issue #{meta.get('issue')} · "
           f"reviewer model `{meta.get('model')}` · variant `{meta.get('variant')}`\n",
           '> The reviewer ran in its own job with a read-only token, saw only the issue, the diff and the '
           'repository at the PR head, and could not push.\n']
    if meta.get('implementer_context'):
        out.append("> ⚠️ Experiment variant: this reviewer was ALSO shown the implementer's design/PR text.\n")
    if error or not review:
        out.append(f'## ⚠️ The review could not be completed\n{error}\n')
        return '\n'.join(out)
    verdict = review['verdict']
    banner = '✅ APPROVE' if verdict == 'approve' else '🔁 REQUEST CHANGES'
    route = '' if verdict == 'approve' else f" → back to **{review.get('target_stage')}**"
    out.append(f'## Verdict: {banner}{route}\n\n{review.get("summary", "")}\n')
    rows = ['| Requirement (derived by the reviewer from the issue) | Status | Evidence |', '|---|---|---|']
    icons = {'met': '✅', 'not_met': '❌', 'unclear': '❔'}
    for item in review.get('issue_checklist') or []:
        rows.append(f"| {cell(item.get('requirement'))} | {icons.get(item.get('status'), '')} {item.get('status')} "
                    f"| {cell(item.get('evidence'))} |")
    out.append('## Checklist\n' + '\n'.join(rows) + '\n')
    findings = review.get('findings') or []
    if findings:
        rows = ['| Severity | Category | Where | Finding | Suggestion |', '|---|---|---|---|---|']
        for f in findings:
            where = f"`{f.get('file')}`" + (f":{f['line']}" if f.get('line') else '')
            rows.append(f"| {f.get('severity')} | {f.get('category')} | {where} | {cell(f.get('description'))} | "
                        f"{cell(f.get('suggestion'))} |")
        out.append('## Findings\n' + '\n'.join(rows) + '\n')
    else:
        out.append('## Findings\n_none_\n')
    out.append(f"## What the reviewer ran itself\n{review.get('probes', '')}\n")
    met = sum(1 for i in review.get('issue_checklist') or [] if i.get('status') == 'met')
    total = len(review.get('issue_checklist') or [])
    blockers = sum(1 for f in findings if f.get('severity') in ('blocker', 'major'))
    tests_ok = facts.get('reviewer_tests_passed')
    out.append('## Evidence recorded by the harness\n'
               '| Item | Value |\n|---|---|\n'
               f'| Requirements from the issue marked met | {met}/{total} |\n'
               f"| Test runs by the reviewer at the PR head | {facts.get('reviewer_test_runs', 0)} "
               f"({'last one passed' if tests_ok else 'none passed'}) |\n"
               f"| CI on the head commit | {facts.get('ci_conclusion', '{{CI}}')} |\n"
               f'| Blocking findings (blocker/major) | {blockers} |\n'
               f'| Verdict | {banner} |\n'
               '| Merge decision | a person (AutoSE never merges) |\n')
    return '\n'.join(out)
