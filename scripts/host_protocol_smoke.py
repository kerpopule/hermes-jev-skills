#!/usr/bin/env python3
"""Exercise real Hermes discovery/middleware and host protocol lookup, offline.

Run using the host interpreter, with --hermes-source naming its source tree.
All home/state is disposable. Router answers are synthetic, never provider calls.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--hermes-source',type=Path,required=True)
    ap.add_argument('--plugin-source',type=Path)
    ap.add_argument('--out',type=Path,required=True)
    args=ap.parse_args()
    root=Path(__file__).resolve().parents[1]
    source=args.plugin_source or root/'hermes/plugin/hermes-jev'
    if args.out.exists():raise ValueError('refuse existing receipt')
    with tempfile.TemporaryDirectory(prefix='jev-host-protocol-') as tmp:
        home=Path(tmp)
        os.environ['HERMES_HOME']=str(home)
        # No provider credential resolution or runtime agent is needed by this probe.
        os.environ['HOME']=str(home)
        target=home/'plugins/hermes-jev';target.mkdir(parents=True)
        for n in ('__init__.py','plugin.yaml'):shutil.copyfile(source/n,target/n)
        library=source/'jevkit' if (source/'jevkit').is_dir() else root/'jevkit'
        shutil.copytree(library,target/'jevkit',ignore=shutil.ignore_patterns('__pycache__'))
        (home/'config.yaml').write_text('plugins:\n  enabled: [hermes-jev]\nmodel:\n  default: glm-5.3-flash\n')
        (home/'jev').mkdir();(home/'jev/state.json').write_text(json.dumps({'routing':'on','skills':'off'}))
        sys.path.insert(0,str(args.hermes_source.resolve()))
        from hermes_cli import plugins
        from hermes_cli.middleware import apply_llm_request_middleware
        manager=plugins.PluginManager();manager.discover_and_load()
        loaded=manager._plugins['hermes-jev']
        p=loaded.module
        assert p is not None and manager.has_middleware('llm_request'), 'plugin not registered'
        actual={m:p._model_api_mode('opencode-go',m) for m in ('glm-5.3-flash','deepseek-v4-flash','grok-4.7')}
        assert actual=={'glm-5.3-flash':'chat_completions','deepseek-v4-flash':'chat_completions','grok-4.7':'codex_responses'},actual
        assert p._model_api_mode('unregistered-synthetic-provider','fixture') is None
        assert p._model_api_mode('custom','fixture') is None
        results=[]
        with mock.patch.object(plugins,'get_plugin_manager',return_value=manager),mock.patch.object(p,'_default_model',return_value='glm-5.3-flash'):
            for candidate,expected in [('grok-4.7','glm-5.3-flash'),('deepseek-v4-flash','deepseek-v4-flash')]:
                session='probe-'+candidate
                p._on_pre_llm_call(session_id=session,turn_id='1',user_message='Synthetic public scheduler code question')
                request={'model':'glm-5.3-flash','messages':[{'role':'user','content':'Synthetic fixture'}]}
                original=json.loads(json.dumps(request))
                with mock.patch.object(p.route,'decide',return_value={'routed':True,'model':'opencode-go:'+candidate,'model_id':candidate}):
                    value=apply_llm_request_middleware(request,session_id=session,turn_id='1',model='glm-5.3-flash',provider='opencode-go',base_url='https://opencode.ai/zen/go/v1',api_mode='chat_completions')
                assert request==original
                assert value.payload['model']==expected,(candidate,value.payload)
                assert value.payload['messages']==original['messages']
                results.append({'candidate':candidate,'effective_model':expected,'changed':value.changed})
        receipt={'schema':'jev.host_protocol_probe_v1','registered_via_real_plugin_manager':True,
                 'actual_host_resolver':actual,'real_middleware_chain':results,'provider_calls':0,
                 'source_plugin_sha256':hashlib.sha256((source/'__init__.py').read_bytes()).hexdigest(),
                 'not_claimed':['live provider transport','live gateway reload','model semantic accuracy']}
        args.out.parent.mkdir(parents=True,exist_ok=True)
        args.out.write_text(json.dumps(receipt,indent=2)+'\n')
        print(json.dumps(receipt,indent=2))
if __name__=='__main__':main()
