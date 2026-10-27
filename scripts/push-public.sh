#!/bin/bash
# Publish the current main to the public GitHub remote (shrimplabs/shrimphub).
#
# The public repo does NOT share history with this one. Its main started as a
# single-commit snapshot (v0.5.0, 2026-10-27) because the private history holds
# LAN hostnames, the Gitea org, unreleased product names and internal docs.
# Never push main itself, and never force-push: each release is ONE new commit
# on top of public main whose tree is exactly this repo's tree at <ref>.
#
# Usage: scripts/push-public.sh [-y] [-t vX.Y.Z] [-m "message"] [ref]
#   ref  what to publish (default: main); only committed content is published
#   -t   also create and push an annotated release tag on the new commit
#   -m   commit message (default: "Release <tag>" or "Sync from private <sha>")
#   -y   skip the confirmation prompt
#
# Requirements: gitleaks (brew install gitleaks)

set -euo pipefail

REMOTE="github"
YES=0
TAG=""
MSG=""
while getopts "yt:m:" opt; do
    case "$opt" in
        y) YES=1 ;;
        t) TAG="$OPTARG" ;;
        m) MSG="$OPTARG" ;;
        *) sed -n 2,17p "$0"; exit 2 ;;
    esac
done
shift $((OPTIND - 1))
REF="${1:-main}"

cd "$(git rev-parse --show-toplevel)"

command -v gitleaks >/dev/null || { echo "Error: gitleaks not found (brew install gitleaks)"; exit 1; }
git remote get-url "$REMOTE" >/dev/null 2>&1 || {
    echo "Error: remote '$REMOTE' not found. Add it with:"
    echo "  git remote add $REMOTE https://github.com/shrimplabs/shrimphub.git"
    exit 1
}

TREE="$(git rev-parse "$REF^{tree}")"
SRC="$(git rev-parse --short "$REF")"

echo "→ Fetching public main..."
git fetch -q --no-tags "$REMOTE" "+refs/heads/main:refs/remotes/$REMOTE/main"
PARENT="$(git rev-parse "$REMOTE/main")"

if [ "$(git rev-parse "$PARENT^{tree}")" = "$TREE" ]; then
    echo "✓ Public main already matches $REF ($SRC). Nothing to publish."
    [ -z "$TAG" ] && exit 0
    NEW="$PARENT"
else
    echo "→ Scanning the tree being published (gitleaks + .gitleaks.toml private terms)..."
    CHECK="$(mktemp -d)"
    trap 'rm -rf "$CHECK"' EXIT
    git archive "$TREE" | tar -x -C "$CHECK"
    if ! gitleaks dir --no-banner --redact -c .gitleaks.toml "$CHECK"; then
        echo ""
        echo "Error: gitleaks found secrets or private references in $REF. Fix them and commit first."
        exit 1
    fi
    echo "✓ gitleaks clean"

    [ -z "$MSG" ] && MSG="${TAG:+Release $TAG}" && MSG="${MSG:-Sync from private $SRC}"
    NEW="$(git commit-tree "$TREE" -p "$PARENT" -m "$MSG")"
    git update-ref refs/heads/public-main "$NEW"

    echo ""
    echo "Publishing $REF ($SRC) as one commit on public main:"
    git --no-pager diff --stat "$PARENT" "$NEW" | tail -15
fi

if [ "$YES" -ne 1 ]; then
    read -r -p "Push to $(git remote get-url "$REMOTE")${TAG:+ and tag $TAG}? [y/N] " confirm
    [[ "$confirm" =~ ^[yY]$ ]] || { echo "Aborted. (Local branch public-main holds the commit.)"; exit 0; }
fi

if [ "$NEW" != "$PARENT" ]; then
    echo "→ Pushing (fast-forward only)..."
    git push "$REMOTE" "$NEW:refs/heads/main"
fi
if [ -n "$TAG" ]; then
    git tag -a "$TAG" "$NEW" -m "$TAG"
    git push "$REMOTE" "refs/tags/$TAG"
fi

echo "✓ Done: https://github.com/shrimplabs/shrimphub"
