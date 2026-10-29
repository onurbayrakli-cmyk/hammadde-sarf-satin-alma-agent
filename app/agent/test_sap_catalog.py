"""Synthetic metadata inventory tests; no SAP, network, model or mailbox reads."""

import hashlib
import json
import unittest
from unittest.mock import patch

from sap_metadata import MetadataError, catalog_metadata

V4 = "http://docs.oasis-open.org/odata/ns/edm"
V2 = "http://schemas.microsoft.com/ado/2008/09/edm"
SAP = "http://www.sap.com/Protocols/SAPData"


def document(*schemas, v2=False, reference=""):
    edm = V2 if v2 else V4
    edmx = "http://schemas.microsoft.com/ado/2007/06/edmx" if v2 else "http://docs.oasis-open.org/odata/ns/edmx"
    body = "".join(schemas) if schemas else schema()
    return (
        f'<edmx:Edmx xmlns:edmx="{edmx}" xmlns:sap="{SAP}" Version="1.0">'
        f'{reference}<edmx:DataServices xmlns="{edm}">{body}</edmx:DataServices></edmx:Edmx>'
    ).encode()


def schema(*, namespace="Model", alias="M", types=None, containers=None):
    if types is None:
        types = item_type()
    if containers is None:
        containers = '<EntityContainer Name="Container"><EntitySet Name="PurchaseRequisitionItems" EntityType="M.Item"/></EntityContainer>'
    alias_attr = f' Alias="{alias}"' if alias is not None else ""
    return f'<Schema Namespace="{namespace}"{alias_attr}>{types}{containers}</Schema>'


def item_type():
    return (
        '<EntityType Name="Item"><Key><PropertyRef Name="PurchaseRequisition"/>'
        '<PropertyRef Name="PurchaseRequisitionItem"/></Key>'
        '<Property Name="PurchaseRequisition" Type="Edm.String" Nullable="false"/>'
        '<Property Name="PurchaseRequisitionItem" Type="Edm.String" Nullable="false"/>'
        '<Property Name="Material" Type="Edm.String"/>'
        '<Property Name="Quantity" Type="Edm.Decimal" Nullable="true"/>'
        '<Property Name="ReleaseStatus" Type="Edm.String" Nullable="false"/>'
        "</EntityType>"
    )


def record(raw):
    result = catalog_metadata(raw)
    return result, result["entity_sets"][0]


class SapMetadataCatalogTests(unittest.TestCase):
    def test_v2_and_v4_alias_resolve_actual_set_container_and_type(self):
        for v2 in (False, True):
            with self.subTest(v2=v2):
                raw = document(v2=v2)
                result, entry = record(raw)
                self.assertEqual(result["metadata_sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(result["metadata_bytes"], len(raw))
                self.assertTrue(result["metadata_verified"])
                self.assertFalse(result["business_semantics_verified"])
                self.assertEqual(entry["name"], "PurchaseRequisitionItems")
                self.assertEqual(entry["container"], "Container")
                self.assertEqual(entry["namespace"], "Model")
                self.assertEqual(entry["qualified_entity_type"], "Model.Item")
                self.assertIsNone(entry["base_type"])
                self.assertIsNone(entry["semantics"])
                self.assertNotIn("contract_compatible", result)

    def test_container_namespace_and_model_namespace_are_independent(self):
        raw = document(
            schema(containers=""),
            schema(
                namespace="Service",
                alias="S",
                types="",
                containers='<EntityContainer Name="API"><EntitySet Name="Items" EntityType="M.Item"/></EntityContainer>',
            ),
        )
        _, entry = record(raw)
        self.assertEqual(entry["namespace"], "Service")
        self.assertEqual(entry["container"], "API")
        self.assertEqual(entry["qualified_entity_type"], "Model.Item")

    def test_string_status_and_absent_email_are_inventory_only(self):
        _, entry = record(document())
        properties = {prop["name"]: prop for prop in entry["properties"]}
        self.assertEqual(properties["ReleaseStatus"]["type"], "Edm.String")
        self.assertFalse(properties["ReleaseStatus"]["nullable"])
        self.assertTrue(properties["Quantity"]["nullable"])
        self.assertIsNone(properties["Material"]["nullable"])
        self.assertNotIn("RequesterEmail", properties)
        self.assertEqual(entry["keys"], ["PurchaseRequisition", "PurchaseRequisitionItem"])
        self.assertNotIn("released", entry)

    def test_aggregate_id_and_parameter_keys_are_not_business_identity_claims(self):
        types = (
            '<EntityType Name="Query" sap:semantics="aggregate"><Key><PropertyRef Name="ID"/></Key>'
            '<Property Name="ID" Type="Edm.String" Nullable="false"/>'
            '<Property Name="Total" Type="Edm.Decimal"/></EntityType>'
            '<EntityType Name="Params" sap:semantics="parameters"><Key><PropertyRef Name="Plant"/></Key>'
            '<Property Name="Plant" Type="Edm.String" Nullable="false"/></EntityType>'
        )
        containers = (
            '<EntityContainer Name="Analytics"><EntitySet Name="Results" EntityType="M.Query"/>'
            '<EntitySet Name="Parameters" EntityType="M.Params"/></EntityContainer>'
        )
        result = catalog_metadata(document(schema(types=types, containers=containers), v2=True))
        aggregate, parameters = result["entity_sets"]
        self.assertEqual(aggregate["semantics"], "aggregate")
        self.assertEqual(aggregate["keys"], ["ID"])
        self.assertIn("aggregate_key_not_business_identity", aggregate["warnings"])
        self.assertEqual(parameters["semantics"], "parameters")
        self.assertEqual(parameters["keys"], ["Plant"])
        self.assertIn("parameter_entity", parameters["warnings"])
        self.assertFalse(result["business_semantics_verified"])

    def test_unknown_semantics_labels_annotations_and_documentation_are_not_copied(self):
        raw = document(
            schema(
                types=item_type().replace(
                    '<EntityType Name="Item">',
                    '<EntityType Name="Item" sap:semantics="arbitrary-private-label" sap:label="private-label">'
                    "<Documentation><Summary>private-summary</Summary></Documentation>"
                    '<Annotation Term="Some.Documentation" String="https://private.example.invalid/label"/>',
                )
            )
        )
        result, entry = record(raw)
        self.assertEqual(entry["semantics"], "other")
        self.assertEqual(entry["warnings"], ["unknown_semantics"])
        rendered = json.dumps(result)
        for value in ("arbitrary-private-label", "private-label", "private-summary", "https://", "Some.Documentation"):
            self.assertNotIn(value, rendered)

    def test_derived_entity_keeps_direct_properties_and_does_not_expand_base_keys(self):
        derived = '<EntityType Name="Derived" BaseType="M.Item"><Property Name="Extra" Type="Edm.String"/></EntityType>'
        raw = document(
            schema(
                types=item_type() + derived,
                containers='<EntityContainer Name="Container"><EntitySet Name="DerivedItems" EntityType="M.Derived"/></EntityContainer>',
            )
        )
        _, entry = record(raw)
        self.assertEqual(entry["base_type"], "Model.Item")
        self.assertEqual(entry["keys"], [])
        self.assertEqual(entry["properties"], [{"name": "Extra", "type": "Edm.String", "nullable": None}])
        self.assertIn("entity_inheritance", entry["warnings"])
        self.assertIn("direct_key_not_declared", entry["warnings"])

    def test_complex_enum_and_typedef_aliases_are_bounded_and_resolved(self):
        extras = (
            '<ComplexType Name="Address"><Property Name="City" Type="Edm.String"/></ComplexType>'
            '<EnumType Name="Kind"><Member Name="Raw" Value="0"/></EnumType>'
            '<TypeDefinition Name="Amount" UnderlyingType="Edm.Decimal"/>'
        )
        item = item_type().replace(
            "</EntityType>",
            '<Property Name="Places" Type="Collection(M.Address)"/>'
            '<Property Name="Kind" Type="M.Kind"/><Property Name="Amount" Type="M.Amount"/></EntityType>',
        )
        _, entry = record(document(schema(types=item + extras)))
        kinds = {prop["name"]: prop["type"] for prop in entry["properties"]}
        self.assertEqual(kinds["Places"], "Collection(Model.Address)")
        self.assertEqual(kinds["Kind"], "Model.Kind")
        self.assertEqual(kinds["Amount"], "Model.Amount")

    def test_duplicate_namespaces_aliases_and_alias_namespace_collisions_fail_closed(self):
        alternatives = [
            document(schema(), schema(alias="Other", containers="")),
            document(schema(), schema(namespace="Other", types="", containers="")),
            document(schema(), schema(namespace="M", alias="Other", types="", containers="")),
        ]
        for raw in alternatives:
            with self.assertRaises(MetadataError):
                catalog_metadata(raw)

    def test_duplicate_type_container_property_key_and_set_declarations_fail_closed(self):
        base = document()
        changes = [
            base.replace(b"</EntityType>", b'</EntityType><EntityType Name="Item"/>'),
            base.replace(b"</EntityContainer>", b'</EntityContainer><EntityContainer Name="Container"/>'),
            base.replace(b"</EntityType>", b'<Property Name="Material" Type="Edm.String"/></EntityType>'),
            base.replace(b"</Key>", b'<PropertyRef Name="PurchaseRequisition"/></Key>'),
            base.replace(b"</EntityType>", b'<Key><PropertyRef Name="Material"/></Key></EntityType>'),
            base.replace(
                b"</EntityContainer>",
                b'<EntitySet Name="PurchaseRequisitionItems" EntityType="M.Item"/></EntityContainer>',
            ),
            base.replace(b"</Key>", b'<PropertyRef Name="Missing"/></Key>'),
            base.replace(b"<Key>", b"<Key/>", 1),
        ]
        for raw in changes:
            with self.assertRaises(MetadataError):
                catalog_metadata(raw)

    def test_same_set_name_in_different_containers_is_ambiguous(self):
        raw = document(
            schema(
                containers='<EntityContainer Name="First"><EntitySet Name="Items" EntityType="M.Item"/></EntityContainer>'
                '<EntityContainer Name="Second"><EntitySet Name="Items" EntityType="M.Item"/></EntityContainer>'
            )
        )
        with self.assertRaises(MetadataError):
            catalog_metadata(raw)

    def test_unresolved_entity_type_property_type_and_base_references_are_rejected(self):
        base = document()
        for raw in (
            base.replace(b'EntityType="M.Item"', b'EntityType="Other.Item"'),
            base.replace(b'Type="Edm.Decimal"', b'Type="Unknown.Amount"'),
            base.replace(b'<EntityType Name="Item">', b'<EntityType Name="Item" BaseType="M.Absent">'),
            base.replace(b'EntityType="M.Item"', b'EntityType="Item"'),
        ):
            with self.assertRaises(MetadataError):
                catalog_metadata(raw)

    def test_inheritance_cycles_and_container_extensions_are_rejected(self):
        types = item_type().replace('<EntityType Name="Item">', '<EntityType Name="Item" BaseType="M.Other">')
        types += '<EntityType Name="Other" BaseType="M.Item"/>'
        with self.assertRaises(MetadataError):
            catalog_metadata(document(schema(types=types)))
        with self.assertRaises(MetadataError):
            catalog_metadata(
                document().replace(
                    b'<EntityContainer Name="Container">',
                    b'<EntityContainer Name="Container" Extends="M.BaseContainer">',
                )
            )

    def test_qualified_names_types_and_nullability_do_not_allow_arbitrary_text(self):
        base = document()
        for raw in (
            base.replace(b'Type="Edm.Decimal"', b'Type="Edm.MadeUp"'),
            base.replace(b'Type="Edm.Decimal"', b'Type="Collection(Collection(Edm.String))"'),
            base.replace(b'Nullable="true"', b'Nullable="FALSE"'),
            base.replace(b'Name="Material"', b'Name="Material/child"'),
            base.replace(b'Name="Material"', ('Name="' + "A" * 129 + '"').encode()),
            base.replace(b'Namespace="Model"', b'Namespace="https://private.example.invalid"'),
        ):
            with self.assertRaises(MetadataError):
                catalog_metadata(raw)

    def test_external_references_are_never_fetched_or_copied(self):
        raw = document(
            reference='<edmx:Reference Uri="https://private.example.invalid/schema">'
            '<edmx:Include Namespace="External" Alias="Ext"/></edmx:Reference>'
        )
        with patch("socket.getaddrinfo", side_effect=AssertionError("network forbidden")) as dns:
            result = catalog_metadata(raw)
        dns.assert_not_called()
        self.assertNotIn("private.example.invalid", json.dumps(result))
        with self.assertRaises(MetadataError):
            catalog_metadata(raw.replace(b'EntityType="M.Item"', b'EntityType="Ext.Item"'))

    def test_utf16_dtd_entities_nul_non_utf8_and_foreign_root_are_rejected(self):
        dangerous = '<!DOCTYPE root [<!ENTITY probe "Edm.String">]>' + document().decode().replace(
            'Type="Edm.String"', 'Type="&probe;"'
        )
        for raw in (
            dangerous.encode(),
            dangerous.encode("utf-16"),
            dangerous.encode("utf-16-le"),
            document().replace(b"Material", b"Mate\x00rial"),
            b"\xff" + document(),
            b"<root/>",
        ):
            with self.assertRaises(MetadataError):
                catalog_metadata(raw)
        result = catalog_metadata(b"\xef\xbb\xbf" + document())
        self.assertTrue(result["metadata_verified"])

    def test_xml_depth_element_count_and_byte_limits_are_enforced(self):
        base = document()
        deep = base.replace(b"</EntityType>", b"<Annotation>" * 35 + b"</Annotation>" * 35 + b"</EntityType>")
        many = base.replace(b"</EntityType>", b"<Annotation/>" * 20_001 + b"</EntityType>")
        for raw in (deep, many, base + b" " * 1_048_576):
            with self.assertRaises(MetadataError):
                catalog_metadata(raw)
        with self.assertRaises(MetadataError):
            catalog_metadata(base, max_bytes=len(base) - 1)
        self.assertTrue(catalog_metadata(base, max_bytes=len(base))["metadata_verified"])
        for limit in (0, True, "1024", 1_048_577):
            with self.assertRaises(MetadataError):
                catalog_metadata(base, max_bytes=limit)

    def test_shared_type_projection_cannot_amplify_a_small_metadata_document(self):
        fields = '<Property Name="ID" Type="Edm.String"/>'
        fields += "".join(f'<Property Name="Field{index}" Type="Edm.String"/>' for index in range(200))
        types = '<EntityType Name="Large"><Key><PropertyRef Name="ID"/></Key>' + fields + "</EntityType>"
        sets = "".join(f'<EntitySet Name="Items{index}" EntityType="M.Large"/>' for index in range(200))
        raw = document(
            schema(types=types, containers='<EntityContainer Name="Container">' + sets + "</EntityContainer>")
        )
        self.assertLess(len(raw), 50_000)
        with self.assertRaises(MetadataError):
            catalog_metadata(raw)
        smaller = raw.replace(
            sets.encode(),
            "".join(f'<EntitySet Name="Items{index}" EntityType="M.Large"/>' for index in range(10)).encode(),
        )
        self.assertEqual(len(catalog_metadata(smaller)["entity_sets"]), 10)

    def test_catalog_set_and_serialized_size_budgets_are_independent(self):
        small_type = '<EntityType Name="Simple"><Key><PropertyRef Name="ID"/></Key><Property Name="ID" Type="Edm.String"/></EntityType>'
        many_sets = "".join(f'<EntitySet Name="Items{index}" EntityType="M.Simple"/>' for index in range(1_001))
        raw = document(
            schema(types=small_type, containers='<EntityContainer Name="Container">' + many_sets + "</EntityContainer>")
        )
        self.assertLess(len(raw), 100_000)
        with self.assertRaises(MetadataError):
            catalog_metadata(raw)
        long_namespace = "Long" + "." + "N" * 230
        long_type = "T" * 125
        properties = '<Property Name="ID" Type="Edm.String"/>'
        properties += "".join(f'<Property Name="{"P" * 110}{index}" Type="L.{long_type}"/>' for index in range(200))
        types = (
            f'<ComplexType Name="{long_type}"><Property Name="Value" Type="Edm.String"/></ComplexType>'
            '<EntityType Name="Large"><Key><PropertyRef Name="ID"/></Key>' + properties + "</EntityType>"
        )
        sets = "".join(f'<EntitySet Name="Items{index}" EntityType="L.Large"/>' for index in range(50))
        raw = document(
            schema(
                namespace=long_namespace,
                alias="L",
                types=types,
                containers='<EntityContainer Name="Container">' + sets + "</EntityContainer>",
            )
        )
        self.assertLess(len(raw), 100_000)
        with self.assertRaises(MetadataError):
            catalog_metadata(raw)

    def test_catalog_top_level_and_entry_schema_are_fixed_and_empty_inventory_is_explicit(self):
        result, entry = record(document())
        self.assertEqual(
            set(result),
            {"metadata_sha256", "metadata_bytes", "metadata_verified", "business_semantics_verified", "entity_sets"},
        )
        self.assertEqual(
            set(entry),
            {
                "name",
                "container",
                "namespace",
                "qualified_entity_type",
                "semantics",
                "base_type",
                "keys",
                "properties",
                "warnings",
            },
        )
        self.assertTrue(all(set(prop) == {"name", "type", "nullable"} for prop in entry["properties"]))
        empty = catalog_metadata(document(schema(types="", containers="")))
        self.assertEqual(empty["entity_sets"], [])
        self.assertFalse(empty["business_semantics_verified"])


if __name__ == "__main__":
    unittest.main()
