"""Build the two Wi-Fi/cellular location shortcuts for one phone.

Usage: python3 -I tools/build_shortcuts.py TEAM NEW_OUTPUT_DIRECTORY

TEAM is the team identifier Swipt is signed with on that phone: the last part of
its bundle identifier, io.nekohasekai.swipt.TEAM. Nothing else about a phone goes
into the files, and no shortcut library is read. Sign each generated file with
`shortcuts sign --mode anyone` and import it through Shortcuts on the phone.
"""
import json
import plistlib
import re
import sys
import uuid
from pathlib import Path

SHORTCUTS = (("favorite", "SimLocation默认位置"), ("clear", "SimLocation恢复真实定位"))


def action(identifier, **parameters):
    return {"WFWorkflowActionIdentifier": identifier,
            "WFWorkflowActionParameters": {"UUID": str(uuid.uuid4()).upper(), **parameters}}


def output(item):
    return {"Value": {"Type": "ActionOutput", "OutputUUID": item["WFWorkflowActionParameters"]["UUID"],
                      "OutputName": "Result"}, "WFSerializationType": "WFTextTokenAttachment"}


def text_token(item):
    return {"Value": {"string": "￼", "attachmentsByRange": {"{0, 1}": output(item)["Value"]}},
            "WFSerializationType": "WFTextTokenString"}


def variable():
    return {"Value": {"Type": "Variable", "VariableName": "LocationResult"},
            "WFSerializationType": "WFTextTokenAttachment"}


def build(team, mode):
    bundle = f"io.nekohasekai.swipt.{team}"

    def call(reconnect=False):
        payload = {"mode": mode, "reconnect": reconnect}
        if mode == "favorite":
            payload["name"] = "默认位置"
        return action(f"{bundle}.RunSwipletActionIntent",
                      AppIntentDescriptor={"AppIntentIdentifier": "RunSwipletActionIntent", "BundleIdentifier": bundle,
                                           "Name": "Swipt", "TeamIdentifier": team},
                      action={"identifier": "simlocation/locationShortcut",
                              "title": {"key": "Apply SimLocation Shortcut"},
                              "subtitle": {"key": "SimLocation"}},
                      input=json.dumps(payload, ensure_ascii=False))

    # The shortcut switches cellular data itself, so it needs no helper shortcuts.
    def cellular(on):
        return action("is.workflow.actions.cellulardata.set", OnValue=on)

    first, offline, after = call(), call(True), call(True)
    dictionary = action("is.workflow.actions.detect.dictionary", WFInput=variable())
    status = action("is.workflow.actions.getvalueforkey", WFInput=output(dictionary), WFDictionaryKey="status")
    status_text = output(status)
    status_text["Value"]["Aggrandizements"] = [{"Type": "WFCoercionVariableAggrandizement", "CoercionItemClass": "WFStringContentItem"}]
    group = str(uuid.uuid4()).upper()
    final_dictionary = action("is.workflow.actions.detect.dictionary", WFInput=variable())
    message = action("is.workflow.actions.getvalueforkey", WFInput=output(final_dictionary), WFDictionaryKey="message")
    return [
        first,
        action("is.workflow.actions.setvariable", WFVariableName="LocationResult", WFInput=output(first)),
        dictionary, status,
        action("is.workflow.actions.conditional", GroupingIdentifier=group, WFControlFlowMode=0,
               WFCondition=4, WFConditionalActionString="retry", WFInput={"Type": "Variable", "Variable": status_text}),
        cellular(False), action("is.workflow.actions.delay", WFDelayTime=1.0), offline,
        cellular(True), action("is.workflow.actions.delay", WFDelayTime=6.0), after,
        action("is.workflow.actions.setvariable", WFVariableName="LocationResult", WFInput=output(after)),
        action("is.workflow.actions.conditional", GroupingIdentifier=group, WFControlFlowMode=2),
        final_dictionary, message,
        action("is.workflow.actions.showresult", Text=text_token(message)),
    ]


def document(name, actions):
    return {
        "WFWorkflowName": name,
        "WFWorkflowClientVersion": "5114.0.8.3",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": -43634177, "WFWorkflowIconGlyphNumber": 61440},
        "WFWorkflowTypes": [], "WFWorkflowInputContentItemClasses": [],
        "WFWorkflowImportQuestions": [], "WFWorkflowActions": actions,
    }


def main():
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    team, destination = sys.argv[1], Path(sys.argv[2])
    if not re.fullmatch(r"[A-Z0-9]{10}", team):
        raise SystemExit("TEAM is the 10-character team identifier at the end of Swipt's bundle identifier.")
    destination.mkdir(parents=True, exist_ok=False)
    for mode, name in SHORTCUTS:
        path = destination / (name + ".unsigned.shortcut")
        with path.open("xb") as stream:
            plistlib.dump(document(name, build(team, mode)), stream, fmt=plistlib.FMT_BINARY)
        print(path)


if __name__ == "__main__":
    main()
