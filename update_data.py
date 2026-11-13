#!/usr/bin/env python3
"""Refresh North Anna gauges on GitHub Actions, writing same-origin JSON for Pages.

No dependencies, API keys, or private tokens. On failure retains a previously
successful snapshot instead of replacing it with empty measurements.
"""
import datetime as dt
import json
import os
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
START = (NOW - dt.timedelta(days=1835)).strftime('%Y-%m-%d')
END = NOW.strftime('%Y-%m-%d')


def get_json(url, attempts=2):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'NorthAnnaRiverWatch/9.0 (public GitHub Pages water-monitoring viewer)',
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
    params = urllib.parse.urlencode({'format':'json','sites':site,
                                      'startDT': (NOW-dt.timedelta(days=days)).strftime('%Y-%m-%d'),
                                      'endDT': END})
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
    bins = {}
    for p in record['points']:
        try:
            d = dt.datetime.fromisoformat(p['time'].replace('Z','+00:00')).astimezone(ZoneInfo('America/New_York')).date().isoformat()
        except (ValueError, TypeError): continue
        bins.setdefault(d, []).append(p['value'])
    return [{'time':d+'T12:00:00-05:00', 'value':round(sum(vals)/len(vals),5)} for d,vals in sorted(bins.items()) if vals]


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
              'description':'Official USGS daily values; derived daily averages of continuous sensor readings are labeled.',
              'sites':{}}
    successes = 0
    for site in SITES:
        daily,err_d = retrieve(site,'daily',1835)
        # Limited sensor period avoids downloading 35,000+ readings per year per parameter.
        sensors,err_s = retrieve(site,'continuous',35)
        stats = {s['code'] for s in daily}
        derived = []
        for sensor in sensors:
            if sensor['code'] in stats: continue
            points = daily_avg_from_sensors(sensor)
            if points:
                derived.append({'code':sensor['code'],'stat':'','unit':sensor['unit'],
                                'name':sensor['name'],'source':'Derived daily average (automated sensor)',
                                'sensorOnly':True,'points':points})
        historical = normalize(daily + derived)
        current = normalize(sensors)
        if historical or current:
            result['sites'][site] = {'updated_at':NOW.isoformat().replace('+00:00','Z'),
                                      'daily':historical,'continuous':current,
                                      'errors':[e for e in (err_d,err_s) if e]}
            successes += 1
        else:
            existing = (previous.get('sites') or {}).get(site)
            if existing and (existing.get('daily') or existing.get('continuous')):
                result['sites'][site] = {**existing,'errors':['Latest refresh failed; showing previously saved USGS data.',err_d,err_s]}
            else:
                result['sites'][site] = {'updated_at':None,'daily':[],'continuous':[],
                                          'errors':[err_d,err_s]}
        print(site, 'daily types:',len(result['sites'][site]['daily']), 'sensor types:',len(result['sites'][site]['continuous']),
              'updated:',result['sites'][site]['updated_at'], flush=True)
    OUT.write_text(json.dumps(result,ensure_ascii=False,separators=(',',':'))+'\n',encoding='utf-8')
    print('Saved',OUT,'successful site updates:',successes, flush=True)
    if not successes:
        raise SystemExit('All USGS queries failed; preserved any prior readings. See workflow log.')

if __name__ == '__main__':
    main()
