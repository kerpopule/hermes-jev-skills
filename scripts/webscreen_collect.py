#!/usr/bin/env python3
"""Authorized, bounded frozen-test collector. No reserve inference or raw logs.

Standalone only: scoped client interception is not safe inside a serving process.
Uses the existing key resolver/client validator and a single-POST, no-retry TLS
transport to the exact TypeSafe origin. Output is hash-only, private by default.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from jevkit import client, webscreen
import webscreen_heldout as held
import webscreen_preflight as preflight

MODEL = 'jev-1.13.0'
PRICE = Decimal('0.042') / Decimal(1_000_000)
# Documented model context is 64k. Reserve 65,536 billed input tokens per POST.
MAX_TOKENS = 65536
MAX_CALL_USD = Decimal(MAX_TOKENS) * PRICE


class Budget:
    def __init__(self, cap):
        self.cap = Decimal(cap)
        if not self.cap.is_finite() or not 0 < self.cap <= 5:
            raise ValueError('cap must be finite, positive and no more than approved $5')
        self.spent = Decimal(0); self.pending = 0; self.stopped = False
        self.lock = threading.Lock()

    def upper(self):
        return self.spent + self.pending * MAX_CALL_USD

    def reserve(self):
        with self.lock:
            if self.stopped or self.upper() + MAX_CALL_USD > self.cap:
                return False
            self.pending += 1
            return True

    def settle(self, tokens, *, stop_unknown=True):
        with self.lock:
            self.pending -= 1
            if type(tokens) is not int or not 0 <= tokens <= MAX_TOKENS:
                self.spent += MAX_CALL_USD
                self.stopped = self.stopped or stop_unknown
            else:
                self.spent += Decimal(tokens) * PRICE


def one_post(body, headers, timeout):
    """One POST, no reconnect/retry/redirect and no error response body in logs."""
    connection = http.client.HTTPSConnection('api.typesafe.ai', 443, timeout=timeout)
    try:
        connection.request('POST', '/v1/systemone', body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(client.MAX_RESPONSE_BYTES + 1)
        if len(raw) > client.MAX_RESPONSE_BYTES:
            raise client.JevError('response_too_large')
        if response.status != 200:
            code = {401:'auth_failed',403:'auth_failed',429:'rate_limited'}.get(response.status, f'http_{response.status}')
            raise client.JevError(code)
        return raw
    except (OSError, http.client.HTTPException):
        raise client.JevError('network') from None
    finally:
        connection.close()


def prior_verified(directory, manifest, plan):
    """Only resume proven never-sent rows; preserve every sent outcome and cost."""
    directory = Path(directory)
    report = json.loads((directory/'report.json').read_text())
    receipt = json.loads((directory/'receipt.json').read_text())
    if report['receipt_sha256'] != held.digest(receipt) or report['plan_sha256'] != held.digest(plan):
        raise ValueError('prior receipt or plan mismatch')
    held.score(manifest, receipt)
    provenance = receipt['provenance']
    if (provenance['provider'] != 'typesafe' or provenance['model_requested'] != MODEL
            or provenance['threshold'] != plan['threshold']
            or provenance['question_sha256'] != plan['question_template_sha256']):
        raise ValueError('prior provider/model/question/threshold mismatch')
    source = directory/'all-calls.jsonl'
    if not source.exists(): source = directory/'calls.jsonl'
    calls = [json.loads(line) for line in source.read_text().splitlines()]
    planned = {row['id']:row for row in plan['rows']}
    sent = set(); spend = Decimal(0)
    for call in calls:
        if 'full_request_sha256' not in call: continue
        identifier = call['id']
        if identifier in sent or identifier not in planned or call['model_requested'] != MODEL:
            raise ValueError('duplicate or unknown prior attempt')
        requests = planned[identifier]['requests']
        if len(requests) != 1 or call['payload_sha256'] != requests[0]['screening_payload_sha256']:
            raise ValueError('prior payload mismatch')
        tokens = call.get('input_tokens')
        expected = Decimal(tokens)*PRICE if type(tokens) is int and 0 <= tokens <= MAX_TOKENS else MAX_CALL_USD
        if Decimal(call['cost_upper_usd']) != expected:
            raise ValueError('prior cost mismatch')
        spend += expected; sent.add(identifier)
    if spend != Decimal(report['spend_upper_usd']) or len(sent) != report['inference_calls']:
        raise ValueError('prior spend/count mismatch')
    carried = [row for row in receipt['outcomes'] if row['id'] in sent
               or (row['status']=='local_only' and not planned[row['id']]['requests'])]
    return calls, carried, spend, held.digest(receipt)


def collect(test_bytes, reserve_bytes, manifest, plan, output, cap, *,
            workers=8, transport=None, rate=10, deadline=540, prior=None):
    if os.environ.get('TYPESAFE_BASE_URL', '').strip():
        raise ValueError('proxy override is outside this approved run')
    if not 1 <= workers <= 8 or not 0 < rate <= 1000 or not 0 < deadline <= 540:
        raise ValueError('invalid bounded worker/rate/deadline settings')
    actual_plan = preflight.prepare(test_bytes, reserve_bytes, manifest)
    if actual_plan != plan or plan['threshold'] != webscreen.INJECTION_THRESHOLD:
        raise ValueError('source, detector, threshold or plan changed')
    if any(len(row['requests']) > 1 for row in plan['rows']):
        raise ValueError('this collector requires at most one planned request per row')
    if transport is None and rate > 10:
        raise ValueError('live rate exceeds the approved conservative rate')
    budget = Budget(cap)
    calls = []; outcomes = []; prior_hash = None
    if prior:
        calls, outcomes, budget.spent, prior_hash = prior_verified(prior, manifest, plan)
        if budget.upper() > budget.cap:
            raise ValueError('prior spend exceeds total approved cap')
    carried_ids = {row['id'] for row in outcomes}
    todo = [row for row in manifest['test'] if row['id'] not in carried_ids]
    output = Path(output); output.mkdir(parents=True, exist_ok=False)
    os.chmod(output, 0o700)
    original = client.ask; send = transport or one_post
    context = threading.local(); write_lock = threading.Lock(); pace_lock = threading.Lock()
    next_send = [time.monotonic()]; started = time.monotonic()
    planned = {row['id']: row for row in plan['rows']}
    import csv, io
    source_rows = list(csv.DictReader(io.StringIO(test_bytes.decode('utf-8-sig'), newline='')))

    def append(name, row):
        with write_lock:
            fd = os.open(str(output / name), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, 'a', encoding='utf-8') as file:
                file.write(json.dumps(row, sort_keys=True, allow_nan=False) + '\n')
                file.flush(); os.fsync(file.fileno())

    def ask(state, questions, **unused):
        row = context.row
        payload_hash = held.digest({'state':state, 'questions':questions})
        if payload_hash != planned[row['id']]['requests'][0]['screening_payload_sha256']:
            raise client.JevError('payload_changed')
        record = {'id':row['id'], 'payload_sha256':payload_hash, 'model_requested':MODEL,
                  'provider':'typesafe', 'started_at':time.time(), 'status':'not_sent'}
        context.record = record

        def metered(body, headers, timeout):
            with pace_lock:
                wait = max(0, next_send[0] - time.monotonic())
                if wait: time.sleep(wait)
                next_send[0] = time.monotonic() + 1/rate
            if not budget.reserve():
                raise client.JevError('budget_stopped')
            record.update(full_request_sha256=held.sha(body), status='attempt_started')
            append('attempts.jsonl', record)
            then = time.monotonic(); tokens = None; response_received = False
            try:
                raw = send(body, headers, timeout)
                response_received = True
                try:
                    response = json.loads(raw)
                    usage = response.get('usage', {})
                    candidate = usage.get('input_tokens')
                    if type(candidate) is int and 0 <= candidate <= MAX_TOKENS:
                        tokens = candidate
                    record['model_returned'] = response.get('model') if isinstance(response.get('model'),str) else None
                    record['input_tokens'] = tokens
                except (ValueError, TypeError, AttributeError):
                    pass
                return raw
            finally:
                record['elapsed_ms'] = int((time.monotonic()-then)*1000)
                record['cost_upper_usd'] = str(Decimal(tokens)*PRICE if tokens is not None else MAX_CALL_USD)
                budget.settle(tokens, stop_unknown=response_received)

        try:
            reply = original(state, questions, model=MODEL, provider='typesafe',
                             timeout=4.0, retries=0, transport=metered)
            if reply.get('jev_model') != MODEL:
                budget.stopped = True
                raise client.JevError('model_changed')
            if record.get('input_tokens') is None:
                raise client.JevError('usage_missing')
            record['status'] = 'ok'
            return reply
        except client.JevError as error:
            record['status'] = 'error'; record['error_code'] = error.code
            if error.code in ('no_key','auth_failed','model_changed','payload_changed'):
                budget.stopped = True
            raise
        finally:
            calls.append(record)
            append('calls.jsonl', record)

    def run(row):
        context.row = row; context.record = None
        if time.monotonic()-started > deadline or budget.stopped:
            outcome = {'id':row['id'], 'flagged':False, 'status':'skipped'}
        else:
            text = source_rows[row['source_index']]['text']
            verdict = webscreen.screen('web_extract', json.dumps({'results': [{'content': text}]}), timeout=4.0)
            record = context.record
            status = ('local_only' if not planned[row['id']]['requests'] else
                      'ok' if record and record['status']=='ok' else 'fail_open')
            outcome = {'id':row['id'], 'flagged':bool(verdict.get('flagged')), 'status':status}
        outcomes.append(outcome); append('outcomes.jsonl', outcome)
        return outcome

    # Approval and provenance persisted before any network call; no secret values.
    meta = {'provider':'typesafe','model':MODEL,'cap_usd':str(budget.cap),
            'manifest_sha256':held.digest(manifest),'plan_sha256':held.digest(plan),
            'rate_input_usd_per_million':'0.042','pricing_source':'https://docs.typesafe.ai/models',
            'reserved_input_tokens_per_call':MAX_TOKENS, 'no_retries':True,
            'threshold':webscreen.INJECTION_THRESHOLD, 'reserve_inference':False}
    meta['prior_receipt_sha256'] = prior_hash
    meta['prior_spend_upper_usd'] = str(budget.spent)
    append('approval.jsonl', meta)
    with patch.object(client, 'ask', new=ask), ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(run, todo))
    for call in calls:
        append('all-calls.jsonl', call)
    observed_models = sorted({c['model_returned'] for c in calls if c.get('model_returned')})
    provenance = {'provider':'typesafe','model_requested':MODEL,'model_returned':observed_models,
                  'question_sha256':plan['question_template_sha256'],
                  'detector_revision':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                  'threshold':webscreen.INJECTION_THRESHOLD,
                  'input_shape':'single dataset row as web_extract content'}
    receipt = {'manifest_sha256':held.digest(manifest), 'provenance':provenance,
               'outcomes':sorted(outcomes,key=lambda row:row['id'])}
    report = held.score(manifest, receipt)
    result = {'provider':'typesafe','model_requested':MODEL,'model_returned':observed_models,
              'inference_calls':sum('full_request_sha256' in c for c in calls),
              'known_input_tokens':sum(c.get('input_tokens') or 0 for c in calls),
              'spend_upper_usd':str(budget.upper()),'approved_cap_usd':str(budget.cap),
              'usage_priced_usd':str(sum(Decimal(c.get('input_tokens') or 0)*PRICE for c in calls)),
              'unknown_usage_calls':sum('full_request_sha256' in c and c.get('input_tokens') is None for c in calls),
              'elapsed_seconds':round(time.monotonic()-started,3),
              'all_eligible_rows_ok':all(o['status'] in ('ok','local_only') for o in outcomes),
              'report':report, 'receipt_sha256':held.digest(receipt), 'plan_sha256':held.digest(plan)}
    for name, obj in [('receipt.json',receipt),('report.json',result)]:
        fd=os.open(str(output/name),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as file:
            json.dump(obj,file,indent=2,sort_keys=True,allow_nan=False); file.write('\n')
    if budget.upper() > budget.cap:
        raise RuntimeError('budget invariant violated')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true',help='Requires explicit owner approval for this run')
    parser.add_argument('--test',type=Path,required=True)
    parser.add_argument('--reserve',type=Path,required=True)
    parser.add_argument('--manifest',type=Path,default=ROOT/'evals/web-screen/heldout-manifest-v1.json')
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--usd-cap',required=True)
    parser.add_argument('--prior',type=Path,help='Verified prior run; only never-sent rows may execute')
    args=parser.parse_args()
    if not args.execute: parser.error('nothing sent; add --execute only with exact owner approval')
    result=collect(args.test.read_bytes(),args.reserve.read_bytes(),json.loads(args.manifest.read_text()),
                   json.loads(args.plan.read_text()),args.out,args.usd_cap,prior=args.prior)
    print(json.dumps({key:value for key,value in result.items() if key!='report'},indent=2,sort_keys=True))


if __name__=='__main__': main()
