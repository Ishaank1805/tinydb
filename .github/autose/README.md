# AutoSE harness

Agent code, prompts and configuration for `.github/workflows/autose*.yml`.

| Path | Purpose |
|---|---|
| `autose.config.yml` | repository guard, target commands, path policy, loop limits, budgets, models per role, experiment variants |
| `prompts/` | system prompts for each role (`_security.md` is prepended to all) |
| `skills/spec-writer/` | the A1 spec-writing skill used by stage 1 (replace the fallback with your own) |
| `autose/orchestrator.py` | stage state machine of a build/rework job |
| `autose/stages/` | stages 1-5 with their checks |
| `autose/review.py` | stage 6: prepare, review (no token), post |
| `autose/gate.py`, `autose/report.py`, `autose/doctor.py` | first job, last job, setup checks |
| `autose/agent.py`, `autose/llm.py` | agent loop; Anthropic and OpenAI-compatible clients |
| `autose/tools.py`, `autose/toolspecs.py`, `autose/sandbox.py`, `autose/untrusted.py`, `autose/secretstore.py` | tools, path policy, command sandbox, untrusted input, credentials |
| `tests/` | unit tests and an end-to-end run against tinydb with a scripted model |

Run the tests locally (Python 3.10+; the end-to-end test clones tinydb, or set `AUTOSE_E2E_REPO` to a local clone):

```bash
python -m venv .venv && .venv/bin/pip install -r .github/autose/requirements.txt
.venv/bin/python -m pytest -q .github/autose/tests
```

Dry run against a local checkout without GitHub (needs a model key in the environment, commits locally, never pushes):

```bash
PYTHONPATH=.github/autose AUTOSE_TARGET_VENV=<venv with pytest> \
  python -m autose local --repo <checkout> --issue-file issue.json   # issue.json: {"number": 1, "title": "...", "body": "..."}
```
