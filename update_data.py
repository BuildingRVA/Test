#!/usr/bin/env python3
"""Refresh North Anna gauges on GitHub Actions, writing same-origin JSON for Pages.

No dependencies, API keys, or private tokens. On failure retains a previously
successful snapshot instead of replacing it with empty measurements.
"""
import datetime as dt
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'north-anna-data.json'
SITES = ('016701405', '01670400', '01671020')
LABELS = {'00060':'Streamflow','00065':'Gage height','00010':'Water temperature',
          '00095':'Specific conductance','00300':'Dissolved oxygen','00400':'pH',
          '63680':'Turbidity','00045':'Precipitation','00062':'Elevation','00025':'Air pressure',
          '00020':'Air temperature'}
UTC = dt.timezone.utc
NOW = dt.datetime.now(UTC)
START = (NOW - dt.timedelta(days=370)).strftime('%Y-%m-%d')
END = NOW.strftime('%Y-%m-%d')


def get_json(url, attempts=2):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'NorthAnnaRiverWatch/11.0 (public GitHub Pages water-monitoring viewer)',
        'Accept': 'application/json',
    })
    last = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=32) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(2 + attempt * 3)
    raise RuntimeError(str(last))


def fetch_legacy(site, dataset, days):
    return fetch_legacy_window(site, dataset, (NOW-dt.timedelta(days=days)).date(), NOW.date())


def fetch_legacy_window(site, dataset, start, end):
    params = urllib.parse.urlencode({'format':'json','sites':site,
                                      'startDT':start.isoformat(), 'endDT':end.isoformat()})
    endpoint = 'dv' if dataset == 'daily' else 'iv'
    data = get_json(f'https://waterservices.usgs.gov/nwis/{endpoint}/?{params}')
    series = data.get('value', {}).get('timeSeries', [])
    result = []
    for record in series:
        v = record.get('variable') or {}
        codes = v.get('variableCode') or []
        code = str(codes[0].get('value','')) if codes else ''
        if not code: continue
        options = (v.get('options') or {}).get('option') or []
        stat = next((str(opt.get('value')) for opt in options if str(opt.get('value','')).isdigit() and len(str(opt.get('value'))) == 5), '')
        if dataset == 'daily' and not stat: stat = '00003'
        if dataset == 'continuous': stat = ''
        unit = (v.get('unit') or {}).get('unitCode') or (v.get('unit') or {}).get('unitName') or ''
        name = LABELS.get(code) or v.get('variableName') or ('Parameter ' + code)
        points = []
        for block in record.get('values', []):
            for item in block.get('value', []):
                try:
                    n = float(item['value'])
                    if not (-1e35 < n < 1e35): continue
                    t = item['dateTime']
                except (TypeError, ValueError, KeyError): continue
                points.append({'time':t, 'value':n})
        if points:
            result.append({'code':code,'stat':stat,'unit':unit,'name':name,'points':points,'source':'USGS'})
    return result


def fetch_modern(site, dataset, days):
    params = {'f':'json','monitoring_location_id':'USGS-'+site,
              'datetime':f'{(NOW-dt.timedelta(days=days)).isoformat(timespec="seconds")}/{NOW.isoformat(timespec="seconds")}',
              'limit':'5000'}
    out = {}
    base = 'https://api.waterdata.usgs.gov/ogcapi/v1/collections/' + dataset + '/items'
    url = base + '?' + urllib.parse.urlencode(params)
    for _ in range(20):
        obj = get_json(url)
        for feature in obj.get('features', []):
            p = feature.get('properties') or {}
            code = str(p.get('parameter_code') or '')
            if not code: continue
            stat = str(p.get('statistic_id') or '') if dataset == 'daily' else ''
            if dataset == 'daily' and not stat: stat = '00003'
            t = p.get('time')
            try: v = float(p.get('value'))
            except (ValueError, TypeError): continue
            if not t or not (-1e35 < v < 1e35): continue
            key = code+':'+stat
            if key not in out:
                out[key] = {'code':code,'stat':stat,'unit':p.get('unit_of_measure') or '',
                            'name':LABELS.get(code) or p.get('parameter_name') or ('Parameter '+code),
                            'points':[],'source':'USGS'}
            out[key]['points'].append({'time':t,'value':v})
        nxt = next((link.get('href') for link in obj.get('links', []) if link.get('rel') == 'next'), None)
        if not nxt: break
        url = urllib.parse.urljoin(url, nxt)
    return list(out.values())


def retrieve(site, dataset, days):
    errors = []
    for func in ((fetch_legacy, fetch_modern) if dataset == 'daily' else (fetch_modern, fetch_legacy)):
        try:
            result = func(site,dataset,days)
            if result: return result, None
            errors.append(func.__name__ + ': no observations')
        except Exception as exc:
            errors.append(func.__name__+': '+str(exc)[:110])
    return [], '; '.join(errors)


def daily_avg_from_sensors(record):
    """Mean sensor observations by local calendar day; these are NOT official USGS daily values."""
    bins = {}
    for point in record['points']:
        try:
            d = dt.datetime.fromisoformat(point['time'].replace('Z','+00:00')).astimezone(ZoneInfo('America/New_York')).date().isoformat()
            v = float(point['value'])
        except (ValueError, TypeError, KeyError):
            continue
        bins.setdefault(d, []).append(v)
    return [{'time':day+'T12:00:00Z', 'value':round(sum(vals)/len(vals),5)}
            for day,vals in sorted(bins.items()) if vals]


def months_in_range(start, end):
    d = start.replace(day=1)
    while d <= end:
        if d.month == 12: nxt = d.replace(year=d.year+1,month=1)
        else: nxt = d.replace(month=d.month+1)
        yield d, min(nxt-dt.timedelta(days=1),end)
        d = nxt


def combine_official(old, fresh):
    """Keep prior valid observations if USGS temporarily returns a shorter range."""
    bykey = {}
    for record in old + fresh:
        k = record['code']+':'+str(record.get('stat') or '00003')
        slot = bykey.setdefault(k, {k:v for k,v in record.items() if k != 'points'})
        slot.setdefault('points',[])
        slot['points'].extend(record.get('points') or [])
    return normalize(list(bykey.values()))


def sensor_daily_history(site, sensors, official_codes, previous_site):
    """Backfill sensor-only parameters in monthly windows and cache completed months.

    Full-year IV requests can be oversized or limited by USGS. Earlier months are
    fetched once, then cached; recent months are rechecked regularly.
    """
    wanted = {s['code'] for s in sensors if s['code'] not in official_codes}
    old_derived = [x for x in previous_site.get('daily',[]) if x.get('sensorOnly')]
    wanted.update(x['code'] for x in old_derived if x['code'] not in official_codes)
    existing = {x['code']:x for x in old_derived if x['code'] in wanted}
    checks = dict(previous_site.get('sensor_month_checks') or {})
    since = (NOW-dt.timedelta(days=365)).date()
    recent_cutoff = NOW.date()-dt.timedelta(days=40)
    bins = {code:{p['time'][:10]:p for p in rec.get('points',[]) if p.get('time') and p['time'][:10]>=since.isoformat()}
            for code,rec in existing.items()}
    errors = []
    # A first-time backfill may require 13 monthly queries per gauge. Thereafter
    # closed months are refreshed at most once every 14 days; only the last 40 days are refreshed each run.
    for first,last in months_in_range(since,NOW.date()):
        key = first.strftime('%Y-%m')
        recent = last >= recent_cutoff
        previous_check = checks.get(key)
        if previous_check and not recent:
            try:
                prior = dt.datetime.fromisoformat(previous_check.replace('Z','+00:00'))
                if (NOW-prior).days < 14: continue
            except (TypeError,ValueError): pass
        try:
            month = fetch_legacy_window(site,'continuous',max(first,since),last)
            # Only fall back to the modern service when the legacy service fails,
            # not when USGS reports genuinely empty periods.
            for series in month:
                code = series['code']
                if code not in wanted: continue
                parsed = daily_avg_from_sensors(series)
                slot = bins.setdefault(code,{})
                for value in parsed: slot[value['time'][:10]]=value
            checks[key] = NOW.isoformat().replace('+00:00','Z')
        except Exception as exc:
            errors.append(key+': '+str(exc)[:90])
    # Ensure last few days are as fresh as the sensor snapshot we already obtained.
    for sensor in sensors:
        code=sensor['code']
        if code not in wanted: continue
        slot=bins.setdefault(code,{})
        for value in daily_avg_from_sensors(sensor): slot[value['time'][:10]]=value
    # Remove checks outside the rolling year.
    checks = {k:v for k,v in checks.items() if k >= since.strftime('%Y-%m')}
    derived=[]
    for code in sorted(wanted):
        points=sorted(bins.get(code,{}).values(),key=lambda x:x['time'])
        if not points: continue
        meta=next((s for s in sensors if s['code']==code),existing.get(code,{}))
        derived.append({'code':code,'stat':'','unit':meta.get('unit',''),
                        'name':meta.get('name') or LABELS.get(code) or 'Parameter '+code,
                        'source':'Derived daily average (automated sensor)',
                        'sensorOnly':True,'points':points})
    return derived,checks,errors


def normalize(records):
    output = []
    for record in records:
        points = {str(x['time']):x for x in record['points']}
        record['points'] = sorted(points.values(),key=lambda x:x['time'])
        if not record['points']: continue
        last = record['points'][-1]
        record['value'] = last['value']
        record['time'] = last['time']
        output.append(record)
    return output


def main():
    try:
        previous = json.loads(OUT.read_text(encoding='utf-8')) if OUT.exists() else {}
    except (OSError, ValueError):
        previous = {}
    result = {'generated_at':NOW.isoformat().replace('+00:00','Z'),
              'description':'Official USGS daily values; derived daily averages from automated sensors where daily values are not available.',
              'sites':{}}
    successes = 0
    for site in SITES:
        prior = (previous.get('sites') or {}).get(site) or {}
        # Daily official values are fetched over the whole year in one request.
        daily,err_d = retrieve(site,'daily',370)
        prior_official = [r for r in prior.get('daily',[]) if not r.get('sensorOnly')]
        official = combine_official(prior_official,daily) if daily else normalize(prior_official)
        official_codes = {s['code'] for s in official}
        # These observations power the current-value cards; the historical
        # sensor-only values are backfilled separately in manageable months.
        sensors,err_s = retrieve(site,'continuous',7)
        current = normalize(sensors) if sensors else prior.get('continuous',[])
        derived,checks,backfill_errors = sensor_daily_history(site,sensors,official_codes,prior)
        historical = normalize(official+derived)
        if daily or sensors or backfill_errors==[] and derived:
            updated = NOW.isoformat().replace('+00:00','Z') if (daily or sensors) else prior.get('updated_at')
            result['sites'][site] = {
                'updated_at':updated, 'daily':historical,'continuous':current,
                'sensor_month_checks':checks,
                'errors':[e for e in (err_d,err_s) if e]+(['Some historical sensor requests failed: '+'; '.join(backfill_errors[:3])] if backfill_errors else [])
            }
            if daily or sensors: successes += 1
        else:
            result['sites'][site] = {
                'updated_at':prior.get('updated_at'), 'daily':historical,
                'continuous':current, 'sensor_month_checks':checks,
                'errors':['Refresh failed; kept previously saved observations.',err_d,err_s]
            }
        for r in historical:
            dates = [p['time'][:10] for p in r['points']]
            print(site,r['code'],r.get('stat',''),'derived' if r.get('sensorOnly') else 'official',
                  'days:',len(dates),'from:',min(dates) if dates else 'none',
                  'to:',max(dates) if dates else 'none',flush=True)
        print(site, 'current sensor types:',len(current),
              'historical fetch issues:',len(backfill_errors),flush=True)
    OUT.write_text(json.dumps(result,ensure_ascii=False,separators=(',',':'))+'\n',encoding='utf-8')
    print('Saved',OUT,'successful site updates:',successes, flush=True)
    if not successes:
        raise SystemExit('All USGS latest queries failed; saved previous readings. See workflow log.')

if __name__ == '__main__':
    main()
