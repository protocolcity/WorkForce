"""Recovery against real Git worktrees, file locks and qualified runner configs.

WorkLane wire responses are isolated here; the owning engine tests its SQLite
checkpoint/CAS contract. No test launches a provider or writes a live desk.
"""
import copy
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from workforce import continuity_recovery as cr, routing_binding as rb, task_runner as tr
from workforce.roster import Roster, RosterError, Worker


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def stamp(minutes=0):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def case(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.email', 'test@example.invalid')
    git(repo, 'config', 'user.name', 'Test')
    (repo/'README.md').write_text('original\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'initial')
    git(repo, 'remote', 'add', 'origin', 'https://example.invalid/project')
    law = tmp_path/'AGENTS.md'; law.write_text('Claim before implementing.')
    prompt = tmp_path/'prompt.md'; prompt.write_text('{authority}\nClaim {task_id} as {worker}.')
    policy = tmp_path/'policy.json'
    configs, workers = {}, {}
    for name in ('primary', 'receiver'):
        configs[name] = dict(project='widgets', worker=name, desk_url='http://desk.test',
            repository=str(repo), expected_remote='https://example.invalid/project', base_ref='main',
            state_dir=str(tmp_path/'runs'), prompt_template=str(prompt), authority_chain=[str(law)],
            continuity_instructions=[str(law)], workspace_id='workspace-example',
            command=[sys.executable, '-c', 'pass'], required_label='execution:bounded')
        path=tmp_path/(name+'.json'); path.write_text(json.dumps(configs[name]))
        workers[name] = Worker(name=name, identity=name, workdir=str(tmp_path), contract=str(law),
            prompt=str(prompt), command=[sys.executable,'-m','workforce.task_runner','--config',str(path)],
            queue_url='http://desk.test/api/admin/tasks/ready?product=widgets',
            qualified_recovery=name=='primary', recovery_fallback_workers=['receiver'] if name=='primary' else [])
    task = dict(id='wd-1', product='widgets', status='backlog', title='Preserve the edit',
        description='Resume safely', updated_at='v1', labels=['worker:primary','execution:bounded','work-kind:implement','risk:low'])
    def feed(url):
        if '/ready?' in url:
            return {'ok':True, 'count':1, 'product':'widgets', 'tasks':[copy.deepcopy(task)]}
        return {'ok':True, 'product':'widgets', 'task':copy.deepcopy(task)}
    monkeypatch.setenv('WL_AGENT_ID','primary')
    prepared=tr.prepare(configs['primary'], feed)
    receipt=json.loads(Path(prepared['receipt']).read_text())
    receipt['execution_lock_protocol']=tr.LOCK_PROTOCOL_VERSION  # simulate the prior guarded executor
    Path(prepared['receipt']).write_text(json.dumps(receipt))
    Path(prepared['lock']).touch()
    checkout=Path(prepared['checkout']); (checkout/'README.md').write_text('partial verified edit\n')
    checkpoint=dict(version=1, project='widgets', workspace_id='workspace-example', objective='Preserve the edit',
        acceptance='Regression passes', scope='Owned checkout', instruction_revision=cr.instruction_revision(configs['primary']),
        source_revision=git(checkout,'rev-parse','HEAD'), branch=receipt['branch'], decisions=[],
        artifacts=[dict(ref='README.md',sha256=hashlib.sha256((checkout/'README.md').read_bytes()).hexdigest(),access='local checkout')],
        checks=['regression passed'], remaining=['review'], next_action='Review partial edit')
    task.update(status='in_progress', comments=[dict(id='cp-1', author='primary', body=cr.CHECKPOINT_PREFIX+json.dumps(checkpoint))])
    rows, results = [], []
    for name, provider in (('primary','claude'),('receiver','grok')):
        configs[name].update(routing_policy=str(policy),routing_host='test-host')
        Path(workers[name].command[-1]).write_text(json.dumps(configs[name]))
        row=dict(version=1,worker=name,provider=provider,model='fixture-model',reasoning_effort='low',
            host='test-host',tools=['wl_show','wl_claim'],project='widgets',account_state='authenticated',
            observed_at=stamp(-1),expires_at=stamp(60),supported_efforts=['low'],
            quota=dict(pool='subscription',pool_id=name,units='requests',remaining=5,observed_at=stamp(-1)),
            runner_sha256=rb.runner_digest(configs[name]),worker_sha256=rb.worker_digest(workers[name]),
            max_run_units=1,budget_units='requests')
        rows.append(row)
        results.append(dict(candidate_id=provider+'/fixture-model@low#test-host::widgets',task_id='edit-01',
                            accepted=True,regressions=0,retries=0,observed_at=stamp(-1)))
    policy.write_text(json.dumps(dict(version=1,seats=rows,evaluation_results=results)))
    roster=Roster(workers=workers,path=str(tmp_path/'roster.json'))
    local=str(tmp_path/'local')
    args=dict(local_root=local,primary=workers['primary'],roster=roster,task=task,receipt_path=prepared['receipt'],
        interruption=cr.classify_interruption('vendor limit: 429 rate limit exceeded'),policy_path=str(policy),host='test-host',
        workspace_id='workspace-example',instruction_revision=checkpoint['instruction_revision'])
    plan=cr.plan_qualified_recovery(**args)
    assert plan['status']=='pending', json.dumps(plan, indent=2)
    calls=[]
    def post(url,payload):
        calls.append(('handoff',payload))
        assert payload['checkpoint_id']=='cp-1' and payload['expected_version']==task['updated_at']
        # Exclusion is held across the owning-engine mutation, including another process.
        script='import fcntl,os,sys; f=os.open(sys.argv[1],os.O_RDWR); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)'
        assert subprocess.run([sys.executable,'-c',script,prepared['lock']],capture_output=True).returncode != 0
        task.update(status='backlog',updated_at='v2',labels=[x if x!='worker:primary' else 'worker:receiver' for x in task['labels']])
        return dict(ok=True,task=copy.deepcopy(task))
    def dispatch(name, lr, receipt_path, reason, *, routing_context):
        calls.append(('dispatch',routing_context))
        assert name=='receiver' and receipt_path==prepared['receipt']
        monkeypatch.setenv('WL_AGENT_ID',name)
        monkeypatch.setenv(rb.CONTEXT_ENV,json.dumps(routing_context))
        result=tr.recover(configs[name],receipt_path,reason,feed)
        fd=tr._acquire_lock(result['lock'])
        try:
            tr._revalidate_launch(configs[name],result,feed)
        finally:
            os.close(fd)
        task['comments'].append(dict(id='claim-2',author=name,body='Owner: '+name+'\nPlan:\n- Resume'))
        task.update(status='in_progress',updated_at='v3')
        return 0
    c=SimpleNamespace(**locals())
    c.run=lambda **kw: cr.execute_pending_recovery(local_root=local,roster=roster,plan=plan,
        fetch_task=lambda w,t: copy.deepcopy(task),post_fn=kw.pop('post_fn',post),
        dispatch_fn=kw.pop('dispatch_fn',dispatch),**kw)
    return c


def test_qualified_handoff_preserves_checkout_and_revalidates_actual_runner(case):
    result=case.run()
    assert result['ok'], result
    assert [c[0] for c in case.calls]==['handoff','dispatch']
    assert (case.checkout/'README.md').read_text()=='partial verified edit\n'
    assert result['plan']['status']=='resumed'
    assert not case.run()['ok']  # replay cannot hand off/dispatch twice
    assert len(case.calls)==2


def test_dry_run_checks_real_facts_without_writes_or_dispatch(case):
    before=Path(cr.plan_path(case.local,'wd-1')).read_bytes()
    assert case.run(dry_run=True)['ok']
    assert not case.calls
    assert Path(cr.plan_path(case.local,'wd-1')).read_bytes()==before
    assert case.task['status']=='in_progress'


@pytest.mark.parametrize('change', ['file','new-file','head','instructions','checkpoint','foreign-checkpoint',
    'owner','project','human-gate','quota','policy','workspace','lock-missing','legacy-lock','unverified-wrapper','auth','permission','cancel'])
def test_changed_or_uncertain_facts_refuse_without_handoff(case,change):
    if change=='file': (case.checkout/'README.md').write_text('unverified edit')
    elif change=='new-file': (case.checkout/'new.txt').write_text('uncheckpointed')
    elif change=='head':
        git(case.checkout,'add','.');git(case.checkout,'commit','-m','another revision')
    elif change=='instructions': case.law.write_text('Different rules')
    elif change=='checkpoint': case.task['comments']=[]
    elif change=='foreign-checkpoint': case.task['comments'].append(dict(case.task['comments'][0],author='someone-else'))
    elif change=='owner': case.task['labels'][0]='worker:another'
    elif change=='project': case.task['product']='foreign'
    elif change=='human-gate': case.task['gate_type']='human'
    elif change in ('quota','policy'):
        d=json.loads(case.policy.read_text())
        if change=='quota': d['seats'][1]['quota']['remaining']=0
        else: d['seats'][1]['runner_sha256']='changed'
        case.policy.write_text(json.dumps(d))
    elif change=='workspace':
        d=case.configs['receiver'];d['workspace_id']='other';Path(case.workers['receiver'].command[-1]).write_text(json.dumps(d))
    elif change=='lock-missing': Path(case.prepared['lock']).unlink()
    elif change=='unverified-wrapper':
        d=case.receipt;d.pop('execution_lock_protocol');Path(case.prepared['receipt']).write_text(json.dumps(d))
    elif change=='legacy-lock':
        d=case.receipt;d.pop('lock_protocol');Path(case.prepared['receipt']).write_text(json.dumps(d))
    else:
        case.plan['interruption']['reason']={'auth':'authentication failed quota','permission':'permission denied quota','cancel':'user decision: canceled quota'}[change]
        cr._save_plan(cr.plan_path(case.local,'wd-1'),case.plan)
    result=case.run()
    assert not result['ok'], (change,result)
    assert not case.calls


def test_live_reservation_and_cross_process_operation_locks_refuse(case):
    fd=tr._acquire_lock(case.prepared['lock'])
    try:
        assert not case.run()['ok']
    finally: os.close(fd)
    with cr._operation_lock(cr.plan_path(case.local,'wd-1')):
        assert not case.run()['ok']
    assert not case.calls


def test_handoff_response_uncertainty_cannot_retry_or_reset_budget(case):
    def uncertain(url,payload):
        case.post(url,payload)
        raise TimeoutError('lost response after committed handoff')
    result=case.run(post_fn=uncertain)
    assert not result['ok']
    assert result['plan']['status']=='needs_reconciliation'
    preserved=cr.plan_qualified_recovery(**case.args)
    assert preserved['attempts_used']==1 and preserved['status']=='needs_reconciliation'
    assert not case.run()['ok'] and len(case.calls)==1


def test_unclaimed_dispatch_retries_do_not_repeat_ownership_transfer(case):
    assert not case.run(dispatch_fn=lambda *a,**kw: 1)['ok']
    assert not case.run()['ok']  # cooldown blocks
    p=cr._load_plan(cr.plan_path(case.local,'wd-1'))
    assert p['attempts_used']==1 and p['transfer_state']=='transferred'
    p['cooldown_until']=stamp(-1);cr._save_plan(cr.plan_path(case.local,'wd-1'),p)
    assert case.run()['ok']
    assert [c[0] for c in case.calls]==['handoff','dispatch']


def test_file_changes_after_transfer_are_rejected_at_provider_launch(case):
    def changed(*args,**kwargs):
        (case.checkout/'README.md').write_text('changed after handoff')
        return case.dispatch(*args,**kwargs)
    result=case.run(dispatch_fn=changed)
    assert not result['ok'] and result['plan']['status']=='needs_reconciliation'
    assert case.task['status']=='backlog'


def test_shared_exhausted_pool_never_selects_another_seat_on_same_pool(case):
    d=json.loads(case.policy.read_text());d['seats'][1]['quota']['pool_id']='primary'
    case.policy.write_text(json.dumps(d))
    p=case.plan;p['exhausted_pools']=['subscription:primary']
    cr._save_plan(cr.plan_path(case.local,'wd-1'),p)
    assert not case.run()['ok'] and not case.calls


def test_roster_requires_explicit_boolean_and_unique_fallbacks(case):
    case.workers['primary'].qualified_recovery='false'
    with pytest.raises(RosterError,match='boolean'): case.workers['primary'].validate()
    case.workers['primary'].qualified_recovery=True
    case.workers['primary'].recovery_fallback_workers=['receiver','receiver']
    with pytest.raises(RosterError,match='unique'): case.workers['primary'].validate()


def test_missing_checkpoint_is_paused_and_never_fabricated(case):
    case.task['id']='wd-2';case.task['comments']=[]
    p=cr.plan_qualified_recovery(**case.args)
    assert p['status']=='paused' and p['pause_reason']=='missing signed WorkLane checkpoint'
    assert any(v['status']=='paused' for v in cr.list_pause_states(case.local))


def test_engine_uses_prepared_receipt_not_first_ready_row(case,monkeypatch):
    from workforce import engine
    Path(cr.plan_path(case.local,'wd-1')).unlink()  # first creation by the engine
    primary=case.workers['primary']
    primary.command=[sys.executable,'-c',"print('Prepared wd-1; not yet claimed. Receipt: '+"+repr(case.prepared['receipt'])+"); print('429 rate limit exceeded'); raise SystemExit(1)"]
    primary.min_free_mb=1
    primary.budget_secs=10
    monkeypatch.setattr(engine,'_probe_ready',lambda *a,**k: (2,[dict(case.task,id='wd-unrelated',status='backlog'),dict(case.task,status='backlog')]))
    monkeypatch.setattr(engine,'_qualified_recovery_context',lambda w: (case.configs['primary'],str(case.policy),'test-host'))
    monkeypatch.setattr(engine,'_roster_for_local_root',lambda p: case.roster)
    monkeypatch.setattr(engine,'_http_json',lambda method,url,*a,**k: {'ok':True,'task':dict(case.task,status='backlog')})
    assert engine.dispatch(primary,case.local,routing_context={'task_id':'wd-1'})==1
    plan=cr._load_plan(cr.plan_path(case.local,'wd-1'))
    assert plan['task_id']=='wd-1' and plan['status']=='pending'
    assert not Path(cr.plan_path(case.local,'wd-unrelated')).exists()
    assert not case.calls  # planning never changes WorkLane ownership or signs a checkpoint


@pytest.mark.parametrize('blocked',['project','worker','policy','capacity','busy','cron'])
def test_supervisor_recovery_cannot_escape_scope_or_capacity(case,monkeypatch,blocked):
    from workforce import supervisor as s
    config=dict(local_root=case.local, projects=['widgets'],workers=['receiver'],
        routing_policy=str(case.policy),routing_host='test-host')
    monkeypatch.setattr(s.engine,'_probe_ready',lambda *a,**k:(0,[]))
    monkeypatch.setattr(s.engine,'lock_inspect',lambda *a,**k:None)
    monkeypatch.setattr(s.pq_mod,'dispatch_blocked_by_capacity',lambda *a,**k:'at capacity' if blocked=='capacity' else None)
    if blocked=='project': config['projects']=[]
    if blocked=='worker': config['workers']=[]
    if blocked=='policy': config['routing_policy']='other-policy'
    if blocked=='busy': monkeypatch.setattr(s.engine,'lock_inspect',lambda *a,**k:{'orphan':False})
    if blocked=='cron': case.workers['receiver'].schedule='* * * * *'
    monkeypatch.setattr(cr,'execute_pending_recovery',lambda **kw:pytest.fail('out-of-scope dispatch'))
    assert s._execute_pending_recoveries(config,case.roster)==[]


def test_supervisor_recovers_without_ready_work_or_provider_call(case,monkeypatch):
    from workforce import supervisor as s
    config=dict(local_root=case.local,roster_path=case.roster.path,projects=['widgets'],workers=['receiver'],
        routing_policy=str(case.policy),routing_host='test-host')
    monkeypatch.setattr(s,'_load_routing',lambda c:{})
    monkeypatch.setattr(s,'collect_state',lambda c:dict(generated_at=stamp(),workers={},projects=['widgets']))
    monkeypatch.setattr(s.roster_mod,'load',lambda **kw:case.roster)
    monkeypatch.setattr(s,'_execute_pending_recoveries',lambda *a:[{'ok':False,'reason':'cooldown active'}])
    monkeypatch.setattr(s,'_run_provider',lambda *a:pytest.fail('recovery needs no planning provider'))
    result=s.run(config,mode='execute')
    assert result['provider_skipped'] and result['pass_outcome']=='qualified_recovery_pass'
