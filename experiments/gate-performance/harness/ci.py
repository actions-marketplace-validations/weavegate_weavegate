"""GitHub Actions attempt-specific provider evidence and interval arithmetic."""
import subprocess

from common import read_json, timestamp, write_json

TASKS = ('go', 'java', 'explore', 'replay', 'rollback', 'death', 'cancellation',
         'accounting', 'upload')


def snapshot(repository, run_id, attempt, output):
    if attempt != 1:
        raise ValueError('primary collection accepts original attempt 1 only')
    def api(path):
        result = subprocess.run(['gh', 'api', path], check=True, capture_output=True, text=True)
        import json
        return json.loads(result.stdout)
    prefix = 'repos/%s/actions/runs/%s/attempts/%s' % (repository, run_id, attempt)
    run = api(prefix)
    pages = []
    page = 1
    while True:
        body = api(prefix + '/jobs?per_page=100&page=%d' % page)
        pages.append(body)
        if len(body['jobs']) < 100:
            break
        page += 1
    write_json(output, {'repository': repository, 'run': run, 'pages': pages})


def endpoint(snapshot, inventory):
    run = snapshot['run']
    if (run.get('status') != 'completed' or run['run_attempt'] != 1 or run['head_sha'] != inventory['execution_sha'] or
            snapshot['repository'] != inventory['repository']):
        raise ValueError('CI attempt/repository/revision mismatch')
    pages = snapshot['pages']
    if not pages or any(p['total_count'] != pages[0]['total_count'] for p in pages):
        raise ValueError('inconsistent jobs pages')
    jobs = [job for page in pages for job in page['jobs']]
    if len(jobs) != pages[0]['total_count'] or len({j['id'] for j in jobs}) != len(jobs):
        raise ValueError('missing or duplicated jobs pages')
    if any(j['run_id'] != run['id'] or j['run_attempt'] != 1 or
           j['head_sha'] != inventory['execution_sha'] for j in jobs):
        raise ValueError('jobs belong to another run/attempt/revision')
    if not set(TASKS) <= set(inventory['tasks']):
        raise ValueError('incomplete frozen logical acceptance bundle')
    completed = []
    for task, mapping in inventory['tasks'].items():
        found = [j for j in jobs if j['name'] == mapping['job']]
        if len(found) != 1 or found[0]['conclusion'] != 'success':
            raise ValueError('required logical job incomplete: ' + task)
        job = found[0]
        if not job.get('completed_at'):
            raise ValueError('missing required job completion: ' + task)
        steps = [s for s in job['steps'] if s['name'] == mapping['step']]
        if len(steps) != 1 or steps[0]['conclusion'] != 'success' or not steps[0].get('completed_at'):
            raise ValueError('required logical step incomplete: ' + task)
        completed.append(timestamp(job['completed_at']))
    intervals = []
    for job in jobs:
        if job.get('started_at') is None or not job.get('runner_id'):
            continue
        # A skipped job never acquired a runner, even if the API supplies timestamps.
        if job['conclusion'] == 'skipped':
            continue
        if not job.get('completed_at'):
            raise ValueError('censored runner use')
        start, end = timestamp(job['started_at']), timestamp(job['completed_at'])
        if end < start:
            raise ValueError('negative provider duration')
        intervals.append((start, end))
    if not intervals:
        raise ValueError('no started jobs')
    first, bundle, created = min(a for a, _ in intervals), max(completed), timestamp(run['created_at'])
    if created > first or bundle < first or (bundle - created).total_seconds() > 5400:
        raise ValueError('invalid/capped workflow boundaries')
    events = [(a, 1) for a, b in intervals if b > a] + [(b, -1) for a, b in intervals if b > a]
    active = peak = 0
    for _, delta in sorted(events):  # End (-1) precedes start (+1): half-open intervals.
        active += delta
        peak = max(peak, active)
    return {'validation_seconds': (bundle - first).total_seconds(),
            'queue_inclusive_seconds': (bundle - created).total_seconds(),
            'initial_queue_seconds': (first - created).total_seconds(),
            'runner_minutes': sum((b - a).total_seconds() / 60 for a, b in intervals),
            'peak_runner_concurrency': peak}
