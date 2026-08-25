#!/bin/bash
# bluray-mkv-fidelity 项目监控脚本
# 用法: bash monitor.sh

REPO="principal0324/bluray-mkv-fidelity"
DATA_FILE="$HOME/.bluray-fidelity-monitor.json"

# 获取当前数据
CURRENT=$(curl -s "https://api.github.com/repos/$REPO" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(json.dumps({
    'stars': d['stargazers_count'],
    'forks': d['forks_count'],
    'watchers': d['subscribers_count'],
    'issues': d['open_issues_count']
}))
")

# 读取上次数据
if [ -f "$DATA_FILE" ]; then
    PREV=$(cat "$DATA_FILE")
else
    PREV='{"stars":0,"forks":0,"watchers":0,"issues":0}'
fi

# 对比并显示
python3 -c "
import json, sys

curr = json.loads('$CURRENT')
prev = json.loads('$PREV')

print('=' * 40)
print('bluray-mkv-fidelity 项目状态')
print('=' * 40)
print(f'⭐ Stars:   {curr[\"stars\"]}  (变化: {curr[\"stars\"] - prev[\"stars\"]:+d})')
print(f'🍴 Forks:   {curr[\"forks\"]}  (变化: {curr[\"forks\"] - prev[\"forks\"]:+d})')
print(f'👀 Watchers: {curr[\"watchers\"]}  (变化: {curr[\"watchers\"] - prev[\"watchers\"]:+d})')
print(f'🐛 Issues:  {curr[\"issues\"]}  (变化: {curr[\"issues\"] - prev[\"issues\"]:+d})')
print('=' * 40)

# 检查是否有变化
has_change = any(curr[k] != prev[k] for k in ['stars', 'forks', 'watchers', 'issues'])
if has_change:
    print('📢 有新动态！')
else:
    print('😴 暂无变化')
"

# 保存当前数据
echo "$CURRENT" > "$DATA_FILE"
