#!/usr/bin/env python3
"""Patch webOS 4 Quick Settings (the panel behind the remote's settings button) to add AirPods pills to Sound Out.

usage: patch_quicksettings.py ORIGINAL_DIR OUTPUT_DIR
Reads SoundOutServiceModel.qml and QuickSettings.qml from ORIGINAL_DIR (a copy of /usr/lib/qt5/qml/QuickSettings)
and writes patched copies to OUTPUT_DIR. Every edit point must match exactly once.
"""
import sys
from pathlib import Path

MODEL_MEMBERS = r'''
    // lgtv-airpods-bridge: AirPods pills, shown while the Mac reports the AirPods connected or playing.
    property var abr: ({})
    readonly property string abrCtl: "/var/lib/webosbrew/airpods-bridge/ctl.sh"
    readonly property var abrNames: ({"airpods_only": "AirPods", "airpods_both": "AirPods + TV"})

    QuickSettingsService {
        id: abrExec
        service: "org.webosbrew.hbchannel.service"
        method: "exec"
        onCallSuccess: {
            var reply = {};
            try { reply = JSON.parse(response.stdoutString || "{}"); } catch (e) { reply = {}; }
            abr = reply;
            abrApply();
        }
        onCallFailure: {
            console.warn("AirPods bridge exec failed", JSON.stringify(response));
        }
    }

    function abrRun(args) {
        abrExec.callService({"command": abrCtl + " " + args});
    }

    function abrApply() {
        // Keep the pills in step with the Mac: remove ours, then add them back if the AirPods are there.
        for (var i = availableModes.count - 1; i >= 0; i--) {
            if (abrNames[availableModes.get(i).value] !== undefined)
                availableModes.remove(i);
        }
        if (abr.airpods || abr.running) {
            availableModes.append({name: abrNames["airpods_only"], value: "airpods_only"});
            availableModes.append({name: abrNames["airpods_both"], value: "airpods_both"});
        }
        abrSelect();
    }

    function abrSelect() {
        // While the bridge plays, its pill is the selected one; otherwise the TV's own output is.
        var wanted = abr.running ? "airpods_" + abr.mode : currentMode;
        for (var i = 0; i < availableModes.count; i++) {
            if (availableModes.get(i).value === wanted) {
                modeReceived(i);
                return true;
            }
        }
        return false;
    }
'''

MODEL_EDITS = [
    ('    property string connectStatus: ""\n',
     '    property string connectStatus: ""\n' + MODEL_MEMBERS),
    # A subscribed TV-output update must not take the highlight away from a playing AirPods pill.
    ('            if(soundoutClickCounter == 0) {\n',
     '            if(soundoutClickCounter == 0 && !(abr.running && abrSelect())) {\n'),
    # The TV rebuilds its list from scratch; add the pills back each time.
    ('                }\n            }\n            getMode();\n',
     '                }\n            }\n            abrApply();\n            getMode();\n'),
    ('    function getInitialValue(){\n',
     '    function getInitialValue(){\n        abrRun("status");\n'),
    ('        var selectedObj = availableModes.get(selectedMode);\n        var settingsObj = { "category": modeCategory, "settings" : {} };\n        settingsObj.settings[modeKey] = selectedObj.value;\n        console.log("setting mode with ", JSON.stringify(settingsObj));\n        soundoutClickCounter++;\n',
     '        var selectedObj = availableModes.get(selectedMode);\n'
     '        if (abrNames[selectedObj.value] !== undefined) {\n'
     '            abrRun("start " + (selectedObj.value === "airpods_only" ? "only" : "both"));\n'
     '            return;\n'
     '        }\n'
     '        if (abr.running)\n'
     '            abrRun("stop");\n'
     '        var settingsObj = { "category": modeCategory, "settings" : {} };\n        settingsObj.settings[modeKey] = selectedObj.value;\n        console.log("setting mode with ", JSON.stringify(settingsObj));\n        soundoutClickCounter++;\n'),
]

PANEL_EDITS = [
    # Refresh on every open rather than polling: each status check runs a shell command on the TV.
    ('        panel.state = "opened";\n',
     '        soundOutService.abrRun("status");\n        panel.state = "opened";\n'),
]


def apply(text, edits, name):
    if 'abrRun' in text:
        raise SystemExit('%s is already patched; give the original files' % name)
    for old, new in edits:
        count = text.count(old)
        if count != 1:
            raise SystemExit('%s: edit point matched %d times (expected 1): %r' % (name, count, old[:70]))
        text = text.replace(old, new)
    return text


def main():
    source, output = Path(sys.argv[1]), Path(sys.argv[2])
    output.mkdir(parents=True, exist_ok=True)
    for name, edits in (('SoundOutServiceModel.qml', MODEL_EDITS), ('QuickSettings.qml', PANEL_EDITS)):
        (output/name).write_text(apply((source/name).read_text(encoding='utf-8'), edits, name), encoding='utf-8')
    print('patched Quick Settings')


if __name__ == '__main__':
    main()
