# Changelog

## [1.0.1] - 2024-03-20

### Added
- **Hardened Permission Alignment**: Added intelligent UID/GID mapping in `entrypoint.sh` to ensure the internal agent user always matches the host user, even on detached/missing `.git` repos.
- **Stale Database Lock Cleanup**: Added automatic cleanup of SQLite journal, WAL, and SHM files in `entrypoint.sh` before boot to prevent "Read-only database" errors after unclean shutdowns.
- **SELinux Support**: Added `:z` labels to all Docker bind-mounts in `docker-compose.yml` for cross-distribution compatibility.
- **Self-Evolution Infrastructure**: Included system files (`Dockerfile`, `docker-compose.yml`, etc.) in the build context and image, enabling the agent to autonomously evolve its own infrastructure.

### Fixed
- Fixed potential lockout when the agent user UID (1000) conflicted with the host user UID.
- Fixed 'git pull' permission errors during updates by resetting host-side `.git` ownership in `start.sh`.

## [1.0.0]

### Added
- Integrated `repair_data_permissions` tool into the central toolset in `app/tools/__init__.py`.
- Added functional test `tests/test_repair_data_permissions.py` to verify the filesystem repair logic.

### Fixed
- Fixed potential 'readonly database' errors by ensuring the `data/` directory and its contents have correct write permissions.

evolved by Ori
