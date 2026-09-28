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
import uuid

# Django
from django.urls import reverse

# wger
from wger.core.tests.base_testcase import WgerTestCase


class ReactWorkoutRoutesTestCase(WgerTestCase):
    """
    Deep links used by the workout UI must be served by Django (a reload would
    404 otherwise) and stay behind login.
    """

    def urls(self):
        return [
            reverse('manager:routine:workouts'),
            reverse('manager:routine:quick'),
            reverse('manager:routine:session', kwargs={'sessionId': uuid.uuid4()}),
        ]

    def test_anonymous_is_sent_to_login(self):
        for url in self.urls():
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertTrue(response['Location'].startswith('/user/login?next='), url)

    def test_logged_in_user_gets_the_react_shell(self):
        self.user_login('test')
        for url in self.urls():
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_session_route_requires_a_uuid(self):
        self.user_login('test')
        self.assertEqual(self.client.get('/en/routine/session/123').status_code, 404)
