from __future__ import annotations


CATALOG_SCHEMA = 2
VERIFIED_EDITOR_VERSION = "3.713.2026.718"
VERIFIED_EDITOR_SHA256 = "2ce5639f669643ded07a9390ef05054b8f95acbfa1b4dc1f4936246df5eae0c3"

EVIDENCE_RANK = {
    "manual": 0,
    "observed": 1,
    "roundtrip": 2,
    "differential": 3,
    "runtime_verified": 4,
}

# BEGIN WOLFLATOR EDITOR CALIBRATION
# Generated only from official Editor save/reopen/copy and Auto evidence.
GENERATED_MANUAL_SHAPES: dict[int, tuple[tuple[int, int], ...]] = {105: ((0, 0),), 125: ((1, 0), (2, 0), (3, 0)), 177: ((0, 0),), 178: ((0, 0),), 211: ((2, 0),), 230: ((0, 0),), 231: ((0, 0),), 240: ((2, 0),), 241: ((1, 0),), 251: ((5, 4),), 281: ((3, 0),), 402: ((1, 0),)}
GENERATED_MANUAL_EVIDENCE: dict[int, str] = {105: 'roundtrip', 125: 'roundtrip', 177: 'roundtrip', 178: 'roundtrip', 211: 'differential', 230: 'roundtrip', 231: 'roundtrip', 240: 'roundtrip', 241: 'roundtrip', 251: 'differential', 281: 'roundtrip', 402: 'roundtrip'}
# END WOLFLATOR EDITOR CALIBRATION

# The free 3.713 command inventory. ProFeature (1000) is deliberately absent.
# Effects describe only state relevant to translation-logic analysis.
COMMAND_CATALOG: dict[int, tuple[str, str, str]] = {
    0: ("Blank", "control_flow", "roundtrip"),
    99: ("Checkpoint", "no_write", "roundtrip"),
    101: ("Message", "no_write", "roundtrip"),
    102: ("Choices", "no_write", "roundtrip"),
    103: ("Comment", "no_write", "roundtrip"),
    105: ("ForceStopMessage", "no_write", "manual"),
    106: ("DebugMessage", "no_write", "roundtrip"),
    107: ("ClearDebugText", "no_write", "roundtrip"),
    111: ("VariableCondition", "control_flow", "roundtrip"),
    112: ("StringCondition", "condition", "runtime_verified"),
    121: ("SetVariable", "numeric_write", "runtime_verified"),
    122: ("SetString", "string_write", "runtime_verified"),
    123: ("InputKey", "numeric_write", "roundtrip"),
    124: ("SetVariableEx", "string_write", "differential"),
    125: ("AutoInput", "no_write", "manual"),
    126: ("BanInput", "no_write", "roundtrip"),
    130: ("Teleport", "no_write", "roundtrip"),
    140: ("Sound", "no_write", "roundtrip"),
    150: ("Picture", "no_write", "roundtrip"),
    151: ("ChangeColor", "no_write", "roundtrip"),
    160: ("SetTransition", "no_write", "roundtrip"),
    161: ("PrepareTransition", "no_write", "roundtrip"),
    162: ("ExecuteTransition", "no_write", "roundtrip"),
    170: ("StartLoop", "control_flow", "roundtrip"),
    171: ("BreakLoop", "control_flow", "roundtrip"),
    172: ("BreakEvent", "control_flow", "roundtrip"),
    173: ("EraseEvent", "control_flow", "roundtrip"),
    174: ("ReturnToTitle", "control_flow", "roundtrip"),
    175: ("EndGame", "control_flow", "roundtrip"),
    176: ("StartLoop2", "control_flow", "roundtrip"),
    177: ("StopNonPic", "no_write", "manual"),
    178: ("ResumeNonPic", "no_write", "manual"),
    179: ("LoopTimes", "control_flow", "roundtrip"),
    180: ("Wait", "no_write", "roundtrip"),
    201: ("Move", "no_write", "roundtrip"),
    202: ("WaitForMove", "no_write", "roundtrip"),
    210: ("CommonEvent", "event_call", "runtime_verified"),
    211: ("CommonEventReserve", "event_call", "roundtrip"),
    212: ("SetLabel", "control_flow", "roundtrip"),
    213: ("JumpLabel", "control_flow", "roundtrip"),
    220: ("SaveLoad", "no_write", "roundtrip"),
    221: ("LoadGame", "string_write", "differential"),
    222: ("SaveGame", "no_write", "roundtrip"),
    230: ("MoveDuringEventOn", "no_write", "manual"),
    231: ("MoveDuringEventOff", "no_write", "manual"),
    240: ("Chip", "no_write", "manual"),
    241: ("ChipSet", "no_write", "manual"),
    242: ("OverwriteMapChips", "no_write", "roundtrip"),
    250: ("Database", "database", "runtime_verified"),
    251: ("ImportDatabase", "database", "manual"),
    270: ("Party", "no_write", "roundtrip"),
    280: ("MapEffect", "no_write", "roundtrip"),
    281: ("ScrollScreen", "no_write", "manual"),
    290: ("Effect", "no_write", "roundtrip"),
    300: ("CommonEventByName", "event_call", "runtime_verified"),
    401: ("ChoiceCase", "control_flow", "roundtrip"),
    402: ("SpecialChoiceCase", "control_flow", "manual"),
    420: ("ElseCase", "control_flow", "roundtrip"),
    421: ("CancelCase", "control_flow", "roundtrip"),
    498: ("LoopEnd", "control_flow", "roundtrip"),
    499: ("BranchEnd", "control_flow", "roundtrip"),
}

CALIBRATED_SHAPES: dict[int, tuple[tuple[int, int], ...]] = {
    0: ((0, 0),),
    99: ((1, 0),),
    101: ((0, 1),),
    102: ((1, 1), (1, 2), (1, 3), (1, 5), (1, 7)),
    103: ((0, 1),),
    106: ((0, 1),),
    107: ((0, 0),),
    111: ((4, 0), (7, 0), (10, 0), (13, 0)),
    112: ((2, 4), (3, 4), (4, 4), (5, 4)),
    121: ((4, 0), (5, 0)),
    122: ((2, 1), (2, 2), (3, 0), (3, 1), (3, 2), (4, 1)),
    123: ((2, 0), (3, 0)),
    124: ((3, 0), (4, 0)),
    126: ((1, 0), (2, 0)),
    130: ((5, 0), (6, 0)),
    140: ((4, 0), (6, 1), (7, 1), (8, 0), (8, 1), (9, 0)),
    150: ((2, 0), (3, 0), (4, 0), (6, 0), (11, 0), (11, 1), (12, 0), (13, 0), (13, 1), (14, 0), (14, 1), (15, 0), (18, 0), (18, 1), (19, 0), (19, 1), (25, 0), (25, 1)),
    151: ((2, 0),),
    160: ((2, 0), (3, 0)),
    161: ((0, 0),),
    162: ((0, 0),),
    170: ((0, 0),),
    171: ((0, 0),),
    172: ((0, 0),),
    173: ((2, 0),),
    174: ((0, 0),),
    175: ((0, 0),),
    176: ((0, 0),),
    179: ((1, 0),),
    180: ((1, 0),),
    201: ((1, 0),),
    202: ((0, 0),),
    210: ((2, 0), (3, 0), (4, 0), (5, 0), (6, 0), (7, 0), (7, 2), (8, 0), (8, 2), (9, 0), (9, 2), (9, 3), (12, 0), (12, 6)),
    211: ((2, 0),),
    212: ((0, 1),),
    213: ((0, 1),),
    220: ((2, 0),),
    221: ((4, 0),),
    222: ((4, 0),),
    242: ((6, 0),),
    250: ((4, 4), (5, 0), (5, 4)),
    270: ((1, 0), (3, 0)),
    280: ((2, 0),),
    290: ((7, 0), (7, 1), (8, 0)),
    300: ((2, 1), (3, 1), (4, 1), (4, 2), (5, 1), (5, 2), (6, 1), (6, 2), (6, 4), (7, 1), (7, 2), (8, 1), (8, 2), (8, 3), (9, 1), (9, 2), (9, 3), (10, 1), (10, 5), (12, 1), (12, 6), (13, 1)),
    401: ((1, 0),),
    420: ((1, 0),),
    421: ((1, 0),),
    498: ((0, 0),),
    499: ((0, 0),),
}
CALIBRATED_SHAPES.update(GENERATED_MANUAL_SHAPES)
for _opcode, _evidence in GENERATED_MANUAL_EVIDENCE.items():
    _name, _effect, _old_evidence = COMMAND_CATALOG[_opcode]
    COMMAND_CATALOG[_opcode] = (_name, _effect, _evidence)

PRO_OPCODE = 1000
SPECIALIZED_OPCODES = frozenset({112, 121, 122, 124, 210, 221, 250, 300})

# These are the only string-bearing command shapes whose parameter semantics
# are consumed by the analyzer. Other strings are display text, labels, or
# resource paths and do not assign an event string variable.
STRING_PARAMETER_ROLES: dict[int, tuple[str, ...]] = {
    112: ("condition_literal",),
    122: ("assignment_literal",),
    210: ("call_argument",),
    250: ("database_selector_or_value",),
    300: ("common_event_name", "call_argument"),
}

# Paths are documentation for the frozen 3.713 free-edition UI. They are not
# used to drive the Editor: the calibration driver pastes official event-code
# records and asks the Editor to normalize them itself.
UI_PATHS: dict[int, str] = {
    105: "A 文章/選択肢 > 文章の強制中断",
    125: "C 変数操作 > キー入力の自動入力",
    177: "その他1 > ピクチャ以外停止",
    178: "その他1 > 停止解除",
    230: "その他1 > 処理中のEv移動ON",
    231: "その他1 > 処理中のEv移動OFF",
    240: "マップ > チップ処理 > チップ設定",
    241: "マップ > チップ処理 > チップセット切替",
    251: "D DB操作 > CSV入出力",
    281: "画面 > 画面スクロール",
    402: "A 文章/選択肢 > 特殊選択肢の分岐",
}

# Candidate records are discovery input, never production evidence. They only
# become calibrated shapes after Editor save, close, reopen, special-copy and
# two identical Auto exports. Values are deliberately harmless sentinels.
MANUAL_CALIBRATION_CASES: tuple[dict[str, object], ...] = (
    {"id": "CAL-105-BASE", "opcode": 105, "record": "[105][0,0]<0>()()"},
    {"id": "CAL-125-BASIC", "opcode": 125, "record": "[125][1,0]<0>(0)()"},
    {"id": "CAL-125-KEYBOARD", "opcode": 125, "record": "[125][2,0]<0>(0,0)()"},
    {"id": "CAL-125-MOUSE", "opcode": 125, "record": "[125][3,0]<0>(0,0,0)()"},
    {"id": "CAL-177-BASE", "opcode": 177, "record": "[177][0,0]<0>()()"},
    {"id": "CAL-178-BASE", "opcode": 178, "record": "[178][0,0]<0>()()"},
    {
        "id": "CAL-211-RESERVE-A",
        "opcode": 211,
        "record": "[211][2,0]<0>(0,0)()",
        "differential": "event_id",
    },
    {
        "id": "CAL-211-RESERVE-B",
        "opcode": 211,
        "record": "[211][2,0]<0>(1,0)()",
        "differential": "event_id",
    },
    {"id": "CAL-230-BASE", "opcode": 230, "record": "[230][0,0]<0>()()"},
    {"id": "CAL-231-BASE", "opcode": 231, "record": "[231][0,0]<0>()()"},
    {"id": "CAL-240-BASE", "opcode": 240, "record": "[240][2,0]<0>(0,0)()"},
    {"id": "CAL-241-BASE", "opcode": 241, "record": "[241][1,0]<0>(0)()"},
    {
        "id": "CAL-251-CSV-A",
        "opcode": 251,
        "record": '[251][5,4]<0>(0,0,0,0,1)("CAL-251-A.csv","","","")',
        "differential": "filename",
    },
    {
        "id": "CAL-251-CSV-B",
        "opcode": 251,
        "record": '[251][5,4]<0>(0,0,0,0,1)("CAL-251-B.csv","","","")',
        "differential": "filename",
    },
    {"id": "CAL-281-BASE", "opcode": 281, "record": "[281][3,0]<0>(0,0,0)()"},
    {"id": "CAL-402-BASE", "opcode": 402, "record": "[402][1,0]<0>(0)()"},
)

# The catalog is deliberately version-scoped. A newer Editor may reuse an
# opcode with a new shape; command_effect() will return None for that shape.
EXCLUDED_COMMANDS = {
    PRO_OPCODE: {
        "name": "ProFeature",
        "status": "excluded_pro",
        "reason": "WOLF RPG Editor Pro is outside the 3.713 free-edition scope",
    }
}


def command_effect(opcode: int, int_count: int, string_count: int) -> str | None:
    item = COMMAND_CATALOG.get(opcode)
    if item is None or (int_count, string_count) not in CALIBRATED_SHAPES.get(opcode, ()):
        return None
    return item[1]


def catalog_record(opcode: int) -> dict[str, object] | None:
    item = COMMAND_CATALOG.get(opcode)
    if item is None:
        return None
    return {
        "opcode": opcode,
        "name": item[0],
        "effect": item[1],
        "evidence": item[2],
        "shapes": [list(shape) for shape in CALIBRATED_SHAPES.get(opcode, ())],
        "ui_path": UI_PATHS.get(opcode, "official sample/corpus"),
        "string_parameters": list(STRING_PARAMETER_ROLES.get(opcode, ())),
        "case_ids": [
            str(case["id"])
            for case in MANUAL_CALIBRATION_CASES
            if case["opcode"] == opcode
        ],
    }
