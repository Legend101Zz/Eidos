"""Exercise local grader using locally trained reference controls."""
import json,shutil,sys
from pathlib import Path
from study.launcher import prepare
from study.grade_block import grade_run
from study.common import write_json
root=Path(sys.argv[1]); controls=Path(sys.argv[2]); rows=[]
for card in ('D03','D04'):
 for case in ('planted','baseline','invalid'):
  run=root/card/case
  prepare(dict(arm=1,task=card,run=1,block_id=f'{card}-{case}'),run)
  shutil.copytree(controls/card/case/'out',run/'out',dirs_exist_ok=True)
  write_json(run/'run_record.json',dict(status='ok'))
  row=grade_run(run,root/'grading'/card/case);rows.append(row)
  expected='invalid_output' if case=='invalid' else 'valid'
  assert row['status']==expected,(card,case,row)
  assert row['verified_success']==(case=='planted'),(card,case,row)
write_json(root/'results.json',rows)
print('PASS: D03/D04 planted passes, baseline fails, invalid candidate failure')
