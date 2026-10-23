"""P2: an expired access token is renewed with the refresh token, so a signed-in
user is not logged out every ACCESS_TOKEN_LIFETIME (60 min).

The frontend (services/api.js) does this on the first 401: it posts the stored
refresh token to /auth/refresh/ once and replays the request with the new
access token. These tests pin the server half of that contract.
"""
import uuid
from datetime import timedelta

from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from apps.accounts.models import Client, User


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class TokenRefreshTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(slug=uid("refresh"), name="Refresh Tenant")
        self.email = f"{uid('u')}@example.com"
        User.objects.create_user(email=self.email, password="Pass1234!", client=self.tenant, name="Refresh User")
        self.api = APIClient()

    def login(self):
        resp = self.api.post("/api/v1/auth/login/", {"email": self.email, "password": "Pass1234!"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        return resp.data["access"], resp.data["refresh"]

    def me(self, access):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return client.get("/api/v1/auth/me/")

    def expired(self, access):
        token = AccessToken(access)
        token.set_exp(lifetime=-timedelta(minutes=1))
        return str(token)

    def test_expired_access_is_401_and_refresh_issues_a_working_token(self):
        access, refresh = self.login()
        self.assertEqual(self.me(access).status_code, 200)
        self.assertEqual(self.me(self.expired(access)).status_code, 401)

        resp = self.api.post("/api/v1/auth/refresh/", {"refresh": refresh}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertEqual(self.me(resp.data["access"]).status_code, 200)

        # Rotation is off, so the same refresh token keeps working.
        again = self.api.post("/api/v1/auth/refresh/", {"refresh": refresh}, format="json")
        self.assertEqual(again.status_code, 200, again.content[:300])

    def test_bad_or_signed_out_refresh_token_is_401(self):
        resp = self.api.post("/api/v1/auth/refresh/", {"refresh": "not-a-token"}, format="json")
        self.assertEqual(resp.status_code, 401)

        access, refresh = self.login()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        client.post("/api/v1/auth/logout/", {"refresh": refresh}, format="json")
        resp = self.api.post("/api/v1/auth/refresh/", {"refresh": refresh}, format="json")
        self.assertEqual(resp.status_code, 401, resp.content[:300])
