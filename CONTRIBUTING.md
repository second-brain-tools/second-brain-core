# Contributing

Thanks for your interest in second-brain-core.

**Shared code is fixed here, not in a server.** Fix it here, release, then move each
server's pin; a server that carries its own copy of a fix is how the copies drifted
apart before this library existed.

## Development setup

We use [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/second-brain-tools/second-brain-core
cd second-brain-core
uv sync
uv run pre-commit install   # enable lint/format on commit
```

To try a change in a server before it is released, point the server's checkout at
yours for the moment, and don't commit it:

```toml
# in the server's pyproject.toml, locally only
[tool.uv.sources]
second-brain-core = { path = "../second-brain-core", editable = true }
```

## Running tests

```bash
uv run pytest --cov-fail-under=90
```

The tests run against a small fake server (`tests/fakeserver`) under two variable
prefixes, so nothing is tied to one server's names. Coverage is printed on every run;
the 90 % gate is enforced in CI.

## Linting and type checking

```bash
uvx pre-commit run --all-files   # ruff lint + format + actionlint (the authoritative gate)
uv run mypy                      # strict, over src, tests and scripts
```

## How CI works

[`.github/workflows/checks.yml`](.github/workflows/checks.yml) holds every gate:
lint, type check, tests on Python 3.11 to 3.15 on Linux and on 3.13 on macOS and
Windows, and a build with `twine check`. The built wheel is then installed in a
fresh environment and used ([`scripts/smoke.py`](scripts/smoke.py)) on all three
systems. [`ci.yml`](.github/workflows/ci.yml) runs it on every push and pull request,
and [`release.yml`](.github/workflows/release.yml) before publishing.

## License of contributions

AGPL-3.0-or-later ([LICENSE](LICENSE)). By opening a pull request you agree to that.
A sign-off (`git commit -s`) says you have the right to submit the change under those
terms ([Developer Certificate of Origin](https://developercertificate.org/)).

## Pull requests

- Target `main`; the PR title is a lower-case conventional commit (`fix: …`).
- One logical change per PR, with tests and a CHANGELOG entry.
- CI must pass before merge. Merges are squashed.
