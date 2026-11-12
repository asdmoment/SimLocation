"""Check the generated control flow without a phone or network changes."""
import importlib.util
import json
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("build_shortcuts", Path(__file__).parents[1] / "tools/build_shortcuts.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)

TEAM = "ABCDE12345"


def execute(actions, replies):
    outputs, variables, events = {}, {}, []
    replies = iter(replies)
    active = True

    def resolve(value):
        if value["WFSerializationType"] == "WFTextTokenString":
            value = {"Value": value["Value"]["attachmentsByRange"]["{0, 1}"]}
        value = value["Value"]
        return variables[value["VariableName"]] if value["Type"] == "Variable" else outputs[value["OutputUUID"]]

    for item in actions:
        identifier, p = item["WFWorkflowActionIdentifier"], item["WFWorkflowActionParameters"]
        result = None
        if identifier == "is.workflow.actions.conditional":
            if p["WFControlFlowMode"] == 0:
                assert p["WFCondition"] == 4
                active = resolve(p["WFInput"]["Variable"]) == p["WFConditionalActionString"]
            else:
                assert p["WFControlFlowMode"] == 2
                active = True
            continue
        if not active:
            continue
        if identifier.endswith("RunSwipletActionIntent"):
            events.append(("call", json.loads(p["input"])))
            result = json.dumps(next(replies))
        elif identifier.endswith(".setvariable"):
            variables[p["WFVariableName"]] = resolve(p["WFInput"])
        elif identifier.endswith(".detect.dictionary"):
            result = json.loads(resolve(p["WFInput"]))
        elif identifier.endswith(".getvalueforkey"):
            result = resolve(p["WFInput"])[p["WFDictionaryKey"]]
        elif identifier.endswith(".cellulardata.set"):
            events.append("data on" if p["OnValue"] else "data off")
        elif identifier.endswith(".delay"):
            events.append(("wait", p["WFDelayTime"]))
        elif identifier.endswith(".showresult"):
            events.append(("show", resolve(p["Text"])))
        else:
            raise AssertionError(identifier)
        outputs[p["UUID"]] = result
    return events


class ShortcutFilesTests(unittest.TestCase):
    def test_direct_success_and_validation_failure_do_not_toggle_network(self):
        for mode in ("favorite", "clear"):
            for status, message in (("ok", "完成"), ("error", "收藏不存在")):
                with self.subTest(mode=mode, status=status):
                    events = execute(builder.build(TEAM, mode), [{"status": status, "message": message}])
                    self.assertEqual(len(events), 2)
                    self.assertEqual(events[0][0], "call")
                    self.assertFalse(events[0][1]["reconnect"])
                    self.assertEqual(events[1], ("show", message))

    def test_fallback_restores_data_even_if_the_offline_action_fails(self):
        for mode in ("favorite", "clear"):
            for offline_status in ("ok", "retry", "error"):
                for final_status in ("ok", "retry", "error"):
                    with self.subTest(mode=mode, offline=offline_status, final=final_status):
                        message = "完成" if final_status == "ok" else "操作未完成"
                        events = execute(builder.build(TEAM, mode), [
                            {"status": "retry", "message": "disconnected"},
                            {"status": offline_status, "message": "intermediate"},
                            {"status": final_status, "message": message},
                        ])
                        self.assertEqual(events[1], "data off")
                        self.assertEqual(events[4], "data on")
                        self.assertEqual(events[5], ("wait", 6.0))
                        self.assertEqual(events[-1], ("show", message))
                        calls = [e[1] for e in events if isinstance(e, tuple) and e[0] == "call"]
                        self.assertEqual([c["reconnect"] for c in calls], [False, True, True])
                        self.assertTrue(all(c["mode"] == mode for c in calls))
                        if mode == "favorite":
                            self.assertTrue(all(c["name"] == "默认位置" for c in calls))

    def test_native_text_types_are_explicit(self):
        for mode in ("favorite", "clear"):
            actions = builder.build(TEAM, mode)
            condition = actions[4]["WFWorkflowActionParameters"]
            value = condition["WFInput"]["Variable"]["Value"]
            self.assertEqual(value["Aggrandizements"], [{"Type": "WFCoercionVariableAggrandizement", "CoercionItemClass": "WFStringContentItem"}])
            self.assertEqual(condition["WFConditionalActionString"], "retry")
            text = actions[-1]["WFWorkflowActionParameters"]["Text"]
            self.assertEqual(text["WFSerializationType"], "WFTextTokenString")
            self.assertEqual(text["Value"]["attachmentsByRange"]["{0, 1}"]["OutputUUID"], actions[-2]["WFWorkflowActionParameters"]["UUID"])

    def test_no_open_app_action_and_unique_action_ids(self):
        for mode in ("favorite", "clear"):
            actions = builder.build(TEAM, mode)
            self.assertFalse(any(a["WFWorkflowActionIdentifier"].endswith(".openapp") for a in actions))
            ids = [a["WFWorkflowActionParameters"]["UUID"] for a in actions]
            self.assertEqual(len(ids), len(set(ids)))

    def test_the_team_is_all_that_ties_a_file_to_a_phone(self):
        for mode in ("favorite", "clear"):
            calls = [a for a in builder.build(TEAM, mode) if a["WFWorkflowActionIdentifier"].endswith("RunSwipletActionIntent")]
            self.assertEqual(len(calls), 3)
            for call in calls:
                self.assertEqual(call["WFWorkflowActionIdentifier"], f"io.nekohasekai.swipt.{TEAM}.RunSwipletActionIntent")
                descriptor = call["WFWorkflowActionParameters"]["AppIntentDescriptor"]
                self.assertEqual((descriptor["BundleIdentifier"], descriptor["TeamIdentifier"]), (f"io.nekohasekai.swipt.{TEAM}", TEAM))
            # No helper shortcut, so nothing from a shortcut library.
            self.assertFalse(any(a["WFWorkflowActionIdentifier"].endswith(".runworkflow") for a in builder.build(TEAM, mode)))


if __name__ == "__main__":
    unittest.main()
