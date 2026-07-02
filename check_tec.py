import json
p = json.load(open('/tmp/tec_bounded.json'))
print('total_events:', p.get('total_events'))
print('total_pages:', p.get('total_pages'))
print('count:', len(p.get('events', [])))
if p.get('events'):
    print('first title:', p['events'][0]['title'])
    print('first date:', p['events'][0].get('start_date'))
    print('last title:', p['events'][-1]['title'])
    print('last date:', p['events'][-1].get('start_date'))
