# 📜 Changelog

All notable changes to this project will be documented in this file.

## [0.8.1] - 2024-03-30
### Added
- **New Skill: `git-management-skill`**: Refined protocols for an autonomous agent to manage its own Git repository, including index hygiene, push collision resolution, and sandbox cleaning.
- **Improved Integration**: `DeveloperAgent` now loads and uses the `git-management-skill` for all evolution tasks.

### Changed
- **System Management Skill**: Mandatory integration of git protocols for all evolution-related actions.
- **Version Bump**: Updated system version to `0.8.1` and refined version bump tests.

### Fixed
- Improved resilience against "ghost" file commits by mandating sandbox hygiene protocols.

evolved by Ori
