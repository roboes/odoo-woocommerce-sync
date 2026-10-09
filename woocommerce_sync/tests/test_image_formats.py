"""Tests for image formats accepted from WooCommerce: Odoo restricts Pillow to a few core formats ('odoo/tools/image.py' calls 'Image.preinit()'), so WebP must be registered explicitly by connector.py."""

from io import BytesIO
from unittest.mock import Mock, patch

from odoo.tests.common import tagged
from PIL import Image

from .common import WoocommerceSyncCommon


@tagged('post_install', '-at_install')
class TestImageFormats(WoocommerceSyncCommon):
    def test_webp_featured_image_is_processed(self):
        webp_image = BytesIO()
        Image.new('RGB', (2, 2), 'red').save(webp_image, format='WEBP')

        with patch.object(type(self.connector), 'image_download', Mock(return_value=Mock(content=webp_image.getvalue()))):
            image_base64 = self.connector.image_download_file_to_base64({'src': 'https://example.test/image.webp'})

        self.assertTrue(image_base64, 'A WebP image from WooCommerce should be converted, not dropped')
