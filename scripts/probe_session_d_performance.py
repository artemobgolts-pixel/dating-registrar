"""Воспроизводимое локальное измерение D: тестовая БД, интеграции отключены.

Запуск тестовым Python: scripts/probe_session_d_performance.py --output artifacts/fix-d-perf/before
CDP throttling — эмуляция, не field INP. Traces/JSON сохраняются в выбранной папке.
"""
import argparse
import hashlib
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from playwright.sync_api import sync_playwright

OBSERVE = """() => {
  window.lab = {lcp: 0, tasks: [], events: [], lcpEntries: []};
  performance.mark('lab-navigation-script');
  new PerformanceObserver(list => list.getEntries().forEach(e => {
    lab.lcp = e.startTime;
    lab.lcpEntries.push({start:e.startTime, render:e.renderTime, load:e.loadTime,
      size:e.size, url:e.url, html:e.element && e.element.outerHTML.slice(0,1500)});
  }))
    .observe({type: 'largest-contentful-paint', buffered: true});
  new PerformanceObserver(list => list.getEntries().forEach(e => lab.tasks.push({start:e.startTime, duration:e.duration})))
    .observe({type: 'longtask', buffered: true});
  new PerformanceObserver(list => list.getEntries().forEach(e => {if(e.interactionId) lab.events.push({name:e.name,duration:e.duration});}))
    .observe({type: 'event', buffered: true, durationThreshold: 16});
}"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--detailed-trace', action='store_true',
                        help='Добавить RunTask, navigation и стеки; сравнивать только одинаковый режим tracing.')
    parser.add_argument('--source-root', type=Path, default=ROOT,
                        help='Optional extracted baseline checkout; never modifies that source.')
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    source_root = args.source_root.resolve()
    sys.path.insert(0, str(source_root / 'tests'))
    from live_backend import LiveBackend
    backend = LiveBackend()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            result = {'browser': browser.version, 'python': sys.version, 'viewport': [390, 844],
                      'cpu_rate': 4, 'latency_ms': 150, 'download_bps': 1600000,
                      'upload_bps': 750000, 'observation': 'navigation through load + 8s; interaction separately',
                      'appearance': 'cold friends/light; warm friends/dark from preceding theme interaction',
                      'serving': 'local FastAPI HTTP actual hashed assets, no Caddy/compression/TLS',
                      'source_root': str(source_root),
                      'detailed_trace': args.detailed_trace,
                      'landing_sha256': hashlib.sha256((source_root / 'app/static/landing-story.js').read_bytes()).hexdigest(),
                      'runs': []}
            for index in range(args.runs):
                context = browser.new_context(viewport={'width':390, 'height':844},
                                              is_mobile=True, has_touch=True, device_scale_factor=1)
                context.add_init_script('(' + OBSERVE + ')()')
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                cdp = context.new_cdp_session(page)
                cdp.send('Emulation.setCPUThrottlingRate', {'rate': 4})
                cdp.send('Network.enable')
                cdp.send('Network.emulateNetworkConditions', {'offline':False, 'latency':150,
                         'downloadThroughput':1600000 / 8, 'uploadThroughput':750000 / 8})
                for cache in ('cold', 'warm'):
                    if cache == 'cold':
                        cdp.send('Network.clearBrowserCache')
                    trace = index == 0 and cache == 'cold'
                    if trace:
                        categories = 'devtools.timeline,v8.execute,disabled-by-default-v8.cpu_profiler'
                        if args.detailed_trace:
                            categories += ',toplevel,blink.user_timing,loading,disabled-by-default-devtools.timeline.stack'
                        cdp.send('Tracing.start', {'categories':categories,
                                                  'transferMode':'ReturnAsStream'})
                    page.goto(backend.url + '/', wait_until='load')
                    page.wait_for_timeout(8000)
                    metrics = page.evaluate('''() => ({...lab,
                      timeOrigin:performance.timeOrigin, clockNow:performance.now(),
                      paints:performance.getEntriesByType('paint').map(e=>({name:e.name,start:e.startTime})),
                      navigation:performance.getEntriesByType('navigation').map(e=>({load:e.loadEventEnd,domContentLoaded:e.domContentLoadedEventEnd})),
                      fonts:[...document.fonts].map(f=>({family:f.family,status:f.status})),
                      ink:window.__inkStats ? window.__inkStats() : null,
                      skin: document.documentElement.dataset.skin, theme: document.documentElement.dataset.theme,
                      bytes: performance.getEntriesByType('resource').reduce((s,e)=>s+e.transferSize,0),
                      resources: performance.getEntriesByType('resource').map(e=>({name:e.name.split('/').pop(),bytes:e.transferSize,
                        start:e.startTime,end:e.responseEnd,type:e.initiatorType,decoded:e.decodedBodySize})),
                      triggers: window.ScrollTrigger ? ScrollTrigger.getAll().length : 0})''')
                    metrics['blocking_ms'] = sum(max(0, task['duration'] - 50) for task in metrics['tasks'])
                    metrics['long_tasks'] = len(metrics['tasks'])
                    metrics['max_task_ms'] = max((task['duration'] for task in metrics['tasks']), default=0)
                    # Реальный клик после загрузки; ожидание автопрокрутки locator не входит в метрику.
                    page.evaluate('''() => document.querySelector('[data-theme-toggle]').addEventListener('click', () => {
                      const start = performance.now(); requestAnimationFrame(() => requestAnimationFrame(() => window.labPaint = performance.now()-start));
                    }, {once:true})''')
                    page.locator('[data-theme-toggle]').first.click()
                    page.wait_for_timeout(500)
                    metrics['click_to_two_frames_ms'] = page.evaluate('window.labPaint')
                    metrics['interaction_events'] = page.evaluate('lab.events')
                    metrics.update({'index':index + 1, 'cache':cache, 'page_errors':list(errors)})
                    if trace:
                        completed = []
                        cdp.on('Tracing.tracingComplete', lambda event: completed.append(event))
                        cdp.send('Tracing.end')
                        while not completed:
                            page.wait_for_timeout(100)
                        stream = completed[0]['stream']
                        with (output / 'startup-trace.json').open('w', encoding='utf-8') as target:
                            while True:
                                chunk = cdp.send('IO.read', {'handle':stream})
                                target.write(chunk['data'])
                                if chunk.get('eof'):
                                    break
                        cdp.send('IO.close', {'handle':stream})
                    page.screenshot(path=str(output / f'{index + 1}-{cache}.png'))
                    result['runs'].append(metrics)
                    print(json.dumps({key:metrics[key] for key in ('index','cache','lcp','blocking_ms','long_tasks','max_task_ms','click_to_two_frames_ms','page_errors')}), flush=True)
                    # Warm navigation сохраняет theme cookie от этого взаимодействия.
                    page.evaluate("localStorage.clear()")
                context.close()
            result['medians'] = {cache: {key:statistics.median(run[key] for run in result['runs'] if run['cache'] == cache)
                                for key in ('lcp','blocking_ms','long_tasks','max_task_ms','click_to_two_frames_ms','bytes')}
                                for cache in ('cold','warm')}
            (output / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(result['medians']), flush=True)
            browser.close()
    finally:
        backend.close()


if __name__ == '__main__':
    main()
