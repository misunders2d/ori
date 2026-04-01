# Changelog

## [1.0.0]

### Added
- Integrated `repair_data_permissions` tool into the central toolset in `app/tools/__init__.py`.
- Added functional test `tests/test_repair_data_permissions.py` to verify the filesystem repair logic.

### Fixed
- Fixed potential 'readonly database' errors by ensuring the `data/` directory and its contents have correct write permissions.

evolved by Ori
