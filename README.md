# AutoLearn

Run small, measurable improvement experiments against any local project:

```text
baseline -> hypothesis -> edit -> test -> score -> keep/revert -> log -> next
```

AutoLearn is the transaction and evidence layer, not another agent wrapper. An AI agent, script, or person proposes a narrowly bounded edit command. The standard-library runner snapshots the project, executes the command without a shell, measures a JSON score, runs preservation gates, checks the changed-file boundary, and either keeps the result or restores the prior bytes and modes. The same protocol works for code, model configuration, datasets, generated behavior, and documentation.

## One-command verification

```bash
sh install.sh
```

Eight tests cover initialization, no-edit planning and auditing, evidence-backed keeps, metric regressions, gate regressions, file-mode restoration, protected-path violations, and evaluator side effects.

## Start

```bash
python3 -m autolearn init --goal "AUTOLEARN: reduce parser latency without changing output"
```

Edit `autolearn.json`. The metric command must print a final JSON line such as `{"score": 12.4}`. Commands are JSON argument arrays, not shell strings. Define explicit writable paths, must-preserve paths, regression gates, direction, epsilon, time limit, snapshot budget, and an optional target.

Place one JSON hypothesis per file in `.ai_programs/autolearn/proposals/`. The generated example documents every required field. Then use the original operating modes:

```bash
python3 -m autolearn audit_only   # map state; never evaluate or apply a proposal
python3 -m autolearn plan_only    # establish/read baseline and show the next proposal
python3 -m autolearn do_it        # execute exactly one complete cycle
python3 -m autolearn run_forever  # consume proposals and wait for more until stopped
python3 -m autolearn report       # machine-readable factual counts and best score
```

Durable evidence lives under `.ai_programs/autolearn/`:

- `state.json`: baseline, best score, processed IDs, and last run
- `results.tsv`: command, exit code, before/after metrics, gates, duration, verdict, notes
- `current.md`: active or last hypothesis and keep/revert conditions
- `project_map.md`: refreshed by `audit_only`
- `runs/<run_id>/patch.diff`: reviewable text diff or binary-change inventory

If evaluation crashes, an optional bounded `repair_command` may run once; otherwise the candidate is restored and logged. A candidate is kept only when its primary metric beats the current best beyond `epsilon` (or meets the target), all gates pass, and every changed path stays inside the declared writable set.

## Concrete distinction

Agent plugins such as [Karpathy's autoresearch](https://github.com/karpathy/autoresearch) and [Evo](https://github.com/evo-hq/evo) focus on directing a particular autonomous research workflow. AutoLearn instead exposes a small agent-neutral execution contract with no package dependencies: proposal files in, deterministic metric/gate evidence out, plus rollback that also works outside Git. It does not discover a useful metric or make an edit intelligent by itself.

## Safety and limitations

- Proposal commands are trusted local programs. Argument arrays prevent accidental shell interpolation, but AutoLearn is not an OS sandbox. Run untrusted candidates in a real sandbox or disposable machine.
- Rollback covers ordinary files and symlinks inside the project, excluding `.git` and AutoLearn's own state. External services, databases, processes, and paths outside the project are not reversible.
- The snapshot byte cap is deliberate. Large model weights and datasets should be immutable inputs; put only small candidate-controlled files in the experiment project.
- AutoLearn never edits secrets intentionally. Preserve patterns are enforced after execution, not a substitute for OS permissions.

See [SUPPORT.md](SUPPORT.md) to fund further development or attach a confirmed public transaction hash to a specific work request.


## Install and run

```sh
chmod +x install.sh run.sh
./install.sh
./run.sh --help
```
