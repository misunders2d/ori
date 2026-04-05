import os

from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _is_child_container() -> bool:
    """Detect if we're running as a spawned child (no .git, no Docker)."""
    return not os.path.isdir(os.path.join(_PROJECT_ROOT, ".git"))


class EvolutionToolset(BaseToolset):
    """Groups all self-evolution tools: read, stage, verify, commit, git ops."""

    async def get_tools(self, readonly_context=None):
        from app.tools.evolution import (
            evolution_read_file,
            evolution_list_directory,
        )
        from app.tools.research import check_installed_package

        # Read-only tools available everywhere
        tools = [
            FunctionTool(func=evolution_read_file),
            FunctionTool(func=evolution_list_directory),
            FunctionTool(func=check_installed_package),
        ]

        if not _is_child_container():
            # Parent-only: mutation and git tools
            from app.tools.evolution import (
                evolution_stage_change,
                evolution_verify_sandbox,
                evolution_commit_and_push,
                evolution_git_pull,
                evolution_git_reset,
                evolution_sync_local_to_upstream,
            )
            from app.tools.evolution_catalog import (
                evolution_catalog,
                evolution_search,
                evolution_share,
                evolution_import,
            )
            tools.extend([
                FunctionTool(func=evolution_stage_change),
                FunctionTool(func=evolution_verify_sandbox),
                FunctionTool(func=evolution_commit_and_push),
                FunctionTool(func=evolution_git_pull),
                FunctionTool(func=evolution_git_reset),
                FunctionTool(func=evolution_sync_local_to_upstream),
                FunctionTool(func=evolution_catalog),
                FunctionTool(func=evolution_search),
                FunctionTool(func=evolution_share),
                FunctionTool(func=evolution_import),
            ])

        return tools
