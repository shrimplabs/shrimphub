#!/bin/bash
# Push a history-filtered copy of main to the public GitHub remote (shrimplabs/shrimphub).
#
# IMPORTANT: Never run `git push github main` directly. The full git history
# contains internal docs (roadmaps, experiment analyses, Fable review sessions)
# that were committed before being moved to internal/ and gitignored. Those
# commits are still in history and would be visible on GitHub.
#
# This script:
#   1. Clones the repo into a temp directory
#   2. Runs git filter-repo to strip internal/ from all commits
#   3. Pushes the cleaned history to the github remote
#   4. Removes the temp directory
#
# Requirements: pip install git-filter-repo
# Run from the repo root.

set -e

REPO_ROOT="$(git rev-parse --show-toplevel)"
GITHUB_REMOTE="${1:-github}"
BRANCH="${2:-main}"
TMPDIR="$(mktemp -d)"

echo "→ Checking dependencies..."
if ! command -v git-filter-repo &>/dev/null; then
    echo "Error: git-filter-repo not found. Install with: pip install git-filter-repo"
    exit 1
fi
if ! command -v gitleaks &>/dev/null; then
    echo "Error: gitleaks not found. Install with: brew install gitleaks"
    exit 1
fi

echo "→ Running gitleaks scan on tracked files..."
cd "$REPO_ROOT"
gitleaks detect --config "$REPO_ROOT/.gitleaks.toml" --redact 2>&1
if [ $? -ne 0 ]; then
    echo ""
    echo "Error: gitleaks found secrets or internal references in tracked files."
    echo "Fix the issues above before pushing to GitHub."
    exit 1
fi
echo "✓ gitleaks scan clean"

echo "→ Checking github remote exists..."
if ! git remote get-url "$GITHUB_REMOTE" &>/dev/null; then
    echo "Error: remote '$GITHUB_REMOTE' not found. Add it with:"
    echo "  git remote add github git@github.com:shrimplabs/shrimphub.git"
    exit 1
fi

GITHUB_URL="$(git remote get-url "$GITHUB_REMOTE")"
echo "→ GitHub remote: $GITHUB_URL"

echo "→ Cloning into temp directory: $TMPDIR/repo"
git clone "$REPO_ROOT" "$TMPDIR/repo" --no-local
cd "$TMPDIR/repo"

echo "→ Stripping internal/ from all commits..."
git filter-repo --invert-paths --path internal/ --force

echo "→ Adding github remote..."
git remote add github "$GITHUB_URL"

echo ""
echo "Filtered history summary:"
git log --oneline | head -10
echo ""
echo "Files that will NOT be present on GitHub (internal/):"
git log --all --full-history -- 'internal/**' | grep -c "^commit" || true

read -p "Looks good? Push to $GITHUB_URL? [y/N] " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
    echo "Aborted."
    cd "$REPO_ROOT"
    rm -rf "$TMPDIR"
    exit 0
fi

echo "→ Pushing to GitHub..."
git push github "$BRANCH"

echo "→ Cleaning up..."
cd "$REPO_ROOT"
rm -rf "$TMPDIR"

echo "✓ Done. Public push complete."
echo "  Verify at: https://github.com/shrimplabs/shrimphub"
