# syntax=docker/dockerfile:1
# agent-base — the homelab agent platform's harness image (ADR-077/078, docs/agents/).
#
# Bundles the *harnesses* (goose + opencode + claude-code, homelab FU-066) plus the *common* project toolchain that recurs across
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
COPY agent-base/agent-storm-watchdog /usr/local/bin/agent-storm-watchdog
RUN chmod +x /usr/local/bin/agent-entrypoint /usr/local/bin/agent-finalize /usr/local/bin/agent-storm-watchdog
# Login shells (bash -l) re-read /etc/profile and would drop the harness bin from PATH; re-add it so
# interactive sessions and recipes find goose/opencode regardless of shell type.
RUN printf 'export PATH=/opt/agent/.devbox/nix/profile/default/bin:$PATH\n' > /etc/profile.d/agent-path.sh

# Pre-seed the claude harness (homelab FU-066): a headless `claude -p` in a fresh pod would otherwise
# stop on the onboarding wizard, the /work/repo trust dialog, or the bypass-permissions warning — the
# same three prompts the agent-coordinator image seeds (keep the two in sync). Auth is deliberately
# NOT here: the pod carries only ANTHROPIC_BASE_URL + an opaque `ref:` token; the egress proxy
# injects the real subscription credential (ADR-087 leg A).
RUN mkdir -p /home/devbox/.claude \
    && printf '%s\n' '{"hasCompletedOnboarding":true,"projects":{"/work/repo":{"hasTrustDialogAccepted":true}}}' \
        > /home/devbox/.claude.json \
    && printf '%s\n' '{"permissions":{"allow":["Bash(*)","Read(*)","Edit(*)","Write(*)","WebSearch(*)","WebFetch(*)"]},"skipDangerousModePermissionPrompt":true}' \
        > /home/devbox/.claude/settings.json \
    && chown -R devbox:devbox /home/devbox/.claude /home/devbox/.claude.json

# Run as a NUMERIC non-root uid so Kubernetes `runAsNonRoot` can verify it without a per-pod
# runAsUser (the 'devbox' user is uid 1000 in the base image).
USER 1000:1000
WORKDIR /work
# entrypoint preps the repo (clone + branch + project `devbox install`), then exec's CMD:
# a recipe for headless, or `bash -l` for interactive. Override CMD per launch.
ENTRYPOINT ["/usr/local/bin/agent-entrypoint"]
CMD ["bash", "-l"]
