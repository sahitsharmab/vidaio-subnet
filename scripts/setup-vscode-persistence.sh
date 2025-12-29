#!/bin/bash
# VS Code Persistence Setup Script
# This script ensures all VS Code related data is stored in the workspace
# so that it persists across pod restarts.
#
# Run this script on pod startup or when setting up a new environment.

set -e

WORKSPACE_VSCODE_DIR="/workspace/vscode-server"
VSCODE_SERVER_DIR="${HOME}/.vscode-server"

echo "=== VS Code Persistence Setup ==="
echo "Workspace storage: ${WORKSPACE_VSCODE_DIR}"
echo "VS Code server dir: ${VSCODE_SERVER_DIR}"

# Create workspace directories if they don't exist
mkdir -p "${WORKSPACE_VSCODE_DIR}/data"
mkdir -p "${WORKSPACE_VSCODE_DIR}/extensions"
mkdir -p "${WORKSPACE_VSCODE_DIR}/cli"

# Create the .vscode-server directory if it doesn't exist
mkdir -p "${VSCODE_SERVER_DIR}"

# Function to setup symlink
setup_symlink() {
    local source="$1"
    local target="$2"
    local name="$3"

    if [ -L "$source" ]; then
        # Already a symlink, check if it points to the right place
        current_target=$(readlink "$source")
        if [ "$current_target" = "$target" ]; then
            echo "[OK] $name already symlinked to $target"
            return 0
        else
            echo "[UPDATE] $name symlink points to $current_target, updating to $target"
            rm "$source"
        fi
    elif [ -d "$source" ]; then
        # It's a real directory, migrate contents and replace with symlink
        echo "[MIGRATE] Moving $name contents to workspace..."
        if [ "$(ls -A "$source" 2>/dev/null)" ]; then
            cp -rn "$source"/* "$target"/ 2>/dev/null || true
        fi
        rm -rf "$source"
    elif [ -e "$source" ]; then
        echo "[WARN] $source exists but is not a directory or symlink"
        rm -rf "$source"
    fi

    # Create the symlink
    ln -s "$target" "$source"
    echo "[CREATED] $name symlinked to $target"
}

# Setup symlinks for VS Code directories
setup_symlink "${VSCODE_SERVER_DIR}/data" "${WORKSPACE_VSCODE_DIR}/data" "data"
setup_symlink "${VSCODE_SERVER_DIR}/extensions" "${WORKSPACE_VSCODE_DIR}/extensions" "extensions"
setup_symlink "${VSCODE_SERVER_DIR}/cli" "${WORKSPACE_VSCODE_DIR}/cli" "cli"

# Handle VS Code Server binaries (these have version-specific names)
# We'll create a symlink for any existing server files if they exist
if ls "${VSCODE_SERVER_DIR}"/code-* 1>/dev/null 2>&1; then
    for server_binary in "${VSCODE_SERVER_DIR}"/code-*; do
        if [ -f "$server_binary" ] && [ ! -L "$server_binary" ]; then
            binary_name=$(basename "$server_binary")
            echo "[MIGRATE] Moving server binary $binary_name to workspace..."
            mv "$server_binary" "${WORKSPACE_VSCODE_DIR}/"
            ln -s "${WORKSPACE_VSCODE_DIR}/${binary_name}" "$server_binary"
            echo "[CREATED] Server binary symlinked"
        fi
    done
fi

echo ""
echo "=== VS Code Persistence Setup Complete ==="
echo ""
echo "The following directories are now persisted in workspace:"
echo "  - Extensions: ${WORKSPACE_VSCODE_DIR}/extensions"
echo "  - User Data:  ${WORKSPACE_VSCODE_DIR}/data"
echo "  - CLI/Server: ${WORKSPACE_VSCODE_DIR}/cli"
echo ""
echo "To verify, run: ls -la ${VSCODE_SERVER_DIR}"
