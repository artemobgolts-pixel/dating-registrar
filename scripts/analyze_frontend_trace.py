"""Разбор одного cold CDP trace из probe_session_d_performance.py.

Категории — неперекрывающиеся интервалы вложенных trace slices, не CPU utilization.
Окно startup заканчивается в load+8s, до отдельного тестового theme click.
Для точных RunTask/навигации снимайте trace с --detailed-trace. Старый формат
восстанавливает часы через EventTiming. Ожидаются results.json и startup-trace.json.
"""
import argparse
import collections
import json
import statistics
from pathlib import Path


def category(name):
    if name == 'EvaluateScript': return 'Script Evaluation'
    if name in ('FunctionCall', 'RunMicrotasks'): return 'Function Call'
    if name in ('Layout', 'UpdateLayoutTree', 'ParseAuthorStyleSheet'): return 'Style / Layout'
    if name in ('Paint', 'PrePaint', 'Layerize', 'CompositeLayers'): return 'Paint / Composite'
    if name in ('ParseHTML','DecodedDataDocumentParser::AppendBytes'): return 'Parse HTML'
    if 'image' in name.lower() and 'decode' in name.lower(): return 'Image decode'
    if name.startswith('V8.GC') or name in ('MinorGC', 'MajorGC'): return 'GC'
    if name in ('v8.compile', 'v8.parse'): return 'Compile / Parse JS'
    return None


def exclusive(events, start, end):
    boundaries = {start, end}
    selected = []
    for event in events:
        kind = category(event['name'])
        a, b = max(start, event['ts']), min(end, event['ts'] + event.get('dur', 0))
        if kind and a < b:
            selected.append((a, b, event['ts'], event.get('dur', 0), kind))
            boundaries.update((a, b))
    result = collections.Counter()
    ordered = sorted(boundaries)
    for a, b in zip(ordered, ordered[1:]):
        active = [e for e in selected if e[0] <= a and e[1] >= b]
        if active:
            event = max(active, key=lambda e: (e[2], -e[3]))
            result[event[4]] += (b - a) / 1000
    return {key: round(value, 3) for key, value in result.items()}


def profile(events, pid, tid):
    found = next(e for e in events if e['name'] == 'Profile' and e['pid'] == pid and e['tid'] == tid)
    clock = found['args']['data']['startTime']
    nodes, samples = {}, []
    for event in events:
        if event['name'] != 'ProfileChunk' or event['pid'] != pid or event.get('id') != found.get('id'):
            continue
        data = event['args']['data']
        cpu = data.get('cpuProfile', {})
        nodes.update({n['id']: n for n in cpu.get('nodes', [])})
        for sample, delta in zip(cpu.get('samples', []), data.get('timeDeltas', [])):
            samples.append((clock, clock + delta, sample))
            clock += delta
    return nodes, samples


def stack_label(nodes, sample):
    frames = []
    while sample in nodes:
        node = nodes[sample]
        frame = node['callFrame']
        url = frame.get('url', '').split('/static/')[-1]
        frames.append({'function': frame.get('functionName', ''), 'script': url,
                       'line': frame.get('lineNumber', -1) + 1,
                       'column': frame.get('columnNumber', -1) + 1})
        sample = node.get('parent')
    return list(reversed(frames))


def sampled_stacks(nodes, samples, start, end):
    counter = collections.Counter()
    for a, b, sample in samples:
        overlap = min(b, end) - max(a, start)
        if overlap > 0:
            counter[json.dumps(stack_label(nodes, sample), sort_keys=True)] += overlap / 1000
    return [{'sampled_ms': round(ms, 3), 'stack': json.loads(stack)} for stack, ms in counter.most_common(10)]


def analyze(directory):
    events = json.loads((directory / 'startup-trace.json').read_text())['traceEvents']
    run = json.loads((directory / 'results.json').read_text())['runs'][0]
    thread = next(e for e in events if e['name'] == 'thread_name' and e['args']['name'] == 'CrRendererMain')
    pid, tid = thread['pid'], thread['tid']
    main = [e for e in events if e['pid'] == pid and e['tid'] == tid]
    timed = [e for e in main if e['name'] == 'EventTiming' and 'timeStamp' in e.get('args', {}).get('data', {})]
    origin = statistics.median(e['ts'] - e['args']['data']['timeStamp'] * 1000 for e in timed)
    origin_range = [min(e['ts'] - e['args']['data']['timeStamp'] * 1000 for e in timed),
                    max(e['ts'] - e['args']['data']['timeStamp'] * 1000 for e in timed)]
    paint = next(e['ts'] for e in main if e['name'] == 'firstPaint')
    lcp_event = [e for e in main if e['name'] == 'largestContentfulPaint::Candidate'][-1]
    lcp = lcp_event['ts']
    frame = lcp_event['args']['frame']
    navigation = next((e for e in main if e['name'] == 'navigationStart' and e.get('args',{}).get('frame') == frame), None)
    if navigation: origin = navigation['ts']
    load = next(e['ts'] for e in main if e['name'] == 'MarkLoad' and e['args'].get('data', {}).get('frame') == frame)
    end = load + 8_000_000
    slices = [e for e in main if e.get('ph') == 'X' and e.get('dur', 0) > 0]
    nodes, samples = profile(events, pid, tid)
    tasks = []
    for task in run['tasks']:
        start, finish = origin + task['start'] * 1000, origin + (task['start'] + task['duration']) * 1000
        run_task = next((e for e in slices if e['name']=='ThreadControllerImpl::RunTask' and abs(e['ts']-start)<1000 and e['dur']>50_000),None)
        if run_task: start, finish = run_task['ts'], run_task['ts']+run_task['dur']
        relevant = [e for e in slices if e['ts'] >= start-2000 and e['ts'] + e['dur'] <= finish+2000 and e['dur'] > 5000]
        tasks.append({**task, 'exact_run_task':run_task, 'position': 'pre-LCP' if finish <= lcp else 'crosses LCP' if start < lcp else 'post-LCP',
                      'exclusive_categories': exclusive(slices, start, finish),
                      'slices_over_5ms': [{'name':e['name'], 'start_ms':round((e['ts']-origin)/1000,3),
                                          'duration_ms':e['dur']/1000, 'args':e.get('args',{})} for e in relevant],
                      'sampled_stacks': sampled_stacks(nodes, samples, start, finish)})
    return {'index':directory.name, 'origin_trace_us':origin, 'origin_inference_spread_us':origin_range,
            'first_paint_ms':round((paint-origin)/1000,3), 'trace_lcp_ms':round((lcp-origin)/1000,3),
            'reported_lcp_ms':run['lcp'], 'lcp':lcp_event['args'], 'window_end_ms':round((end-origin)/1000,3),
            'exclusive_categories': {'before_first_paint':exclusive(slices,origin,paint),
                                     'first_paint_to_lcp':exclusive(slices,paint,lcp),
                                     'post_lcp_to_load_plus_8s':exclusive(slices,lcp,end),
                                     'total':exclusive(slices,origin,end)},
            'tasks': tasks,
            'script_evaluation': [{'url':e['args']['data'].get('url'),'ms':e['dur']/1000,
                                   'exclusive':exclusive(slices,e['ts'],e['ts']+e['dur'])}
                                  for e in slices if e['name']=='EvaluateScript'],
            'layout': [{'start_ms':round((e['ts']-origin)/1000,3), 'ms':e['dur']/1000, 'args':e['args']}
                       for e in slices if e['name']=='Layout' and e['dur']>50_000]}



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directories', type=Path, nargs='+')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = {'method': __doc__, 'runs': [analyze(path.resolve()) for path in args.directories]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    for run in result['runs']:
        print(json.dumps({key: run[key] for key in ('index','reported_lcp_ms','exclusive_categories')}, ensure_ascii=False))
    print(args.output)


if __name__ == '__main__':
    main()
