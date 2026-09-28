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
from django.core.files.uploadedfile import SimpleUploadedFile
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
    Videos are either an upload or a link (source_url), never neither or both
    """

    link = 'https://videos.example.test/squat.mp4'

    def setUp(self):
        super().setUp()
        self.authenticate('admin')

    def upload(self):
        return SimpleUploadedFile('squat.mp4', b'\x00' * 16, content_type='video/mp4')

    def create_linked(self):
        return ExerciseVideo.objects.create(exercise_id=1, source_url=self.link, license_id=1)

    def test_create_linked_video_over_json(self):
        response = self.client.post(
            reverse('video-list'),
            {'exercise': 1, 'source_url': self.link, 'is_main': False},
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()['video'], self.link)
        video = ExerciseVideo.objects.get(pk=response.json()['id'])
        self.assertEqual(video.history.get().source_url, self.link)

    def test_create_uploaded_video_over_multipart(self):
        response = self.client.post(
            reverse('video-list'),
            {'exercise': 1, 'video': self.upload(), 'is_main': False},
            format='multipart',
        )
        self.assertEqual(response.status_code, 201, response.content)
        video = ExerciseVideo.objects.get(pk=response.json()['id'])
        self.assertTrue(video.video.name.endswith('.mp4'))
        self.assertEqual(video.source_url, '')
        self.assertEqual(response.json()['video'], f'http://testserver{video.video.url}')

    def test_create_without_upload_or_link_is_rejected(self):
        before = ExerciseVideo.objects.count()
        for fmt in ('json', 'multipart'):
            for data in ({'exercise': 1}, {'exercise': 1, 'source_url': ''}):
                response = self.client.post(reverse('video-list'), data, format=fmt)
                self.assertEqual(response.status_code, 400, (fmt, data, response.content))
        self.assertEqual(ExerciseVideo.objects.count(), before)

    def test_create_with_upload_and_link_is_rejected(self):
        before = ExerciseVideo.objects.count()
        response = self.client.post(
            reverse('video-list'),
            {'exercise': 1, 'video': self.upload(), 'source_url': self.link},
            format='multipart',
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(ExerciseVideo.objects.count(), before)

    def test_partial_update_keeps_existing_link(self):
        video = self.create_linked()
        response = self.client.patch(
            reverse('video-detail', kwargs={'pk': video.pk}), {'is_main': True}, format='json'
        )
        self.assertEqual(response.status_code, 200, response.content)
        video.refresh_from_db()
        self.assertTrue(video.is_main)
        self.assertEqual(video.source_url, self.link)

    def test_partial_update_replaces_link(self):
        video = self.create_linked()
        new_link = 'https://videos.example.test/lunge.mp4'
        response = self.client.patch(
            reverse('video-detail', kwargs={'pk': video.pk}),
            {'source_url': new_link},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        video.refresh_from_db()
        self.assertEqual(video.source_url, new_link)

    def test_partial_update_keeps_existing_upload(self):
        response = self.client.patch(
            reverse('video-detail', kwargs={'pk': 1}), {'is_main': False}, format='multipart'
        )
        self.assertEqual(response.status_code, 200, response.content)
        video = ExerciseVideo.objects.get(pk=1)
        self.assertFalse(video.is_main)
        self.assertEqual(video.video.name, 'exercise-images/1/protestschwein.jpg')

    def test_partial_update_clearing_link_is_rejected(self):
        video = self.create_linked()
        response = self.client.patch(
            reverse('video-detail', kwargs={'pk': video.pk}), {'source_url': ''}, format='json'
        )
        self.assertEqual(response.status_code, 400, response.content)
        video.refresh_from_db()
        self.assertEqual(video.source_url, self.link)

    def test_partial_update_adding_link_to_upload_is_rejected(self):
        response = self.client.patch(
            reverse('video-detail', kwargs={'pk': 1}), {'source_url': self.link}, format='json'
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(ExerciseVideo.objects.get(pk=1).source_url, '')
