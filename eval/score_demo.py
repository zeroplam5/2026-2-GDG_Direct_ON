"""DEMO-only speed/transport summary. No LLM judgment or official accuracy scoring."""
import argparse, json, math
from pathlib import Path
TABLE=[(1.5,100),(2.0,90),(2.5,80),(3.0,70),(3.5,60),(4.0,50),(5.0,30),(6.0,10)]
def pts(t):
    if t is None or not isinstance(t,(int,float)) or not math.isfinite(t) or t<0:return 0
    return next((v for bound,v in TABLE if t<=bound),0)
def main():
    p=argparse.ArgumentParser();p.add_argument('--run',required=True);a=p.parse_args()
    f=Path(a.run)/'results.jsonl'; rows=[json.loads(x) for x in f.read_text().splitlines() if x.strip()]
    for r in rows:
        bad=r.get('spoke_before_end') or 'error' in r or r.get('turn_end') is None or r.get('turn_end',999)>25
        sp=0 if bad else pts(r.get('first_sound'))
        print(f"{r['case_id']}: first_sound={r.get('first_sound')}s | speed={sp} | calls={len(r.get('tool_calls',[]))} | error={r.get('error','-')}")
    print('\n[중요] 이것은 통신·속도 확인 전용입니다. 공식 평가 점수/응대 정확도가 아닙니다.')
if __name__=='__main__':main()
