"""UI-проверка на тестовых данных; --axe задаёт локальную фиксированную версию axe.min.js."""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_session_d_ui import SessionDUiTests
from playwright.sync_api import expect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--axe')
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    axe = Path(args.axe).resolve().read_text('utf-8') if args.axe else None
    SessionDUiTests.setUpClass()
    case = SessionDUiTests()
    case.setUp()
    results = {'browser':case.browser.version, 'axe':[], 'logos':{}}
    try:
        for path, selector, name in (
            ('/admin/profile','#profileForm','profile'),
            ('/admin/?share=' + str(case.cats[0]),'.share-pick','dashboard'),
            ('/admin/dates','.controls .filters','dates'),
            ('/admin/dates/' + str(case.date) + '/edit','.chips-cat','editor'),
        ):
            case.goto(path)
            node = case.page.locator(selector)
            (output / (name + '-ax.yml')).write_text(node.aria_snapshot(), encoding='utf-8')
            node.screenshot(path=str(output / (name + '.png')))
            if axe:
                case.page.evaluate(axe)
                audit = case.page.evaluate('''selector => axe.run(document.querySelector(selector), {
                    runOnly: {type:'rule', values:['label','select-name','aria-input-field-name',
                    'aria-allowed-attr','aria-valid-attr-value','duplicate-id-aria']}
                })''', selector)
                results['axe'].append({'page':name, 'version':audit['testEngine']['version'],
                                       'violations':audit['violations'], 'incomplete':audit['incomplete']})
        case.context.clear_cookies()
        cdp = case.context.new_cdp_session(case.page)
        cdp.send('Network.enable')
        cdp.send('Network.clearBrowserCache')
        cdp.send('Network.emulateNetworkConditions', {'offline':False, 'latency':150,
                 'downloadThroughput':1600000/8, 'uploadThroughput':750000/8})
        case.goto('/c/session-d-0')
        case.page.wait_for_timeout(1000)
        resources = "() => performance.getEntriesByType('resource').filter(e => /logo-(standard|romantic)\\.png/.test(e.name)).map(e=>({name:e.name.split('/').pop(),bytes:e.encodedBodySize,start:e.startTime,end:e.responseEnd}))"
        results['logos']['closed'] = case.page.evaluate(resources)
        started = case.page.evaluate('performance.now()')
        case.page.locator('#loginOpen').click()
        expect(case.page.locator('#loginDlg')).to_be_visible()
        results['logos']['open_ms'] = case.page.evaluate('performance.now()') - started
        active = case.page.locator('.auth-module__logo-standard')
        expect(active).to_have_attribute('src', re.compile('logo-standard'))
        expect(active).to_have_js_property('complete', True)
        results['logos']['ready_ms'] = case.page.evaluate('performance.now()') - started
        results['logos']['first_open'] = case.page.evaluate(resources)
        case.page.locator('#loginDlg').screenshot(path=str(output / 'login-first-open.png'))
        if axe:
            case.page.evaluate(axe)
            audit = case.page.evaluate("axe.run(document.querySelector('#loginDlg'), {runOnly:{type:'rule',values:['aria-dialog-name','button-name','aria-allowed-attr']}})")
            results['axe'].append({'page':'login', 'version':audit['testEngine']['version'],
                                   'violations':audit['violations'], 'incomplete':audit['incomplete']})
        results['page_errors'] = case.errors
        (output / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(results, ensure_ascii=False), flush=True)
    finally:
        case.doCleanups()
        SessionDUiTests.tearDownClass()


if __name__ == '__main__':
    main()
