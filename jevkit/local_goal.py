"""Opt-in closed action tables for a bounded local goal loop.

This module selects only; it never launches a browser, runs input, lowers the
chooser's confidence floor, or treats a model's done verdict as completion.
The owning executor must enforce target scope, stale guards, step/deadline
budgets and deterministic end-state verification. Field values are included
only for explicitly supplied, non-sensitive bindings; no unbound values leave.
"""
from __future__ import annotations
import re
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple
from . import privacy
from .choose import MIN_CONFIDENCE, choose, validate

# Conservative refusal, not a replacement for executor action approvals.
_RISK = re.compile(r'\b(publish|send|post|share|buy|purchase|pay|checkout|delete|remove|erase|'
                   r'trash|close|quit|restart|sign in|log in|sign out|accept terms|agree|install)\b', re.I)


def _private(text: str) -> bool:
    return privacy.is_sensitive(text) or privacy.redact(text) != privacy.normalize(text)


def build(goal: str, observation_id: str, elements: Sequence[Mapping[str, Any]], *,
          bindings: Optional[Mapping[str, str]] = None, inputs: Optional[Mapping[str, str]] = None,
          history: Sequence[Mapping[str, str]] = ()) -> Tuple[Dict[str, Any], Dict[str, Dict[str, str]]]:
    """Build opaque ids backed by complete caller-owned actions, fail closed.

    Only CLICK and TYPE_TEXT are supported. Exact field-name -> input-key
    bindings are supplied by the planner. All currently visible bound fields
    must match before progress clicks are offered. Filled fields are neither
    typed nor clicked again. No typed text comes from a model or page.
    """
    bindings = dict(bindings or {})
    inputs = dict(inputs or {})
    if not isinstance(goal, str) or _private(goal):
        raise ValueError('private goal is excluded from this pilot')
    for record in history:
        if any(not isinstance(value, str) or _private(value) for value in record.values()):
            raise ValueError('private history is excluded from this pilot')
    for name, key in bindings.items():
        value = inputs.get(key)
        if not isinstance(name,str) or not name or not isinstance(value,str) or not value or len(value)>300:
            raise ValueError('binding needs a nonempty name and supplied short text input')
        if _private(name) or _private(value) or _RISK.search(privacy.normalize(name)):
            raise ValueError('sensitive or consequential field is excluded from this pilot')
    ids=set(); regions=[]; table={}; candidates=[]; bound=[]
    for item in elements:
        identifier=item.get('id'); name=item.get('name','')
        if not isinstance(identifier,str) or not identifier or identifier in ids:
            raise ValueError('observed element ids must be nonempty and unique')
        ids.add(identifier)
        if not item.get('visible',True) or not name:continue
        if not isinstance(name,str) or len(name)>280 or _private(name):
            raise ValueError('sensitive or oversized observed label')
        role=privacy.normalize(str(item.get('role',''))).lower()
        if privacy.is_sensitive(role) or 'securetext' in role or item.get('secret') or item.get('input_type')=='password':
            raise ValueError('secure fields are excluded from this pilot')
        actions=item.get('actions',())
        if name in bindings and 'TYPE_TEXT' in actions:
            if item.get('input_type') not in ('text','search','textarea'):
                raise ValueError('bound fields require a verified plaintext input type')
            bound.append(item)
    pending = [item for item in bound if item.get('value') != inputs[bindings[item['name']]]]
    if len({item['name'] for item in bound}) != len(bound):
        raise ValueError('ambiguous bound field labels')
    for item in elements:
        if not item.get('visible',True) or not item.get('name'):continue
        name=item['name'];actions=item.get('actions',())
        filled=item in bound and item not in pending
        regions.append({'id':item['id'],'role':str(item.get('role','element'))[:64],
                        'label':name+(' (bound input already filled)' if filled else ''),
                        'interactive':bool(actions) and bool(item.get('enabled',True))})
        if not item.get('enabled',True) or _RISK.search(privacy.normalize(name)):continue
        action=None; description=None
        if item in pending:
            key=bindings[name]
            action={'kind':'TYPE_TEXT','target_id':item['id'],'input_key':key}
            description='Fill '+name+' with '+inputs[key]
        elif not pending and 'CLICK' in actions and 'TYPE_TEXT' not in actions:
            action={'kind':'CLICK','target_id':item['id']};description='Click '+name
        if action:
            identifier='a'+str(len(table));table[identifier]=action
            candidates.append({'id':identifier,'description':description})
    candidates += [{'id':'reobserve','description':'Refresh because no safe progress action is currently available'},
                   {'id':'abstain','description':'Stop because the task is blocked or unsupported'}]
    request={'schema':'jev.action_choice_request_v1','goal':goal,'observation_id':observation_id,
             'regions':regions,'history':list(history),'candidates':candidates}
    validate(request)  # Limits/privacy checked before the chooser can make a request.
    return request,table


def select(goal: str, observation_id: str, elements: Sequence[Mapping[str, Any]], *,
           timeout: float = 3.0, bindings: Optional[Mapping[str, str]] = None,
           inputs: Optional[Mapping[str, str]] = None, history: Sequence[Mapping[str, str]] = ()) -> Dict[str, Any]:
    request,table=build(goal,observation_id,elements,bindings=bindings,inputs=inputs,history=history)
    pick=choose(request,timeout=timeout)
    confidence = pick.get('confidence')
    safe = (pick.get('observation_id') == observation_id
            and isinstance(confidence, (float, int)) and not isinstance(confidence, bool)
            and MIN_CONFIDENCE <= confidence <= 1)
    return {'choice':pick,'action':table.get(str(pick.get('selected_id'))) if safe else None}
