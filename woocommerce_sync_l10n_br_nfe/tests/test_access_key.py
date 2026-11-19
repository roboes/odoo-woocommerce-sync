from odoo.tests import TransactionCase, tagged

from ..utils import is_nfe_access_key, normalize_nfe_access_key


@tagged('post_install', '-at_install')
class TestNFeAccessKey(TransactionCase):
    def test_accepts_model_55_access_key(self):
        self.assertTrue(is_nfe_access_key('35240612345678000190550010000001231234567890'))

    def test_rejects_other_document_models_and_malformed_keys(self):
        self.assertFalse(is_nfe_access_key('35240612345678000190650010000001231234567890'))
        self.assertFalse(is_nfe_access_key('3524061234567800019055001000000123123456789'))
        self.assertFalse(is_nfe_access_key(None))

    def test_normalizes_grouped_keys_for_woocommerce_note(self):
        self.assertEqual(
            normalize_nfe_access_key('3524 0612 3456 7800 0190 5500 1000 0001 2312 3456 7890'),
            '35240612345678000190550010000001231234567890',
        )
