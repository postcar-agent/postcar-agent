#!/bin/bash
# Install pre-commit hook: bumps version on every commit.
# Run once after cloning: bash scripts/install_hooks.sh

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK="$REPO_ROOT/.git/hooks/pre-commit"

# A global core.hooksPath (e.g. ~/.git-hooks) silently overrides .git/hooks
# for every repo on the machine, so the hook installed below would never
# fire. Force this repo to use its own .git/hooks regardless of any global
# override.
git config --local core.hooksPath "$REPO_ROOT/.git/hooks"

cat > "$HOOK" << 'EOF'
#!/bin/bash
python3 "$(git rev-parse --show-toplevel)/scripts/bump_version.py"
git add "$(git rev-parse --show-toplevel)/VERSION" \
        "$(git rev-parse --show-toplevel)/postcar_check.py"
EOF

chmod +x "$HOOK"
echo "installed: $HOOK (core.hooksPath pinned to $REPO_ROOT/.git/hooks)"
