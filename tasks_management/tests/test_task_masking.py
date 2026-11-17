"""Tasks are masked by the masks other modules register for Task and HistoricalTask, for readers without the
masked-data right, on every path reaching them; the entity string filter is refused to those readers."""
import json

from graphene import relay

from core.data_masking import DataMaskAbs, MaskingClassRegistryPoint
from core.models.openimis_graphql_test_case import openIMISGraphQLTestCase, BaseTestContext
from core.test_helpers import create_test_interactive_user, create_test_role
from tasks_management.apps import TasksManagementConfig
from tasks_management.models import Task, TaskExecutor, TaskGroup

MASK = DataMaskAbs().anonymize("")


def relay_id(type_name, pk):
    return relay.Node.to_global_id(type_name, str(pk))
SOURCE = "mask_task_source"


class TaskTestMask(DataMaskAbs):
    masking_model = "Task"
    anon_fields = ["data.incoming_data.first_name", "json_ext.secret", "entity_string"]
    masking_enabled = True


class HistoricalTaskTestMask(TaskTestMask):
    masking_model = "HistoricalTask"


class EchoBusinessDataSerializer:
    def __init__(self, user):
        self.user = user

    def serialize(self, data):
        return data


class TaskMaskingGQLTestCase(openIMISGraphQLTestCase):
    masks = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        admin = create_test_interactive_user(username="mask_task_admin")
        reader_role = create_test_role(
            perm_names=["gql_task_search_perms", "gql_task_group_search_perms"], name="MaskTaskReaderRole")
        unmasked_role = create_test_role(
            perm_names=["gql_task_search_perms", "gql_task_group_search_perms",
                        "gql_query_enable_viewing_masked_data_perms"],
            name="MaskTaskUnmaskedRole")
        cls.reader = create_test_interactive_user(username="mask_task_reader", roles=[reader_role.id])
        cls.unmasked_reader = create_test_interactive_user(username="mask_task_unmasked", roles=[unmasked_role.id])
        cls.entity = create_test_interactive_user(username="mask_task_entity")
        cls.reader_token = BaseTestContext(user=cls.reader).get_jwt()
        cls.unmasked_token = BaseTestContext(user=cls.unmasked_reader).get_jwt()

        cls.group = TaskGroup(code="mask_task_group", completion_policy="ANY")
        cls.group.save(username=admin.username)
        for user in (cls.reader, cls.unmasked_reader):
            TaskExecutor(task_group=cls.group, user=user).save(username=admin.username)
        cls.task = Task(
            source=SOURCE, status=Task.Status.ACCEPTED, business_event="mask_task_event",
            executor_action_event=TasksManagementConfig.default_executor_event,
            business_status={}, task_group=cls.group, entity=cls.entity,
            data={"incoming_data": {"first_name": "Ninette", "code": "C-1"}},
            json_ext={"secret": "S-42", "open": "visible"},
            business_data_serializer=f"{__name__}.EchoBusinessDataSerializer.serialize",
        )
        cls.task.save(username=admin.username)
        cls.masks = [TaskTestMask(), HistoricalTaskTestMask()]
        MaskingClassRegistryPoint.register_masking_class(cls.masks)

    @classmethod
    def tearDownClass(cls):
        MaskingClassRegistryPoint.REGISTERED_MASKING_CLASS[:] = [
            entry for entry in MaskingClassRegistryPoint.REGISTERED_MASKING_CLASS
            if entry["class_reference"] not in (cls.masks or [])]
        super().tearDownClass()

    def _query(self, token, query):
        response = self.query(query, headers={"HTTP_AUTHORIZATION": f"Bearer {token}"})
        return json.loads(response.content)

    def _nodes(self, token, field, selection, arguments=f'source: "{SOURCE}"'):
        content = self._query(token, f"{{ {field}({arguments}) {{ edges {{ node {{ {selection} }} }} }} }}")
        self.assertNotIn("errors", content)
        return [edge["node"] for edge in content["data"][field]["edges"]]

    def _task_set_nodes(self, token, selection):
        content = self._query(token, f"""
            {{ taskGroup(code: "mask_task_group") {{ edges {{ node {{
                taskSet {{ edges {{ node {{ {selection} }} }} }} }} }} }} }}""")
        self.assertNotIn("errors", content)
        return [edge["node"] for edge in content["data"]["taskGroup"]["edges"][0]["node"]["taskSet"]["edges"]]

    def test_task_fields_are_masked_without_the_masked_data_right(self):
        node, = self._nodes(self.reader_token, "task", "data jsonExt")
        self.assertEqual(json.loads(node["data"])["incoming_data"], {"first_name": MASK, "code": "C-1"})
        self.assertEqual(json.loads(node["jsonExt"]), {"secret": MASK, "open": "visible"})

    def test_entity_string_alone_is_masked(self):
        node, = self._nodes(self.reader_token, "task", "entityString")
        self.assertEqual(node["entityString"], MASK)

    def test_business_data_alone_is_built_from_the_masked_data(self):
        node, = self._nodes(self.reader_token, "task", "businessData")
        self.assertEqual(json.loads(node["businessData"])["incoming_data"]["first_name"], MASK)

    def test_nested_task_set_entity_string_is_masked(self):
        node, = self._task_set_nodes(self.reader_token, "entityString")
        self.assertEqual(node["entityString"], MASK)

    def test_nested_task_set_business_data_is_masked(self):
        node, = self._task_set_nodes(self.reader_token, "businessData")
        self.assertEqual(json.loads(node["businessData"])["incoming_data"]["first_name"], MASK)

    def test_nested_task_set_json_ext_is_masked(self):
        node, = self._task_set_nodes(self.reader_token, "jsonExt")
        self.assertEqual(json.loads(node["jsonExt"])["secret"], MASK)

    def test_task_history_is_masked(self):
        nodes = self._nodes(self.reader_token, "taskHistory", "entityString")
        self.assertTrue(nodes)
        self.assertEqual({node["entityString"] for node in nodes}, {MASK})
        nodes = self._nodes(self.reader_token, "taskHistory", "businessData")
        self.assertEqual({json.loads(node["businessData"])["incoming_data"]["first_name"] for node in nodes}, {MASK})
        nodes = self._nodes(self.reader_token, "taskHistory", "data")
        self.assertEqual({json.loads(node["data"])["incoming_data"]["first_name"] for node in nodes}, {MASK})

    def test_a_reader_with_the_masked_data_right_reads_the_values(self):
        node, = self._nodes(self.unmasked_token, "task", "data jsonExt entityString businessData")
        self.assertEqual(json.loads(node["data"])["incoming_data"]["first_name"], "Ninette")
        self.assertEqual(json.loads(node["jsonExt"])["secret"], "S-42")
        self.assertEqual(node["entityString"], str(self.entity))
        self.assertEqual(json.loads(node["businessData"])["incoming_data"]["first_name"], "Ninette")
        node, = self._task_set_nodes(self.unmasked_token, "entityString")
        self.assertEqual(node["entityString"], str(self.entity))

    def test_entity_string_filter_is_refused_without_the_masked_data_right(self):
        for field in ("task", "taskHistory"):
            content = self._query(self.reader_token, f"""
                {{ {field}(source: "{SOURCE}", entityString_Icontains: "{str(self.entity)[:3]}") {{
                    edges {{ node {{ id }} }} }} }}""")
            self.assertIn("errors", content)
            self.assertIn("view masked data", content["errors"][0]["message"])

    def test_entity_string_filter_is_applied_with_the_masked_data_right(self):
        arguments = f'source: "{SOURCE}", entityString_Icontains: "{str(self.entity)}"'
        self.assertEqual(len(self._nodes(self.unmasked_token, "task", "id", arguments)), 1)

    def _ordered_names(self, token, field, order):
        nodes = self._nodes(token, field, "data", f'source: "mask_order_source", orderBy: ["{order}"]')
        return [json.loads(node["data"])["incoming_data"]["first_name"] for node in nodes]

    def test_an_ordering_on_masked_task_data_is_left_out_without_the_masked_data_right(self):
        tasks = []
        for _ in range(2):
            task = Task(
                source="mask_order_source", status=Task.Status.ACCEPTED, business_event="mask_task_event",
                executor_action_event=TasksManagementConfig.default_executor_event, business_status={},
                task_group=self.group, data={"incoming_data": {"first_name": ""}}, json_ext={"secret": ""})
            task.save(username=self.reader.username)
            tasks.append(task)
        by_id = sorted(tasks, key=lambda task: str(task.id))
        for task, name in zip(by_id, ("Zelda", "Abel")):
            task.data = {"incoming_data": {"first_name": name}}
            task.json_ext = {"secret": name}
            task.save(username=self.reader.username)
        for field in ("task", "taskHistory"):
            for order in ("data__incoming_data__first_name", "json_ext__secret"):
                with self.subTest(field=field, order=order):
                    self.assertEqual(self._ordered_names(self.unmasked_token, field, order)[-1], "Zelda")
        for order in ("data__incoming_data__first_name", "json_ext__secret"):
            with self.subTest(order=order):
                self.assertEqual(self._ordered_names(self.reader_token, "task", order), [MASK, MASK])
                nodes = self._nodes(self.reader_token, "task", "id",
                                    f'source: "mask_order_source", orderBy: ["{order}"]')
                self.assertEqual([node["id"] for node in nodes],
                                 [self._global_id(task) for task in by_id])
                rows = Task.history.filter(id__in=[task.id for task in by_id]).order_by("history_id")
                nodes = self._nodes(self.reader_token, "taskHistory", "id",
                                    f'source: "mask_order_source", orderBy: ["{order}"]')
                self.assertEqual([node["id"] for node in nodes],
                                 [relay_id("TaskHistoryGQLType", row.history_id) for row in rows])

    def _global_id(self, task):
        return relay_id("TaskGQLType", task.id)
