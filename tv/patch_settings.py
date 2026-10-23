#!/usr/bin/env python3
"""Patch webOS 4 Settings (com.palm.app.settings 4.0.1) to add AirPods rows to Sound > Sound Out.

usage: patch_settings.py ORIGINAL_DIR OUTPUT_DIR
Reads main.js and appinfo.json from ORIGINAL_DIR and writes patched copies to OUTPUT_DIR. Every edit point must
match exactly once, so a different firmware fails here instead of producing a broken Settings app.
"""
import json
import sys
from pathlib import Path

VERSION = 1

# Runs before the bundle. ES5 only: this is Chromium 53.
HELPER = r'''
window.__abr=(function(){
  var CTL='/var/lib/webosbrew/airpods-bridge/ctl.sh';
  var st={data:null,busy:false,last:0,view:null,pending:null,calls:[]};
  function exec(args,cb){
    try{
      var b=new PalmServiceBridge();st.calls.push(b);
      b.onservicecallback=function(msg){
        st.calls.splice(st.calls.indexOf(b),1);
        var r={},o;try{r=JSON.parse(msg);}catch(e){}
        try{o=JSON.parse(r.stdoutString||'');}catch(e){o={reachable:false,error:'The bridge script did not answer.'};}
        cb(o);
      };
      b.call('luna://org.webosbrew.hbchannel.service/exec',JSON.stringify({command:CTL+' '+args}));
    }catch(e){cb({reachable:false,error:String(e)});}
  }
  function redraw(){if(st.view){try{st.view.forceUpdate();}catch(e){}}}
  function done(o){st.busy=false;st.pending=null;st.last=Date.now();st.data=o||{};redraw();}
  function refresh(){if(st.busy)return;st.busy=true;exec('status',done);}
  function act(args,text){st.busy=true;st.pending=text;redraw();exec(args,done);}
  function note(d){
    if(st.pending)return st.pending;
    if(d.error)return d.error;
    if(d.running)return 'Playing on '+(d.output||'AirPods')+(d.mode==='only'?' (TV muted)':'');
    if(d.airpods)return 'Ready: '+d.airpods+' is connected to the Mac.';
    if(st.data)return 'Connect your AirPods to the Mac and select them as its sound output.';
    return 'Checking the Mac...';
  }
  return {
    version:%d,
    view:function(component,natives){
      st.view=component;
      if(Date.now()-st.last>3000)setTimeout(refresh,0);
      var d=st.data||{},only=!!(d.running&&d.mode==='only'),both=!!(d.running&&d.mode==='both');
      if(only)for(var i=0;i<natives.length;i++)if(natives[i])natives[i].selected=false;
      var text=note(d);
      return {
        only:{onToggle:function(e){act(e.selected?'start only':'stop',e.selected?'Starting AirPods...':'Stopping AirPods...');},
              value:'airpods_only',children:'AirPods (via Mac)',selected:only,helpTitle:'AirPods (via Mac)',
              helpString:'Play TV sound on AirPods connected to your Mac. The TV is muted while they play. The Mac must be awake and on the same Wi-Fi.'},
        both:{onToggle:function(e){act(e.selected?'start both':'stop',e.selected?'Starting AirPods...':'Stopping AirPods...');},
              value:'airpods_both',children:'Current Sound Out + AirPods',selected:both,helpTitle:'Current Sound Out + AirPods',
              helpString:'Play TV sound on AirPods connected to your Mac while the current sound output keeps playing.'},
        noteOnly:both?null:text,
        noteBoth:both?text:null
      };
    },
    nativeSelected:function(){var d=st.data;if(d&&d.running&&d.mode==='only')act('stop','Stopping AirPods...');}
  };
})();
''' % VERSION

NOTE = 'A("div",{className:k.a.soundOutSubItem},void 0,u.a.createElement(m.a,{className:k.a.bodyText},%s))'

EDITS = [
    ('key:"itemChanged",value:function(e){e.selected&&this.props.setSystemSettings(',
     'key:"itemChanged",value:function(e){e.selected&&window.__abr&&window.__abr.nativeSelected();e.selected&&this.props.setSystemSettings('),
    ('W=this.headphoneVolumeProps();return u.a.createElement(v.a,I({},a,{title:Object(d.b)("Sound Out")}),A(S.a,{},void 0,A(w.a,{},void 0,Object(d.b)("Single Sound Out")),u.a.createElement(C.e,T),',
     'W=this.headphoneVolumeProps(),ABR=window.__abr?window.__abr.view(this,[T,R,_,B,H,F,U,G]):null;return u.a.createElement(v.a,I({},a,{title:Object(d.b)("Sound Out")}),A(S.a,{},void 0,A(w.a,{},void 0,Object(d.b)("Single Sound Out")),ABR&&u.a.createElement(C.e,ABR.only),ABR&&ABR.noteOnly&&' + NOTE % 'ABR.noteOnly' + ',u.a.createElement(C.e,T),'),
    ('A(w.a,{},void 0,Object(d.b)("Simultaneous Sound Out with Internal TV Speaker")),u.a.createElement(C.e,U),',
     'A(w.a,{},void 0,Object(d.b)("Simultaneous Sound Out with Internal TV Speaker")),ABR&&u.a.createElement(C.e,ABR.both),ABR&&ABR.noteBoth&&' + NOTE % 'ABR.noteBoth' + ',u.a.createElement(C.e,U),'),
]


def main():
    source, output = Path(sys.argv[1]), Path(sys.argv[2])
    script = (source/'main.js').read_text(encoding='utf-8')
    if 'window.__abr' in script:
        raise SystemExit('main.js is already patched; give the original files')
    for old, new in EDITS:
        count = script.count(old)
        if count != 1:
            raise SystemExit('edit point matched %d times (expected 1): %s...' % (count, old[:60]))
        script = script.replace(old, new)
    appinfo = json.loads((source/'appinfo.json').read_text())
    # The V8 snapshot is a precompiled copy of the original main.js; without it the loader runs ours.
    appinfo.pop('v8SnapshotFile', None)
    appinfo['usePrerendering'] = False
    output.mkdir(parents=True, exist_ok=True)
    (output/'main.js').write_text(HELPER + script, encoding='utf-8')
    (output/'appinfo.json').write_text(json.dumps(appinfo, indent='\t'))
    print('patched (helper version %d)' % VERSION)


if __name__ == '__main__':
    main()
