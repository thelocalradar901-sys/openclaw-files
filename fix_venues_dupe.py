"""Remove duplicate venues() method from openclaw-monitor.php"""
f = '/var/www/html/wp-content/plugins/openclaw-monitor/openclaw-monitor.php'
content = open(f).read()
before = content.count('private function venues()')
# Find and remove second occurrence of the entire venues() method block
import re
pattern = r'\n\n    private function venues\(\) \{[^}]+\}[^}]*\}'
matches = [(m.start(), m.end()) for m in re.finditer(pattern, content)]
if len(matches) >= 2:
    s, e = matches[1]
    content = content[:s] + content[e:]
    open(f, 'w').write(content)
    print(f"Fixed! venues() count: {before} → {content.count('private function venues()')}")
else:
    print(f"venues() count is {before} — nothing to fix")
