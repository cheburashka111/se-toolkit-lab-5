"""ETL pipeline: fetch data from the autochecker API and load it into the database.

The autochecker dashboard API provides two endpoints:
- GET /api/items — lab/task catalog
- GET /api/logs  — anonymized check results (supports ?since= and ?limit= params)

Both require HTTP Basic Auth (email + password from settings).
"""

from datetime import datetime

import httpx
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select

from app.models.item import ItemRecord
from app.models.learner import Learner
from app.models.interaction import InteractionLog
from app.settings import settings


# ---------------------------------------------------------------------------
# Extract — fetch data from the autochecker API
# ---------------------------------------------------------------------------


async def fetch_items() -> list[dict]:
    """Fetch the lab/task catalog from the autochecker API.

    - Uses httpx.AsyncClient to GET {settings.autochecker_api_url}/api/items
    - Passes HTTP Basic Auth using settings.autochecker_email and
      settings.autochecker_password
    - The response is a JSON array of objects with keys:
      lab (str), task (str | null), title (str), type ("lab" | "task")
    - Returns the parsed list of dicts
    - Raises an exception if the response status is not 200
    """
    url = f"{settings.autochecker_api_url}/api/items"
    auth = (settings.autochecker_email, settings.autochecker_password)

    async with httpx.AsyncClient() as client:
        response = await client.get(url, auth=auth)
        response.raise_for_status()
        return response.json()


async def fetch_logs(since: datetime | None = None) -> list[dict]:
    """Fetch check results from the autochecker API.

    - Uses httpx.AsyncClient to GET {settings.autochecker_api_url}/api/logs
    - Passes HTTP Basic Auth using settings.autochecker_email and
      settings.autochecker_password
    - Query parameters:
      - limit=500 (fetch in batches)
      - since={iso timestamp} if provided (for incremental sync)
    - The response JSON has shape:
      {"logs": [...], "count": int, "has_more": bool}
    - Handles pagination: keeps fetching while has_more is True
      - Uses the submitted_at of the last log as the new "since" value
    - Returns the combined list of all log dicts from all pages
    """
    url = f"{settings.autochecker_api_url}/api/logs"
    auth = (settings.autochecker_email, settings.autochecker_password)

    all_logs = []
    current_since = since

    async with httpx.AsyncClient() as client:
        while True:
            params = {"limit": 500}
            if current_since:
                params["since"] = current_since.isoformat()

            response = await client.get(url, auth=auth, params=params)
            response.raise_for_status()
            data = response.json()

            logs = data.get("logs", [])
            all_logs.extend(logs)

            if not data.get("has_more", False):
                break

            # Update since to the last log's submitted_at for next iteration
            if logs:
                last_log = logs[-1]
                submitted_at_str = last_log.get("submitted_at")
                if submitted_at_str:
                    current_since = datetime.fromisoformat(submitted_at_str.replace("Z", "+00:00"))
            else:
                break

    return all_logs


# ---------------------------------------------------------------------------
# Load — insert fetched data into the local database
# ---------------------------------------------------------------------------


async def load_items(items: list[dict], session: AsyncSession) -> int:
    """Load items (labs and tasks) into the database.

    - Processes labs first (items where type="lab"):
      - For each lab, checks if an item with type="lab" and matching title
        already exists (SELECT)
      - If not, INSERT a new ItemRecord(type="lab", title=lab_title)
      - Builds a dict mapping the lab's short ID (the "lab" field, e.g.
        "lab-01") to the lab's database record, so you can look up
        parent IDs when processing tasks
    - Then processes tasks (items where type="task"):
      - Finds the parent lab item using the task's "lab" field (e.g.
        "lab-01") as the key into the dict built above
      - Checks if a task with this title and parent_id already exists
      - If not, INSERT a new ItemRecord(type="task", title=task_title,
        parent_id=lab_item.id)
    - Commits after all inserts
    - Returns the number of newly created items
    """
    new_items_count = 0
    lab_id_to_record: dict[str, ItemRecord] = {}

    # Process labs first
    for item in items:
        if item.get("type") != "lab":
            continue

        lab_title = item.get("title")
        lab_short_id = item.get("lab")

        # Check if lab already exists
        existing = await session.exec(
            select(ItemRecord).where(
                ItemRecord.type == "lab",
                ItemRecord.title == lab_title
            )
        )
        lab_record = existing.first()

        if not lab_record:
            lab_record = ItemRecord(type="lab", title=lab_title)
            session.add(lab_record)
            new_items_count += 1

        # Map short ID to record for task lookup
        if lab_short_id:
            lab_id_to_record[lab_short_id] = lab_record

    # Process tasks
    for item in items:
        if item.get("type") != "task":
            continue

        task_title = item.get("title")
        lab_short_id = item.get("lab")

        # Find parent lab
        parent_lab = lab_id_to_record.get(lab_short_id)
        if not parent_lab:
            continue

        # Check if task already exists
        existing = await session.exec(
            select(ItemRecord).where(
                ItemRecord.type == "task",
                ItemRecord.title == task_title,
                ItemRecord.parent_id == parent_lab.id
            )
        )
        task_record = existing.first()

        if not task_record:
            task_record = ItemRecord(type="task", title=task_title, parent_id=parent_lab.id)
            session.add(task_record)
            new_items_count += 1

    await session.commit()
    return new_items_count


async def load_logs(
    logs: list[dict], items_catalog: list[dict], session: AsyncSession
) -> int:
    """Load interaction logs into the database.

    Args:
        logs: Raw log dicts from the API (each has lab, task, student_id, etc.)
        items_catalog: Raw item dicts from fetch_items() — needed to map
            short IDs (e.g. "lab-01", "setup") to item titles stored in the DB.
        session: Database session.

    - Builds a lookup from (lab_short_id, task_short_id) to item title
      using items_catalog. For labs, the key is (lab, None). For tasks,
      the key is (lab, task). The value is the item's title.
    - For each log dict:
      1. Find or create a Learner by external_id (log["student_id"])
         - If creating, set student_group from log["group"]
      2. Find the matching item in the database:
         - Use the lookup to get the title for (log["lab"], log["task"])
         - Query the DB for an ItemRecord with that title
         - Skip this log if no matching item is found
      3. Check if an InteractionLog with this external_id already exists
         (for idempotent upsert — skip if it does)
      4. Create InteractionLog with:
         - external_id = log["id"]
         - learner_id = learner.id
         - item_id = item.id
         - kind = "attempt"
         - score = log["score"]
         - checks_passed = log["passed"]
         - checks_total = log["total"]
         - created_at = parsed log["submitted_at"]
    - Commits after all inserts
    - Returns the number of newly created interactions
    """
    new_logs_count = 0

    # Build lookup from (lab, task) to title
    # Key: (lab_short_id, task_short_id or None), Value: title
    item_title_lookup: dict[tuple[str, str | None], str] = {}
    for item in items_catalog:
        lab_short_id = item.get("lab")
        task_short_id = item.get("task")  # Can be None for labs
        title = item.get("title")
        if lab_short_id and title:
            item_title_lookup[(lab_short_id, task_short_id)] = title

    for log in logs:
        # 1. Find or create learner
        student_id = log.get("student_id")
        student_group = log.get("group", "")

        learner = await session.exec(
            select(Learner).where(Learner.external_id == student_id)
        )
        learner_record = learner.first()

        if not learner_record:
            learner_record = Learner(external_id=student_id, student_group=student_group)
            session.add(learner_record)
            await session.flush()  # Get the ID

        # 2. Find matching item
        lab_short_id = log.get("lab")
        task_short_id = log.get("task")  # Can be None

        # Build the lookup key - for logs, task can be None for lab-level logs
        lookup_key = (lab_short_id, task_short_id if task_short_id else None)
        item_title = item_title_lookup.get(lookup_key)

        if not item_title:
            # Skip if no matching item found
            continue

        # Query for the item by title
        # For tasks, we need to find by title; for labs, type="lab" and title
        if task_short_id:
            # It's a task - need to find the task item
            # We need to find parent lab first
            lab_title = item_title_lookup.get((lab_short_id, None))
            if not lab_title:
                continue

            parent_lab = await session.exec(
                select(ItemRecord).where(
                    ItemRecord.type == "lab",
                    ItemRecord.title == lab_title
                )
            )
            parent_lab_record = parent_lab.first()

            if not parent_lab_record:
                continue

            item_record = await session.exec(
                select(ItemRecord).where(
                    ItemRecord.type == "task",
                    ItemRecord.title == item_title,
                    ItemRecord.parent_id == parent_lab_record.id
                )
            )
        else:
            # It's a lab
            item_record = await session.exec(
                select(ItemRecord).where(
                    ItemRecord.type == "lab",
                    ItemRecord.title == item_title
                )
            )

        item_obj = item_record.first()
        if not item_obj:
            continue

        # 3. Check for idempotency - skip if external_id already exists
        existing_log = await session.exec(
            select(InteractionLog).where(InteractionLog.external_id == log.get("id"))
        )
        if existing_log.first():
            continue

        # 4. Create InteractionLog
        submitted_at_str = log.get("submitted_at")
        created_at = None
        if submitted_at_str:
            created_at = datetime.fromisoformat(submitted_at_str.replace("Z", "+00:00"))

        interaction_log = InteractionLog(
            external_id=log.get("id"),
            learner_id=learner_record.id,
            item_id=item_obj.id,
            kind="attempt",
            score=log.get("score"),
            checks_passed=log.get("passed"),
            checks_total=log.get("total"),
            created_at=created_at
        )
        session.add(interaction_log)
        new_logs_count += 1

    await session.commit()
    return new_logs_count


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def sync(session: AsyncSession) -> dict:
    """Run the full ETL pipeline.

    - Step 1: Fetch items from the API (keep the raw list) and load them
      into the database
    - Step 2: Determine the last synced timestamp
      - Query the most recent created_at from InteractionLog
      - If no records exist, since=None (fetch everything)
    - Step 3: Fetch logs since that timestamp and load them
      - Pass the raw items list to load_logs so it can map short IDs
        to titles
    - Return a dict: {"new_records": <number of new interactions>,
                      "total_records": <total interactions in DB>}
    """
    # Step 1: Fetch and load items
    items = await fetch_items()
    await load_items(items, session)

    # Step 2: Determine last synced timestamp
    latest_log = await session.exec(
        select(InteractionLog).order_by(InteractionLog.created_at.desc()).limit(1)
    )
    latest_record = latest_log.first()
    since = latest_record.created_at if latest_record else None

    # Step 3: Fetch and load logs
    logs = await fetch_logs(since=since)
    new_records = await load_logs(logs, items, session)

    # Get total count
    total_count = await session.exec(
        select(InteractionLog).count()
    )

    return {"new_records": new_records, "total_records": total_count}
