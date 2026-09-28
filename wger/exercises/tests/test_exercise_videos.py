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

# Django
from django.urls import reverse

# wger
from wger.core.tests import api_base_test
from wger.exercises.models import ExerciseVideo
from wger.exercises.tests.api_mixins import ActstreamUpdateMixin


# TODO: add POST and DELETE tests
class ExerciseVideosApiTestCase(
    ActstreamUpdateMixin,
    api_base_test.BaseTestCase,
    api_base_test.ApiBaseTestCase,
    api_base_test.ApiGetTestCase,
):
    """
    Tests the exercise video resource
    """

    pk = 1
    private_resource = False
    resource = ExerciseVideo
    overview_cached = False
    data = {'is_main': True}
    patch_format = 'multipart'

    def get_resource_name(self):
        # The video endpoint is registered as ``video``, not ``exercisevideo``.
        return 'video'


class LinkedExerciseVideoApiTestCase(api_base_test.BaseTestCase, api_base_test.ApiBaseTestCase):
    """
    Linked (source_url) videos are created over JSON and recorded in history
    """

    def test_create_linked_video_over_json(self):
        self.authenticate('admin')
        url = 'https://videos.example.test/squat.mp4'
        response = self.client.post(
            reverse('video-list'),
            {'exercise': 1, 'source_url': url, 'is_main': False},
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()['video'], url)
        video = ExerciseVideo.objects.get(pk=response.json()['id'])
        self.assertEqual(video.history.get().source_url, url)
