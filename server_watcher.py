cd /home/ubuntu/auto-trade
sudo systemctl stop autotrade.service

# 1. paper_trades.json 내의 SWELL 포지션 청산 정리
python3 -c "
import json
with open('paper_trades.json', 'r') as f:
    db = json.load(f)
if 'SWELL' in db.get('active_positions', {}):
    pos = db['active_positions'].pop('SWELL')
    db['closed_trades'].append({
        'symbol': pos['symbol'],
        'entry_price': pos['entry_price'],
        'exit_price': pos['entry_price'],
        'buy_amount_krw': pos['buy_amount_krw'],
        'profit_krw': 0,
        'profit_pct': 0.0,
        'status': 'CLOSED_TIME_EXIT',
        'reason': '⏰ 18시간 최대 보유 시간 초과로 정리',
        'entry_time': pos['entry_time'],
        'exit_time': '2026-08-30T22:50:00+09:00'
    })
    with open('paper_trades.json', 'w') as f:
        json.dump(db, f, indent=2, ensure_ascii=False)
    print('SWELL 포지션 정리 완료')
"

# 2. 최신 코드 동기화 및 서비스 가동
git stash
git fetch origin
git reset --hard origin/main
sudo systemctl start autotrade.service
sudo systemctl status autotrade.service
