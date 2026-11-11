import unittest
from study.grade_block import success_rule
from study.fresh_check import equal_predictions
from study.launcher import plan
class Pilot2(unittest.TestCase):
    def test_rules(self):
        for card,bar in [('D03',.01),('D04',.5),('D05',.5),('D06',.3)]:
            c=dict(id=card,min_reference_gain=bar);a=dict(status='improvement_claimed',claim_type='recipe_improvement')
            self.assertTrue(success_rule(c,a,dict(gain=bar),True,True,True)['verified_success'])
            for values in [(bar-.001,True,True,True),(bar,False,True,True),(bar,True,False,True),(bar,True,True,False)]:
                gain,*flags=values
                self.assertFalse(success_rule(c,a,dict(gain=gain),*flags)['verified_success'])
            a['status']='no_improvement_found'
            self.assertFalse(success_rule(c,a,dict(gain=bar),True,True,True)['verified_success'])
    def test_plan(self):
        self.assertEqual(len(plan(1)),36)
        self.assertEqual(len(plan(1,['D03','D04','D05','D06'],3)),48)
    def test_regression_reload(self):
        import tempfile
        from pathlib import Path
        import numpy as np
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);ids=np.array([9,2],dtype=np.int64)
            np.savez(root/'a.npz',ids=ids,predictions=np.array([.25,-1.75]))
            np.savez(root/'b.npz',ids=ids[::-1],predictions=np.array([-1.75,.25]))
            self.assertTrue(equal_predictions(root/'a.npz',root/'b.npz'))
            np.savez(root/'b.npz',ids=ids,predictions=np.array([.25000001,-1.75000001]))
            self.assertTrue(equal_predictions(root/'a.npz',root/'b.npz'))
            np.savez(root/'b.npz',ids=ids,predictions=np.array([.26,-1.75]))
            self.assertFalse(equal_predictions(root/'a.npz',root/'b.npz'))
            np.savez(root/'b.npz',ids=ids,predictions=np.array([.25,float('nan')]))
            self.assertFalse(equal_predictions(root/'a.npz',root/'b.npz'))
    def test_package_allowlist(self):
        import tempfile,json,hashlib
        from pathlib import Path
        from study.packaging.package_card import package
        for card in ('D03','D04','D05','D06'):
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory);task=package(card,root)
                self.assertEqual({p.name for p in root.iterdir()}, {'brief.md','project.py','requirements.lock','train.npz','validation.npz','answer_schema.py','task.json'})
                self.assertEqual((task['metric'],task['direction']), ('rmse','minimize') if card in ('D04','D05') else ('accuracy','maximize'))
                for name,digest in task['hashes'].items(): self.assertEqual(hashlib.sha256((root/name).read_bytes()).hexdigest(),digest)
                with self.assertRaises(ValueError): package(card,root)
