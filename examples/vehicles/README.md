# Vehicle CAN YAML + DBC Specification v1

This document defines the vehicle profile format used by the example catalog.
Profiles pair YAML with DBC files:

- DBC decodes received CAN frames into named signals.
- YAML defines requests, action flows, monitor subscriptions, applicability,
  and references to DBC mappings.

DBC never defines workflow. YAML never defines bit math.

## File Structure

Vehicle folders contribute partial definitions. Profiles merge by
applicability, with the most-specific match winning.

```text
vehicles/
  universal/
    profile.yaml
    signals.dbc

  nissan/sentra/
    profile.yaml
    signals.dbc
```

The current common OBD-II PID example lives at:

```text
vehicles/universal/pid/
  profile.yaml
  signals.dbc
```

## YAML Root Structure

Only `version` is required.

```yaml
version: 1

applies_to: {}

dbc:
  files: []

endpoints: {}

sequences: {}

signals: {}

queries: {}

actions: {}
```

## Applicability

`applies_to` describes which vehicles a YAML profile and its DBC files are
compatible with. If `applies_to` is missing, the profile is universal and
applies to all makes and models. Standard OBD-II PIDs are a typical universal
profile.

Supported compatibility keys:

- `manufacturers`
- `models`
- `years`
- `trims`
- `engines`
- `markets`

Missing fields are wildcards and match all values.

```yaml
applies_to:
  manufacturers: [Nissan]
  models: [Sentra, Versa]
  years:
    - 2010
    - 2011
    - range: [2012, 2015]
  trims: [S, SV]
  engines: [2.0L]
  markets: [NA]
```

Year ranges are inclusive:

```yaml
years:
  - range: [2010, 2015]
```

This matches 2010 through 2015.

## DBC Rules

DBC files decode received CAN payloads only.

Use DBC for:

- passive broadcast frames
- request/response diagnostic replies
- UDS positive responses
- ECU status messages
- sensor values

Do not use DBC for:

- action flows
- multi-step transactions
- security/session setup
- tester-present keepalives
- applicability logic

DBC message IDs should be real CAN response IDs, not invented logical IDs.

```dbc
VERSION ""

BU_: Tester ECU

BO_ 2024 OBD_Response_7E8: 8 ECU
 SG_ vehicle_speed : 24|8@1+ (1,0) [0|255] "km/h" Vector__XXX

BO_ 1893 Body_Response: 8 ECU
 SG_ response_service : 8|8@1+ (1,0) [0|255] "" Vector__XXX
 SG_ routine_high     : 16|8@1+ (1,0) [0|255] "" Vector__XXX
 SG_ routine_low      : 24|8@1+ (1,0) [0|255] "" Vector__XXX
 SG_ routine_status   : 32|8@1+ (1,0) [0|255] "" Vector__XXX
```

`0x7E8` is decimal `2024`; `0x765` is decimal `1893`.

## DBC File References

Profiles reference one or more DBC files by path relative to the profile YAML.

```yaml
dbc:
  files:
    - path: signals.dbc
```

## Endpoints

An endpoint defines a reusable request/response transport target.

```yaml
endpoints:
  obd:
    request_id: 0x7DF
    response_ids:
      - range: [0x7E8, 0x7EF]
    timeout_ms: 500

  body:
    request_id: 0x745
    response_id: 0x765
    timeout_ms: 500
```

Rules:

- `request_id` is a single transmit CAN ID.
- `response_id` is a single accepted response CAN ID.
- `response_ids` is a list of accepted response IDs or inclusive ID ranges.
- Endpoints are reused by queries, actions, and sequences.

## Transaction Steps

A step is one BLE firmware request transaction.

```yaml
- send: [0x10, 0x81]
  expect: [0x50, 0x81]
```

The step sends bytes, waits for a response, and validates the response prefix.
If `expect` is omitted, the step sends bytes only and does not wait for a
response. `send` and `expect` always belong to the same step.

## Expect Matching

`expect` is a response pattern.

By default, an `expect` array is a prefix match and extra response bytes are
ignored.

```yaml
expect: [0x41, 0x0D]
```

This matches:

```text
41 0D 00
41 0D 7F
41 0D ...
```

Use `"*"` as a wildcard byte:

```yaml
expect: [0x70, 0x30, "*"]
```

This matches:

```text
70 30 00
70 30 01
70 30 FF
```

Use object form for exact matching:

```yaml
expect:
  pattern: [0x70, 0x30, 0x01]
  exact: true
```

Omitting `expect` means no response wait.

## Sequences

Sequences are reusable step groups for shared setup logic. They are expanded
inline, may be nested, and may not contain circular references.

```yaml
sequences:
  body_session_prep:
    steps:
      - send: [0x10, 0x81]
        expect: [0x50, 0x81]

      - send: [0x10, 0xC0]
        expect: [0x50, 0xC0]
```

## Sequence References

A step may reference a sequence.

```yaml
- ref: sequences.body_session_prep
```

Example:

```yaml
actions:
  horn_with_session:
    endpoint: body
    steps:
      - ref: sequences.body_session_prep
      - send: [0x30, 0x30, 0x00, 0x01]
        expect: none
```

Rules:

- References expand in place.
- Circular references are invalid.
- A referenced sequence inherits the action endpoint unless the sequence
  declares its own endpoint.

## Signals

Signals represent passively collected values. These are usually regular
broadcasts on the CAN bus and are normally decoded with a DBC file.

```yaml
signals:
  door_lock_state:
    monitor:
      message: Body_Status
      signal: door_lock_state
```

Firmware compiles monitor signals to:

- configure monitor CAN IDs from DBC messages
- receive notify streams
- decode signals continuously

## Queries

Queries are request/response reads from the CAN bus, typically to an ECU that
responds with a CAN frame. They are used for PIDs, UDS reads, VINs, serial
numbers, ASCII strings, multi-frame identifiers, and status blobs.

```yaml
queries:
  vin:
    endpoint: obd
    send: [0x09, 0x02]
    expect: [0x49, 0x02]
    decoder: ascii
    length: 17
```

Queries always use BLE request/response.

DBC-backed queries map to a message and signal from a DBC file:

```yaml
queries:
  SPEED:
    endpoint: obd
    send: [0x01, 0x0D]
    expect: [0x41, 0x0D]
    dbc_mapping:
      message: OBD_Response_7E8
      signal: SPEED
```

## Actions

Actions change vehicle state. They may be request-only, verified
request/response, or multi-step flows.

Single request:

```yaml
actions:
  flash_lights:
    send:
      request_id: 0x123
      request: [0x01, 0x00, 0x00, 0x00]
```

Verified request/response:

```yaml
actions:
  lock:
    endpoint: body
    send: [0x30, 0x38, 0x00, 0x01]
    expect: [0x70, 0x38, 0x01]
```

Multi-step flow:

```yaml
actions:
  horn:
    endpoint: body
    steps:
      - ref: body_session_prep
      - send: [0x30, 0x30, 0x00, 0x01]
        expect: [0x70, 0x30, 0x01]
```

Actions may return nothing, or boolean success/failure if `expect` validation is
used.

## Firmware Execution Mapping

The firmware exposes request/response and monitor primitives over three BLE
characteristics.

### Request Characteristic

Write + Notify. The phone writes a request to the ESP, the ESP sends it to the
CAN board, and any returned response is placed in the notify buffer.

A request may also be no-response, such as an action that does not expect a CAN
reply. In that case the firmware does not send a notify unless TX status ACKs
are explicitly requested.

Request without CAN response:

```text
[0]    seq
[1]    flags
       bit0 = expect_can_response
       bit1 = notify_tx_status (ACK)
       bit2..7 reserved
[2..5] tx_can_id
[6]    dlc 0-8
[7..14] payload[8]
```

Request with CAN response:

```text
[0]     seq
[1]     flags
        bit0 = expect_can_response
        bit1 = notify_tx_status (ACK)
        bit2..7 reserved
[2..5]  tx_can_id
[6..9]  response_id_start
[10..13] response_id_end
[14..15] timeout_ms (0-65535 ms)
[16]    dlc 0-8
[17..24] payload[8]
```

Request notify packet:

```text
[0]    seq
[1]    status
[2..5] response_can_id
[6..7] total_len
[8]    chunk_len
[9..]  payload chunk
```

### Monitor Control Characteristic

Write + Notify. This characteristic configures which CAN IDs are included in the
monitor data stream.

ADD:

```text
[0]    opcode = 0x01
[1]    seq
[2]    count
[3..]  count * u32 CAN IDs
```

REMOVE:

```text
[0]    opcode = 0x02
[1]    seq
[2]    count
[3..]  count * u32 CAN IDs
```

CLEAR:

```text
[0] opcode = 0x03
[1] seq
```

Monitor config response:

```text
[0] opcode = 0x80
[1] seq
[2] status
[3] current_monitor_count
```

Status values:

```text
0x00 OK
0x01 invalid_opcode
0x02 invalid_length
0x03 monitor_full
0x04 duplicate_id
0x05 invalid_can_id
0x06 internal_error
```

### Monitor Data Characteristic

Notify. The firmware puts subscribed CAN IDs into a snapshot buffer as frames
arrive. If an ID already exists in the buffer, the latest frame replaces it.
Notifications are emitted on the firmware's fixed monitor schedule.

Notify message packet:

```text
[0]    opcode = 0x81
[1..2] snapshot_seq u16
[3]    frame_count
[4..]  repeated frames, 13 bytes each:
       u32 can_id
       u8  dlc
       u8[8] data
```

### Profile Mapping

Signals with `monitor` configure CAN IDs through the Monitor Control
Characteristic and receive decoded values from the Monitor Data Characteristic.

Queries and signals with `send`/`expect` use the Request Characteristic with
`expect_can_response` set.

Actions use the Request Characteristic. If `expect` exists,
`expect_can_response` is set and the action returns boolean success/failure. If
no `expect` exists, the action is request-only and returns no value.

Sequences expand into multiple request transactions.

## Final Architectural Rule

DBC is the physical CAN payload decoder.

YAML is the source of transport details, workflows, request bytes, expected
replies, monitor definitions, and DBC mapping references.

Capabilities are the stable universal API.

Firmware BLE exposes these primitives through the characteristics above:

- monitor subscription/configuration
- monitor data notifications
- request/response, including request-only actions
