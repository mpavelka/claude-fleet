# Publishing the Docker image

Built and pushed by [.github/workflows/docker-publish.yml](../.github/workflows/docker-publish.yml)
via `docker/build-push-action`, multi-arch (`linux/amd64` + `linux/arm64`, via
QEMU emulation on a standard `ubuntu-latest` runner).

**Registry:** `ghcr.io/mpavelka/claude-fleet`. Auth is automatic — GHCR accepts
the workflow's built-in `GITHUB_TOKEN` (scoped `packages: write` in the
job), so there's no secret to create or rotate for this to work.

## Triggers and tags

| Event | Tags produced |
|---|---|
| Push to `main` | `:main`, `:sha-<short-sha>` |
| Push a tag matching `v*.*.*` (e.g. `v1.2.3`) | `:1.2.3`, `:1.2`, `:sha-<short-sha>`, **`:latest`** |
| Manual (`workflow_dispatch`) | whatever the above rules resolve to for the ref it's run on |

`:latest` is **not** produced by a plain push to `main` — it only appears
alongside a semver tag push. This is `docker/metadata-action`'s default
`flavor.latest=auto` behavior: `latest` is added only when a `ref,event=tag`
or `semver` rule actually matches, never for a branch push. There's no
explicit `flavor:` or `type=raw,value=latest` in the workflow — this is
implicit default behavior, not something written in the file.

## Cutting a release

```bash
git tag v0.1.0
git push origin v0.1.0
```

That's the entire release step — pushing the tag alone triggers the build and
populates `:0.1.0`, `:0.1`, and `:latest`.

## Not yet set up

Publishing to Docker Hub in addition to (or instead of) GHCR — open decisions
(repo visibility, whether to keep GHCR, Docker Hub access-token secret setup)
were paused mid-discussion; nothing Docker-Hub-specific exists in the
workflow yet.
