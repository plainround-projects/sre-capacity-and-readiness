# pip, virtual environments and hash-pinned lock files

## What it is

- **Python packages** are libraries other people wrote. **pip** is Python's package installer: `pip install aiohttp` downloads and installs the library.
- A **virtual environment** (venv) is a private folder holding its own Python and its own installed packages, so one project's libraries do not interfere with another's or with the system's.
- A **lock file** lists the exact version of every package, including packages those packages need (**transitive dependencies**). A **hash** is a checksum of the downloaded file. A lock file with hashes lets pip reject a file that is not exactly the one that was reviewed.
- **uv** is a fast tool from Astral that can produce such lock files (`uv pip compile`). **pip-audit** checks locked packages against a database of known security problems.

## Why this repository uses it

A repeatable build needs the same libraries on your machine, in the container and in CI. Hash pinning also protects against a package being swapped on the package index. The audit step makes "no known vulnerabilities" a checked property.

## Where it appears

| File | Role |
| --- | --- |
| `requirements.lock` | Runtime packages with versions and hashes, used by the `Dockerfile`. |
| `requirements-dev.lock` | The same plus development tools, used by `make install`. |
| `pyproject.toml` | The loose list of what is wanted, which the lock files are compiled from. |
| `Makefile` | `$(STAMP)` creates the venv and installs; `lock` regenerates; `audit` runs pip-audit. |
| `Dockerfile` | `pip install --require-hashes -r requirements.lock`. |

## How it works underneath

1. `python3 -m venv .venv` creates a folder with a copy (or link) of Python and an empty `site-packages` folder where installed libraries go. Running `.venv/bin/python` uses that folder.
2. `pip install -r file` reads the file, asks the package index (PyPI) for each file, downloads it, and unpacks it into `site-packages`.
3. With `--require-hashes`, pip computes the SHA-256 of each download and only installs it if it matches a `--hash=` entry. In this mode all packages must be pinned with `==`.
4. `uv pip compile pyproject.toml --generate-hashes` resolves the loose requirements into exact versions for the target Python version and writes hashes for each.

## Walkthrough of this repository's use

### The lock file

```text
aiohttp==3.14.5 \
    --hash=sha256:0133c3c3b54a0bf1e71fa5c1ad95c93f07fd54e24ef1fe182f5122e1573d2bf1 \
```

Each package is on a line like `aiohttp==3.14.5 \` followed by one or more `--hash=sha256:...` lines (only the first hash is shown above). `==3.14.5` pins the exact version. There are several hashes per package, one per downloadable file (for different platforms). The file is sorted alphabetically, so it starts with `aiohappyeyeballs==2.7.1`, a small library that `aiohttp` needs. The lock files have no header (`--no-header`) so they stay small and stable.

### How it is created

```make
lock: ## regenerate the hash-pinned lock files (needs uv)
	uv pip compile pyproject.toml --generate-hashes --python-version 3.12 --no-header -o requirements.lock
	uv pip compile pyproject.toml --extra dev --generate-hashes --python-version 3.12 --no-header -o requirements-dev.lock
```

`uv pip compile pyproject.toml --generate-hashes --python-version 3.12 --no-header -o requirements.lock` reads the runtime `dependencies` and writes a lock file for Python 3.12. The second command adds `--extra dev` to include the development group. `-o` names the output file.

### How it is used to install

```make
$(STAMP): requirements-dev.lock pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -q --require-hashes -r requirements-dev.lock
	$(VENV)/bin/pip install -q --no-deps -e .
	touch $@
```

- `$(PYTHON) -m venv $(VENV)` creates `.venv`.
- `pip install -q --require-hashes -r requirements-dev.lock` installs every locked package and verifies hashes.
- `pip install -q --no-deps -e .` installs the project itself without touching dependencies.
- `touch $@` marks success so Make skips this next time.

### Why `--no-deps` for the project

The lock file already installed everything. Without `--no-deps`, pip would try to resolve `aiohttp>=3.14.4,<4` again, and in hash mode that would conflict.

### The audit

```make
audit: $(STAMP) ## check the locked dependencies for known vulnerabilities
	$(VENV)/bin/pip-audit --no-deps --disable-pip -r requirements-dev.lock
```

`pip-audit --no-deps --disable-pip -r requirements-dev.lock` reads the locked list and checks each package and version against a vulnerability database. `--no-deps` and `--disable-pip` tell it to trust the list as the complete answer and not create its own environment. It exits non-zero when it finds a known vulnerability. This repository re-locked `aiohttp` to 3.14.5 when an earlier version had known problems. The lock also pins `psycopg==3.2.9`, `psycopg-binary==3.2.9` and, in this repository, `psycopg-pool==3.2.6` (the ledger uses psycopg's own connection pool).

## Try it yourself

1. `head -3 requirements.lock` shows the first package and its hashes.
2. `.venv/bin/pip list | head` lists installed packages and versions.
3. `.venv/bin/pip freeze | grep aiohttp` prints `aiohttp==3.14.5`.
4. `make audit` prints `No known vulnerabilities found`.
5. Edit a hash in a copy of the lock file and run `pip install --require-hashes -r copy.lock` in a scratch venv; pip refuses with a hash mismatch message.

## Mistakes people make

- Installing into the system Python with `pip install` and no venv.
- Editing the lock file by hand. Regenerate it with `make lock`.
- Mixing hashed and unhashed requirements: pip's hash mode requires all to be pinned.
- Forgetting that a lock is for a specific Python version (`--python-version 3.12`).

## Check yourself

1. What does a virtual environment isolate?
   *Answer: the installed packages (and the Python used) for one project.*
2. What does `--require-hashes` guarantee?
   *Answer: each downloaded file matches a checksum recorded in the lock file.*
3. What is a transitive dependency?
   *Answer: a package that one of your packages needs.*
4. Why `--no-deps` when installing the project itself?
   *Answer: all dependencies were already installed from the lock file.*
5. What does `make audit` do?
   *Answer: checks the locked packages for known vulnerabilities.*

## Further reading

- pip 24+ documentation, hash-checking mode: <https://pip.pypa.io/en/stable/topics/secure-installs/>
- venv: <https://docs.python.org/3.12/library/venv.html>
- uv pip compile: <https://docs.astral.sh/uv/pip/compile/>
- pip-audit 2.10: <https://pypi.org/project/pip-audit/>
