# syntax=docker/dockerfile:1
# agent-base — the homelab agent platform's harness image (ADR-077/078, docs/agents/).
#
# Bundles the *harnesses* (goose + opencode) plus the *common* project toolchain that recurs across
# the agent-target repos (python/uv/kubectl/gitleaks) on a devbox/nix base. The cloned repo's own
# devbox.json stays AUTHORITATIVE and still runs at startup (boot-from-git) — baking here is purely a
# /nix-store cache-warm: it realises the shared closure at build time so the runtime `devbox install`
# only has to fetch the project-specific delta (k3d/helm/ansible/… stay out of this image). Versions
# track the projects' specifiers so the store paths line up; on drift the worst case is a runtime
# re-fetch, never a wrong toolchain.
#
# One image, two launch modes (see agents/agent-session.sh):
#   • non-interactive — the coordinator runs a recipe headless           → branch + PR
#   • interactive     — you `exec` a shell and drive goose/opencode by hand → branch + PR
# Same scope/secrets/lifecycle either way; only the command differs.
#
# Base = the upstream devbox image (nix + devbox preinstalled, real Debian userland so `exec`
# gives a shell). Floating tag for now; pin to a digest via Renovate once this lands (same
# convention as sleep-tracking/Dockerfile).
FROM jetpackio/devbox:latest

USER root
RUN mkdir -p /opt/agent /work && chown -R devbox:devbox /opt/agent /work

# Materialize the harness layer from the committed lock → deterministic goose/opencode versions.
WORKDIR /opt/agent
COPY --chown=devbox:devbox agent-base/devbox.json agent-base/devbox.lock ./
USER devbox
RUN devbox install

# Put the harness profile on PATH for every shell/exec (interactive + headless).
ENV PATH=/opt/agent/.devbox/nix/profile/default/bin:$PATH

USER root
COPY agent-base/entrypoint.sh /usr/local/bin/agent-entrypoint
COPY agent-base/agent-finalize /usr/local/bin/agent-finalize
RUN chmod +x /usr/local/bin/agent-entrypoint /usr/local/bin/agent-finalize
# Login shells (bash -l) re-read /etc/profile and would drop the harness bin from PATH; re-add it so
# interactive sessions and recipes find goose/opencode regardless of shell type.
RUN printf 'export PATH=/opt/agent/.devbox/nix/profile/default/bin:$PATH\n' > /etc/profile.d/agent-path.sh

# Run as a NUMERIC non-root uid so Kubernetes `runAsNonRoot` can verify it without a per-pod
# runAsUser (the 'devbox' user is uid 1000 in the base image).
USER 1000:1000
WORKDIR /work
# entrypoint preps the repo (clone + branch + project `devbox install`), then exec's CMD:
# a recipe for headless, or `bash -l` for interactive. Override CMD per launch.
ENTRYPOINT ["/usr/local/bin/agent-entrypoint"]
CMD ["bash", "-l"]
