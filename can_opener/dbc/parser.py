from __future__ import annotations

import re

from .types import (
    AttributeValue,
    DbcFile,
    RawDbc,
    RawDbcAttribute,
    RawDbcAttributeDefinition,
    RawDbcMessage,
    RawDbcSignal,
    RawDbcValueTable,
)

MESSAGE_RE = re.compile(r"^BO_\s+(\d+)\s+(\w+)\s*:\s*(\d+)\s+(\w+)")
SIGNAL_RE = re.compile(
    r'^SG_\s+(\w+)(?:\s+(?:M|m\d+M?))?\s*:\s*(\d+)\|(\d+)@([01])([+-])\s+'
    r'\(([-+.\deE]+),([-+.\deE]+)\)\s+\[([-+.\deE]+)\|([-+.\deE]+)\]\s+"([^"]*)"\s*(.*)$'
)
VALUE_TABLE_RE = re.compile(r"^VAL_\s+(\d+)\s+(\w+)\s+(.+);$")
ATTRIBUTE_DEFINITION_RE = re.compile(
    r'^BA_DEF_\s+(?:(SG_|BO_|BU_|EV_)\s+)?"([^"]+)"\s+(\w+)(?:\s+(.+))?;$'
)
ATTRIBUTE_RE = re.compile(r'^BA_\s+"([^"]+)"\s+(?:(SG_|BO_|BU_|EV_)\s+)?(.+);$')


class DbcParser:
    def parse(self, file: DbcFile) -> RawDbc:
        messages: list[RawDbcMessage] = []
        attribute_definitions: list[RawDbcAttributeDefinition] = []
        attributes: list[RawDbcAttribute] = []
        value_tables: list[RawDbcValueTable] = []
        current_message: RawDbcMessage | None = None

        for raw_line in file.content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("CM_", "BU_")):
                continue

            if match := MESSAGE_RE.match(line):
                current_message = RawDbcMessage(
                    id=int(match.group(1)),
                    name=match.group(2),
                    size=int(match.group(3)),
                    transmitter=match.group(4),
                )
                messages.append(current_message)
                continue

            if match := SIGNAL_RE.match(line):
                if current_message is None:
                    raise ValueError(f"DBC signal without preceding message in {file.name}: {line}")
                current_message.signals.append(_parse_signal(match, current_message.id))
                continue

            if match := VALUE_TABLE_RE.match(line):
                value_tables.append(_parse_value_table(match))
                continue

            if match := ATTRIBUTE_DEFINITION_RE.match(line):
                attribute_definitions.append(_parse_attribute_definition(match))
                continue

            if match := ATTRIBUTE_RE.match(line):
                attributes.append(_parse_attribute(match))

        return RawDbc(
            file_name=file.name,
            messages=messages,
            attribute_definitions=attribute_definitions,
            attributes=attributes,
            value_tables=value_tables,
        )


def parse_dbc(file: DbcFile) -> RawDbc:
    return DbcParser().parse(file)


def _parse_signal(match: re.Match[str], message_id: int) -> RawDbcSignal:
    return RawDbcSignal(
        name=match.group(1),
        message_id=message_id,
        start_bit=int(match.group(2)),
        length=int(match.group(3)),
        byte_order="little" if match.group(4) == "1" else "big",
        signed=match.group(5) == "-",
        scale=float(match.group(6)),
        offset=float(match.group(7)),
        minimum=float(match.group(8)),
        maximum=float(match.group(9)),
        unit=match.group(10),
        receivers=[receiver.strip() for receiver in match.group(11).split(",") if receiver.strip()],
    )


def _parse_value_table(match: re.Match[str]) -> RawDbcValueTable:
    values: dict[int, str] = {}
    for pair in re.finditer(r'(-?\d+)\s+"([^"]*)"', match.group(3)):
        values[int(pair.group(1))] = pair.group(2)
    return RawDbcValueTable(
        message_id=int(match.group(1)),
        signal_name=match.group(2),
        values=values,
    )


def _parse_attribute_definition(match: re.Match[str]) -> RawDbcAttributeDefinition:
    attr_type = _normalize_attribute_type(match.group(3) or "STRING")
    definition = RawDbcAttributeDefinition(
        name=match.group(2),
        type=attr_type,
        scope=match.group(1),
    )
    if attr_type == "ENUM":
        definition.enum_values = _parse_enum_values(match.group(4) or "")
    return definition


def _parse_attribute(match: re.Match[str]) -> RawDbcAttribute:
    name = match.group(1)
    scope = match.group(2)
    rest = (match.group(3) or "").strip()

    if scope == "SG_":
        sg_match = re.match(r"^(\d+)\s+(\w+)\s+(.+)$", rest)
        if sg_match is None:
            raise ValueError(f"Invalid signal attribute: {name} {rest}")
        return RawDbcAttribute(
            name=name,
            scope=scope,
            message_id=int(sg_match.group(1)),
            signal_name=sg_match.group(2),
            value=_parse_attribute_value(sg_match.group(3)),
        )

    if scope == "BO_":
        bo_match = re.match(r"^(\d+)\s+(.+)$", rest)
        if bo_match is None:
            raise ValueError(f"Invalid message attribute: {name} {rest}")
        return RawDbcAttribute(
            name=name,
            scope=scope,
            message_id=int(bo_match.group(1)),
            value=_parse_attribute_value(bo_match.group(2)),
        )

    return RawDbcAttribute(name=name, scope=scope, value=_parse_attribute_value(rest))


def _parse_attribute_value(raw: str) -> AttributeValue:
    value = raw.strip()
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    if re.match(r"^0x[\da-f]+$", value, re.IGNORECASE):
        return int(value, 16)
    if re.match(r"^-?\d+(?:\.\d+)?(?:e[-+]?\d+)?$", value, re.IGNORECASE):
        parsed = float(value)
        return int(parsed) if parsed.is_integer() else parsed
    if value in {"true", "false"}:
        return value == "true"
    return value


def _normalize_attribute_type(raw: str) -> str:
    return raw if raw in {"STRING", "INT", "FLOAT", "ENUM", "HEX"} else "STRING"


def _parse_enum_values(raw: str) -> list[str]:
    return [match.group(1) for match in re.finditer(r'"([^"]*)"', raw)]
