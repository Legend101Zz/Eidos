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
