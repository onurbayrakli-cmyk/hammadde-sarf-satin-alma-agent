"""Bounded OData schema checks for the purchasing-requisition read pilot."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from xml.etree import ElementTree

EDMX_NAMESPACES = {
    "http://docs.oasis-open.org/odata/ns/edmx",
    "http://schemas.microsoft.com/ado/2007/06/edmx",
    "http://schemas.microsoft.com/ado/2009/11/edmx",
}
EDM_NAMESPACES = {
    "http://docs.oasis-open.org/odata/ns/edm",
    "http://schemas.microsoft.com/ado/2006/04/edm",
    "http://schemas.microsoft.com/ado/2008/09/edm",
    "http://schemas.microsoft.com/ado/2009/11/edm",
}
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
QUALIFIED = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")
NUMBER_TYPES = {"Edm.Decimal", "Edm.Byte", "Edm.SByte", "Edm.Int16", "Edm.Int32", "Edm.Int64"}
FIELD_TYPES = {
    "purchase_requisition": {"Edm.String"},
    "purchase_requisition_item": {"Edm.String"},
    "material_code": {"Edm.String"},
    "name": {"Edm.String"},
    "quantity": NUMBER_TYPES,
    "ordered_quantity": NUMBER_TYPES,
    "unit": {"Edm.String"},
    "requester_email": {"Edm.String"},
    "deleted": {"Edm.Boolean"},
    "closed": {"Edm.Boolean"},
    "blocked": {"Edm.Boolean"},
    "released": {"Edm.Boolean"},
}


class MetadataError(ValueError):
    """Schema failure without source XML, endpoint or credential details."""


def _fail():
    raise MetadataError("SAP metadata yapısı bu satın alma sözleşmesi için doğrulanamadı") from None


def _tag(element):
    if not isinstance(element.tag, str) or not element.tag.startswith("{"):
        return "", ""
    namespace, _, name = element.tag[1:].partition("}")
    return namespace, name


def _edm_children(element, kind):
    return [child for child in element if _tag(child)[0] in EDM_NAMESPACES and _tag(child)[1] == kind]


def _metadata_schemas(raw, max_bytes):
    """Parse one bounded, self-contained UTF-8 metadata document without DTDs."""
    if (
        not isinstance(raw, bytes)
        or not raw
        or type(max_bytes) is not int
        or not 1 <= max_bytes <= 10_000_000
        or len(raw) > max_bytes
    ):
        _fail()
    # Decode before the DTD guard: byte matching alone misses UTF-16 entity
    # declarations that ElementTree otherwise recognizes and expands.
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        _fail()
    declaration = re.search(r"<\?xml\b[^?]*\bencoding\s*=\s*['\"]([^'\"]+)['\"]", decoded, flags=re.IGNORECASE)
    if (
        "\x00" in decoded
        or declaration
        and declaration.group(1).lower() not in {"utf-8", "utf8", "us-ascii"}
        or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", decoded, flags=re.IGNORECASE)
    ):
        _fail()
    try:
        root = ElementTree.fromstring(decoded)
    except (ElementTree.ParseError, ValueError, RecursionError):
        _fail()
    if _tag(root)[0] not in EDMX_NAMESPACES or _tag(root)[1] != "Edmx":
        _fail()
    stack, count = [(root, 0)], 0
    while stack:
        element, depth = stack.pop()
        count += 1
        if count > 20_000 or depth > 32:
            _fail()
        stack.extend((child, depth + 1) for child in element)
    data_services = [child for child in root if _tag(child)[0] in EDMX_NAMESPACES and _tag(child)[1] == "DataServices"]
    if len(data_services) != 1:
        _fail()
    schemas = _edm_children(data_services[0], "Schema")
    if not schemas:
        _fail()
    return schemas


def inspect_metadata(raw, entity_set, field_map, *, max_bytes=1_048_576):
    """Check one explicit entity set, its keys and directly declared PR fields.

    The metadata proves names and EDM types only. Organization-specific status
    semantics and authorization still require the SAP owner's acceptance.
    Inherited entity types are deliberately unsupported in this first pilot.
    """
    if (
        not isinstance(entity_set, str)
        or not IDENTIFIER.fullmatch(entity_set)
        or not isinstance(field_map, Mapping)
        or set(field_map) != set(FIELD_TYPES)
    ):
        _fail()
    schemas = _metadata_schemas(raw, max_bytes)
    namespaces, aliases, types, entity_sets = set(), {}, {}, []
    for schema in schemas:
        namespace, alias = schema.get("Namespace"), schema.get("Alias")
        if not namespace or len(namespace) > 256 or not QUALIFIED.fullmatch(namespace) or namespace in namespaces:
            _fail()
        namespaces.add(namespace)
        if alias is not None:
            if not IDENTIFIER.fullmatch(alias) or alias in aliases:
                _fail()
            aliases[alias] = namespace
        for entity_type in _edm_children(schema, "EntityType"):
            name = entity_type.get("Name")
            if not name or not IDENTIFIER.fullmatch(name) or namespace + "." + name in types:
                _fail()
            types[namespace + "." + name] = entity_type
        for container in _edm_children(schema, "EntityContainer"):
            entity_sets.extend(
                element for element in _edm_children(container, "EntitySet") if element.get("Name") == entity_set
            )
    if any(alias in namespaces and alias != namespace for alias, namespace in aliases.items()):
        _fail()
    if len(entity_sets) != 1:
        _fail()
    qualified_type = entity_sets[0].get("EntityType")
    if (
        not qualified_type
        or len(qualified_type) > 400
        or not QUALIFIED.fullmatch(qualified_type)
        or "." not in qualified_type
    ):
        _fail()
    namespace, type_name = qualified_type.rsplit(".", 1)
    qualified_type = aliases.get(namespace, namespace) + "." + type_name
    if qualified_type not in types:
        _fail()
    entity_type = types[qualified_type]
    result = {
        "metadata_verified": True,
        "contract_compatible": False,
        "metadata_sha256": hashlib.sha256(raw).hexdigest(),
        "entity_set": entity_set,
        "missing_mappings": [],
        "missing_fields": [],
        "type_mismatches": [],
        "key_fields_verified": False,
        "business_semantics_verified": False,
    }
    if entity_type.get("BaseType") is not None:
        return {**result, "unsupported_schema": "entity_inheritance"}
    properties = {}
    for prop in _edm_children(entity_type, "Property"):
        name, kind = prop.get("Name"), prop.get("Type")
        if not name or not IDENTIFIER.fullmatch(name) or name in properties or not isinstance(kind, str):
            _fail()
        properties[name] = kind
    key_elements = _edm_children(entity_type, "Key")
    if len(key_elements) != 1:
        _fail()
    keys = [prop.get("Name") for prop in _edm_children(key_elements[0], "PropertyRef")]
    if (
        not keys
        or any(not name or not IDENTIFIER.fullmatch(name) or name not in properties for name in keys)
        or len(keys) != len(set(keys))
    ):
        _fail()
    mapped_names = []
    for internal, accepted_types in FIELD_TYPES.items():
        source = field_map[internal]
        if source is None:
            result["missing_mappings"].append(internal)
            continue
        if not isinstance(source, str) or not IDENTIFIER.fullmatch(source):
            _fail()
        mapped_names.append(source)
        if source not in properties:
            result["missing_fields"].append(internal)
        elif properties[source] not in accepted_types:
            result["type_mismatches"].append(internal)
    key_fields = [field_map[name] for name in ("purchase_requisition", "purchase_requisition_item")]
    result["key_fields_verified"] = len(set(key_fields)) == 2 and len(keys) == 2 and set(key_fields) == set(keys)
    if len(mapped_names) != len(set(mapped_names)):
        return {**result, "unsupported_schema": "overlapping_field_mapping"}
    result["contract_compatible"] = (
        not result["missing_mappings"]
        and not result["missing_fields"]
        and not result["type_mismatches"]
        and result["key_fields_verified"]
    )
    return result


SAP_SEMANTICS = "{http://www.sap.com/Protocols/SAPData}semantics"
CATALOG_MAX_BYTES = 1_048_576
CATALOG_MAX_SETS = 1_000
CATALOG_MAX_PROJECTED_FIELDS = 20_000
CATALOG_MAX_OUTPUT_BYTES = 4_194_304
EDM_PRIMITIVES = {
    "Binary",
    "Boolean",
    "Byte",
    "Date",
    "DateTime",
    "DateTimeOffset",
    "Decimal",
    "Double",
    "Duration",
    "Guid",
    "Int16",
    "Int32",
    "Int64",
    "SByte",
    "Single",
    "Stream",
    "String",
    "Time",
    "TimeOfDay",
    "Untyped",
    "Geography",
    "GeographyPoint",
    "GeographyLineString",
    "GeographyPolygon",
    "GeographyMultiPoint",
    "GeographyMultiLineString",
    "GeographyMultiPolygon",
    "GeographyCollection",
    "Geometry",
    "GeometryPoint",
    "GeometryLineString",
    "GeometryPolygon",
    "GeometryMultiPoint",
    "GeometryMultiLineString",
    "GeometryMultiPolygon",
    "GeometryCollection",
}


def _catalog_name(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        _fail()
    return value


def _catalog_qualified(value, aliases):
    if not isinstance(value, str) or len(value) > 400 or not QUALIFIED.fullmatch(value) or "." not in value:
        _fail()
    namespace, name = value.rsplit(".", 1)
    return aliases.get(namespace, namespace) + "." + name


def _catalog_property_type(value, aliases, declared):
    if not isinstance(value, str) or len(value) > 512:
        _fail()
    collection = value.startswith("Collection(") and value.endswith(")")
    scalar = value[11:-1] if collection else value
    qualified = _catalog_qualified(scalar, aliases)
    if qualified.startswith("Edm."):
        if qualified[4:] not in EDM_PRIMITIVES:
            _fail()
    elif qualified not in declared or declared[qualified][0] not in {"ComplexType", "EnumType", "TypeDefinition"}:
        _fail()
    return "Collection(" + qualified + ")" if collection else qualified


def _catalog_direct_fields(entity, aliases, declared):
    properties, names = [], set()
    for child in _edm_children(entity, "Property"):
        name = _catalog_name(child.get("Name"))
        if name in names:
            _fail()
        names.add(name)
        raw_nullable = child.get("Nullable")
        if raw_nullable is not None and raw_nullable not in {"true", "false", "1", "0"}:
            _fail()
        properties.append(
            {
                "name": name,
                "type": _catalog_property_type(child.get("Type"), aliases, declared),
                "nullable": None if raw_nullable is None else raw_nullable in {"true", "1"},
            }
        )
    key_elements = _edm_children(entity, "Key")
    if len(key_elements) > 1:
        _fail()
    keys = []
    if key_elements:
        keys = [_catalog_name(child.get("Name")) for child in _edm_children(key_elements[0], "PropertyRef")]
        if not keys or len(keys) != len(set(keys)) or any(key not in names for key in keys):
            _fail()
    return properties, keys


def _catalog_semantics(entity, entity_set):
    values = {element.get(SAP_SEMANTICS) for element in (entity, entity_set) if element.get(SAP_SEMANTICS) is not None}
    if len(values) > 1:
        _fail()
    if not values:
        return None, []
    value = next(iter(values))
    if value == "aggregate":
        return value, ["aggregate_entity", "aggregate_key_not_business_identity"]
    if value == "parameters":
        return value, ["parameter_entity"]
    return "other", ["unknown_semantics"]


def catalog_metadata(raw, *, max_bytes=CATALOG_MAX_BYTES):
    """Inventory direct schema facts without inferring procurement semantics.

    References are resolved only against declarations in this document. No
    annotations, labels, URLs, documentation text or external resources are
    copied to the catalog. Inherited fields are intentionally not expanded.
    """
    if type(max_bytes) is not int or not 1 <= max_bytes <= CATALOG_MAX_BYTES:
        _fail()
    schemas = _metadata_schemas(raw, max_bytes)
    namespaces, aliases, declared, containers = set(), {}, {}, {}
    for schema in schemas:
        namespace, alias = schema.get("Namespace"), schema.get("Alias")
        if (
            not namespace
            or len(namespace) > 256
            or not QUALIFIED.fullmatch(namespace)
            or namespace == "Edm"
            or namespace in namespaces
        ):
            _fail()
        namespaces.add(namespace)
        if alias is not None:
            alias = _catalog_name(alias)
            if alias == "Edm" or alias in aliases:
                _fail()
            aliases[alias] = namespace
        for element in schema:
            kind = _tag(element)[1]
            if _tag(element)[0] not in EDM_NAMESPACES or kind not in {
                "EntityType",
                "ComplexType",
                "EnumType",
                "TypeDefinition",
                "EntityContainer",
            }:
                continue
            name = _catalog_name(element.get("Name"))
            qualified = namespace + "." + name
            if qualified in declared or qualified in containers:
                _fail()
            if kind == "EntityContainer":
                if element.get("Extends") is not None:
                    _fail()
                containers[qualified] = (namespace, name, element)
            else:
                declared[qualified] = (kind, element)
    if any(alias in namespaces and alias != namespace for alias, namespace in aliases.items()):
        _fail()
    fields, bases = {}, {}
    for qualified, (kind, entity) in declared.items():
        if kind in {"EntityType", "ComplexType"}:
            fields[qualified] = _catalog_direct_fields(entity, aliases, declared)
            raw_base = entity.get("BaseType")
            base = _catalog_qualified(raw_base, aliases) if raw_base is not None else None
            if base is not None and (base not in declared or declared[base][0] != kind):
                _fail()
            bases[qualified] = base
        elif kind == "TypeDefinition":
            underlying = _catalog_property_type(entity.get("UnderlyingType"), aliases, declared)
            if not underlying.startswith("Edm.") or underlying in {"Edm.Stream", "Edm.Untyped"}:
                _fail()
        elif kind == "EnumType":
            underlying = entity.get("UnderlyingType", "Edm.Int32")
            if underlying not in {"Edm.Byte", "Edm.SByte", "Edm.Int16", "Edm.Int32", "Edm.Int64"}:
                _fail()
            member_names = [_catalog_name(member.get("Name")) for member in _edm_children(entity, "Member")]
            if len(member_names) != len(set(member_names)):
                _fail()
    for qualified in bases:
        seen, current = set(), qualified
        while current is not None:
            if current in seen or len(seen) > 32:
                _fail()
            seen.add(current)
            current = bases[current]
    entity_sets, set_names = [], set()
    projected_fields, projected_bytes = 0, 512
    for _, (namespace, container_name, container) in containers.items():
        for entity_set in _edm_children(container, "EntitySet"):
            name = _catalog_name(entity_set.get("Name"))
            if name in set_names:
                _fail()
            set_names.add(name)
            if len(set_names) > CATALOG_MAX_SETS:
                _fail()
            qualified = _catalog_qualified(entity_set.get("EntityType"), aliases)
            if qualified not in declared or declared[qualified][0] != "EntityType":
                _fail()
            entity = declared[qualified][1]
            properties, keys = fields[qualified]
            # One shared type may be bound to many entity sets. XML node/byte
            # limits alone do not bound the duplicated catalog projection.
            projected_fields += 1 + len(properties) + len(keys)
            if projected_fields > CATALOG_MAX_PROJECTED_FIELDS:
                _fail()
            semantics, warnings = _catalog_semantics(entity, entity_set)
            if bases[qualified] is not None:
                warnings.append("entity_inheritance")
            if not keys:
                warnings.append("direct_key_not_declared")
            entry = {
                "name": name,
                "container": container_name,
                "namespace": namespace,
                "qualified_entity_type": qualified,
                "semantics": semantics,
                "base_type": bases[qualified],
                "keys": list(keys),
                "properties": [dict(prop) for prop in properties],
                "warnings": warnings,
            }
            projected_bytes += len(json.dumps(entry, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
            if projected_bytes > CATALOG_MAX_OUTPUT_BYTES:
                _fail()
            entity_sets.append(entry)
    return {
        "metadata_sha256": hashlib.sha256(raw).hexdigest(),
        "metadata_bytes": len(raw),
        "metadata_verified": True,
        "business_semantics_verified": False,
        "entity_sets": entity_sets,
    }
