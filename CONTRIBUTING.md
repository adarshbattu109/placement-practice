# Contributing to placement-practice

Thank you for considering contributing to placement-practice! This document explains the
process and conventions we follow.

Development conventions and architecture rules live in [CLAUDE.md](CLAUDE.md) — read that first.

## Getting Started

```bash
git clone https://github.com/adarshbattu109/placement-practice.git
cd placement-practice
uv sync
cp .env.example .env.local     # then add your HF_TOKEN
```

Get a token at [hf.co/settings/tokens](https://huggingface.co/settings/tokens) with the
"Make calls to Inference Providers" permission. The free tier is enough to develop against.

## Development Workflow

1. Create a branch from `main`
2. Make your changes
3. Run the checks (see below)
4. Open a pull request

## Running Checks

```bash
# Lint and format
uv tool run ruff check .
uv tool run ruff format --check .

# Self-check (no HF_TOKEN needed — the judge is stubbed)
uv run python -m app.evaluate

# Manually: start the server and record an answer
uv run practice
```

## Test Conventions

There is no test suite. Non-trivial logic leaves **one runnable check** behind — the smallest
thing that fails if the logic breaks:

- An `assert`-based `demo()` guarded by `if __name__ == "__main__":`, as in `app/evaluate.py`
- Or one small `tests/test_<module>.py` if the module needs more than that

No frameworks, no fixtures, no per-function suites. Extend `demo()` rather than adding a
parallel suite for the same module.

When you do write a check:

- Test **observable behavior** (return values, exceptions, the shape of the report), not
  internal state
- Stub the remote calls — a check that needs `HF_TOKEN` will not run in CI
- Assert the actual number, not just "it's a number". `assert scored["beginner"] == 57`
  catches a broken weight; `assert scored["beginner"] > 0` does not

## Code Style

- **Formatter:** Ruff
- **Line length:** 100 characters
- **Target:** Python 3.14
- **Type hints:** Required on route signatures and public helpers
- **Docstrings:** One-line summary on modules and public functions
- **Comments:** Only when the "why" is non-obvious
- **`ponytail:` comments:** Mark a deliberate simplification with its ceiling and upgrade
  path, e.g. `# ponytail: flat RMS gate, no VAD`

## Pull Request Guidelines

- Keep PRs focused — one logical change per PR
- Include a check for new behavior
- Ensure all CI checks pass before requesting review
- Never commit `.env.local`, `logs/`, or recorded audio

## Changing the Grader

`LEVELS` in `app/evaluate.py` is the calibration knob for every score the app produces.
Changing a weight or a level `brief` changes every candidate's result, so:

- Weights must sum to 1 — this is asserted at import, so a bad edit fails loudly
- Update the expected scores in `demo()` in the same commit
- Say in the PR description what moved and why. "Expert felt too generous on thin answers"
  is a good reason; "rounder numbers" is not

## Architecture Decisions

For significant design changes (a new interview mode, a different judge model, anything
needing local model weights or a database), open an issue describing the proposal before
writing code. Include:

- Problem statement
- Proposed solution
- Alternatives considered
- Cost impact, if it changes how many inference calls a session makes

**Cloud-only inference is a hard constraint.** Proposals that download model weights need to
justify themselves against it.

## Commit Messages

We use [Conventional Commits](https://www.conventionalcommits.org/) to enable automated
semantic versioning. Commit messages must follow this format:

```
<type>(<optional scope>): <description>

[optional body]

[optional footer(s)]
```

### Commit Types

| Type | Version Bump | Purpose | Example |
|------|--------------|---------|---------|
| `feat:` | **MINOR** (0.1.0 → 0.2.0) | New feature | `feat: add telephonic interview mode` |
| `fix:` | **PATCH** (0.1.0 → 0.1.1) | Bug fix | `fix: reject undecodable audio with 400` |
| `perf:` | **PATCH** | Performance improvement | `perf: resample in one offline pass` |
| `refactor:` | **PATCH** | Code refactor (no behavior change) | `refactor: extract the silence gate` |
| `docs:` | No bump | Documentation only | `docs: document the level weights` |
| `style:` | No bump | Code style | `style: apply ruff format` |
| `test:` | No bump | Add or fix checks | `test: cover the empty transcript path` |
| `build:` | No bump | Build system or deps | `build: pin huggingface-hub` |
| `ci:` | No bump | CI configuration | `ci: add pip-audit to the pipeline` |
| `chore:` | No bump | Other maintenance | `chore: update ruff` |

### Breaking Changes

To trigger a **MAJOR** version bump (0.x.0 → 1.0.0), use `!` or include `BREAKING CHANGE:`
in the footer:

```
feat!: return per-parameter weights in the report

BREAKING CHANGE: /submit response shape changed — `weights` is now required
```

### Examples

```bash
# New feature (bumps 0.1.0 → 0.2.0)
git commit -m "feat: add a system design topic to the question bank"

# Bug fix (bumps 0.1.0 → 0.1.1)
git commit -m "fix: stop whisper hallucinating on silent recordings"

# Breaking change (bumps 0.x.y → 1.0.0)
git commit -m "feat!: replace the keyword scorer with an LLM judge"

# No version bump
git commit -m "docs: explain the free-tier cost per 20-question run"
```

### Scope (Optional)

Use scope to indicate which component is affected:

```
feat(evaluate): weight parameters by expertise level
fix(api): gate silent recordings before calling ASR
docs(readme): add the HF_TOKEN setup step
```

### Automated Versioning

When you open a PR targeting `main`:

1. `PR Title Check` validates the title format (blocks merge if invalid)
2. `Version Bump` determines the bump type from the PR title
3. Updates `pyproject.toml` and commits to your branch
4. On merge, `Publish Package` tags the commit and creates a GitHub Release

**Skip release:** Types that don't trigger bumps (`docs:`, `test:`, `ci:`, `chore:`, `build:`,
`style:`) will not update the version — the publish workflow will see the tag already exists
and skip.

## Releasing

Releases are **fully automated** via GitHub Actions when PRs are merged to `main`:

### How it works

1. **PR title validation** — The `PR Title Check` workflow enforces conventional commit
   format on all PR titles. PRs with invalid titles cannot be merged.

2. **Automatic version bump** — The `Version Bump` workflow runs on every PR:
   - Parses the PR title to determine bump type (`feat:` → MINOR, `fix:`/`perf:`/`refactor:`
     → PATCH, `feat!:` → MAJOR)
   - Checks for version conflicts with `main` (handles race conditions when another PR merges
     first)
   - Updates `pyproject.toml` and commits back to the PR branch as
     `chore(release): bump version to X.Y.Z`
   - Non-bumping types (`docs:`, `test:`, `ci:`, `chore:`, etc.) are skipped

3. **Tag and release on merge** — The `Publish Package` workflow runs on `main`:
   - Reads the version from `pyproject.toml`
   - Creates git tag `v{version}` if it doesn't already exist
   - Builds the package
   - Creates a GitHub Release with artifacts attached

4. **Re-run on failure** — If publishing fails, use `workflow_dispatch` (Actions tab → Run
   workflow) to retry without needing another merge.

### Version bump rules

| PR Title Prefix | Version Bump | Example |
|----------------|--------------|---------|
| `feat:` | MINOR (0.1.2 → 0.2.0) | `feat: add telephonic mode` |
| `fix:` | PATCH (0.1.2 → 0.1.3) | `fix: handle a truncated WAV upload` |
| `perf:` | PATCH | `perf: cache the inference client` |
| `refactor:` | PATCH | `refactor: extract the judge prompt` |
| `feat!:` or `fix!:` | MAJOR (0.1.2 → 1.0.0) | `feat!: change the /submit response shape` |
| `docs:`, `test:`, `ci:`, `chore:`, etc. | No bump | `docs: update examples` |

### Race condition handling

If two PRs are open and one merges first (advancing the version on `main`), the `Version Bump`
workflow on the remaining PR will:

1. Detect that its base version doesn't match `main`
2. Recalculate the new version from `main`'s current version
3. Update and commit the corrected version

This ensures no duplicate versions are ever created.

**No manual intervention required** — just merge PRs with properly formatted commit messages.
