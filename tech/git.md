# Git and the ignore files

## What it is

Git is a version control system: it records the history of a folder of files as a series of **commits** (snapshots with a message). You can see who changed what and when, and go back. A **repository** is the folder plus its hidden `.git` directory holding the history. Git does not track everything: a file named in `.gitignore` is left alone.

## Why this repository uses it

Every project in this track is its own git repository with small, meaningful commits (for example `feat(domain): ...`, `test: ...`, `docs: ...`). The history shows how the project was built. The ignore files keep generated and private files out.

## Where it appears

| File | Role |
| --- | --- |
| `.gitignore` | Names files and folders Git must not track. |
| `.dockerignore` | The same idea for Docker's build context (see the Docker note). |
| `.git/` | The history (not a file you edit). |
| `requirements*.lock`, `slo/generated/*.yml` | **Are** committed on purpose (locks and generated rule files that are checked in sync). |

## How it works underneath

- Each commit stores a snapshot, a message, an author, a time and a pointer to its parent commit. A **branch** is a movable name for a commit.
- `git add` puts changes in the **staging area**; `git commit` records the staged changes.
- A file already tracked stays tracked even if added to `.gitignore` later.

## Walkthrough of this repository's use

```text
.venv/
__pycache__/
*.pyc
*.egg-info/
build/
dist/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.import_linter_cache/
.coverage
.env
.DS_Store
logs/
data/
.omc/
```

- `.venv/`, `__pycache__/`, `*.pyc`, `*.egg-info/`, `build/`, `dist/`: Python environment and build output. A trailing `/` means a folder. `*` is a wildcard.
- `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `.import_linter_cache/`, `.coverage`: caches and reports from the tools.
- `.env`: a reader's private overrides (never commit). `.env.example` is committed instead and shows the names.
- `.DS_Store`: a macOS folder-view file.
- `logs/`, `data/`: the API's request log (`logs/api`) and the files each rehearsal run writes (`data/runs`, `data/manual`), produced when you run the project.
- `.omc/`: a local tool-state folder.

The rule of this track: after the final commit, `git status` shows nothing untracked.

## Try it yourself

1. `git log --oneline` lists the commits, newest first.
2. `git status --short` prints nothing on a clean tree.
3. `touch .env && git status --short` still prints nothing, because `.env` is ignored.
4. `git check-ignore -v .venv logs data` shows which rule ignores each name.
5. `git show --stat HEAD` shows what the last commit changed.

## Mistakes people make

- Committing secrets or `.env` files.
- Committing generated caches.
- Adding a tracked file to `.gitignore` and expecting it to vanish.
- Large mixed commits that are hard to review.

## Check yourself

1. What does `.gitignore` do?
   *Answer: tells Git which files to leave untracked.*
2. Why is `.env.example` committed but `.env` is not?
   *Answer: the example documents names and safe defaults; `.env` may hold private values.*
3. Why are lock files committed?
   *Answer: they make installs repeatable.*
4. What is a commit?
   *Answer: a snapshot of the project with a message and a parent.*
5. How do you list ignored-file rules that match a name?
   *Answer: `git check-ignore -v NAME`.*

## Further reading

- Pro Git (free book): <https://git-scm.com/book/en/v2>
- gitignore format: <https://git-scm.com/docs/gitignore>
