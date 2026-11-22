# Releasing

Releases are published to **PyPI** by the [`release`](../.github/workflows/release.yml)
workflow through an OIDC **trusted publisher**: no API token is stored anywhere.

On a `v*` tag push the workflow runs the full [`checks`](../.github/workflows/checks.yml)
suite on the tagged commit. A red check blocks the publish. The sdist and wheel that are
published are the ones built and tested in that suite. Re-publishing a version that is
already on PyPI is skipped, so re-running a release is safe. The GitHub Release is then
made from the version's CHANGELOG section.

## The release order

This library is released **before** the servers that need the change:

1. Merge the change here, cut the release, and check it installs from PyPI
   (`uv run scripts/smoke.py --refresh --from "second-brain-core==X.Y.Z"`).
2. In each server, move the pin (`second-brain-core>=X.Y,<X.(Y+1)` in 0.x), lock, and
   release the server as usual.

A server never depends on an unreleased library: a path source for local work stays out
of its commits.

## One-time operator setup

1. **A trusted publisher on PyPI**: a GitHub publisher with owner `second-brain-tools`,
   repository `second-brain-core`, workflow `release.yml`, environment `pypi`. Before the
   first release it is a *pending* publisher (**Account → Publishing → Add a pending
   publisher**); after it, **Manage project → Publishing**.
2. **The GitHub environment** `pypi` (Settings → Environments). The name must match the
   publisher's exactly.

## Cutting a release

1. Bump `__version__` in [`src/second_brain_core/__init__.py`](../src/second_brain_core/__init__.py)
   (hatch reads it) and name the release in [`CHANGELOG.md`](../CHANGELOG.md):
   `## [X.Y.Z] — Name`, with its compare link.
2. Merge that to `main`, put a signed tag matching the version on the merged commit, and
   push it:

   ```bash
   git tag -s v0.1.0 -m "v0.1.0 — First cut"
   git push origin v0.1.0
   ```

3. Watch the workflow; then smoke-test the release from PyPI (step 1 of the order above).
