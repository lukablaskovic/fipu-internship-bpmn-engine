from pony.orm import *
from datetime import datetime
import env
import os
import logging
import json

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

DB = Database()


class Event(DB.Entity):
    model_name = Required(str)
    instance_id = Required(str)
    activity_id = Required(str)
    timestamp = Required(datetime, precision=6)
    pending = Required(StrArray)
    activity_variables = Required(Json)

    def to_dict(self):
        return {
            "model_name": self.model_name,
            "instance_id": self.instance_id,
            "activity_id": self.activity_id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "pending": self.pending,
            "activity_variables": self.activity_variables,
        }


class RunningInstance(DB.Entity):
    running = Required(bool)
    instance_id = Required(str, unique=True)


class PendingSubmission(DB.Entity):
    instance_id = Required(str)
    task_id = Required(str)
    form_data = Required(Json)
    composite_key(instance_id, task_id)


@db_session
def save_submission(instance_id, task_id, form_data):
    submission = PendingSubmission(
        instance_id=instance_id, task_id=task_id, form_data=form_data
    )
    commit()
    return submission.id


@db_session
def get_pending_submissions(instance_id):
    return [
        {"id": s.id, "task_id": s.task_id, "form_data": json.loads(json.dumps(s.form_data))}
        for s in PendingSubmission.select(lambda s: s.instance_id == instance_id)
        .order_by(PendingSubmission.id)
    ]


def setup_db():
    try:
        if not os.path.isdir("database"):
            os.mkdir("database")
            logger.info("Created database directory")
        if env.DB["provider"] == "postgres":
            DB.bind(**env.DB)
        else:
            DB.bind(
                provider="sqlite", filename=os.path.abspath("database/database.sqlite"), create_db=True
            )
        DB.generate_mapping(create_tables=True)
        logger.info("Database setup completed")
    except Exception:
        logger.error("Error setting up the database")
        raise


@db_session
def add_event(
    model_name, instance_id, activity_id, timestamp, pending, activity_variables,
    submission_id=None,
):
    Event(
        model_name=model_name,
        instance_id=instance_id,
        activity_id=activity_id,
        timestamp=timestamp,
        pending=pending,
        activity_variables=activity_variables,
    )
    instance = RunningInstance.get(instance_id=instance_id)
    if instance is None:
        RunningInstance(instance_id=instance_id, running=bool(pending))
    else:
        instance.running = bool(pending)
    if submission_id is not None:
        submission = PendingSubmission[submission_id]
        if submission.instance_id != instance_id or submission.task_id != activity_id:
            raise ValueError("Submission does not match the completed task")
        submission.delete()
    # The transition and inbox consumption must commit together.
    commit()


@db_session
def get_all_events():
    logger.info("Fetching all events")
    return select(e for e in Event)[:]


@db_session
def delete_instance(instance_id):
    try:
        instance_to_delete = RunningInstance.get(instance_id=instance_id)
        if instance_to_delete:

            Event.select(lambda e: e.instance_id == instance_id).delete()
            PendingSubmission.select(lambda s: s.instance_id == instance_id).delete()

            instance_to_delete.delete()
            commit()

            logger.info(
                f"Instance and related events deleted for instance_id={instance_id}"
            )
            return {"status": "success"}
        else:
            logger.warning(
                f"Instance not found for deletion with instance_id={instance_id}"
            )
            return {"status": "error", "message": "Instance not found"}
    except Exception as e:
        rollback()
        logger.error(f"Error deleting instance with instance_id={instance_id}: {e}")
        return {"status": "error", "message": str(e)}


@db_session
def get_running_instances_log(running_only=True):
    log = []
    instances = RunningInstance.select(lambda i: i.running or not running_only)[:]
    for instance in instances:
        events = Event.select(
            lambda e: e.instance_id == instance.instance_id
        ).order_by(Event.id)[:]
        if not events:
            raise RuntimeError("Recorded instance has no recovery events")
        log.append({instance.instance_id: {
            "model_path": events[-1].model_name,
            "events": [event.to_dict() for event in events],
        }})
    return log
