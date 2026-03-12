"""Router for analytics endpoints.

Each endpoint performs SQL aggregation queries on the interaction data
populated by the ETL pipeline. All endpoints require a `lab` query
parameter to filter results by lab (e.g., "lab-01").
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlmodel import select as sqlmodel_select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.models.item import ItemRecord
from app.models.learner import Learner
from app.models.interaction import InteractionLog

router = APIRouter()


async def _get_lab_and_task_ids(session: AsyncSession, lab: str):
    """Helper to get lab record and task IDs for a given lab identifier."""
    # Convert lab-04 to "Lab 04" pattern for title matching
    lab_number = lab.replace("lab-", "Lab ")
    
    # Find the lab item using scalars to get model instances
    stmt = sqlmodel_select(ItemRecord).where(
        ItemRecord.type == "lab",
        ItemRecord.title.like(f"%{lab_number}%")
    )
    result = await session.exec(stmt)
    items = list(result)
    
    if not items:
        return None, []
    
    lab_record = items[0]
    
    # Find all task items for this lab
    task_stmt = sqlmodel_select(ItemRecord).where(
        ItemRecord.type == "task",
        ItemRecord.parent_id == lab_record.id
    )
    task_result = await session.exec(task_stmt)
    task_ids = [task.id for task in task_result]
    
    return lab_record, task_ids


@router.get("/scores")
async def get_scores(
    lab: str = Query(..., description="Lab identifier, e.g. 'lab-01'"),
    session: AsyncSession = Depends(get_session),
):
    """Score distribution histogram for a given lab."""
    lab_record, task_ids = await _get_lab_and_task_ids(session, lab)
    
    if not lab_record:
        return [
            {"bucket": "0-25", "count": 0},
            {"bucket": "26-50", "count": 0},
            {"bucket": "51-75", "count": 0},
            {"bucket": "76-100", "count": 0},
        ]
    
    # Include lab itself for interactions
    item_ids = task_ids + [lab_record.id]
    
    # Define score bucket CASE expression
    score_bucket = case(
        (InteractionLog.score <= 25, "0-25"),
        (InteractionLog.score <= 50, "26-50"),
        (InteractionLog.score <= 75, "51-75"),
        (InteractionLog.score <= 100, "76-100"),
        else_="unknown"
    ).label("bucket")
    
    # Query interactions grouped by bucket
    stmt = (
        select(score_bucket, func.count().label("count"))
        .where(
            InteractionLog.item_id.in_(item_ids),
            InteractionLog.score.isnot(None)
        )
        .group_by(score_bucket)
    )
    
    result = await session.exec(stmt)
    bucket_counts = {row.bucket: row.count for row in result}
    
    # Always return all four buckets
    return [
        {"bucket": "0-25", "count": bucket_counts.get("0-25", 0)},
        {"bucket": "26-50", "count": bucket_counts.get("26-50", 0)},
        {"bucket": "51-75", "count": bucket_counts.get("51-75", 0)},
        {"bucket": "76-100", "count": bucket_counts.get("76-100", 0)},
    ]


@router.get("/pass-rates")
async def get_pass_rates(
    lab: str = Query(..., description="Lab identifier, e.g. 'lab-01'"),
    session: AsyncSession = Depends(get_session),
):
    """Per-task pass rates for a given lab."""
    lab_record, task_ids = await _get_lab_and_task_ids(session, lab)
    
    if not lab_record:
        return []
    
    # Get task items with their details
    task_stmt = sqlmodel_select(ItemRecord).where(
        ItemRecord.type == "task",
        ItemRecord.parent_id == lab_record.id
    ).order_by(ItemRecord.title)
    tasks_result = await session.exec(task_stmt)
    
    result = []
    for task in tasks_result:
        # Query avg score and attempts for this task
        stmt = (
            select(
                func.avg(InteractionLog.score).label("avg_score"),
                func.count().label("attempts")
            )
            .where(InteractionLog.item_id == task.id)
        )
        row = (await session.exec(stmt)).first()
        
        if row and row.attempts > 0:
            avg_score = round(float(row.avg_score), 1) if row.avg_score else 0.0
            result.append({
                "task": task.title,
                "avg_score": avg_score,
                "attempts": row.attempts
            })
    
    return result


@router.get("/timeline")
async def get_timeline(
    lab: str = Query(..., description="Lab identifier, e.g. 'lab-01'"),
    session: AsyncSession = Depends(get_session),
):
    """Submissions per day for a given lab."""
    lab_record, task_ids = await _get_lab_and_task_ids(session, lab)
    
    if not lab_record:
        return []
    
    # Include lab itself
    item_ids = task_ids + [lab_record.id]
    
    # Query submissions grouped by date
    stmt = (
        select(
            func.date(InteractionLog.created_at).label("date"),
            func.count().label("submissions")
        )
        .where(InteractionLog.item_id.in_(item_ids))
        .group_by(func.date(InteractionLog.created_at))
        .order_by(func.date(InteractionLog.created_at))
    )
    
    result = await session.exec(stmt)
    
    return [
        {"date": str(row.date), "submissions": row.submissions}
        for row in result
    ]


@router.get("/groups")
async def get_groups(
    lab: str = Query(..., description="Lab identifier, e.g. 'lab-01'"),
    session: AsyncSession = Depends(get_session),
):
    """Per-group performance for a given lab."""
    lab_record, task_ids = await _get_lab_and_task_ids(session, lab)
    
    if not lab_record:
        return []
    
    # Include lab itself
    item_ids = task_ids + [lab_record.id]
    
    # Query per-group stats
    stmt = (
        select(
            Learner.student_group.label("group"),
            func.avg(InteractionLog.score).label("avg_score"),
            func.count(func.distinct(Learner.id)).label("students")
        )
        .join(InteractionLog, InteractionLog.learner_id == Learner.id)
        .where(InteractionLog.item_id.in_(item_ids))
        .group_by(Learner.student_group)
        .order_by(Learner.student_group)
    )
    
    result = await session.exec(stmt)
    
    return [
        {
            "group": row.group,
            "avg_score": round(float(row.avg_score), 1) if row.avg_score else 0.0,
            "students": row.students
        }
        for row in result
    ]
