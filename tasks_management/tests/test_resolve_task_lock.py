"""resolve_task locks the task and refuses a task that is no longer open, so a vote never saves back a task that
another transaction closed."""
import threading
import time

from django.db import connection, connections, transaction
from django.test import TestCase, TransactionTestCase

from core.test_helpers import create_test_interactive_user
from tasks_management.apps import TasksManagementConfig
from tasks_management.models import Task, TaskDecision, TaskExecutor, TaskGroup
from tasks_management.services import COMPLETION_ERROR_KEY, TaskService, _fail_completed_task

WAIT_SECONDS = 10
WAITING_ON_A_LOCK = (
    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
    "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
)
EVENT = "tm_resolve_lock.event"
REASON = "closed by a business rule"


class ResolveFixtures:
    GROUP_CODE = "TM-RL-ANY"

    def build(self, username):
        self.user = create_test_interactive_user(username=username)
        self.group = TaskGroup(code=self.GROUP_CODE, completion_policy=TaskGroup.TaskGroupCompletionPolicy.ANY)
        self.group.save(user=self.user)
        TaskExecutor(task_group=self.group, user=self.user).save(user=self.user)
        self.task = Task(source="tm_resolve_lock", business_event=EVENT, task_group=self.group,
                         status=Task.Status.ACCEPTED,
                         executor_action_event=TasksManagementConfig.default_executor_event)
        self.task.save(user=self.user)

    def resolve(self):
        return TaskService(self.user).resolve_task({
            "id": self.task.id, "business_status": {str(self.user.id): "APPROVED"}})


class ResolveClosedTaskTest(ResolveFixtures, TestCase):
    def setUp(self):
        self.build("tm_resolve_closed")

    def test_a_failed_task_is_refused_and_keeps_its_status_and_reason(self):
        _fail_completed_task(self.task.id, self.user, REASON)

        result = self.resolve()

        self.assertFalse(result["success"], result)
        task = Task.objects.get(id=self.task.id)
        self.assertEqual((task.status, task.json_ext[COMPLETION_ERROR_KEY]), (Task.Status.FAILED, REASON))
        self.assertFalse(task.business_status)

    def test_a_completed_task_is_refused(self):
        self.assertTrue(TaskService(self.user).complete_task({"id": self.task.id})["success"])

        result = self.resolve()

        self.assertFalse(result["success"], result)
        self.assertEqual(Task.objects.get(id=self.task.id).status, Task.Status.COMPLETED)

    def test_an_open_task_is_resolved(self):
        result = self.resolve()

        self.assertTrue(result["success"], result)
        self.assertEqual(Task.objects.get(id=self.task.id).status, Task.Status.COMPLETED)


class ResolveAgainstCloserTest(ResolveFixtures, TransactionTestCase):
    """Committed rows, removed on teardown in place of a flush."""

    GROUP_CODE = "TM-RL-RACE"

    def setUp(self):
        self.build("tm_resolve_race")

    def _fixture_teardown(self):
        tasks = list(Task.objects.filter(business_event=EVENT).values_list("id", flat=True))
        TaskDecision.history.model.objects.filter(task_id__in=tasks).delete()
        TaskDecision.objects.filter(task_id__in=tasks).delete()
        Task.history.model.objects.filter(id__in=tasks).delete()
        Task.objects.filter(id__in=tasks).delete()
        executors = TaskExecutor.objects.filter(task_group__code=self.GROUP_CODE)
        TaskExecutor.history.model.objects.filter(id__in=list(executors.values_list("id", flat=True))).delete()
        executors.delete()
        TaskGroup.history.model.objects.filter(code=self.GROUP_CODE).delete()
        TaskGroup.objects.filter(code=self.GROUP_CODE).delete()

    @staticmethod
    def wait_for_a_lock_waiter():
        deadline = time.monotonic() + WAIT_SECONDS
        with connection.cursor() as cursor:
            while time.monotonic() < deadline:
                cursor.execute("SELECT pg_stat_clear_snapshot()")
                cursor.execute(WAITING_ON_A_LOCK)
                if cursor.fetchone()[0]:
                    return True
                time.sleep(0.05)
        return False

    def test_a_resolve_while_a_closer_fails_the_task_is_refused_and_the_task_stays_failed(self):
        holding, waited, results = threading.Event(), [], {}

        def closer():
            with transaction.atomic():
                Task.objects.select_for_update().get(id=self.task.id)
                holding.set()
                waited.append(self.wait_for_a_lock_waiter())
                _fail_completed_task(self.task.id, self.user, REASON)

        def approver():
            holding.wait(WAIT_SECONDS)
            results["approver"] = self.resolve()

        def in_thread(target):
            def body():
                try:
                    target()
                except Exception as exc:
                    results[target.__name__] = repr(exc)
                finally:
                    connections.close_all()

            return threading.Thread(target=body)

        threads = [in_thread(closer), in_thread(approver)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(4 * WAIT_SECONDS)
            self.assertFalse(thread.is_alive())
        self.assertEqual(waited, [True], results)
        self.assertFalse(results["approver"]["success"], results)
        self.assertIn("is FAILED and can no longer be resolved", results["approver"]["detail"])
        task = Task.objects.get(id=self.task.id)
        self.assertEqual((task.status, task.json_ext[COMPLETION_ERROR_KEY]), (Task.Status.FAILED, REASON))
