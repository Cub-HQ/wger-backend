# This file is part of wger Workout Manager.
#
# wger Workout Manager is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# wger Workout Manager is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License

# Standard Library
import datetime
from unittest.mock import patch

# Django
from django import forms
from django.test import (
    SimpleTestCase,
    override_settings,
)
from django.urls import reverse
from django.utils import (
    formats,
    translation,
)

# wger
from wger.core.tests.base_testcase import WgerTestCase
from wger.utils.pdf import render_footer


class AustralianFormatTestCase(SimpleTestCase):
    def test_en_au_uses_day_first_dates_and_accepts_both_input_orders(self):
        moment = datetime.datetime(2026, 3, 4, 17, 5)
        with translation.override('en-au'):
            self.assertEqual(formats.date_format(moment, 'SHORT_DATE_FORMAT'), '04/03/2026')
            self.assertEqual(formats.date_format(moment, 'SHORT_DATETIME_FORMAT'), '04/03/2026 17:05')
            field = forms.DateField()
            self.assertEqual(field.clean('04/03/2026'), datetime.date(2026, 3, 4))
            self.assertEqual(field.clean('2026-03-04'), datetime.date(2026, 3, 4))

    def test_pdf_footer_default_date_is_day_first(self):
        with patch('wger.utils.pdf.datetime') as mocked:
            mocked.date.today.return_value = datetime.date(2026, 3, 4)
            self.assertIn('04/03/2026', render_footer('https://gym.example/routine/1').text)


class FooterSourceLinksTestCase(WgerTestCase):
    @override_settings(
        WGER_SERVER_SOURCE_URL='https://example.test/server/abc',
        WGER_UI_SOURCE_URL='https://example.test/ui/def',
    )
    def test_footer_links_to_configured_source(self):
        response = self.client.get(reverse('software:about-us'))
        self.assertContains(response, 'href="https://example.test/server/abc"')
        self.assertContains(response, 'href="https://example.test/ui/def"')
