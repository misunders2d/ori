"""One-shot migration: stamp authorship as node properties and drop :AUTHORED edges.

Reverses the edge-based authorship model from the Pinecone cutover. For every
`(:Person)-[r:AUTHORED]->(n)` in the graph, copies the author's
`primary_user_id` and the edge's `via_bot` onto `n` as `author_user_id` /
`via_bot` properties (leaving the node's existing `created_at` alone — the
edge `created_at` was redundant with the node's own). Then removes the edge.

Idempotent: re-runs are safe; nodes already stamped are left alone and any
straggler edges are cleaned up. Uses a dry-run counter by default so you can
see the scope before committing.

Usage:
    uv run python scripts/drop_authored_edges.py --dry-run
    uv run python scripts/drop_authored_edges.py --apply

Required env:
    NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from deploy.vault import load_vault  # noqa: E402
load_vault()

from app.core import graph as neo4j_graph  # noqa: E402
from app.core.graph_schema import ensure_schema  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("drop_authored_edges")


COUNT_EDGES = "MATCH (:Person)-[r:AUTHORED]->() RETURN count(r) AS n"

COUNT_UNSTAMPED = (
    "MATCH (p:Person)-[r:AUTHORED]->(n) "
    "WHERE n.author_user_id IS NULL "
    "RETURN count(r) AS n"
)

# Stamp author_user_id + via_bot onto targets that don't already have it.
# Running over the same edges repeatedly is a no-op once stamped.
STAMP = (
    "MATCH (p:Person)-[r:AUTHORED]->(n) "
    "WHERE n.author_user_id IS NULL "
    "SET n.author_user_id = p.primary_user_id, "
    "    n.via_bot        = coalesce(n.via_bot, r.via_bot) "
    "RETURN count(r) AS stamped"
)

# Drop every :AUTHORED edge in the graph. Run only after STAMP completes.
DROP = "MATCH ()-[r:AUTHORED]->() DELETE r RETURN count(r) AS dropped"

# Stamp self-authorship on orphan :Person nodes that never had an :AUTHORED
# edge in the first place (auto-provisioned caller stubs pre-date the edge
# model; also any legacy manually-created stubs). `author_user_id =
# primary_user_id` treats them as self-authored, which matches the
# forward-looking auto-provisioning path.
STAMP_PERSON_ORPHANS = (
    "MATCH (p:Person) "
    "WHERE p.author_user_id IS NULL AND p.primary_user_id IS NOT NULL "
    "SET p.author_user_id = p.primary_user_id "
    "RETURN count(p) AS stamped"
)

# Post-migration sanity: :Memory / :Person nodes with no author_user_id
# shouldn't exist. Report count so we can spot orphans.
COUNT_ORPHANS = (
    "MATCH (n) WHERE (n:Memory OR n:Person) AND n.author_user_id IS NULL "
    "RETURN labels(n) AS labels, count(n) AS n"
)


async def run(apply: bool) -> int:
    driver = neo4j_graph._get_driver()
    if driver is None:
        logger.error("Neo4j not configured (NEO4J_URI / NEO4J_PASSWORD missing).")
        return 1

    # Ensure the new author_user_id indexes exist before we read/write.
    await ensure_schema(driver)

    async with driver.session() as session:
        before_edges = (await (await session.run(COUNT_EDGES)).single())["n"]
        before_unstamped = (await (await session.run(COUNT_UNSTAMPED)).single())["n"]
        logger.info(
            "Pre-flight: %d :AUTHORED edge(s), %d target(s) still missing author_user_id.",
            before_edges, before_unstamped,
        )

        if not apply:
            logger.info("Dry run — no changes made. Re-run with --apply to proceed.")
            return 0

        stamped = (await (await session.run(STAMP)).single())["stamped"]
        logger.info("Stamped author_user_id onto %d node(s).", stamped)

        dropped = (await (await session.run(DROP)).single())["dropped"]
        logger.info("Dropped %d :AUTHORED edge(s).", dropped)

        person_orphans_stamped = (
            await (await session.run(STAMP_PERSON_ORPHANS)).single()
        )["stamped"]
        logger.info(
            "Stamped %d orphan :Person node(s) with self-authorship "
            "(author_user_id = primary_user_id).",
            person_orphans_stamped,
        )

        after_edges = (await (await session.run(COUNT_EDGES)).single())["n"]
        orphans = [dict(r) async for r in await session.run(COUNT_ORPHANS)]
        logger.info("Post-flight: %d :AUTHORED edge(s) remaining.", after_edges)
        if orphans:
            for row in orphans:
                logger.warning(
                    "Orphan without author_user_id: labels=%s count=%d",
                    row["labels"], row["n"],
                )
        else:
            logger.info("No orphan nodes — every :Memory / :Person has author_user_id.")

    await driver.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--dry-run", action="store_true", help="Report counts, make no changes.")
    grp.add_argument("--apply", action="store_true", help="Execute the migration.")
    args = ap.parse_args()
    return asyncio.run(run(apply=args.apply))


if __name__ == "__main__":
    sys.exit(main())
