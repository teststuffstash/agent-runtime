# agent-runtime

The homelab agent platform's **harness image** family. One small repo, one responsibility: build and
publish the container image that agent sessions run in. Design + operation live in the
[homelab repo](https://github.com/teststuffstash/homelab) (`docs/agents/`, ADR-077/078/081); this
repo only produces the artifact.

## Why this is its own repo

The image needs a build-and-publish pipeline (docker → ghcr, versioned, Renovate). Bolting that onto
the homelab IaC monorepo would mean path filters and "what does this push build/deploy?" rules on a
repo that otherwise just applies Tofu/Ansible. Here the rule is trivial: **every push to `master`
builds**. Same Tier-A pattern as the app repos (sleep-tracking, snore-recorder) — homelab is the
*consumer* (it references the image by tag), not the builder.

## `agent-base`

`FROM jetpackio/devbox` + the **harnesses** pinned via devbox/nix: **goose-cli + opencode** (plus
the basic shell tools agents reach for so they don't fall back to writing Python: gh, git, ripgrep,
curl, wget, jq). It deliberately bakes **no project toolchain** — the per-project
python/uv/pytest/… is materialized at runtime from the cloned repo's own `devbox.json`
(boot-from-git), so the image stays lean and project-agnostic. One image, two launch modes (the
launcher lives in homelab as `agents/agent-session.sh`):

- **non-interactive** — a recipe runs headless → branch + PR
- **interactive** — you `exec` a shell, drive goose/opencode with model overrides → branch + PR

The only seam in/out is **git** (clone at base ref → branch → push) — the contract borrowed from
Daytona's opencode-plugin, on a self-hosted, ephemeral k8s pod instead of hosted persistent sandboxes.

## Versioning

The image tag is **content-addressed by `agent-base/devbox.lock`**: `YYYY-MM-DD-<lockhash8>`. The lock
pins the exact nixpkgs closure, so the same lock rebuilds bit-for-bit, and a Renovate lock bump is
what cuts a new image. `:latest` tracks `master`.

## Build

```sh
bash scripts/build-image.sh             # build only (needs docker)
PUSH=true bash scripts/build-image.sh   # build + push (after `docker login ghcr.io`)
```

CI does the same on every `master` push (`.github/workflows/build-image.yaml`, runner
`homelab-ephemeral`, ghcr via the job `GITHUB_TOKEN`).

## Layout

```
agent-base/
  Dockerfile         FROM jetpackio/devbox; devbox install the harnesses
  devbox.json/.lock  the harness pins (goose-cli, opencode, gh, git, rg, jq)
  entrypoint.sh      git-only seam: clone → branch → project devbox install → exec harness/shell
scripts/build-image.sh        reproducible build, tag-by-lockhash, cosign/SBOM stubs (SLSA-L2 TODO)
.github/workflows/build-image.yaml
```

## Bootstrap (one-time, per homelab `docs/github-setup`)

- Create the GitHub repo `teststuffstash/agent-runtime`, push `master`.
- First image push: make the ghcr package **public** (or wire a pull secret) — the "required click".
- Enable Renovate.

## Roadmap

- **Profiles** — a lean `fix` image (current) and a heavy `gate` image (k3d + Playwright) for the
  full-stack confidence test; both built here with the same one-line workflow.
- **SLSA-L2** — flip on the cosign-sign + SBOM-attest stubs (homelab `docs/slsa.md`).
- **Digest-pin the base** — Renovate the `jetpackio/devbox` `FROM` to a `@sha256:` digest.
