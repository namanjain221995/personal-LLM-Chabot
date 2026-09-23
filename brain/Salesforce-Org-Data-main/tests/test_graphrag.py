import json
from pathlib import Path
import tempfile
import unittest

from graphrag.graph import build_graph, read_graph, write_graph
from graphrag.models import Edge, Graph, Node
from graphrag.obsidian import export_obsidian
from graphrag.queries import (
    dependencies,
    fields_for_object,
    find_nodes,
    required_fields,
    picklist_values,
    traverse_dependencies,
)
from graphrag.scanner import ScanError, classify_path


class GraphRagTests(unittest.TestCase):
    def test_exports_obsidian_notes_and_links(self):
        graph = Graph(
            nodes=(Node("object:Account", "object", "Account"),
                   Node("field:Account.Name", "field", "Account.Name")),
            edges=(Edge("object:Account", "field:Account.Name", "has_field"),),
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "vault"
            self.assertEqual(export_obsidian(graph, output), 2)
            note = (output / "object" / "Account.md").read_text(encoding="utf-8")
            self.assertIn("[[field/Account.Name]]", note)
            self.assertTrue((output / "README.md").is_file())

    def test_classifies_dx_paths(self):
        self.assertEqual(classify_path("classes/OrderService.cls-meta.xml"), ("classes", "apex_class"))
        self.assertEqual(classify_path("objects/Invoice__c/fields/Total__c.field-meta.xml")[1], "custom_object")

    def test_builds_apex_and_xml_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "default"
            (root / "classes").mkdir(parents=True)
            (root / "classes" / "Order.cls").write_text("public class Order { Account a; List<Contact> c; }", encoding="utf-8")
            (root / "classes" / "Order.cls-meta.xml").write_text("<ApexClass><apiVersion>60.0</apiVersion><status>Active</status></ApexClass>", encoding="utf-8")
            graph = build_graph(root)
            self.assertTrue(any(node.id == "file:classes/Order.cls" for node in graph.nodes))
            self.assertTrue(any(node.id == "object:Account" for node in graph.nodes))
            self.assertTrue(graph.edges[0].evidence[0].source_path.startswith("classes/"))

    def test_writes_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "default"
            (root / "classes").mkdir(parents=True)
            (root / "classes" / "A.cls").write_text("class A {}", encoding="utf-8")
            output = Path(directory) / "graph.jsonl"
            write_graph(build_graph(root), output)
            records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(records[0]["type"], "node")
            loaded = read_graph(output)
            self.assertEqual(loaded.nodes[0].id, "file:classes/A.cls")

    def test_missing_source_is_explicit(self):
        with self.assertRaises(ScanError):
            build_graph(Path("missing"))

    def test_queries_find_and_reverse_dependencies(self):
        graph = Graph(
            nodes=(
                Node("file:A.cls", "apex_class", "A.cls"),
                Node("file:B.cls", "apex_class", "B.cls"),
                Node("object:Account", "object", "Account"),
            ),
            edges=(
                Edge("file:A.cls", "object:Account", "references"),
                Edge("file:B.cls", "file:A.cls", "references"),
            ),
        )
        self.assertEqual(find_nodes(graph, "object:Account")[0].id, "object:Account")
        self.assertEqual(dependencies(graph, "object:Account")[0].node.id, "file:A.cls")
        self.assertEqual(
            traverse_dependencies(graph, "object:Account", depth=2)[1].node.id,
            "file:B.cls",
        )

    def test_creates_object_field_relationships(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "default"
            field_dir = root / "objects" / "Interview__c" / "fields"
            field_dir.mkdir(parents=True)
            (field_dir / "Status__c.field-meta.xml").write_text(
                "<CustomField><fullName>Status__c</fullName><type>Picklist</type></CustomField>",
                encoding="utf-8",
            )
            graph = build_graph(root)
            fields = fields_for_object(graph, "Interview__c")
            self.assertEqual([field.id for field in fields], ["field:Interview__c.Status__c"])
            self.assertTrue(any(edge.kind == "has_field" for edge in graph.edges))
            (field_dir / "Required__c.field-meta.xml").write_text(
                "<CustomField><fullName>Required__c</fullName><required>true</required>"
                "<label>Required</label><type>Text</type></CustomField>",
                encoding="utf-8",
            )
            graph = build_graph(root)
            required = required_fields(graph, "Interview__c")
            self.assertEqual([field.name for field in required], ["Interview__c.Required__c"])

    def test_extracts_picklist_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "default"
            field_dir = root / "objects" / "Account" / "fields"
            field_dir.mkdir(parents=True)
            (field_dir / "Status__c.field-meta.xml").write_text(
                """<CustomField><fullName>Status__c</fullName><type>Picklist</type>
                <valueSet><valueSetDefinition>
                  <value><fullName>Active</fullName><default>true</default><isActive>true</isActive></value>
                  <value><fullName>Inactive</fullName><default>false</default><isActive>true</isActive></value>
                </valueSetDefinition></valueSet></CustomField>""",
                encoding="utf-8",
            )
            graph = build_graph(root)
            values = picklist_values(graph, "Account.Status__c")
            self.assertEqual([value.name for value in values], ["Active", "Inactive"])
            self.assertTrue(values[0].properties["default"])
            self.assertTrue(any(edge.kind == "has_value" for edge in graph.edges))

    def test_creates_lookup_relationship(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "default"
            field_dir = root / "objects" / "Interview__c" / "fields"
            field_dir.mkdir(parents=True)
            (field_dir / "Candidate__c.field-meta.xml").write_text(
                "<CustomField><fullName>Candidate__c</fullName><type>Lookup</type>"
                "<referenceTo>Candidate__c</referenceTo>"
                "<relationshipName>Candidate__r</relationshipName></CustomField>",
                encoding="utf-8",
            )
            graph = build_graph(root)
            relationship = next(edge for edge in graph.edges if edge.kind == "lookup_to")
            self.assertEqual(relationship.source, "field:Interview__c.Candidate__c")
            self.assertEqual(relationship.target, "object:Candidate__c")
            self.assertEqual(relationship.evidence[0].source_path,
                             "objects/Interview__c/fields/Candidate__c.field-meta.xml")
            self.assertIn(
                ("object:Candidate__c", "object:Interview__c", "parent_of"),
                {(edge.source, edge.target, edge.kind) for edge in graph.edges},
            )
            self.assertIn(
                ("object:Interview__c", "object:Candidate__c", "child_of"),
                {(edge.source, edge.target, edge.kind) for edge in graph.edges},
            )

    def test_field_metadata_does_not_create_generic_reference_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "default"
            field_dir = root / "objects" / "Interview__c" / "fields"
            field_dir.mkdir(parents=True)
            (field_dir / "Candidate__c.field-meta.xml").write_text(
                "<CustomField><fullName>Candidate__c</fullName><type>MasterDetail</type>"
                "<referenceTo>Account</referenceTo></CustomField>",
                encoding="utf-8",
            )
            graph = build_graph(root)
            self.assertFalse(any(
                node.id == "xml_reference:Candidate__c" for node in graph.nodes
            ))
            self.assertTrue(any(
                node.id == "field:Interview__c.Candidate__c" for node in graph.nodes
            ))

    def test_known_field_name_is_not_emitted_as_xml_object_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "default"
            field_dir = root / "objects" / "Interview__c" / "fields"
            field_dir.mkdir(parents=True)
            (field_dir / "Candidate__c.field-meta.xml").write_text(
                "<CustomField><fullName>Candidate__c</fullName><type>Text</type></CustomField>",
                encoding="utf-8",
            )
            (root / "layouts").mkdir()
            (root / "layouts" / "Interview.layout-meta.xml").write_text(
                "<Layout><fields><name>Candidate__c</name></fields></Layout>",
                encoding="utf-8",
            )
            graph = build_graph(root)
            self.assertFalse(any(
                node.id == "xml_reference:Candidate__c" for node in graph.nodes
            ))

    def test_extracts_metadata_relationships_with_xml_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "default"
            for folder in ("objects/Account/validationRules", "permissionsets",
                           "permissionSetGroups", "queues", "reports/Public",
                           "dashboards/Public"):
                (root / folder).mkdir(parents=True)
            (root / "objects/Account/validationRules/Check.validationRule-meta.xml").write_text(
                "<ValidationRule><errorConditionFormula>ISNEW() &amp;&amp; Amount__c &gt; 0</errorConditionFormula>"
                "<errorMessage>Bad amount</errorMessage></ValidationRule>", encoding="utf-8")
            (root / "objects/Account/fields/Amount__c.field-meta.xml").parent.mkdir(parents=True)
            (root / "objects/Account/fields/Amount__c.field-meta.xml").write_text(
                "<CustomField><fullName>Amount__c</fullName><type>Currency</type></CustomField>",
                encoding="utf-8")
            (root / "permissionsets/Sales.permissionset-meta.xml").write_text(
                "<PermissionSet><objectPermissions><object>Account</object></objectPermissions>"
                "<fieldPermissions><field>Account.Amount__c</field></fieldPermissions></PermissionSet>",
                encoding="utf-8")
            (root / "permissionSetGroups/Sales.permissionsetgroup-meta.xml").write_text(
                "<PermissionSetGroup><permissionSets><permissionSet>Sales</permissionSet></permissionSets>"
                "</PermissionSetGroup>", encoding="utf-8")
            (root / "queues/Support.queue-meta.xml").write_text(
                "<Queue><queueSobjects><queueSobject><object>Case</object></queueSobject></queueSobjects></Queue>",
                encoding="utf-8")
            (root / "reports/Public/Sales.report-meta.xml").write_text(
                "<Report><reportMetadata><objects><objectName>Account</objectName></objects>"
                "<detailColumns><field>Account.Name</field></detailColumns>"
                "<filters><filter><column>Account.Amount__c</column></filter></filters></reportMetadata></Report>",
                encoding="utf-8")
            (root / "dashboards/Public/Overview.dashboard-meta.xml").write_text(
                "<Dashboard><dashboardComponents><dashboardComponent><reportName>Public/Sales</reportName>"
                "</dashboardComponent></dashboardComponents></Dashboard>", encoding="utf-8")
            graph = build_graph(root)
            edge_keys = {(edge.source, edge.target, edge.kind) for edge in graph.edges}
            self.assertIn(("validation_rule:Account.Check", "operation:Create", "blocks_operation"), edge_keys)
            self.assertIn(("validation_rule:Account.Check", "field:Account.Amount__c", "references_field"), edge_keys)
            self.assertIn(("permission_set:Sales", "object:Account", "object_permission"), edge_keys)
            self.assertIn(("permission_set_group:Sales", "permission_set:Sales", "contains"), edge_keys)
            self.assertIn(("queue:Support", "object:Case", "supports"), edge_keys)
            self.assertIn(("report:Sales", "folder:Public", "stored_in"), edge_keys)
            self.assertTrue(any(edge.kind == "uses_filter" for edge in graph.edges))

    def test_creates_structural_object_ownership_edges(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "default"
            object_root = root / "objects" / "Account"
            for folder, filename, content in (
                ("recordTypes", "Customer.recordType-meta.xml", "<RecordType/>"),
                ("validationRules", "Required.validationRule-meta.xml", "<ValidationRule/>"),
                ("duplicateRules", "Duplicate.duplicateRule-meta.xml", "<DuplicateRule/>"),
                ("compactLayouts", "Compact.compactLayout-meta.xml", "<CompactLayout/>"),
                ("searchLayouts", "Search.searchLayouts-meta.xml", "<SearchLayouts/>"),
                ("businessProcesses", "Sales.businessProcess-meta.xml", "<BusinessProcess/>"),
                ("sharingRules", "Internal.sharingRules-meta.xml", "<SharingRules/>"),
                ("indexes", "Account.index-meta.xml", "<Index/>"),
            ):
                folder_path = object_root / folder
                folder_path.mkdir(parents=True, exist_ok=True)
                (folder_path / filename).write_text(content, encoding="utf-8")
            graph = build_graph(root)
            expected = {
                "has_record_type", "has_validation_rule", "has_duplicate_rule",
                "has_compact_layout", "has_search_layout", "has_business_process",
                "has_sharing_rule", "has_index",
            }
            self.assertEqual(
                {edge.kind for edge in graph.edges if edge.source == "object:Account"},
                expected,
            )

    def test_creates_flow_relationships(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "default"
            field_dir = root / "objects" / "Account" / "fields"
            field_dir.mkdir(parents=True)
            (field_dir / "Status__c.field-meta.xml").write_text(
                "<CustomField><fullName>Status__c</fullName><type>Text</type></CustomField>",
                encoding="utf-8",
            )
            flow_dir = root / "flows"
            flow_dir.mkdir()
            (flow_dir / "Account_Flow.flow-meta.xml").write_text(
                """<Flow>
                    <start><object>Account</object><recordTriggerType>CreateAndUpdate</recordTriggerType></start>
                    <assignments><name>Set_Status</name><assignmentItems>
                        <assignToReference>$Record.Status__c</assignToReference>
                    </assignmentItems></assignments>
                    <recordCreates><name>Create_Account</name><object>Account</object>
                        <inputAssignments><field>Status__c</field></inputAssignments>
                    </recordCreates>
                    <recordLookups><name>Get_Account</name><object>Account</object></recordLookups>
                    <decisions><name>Check_Status</name></decisions>
                    <assignmentItems><inputReference>$Record.Status__c</inputReference></assignmentItems>
                    <actionCalls><name>Notify</name><actionType>customNotification</actionType></actionCalls>
                    <subflows><name>Child</name><flowName>Child_Flow</flowName></subflows>
                </Flow>""",
                encoding="utf-8",
            )
            graph = build_graph(root)
            edge_triples = {(edge.source, edge.target, edge.kind) for edge in graph.edges}
            self.assertIn(("flow:Account_Flow", "object:Account", "triggers_on"), edge_triples)
            self.assertIn(("flow:Account_Flow", "event:CreateAndUpdate", "triggers_on_event"), edge_triples)
            self.assertIn(("flow:Account_Flow", "field:Account.Status__c", "writes_field"), edge_triples)
            self.assertIn(("flow:Account_Flow", "field:Account.Status__c", "reads_field"), edge_triples)
            self.assertIn(("flow:Account_Flow", "object:Account", "creates"), edge_triples)
            self.assertIn(("flow:Account_Flow", "custom_notification:Notify", "uses"), edge_triples)
            self.assertIn(("flow:Account_Flow", "subflow:Child_Flow", "calls_subflow"), edge_triples)
            self.assertTrue(any(edge.kind == "has_element" for edge in graph.edges))
